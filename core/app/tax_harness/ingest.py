"""Tax Ingest Harness — file ingestion (CSV + PDF).

CSV: Python stdlib `csv` inline — handles Wirex, Swyftx/EasyCrypto, and Etherscan exports.
PDF: `pdf` skill via nanobot python3_exec.

Ingestion is dumb and fast — it records what happened faithfully.
No classification of income / disposal / internal transfer is performed here.
All tax treatment is determined by /do_tax at report time.

Event tag rules:
  tax:crypto  — any row involving a crypto asset (non-NZD currency).
                Exchange-side addresses populated as "wirex:account" or "swyftx:account".
                Etherscan rows carry the actual on-chain from/to addresses.
  tax:expense — any NZD fiat spend row (card spend, fee, receipt).

Etherscan format notes (newer export format as of 2026):
  Standard:  Transaction Hash, Status, Method, Blockno, DateTime (UTC), From, From_Nametag,
             To, To_Nametag, Amount, Value (USD), Txn Fee
  Internal:  Parent Transaction Hash, Status, Blockno, DateTime (UTC), From, From_Nametag,
             To, To_Nametag, Amount, Value (USD)
  Amount field is a combined string e.g. "0.45600614 ETH".
  Status is "Success"/"Fail" text; zero-amount and failed rows are skipped.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date as _date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import TaxEvent, format_amount, make_tax_id, resolve_tax_year

logger = logging.getLogger(__name__)

_TAX_BASE     = "/Digiant/Tax"
_INGESTED_TAG = "tax_file_ingested"

# Currency codes that indicate a crypto asset (not fiat)
_FIAT_CURRENCIES = {"NZD", "AUD", "USD", "EUR", "GBP", "JPY", "CAD", "CHF", "SGD"}


def _active_fy_folders() -> list[str]:
    """Return FY folder paths for the current and immediately prior NZ fiscal year.

    NZ FY YYYY: 1 Apr (YYYY-1) → 31 Mar YYYY.
    Example: today = May 2026 → current_fy = 2027; scan FY2027 and FY2026.
    Archived years (renamed to FYxxxx by Director but not in this range) are skipped.
    """
    today = _date.today()
    current_fy = today.year + 1 if today.month >= 4 else today.year
    return [
        f"{_TAX_BASE}/FY{current_fy}",
        f"{_TAX_BASE}/FY{current_fy - 1}",
    ]


# ── File listing ───────────────────────────────────────────────────────────────

async def list_unprocessed_files(nanobot) -> list[dict]:
    """Return files in active FY folders that do NOT have the tax_file_ingested tag.

    Only scans FY{current_fy} and FY{current_fy-1} — archived prior years are ignored.
    """
    all_files: list[dict] = []
    for folder in _active_fy_folders():
        try:
            nb = await nanobot.run(
                "sovereign-nextcloud-fs", "fs_list_recursive", {"path": folder}
            )
            result = nb.get("result") if nb.get("result") is not None else nb
            files = result if isinstance(result, list) else (
                result.get("files") or result.get("items") or []
                if isinstance(result, dict) else []
            )
            all_files.extend(files)
        except Exception as exc:
            logger.warning("ingest: list_unprocessed_files %s failed: %s", folder, exc)
    return [
        f for f in all_files
        if _INGESTED_TAG not in (f.get("tags") or [])
        and f.get("type", "file") in ("file", None)
    ]


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

    # Re-decode UTF-16 LE content that arrived latin-1-decoded (no BOM variant).
    # Wirex NZD Statement exports are UTF-16 LE without BOM; every ASCII char has a
    # trailing \x00 null byte which survives as U+0000 in the string when httpx falls
    # back to latin-1.  Detect by null density in the first 40 chars.
    if content and content[:40].count('\x00') >= 5:
        try:
            content = content.encode('latin-1').decode('utf-16-le')
        except Exception:
            logger.warning("ingest: UTF-16 LE re-decode failed for %s", file_path)

    # Strip UTF-8 BOM (Etherscan) or UTF-16 BOM that survived re-decode
    content = content.lstrip('﻿')

    # Auto-detect delimiter — Wirex NZD Statement uses ';', others use ','
    _sample = content[:4096]
    try:
        _dialect = csv.Sniffer().sniff(_sample, delimiters=',;\t|')
        _delim = _dialect.delimiter
    except csv.Error:
        _delim = ','

    def _reader(text: str) -> csv.DictReader:
        return csv.DictReader(io.StringIO(text), delimiter=_delim)

    raw_headers = list(_reader(content).fieldnames or [])
    headers = [h.lower().strip() for h in raw_headers]

    if _is_receipts_format(headers):
        return _parse_receipts_csv(_reader(content), source_label)
    elif _is_wirex_format(headers):
        return _parse_wirex_csv(_reader(content), source_label, headers)
    elif _is_swyftx_format(headers):
        return _parse_swyftx_csv(_reader(content), source_label)
    elif _is_etherscan_internal(headers):
        return await _parse_etherscan_csv(_reader(content), source_label, is_internal=True)
    elif _is_etherscan_standard(headers):
        return await _parse_etherscan_csv(_reader(content), source_label, is_internal=False)
    else:
        logger.warning(
            "ingest: unknown CSV format in %s — headers: %s", file_path, headers
        )
        return []


def _is_receipts_format(headers: list[str]) -> bool:
    header_set = set(headers)
    has_date   = "date" in header_set
    has_amount = (
        "amount nzd" in header_set
        or "total cost (incl gst)" in header_set
        or "total cost (incl. gst)" in header_set
    )
    has_desc   = "description" in header_set or "item" in header_set
    return has_date and has_amount and has_desc


def _is_wirex_format(headers: list[str]) -> bool:
    # Wirex NZD Statement (semicolon-delimited, UTF-16 LE, actual export format)
    if "completed date" in headers and "account currency" in headers:
        return True
    # Wirex trade export (legacy / speculative format)
    return (
        any("merchant" in h or "wirex" in h for h in headers)
        or ("transaction type" in headers and "currency" in headers and "amount" in headers)
    )


def _is_swyftx_format(headers: list[str]) -> bool:
    return (
        any("asset code" in h or "assetcode" in h for h in headers)
        or ("order type" in headers and ("asset" in headers or "amount" in headers))
    )


def _is_etherscan_standard(headers: list[str]) -> bool:
    return "transaction hash" in headers and "method" in headers


def _is_etherscan_internal(headers: list[str]) -> bool:
    return "parent transaction hash" in headers


async def _parse_etherscan_csv(
    reader: csv.DictReader,
    source: str,
    is_internal: bool,
) -> list[TaxEvent]:
    """Parse Etherscan standard or internal-transaction CSV export (2026 format).

    Standard:  Transaction Hash, Status, Method, Blockno, DateTime (UTC), From,
               From_Nametag, To, To_Nametag, Amount, Value (USD), Txn Fee
    Internal:  Parent Transaction Hash, Status, Blockno, DateTime (UTC), From,
               From_Nametag, To, To_Nametag, Amount, Value (USD)

    All rows → tax:crypto.  Failed rows (Status != "Success") and zero-amount rows
    are skipped.  NZD value fetched via CoinGecko; None on failure.
    """
    from .pricing import enrich_nzd

    events: list[TaxEvent] = []
    hash_field = "Parent Transaction Hash" if is_internal else "Transaction Hash"

    for row in reader:
        try:
            if (row.get("Status") or "").strip().lower() != "success":
                continue

            tx_hash = (row.get(hash_field) or "").strip()
            if not tx_hash:
                continue

            raw_dt = (row.get("DateTime (UTC)") or "").strip()
            timestamp = _normalise_timestamp(raw_dt)
            if not timestamp:
                continue

            from_address = (row.get("From") or "").strip().lower() or None
            to_address   = (row.get("To")   or "").strip().lower() or None

            # Amount is "0.45600614 ETH" — split on last space
            raw_amount = (row.get("Amount") or "").strip()
            if not raw_amount:
                continue
            parts = raw_amount.rsplit(" ", 1)
            try:
                amount_d = Decimal(parts[0].replace(",", ""))
            except InvalidOperation:
                continue
            asset = parts[1].upper() if len(parts) == 2 else "ETH"

            # Normalize WEI to ETH — Etherscan labels some 1-WEI transactions as "WEI"
            if asset == "WEI":
                amount_d = amount_d / Decimal("1000000000000000000")
                asset = "ETH"

            # Skip zero-value and dust rows (contract probes, 1-WEI spam, RP interactions)
            if amount_d == 0:
                continue
            if asset == "ETH" and amount_d < Decimal("0.0001"):
                continue

            reference = f"etherscan:{tx_hash}"
            nzd_value = await enrich_nzd(asset, timestamp, amount_d)

            events.append(TaxEvent(
                id=make_tax_id(reference),
                event_tag="tax:crypto",
                timestamp=timestamp,
                tax_year=resolve_tax_year(timestamp),
                source=source,
                reference=reference,
                nzd_value=nzd_value,
                from_address=from_address,
                to_address=to_address,
                asset=asset,
                amount=format_amount(amount_d, asset),
                tx_hash=tx_hash,
                metadata={
                    "etherscan_type": "internal" if is_internal else "standard",
                    "method": (row.get("Method") or "").strip(),
                },
            ))
        except Exception as exc:
            logger.warning("ingest: etherscan row parse error: %s", exc)

    return events


def _parse_wirex_csv(
    reader: csv.DictReader,
    source: str,
    headers: list[str] | None = None,
) -> list[TaxEvent]:
    """Dispatch to the correct Wirex sub-parser based on column headers."""
    if headers is None:
        headers = [h.lower().strip() for h in (reader.fieldnames or [])]
    if "completed date" in headers:
        return _parse_wirex_nzd_statement(reader, source)
    return _parse_wirex_trade_csv(reader, source)


def _parse_wirex_nzd_statement(reader: csv.DictReader, source: str) -> list[TaxEvent]:
    """Parse Wirex NZD Statement export (semicolon-delimited, UTF-16 LE, 2025+ format).

    Columns: Completed Date; Type; Description; Amount; Account Currency;
             Rate; Foreign Amount; Foreign Currency; Balance; Related Entity ID

    Card Payment rows with negative Amount → tax:expense (NZD card spend).
    Rows with a non-fiat Foreign Currency → tax:crypto (exchange trade; NZD value
    taken from Amount, crypto amount from Foreign Amount).
    All other rows (Top Up, Balance, positive Card Payment refunds, etc.) are skipped.
    """
    events: list[TaxEvent] = []
    for row in reader:
        try:
            raw_type   = (row.get("Type") or "").strip()
            raw_date   = (row.get("Completed Date") or "").strip()
            raw_amount = (row.get("Amount") or "0").strip()
            acct_ccy   = (row.get("Account Currency") or "NZD").strip().upper()
            foreign_ccy    = (row.get("Foreign Currency") or "").strip().upper()
            raw_foreign_amt = (row.get("Foreign Amount") or "0").strip()
            description    = (row.get("Description") or "").strip()
            external_id    = (row.get("Related Entity ID") or "").strip()

            if not raw_date:
                continue

            timestamp = _normalise_timestamp(raw_date)
            if not timestamp:
                continue

            try:
                amount_d = Decimal(raw_amount.replace(",", ""))
            except InvalidOperation:
                continue

            reference = f"wirex:{external_id}" if external_id else f"wirex:{timestamp}:{raw_type}"

            # Crypto exchange row — Foreign Currency is a non-fiat asset
            if foreign_ccy and foreign_ccy not in _FIAT_CURRENCIES:
                try:
                    foreign_d = abs(Decimal(raw_foreign_amt.replace(",", "")))
                except InvalidOperation:
                    foreign_d = None

                nzd_value = format_amount(abs(amount_d), "NZD") if amount_d else None
                # Direction: primary check from Description (reliable Wirex phrases);
                # fallback to NZD amount sign.
                # "Bought NZD with ETH/X" → sold crypto → disposal (sell)
                # Negative NZD / "Bought ETH/X with NZD" → acquired crypto → acquisition (buy)
                desc_lower = description.lower()
                if "bought nzd" in desc_lower:
                    direction = "sell"
                elif amount_d > 0:
                    direction = "sell"
                else:
                    direction = "buy"
                events.append(TaxEvent(
                    id=make_tax_id(reference),
                    event_tag="tax:crypto",
                    timestamp=timestamp,
                    tax_year=resolve_tax_year(timestamp),
                    source=source,
                    reference=reference,
                    nzd_value=nzd_value,
                    from_address="wirex:account",
                    to_address="wirex:account",
                    asset=foreign_ccy,
                    amount=format_amount(foreign_d, foreign_ccy) if foreign_d else None,
                    tx_hash=external_id or None,
                    metadata={"raw_type": raw_type, "description": description, "direction": direction},
                ))
                continue

            # Card spend — negative NZD amount
            if raw_type.lower() == "card payment" and amount_d < 0:
                spend = abs(amount_d)
                events.append(TaxEvent(
                    id=make_tax_id(reference),
                    event_tag="tax:expense",
                    timestamp=timestamp,
                    tax_year=resolve_tax_year(timestamp),
                    source=source,
                    reference=reference,
                    nzd_value=format_amount(spend, "NZD"),
                    vendor=description or raw_type,
                    amount_nzd=format_amount(spend, "NZD"),
                    metadata={"raw_type": raw_type},
                ))
                continue

            # All other rows (Top Up, positive refunds, etc.) — not taxable, skip

        except Exception as exc:
            logger.warning("ingest: wirex nzd statement row parse error: %s", exc)

    return events


def _parse_wirex_trade_csv(reader: csv.DictReader, source: str) -> list[TaxEvent]:
    """Parse legacy Wirex trade CSV export.

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


