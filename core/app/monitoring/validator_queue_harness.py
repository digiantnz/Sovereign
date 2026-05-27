"""Validator Queue Monitor — Ethereum entry/exit queue wait time via local beacon node.

Post-Electra (fork 0x06+), the entry queue is ETH-denominated:
  - Entry: beacon_state.pending_deposits — total Gwei waiting for activation
  - Exit:  active_exiting validators — total balance Gwei pending withdrawal
  - Churn: MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA (128 ETH) to
           MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT (256 ETH) per epoch
           With 39M+ ETH staked, effective churn = MAX (256 ETH/epoch).

No LLM, no external dependency. Returns alert=True when total wait < 7 days.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_SLOTS_PER_EPOCH = 32
_SECONDS_PER_SLOT = 12
_EPOCHS_PER_DAY = 86_400 // (_SLOTS_PER_EPOCH * _SECONDS_PER_SLOT)  # 225
_ALERT_THRESHOLD_DAYS = 28.0

# Electra churn constants (Gwei/epoch). Effective = min(MAX, max(MIN, staked/quotient)).
# With 39M+ ETH staked the cap is always active; use MAX as the effective rate.
_MIN_CHURN_GWEI = 128_000_000_000   # 128 ETH/epoch
_MAX_CHURN_GWEI = 256_000_000_000   # 256 ETH/epoch — effective cap at current stake levels
_CHURN_ETH_PER_DAY = (_MAX_CHURN_GWEI / 1e9) * _EPOCHS_PER_DAY  # 57,600 ETH/day


def _wait_days(gwei: int) -> float:
    eth = gwei / 1e9
    return eth / _CHURN_ETH_PER_DAY if _CHURN_ETH_PER_DAY else 0.0


def _fmt(days: float) -> str:
    if days < 1 / (24 * 60):  # under 1 minute
        return "< 1 minute"
    if days < 1 / 24:
        mins = int(days * 24 * 60)
        return f"{mins} minute{'s' if mins != 1 else ''}"
    if days < 1:
        hours = round(days * 24, 1)
        return f"{hours} hours"
    return f"{days:.1f} days"


async def run_validator_queue_check() -> dict:
    """Query local Lighthouse beacon node for ETH-denominated queue depths and wait times."""
    beacon_url = os.environ.get("ETH_BEACON_API", "http://172.16.201.15:5052").rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Entry queue: pending_deposits (Electra EIP-6110)
            r_entry = await client.get(
                f"{beacon_url}/eth/v1/beacon/states/head/pending_deposits"
            )
            r_entry.raise_for_status()
            entry_deposits = r_entry.json().get("data", [])
            entry_gwei = sum(int(d["amount"]) for d in entry_deposits)

            # Exit queue: active_exiting validators (ETH-denominated by balance)
            r_exit = await client.get(
                f"{beacon_url}/eth/v1/beacon/states/head/validators",
                params={"status": "active_exiting"},
            )
            r_exit.raise_for_status()
            exit_validators = r_exit.json().get("data", [])
            exit_gwei = sum(int(v["balance"]) for v in exit_validators)

    except Exception as exc:
        logger.error("validator_queue: beacon node query failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    entry_eth  = entry_gwei / 1e9
    exit_eth   = exit_gwei / 1e9
    entry_days = _wait_days(entry_gwei)
    exit_days  = _wait_days(exit_gwei)
    total_days = entry_days + exit_days

    result = {
        "status":                  "ok",
        "entry_queue_eth":         round(entry_eth, 2),
        "exit_queue_eth":          round(exit_eth, 2),
        "entry_queue_deposits":    len(entry_deposits),
        "exit_queue_validators":   len(exit_validators),
        "entry_wait":              _fmt(entry_days),
        "exit_wait":               _fmt(exit_days),
        "total_wait":              _fmt(total_days),
        "total_days":              round(total_days, 2),
        "churn_eth_per_day":       _CHURN_ETH_PER_DAY,
        "alert":                   total_days < _ALERT_THRESHOLD_DAYS,
    }

    if result["alert"]:
        logger.info(
            "validator_queue: ALERT — total wait %s (entry %s ETH=%s + exit %s ETH=%s)",
            _fmt(total_days),
            _fmt(entry_days), round(entry_eth),
            _fmt(exit_days),  round(exit_eth),
        )
    else:
        logger.info(
            "validator_queue: entry=%s (%s ETH) exit=%s (%s ETH) total=%s",
            _fmt(entry_days), round(entry_eth),
            _fmt(exit_days),  round(exit_eth),
            _fmt(total_days),
        )

    return result
