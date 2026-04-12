"""Tax Ingest Harness — wallet event ingestion.

Called from /wallet_event endpoint as asyncio.create_task (non-blocking).
Writes a working_memory entry tagged _tax_pending=True for the harness to collect.

Every transaction pushed by the wallet watcher is stored as a tax:crypto event.
No filtering on from_address or to_address — if the wallet watcher sent it, it is
relevant by definition. The wallet watcher only pushes transactions involving watched
addresses. Classification (income / disposal / internal transfer) happens at report
time in /do_tax, which has full access to the known address lists in semantic memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from .models import TaxEvent, format_amount, make_tax_id, resolve_tax_year

logger = logging.getLogger(__name__)


async def handle_wallet_event(
    event: dict,
    qdrant,
    semantic_cache: dict | None = None,
) -> "TaxEvent | None":
    """Record an incoming wallet event as a tax:crypto event.

    Returns the TaxEvent on success, or None on error (missing tx_hash or bad amount).
    """
    from_addr  = (event.get("from_address") or "").lower().strip()
    to_addr    = (event.get("to_address")   or "").lower().strip()
    tx_hash    = event.get("tx_hash", "")
    currency   = (event.get("currency") or "ETH").upper()
    timestamp  = event.get("timestamp", "")
    raw_amount = event.get("amount", 0)

    if not tx_hash:
        return None
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        amount_d = Decimal(str(raw_amount))
    except Exception:
        logger.warning(
            "wallet_events: invalid amount %r for tx %s", raw_amount, tx_hash[:16]
        )
        return None

    tax_event = TaxEvent(
        id=make_tax_id(tx_hash),
        event_tag="tax:crypto",
        timestamp=timestamp,
        tax_year=resolve_tax_year(timestamp),
        source=event.get("chain", ""),
        reference=tx_hash,
        nzd_value=None,   # enriched by harness enrich step
        from_address=from_addr,
        to_address=to_addr,
        asset=currency,
        amount=format_amount(amount_d, currency),
        tx_hash=tx_hash,
        metadata={"confirmations": event.get("confirmations", 0)},
    )

    # Write to working_memory so harness can collect on next run
    try:
        payload = tax_event.to_qdrant_payload()
        payload["_tax_pending"] = True
        payload["_tax_stored"]  = False
        await qdrant.store(
            collection="working_memory",
            content=(
                f"Pending tax event: tax:crypto {tax_event.amount} "
                f"from:{from_addr[:12]} to:{to_addr[:12]} "
                f"ref:{tx_hash[:24]}"
            ),
            metadata=payload,
        )
    except Exception as exc:
        logger.warning("wallet_events: working_memory write failed: %s", exc)

    return tax_event