def _parse_receipts_csv(reader: csv.DictReader, source: str) -> list[TaxEvent]:
    """Parse manually-maintained receipts spreadsheet.

    Columns: Date, Merchant, Description, Amount NZD, Reference
    All rows → tax:expense. Description stored in full — no truncation.
    Date format: DD/MM/YYYY (NZ standard); other formats also accepted.
    Amount may include a leading '$' or comma thousands separators.
    Rows with missing date, unparseable amount, or zero/negative amount are skipped.
    """
    events: list[TaxEvent] = []
    for row in reader:
        try:
            raw_date    = (row.get("Date") or "").strip()
            merchant    = (
                row.get("Merchant") or row.get("Vendor") or row.get("Store") or ""
            ).strip()
            description = (row.get("Description") or row.get("Item") or "").strip()
            raw_amount  = (
                row.get("Amount NZD")
                or row.get("Total Cost (incl GST)")
                or row.get("Total Cost (incl. GST)")
                or ""
            ).strip()
            reference   = (
                row.get("Reference") or row.get("Order ID") or row.get("Order No.") or ""
            ).strip()

            if not raw_date or not raw_amount:
                continue

            timestamp = _normalise_timestamp(raw_date)
            if not timestamp:
                continue

            try:
                amount_d = Decimal(raw_amount.replace(",", "").replace("$", "").strip())
            except InvalidOperation:
                continue

            if amount_d <= 0:
                continue

            ref_key    = f"receipt:{reference}" if reference else f"receipt:{merchant}:{timestamp}"
            amount_nzd = format_amount(amount_d, "NZD")

            events.append(TaxEvent(
                id=make_tax_id(ref_key),
                event_tag="tax:expense",
                timestamp=timestamp,
                tax_year=resolve_tax_year(timestamp),
                source=source,
                reference=ref_key,
                nzd_value=amount_nzd,
                vendor=merchant,
                amount_nzd=amount_nzd,
                metadata={"description": description},
            ))
        except Exception as exc:
            logger.warning("ingest: receipts row parse error: %s", exc)

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
        "%d/%m/%Y",           # DD/MM/YYYY (four-digit year first to avoid ambiguity)
        "%d/%m/%y",           # DD/MM/YY  (NZ receipts spreadsheet: "10/03/26")
        "%d-%m-%Y %H:%M:%S",  # Wirex NZD Statement: "02-04-2025 00:00:03"
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
