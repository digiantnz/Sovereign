"""Ethereum Validator Monitor Adapter.

Query-only — never executes transactions or modifies validator config. No LLM calls —
fully deterministic.

Two validator populations, one beacon API: Rocket Pool minipools and eth-docker solo
validators are tracked together because the beacon chain doesn't care who staked a
given index — node02's beacon API (5052) answers status/balance queries for all of
them regardless of origin.

Rocket Pool-specific data (RPL stake, node withdrawal address) comes from on-chain
contract calls via RocketStorage's registry lookup, never a hardcoded sub-contract
address (those can change across protocol upgrades) and never the Smartnode HTTP API —
that API is bound to node02's loopback interface only (confirmed 2026-07-01; `docker ps -a`
showed `127.0.0.1:8280->8280/tcp`, and the `rocketpool service config` TUI has no
exposure toggle for it, unlike the beacon client). It will never be reachable from
sovereign-core.

Validator index list, node addresses, and confirmed withdrawal addresses are read from
`semantic:eth:validator-indices` at call time, never hardcoded — Director is planning a
"megapool" migration that will retire/replace minipools in tranches, so the tracked set
changes over time.

Attestation-effectiveness tracking was evaluated and dropped (2026-07-01) — beaconcha.in's
free tier is discontinued and a custom calculator was judged not worth the complexity for
this use case. Available manually via the existing Smartnode Grafana/Prometheus stack on
node02 if ever wanted back.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

import httpx
from eth_utils import keccak
from pydantic import BaseModel

from execution.adapters.qdrant import EPISODIC

logger = logging.getLogger(__name__)

_VALIDATOR_KEY = "semantic:eth:validator-indices"
_ROCKET_STORAGE = "0x1d8f8f00cfa6758d7bE78336684788Fb0ee0Fa46"
_GWEI = 1_000_000_000
_BALANCE_DROP_THRESHOLD_ETH = 0.01


class ValidatorAlert(BaseModel):
    severity: Literal["info", "warning", "critical"]
    validator_index: int | None
    category: Literal["balance_drop", "offline", "exit_detected", "sync_committee_active",
                       "sync_committee_upcoming", "rpl_reward_claimed"]
    message: str


class ValidatorStatus(BaseModel):
    index: int
    group: Literal["rocketpool", "eth_docker"]
    balance_eth: float
    balance_delta_eth: float
    sync_committee_active: bool
    sync_committee_upcoming: bool
    status: str


class ValidatorCheckResult(BaseModel):
    checked_at: datetime
    validator_count: int
    total_operator_eth: float
    total_balance_eth: float
    unrealised_reward_eth: float
    rpl_loose_at_node: float
    rpl_staked_total: float
    rpl_delta_since_last: float
    alerts: list[ValidatorAlert]
    per_validator: list[ValidatorStatus]


# ── ABI encoding helpers — no web3 dependency, matches the pattern proven during
# Phase B discovery (raw eth_call + manual encoding via eth_utils.keccak) ──────────

def _selector(sig: str) -> str:
    return keccak(text=sig)[:4].hex()


def _encode_address(addr: str) -> str:
    return addr[2:].lower().rjust(64, "0")


def _decode_address(hexdata: str) -> str:
    return "0x" + hexdata[-40:]


def _decode_uint(hexdata: str) -> int:
    return int(hexdata, 16)


class ValidatorMonitorAdapter:
    def __init__(self, qdrant):
        self.qdrant = qdrant

    async def _get_validator_config(self) -> dict:
        cfg = await self.qdrant.retrieve_by_key(_VALIDATOR_KEY)
        if not cfg:
            raise RuntimeError(f"{_VALIDATOR_KEY} not found in semantic memory — run Phase A seed first")
        return cfg

    async def _eth_call(self, rpc_url: str, to: str, data: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
            })
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"eth_call to {to} failed: {body['error']}")
            return body["result"]

    async def _rocket_contract(self, rpc_url: str, name: str) -> str:
        """Resolve a Rocket Pool contract address via RocketStorage's registry."""
        key = keccak(b"contract.address" + name.encode()).hex()
        raw = await self._eth_call(rpc_url, _ROCKET_STORAGE, "0x" + _selector("getAddress(bytes32)") + key)
        return _decode_address(raw)

    async def _beacon_get(self, beacon_url: str, path: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{beacon_url}{path}")
            r.raise_for_status()
            return r.json()

    # ── public methods ──────────────────────────────────────────────────────────

    async def get_validator_summary(self) -> dict:
        """All validators' status/balance/withdrawal-credentials via the beacon API.
        Works for both Rocket Pool and eth-docker validators."""
        cfg = await self._get_validator_config()
        beacon_url = cfg["beacon_url"]
        indices = cfg["validator_indices"]
        rp_indices = set(cfg["validator_groups"]["rocketpool"]["indices"])

        ids = ",".join(str(i) for i in indices)
        vdata = (await self._beacon_get(beacon_url, f"/eth/v1/beacon/states/head/validators?id={ids}"))["data"]

        per_validator = []
        total_balance = 0.0
        for v in vdata:
            idx = int(v["index"])
            balance_eth = int(v["balance"]) / _GWEI
            total_balance += balance_eth
            per_validator.append({
                "index": idx,
                "group": "rocketpool" if idx in rp_indices else "eth_docker",
                "balance_eth": balance_eth,
                "status": v["status"],
                "withdrawal_credentials": v["validator"]["withdrawal_credentials"],
                "activation_epoch": v["validator"].get("activation_epoch"),
                "exit_epoch": v["validator"].get("exit_epoch"),
            })

        return {
            "validator_count": len(per_validator),
            "total_balance_eth": total_balance,
            "total_operator_eth": cfg["total_operator_eth"],
            "per_validator": per_validator,
        }

    async def check_sync_committee_status(self) -> dict:
        """Current sync committee membership. 'upcoming' is always empty — Nimbus's
        standard API surface doesn't reliably support next-period look-ahead this far
        out, and this is a documented limitation, not a silently-wrong guess."""
        cfg = await self._get_validator_config()
        beacon_url = cfg["beacon_url"]
        indices = set(str(i) for i in cfg["validator_indices"])

        sc = (await self._beacon_get(beacon_url, "/eth/v1/beacon/states/head/sync_committees"))["data"]
        current = set(sc.get("validators", []))
        ours_active = sorted(int(i) for i in (indices & current))

        return {"active": ours_active, "upcoming": []}

    async def check_balance_changes(self, summary: dict | None = None,
                                     threshold_eth: float = _BALANCE_DROP_THRESHOLD_ETH) -> dict:
        """Compares current balances against the most recent prior check for each
        validator. This is what catches drift between runs — the daily scheduled
        check relies on this, not a continuous watcher."""
        if summary is None:
            summary = await self.get_validator_summary()

        deltas = []
        for v in summary["per_validator"]:
            prior = await self.qdrant.retrieve_by_key(f"episodic:eth:validator-last-balance:{v['index']}")
            prior_balance = prior["balance_eth"] if prior else v["balance_eth"]
            delta = v["balance_eth"] - prior_balance
            deltas.append({
                "index": v["index"],
                "balance_eth": v["balance_eth"],
                "balance_delta_eth": delta,
                "flagged": delta <= -threshold_eth,
            })
        return {"deltas": deltas}

    async def get_pending_rewards(self) -> dict:
        """Rocket Pool only — eth-docker sweeps automatically, nothing to check.

        Covers loose RPL at the node (ERC20 balanceOf) and total staked RPL
        (RocketNodeStaking.getNodeStakedRPL — note: NOT getNodeRPLStake, which
        reverts post-Saturn/megapool upgrade; getNodeStakedRPL is the correct
        current function, confirmed 2026-07-01 by cross-checking a live restake
        transaction's delta against the CLI's before/after figures).

        Interval claim rewards and minipool refunds still require the rocketpool
        CLI (not reachable programmatically — Smartnode API is loopback-only, see
        module docstring); surfaced as a known gap, not silently omitted.
        """
        cfg = await self._get_validator_config()
        rp = cfg["validator_groups"]["rocketpool"]
        rpc_url = f"http://{rp['node_host']}:8545"
        node_address = rp["node_address"]

        rpl_token = await self._rocket_contract(rpc_url, "rocketTokenRPL")
        raw = await self._eth_call(rpc_url, rpl_token, "0x" + _selector("balanceOf(address)") + _encode_address(node_address))
        rpl_loose = _decode_uint(raw) / 1e18

        node_staking = await self._rocket_contract(rpc_url, "rocketNodeStaking")
        raw2 = await self._eth_call(rpc_url, node_staking, "0x" + _selector("getNodeStakedRPL(address)") + _encode_address(node_address))
        rpl_staked = _decode_uint(raw2) / 1e18

        return {
            "rpl_loose_at_node": rpl_loose,
            "rpl_staked_total": rpl_staked,
            "note": ("Interval claim rewards and minipool refunds require the rocketpool CLI "
                     "on node02 — not reachable programmatically."),
        }

    async def run_full_check(self) -> ValidatorCheckResult:
        """Runs all checks, writes an episodic result, and returns structured alerts.

        RPL delta tracking records that a reward claim happened (loose + staked total
        increased since last check) without deciding tax treatment — restake-vs-withdraw
        income timing is a Director/accountant call, not something this adapter assumes.
        The report-time classifier is where that decision should be applied, same as
        every other tax classification in this system.
        """
        summary = await self.get_validator_summary()
        balance_changes = await self.check_balance_changes(summary=summary)
        sync = await self.check_sync_committee_status()
        rewards = await self.get_pending_rewards()

        delta_by_index = {d["index"]: d["balance_delta_eth"] for d in balance_changes["deltas"]}
        sync_active_set = set(sync["active"])

        alerts: list[ValidatorAlert] = []
        per_validator: list[ValidatorStatus] = []
        now = datetime.now(timezone.utc)

        for v in summary["per_validator"]:
            idx = v["index"]
            delta = delta_by_index.get(idx, 0.0)
            in_sync = idx in sync_active_set
            exit_epoch = v.get("exit_epoch")
            has_exited = exit_epoch not in (None, "18446744073709551615")

            if v["status"] != "active_ongoing":
                alerts.append(ValidatorAlert(
                    severity="critical", validator_index=idx, category="offline",
                    message=f"Validator {idx} status is {v['status']}, expected active_ongoing",
                ))
            if has_exited:
                alerts.append(ValidatorAlert(
                    severity="critical", validator_index=idx, category="exit_detected",
                    message=f"Validator {idx} has exit_epoch set: {exit_epoch}",
                ))
            if delta <= -_BALANCE_DROP_THRESHOLD_ETH:
                alerts.append(ValidatorAlert(
                    severity="warning", validator_index=idx, category="balance_drop",
                    message=f"Validator {idx} balance dropped {abs(delta):.5f} ETH since last check",
                ))
            if in_sync:
                alerts.append(ValidatorAlert(
                    severity="info", validator_index=idx, category="sync_committee_active",
                    message=f"Validator {idx} is in the current sync committee",
                ))

            per_validator.append(ValidatorStatus(
                index=idx, group=v["group"], balance_eth=v["balance_eth"], balance_delta_eth=delta,
                sync_committee_active=in_sync, sync_committee_upcoming=False, status=v["status"],
            ))

            # Stash this run's balance as "last known" for next run's delta comparison.
            await self.qdrant.store(
                content=f"Validator {idx} last-known balance: {v['balance_eth']:.5f} ETH",
                metadata={
                    "type": "episodic", "domain": "eth.validators",
                    "_key": f"episodic:eth:validator-last-balance:{idx}",
                    "balance_eth": v["balance_eth"], "checked_at": now.isoformat(),
                },
                collection=EPISODIC, writer="sovereign-core",
            )

        # RPL delta tracking — records that a claim happened, does not classify it.
        rpl_total_now = rewards["rpl_loose_at_node"] + rewards["rpl_staked_total"]
        prior_rpl = await self.qdrant.retrieve_by_key("episodic:eth:rpl-last-total")
        prior_rpl_total = prior_rpl["rpl_total"] if prior_rpl else rpl_total_now
        rpl_delta = rpl_total_now - prior_rpl_total
        if rpl_delta > 0.01:
            alerts.append(ValidatorAlert(
                severity="info", validator_index=None, category="rpl_reward_claimed",
                message=(f"RPL total (loose + staked) increased {rpl_delta:.6f} RPL since last check "
                         f"— a reward claim happened; tax treatment (restake vs withdraw timing) not "
                         f"yet decided, not classified here"),
            ))
        await self.qdrant.store(
            content=f"RP_NODE RPL total (loose + staked): {rpl_total_now:.6f} RPL",
            metadata={
                "type": "episodic", "domain": "eth.validators",
                "_key": "episodic:eth:rpl-last-total",
                "rpl_total": rpl_total_now, "checked_at": now.isoformat(),
            },
            collection=EPISODIC, writer="sovereign-core",
        )

        total_balance = summary["total_balance_eth"]
        total_operator = summary["total_operator_eth"]
        result = ValidatorCheckResult(
            checked_at=now,
            validator_count=summary["validator_count"],
            total_operator_eth=total_operator,
            total_balance_eth=total_balance,
            unrealised_reward_eth=total_balance - total_operator,
            rpl_loose_at_node=rewards["rpl_loose_at_node"],
            rpl_staked_total=rewards["rpl_staked_total"],
            rpl_delta_since_last=rpl_delta,
            alerts=alerts,
            per_validator=per_validator,
        )

        date_str = now.strftime("%Y-%m-%d")
        await self.qdrant.store(
            content=(f"Validator full check {date_str} — {result.validator_count} validators, "
                     f"{result.total_balance_eth:.5f} ETH total, {len(alerts)} alert(s)."),
            metadata={
                "type": "episodic", "domain": "eth.validators",
                "_key": f"episodic:eth:validator-check:{date_str}",
                "result": result.model_dump(mode="json"),
            },
            collection=EPISODIC, writer="sovereign-core",
        )

        return result
