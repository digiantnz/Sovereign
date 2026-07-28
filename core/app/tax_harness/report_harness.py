"""Tax Report Harness — /do_tax command handler.

Triggered by /do_tax [year] Telegram command. Produces three accountant-ready
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
    create step: generates income, expenses, revenue, and disposals CSVs.
    Saves to /Digiant/Tax/FY{year}/ via nanobot.
    Data gaps queued to semantic:tax:data_gaps:{year}.
    notify step: Telegram summary.
    clear step: deletes checkpoint from working_memory.

Output files (saved to /Digiant/Tax/FY{year}/):
  income{year}.csv     — all tax:crypto events with classifier labels
  expenses{year}.csv   — Wirex + AliExpress + /receipt camera entries, deduped
  revenue{year}.csv    — mining_income events only (Date, Asset, Amount, NZD Value, Reference)
  disposals{year}.csv  — FIFO disposal gain/loss per acquisition lot

Expense sources (Turn 2):
  Wirex statement CSV and AliExpress export: Director-supplied files only.
  /receipt camera entries: pulled automatically from semantic memory (source=receipt_camera).
  All three merged and deduped on (Date, Amount NZD). Full overwrite every run.

NZ tax year YYYY = 01 Apr YYYY-1 → 31 Mar YYYY
  e.g. /do_tax 2026 → 2025-04-01 to 2026-03-31
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import resolve_tax_year

logger = logging.getLogger(__name__)

_SESSION_FLAG  = "_tax_report_harness_checkpoint"
_TAX_YEAR_ROOT = "/Digiant/Tax"

# Reject any Director-supplied filename that matches a harness output file.
_HARNESS_OUTPUT_RE = re.compile(r'^(income|expenses|revenue|disposals)\d{4}\.csv$', re.IGNORECASE)


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
        self.tax_year = tax_year or str(
            int(resolve_tax_year(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))) - 1
        )

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(self, user_input: str = "", confirmed: bool = False) -> dict:
        """Advance the harness state machine by one turn.

        Reads current checkpoint to determine which step to execute next.
        """
        checkpoint = await self._load_checkpoint()

        # Discard stale checkpoint if it belongs to a different tax year
        if checkpoint and self.tax_year:
            ck_year = checkpoint.get("tax_year", "")
            if ck_year and ck_year != self.tax_year:
                logger.info(
                    "report_harness: year changed %s→%s, clearing stale checkpoint",
                    ck_year, self.tax_year,
                )
                await self._step_clear()
                checkpoint = None

        current_step = (checkpoint or {}).get("current_step", "start")

        # Explicit cancellation from any step
        if (user_input or "").strip().lower() in ("cancel", "no", "n", "abort"):
            if checkpoint:
                await self._step_clear()
            year = (checkpoint or {}).get("tax_year", self.tax_year)
            return {
                "status": "cancelled",
                "response": f"FY{year} tax report cancelled. Run /do_tax {year} to start again.",
                "_translator_bypass": True,
            }

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
                    f"income{year}.csv, expenses{year}.csv, revenue{year}.csv, and "
                    f"disposals{year}.csv in "
                    f"{_TAX_YEAR_ROOT}/FY{year}/?"
                ),
                "_translator_bypass": True,
            }

        return {"status": "error", "response": "Unknown harness state. Run /do_tax to restart."}

    # ── Step 1: query ──────────────────────────────────────────────────────────

    async def _list_fy_csvs(self, year: str) -> str:
        """Return a one-line summary of CSV files found in the FY Nextcloud folder."""
        folder = f"{_TAX_YEAR_ROOT}/FY{year}"
        try:
            nb = await self.nanobot.run(
                "sovereign-nextcloud-fs", "fs_list", {"path": folder}
            )
            result = nb.get("result") if isinstance(nb, dict) and nb.get("result") is not None else nb
            files = result if isinstance(result, list) else (
                result.get("files") or result.get("items") or []
                if isinstance(result, dict) else []
            )
            csvs = [
                f.get("name", "") for f in files
                if isinstance(f, dict) and f.get("name", "").lower().endswith(".csv")
            ]
            if csvs:
                return f"Available CSVs in {folder}/: {', '.join(csvs)}"
            return f"No CSVs found in {folder}/ (folder may not exist yet)."
        except Exception:
            return ""

    async def _step_query(self) -> dict:
        """Query semantic memory for all tax events in the FY date range.

        Classifies tax:crypto events. Loads tax:expense events.
        Reports counts to Director. Asks for supplementary expense CSV names.
        """
        year       = self.tax_year
        start, end = _fy_date_range(year)

        # Scroll semantic collection for domain=tax entries in date range
        events = await self._query_semantic_tax_events(start, end)
        csv_hint = await self._list_fy_csvs(year)
        csv_hint_line = f"\n{csv_hint}" if csv_hint else ""

        if not events:
            await self._write_checkpoint({
                "current_step":  "awaiting_csv_names",
                "tax_year":      year,
                "date_start":    start,
                "date_end":      end,
                "income_rows":   [],
                "receipt_rows":  [],
                "income_count":  0,
                "receipt_count": 0,
            })
            return {
                "status":   "awaiting_csv_names",
                "response": (
                    f"No tax events found in semantic memory for FY{year} "
                    f"(01 Apr {int(year)-1} – 31 Mar {year}). "
                    f"Provide Wirex statement CSV and/or AliExpress export filename(s) "
                    f"(comma-separated), or reply 'none' to skip. "
                    f"Bare filenames resolve from {_TAX_YEAR_ROOT}/FY{year}/; "
                    f"absolute Nextcloud paths (e.g. /Digiant/Tax/25-26/file.csv) are used as-is."
                    f"{csv_hint_line}"
                ),
                "_translator_bypass": True,
            }

        # Classify
        from .classifier import classify_events
        classified = await classify_events(events, self.qdrant)

        income_rows  = [_income_row(ev)  for ev in classified.income
                        if ev.subtype != "loan_disbursement"]
        excluded_count = sum(1 for ev in classified.income if ev.subtype == "loan_disbursement")
        # Only pull /receipt camera entries from memory — Wirex and AliExpress are
        # supplied by the Director as files at step 2 to avoid double-counting.
        receipt_rows = [_expense_row(ev) for ev in classified.expenses
                        if (ev.source or "") == "receipt_camera"]

        await self._write_checkpoint({
            "current_step":  "awaiting_csv_names",
            "tax_year":      year,
            "date_start":    start,
            "date_end":      end,
            "income_rows":   income_rows,
            "receipt_rows":  receipt_rows,
            "income_count":  len(income_rows),
            "receipt_count": len(receipt_rows),
        })

        excl_note = f" ({excluded_count} loan_disbursement excluded)" if excluded_count else ""
        return {
            "status": "awaiting_csv_names",
            "response": (
                f"FY{year} (01 Apr {int(year)-1} – 31 Mar {year}): "
                f"found {len(income_rows)} crypto/income record(s){excl_note} and "
                f"{len(receipt_rows)} receipt capture(s) in memory. "
                f"Provide Wirex statement CSV and/or AliExpress export filename(s) "
                f"(comma-separated), or reply 'none' to skip. "
                f"Bare filenames are resolved from {_TAX_YEAR_ROOT}/FY{year}/; "
                f"absolute paths (e.g. /Digiant/Tax/25-26/wirex.csv) are used as-is."
                f"{csv_hint_line}"
            ),
            "_translator_bypass": True,
        }

    # ── Step 2: ingest supplementary CSVs ─────────────────────────────────────

    async def _step_ingest(self, user_input: str, checkpoint: dict) -> dict:
        """Parse Director-specified Wirex/AliExpress CSVs from Nextcloud.

        Merges parsed file rows with /receipt camera entries already in memory.
        Deduplicates on (Date, Amount NZD). Does NOT store events to memory.
        "none" = no file inputs — proceed using receipt memory entries only.
        """
        year         = checkpoint.get("tax_year", self.tax_year)
        start        = checkpoint.get("date_start", "")
        end          = checkpoint.get("date_end",   "")
        folder       = f"{_TAX_YEAR_ROOT}/FY{year}"
        receipt_rows: list[dict] = list(checkpoint.get("receipt_rows") or [])
        income_rows:  list[dict] = list(checkpoint.get("income_rows")  or [])

        raw = user_input.strip().lower()
        skip_files = raw in ("none", "no", "skip", "n")

        file_summaries:   list[str]  = []
        file_expense_rows: list[dict] = []

        if not skip_files:
            raw_names = user_input.replace("\n", ",").split(",")
            filenames = [n.strip() for n in raw_names if n.strip()]

            for fname in filenames:
                path = fname if fname.startswith("/") else f"{folder}/{fname}"
                bare = path.split("/")[-1]

                # Guard: reject harness output files to prevent self-feeding
                if _HARNESS_OUTPUT_RE.match(bare):
                    file_summaries.append(
                        f"{fname}: REJECTED — '{bare}' is a harness output file, not a valid "
                        f"input. Supply only source CSVs (Wirex statement, AliExpress export)."
                    )
                    logger.warning("report_harness: rejected output file as input: %s", bare)
                    continue

                try:
                    from .ingest import ingest_csv_file
                    parsed_events = await ingest_csv_file(
                        self.nanobot, path, fname,
                        start_date=start, end_date=end,
                    )
                    in_scope = [
                        ev for ev in parsed_events
                        if _in_range(ev.timestamp, start, end)
                    ]
                    if parsed_events:
                        _f = parsed_events[0]
                        logger.info(
                            "report_harness [%s]: %d parsed → %d in scope "
                            "(first ts=%r, in_range=%s, FY%s %s→%s)",
                            fname, len(parsed_events), len(in_scope),
                            _f.timestamp, _in_range(_f.timestamp, start, end),
                            year, start[:10], end[:10],
                        )
                    new_expense = [_expense_row(ev) for ev in in_scope if ev.event_tag == "tax:expense"]
                    new_income: list[dict] = []
                    if any(ev.event_tag == "tax:crypto" for ev in in_scope):
                        from .classifier import classify_events
                        crypto_evs = [ev for ev in in_scope if ev.event_tag == "tax:crypto"]
                        cls = await classify_events(crypto_evs, self.qdrant)
                        new_income = [_income_row(ev) for ev in cls.income
                                      if ev.subtype != "loan_disbursement"]

                    file_expense_rows.extend(new_expense)
                    income_rows.extend(new_income)
                    file_summaries.append(
                        f"{fname}: {len(new_expense)} expense row(s), "
                        f"{len(new_income)} income row(s) in scope"
                    )
                except Exception as exc:
                    logger.warning("report_harness: failed to parse %s: %s", path, exc)
                    file_summaries.append(f"{fname}: parse error — {exc}")

        # Merge file rows + receipt memory rows; dedup on (Date, Amount NZD)
        expense_rows = _merge_expense_rows(file_expense_rows, receipt_rows)
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

        source_counts: dict[str, int] = {}
        for r in expense_rows:
            src = r.get("Source", "Unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        source_breakdown = ", ".join(
            f"{cnt} {src}" for src, cnt in sorted(source_counts.items())
        )
        expense_detail = (
            f"{len(expense_rows)} record(s) ({source_breakdown})"
            if source_breakdown else f"{len(expense_rows)} record(s)"
        )

        revenue_count = sum(1 for r in income_rows if r.get("Classification") == "mining_income")

        if skip_files:
            summary = "No file inputs added."
        else:
            summary = "\n".join(file_summaries) if file_summaries else "No files parsed."

        return {
            "status": "awaiting_confirm",
            "response": (
                f"{summary}\n\n"
                f"Ready to generate FY{year} report:\n"
                f"  income{year}.csv — {len(income_rows)} record(s)\n"
                f"  expenses{year}.csv — {expense_detail}\n"
                f"  revenue{year}.csv — {revenue_count} mining_income record(s)\n"
                f"  disposals{year}.csv — computed from FIFO (cross-year lot pool)\n\n"
                f"Confirm to create files in {folder}/?"
            ),
            "_translator_bypass": True,
        }

    # ── Step 3: create + save CSVs ─────────────────────────────────────────────

    async def _step_create(self, checkpoint: dict) -> dict:
        """Generate income, expenses, and FIFO disposal CSVs and save to Nextcloud."""
        year         = checkpoint.get("tax_year", self.tax_year)
        income_rows  = checkpoint.get("income_rows",  [])
        expense_rows = checkpoint.get("expense_rows", [])
        folder       = f"{_TAX_YEAR_ROOT}/FY{year}"
        start, end   = _fy_date_range(year)

        income_csv    = _build_income_csv(income_rows)
        expense_csv   = _build_expense_csv(expense_rows)
        revenue_csv   = _build_revenue_csv(income_rows)
        fifo_result   = await self._run_fifo_for_year(year, start, end)
        disposals_csv = _build_disposals_csv(fifo_result)

        # Ensure output folder exists
        try:
            await self.nanobot.run(
                "sovereign-nextcloud-fs", "fs_mkdir",
                {"path": folder},
            )
        except Exception as exc:
            logger.info("report_harness: mkdir %s: %s", folder, exc)

        saved: list[str] = []
        errors: list[str] = []

        for filename, content in [
            (f"income{year}.csv",    income_csv),
            (f"expenses{year}.csv",  expense_csv),
            (f"revenue{year}.csv",   revenue_csv),
            (f"disposals{year}.csv", disposals_csv),
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

        # Queue unresolved FIFO gaps to semantic memory (non-blocking)
        if fifo_result.data_gaps:
            asyncio.create_task(self._store_data_gaps(year, fifo_result.data_gaps))

        await self._step_notify(year, income_rows, expense_rows, saved, errors, fifo_result)
        await self._step_clear()

        n_disposals  = len(fifo_result.disposal_results)
        n_gaps       = len(fifo_result.data_gaps)
        n_unresolved = len(fifo_result.unresolved)

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

        disposal_summary = f"{n_disposals} disposal row(s)"
        if n_gaps:
            disposal_summary += f", {n_gaps} data gap(s) queued"
        if n_unresolved:
            disposal_summary += f", {n_unresolved} unresolved"

        n_revenue = sum(1 for r in income_rows if r.get("Classification") == "mining_income")

        return {
            "status":   "ok",
            "response": (
                f"FY{year} tax report complete.\n"
                f"  income{year}.csv — {len(income_rows)} record(s)\n"
                f"  expenses{year}.csv — {len(expense_rows)} record(s)\n"
                f"  revenue{year}.csv — {n_revenue} mining_income record(s)\n"
                f"  disposals{year}.csv — {disposal_summary}\n"
                f"Saved to {folder}/."
            ),
            "_translator_bypass": True,
        }

    # ── Notify ─────────────────────────────────────────────────────────────────

    async def _step_notify(
        self, year: str, income_rows: list, expense_rows: list,
        saved: list, errors: list, fifo_result=None,
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

        # FIFO disposal summary
        if fifo_result is not None:
            n_dr = len(fifo_result.disposal_results)
            if n_dr:
                total_gain = sum(d.gain_loss_nzd for d in fifo_result.disposal_results)
                lines.append(f"  FIFO disposals: {n_dr} lot match(es), net: ${total_gain:.2f} NZD")
            if fifo_result.data_gaps:
                lines.append(
                    f"  Data gaps: {len(fifo_result.data_gaps)} "
                    f"(queued to semantic:tax:data_gaps:{year})"
                )
            if fifo_result.unresolved:
                lines.append(
                    f"  Unresolved disposals: {len(fifo_result.unresolved)} (missing NZD value)"
                )

        n_revenue = sum(1 for r in income_rows if r.get("Classification") == "mining_income")
        if n_revenue:
            lines.append(f"  Mining revenue: {n_revenue} record(s) → revenue{year}.csv")
        lines.append(f"  Expense file: expenses{year}.csv")
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

    # ── FIFO helpers ───────────────────────────────────────────────────────────

    async def _run_fifo_for_year(self, year: str, start: str, end: str):
        """Query all-years tax events, enrich unpriced lots, run FIFO engine.

        Uses a cross-year lot pool (all acquisitions from 2000-01-01 to FY end)
        so disposals are matched against the correct historical cost basis even
        when the acquisition fell in a prior financial year.

        On-the-fly enrichment attempts to price any lot with nzd_value=None
        before passing to run_fifo(). Lots that remain unpriced after enrichment
        appear in FifoResult.data_gaps with reason='unpriced_acquisition_lot'.
        """
        from .classifier import classify_events
        from .fifo import run_fifo, FifoResult
        from .pricing import enrich_nzd

        all_events = await self._query_semantic_tax_events("2000-01-01T00:00:00Z", end)
        if not all_events:
            return FifoResult()

        classified = await classify_events(all_events, self.qdrant)

        _ACQ = frozenset({"mining_income", "staking_reward", "exchange_acquisition"})
        acquisition_lots = [ev for ev in classified.income if ev.subtype in _ACQ]
        fy_disposals = [
            ev for ev in classified.income
            if ev.subtype == "exchange_disposal" and _in_range(ev.timestamp, start, end)
        ]

        # Enrich acquisition lots that were unpriced at ingest time
        for ev in acquisition_lots:
            if ev.nzd_value is None and ev.asset and ev.timestamp:
                try:
                    raw = (ev.amount or "").split()[0]
                    nzd = await enrich_nzd(
                        ev.asset, ev.timestamp,
                        Decimal(raw) if raw else None,
                    )
                    if nzd:
                        ev.nzd_value = nzd
                except Exception as exc:
                    logger.warning(
                        "report_harness: on-the-fly enrichment failed %s %s: %s",
                        ev.asset, ev.timestamp[:10], exc,
                    )

        return run_fifo(acquisition_lots, fy_disposals)

    async def _store_data_gaps(self, year: str, gaps: list[dict]) -> None:
        """Write FIFO data gaps to semantic memory as an unresolved disposal queue.

        Key: semantic:tax:data_gaps:{year}
        Re-running /do_tax with more historical data overwrites this entry.
        """
        try:
            await self.qdrant.store(
                collection="semantic",
                content=f"FIFO data gaps FY{year}: {len(gaps)} unresolved disposal(s)",
                metadata={
                    "type":      "tax_data_gap",
                    "domain":    "tax",
                    "tax_year":  year,
                    "gaps":      gaps,
                    "gap_count": len(gaps),
                    "_key":      f"semantic:tax:data_gaps:{year}",
                },
            )
            logger.info(
                "report_harness: wrote %d data gap(s) to semantic:tax:data_gaps:%s",
                len(gaps), year,
            )
        except Exception as exc:
            logger.warning("report_harness: data_gaps store failed FY%s: %s", year, exc)

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


# ── Expense merge ─────────────────────────────────────────────────────────────


def _merge_expense_rows(file_rows: list[dict], receipt_rows: list[dict]) -> list[dict]:
    """Merge Director-supplied file rows with /receipt camera memory rows.

    Dedup key: (Date, Amount NZD). Receipt description wins over blank statement
    description when the same transaction appears in multiple sources.
    """
    seen: dict[tuple, dict] = {}
    for row in file_rows + receipt_rows:
        key = (row.get("Date", ""), row.get("Amount NZD", ""))
        if key not in seen:
            seen[key] = {**row}
        else:
            # Receipt processed last — its description overwrites blank statement fields
            if row.get("Description") and not seen[key].get("Description"):
                seen[key]["Description"] = row["Description"]
            if not seen[key].get("Vendor") and row.get("Vendor"):
                seen[key]["Vendor"] = row["Vendor"]
    return list(seen.values())


# ── CSV helpers ────────────────────────────────────────────────────────────────

_CARD_PREFIX_RE    = re.compile(r'^Card\s+\d+\s*:\s*', re.IGNORECASE)
_COUNTRY_CODE_RE   = re.compile(r'\s+[A-Z]{2,3}$')
_CITY_SUFFIX_RE    = re.compile(r'\s+[A-Z]{3,}$')
# Wirex NZD Statement uses comma-separated location: "MERCHANT,CITY,COUNTRY"
_COMMA_LOCATION_RE = re.compile(r',\s*[^,]+,\s*[A-Z]{2,3}$')


def _clean_vendor(raw: str) -> str:
    """Strip Wirex card prefix and trailing location from vendor string.

    "Card 8820 : www.aliexpress.com SHENZHEN CHN" → "www.aliexpress.com"
    "BAR YOKU,CHRISTCHURCH,NZL" → "BAR YOKU"
    "NETFLIX.COM LOS GATOS CA" → "NETFLIX.COM" (multi-pass until stable)
    Non-Wirex strings are returned unchanged.
    """
    clean = _CARD_PREFIX_RE.sub('', raw).strip()
    while True:
        prev = clean
        clean = _COUNTRY_CODE_RE.sub('', clean).strip()
        clean = _CITY_SUFFIX_RE.sub('', clean).strip()
        clean = _COMMA_LOCATION_RE.sub('', clean).strip()
        if clean == prev:
            break
    return clean or raw


def _date_to_dmy(iso_date: str) -> str:
    """Convert YYYY-MM-DD to DD/MM/YYYY (NZ accountant format)."""
    if not iso_date or len(iso_date) < 10:
        return iso_date
    try:
        dt = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


def _nzd_plain(nzd_str: str) -> str:
    """Convert '$X.XX NZD' to plain decimal string (no currency symbols)."""
    if not nzd_str:
        return ""
    return nzd_str.replace("$", "").replace("NZD", "").replace(",", "").strip()


def _source_label(source: str, metadata: dict | None = None) -> str:
    """Map a file source string (and optional metadata) to a human-readable Source column label."""
    s = (source or "").lower()
    if "aliexpress" in s:
        return "AliExpress"
    if "wirex" in s:
        return "Wirex"
    raw_type = ((metadata or {}).get("raw_type") or "").lower()
    # "Card Payment" is the Wirex NZD Statement row type for card spend
    if "wirex" in raw_type or raw_type == "card payment":
        return "Wirex"
    return "Receipt"


def _rate_nzd_per_eth(ev) -> str:
    """Return NZD/ETH rate for exchange_disposal rows (NZD Value ÷ ETH amount, 2 dp)."""
    if ev.subtype != "exchange_disposal":
        return ""
    try:
        nzd = _nzd_decimal(ev.nzd_value)
        if nzd is None:
            return ""
        amount_parts = (ev.amount or "").split()
        if not amount_parts:
            return ""
        eth = Decimal(amount_parts[0].replace(",", ""))
        if eth == 0:
            return ""
        return f"{(nzd / eth):.2f}"
    except (InvalidOperation, IndexError):
        return ""


def _income_row(ev) -> dict:
    """Convert a classified TaxEvent to an income CSV row dict."""
    return {
        "Date":           ev.timestamp[:10] if ev.timestamp else "",
        "Classification": ev.subtype or "unknown",
        "From Address":   ev.from_address or "",
        "To Address":     ev.to_address   or "",
        "Asset":          ev.asset         or "",
        "Amount":         ev.amount        or "",
        "NZD Value":      ev.nzd_value     or "",
        "Source":         ev.source        or "",
        "Reference":      ev.reference     or "",
        "Rate NZD/ETH":   _rate_nzd_per_eth(ev),
        "Notes":          "",
    }


def _expense_row(ev) -> dict:
    """Convert a TaxEvent (tax:expense) to an expenses_master CSV row dict."""
    raw_vendor  = ev.vendor or ev.source or ""
    src_label   = _source_label(ev.source or "", ev.metadata)
    meta_desc   = (ev.metadata or {}).get("description", "")
    description = "" if src_label == "Wirex" else meta_desc
    date_raw    = ev.timestamp[:10] if ev.timestamp else ""
    return {
        "Date":        _date_to_dmy(date_raw),
        "Source":      src_label,
        "Vendor":      _clean_vendor(raw_vendor),
        "Description": description,
        "Amount NZD":  _nzd_plain(ev.amount_nzd or ev.nzd_value or ""),
        "Reference":   ev.reference or "",
        "Notes":       "",
    }


def _build_income_csv(rows: list[dict]) -> str:
    """Serialise income rows to CSV string."""
    if not rows:
        rows = [{}]
    headers = ["Date", "Classification", "From Address", "To Address",
               "Asset", "Amount", "NZD Value", "Source", "Reference",
               "Rate NZD/ETH", "Notes"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _build_revenue_csv(income_rows: list[dict]) -> str:
    """Build mining revenue CSV: mining_income classified rows only.

    Narrower columns than income.csv — accountant view of ETH mining receipts.
    """
    rows = [r for r in income_rows if r.get("Classification") == "mining_income"]
    if not rows:
        rows = [{}]
    headers = ["Date", "Asset", "Amount", "NZD Value", "Reference"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _build_expense_csv(rows: list[dict]) -> str:
    """Serialise expense rows to expenses_master CSV string."""
    if not rows:
        rows = [{}]
    headers = ["Date", "Source", "Vendor", "Description", "Amount NZD", "Reference", "Notes"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _build_disposals_csv(fifo_result) -> str:
    """Serialise FifoResult to crypto_disposals CSV string.

    Sections: disposal_results (matched lots), unresolved (missing proceeds),
    data_gaps (insufficient or unpriced lots). Multiple rows per disposal event
    is expected when a single disposal spans more than one acquisition lot.
    """
    headers = [
        "Date", "Asset", "Amount Disposed", "Proceeds NZD",
        "Acquisition Date", "Acquisition Subtype", "Cost Basis NZD",
        "Gain/Loss NZD", "Reference", "Notes",
    ]
    rows: list[dict] = []

    for dr in fifo_result.disposal_results:
        rows.append({
            "Date":                dr.timestamp[:10] if dr.timestamp else "",
            "Asset":               dr.asset,
            "Amount Disposed":     f"{dr.amount_disposed:.6f}",
            "Proceeds NZD":        f"${dr.proceeds_nzd:.2f}",
            "Acquisition Date":    dr.acquisition_date,
            "Acquisition Subtype": dr.acquisition_subtype,
            "Cost Basis NZD":      f"${dr.cost_basis_nzd:.2f}",
            "Gain/Loss NZD":       f"${dr.gain_loss_nzd:.2f}",
            "Reference":           dr.reference,
            "Notes":               "",
        })

    for u in fifo_result.unresolved:
        rows.append({
            "Date":                (u.get("timestamp") or "")[:10],
            "Asset":               u.get("asset", ""),
            "Amount Disposed":     u.get("amount", ""),
            "Proceeds NZD":        "",
            "Acquisition Date":    "",
            "Acquisition Subtype": "",
            "Cost Basis NZD":      "",
            "Gain/Loss NZD":       "",
            "Reference":           u.get("reference", ""),
            "Notes":               "unresolved: missing proceeds NZD",
        })

    for g in fifo_result.data_gaps:
        rows.append({
            "Date":                (g.get("timestamp") or "")[:10],
            "Asset":               g.get("asset", ""),
            "Amount Disposed":     g.get("amount", ""),
            "Proceeds NZD":        "",
            "Acquisition Date":    g.get("acquisition_date", ""),
            "Acquisition Subtype": "",
            "Cost Basis NZD":      "",
            "Gain/Loss NZD":       "",
            "Reference":           g.get("reference", ""),
            "Notes":               f"data_gap: {g.get('reason', 'unknown')}",
        })

    if not rows:
        rows = [{}]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
