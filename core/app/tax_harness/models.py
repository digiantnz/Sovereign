"""Tax Ingest Harness — core data model."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


@dataclass
class TaxEvent:
    """A single tax-relevant financial event.

    Two event tags:
      tax:crypto  — any on-chain or exchange transaction involving a known address.
                    Populated: from_address, to_address, asset, amount, tx_hash, nzd_value.
      tax:expense — a receipt, invoice PDF, or fiat card spend row from CSV.
                    Populated: vendor, amount_nzd, source, reference.

    Classification (income / disposal / internal transfer) is NOT done at ingest.
    All tax treatment is determined by /do_tax at report time using semantic memory.
    """

    id: str              # UUID5 — deterministic dedup from reference
    event_tag: str       # "tax:crypto" | "tax:expense"
    timestamp: str       # ISO8601 UTC
    tax_year: str        # NZ FY e.g. "2026"
    source: str          # filename (CSV/PDF) or chain identifier (on-chain)
    reference: str       # tx_hash (on-chain) or "{source}:{external_id}" (CSV)
    nzd_value: str | None  # "$X.XX NZD" or None (pricing unresolved — crypto only)

    # tax:crypto fields — None for tax:expense
    from_address: str | None = None
    to_address: str | None = None
    asset: str | None = None     # ETH | BTC | etc.
    amount: str | None = None    # formatted crypto amount e.g. "0.001000 ETH"
    tx_hash: str | None = None

    # tax:expense fields — None for tax:crypto
    vendor: str | None = None        # merchant name or PDF source label
    amount_nzd: str | None = None    # "$X.XX NZD"

    # Classifier-assigned label — in-memory only during report run, never persisted
    subtype: str | None = None

    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_qdrant_payload(self) -> dict:
        return {
            "type":          "tax_event",
            "domain":        "tax",
            "event_tag":     self.event_tag,
            "timestamp":     self.timestamp,
            "tax_year":      self.tax_year,
            "source":        self.source,
            "reference":     self.reference,
            "nzd_value":     self.nzd_value,
            # tax:crypto fields
            "from_address":  self.from_address,
            "to_address":    self.to_address,
            "asset":         self.asset,
            "amount":        self.amount,
            "tx_hash":       self.tx_hash,
            # tax:expense fields
            "vendor":        self.vendor,
            "amount_nzd":    self.amount_nzd,
            "sov_id":        self.id,
            "tags":          self.tags,
            **self.metadata,
        }


def resolve_tax_year(timestamp: str) -> str:
    """Return NZ tax year string for the given ISO8601 UTC timestamp.

    NZ fiscal year: 1 Apr YYYY → 31 Mar YYYY+1, labelled as YYYY+1.
    E.g. 2025-06-01 → "2026"; 2026-02-15 → "2026"; 2026-04-01 → "2027".
    """
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if dt.month >= 4:
        return str(dt.year + 1)
    return str(dt.year)


def format_amount(value: "Decimal | float | str", asset: str) -> str:
    """Return amount as canonical storage string."""
    if value is None:
        raise ValueError("amount is required")
    if not asset:
        raise ValueError("asset is required")
    d = Decimal(str(value))
    if asset.upper() == "NZD":
        return f"${d:.2f} NZD"
    return f"{d:.6f} {asset.upper()}"


def make_tax_id(reference: str) -> str:
    """Return UUID5 for the given transaction reference.

    Same reference → same UUID → Qdrant upsert is idempotent (dedup).
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tax:{reference}"))
