"""Tax Ingest Harness — TaxIngestHarness.

Steps: check → ingest → enrich → store → notify → clear
Session flag: _tax_ingest_harness_checkpoint
Session key:  tax_ingest:session
Schedule:     0 * * * * (hourly UTC) — status pending_approval until Director activates
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

_SESSION_FLAG = "_tax_ingest_harness_checkpoint"
_SESSION_KEY  = "tax_ingest:session"


class TaxIngestHarness:
    def __init__(self, cog, nanobot, qdrant):
        self.cog     = cog
        self.nanobot = nanobot
        self.qdrant  = qdrant
        self._semantic_cache: dict = {}

    async def run(self) -> dict:
        """Execute the full ingest cycle.

        Returns a summary dict with status, counts, and summary string.
        Each step is individually try/excepted — harness is fault-tolerant.
        """
        session: dict = {
            "started_at":       datetime.now(timezone.utc).isoformat(),
            "files_processed":  0,
            "events_raw":       0,
            "events_enriched":  0,
            "events_stored":    0,
            "errors":           [],
            "status":           "ok",
        }

        # ── Step 1: check ──────────────────────────────────────────────────
        try:
            await self._step_check()
        except Exception as exc:
            session["errors"].append(f"check: {exc}")
            session["status"] = "partial"
            logger.warning("tax_harness: check step failed: %s", exc)

        # ── Step 2: ingest ─────────────────────────────────────────────────
        events: list = []
        files_processed = 0
        try:
            file_events, wallet_ev = await self._step_ingest()
            events = file_events + wallet_ev
            files_processed = len(file_events)
            session["files_processed"] = files_processed
            session["events_raw"]      = len(events)
        except Exception as exc:
            session["errors"].append(f"ingest: {exc}")
            session["status"] = "partial"
            logger.warning("tax_harness: ingest step failed: %s", exc)

        if not events:
            session["summary"] = "No new tax events to process."
            return session

        # ── Step 3: enrich ─────────────────────────────────────────────────
        try:
            events = await self._step_enrich(events)
            session["events_enriched"] = len(events)
        except Exception as exc:
            session["errors"].append(f"enrich: {exc}")
            session["status"] = "partial"
            session["events_enriched"] = len(events)
            logger.warning("tax_harness: enrich step failed: %s", exc)

        # ── Step 4: store (MID — harness is called after Director approval) ─
        stored = 0
        try:
            stored = await self._step_store(events)
            session["events_stored"] = stored
        except Exception as exc:
            session["errors"].append(f"store: {exc}")
            session["status"] = "partial"
            logger.warning("tax_harness: store step failed: %s", exc)

        # ── Step 5: notify ─────────────────────────────────────────────────
        if stored > 0:
            try:
                await self._step_notify(session, events)
            except Exception as exc:
                session["errors"].append(f"notify: {exc}")
                logger.warning("tax_harness: notify step failed: %s", exc)

        # ── Step 6: clear ──────────────────────────────────────────────────
        try:
            await self._step_clear()
        except Exception as exc:
            logger.warning("tax_harness: clear step failed: %s", exc)

        session["summary"] = (
            f"Tax ingest complete: {stored} event(s) stored "
            f"from {session['events_raw']} raw ({files_processed} file(s) processed)."
        )
        return session

    # ── Step implementations ───────────────────────────────────────────────────

    async def _step_check(self) -> None:
        """Validate skill availability — empty result is a valid success."""
        from .ingest import list_unprocessed_files
        await list_unprocessed_files(self.nanobot)

    async def _step_ingest(self) -> tuple[list, list]:
        """Ingest unprocessed files + pending wallet events from working_memory."""
        from .ingest import (
            list_unprocessed_files, ingest_csv_file,
            ingest_pdf_receipt, mark_file_ingested,
        )

        file_events: list = []
        files = await list_unprocessed_files(self.nanobot)

        for f in files:
            path = f.get("path") or f.get("name") or ""
            if not path:
                continue
            name = path.split("/")[-1].lower()

            if name.endswith(".csv"):
                evts = await ingest_csv_file(self.nanobot, path, path)
                if evts:
                    file_events.extend(evts)
                    await mark_file_ingested(self.nanobot, path)

            elif name.endswith(".pdf"):
                evts = await ingest_pdf_receipt(self.nanobot, path, path)
                if evts:
                    file_events.extend(evts)
                    await mark_file_ingested(self.nanobot, path)

        # Collect pending wallet events from working_memory
        wallet_ev: list = []
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            hits, _ = await self.qdrant.client.scroll(
                collection_name="working_memory",
                scroll_filter=Filter(must=[
                    FieldCondition(key="type",         match=MatchValue(value="tax_event")),
                    FieldCondition(key="_tax_pending",  match=MatchValue(value=True)),
                    FieldCondition(key="_tax_stored",   match=MatchValue(value=False)),
                ]),
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
            for h in hits:
                p = dict(h.payload or {})
                ev = _payload_to_tax_event(p)
                if ev:
                    wallet_ev.append(ev)
        except Exception as exc:
            logger.warning("tax_harness: wallet event scan failed: %s", exc)

        return file_events, wallet_ev

    async def _step_enrich(self, events: list) -> list:
        """Add NZD values via CoinGecko for tax:crypto events.

        tax:expense events: nzd_value is already set from amount_nzd — no lookup needed.
        tax:crypto events: query CoinGecko if nzd_value is None.
        Output count must equal input count.
        """
        from .pricing import enrich_nzd

        enriched = []
        for ev in events:
            if ev.event_tag == "tax:expense":
                # Expense amounts are already in NZD
                if ev.nzd_value is None and ev.amount_nzd:
                    ev.nzd_value = ev.amount_nzd

            elif ev.event_tag == "tax:crypto":
                if ev.nzd_value is None and ev.asset and ev.amount:
                    try:
                        parts = ev.amount.split()
                        raw_d = Decimal(parts[0].lstrip("$"))
                        nzd   = await enrich_nzd(ev.asset, ev.timestamp, raw_d)
                        if nzd:
                            ev.nzd_value = nzd
                        else:
                            if "pricing_unresolved" not in ev.tags:
                                ev.tags.append("pricing_unresolved")
                    except Exception as exc:
                        logger.warning(
                            "tax_harness: enrich failed for %s: %s",
                            ev.reference[:16], exc,
                        )
                        if "pricing_unresolved" not in ev.tags:
                            ev.tags.append("pricing_unresolved")

            enriched.append(ev)

        return enriched

    async def _step_store(self, events: list) -> int:
        """Store tax events in SEMANTIC collection using UUID5 sov_id for dedup.

        Partial failure → logs to audit ledger and continues.
        """
        stored      = 0
        failed_refs: list[str] = []

        for ev in events:
            try:
                payload = ev.to_qdrant_payload()
                label   = ev.amount if ev.event_tag == "tax:crypto" else ev.amount_nzd
                await self.qdrant.store(
                    collection="semantic",
                    content=(
                        f"Tax event: {ev.event_tag} {label or 'unknown'} "
                        f"({ev.nzd_value or 'NZD TBD'}) "
                        f"ref:{ev.reference[:24]} tax_year:{ev.tax_year}"
                    ),
                    metadata=payload,
                )
                stored += 1
            except Exception as exc:
                failed_refs.append(ev.reference[:16])
                logger.warning(
                    "tax_harness: store failed for %s: %s", ev.reference[:16], exc
                )

        if failed_refs:
            logger.error(
                "tax_harness: store partial failure — %d failed: %s",
                len(failed_refs), failed_refs,
            )

        return stored

    async def _step_notify(self, session: dict, events: list) -> None:
        """Send Telegram notification when events were stored.

        Reports: count of tax:crypto events, count of tax:expense events,
        count of events with nzd_value null (pricing unresolved — needs attention at report time).
        """
        stored = session.get("events_stored", 0)
        if stored == 0:
            return

        n_crypto   = sum(1 for e in events if e.event_tag == "tax:crypto")
        n_expense  = sum(1 for e in events if e.event_tag == "tax:expense")
        n_unpriced = sum(1 for e in events if e.nzd_value is None)

        msg = (
            f"Tax Ingest Harness: {stored} event(s) stored.\n"
            f"  tax:crypto: {n_crypto}\n"
            f"  tax:expense: {n_expense}\n"
            f"  nzd_value null (pricing unresolved): {n_unpriced}"
        )

        _token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        _chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
        if _token and _chat_id:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as cl:
                await cl.post(
                    f"https://api.telegram.org/bot{_token}/sendMessage",
                    json={"chat_id": _chat_id, "text": msg},
                )

    async def _step_clear(self) -> None:
        """Delete session checkpoint entries from working_memory."""
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            hits, _ = await self.qdrant.client.scroll(
                collection_name="working_memory",
                scroll_filter=Filter(must=[
                    FieldCondition(
                        key=_SESSION_FLAG, match=MatchValue(value=True)
                    ),
                ]),
                limit=100,
                with_payload=False,
                with_vectors=False,
            )
            if hits:
                from qdrant_client.models import PointIdsList
                await self.qdrant.client.delete(
                    collection_name="working_memory",
                    points_selector=PointIdsList(points=[h.id for h in hits]),
                )
        except Exception as exc:
            logger.warning("tax_harness: clear step failed: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _payload_to_tax_event(p: dict):
    """Reconstruct a TaxEvent from a Qdrant working_memory payload dict."""
    from .models import TaxEvent
    _skip = {
        "type", "domain", "sov_id",
        "_tax_pending", "_tax_stored", "_trust",
        "_key", "title", "last_updated",
    }
    try:
        return TaxEvent(
            id=p.get("sov_id") or p.get("id", ""),
            event_tag=p.get("event_tag", "tax:crypto"),
            timestamp=p.get("timestamp", ""),
            tax_year=p.get("tax_year", ""),
            source=p.get("source", ""),
            reference=p.get("reference", ""),
            nzd_value=p.get("nzd_value"),
            from_address=p.get("from_address"),
            to_address=p.get("to_address"),
            asset=p.get("asset"),
            amount=p.get("amount"),
            tx_hash=p.get("tx_hash"),
            vendor=p.get("vendor"),
            amount_nzd=p.get("amount_nzd"),
            tags=list(p.get("tags") or []),
            metadata={k: v for k, v in p.items() if k not in _skip},
        )
    except Exception as exc:
        logger.warning("tax_harness: _payload_to_tax_event failed: %s", exc)
        return None
