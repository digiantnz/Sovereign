"""Tax Report Harness — /do_tax command handler.

Triggered by /do_tax [year] Telegram command. Produces two accountant-ready
CSV files for the requested NZ financial year.

Session flag: _tax_report_harness_checkpoint
Session key:  tax_report:session

Human-in-the-loop flow (3 turns):

  Turn 1 — /do_tax [year]
    query step: date-range query on semantic memory for all tax events in FY.
    Classifies tax:crypto events; loads tax:expense events from memory.
    Reports counts to Director. Asks for supplementary expense CSV names.
    Checkpoint: awaiting_csv_names

  Turn 2 — Director provides CSV filename(s) or "none"
    ingest step: fetches + parses each named CSV from Nextcloud.
    Filters rows to date range. Merges into expense array (NOT stored to memory).
    Reports row counts. Asks for confirmation to generate files.
    "none" = permission to proceed without supplementary files.
    Checkpoint: awaiting_confirm

  Turn 3 — Director confirms
    create step: generates income{year}.csv and expenses{year}.csv in memory.
    Saves to /Digiant/Tax/FY{year}/ via nanobot.
    notify step: Telegram summary.
    clear step: deletes checkpoint from working_memory.

Output files (saved to /Digiant/Tax/FY{year}/):
  income{year}.csv  — all tax:crypto events with classifier labels
  expenses{year}.csv — all tax:expense events (memory + supplementary CSVs)

NZ tax year YYYY = 01 Apr YYYY-1 → 31 Mar YYYY
  e.g. /do_tax 2026 → 2025-04-01 to 2026-03-31
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import resolve_tax_year

logger = logging.getLogger(__name__)

_SESSION_FLAG  = "_tax_report_harness_checkpoint"
_TAX_YEAR_ROOT = "/Digiant/Tax"


def _fy_date_range(tax_year: str) -> tuple[str, str]:
    """Return (start_iso, end_iso) for the given NZ tax year string.

    Tax year "2026" → 2025-04-01T00:00:00Z to 2026-03-31T23:59:59Z
    """
    year = int(tax_year)
    start = f"{year - 1}-04-01T00:00:00Z"
    end   = f"{year}-03-31T23:59:59Z"
    return start, end


def _in_range(timestamp: str, start: str, end: str) -> bool:
    """Return True if timestamp falls within [start, end] inclusive."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        s  = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e  = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return s <= ts <= e
    except Exception:
        return False


