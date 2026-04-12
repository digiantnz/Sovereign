"""Tax Ingest Harness — file ingestion (CSV + PDF).

CSV: Python stdlib `csv` inline — handles Wirex and Swyftx/EasyCrypto exports.
PDF: `pdf` skill via nanobot python3_exec.

Ingestion is dumb and fast — it records what happened faithfully.
No classification of income / disposal / internal transfer is performed here.
All tax treatment is determined by /do_tax at report time.

Event tag rules:
  tax:crypto  — any row involving a crypto asset (non-NZD currency).
                Exchange-side addresses populated as "wirex:account" or "swyftx:account".
  tax:expense — any NZD fiat spend row (card spend, fee, receipt).
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import TaxEvent, format_amount, make_tax_id, resolve_tax_year

logger = logging.getLogger(__name__)

_TAX_FOLDER   = "/Digiant/Tax"
_INGESTED_TAG = "tax_file_ingested"

# Currency codes that indicate a crypto asset (not fiat)
_FIAT_CURRENCIES = {"NZD", "AUD", "USD", "EUR", "GBP", "JPY", "CAD", "CHF", "SGD"}


# ── File listing ───────────────────────────────────────────────────────────────

async def list_unprocessed_files(nanobot) -> list[dict]:
    """Return files in /Tax that do NOT have the tax_file_ingested tag."""
    try:
        nb = await nanobot.run(
            "sovereign-nextcloud-fs", "fs_list", {"path": _TAX_FOLDER}
        )
        result = nb.get("result") if nb.get("result") is not None else nb
        files = result if isinstance(result, list) else (
            result.get("files") or result.get("items") or []
            if isinstance(result, dict) else []
        )
        return [
            f for f in files
            if _INGESTED_TAG not in (f.get("tags") or [])
            and f.get("type", "file") in ("file", None)
        ]
    except Exception as exc:
        logger.warning("ingest: list_unprocessed_files failed: %s", exc)
        return []


# ── CSV ingestion ──────────────────────────────────────────────────────────────

async def ingest_csv_file(
    nanobot, file_path: str, source_label: str
) -> list[TaxEvent]:
    """Download and parse a CSV file from Nextcloud, returning TaxEvents."""
    try:
        nb = await nanobot.run(
            "sovereign-nextcloud-fs", "fs_read", {"path": file_path}
        )
        result = nb.get("result") if nb.get("result") is not None else nb
        content = (
            result.get("content", "") if isinstance(result, dict) else str(result)
        )
    except Exception as exc:
        logger.warning("ingest: fs_read %s failed: %s", file_path, exc)
        return []

    if not content.strip():
        return []

    reader = csv.DictReader(io.StringIO(content))
    raw_headers = list(reader.fieldnames or [])
    headers = [h.lower().strip() for h in raw_headers]

    if _is_wirex_format(headers):
        return _parse_wirex_csv(csv.DictReader(io.StringIO(content)), source_label)
    elif _is_swyftx_format(headers):
        return _parse_swyftx_csv(csv.DictReader(io.StringIO(content)), source_label)
    else:
        logger.warning(
            "ingest: unknown CSV format in %s — headers: %s", file_path, headers
        )
        return []


def _is_wirex_format(headers: list[str]) -> bool:
    return (
        any("merchant" in h or "wirex" in h for h in headers)
        or ("transaction type" in headers and "currency" in headers and "amount" in headers)
    )


def _is_swyftx_format(headers: list[str]) -> bool:
    return (
        any("asset code" in h or "assetcode" in h for h in headers)
        or ("order type" in headers and ("asset" in headers or "amount" in headers))
    )


def _parse_wirex_csv(reader: csv.DictReader, source: str) -> list[TaxEvent]:
    """Parse Wirex CSV export.

    Card spend rows (NZD fiat) → tax:expense with vendor and amount_nzd.
    Trade rows (crypto asset) → tax:crypto with "wirex:account" as both addresses.
    """
    events: list[TaxEvent] = []
    for row in reader:
        try:
            raw_type  = (row.get("Transaction Type") or row.get("transaction type") or "").lower()
            raw_amount = row.get("Amount") or row.get("amount") or "0"
            currency   = (row.get("Currency") or row.get("currency") or "NZD").upper()
            raw_date   = (
                row.get("Date") or row.get("date")
                or row.get("Transaction Date") or row.get("transaction date") or ""
            )
            external_id = (
                row.get("Transaction ID") or row.get("transaction id")
                or row.get("Reference") or row.get("reference") or ""
            )
            merchant = (
                row.get("Merchant") or row.get("merchant")
                or row.get("Description") or row.get("description") or ""
            )

            if not raw_date or not external_id:
                continue

            timestamp = _normalise_timestamp(raw_date)
            if not timestamp:
                continue

            try:
                amount_d = Decimal(str(raw_amount).replace(",", "").strip())
            except InvalidOperation:
                continue

            reference = f"wirex:{external_id}"

            if currency not in _FIAT_CURRENCIES:
                # Crypto trade row → tax:crypto
                events.append(TaxEvent(
                    id=make_tax_id(reference),
                    event_tag="tax:crypto",
                    timestamp=timestamp,
                    tax_year=resolve_tax_year(timestamp),
                    source=source,
                    reference=reference,
                    nzd_value=None,
                    from_address="wirex:account",
                    to_address="wirex:account",
                    asset=currency,
                    amount=format_amount(amount_d, currency),
                    tx_hash=external_id,
                    metadata={"merchant": merchant, "raw_type": raw_type},
                ))
            else:
                # Fiat card spend row → tax:expense
                events.append(TaxEvent(
                    id=make_tax_id(reference),
                    event_tag="tax:expense",
                    timestamp=timestamp,
                    tax_year=resolve_tax_year(timestamp),
                    source=source,
                    reference=reference,
                    nzd_value=format_amount(amount_d, "NZD"),
                    vendor=merchant or raw_type,
                    amount_nzd=format_amount(amount_d, "NZD"),
                    metadata={"raw_type": raw_type},
                ))
        except Exception as exc:
            logger.warning("ingest: wirex row parse error: %s", exc)

    return events


def _parse_swyftx_csv(reader: csv.DictReader, source: str) -> list[TaxEvent]:
    """Parse Swyftx/EasyCrypto CSV export.

    All rows are crypto trades → tax:crypto with "swyftx:account" as exchange addresses.
    /do_tax determines whether each trade is an acquisition or disposal at report time.
    """
    events: list[TaxEvent] = []
    for row in reader:
        try:
            asset = (
                row.get("Asset Code") or row.get("AssetCode")
                or row.get("Asset") or row.get("asset") or "NZD"
            ).upper()
            raw_amount  = row.get("Amount") or row.get("amount") or "0"
            order_type  = (row.get("Order Type") or row.get("order type") or "").lower()
            raw_date    = (
                row.get("Date") or row.get("date")
                or row.get("Transaction Date") or ""
            )
            external_id = (
                row.get("Order ID") or row.get("order id")
                or row.get("ID") or row.get("id") or ""
            )

            if not raw_date or not external_id:
                continue

            timestamp = _normalise_timestamp(raw_date)
            if not timestamp:
                continue

            try:
                amount_d = abs(Decimal(str(raw_amount).replace(",", "").strip()))
            except InvalidOperation:
                continue

            reference = f"swyftx:{external_id}"
            events.append(TaxEvent(
                id=make_tax_id(reference),
                event_tag="tax:crypto",
                timestamp=timestamp,
                tax_year=resolve_tax_year(timestamp),
                source=source,
                reference=reference,
                nzd_value=None,
                from_address="swyftx:account",
                to_address="swyftx:account",
                asset=asset,
                amount=format_amount(amount_d, asset),
                tx_hash=external_id,
                metadata={"order_type": order_type},
            ))
        except Exception as exc:
            logger.warning("ingest: swyftx row parse error: %s", exc)

    return events


def _normalise_timestamp(raw: str) -> str | None:
    """Normalise various date/datetime strings to ISO8601 UTC."""
    raw = raw.strip()
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
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
    logger.warning("ingest: cannot normalise timestamp %r", raw)
    return None


# ── PDF ingestion ──────────────────────────────────────────────────────────────

async def ingest_pdf_receipt(
    nanobot, file_path: str, source_label: str
) -> list[TaxEvent]:
    """Extract a tax:expense TaxEvent from a PDF receipt via the pdf skill."""
    try:
        nb = await nanobot.run("pdf", "extract_text", {"path": file_path})
        result = nb.get("result") if nb.get("result") is not None else nb
        text = result.get("text", "") if isinstance(result, dict) else str(result)
    except Exception as exc:
        logger.warning("ingest: pdf extract_text %s failed: %s", file_path, exc)
        return []

    if not text.strip():
        return []

    amount_match = re.search(r"\$\s*([\d,]+\.?\d*)", text)
    date_match   = re.search(
        r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}-\d{2}-\d{2})", text
    )
    vendor_match = re.search(r"(?:from|vendor|merchant|issued by)[:\s]+([^\n]+)", text, re.IGNORECASE)

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

    reference = f"receipt:{source_label}"
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
