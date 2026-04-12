"""Tax Report Harness — event classifier.

Pure deterministic Python. No LLM calls.

Classifies a list of TaxEvents for report generation. Sets ev.subtype in-memory
on each event — subtype is never persisted back to Qdrant.

Classification is done once per report run. Address lists are loaded from semantic
memory at call start and cached for the duration.

Rules applied to tax:crypto events (in order, first match wins):
  1. from_address in staking_contracts          → income,    staking_reward
  2. both addresses in taxable_wallets          → income,    internal_transfer
  3. from_address is exchange account           → income,    exchange_acquisition
  4. to_address is exchange account             → income,    exchange_disposal
  5. to_address in taxable_wallets              → income,    unknown_inbound
  6. from_address in taxable_wallets            → income,    unknown_outbound
  7. none of the above                          → income,    unknown

All tax:crypto events land in the income list regardless of subtype — the accountant
sees the full picture with labels. All tax:expense events land in the expenses list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Exchange account placeholder addresses (used by ingest.py for CSV trades)
_EXCHANGE_ACCOUNTS = {"wirex:account", "swyftx:account"}


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


async def classify_events(
    events: list,
    qdrant,
) -> ClassifiedEventSet:
    """Classify a list of TaxEvents for report generation.

    Loads taxable_wallets and staking_contracts from semantic memory once.
    Sets ev.subtype on each event in-memory.
    Returns a ClassifiedEventSet with income and expenses lists.

    Parameters
    ----------
    events : list[TaxEvent]   — mixed tax:crypto and tax:expense events
    qdrant                    — QdrantAdapter for semantic memory lookups
    """
    # Load address lists once — cached for this call only
    taxable_wallets   = await _load_address_set(qdrant, "semantic:tax:taxable_wallets")
    staking_contracts = await _load_address_set(qdrant, "semantic:tax:staking_contracts")

    income:   list = []
    expenses: list = []

    for ev in events:
        if ev.event_tag == "tax:expense":
            ev.subtype = ev.subtype or "expense"
            expenses.append(ev)
            continue

        # tax:crypto — apply rules in order, first match wins
        from_addr = (ev.from_address or "").lower()
        to_addr   = (ev.to_address   or "").lower()

        if from_addr and from_addr in staking_contracts:
            ev.subtype = "staking_reward"

        elif (from_addr in taxable_wallets and to_addr in taxable_wallets
              and from_addr and to_addr):
            ev.subtype = "internal_transfer"

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
