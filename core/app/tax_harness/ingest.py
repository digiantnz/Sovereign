"""Tax Ingest Harness — file ingestion (CSV + PDF).

CSV: `csv` nanobot skill — handles encoding, format detection, and parsing.
PDF: `pypdf` nanobot skill — text extraction.

Ingestion is dumb and fast — it records what happened faithfully.
No classification of income / disposal / internal transfer is performed here.
All tax treatment is determined by /do_tax at report time.

Event tag rules:
  tax:crypto  — any row involving a crypto asset (non-NZD currency).
                Exchange-side addresses populated as "wirex:account",
                "swyftx:account", or "easycrypto:account" as appropriate.
                Etherscan rows carry the actual on-chain from/to addresses.
  tax:expense — a receipt, invoice PDF, or fiat card spend row from CSV.

Etherscan rows arrive from the csv skill without nzd_value — this module
enriches them via CoinGecko before constructing the TaxEvent.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import TaxEvent, format_amount, make_tax_id, resolve_tax_year

logger = logging.getLogger(__name__)

_TAX_BASE     = "/Digiant/Tax"
_INGESTED_TAG = "tax_file_ingested"


# ── File listing ───────────────────────────────────────────────────────────────

async def list_unprocessed_files(nanobot) -> list[dict]:
    """Return untagged files anywhere under /Digiant/Tax/ (recursive).

    Scans the root rather than individual FY subfolders so files placed in the
    root before being sorted are still picked up. Tag-based dedup prevents
    reprocessing once a file has been ingested.
    """
    try:
        nb = await nanobot.run(
            "sovereign-nextcloud-fs", "fs_list_recursive", {"path": _TAX_BASE}
        )
        result = nb.get("result") if nb.get("result") is not None else nb
        all_files = result if isinstance(result, list) else (
            result.get("files") or result.get("items") or []
            if isinstance(result, dict) else []
        )
    except Exception as exc:
        logger.warning("ingest: list_unprocessed_files %s failed: %s", _TAX_BASE, exc)
        return []
    return [
        f for f in all_files
        if _INGESTED_TAG not in (f.get("tags") or [])
        and f.get("type", "file") in ("file", None)
    ]


# ── CSV ingestion ──────────────────────────────────────────────────────────────

def _row_to_tax_event(row: dict, source_label: str) -> TaxEvent | None:
    """Convert a parsed CSV skill row dict to a TaxEvent.

    Returns None if the row is missing required fields.
    nzd_value for Etherscan rows is left as None here — caller enriches via CoinGecko.
    """
    date_iso  = row.get("date_iso", "")
    event_tag = row.get("event_tag", "")
    fmt       = row.get("format", "")

    if not date_iso or not event_tag:
        return None

    if event_tag == "tax:expense":
        vendor     = row.get("vendor", "")
        raw_amount = row.get("amount_nzd", "")
        reference  = row.get("reference", "")
        raw_type   = row.get("raw_type", "")
        desc       = row.get("description", "")

        try:
            amount_d = Decimal(raw_amount)
        except (InvalidOperation, Exception):
            return None
        amount_nzd = format_amount(amount_d, "NZD")
        ref_key    = (
            f"receipt:{reference}" if reference
            else f"receipt:{vendor}:{date_iso}"
        )
        return TaxEvent(
            id=make_tax_id(ref_key),
            event_tag="tax:expense",
            timestamp=date_iso,
            tax_year=resolve_tax_year(date_iso),
            source=source_label,
            reference=ref_key,
            nzd_value=amount_nzd,
            vendor=vendor,
            amount_nzd=amount_nzd,
            metadata={k: v for k, v in {
                "description": desc,
                "raw_type": raw_type,
            }.items() if v},
        )

    elif event_tag == "tax:crypto":
        asset      = row.get("asset", "")
        raw_amount = row.get("amount", "")
        raw_nzd    = row.get("nzd_value", "")
        tx_hash    = row.get("tx_hash", "") or None
        from_addr  = row.get("from_address", "") or None
        to_addr    = row.get("to_address", "") or None
        reference  = row.get("reference", f"csv:{source_label}:{date_iso}")

        try:
            amount_d = Decimal(raw_amount)
        except (InvalidOperation, Exception):
            return None

        nzd_value: str | None = None
        if raw_nzd:
            try:
                nzd_value = format_amount(Decimal(raw_nzd), "NZD")
            except (InvalidOperation, Exception):
                pass

        # Build metadata from format-specific fields that are present
        meta: dict = {}
        for k in ("raw_type", "description", "direction", "method", "order_type", "order_id"):
            v = row.get(k, "")
            if v:
                meta[k] = v
        if fmt.startswith("etherscan"):
            meta["etherscan_type"] = "internal" if "internal" in fmt else "standard"

        return TaxEvent(
            id=make_tax_id(reference),
            event_tag="tax:crypto",
            timestamp=date_iso,
            tax_year=resolve_tax_year(date_iso),
            source=source_label,
            reference=reference,
            nzd_value=nzd_value,
            from_address=from_addr,
            to_address=to_addr,
            asset=asset,
            amount=format_amount(amount_d, asset),
            tx_hash=tx_hash,
            metadata=meta,
        )

    return None


async def ingest_csv_file(
    nanobot, file_path: str, source_label: str,
    start_date: str = "", end_date: str = "",
) -> list[TaxEvent]:
    """Parse a CSV file via the csv nanobot skill, returning TaxEvents.

    The csv skill handles encoding, delimiter sniffing, format detection, and
    row normalisation. Etherscan rows arrive without nzd_value and are enriched
    here via CoinGecko.

    start_date / end_date: optional ISO8601 strings forwarded to the skill
    for date-range filtering before payload is returned (reduces transfer size
    for large historical files like EasyCrypto going back to 2020).
    """
    from .pricing import enrich_nzd

    payload: dict = {"path": file_path, "source": source_label}
    if start_date:
        payload["start_date"] = start_date
    if end_date:
        payload["end_date"] = end_date

    try:
        nb = await nanobot.run("csv", "parse_tax_csv", payload)
        # Status lives on the outer nb wrapper — inner result dict has no "status" field
        if nb.get("status") != "ok":
            logger.warning(
                "ingest: csv skill error for %s: %s",
                file_path, nb.get("error", "unknown"),
            )
            return []
        result = nb.get("result") or {}
        if not isinstance(result, dict):
            logger.warning("ingest: csv skill returned unexpected type for %s", file_path)
            return []
        rows        = result.get("rows", [])
        fmt         = result.get("format", "unknown")
        total_rows  = result.get("total_rows", len(rows))
        skipped     = result.get("skipped", 0)
        skip_reasons = result.get("skip_reasons", [])
    except Exception as exc:
        logger.warning("ingest: csv skill failed for %s: %s", file_path, exc)
        return []

    logger.info(
        "ingest [%s]: format=%s total_rows=%d parsed=%d skipped=%d",
        source_label, fmt, total_rows, len(rows), skipped,
    )
    if skip_reasons:
        logger.debug("ingest [%s]: skip reasons: %s", source_label, skip_reasons[:5])

    events: list[TaxEvent] = []
    for row in rows:
        ev = _row_to_tax_event(row, source_label)
        if ev is None:
            continue

        # Etherscan rows need CoinGecko NZD enrichment — nzd_value is None here
        if (
            ev.event_tag == "tax:crypto"
            and ev.nzd_value is None
            and row.get("format", "").startswith("etherscan")
        ):
            try:
                raw = (ev.amount or "0").split()[0].replace(",", "")
                amount_d = Decimal(raw)
                ev.nzd_value = await enrich_nzd(ev.asset, ev.timestamp, amount_d)
            except Exception as exc:
                logger.warning(
                    "ingest: enrich_nzd failed for %s: %s", ev.reference, exc
                )

        events.append(ev)

    return events


# ── PDF ingestion ──────────────────────────────────────────────────────────────

def _normalise_timestamp(raw: str) -> str | None:
    """Normalise date/datetime strings to ISO8601 UTC. Used by PDF parser."""
    raw = raw.strip()
    formats = [
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d/%m/%y",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
        "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


async def ingest_pdf_receipt(
    nanobot, file_path: str, source_label: str
) -> list[TaxEvent]:
    """Extract a tax:expense TaxEvent from a PDF receipt via the pypdf skill."""
    try:
        nb = await nanobot.run("pypdf", "extract_text", {"path": file_path})
        result = nb.get("result") if nb.get("result") is not None else nb
        text = result.get("text", "") if isinstance(result, dict) else str(result)
    except Exception as exc:
        logger.warning("ingest: pypdf extract_text %s failed: %s", file_path, exc)
        return []

    if not text.strip():
        return []

    amount_match = re.search(r"\$\s*([\d,]+\.?\d*)", text)
    date_match   = re.search(
        r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}-\d{2}-\d{2})", text
    )
    vendor_match = re.search(
        r"(?:from|vendor|merchant|issued by)[:\s]+([^\n]+)", text, re.IGNORECASE
    )

    if not amount_match:
        logger.warning("ingest: no amount found in PDF %s", file_path)
        return []

    raw_amount = amount_match.group(1).replace(",", "")
    raw_date   = date_match.group(1) if date_match else ""
    vendor     = vendor_match.group(1).strip() if vendor_match else source_label
    timestamp  = (
        _normalise_timestamp(raw_date)
        if raw_date
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    try:
        amount_d = Decimal(raw_amount)
    except InvalidOperation:
        return []

    reference  = f"receipt:{source_label}"
    amount_nzd = format_amount(amount_d, "NZD")
    return [TaxEvent(
        id=make_tax_id(reference),
        event_tag="tax:expense",
        timestamp=timestamp,
        tax_year=resolve_tax_year(timestamp),
        source=source_label,
        reference=reference,
        nzd_value=amount_nzd,
        vendor=vendor,
        amount_nzd=amount_nzd,
        metadata={"pdf_path": file_path},
    )]


# ── Tag management ─────────────────────────────────────────────────────────────

async def mark_file_ingested(nanobot, file_path: str) -> bool:
    """Tag a Nextcloud file with tax_file_ingested."""
    try:
        nb = await nanobot.run(
            "sovereign-nextcloud-fs", "fs_tag",
            {"path": file_path, "tag": _INGESTED_TAG},
        )
        result = nb.get("result") if nb.get("result") is not None else nb
        return (
            nb.get("status") == "ok"
            or (isinstance(result, dict) and result.get("status") == "ok")
        )
    except Exception as exc:
        logger.warning("ingest: mark_file_ingested %s failed: %s", file_path, exc)
        return False
