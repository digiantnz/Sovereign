"""Tax Report Harness — event classifier.

Pure deterministic Python. No LLM calls.

Classifies a list of TaxEvents for report generation. Sets ev.subtype in-memory
on each event — subtype is never persisted back to Qdrant.

Classification is done once per report run. Address lists are loaded from semantic
memory at call start and cached for the duration.

Rules applied to tax:crypto events (in order, first match wins):
  1. source == lightning_channel                → income,    internal_transfer
  2. source == lightning                        → income,    inbound/outbound/unknown
  3. dust filter (ETH < 0.0001)                → income,    dust
     catches 1-WEI spam/contract probes sent to any address including mining wallets
  4. from AND to in all_own_wallets (taxable|mining|watchlist-tax)
                                               → income,    internal_transfer
     own-wallet-to-own-wallet regardless of which wallet types are involved;
     this must come before mining_income so LEDGER_BUSINESS→LEDGER_MINING is internal
  5. to_address in mining_wallets              → income,    mining_income
     only fires when sender is NOT an own wallet (external pool deposit)
  6. from_address in mining_wallets            → income,    mining_outbound
     mining address sending to unknown external destination
  7. from_address in staking_contracts         → income,    staking_reward
  8. wirex:account both sides + direction=sell → income,    exchange_disposal
  9. wirex:account both sides + direction=buy  → income,    exchange_acquisition
 10. from_address is exchange account          → income,    exchange_acquisition
 11. to_address is exchange account            → income,    exchange_disposal
 12. to_address in taxable_wallets             → income,    unknown_inbound
 13. from_address in taxable_wallets           → income,    unknown_outbound
 14. none of the above                         → income,    unknown

Wirex ETH deposit address (0xd14d...) is in wallet.watchlist tagged harness="tax"
(loaded into all_own_wallets — see _load_watchlist_tax_addresses) — transfers to it
from own wallets are internal_transfer (rule 4). The disposal only materialises
inside Wirex's own ledger and is visible in the Wirex NZD statement CSV, not on-chain.

All tax:crypto events land in the income list regardless of subtype — the accountant
sees the full picture with labels. All tax:expense events land in the expenses list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Off-chain exchange account virtual addresses (used by ingest.py for CSV trades).
# Do NOT include on-chain deposit addresses here — those belong in taxable_wallets.
_EXCHANGE_ACCOUNTS = {
    "wirex:account",
    "swyftx:account",
    "easycrypto:account",
}

# Sub-threshold ETH amount treated as dust (contract probes, spam, 1-WEI attacks).
_DUST_THRESHOLD_ETH = Decimal("0.0001")


@dataclass
class ClassifiedEventSet:
    """Output of classify_events(). All tax:crypto events are in income regardless
    of subtype. tax:expense events are in expenses. Counts must sum to input length."""
    income:   list  # all tax:crypto events (subtype set)
    expenses: list  # all tax:expense events


async def _load_address_set(qdrant, key: str) -> set[str]:
    """Load a lowercase address set from a semantic memory entry."""
    try:
        entry = await qdrant.retrieve_by_key(key)
        if entry:
            return {
                a.lower() for a in (entry.get("addresses") or [])
                if isinstance(a, str) and a.strip()
            }
    except Exception as exc:
        logger.warning("classifier: failed to load %s: %s", key, exc)
    return set()


async def _load_watchlist_tax_addresses(qdrant) -> set[str]:
    """ETH addresses from wallet.watchlist tagged harness="tax".

    wallet.watchlist is the live, actively-maintained registry of every address
    Director watches (sov-wallet polls it every 30s) — reusing it here avoids a
    separately-maintained "internal_addresses" list that drifts out of sync with
    reality (see semantic:tax:internal_addresses, retired — never read by this
    module, superseded by this query). Addresses without harness="tax" (e.g. the
    Blackwall private addresses) are deliberately excluded from tax classification.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    try:
        points, _ = await qdrant.archive_client.scroll(
            collection_name="semantic",
            scroll_filter=Filter(must=[FieldCondition(key="domain", match=MatchValue(value="wallet.watchlist"))]),
            limit=64, with_payload=True, with_vectors=False,
        )
    except Exception as exc:
        logger.warning("classifier: failed to load wallet.watchlist: %s", exc)
        return set()
    out = set()
    for p in points:
        payload = p.payload or {}
        meta = payload.get("metadata") or {}
        if meta.get("chain") != "eth":
            continue
        if "tax" not in (meta.get("harness") or []):
            continue
        value = payload.get("value")
        if isinstance(value, str) and value.strip():
            out.add(value.lower())
    return out


def _is_dust(ev) -> bool:
    """Return True if the event amount is below the dust threshold."""
    asset = (ev.asset or "").upper()
    if asset == "WEI":
        # 1 WEI = 1e-18 ETH — always dust regardless of amount stored
        return True
    if asset != "ETH":
        return False
    try:
        raw = (ev.amount or "").split(" ")[0].replace(",", "")
        return raw and Decimal(raw) < _DUST_THRESHOLD_ETH
    except (InvalidOperation, IndexError):
        return False


