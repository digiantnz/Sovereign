"""Sovereign blockchain address watcher.

Polls the local Ethereum node every 15 seconds for new blocks.
On each new block, scans transactions against the watched address set
(Safe address + Rex's EOA). Sends Telegram alert on any match.

BTC watching: activates automatically when wallet-config.json has a
populated multisig_zpub and the Bitcoin node credentials are configured.
BTC polling uses listunspent via bitcoin-cli JSON-RPC.

check_now(address): on-demand payment check callable from execution engine
(e.g. when Director asks "has X paid?").
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

POLL_INTERVAL = 15          # seconds
RPC_TIMEOUT   = 10.0

_RPC_PRIMARY   = os.environ.get("ETH_RPC_PRIMARY",   "http://172.16.201.15:8545")
_RPC_SECONDARY = os.environ.get("ETH_RPC_SECONDARY", "http://172.16.201.2:8545")
_SAFE_ADDRESS  = os.environ.get("SAFE_ADDRESS", "").lower()

_BTC_RPC_URL  = os.environ.get("BTC_RPC_URL", "http://172.16.201.5:8332")
_BTC_RPC_USER = os.environ.get("BTC_RPC_USER", "")
_BTC_RPC_PASS = os.environ.get("BTC_RPC_PASS", "")

_WALLET_STATE_PATH  = Path("/home/sovereign/keys/wallet-state.json")
_WALLET_CONFIG_PATH = Path("/home/sovereign/governance/wallet-config.json")

# Watcher state shared across tasks — used by check_now()
_last_eth_balances: dict[str, int] = {}
_last_btc_balances: dict[str, float] = {}


# ── Address discovery ─────────────────────────────────────────────────────────

def _load_rex_address() -> str:
    try:
        import json
        return json.loads(_WALLET_STATE_PATH.read_text()).get("address", "").lower()
    except Exception:
        return ""


def _load_btc_watched() -> list[str]:
    """Return list of BTC addresses to watch from wallet-config.json.

    Activates when multisig_fingerprint is populated (post-Specter setup).
    Currently watches the multisig receive address once it's known.
    """
    try:
        import json
        cfg = json.loads(_WALLET_CONFIG_PATH.read_text())
        btc = cfg.get("bitcoin", {})
        # Populate when Specter multisig address is stored in config
        addrs = []
        if btc.get("multisig_address"):
            addrs.append(btc["multisig_address"])
        return addrs
    except Exception:
        return []


def _eth_watched() -> set[str]:
    addrs = set()
    if _SAFE_ADDRESS:
        addrs.add(_SAFE_ADDRESS)
    rex = _load_rex_address()
    if rex:
        addrs.add(rex)
    return addrs


# ── Telegram ─────────────────────────────────────────────────────────────────

async def _notify_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("Watcher: Telegram notification failed: %s", e)


# ── ETH helpers ───────────────────────────────────────────────────────────────

def _wei_to_eth(hex_value: str) -> str:
    try:
        return f"{int(hex_value, 16) / 1e18:.6f}"
    except Exception:
        return "?"


async def _eth_rpc(client: httpx.AsyncClient, method: str, params: list, url: str):
    try:
        r = await client.post(
            url,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=RPC_TIMEOUT,
        )
        return r.json().get("result")
    except Exception as e:
        logger.debug("Watcher ETH RPC %s @ %s: %s", method, url, e)
        return None


async def _eth_rpc_fb(client: httpx.AsyncClient, method: str, params: list):
    result = await _eth_rpc(client, method, params, _RPC_PRIMARY)
    if result is None:
        result = await _eth_rpc(client, method, params, _RPC_SECONDARY)
    return result


def _fmt_eth_tx(tx: dict, matched: str) -> str:
    from_addr = tx.get("from", "?")
    to_addr   = tx.get("to") or "contract-create"
    value     = _wei_to_eth(tx.get("value", "0x0"))
    tx_hash   = tx.get("hash", "?")

    role = "Safe" if matched == _SAFE_ADDRESS else "Rex EOA"
    direction = "incoming" if to_addr.lower() == matched else "outgoing"

    lines = [
        f"*ETH Transaction — {role}*",
        f"Direction: {direction}",
        f"Value: {value} ETH",
        f"From: `{from_addr[:10]}...{from_addr[-6:]}`",
        f"To: `{to_addr[:10]}...{to_addr[-6:]}`" if to_addr != "contract-create" else "To: contract creation",
        f"Tx: `{tx_hash[:16]}...`",
        f"Block: {int(tx.get('blockNumber', '0x0'), 16)}",
    ]
    return "\n".join(lines)


async def _scan_eth_block(client, block_hex: str, watched: set[str], ledger) -> int:
    block = await _eth_rpc_fb(client, "eth_getBlockByNumber", [block_hex, True])
    if not block or not isinstance(block.get("transactions"), list):
        return 0

    matches = 0
    for tx in block["transactions"]:
        from_addr = (tx.get("from") or "").lower()
        to_addr   = (tx.get("to")   or "").lower()
        hit = to_addr if to_addr in watched else (from_addr if from_addr in watched else None)
        if not hit:
            continue

        matches += 1
        await _notify_telegram(_fmt_eth_tx(tx, hit))
        logger.info("Watcher: ETH tx %s matched %s", tx.get("hash"), hit)
        if ledger:
            try:
                ledger.append("eth_transaction_detected", "wallet", {
                    "tx_hash":         tx.get("hash"),
                    "matched_address": hit,
                    "from":            tx.get("from"),
                    "to":              tx.get("to"),
                    "value_wei":       tx.get("value"),
                    "block":           tx.get("blockNumber"),
                })
            except Exception as le:
                logger.warning("Watcher: ledger write failed: %s", le)

    return matches


# ── BTC helpers ───────────────────────────────────────────────────────────────

async def _btc_rpc(client: httpx.AsyncClient, method: str, params: list):
    if not _BTC_RPC_USER or not _BTC_RPC_PASS:
        return None
    try:
        r = await client.post(
            _BTC_RPC_URL,
            json={"jsonrpc": "1.0", "method": method, "params": params, "id": 1},
            auth=(_BTC_RPC_USER, _BTC_RPC_PASS),
            timeout=RPC_TIMEOUT,
        )
        return r.json().get("result")
    except Exception as e:
        logger.debug("Watcher BTC RPC %s: %s", method, e)
        return None


async def _check_btc_addresses(client, addresses: list[str], ledger) -> None:
    """Check BTC addresses for balance changes since last poll."""
    global _last_btc_balances

    for addr in addresses:
        # Use getreceivedbyaddress (requires address imported as watch-only)
        received = await _btc_rpc(client, "getreceivedbyaddress", [addr, 0])
        if received is None:
            continue

        prev = _last_btc_balances.get(addr, -1.0)
        if prev < 0:
            # First observation — record baseline
            _last_btc_balances[addr] = float(received)
            logger.info("Watcher: BTC baseline for %s...%s = %.8f BTC",
                        addr[:8], addr[-6:], float(received))
            continue

        current = float(received)
        if current > prev:
            delta = current - prev
            msg = (
                f"*BTC Received — multisig*\n"
                f"Address: `{addr[:10]}...{addr[-6:]}`\n"
                f"Received: +{delta:.8f} BTC\n"
                f"Total received: {current:.8f} BTC"
            )
            await _notify_telegram(msg)
            logger.info("Watcher: BTC received %.8f BTC at %s", delta, addr[:16])
            if ledger:
                try:
                    ledger.append("btc_received", "wallet", {
                        "address": addr,
                        "delta_btc": delta,
                        "total_received_btc": current,
                    })
                except Exception:
                    pass
            _last_btc_balances[addr] = current


# ── On-demand check ───────────────────────────────────────────────────────────

async def check_now(address: str = None, ledger=None) -> dict:
    """Immediate balance + recent activity check for an address.

    Called by execution engine when Director asks 'has X paid?' or similar.
    Returns current ETH balance and last 5 blocks of matching transactions.
    address: specific address to check, or None to check all watched addresses.
    """
    results = {}
    watched = _eth_watched() if not address else {address.lower()}

    async with httpx.AsyncClient() as client:
        for addr in watched:
            bal_hex = await _eth_rpc_fb(client, "eth_getBalance", [addr, "latest"])
            balance_eth = _wei_to_eth(bal_hex) if bal_hex else "?"

            # Scan last 5 blocks for recent activity
            latest_hex = await _eth_rpc_fb(client, "eth_blockNumber", [])
            recent_txs = []
            if latest_hex:
                tip = int(latest_hex, 16)
                for block_num in range(tip - 4, tip + 1):
                    block = await _eth_rpc_fb(
                        client, "eth_getBlockByNumber", [hex(block_num), True]
                    )
                    if not block:
                        continue
                    for tx in block.get("transactions", []):
                        from_a = (tx.get("from") or "").lower()
                        to_a   = (tx.get("to")   or "").lower()
                        if from_a == addr or to_a == addr:
                            recent_txs.append({
                                "hash":      tx.get("hash"),
                                "from":      tx.get("from"),
                                "to":        tx.get("to"),
                                "value_eth": _wei_to_eth(tx.get("value", "0x0")),
                                "block":     block_num,
                            })

            results[addr] = {
                "balance_eth": balance_eth,
                "recent_transactions": recent_txs[-10:],  # cap at 10
            }

    return results


# ── Main watcher loop ─────────────────────────────────────────────────────────

async def eth_watch_loop(ledger=None) -> None:
    """Background task — polls every 15 seconds for new ETH + BTC activity."""
    last_eth_block: int | None = None
    startup_notified = False

    logger.info("Watcher: starting — polling every %ds", POLL_INTERVAL)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                # ── ETH ──────────────────────────────────────────────────
                watched = _eth_watched()
                if watched:
                    result = await _eth_rpc_fb(client, "eth_blockNumber", [])
                    if result is not None:
                        current = int(result, 16)
                        if not startup_notified:
                            logger.info(
                                "Watcher: ETH connected — block %d — watching %s",
                                current, ", ".join(sorted(watched)),
                            )
                            startup_notified = True

                        if last_eth_block is None:
                            last_eth_block = current
                        elif current > last_eth_block:
                            for bn in range(last_eth_block + 1, current + 1):
                                cnt = await _scan_eth_block(client, hex(bn), watched, ledger)
                                if cnt:
                                    logger.info("Watcher: block %d — %d ETH match(es)", bn, cnt)
                            last_eth_block = current
                    else:
                        logger.warning("Watcher: ETH nodes unreachable — will retry")

                # ── BTC ──────────────────────────────────────────────────
                btc_addrs = _load_btc_watched()
                if btc_addrs and _BTC_RPC_USER:
                    await _check_btc_addresses(client, btc_addrs, ledger)

            except asyncio.CancelledError:
                logger.info("Watcher: cancelled — shutting down")
                return
            except Exception as e:
                logger.error("Watcher: unexpected error: %s", e)

            await asyncio.sleep(POLL_INTERVAL)


def start_eth_watcher(ledger=None) -> asyncio.Task:
    task = asyncio.create_task(eth_watch_loop(ledger=ledger))
    logger.info("Watcher: started (ETH + BTC when configured)")
    return task