def _resolve_tax_year_from_now() -> str:
    """Return the current NZ financial year string."""
    return resolve_tax_year(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _nzd_decimal(nzd_str: str | None) -> Decimal | None:
    """Parse "$X.XX NZD" → Decimal, or return None."""
    if not nzd_str:
        return None
    try:
        cleaned = nzd_str.replace("$", "").replace("NZD", "").replace(",", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


class TaxReportHarness:
    """Three-turn human-in-the-loop tax report harness."""

    def __init__(self, cog, nanobot, qdrant, tax_year: str | None = None):
        self.cog      = cog
        self.nanobot  = nanobot
        self.qdrant   = qdrant
        self.tax_year = tax_year or _resolve_tax_year_from_now()

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(self, user_input: str = "", confirmed: bool = False) -> dict:
        """Advance the harness state machine by one turn.

        Reads current checkpoint to determine which step to execute next.
        """
        checkpoint = await self._load_checkpoint()
        current_step = (checkpoint or {}).get("current_step", "start")

        if current_step == "start":
            return await self._step_query()

        if current_step == "awaiting_csv_names":
            return await self._step_ingest(user_input, checkpoint)

        if current_step == "awaiting_confirm":
            if confirmed:
                return await self._step_create(checkpoint)
            # Re-surface the confirmation prompt
            n_income  = checkpoint.get("income_count", 0)
            n_expense = checkpoint.get("expense_count", 0)
            year      = self.tax_year
            return {
                "status": "awaiting_confirm",
                "response": (
                    f"Ready to generate FY{year} report: {n_income} income records, "
                    f"{n_expense} expense records. Confirm to create "
                    f"income{year}.csv and expenses{year}.csv in "
                    f"{_TAX_YEAR_ROOT}/FY{year}/?"
                ),
                "_translator_bypass": True,
            }

        return {"status": "error", "response": "Unknown harness state. Run /do_tax to restart."}

    # ── Step 1: query ──────────────────────────────────────────────────────────

    async def _step_query(self) -> dict:
        """Query semantic memory for all tax events in the FY date range.

        Classifies tax:crypto events. Loads tax:expense events.
        Reports counts to Director. Asks for supplementary expense CSV names.
        """
        year       = self.tax_year
        start, end = _fy_date_range(year)

        # Scroll semantic collection for domain=tax entries in date range
        events = await self._query_semantic_tax_events(start, end)

        if not events:
            await self._write_checkpoint({
                "current_step":  "awaiting_csv_names",
                "tax_year":      year,
                "date_start":    start,
                "date_end":      end,
                "income_rows":   [],
                "expense_rows":  [],
                "income_count":  0,
                "expense_count": 0,
            })
            return {
                "status":   "awaiting_csv_names",
                "response": (
                    f"No tax events found in semantic memory for FY{year} "
                    f"(01 Apr {int(year)-1} – 31 Mar {year}). "
                    f"Provide expense CSV filename(s) from {_TAX_YEAR_ROOT}/FY{year}/ "
                    f"to include, or reply 'none' to proceed."
                ),
                "_translator_bypass": True,
            }

        # Classify
        from .classifier import classify_events
        classified = await classify_events(events, self.qdrant)

        income_rows  = [_income_row(ev)  for ev in classified.income]
        expense_rows = [_expense_row(ev) for ev in classified.expenses]

        await self._write_checkpoint({
            "current_step":  "awaiting_csv_names",
            "tax_year":      year,
            "date_start":    start,
            "date_end":      end,
            "income_rows":   income_rows,
            "expense_rows":  expense_rows,
            "income_count":  len(income_rows),
            "expense_count": len(expense_rows),
        })

        return {
            "status": "awaiting_csv_names",
            "response": (
                f"FY{year} (01 Apr {int(year)-1} – 31 Mar {year}): "
                f"found {len(income_rows)} crypto/income record(s) and "
                f"{len(expense_rows)} expense record(s) in memory. "
                f"Provide expense CSV filename(s) from {_TAX_YEAR_ROOT}/FY{year}/ "
                f"to include in the report (comma-separated), or reply 'none' to proceed."
            ),
            "_translator_bypass": True,
        }

    # ── Step 2: ingest supplementary CSVs ─────────────────────────────────────

    async def _step_ingest(self, user_input: str, checkpoint: dict) -> dict:
        """Parse Director-specified CSVs from Nextcloud. Merge into expense array.

        Does NOT store events to memory. Does NOT tag files as ingested.
        "none" = no supplementary files — proceed directly to confirm prompt.
        """
        year     = checkpoint.get("tax_year", self.tax_year)
        start    = checkpoint.get("date_start", "")
        end      = checkpoint.get("date_end",   "")
        folder   = f"{_TAX_YEAR_ROOT}/FY{year}"

        expense_rows: list[dict] = list(checkpoint.get("expense_rows") or [])
        income_rows:  list[dict] = list(checkpoint.get("income_rows")  or [])

        raw = user_input.strip().lower()
        skip_files = raw in ("none", "no", "skip", "n")

        file_summaries: list[str] = []
        total_new = 0

        if not skip_files:
            # Parse comma/newline-separated filenames
            raw_names = user_input.replace("\n", ",").split(",")
            filenames = [n.strip() for n in raw_names if n.strip()]

            for fname in filenames:
                path = f"{folder}/{fname}"
                try:
                    from .ingest import ingest_csv_file
                    parsed_events = await ingest_csv_file(self.nanobot, path, fname)
                    # Filter to date range and extract expense rows only
                    in_scope = [
                        ev for ev in parsed_events
                        if _in_range(ev.timestamp, start, end)
                    ]
                    # Separate: expense rows go to expenses, crypto rows go to income
                    new_expense = [_expense_row(ev) for ev in in_scope if ev.event_tag == "tax:expense"]
                    new_income  = []
                    if any(ev.event_tag == "tax:crypto" for ev in in_scope):
                        from .classifier import classify_events
                        crypto_evs = [ev for ev in in_scope if ev.event_tag == "tax:crypto"]
                        cls = await classify_events(crypto_evs, self.qdrant)
                        new_income = [_income_row(ev) for ev in cls.income]

                    expense_rows.extend(new_expense)
                    income_rows.extend(new_income)
                    n_added = len(new_expense) + len(new_income)
                    total_new += n_added
                    file_summaries.append(
                        f"{fname}: {len(new_expense)} expense row(s), "
                        f"{len(new_income)} income row(s) in scope"
                    )
                except Exception as exc:
                    logger.warning("report_harness: failed to parse %s: %s", path, exc)
                    file_summaries.append(f"{fname}: parse error — {exc}")

        # Sort both arrays chronologically
        expense_rows.sort(key=lambda r: r.get("Date", ""))
        income_rows.sort(key=lambda r: r.get("Date", ""))

        await self._write_checkpoint({
            "current_step":  "awaiting_confirm",
            "tax_year":      year,
            "date_start":    start,
            "date_end":      end,
            "income_rows":   income_rows,
            "expense_rows":  expense_rows,
            "income_count":  len(income_rows),
            "expense_count": len(expense_rows),
        })

        if skip_files:
            summary = "No supplementary files added."
        else:
            summary = "\n".join(file_summaries) if file_summaries else "No files parsed."

        return {
            "status": "awaiting_confirm",
            "response": (
                f"{summary}\n\n"
                f"Ready to generate FY{year} report:\n"
                f"  income{year}.csv — {len(income_rows)} record(s)\n"
                f"  expenses{year}.csv — {len(expense_rows)} record(s)\n\n"
                f"Confirm to create files in {folder}/?"
            ),
            "_translator_bypass": True,
        }

    # ── Step 3: create + save CSVs ─────────────────────────────────────────────

    async def _step_create(self, checkpoint: dict) -> dict:
        """Generate income and expenses CSVs and save to Nextcloud."""
        year         = checkpoint.get("tax_year", self.tax_year)
        income_rows  = checkpoint.get("income_rows",  [])
        expense_rows = checkpoint.get("expense_rows", [])
        folder       = f"{_TAX_YEAR_ROOT}/FY{year}"

        income_csv  = _build_income_csv(income_rows)
        expense_csv = _build_expense_csv(expense_rows)

        saved: list[str] = []
        errors: list[str] = []

        for filename, content in [
            (f"income{year}.csv",   income_csv),
            (f"expenses{year}.csv", expense_csv),
        ]:
            path = f"{folder}/{filename}"
            try:
                nb = await self.nanobot.run(
                    "openclaw-nextcloud", "files_write",
                    {"path": path, "content": content},
                )
                result = nb.get("result") if nb.get("result") is not None else nb
                ok = (
                    nb.get("status") == "ok"
                    or (isinstance(result, dict) and result.get("status") == "ok")
                    or nb.get("success") is True
                )
                if ok:
                    saved.append(path)
                else:
                    errors.append(f"{filename}: {nb.get('error', 'unknown error')}")
            except Exception as exc:
                errors.append(f"{filename}: {exc}")
                logger.warning("report_harness: save %s failed: %s", filename, exc)

        await self._step_notify(year, income_rows, expense_rows, saved, errors)
        await self._step_clear()

        if errors:
            return {
                "status":   "partial",
                "response": (
                    f"FY{year} report partially saved.\n"
                    f"Saved: {', '.join(saved) or 'none'}\n"
                    f"Errors: {'; '.join(errors)}"
                ),
                "_translator_bypass": True,
            }

        return {
            "status":   "ok",
            "response": (
                f"FY{year} tax report complete.\n"
                f"  income{year}.csv — {len(income_rows)} record(s)\n"
                f"  expenses{year}.csv — {len(expense_rows)} record(s)\n"
                f"Saved to {folder}/."
            ),
            "_translator_bypass": True,
        }

    # ── Notify ─────────────────────────────────────────────────────────────────

    async def _step_notify(
        self, year: str, income_rows: list, expense_rows: list,
        saved: list, errors: list,
    ) -> None:
        """Send Telegram summary to Director."""
        lines = [f"Tax Report FY{year} complete."]
        lines.append(f"  Income records: {len(income_rows)}")
        lines.append(f"  Expense records: {len(expense_rows)}")

        # Count subtypes in income
        subtypes: dict[str, int] = {}
        for r in income_rows:
            st = r.get("Classification", "unknown")
            subtypes[st] = subtypes.get(st, 0) + 1
        for st, cnt in sorted(subtypes.items()):
            lines.append(f"    {st}: {cnt}")

        # Count unpriced
        unpriced = sum(1 for r in income_rows if not r.get("NZD Value"))
        if unpriced:
            lines.append(f"  Unpriced income records (NZD value missing): {unpriced}")

        lines.append(f"  Files saved: {', '.join(saved) or 'none'}")
        if errors:
            lines.append(f"  Save errors: {'; '.join(errors)}")

        msg = "\n".join(lines)
        _token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        _chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
        if _token and _chat_id:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as cl:
                await cl.post(
                    f"https://api.telegram.org/bot{_token}/sendMessage",
                    json={"chat_id": _chat_id, "text": msg},
                )

    # ── Clear ──────────────────────────────────────────────────────────────────

    async def _step_clear(self) -> None:
        """Delete checkpoint entries from working_memory."""
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue, PointIdsList
            hits, _ = await self.qdrant.client.scroll(
                collection_name="working_memory",
                scroll_filter=Filter(must=[
                    FieldCondition(key=_SESSION_FLAG, match=MatchValue(value=True)),
                ]),
                limit=50,
                with_payload=False,
                with_vectors=False,
            )
            if hits:
                await self.qdrant.client.delete(
                    collection_name="working_memory",
                    points_selector=PointIdsList(points=[h.id for h in hits]),
                )
        except Exception as exc:
            logger.warning("report_harness: clear failed: %s", exc)

    # ── Semantic query ─────────────────────────────────────────────────────────

    async def _query_semantic_tax_events(self, start: str, end: str) -> list:
        """Scroll semantic collection for tax events within the date range.

        Uses payload filters (domain=tax) + Python-side timestamp filtering.
        Returns a list of TaxEvent objects reconstructed from payloads.
        """
        from .harness import _payload_to_tax_event
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            events = []
            offset = None
            while True:
                hits, next_offset = await self.qdrant.archive_client.scroll(
                    collection_name="semantic",
                    scroll_filter=Filter(must=[
                        FieldCondition(key="domain", match=MatchValue(value="tax")),
                        FieldCondition(key="type",   match=MatchValue(value="tax_event")),
                    ]),
                    limit=200,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for h in hits:
                    p = dict(h.payload or {})
                    ts = p.get("timestamp", "")
                    if _in_range(ts, start, end):
                        ev = _payload_to_tax_event(p)
                        if ev:
                            events.append(ev)
                if next_offset is None or not hits:
                    break
                offset = next_offset
            logger.info(
                "report_harness: query found %d tax events in range %s → %s",
                len(events), start[:10], end[:10],
            )
            return events
        except Exception as exc:
            logger.warning("report_harness: semantic query failed: %s", exc)
            return []

    # ── Checkpoint helpers ─────────────────────────────────────────────────────

    async def _write_checkpoint(self, state: dict) -> None:
        state[_SESSION_FLAG] = True
        try:
            await self.qdrant.store(
                collection="working_memory",
                content=f"Tax report harness checkpoint: {state.get('current_step')}",
                metadata=state,
            )
        except Exception as exc:
            logger.warning("report_harness: checkpoint write failed: %s", exc)

    async def _load_checkpoint(self) -> dict | None:
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            hits, _ = await self.qdrant.client.scroll(
                collection_name="working_memory",
                scroll_filter=Filter(must=[
                    FieldCondition(key=_SESSION_FLAG, match=MatchValue(value=True)),
                ]),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
            if hits:
                return dict(hits[0].payload or {})
        except Exception as exc:
            logger.warning("report_harness: checkpoint load failed: %s", exc)
        return None


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _income_row(ev) -> dict:
    """Convert a classified TaxEvent to an income CSV row dict."""
    return {
        "Date":          ev.timestamp[:10] if ev.timestamp else "",
        "Classification": ev.subtype or "unknown",
        "From Address":  ev.from_address or "",
        "To Address":    ev.to_address   or "",
        "Asset":         ev.asset         or "",
        "Amount":        ev.amount        or "",
        "NZD Value":     ev.nzd_value     or "",
        "Source":        ev.source        or "",
        "Reference":     ev.reference     or "",
    }


def _expense_row(ev) -> dict:
    """Convert a TaxEvent (tax:expense) to an expense CSV row dict."""
    return {
        "Date":       ev.timestamp  [:10] if ev.timestamp else "",
        "Vendor":     ev.vendor         or ev.source or "",
        "Amount NZD": ev.amount_nzd     or ev.nzd_value or "",
        "Source":     ev.source         or "",
        "Reference":  ev.reference      or "",
    }


def _build_income_csv(rows: list[dict]) -> str:
    """Serialise income rows to CSV string."""
    if not rows:
        rows = [{}]
    headers = ["Date", "Classification", "From Address", "To Address",
               "Asset", "Amount", "NZD Value", "Source", "Reference"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _build_expense_csv(rows: list[dict]) -> str:
    """Serialise expense rows to CSV string."""
    if not rows:
        rows = [{}]
    headers = ["Date", "Vendor", "Amount NZD", "Source", "Reference"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