async def classify_events(
    events: list,
    qdrant,
) -> ClassifiedEventSet:
    """Classify a list of TaxEvents for report generation.

    Loads taxable_wallets, mining_wallets, staking_contracts, and excluded_references
    from semantic memory once. Sets ev.subtype on each event in-memory.
    Returns a ClassifiedEventSet with income and expenses lists.

    Parameters
    ----------
    events : list[TaxEvent]   — mixed tax:crypto and tax:expense events
    qdrant                    — QdrantAdapter for semantic memory lookups
    """
    taxable_wallets   = await _load_address_set(qdrant, "semantic:tax:taxable_wallets")
    staking_contracts = await _load_address_set(qdrant, "semantic:tax:staking_contracts")
    mining_wallets    = await _load_address_set(qdrant, "semantic:tax:mining_wallets")
    # excluded_references: references of non-taxable events (loan disbursements etc.)
    # stored as lowercased strings in the "addresses" list of this semantic entry
    excluded_refs     = await _load_address_set(qdrant, "semantic:tax:excluded_references")
    watchlist_tax     = await _load_watchlist_tax_addresses(qdrant)

    # All addresses the Director controls — used for own-wallet-to-own-wallet detection.
    # Includes wallet.watchlist addresses tagged harness="tax" (LEDGER_BUSINESS,
    # LEDGER_INVEST, SAFE_MULTISIG, WIREX_ETH, etc.) so transfers to/from those wallets
    # are recognised as internal even though they never receive mining/staking income.
    all_own_wallets = taxable_wallets | mining_wallets | watchlist_tax

    income:   list = []
    expenses: list = []

    for ev in events:
        if ev.event_tag == "tax:expense":
            ev.subtype = ev.subtype or "expense"
            expenses.append(ev)
            continue

        # tax:crypto — apply rules in order, first match wins

        # Excluded references — loan disbursements, non-taxable events
        # Stored in semantic:tax:excluded_references (lowercased reference strings)
        if excluded_refs and ev.reference and ev.reference.lower() in excluded_refs:
            ev.subtype = "loan_disbursement"
            income.append(ev)
            continue

        # Lightning — checked first because Lightning events have non-standard addresses
        if ev.source == "lightning_channel":
            ev.subtype = "internal_transfer"
            income.append(ev)
            continue

        if ev.source == "lightning":
            direction = (ev.metadata.get("direction") or "").lower()
            if direction == "inbound":
                ev.subtype = "unknown_inbound"
            elif direction == "outbound":
                ev.subtype = "unknown_outbound"
            else:
                ev.subtype = "unknown"
            income.append(ev)
            continue

        from_addr = (ev.from_address or "").lower()
        to_addr   = (ev.to_address   or "").lower()

        # Dust filter — 1 WEI probes, spam tokens, contract interactions with no real value.
        # Catches unsolicited dust sent to mining addresses that would otherwise be mining_income.
        if _is_dust(ev):
            ev.subtype = "dust"

        # Own-wallet-to-own-wallet — must come before mining_income so that transfers
        # from LEDGER_BUSINESS or any other own wallet TO a mining address are not
        # misclassified as mining income.
        elif from_addr and to_addr and from_addr in all_own_wallets and to_addr in all_own_wallets:
            ev.subtype = "internal_transfer"

        # External deposit to mining address — only fires when sender is not an own wallet
        elif to_addr and to_addr in mining_wallets:
            ev.subtype = "mining_income"

        # Mining address sending to an external unknown destination
        elif from_addr and from_addr in mining_wallets:
            ev.subtype = "mining_outbound"

        elif from_addr and from_addr in staking_contracts:
            ev.subtype = "staking_reward"

        elif from_addr == "wirex:account" and to_addr == "wirex:account":
            # Off-chain Wirex trade — direction stored at ingest from NZD amount sign
            direction = (ev.metadata.get("direction") or "").lower()
            ev.subtype = "exchange_disposal" if direction == "sell" else "exchange_acquisition"

        elif from_addr in _EXCHANGE_ACCOUNTS:
            ev.subtype = "exchange_acquisition"

        elif to_addr in _EXCHANGE_ACCOUNTS:
            ev.subtype = "exchange_disposal"

        elif to_addr and to_addr in taxable_wallets:
            ev.subtype = "unknown_inbound"

        elif from_addr and from_addr in taxable_wallets:
            ev.subtype = "unknown_outbound"

        else:
            ev.subtype = "unknown"

        income.append(ev)

    logger.info(
        "classifier: %d income events, %d expense events (from %d total)",
        len(income), len(expenses), len(events),
    )
    return ClassifiedEventSet(income=income, expenses=expenses)
