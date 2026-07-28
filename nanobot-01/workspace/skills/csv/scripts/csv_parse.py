#!/usr/bin/env python3
"""csv — python3_exec skill for nanobot-01.

Parses tax-related CSV files from Nextcloud via WebDAV. Handles encoding
detection, delimiter sniffing, format classification, and row normalisation.
All column access is case-insensitive, which eliminates the class of bugs
caused by CSV exporters changing header capitalisation between versions.

Commands:
  parse_tax_csv  -- download + parse, return normalised rows as JSON

Output (parse_tax_csv):
  success: {status, path, format, total_rows, in_range_rows, skipped, rows:[...]}
  error:   {status:"error", error:"..."} + exit 1

Supported formats (auto-detected from headers):
  receipts            -- manually-maintained NZ expense spreadsheet
  wirex_nzd_statement -- Wirex NZD Statement (semicolon-delimited, UTF-16 LE)
  wirex_trade         -- legacy Wirex trade CSV
  easycrypto          -- EasyCrypto orders CSV
  swyftx              -- Swyftx/EasyCrypto legacy export
  etherscan_standard  -- Etherscan standard transaction export (2026 format)
  etherscan_internal  -- Etherscan internal transaction export (2026 format)

Unified row schema:
  event_tag   str  "tax:crypto" | "tax:expense"
  date_iso    str  ISO8601 UTC
  raw_date    str  original date string (for debugging)
  format      str  detected format name
  + format-specific fields (see per-parser docstrings)

Optional date range filter:
  Pass --start-date / --end-date (ISO8601) to return only in-range rows.
  total_rows always reflects parsed rows before filtering.
  skipped reflects rows dropped by the range filter + parse failures.

Env vars (injected by nanobot-01 from nanobot.env):
  NEXTCLOUD_URL            -- base URL (default: http://nextcloud)
  NEXTCLOUD_ADMIN_USER     -- WebDAV username
  NEXTCLOUD_ADMIN_PASSWORD -- WebDAV password
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

_NC_URL  = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NC_USER = os.environ.get("NEXTCLOUD_ADMIN_USER", "")
_NC_PASS = os.environ.get("NEXTCLOUD_ADMIN_PASSWORD", "")

_FIAT_CURRENCIES = frozenset({
    "NZD", "AUD", "USD", "EUR", "GBP", "JPY", "CAD", "CHF", "SGD",
})

_DUST_THRESHOLD_ETH = Decimal("0.0001")


# ── I/O helpers ────────────────────────────────────────────────────────────────

def _auth():
    return (_NC_USER, _NC_PASS)


def _dav_url(path: str) -> str:
    return f"{_NC_URL}/remote.php/dav/files/{_NC_USER}/{path.lstrip('/')}"


def _out(data: dict):
    print(json.dumps(data))
    sys.exit(0)


def _err(msg: str, **kw):
    print(json.dumps({"status": "error", "error": msg, **kw}))
    sys.exit(1)


def _log(msg: str):
    print(f"[csv] {msg}", file=sys.stderr, flush=True)


# ── Encoding detection ─────────────────────────────────────────────────────────

def _decode_bytes(data: bytes) -> str:
    """Decode raw CSV bytes; handles UTF-16 LE (Wirex), UTF-8 BOM (Etherscan)."""
    # UTF-16 LE with explicit BOM
    if data[:2] == b'\xff\xfe':
        return data[2:].decode('utf-16-le')
    # UTF-16 LE without BOM — detect by null-byte density in first 40 bytes
    if len(data) >= 40 and data[:40].count(b'\x00') >= 5:
        return data.decode('utf-16-le')
    # UTF-8 with or without BOM (utf-8-sig strips the BOM automatically)
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('latin-1')


# ── Delimiter sniffing ─────────────────────────────────────────────────────────

def _sniff_delim(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
    except csv.Error:
        return ','


# ── Case-insensitive CSV reader ────────────────────────────────────────────────

def _read_rows(content: str, delim: str) -> tuple[list[str], list[dict]]:
    """Return (lowercase_headers, rows_with_lowercase_keys).

    Normalising header case at read time means all parsers use lowercase keys
    unconditionally — no 'row.get("Date") vs row.get("date")' ambiguity.
    """
    reader = csv.DictReader(io.StringIO(content), delimiter=delim)
    headers = [h.lower().strip() for h in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        rows.append({k.lower().strip(): (v or "") for k, v in row.items() if k is not None})
    return headers, rows


# ── Date normalisation ─────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",   # EasyCrypto / Etherscan
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",             # NZ receipts DD/MM/YYYY (4-digit year first — avoid MM/DD ambiguity)
    "%d/%m/%y",             # NZ receipts DD/MM/YY  e.g. "10/03/26"
    "%d-%m-%Y %H:%M:%S",   # Wirex NZD Statement   e.g. "02-04-2025 00:00:03"
    "%d-%m-%Y",
    "%m/%d/%Y",             # US format fallback
]


def _normalise_ts(raw: str) -> str:
    """Normalise a date/datetime string to ISO8601 UTC. Returns "" on failure."""
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return ""


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO8601 UTC string back to a datetime (for range checks)."""
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _in_range(date_iso: str, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    dt = _parse_iso(date_iso)
    if dt is None:
        return False
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


# ── Amount cleaning ────────────────────────────────────────────────────────────

def _clean_amount(raw: str) -> Decimal | None:
    """Strip $, commas, currency suffixes; return Decimal or None on failure."""
    s = raw.strip().lstrip("$").replace(",", "").strip()
    # Drop trailing alphabetic suffix (e.g. "1.23 NZD" → "1.23")
    parts = s.split()
    if parts:
        s = parts[0]
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# ── Format detectors ───────────────────────────────────────────────────────────

def _is_receipts(h: list[str]) -> bool:
    s = set(h)
    has_amount = (
        "amount nzd" in s
        or "total cost (incl gst)" in s
        or "total cost (incl. gst)" in s
    )
    return "date" in s and has_amount and ("description" in s or "item" in s)


def _is_wirex_nzd(h: list[str]) -> bool:
    return "completed date" in h and "account currency" in h


def _is_wirex_trade(h: list[str]) -> bool:
    return (
        any("merchant" in x or "wirex" in x for x in h)
        or ("transaction type" in h and "currency" in h and "amount" in h)
    )


def _is_easycrypto(h: list[str]) -> bool:
    # EasyCrypto: Date, Order ID, Type, From symbol, To symbol, From amount, To amount, To address ...
    return "order id" in h and "from symbol" in h and "to symbol" in h


def _is_swyftx(h: list[str]) -> bool:
    return (
        any("asset code" in x or "assetcode" in x for x in h)
        or ("order type" in h and ("asset" in h or "amount" in h))
    )


def _is_etherscan_standard(h: list[str]) -> bool:
    return "transaction hash" in h and "method" in h


def _is_etherscan_internal(h: list[str]) -> bool:
    return "parent transaction hash" in h


# ── Parsers ────────────────────────────────────────────────────────────────────

def _parse_receipts(rows: list[dict], source: str) -> tuple[list[dict], list[str]]:
    """Parse manually-maintained NZ expense receipts spreadsheet.

    All rows → tax:expense.
    Date: DD/MM/YYYY or DD/MM/YY (NZ standard).
    Amount may have leading $, comma thousands separators.

    Row fields: event_tag, date_iso, raw_date, vendor, description,
                amount_nzd, reference, format
    """
    events: list[dict] = []
    skipped: list[str] = []

    for i, row in enumerate(rows):
        raw_date   = row.get("date", "").strip()
        vendor     = (row.get("merchant") or row.get("vendor") or row.get("store") or "").strip()
        desc       = (row.get("description") or row.get("item") or "").strip()
        raw_amount = (
            row.get("amount nzd")
            or row.get("total cost (incl gst)")
            or row.get("total cost (incl. gst)")
            or ""
        ).strip()
        reference  = (row.get("reference") or row.get("order id") or row.get("order no.") or "").strip()

        if i == 0:
            _log(f"receipts [{source}]: first row keys={list(row.keys())[:8]} "
                 f"raw_date={raw_date!r} raw_amount={raw_amount!r}")

        if not raw_date or not raw_amount:
            skipped.append(f"row {i+2}: missing date or amount")
            continue

        date_iso = _normalise_ts(raw_date)
        if not date_iso:
            skipped.append(f"row {i+2}: unparseable date {raw_date!r}")
            _log(f"receipts [{source}]: date {raw_date!r} did not match any known format")
            continue

        amount = _clean_amount(raw_amount)
        if amount is None or amount <= 0:
            skipped.append(f"row {i+2}: invalid amount {raw_amount!r}")
            continue

        events.append({
            "event_tag":   "tax:expense",
            "date_iso":    date_iso,
            "raw_date":    raw_date,
            "vendor":      vendor,
            "description": desc,
            "amount_nzd":  str(amount),
            "reference":   reference,
            "format":      "receipts",
        })

    _log(f"receipts [{source}]: {len(events)} events, {len(skipped)} skipped from {len(rows)} rows")
    return events, skipped


def _parse_wirex_nzd(rows: list[dict], source: str) -> tuple[list[dict], list[str]]:
    """Parse Wirex NZD Statement export (semicolon-delimited, UTF-16 LE, 2025+ format).

    Columns: Completed Date, Type, Description, Amount, Account Currency,
             Rate, Foreign Amount, Foreign Currency, Balance, Related Entity ID.

    Card Payment rows with negative Amount → tax:expense (NZD card spend).
    Rows with non-fiat Foreign Currency → tax:crypto (exchange trade).
    All other rows (Top Up, positive refunds, Balance) → skipped.

    Crypto row fields:   event_tag, date_iso, raw_date, asset, amount, nzd_value,
                         from_address, to_address, direction, reference, raw_type,
                         description, tx_hash, format
    Expense row fields:  event_tag, date_iso, raw_date, vendor, amount_nzd,
                         reference, raw_type, format
    """
    events: list[dict] = []
    skipped: list[str] = []

    for i, row in enumerate(rows):
        try:
            raw_type    = row.get("type", "").strip()
            raw_date    = row.get("completed date", "").strip()
            raw_amount  = row.get("amount", "0").strip()
            foreign_ccy = row.get("foreign currency", "").strip().upper()
            raw_foreign = row.get("foreign amount", "0").strip()
            description = row.get("description", "").strip()
            ext_id      = row.get("related entity id", "").strip()

            if not raw_date:
                skipped.append(f"row {i+2}: missing date")
                continue

            date_iso = _normalise_ts(raw_date)
            if not date_iso:
                skipped.append(f"row {i+2}: unparseable date {raw_date!r}")
                continue

            amount = _clean_amount(raw_amount)
            if amount is None:
                skipped.append(f"row {i+2}: invalid amount {raw_amount!r}")
                continue

            reference = (
                f"wirex:{ext_id}" if ext_id
                else f"wirex:{date_iso}:{raw_type}"
            )

            # Crypto exchange row
            if foreign_ccy and foreign_ccy not in _FIAT_CURRENCIES:
                foreign = _clean_amount(raw_foreign)
                desc_l  = description.lower()
                direction = (
                    "sell" if ("bought nzd" in desc_l or amount > 0)
                    else "buy"
                )
                events.append({
                    "event_tag":    "tax:crypto",
                    "date_iso":     date_iso,
                    "raw_date":     raw_date,
                    "asset":        foreign_ccy,
                    "amount":       str(abs(foreign)) if foreign else "",
                    "nzd_value":    str(abs(amount)),
                    "from_address": "wirex:account",
                    "to_address":   "wirex:account",
                    "direction":    direction,
                    "reference":    reference,
                    "raw_type":     raw_type,
                    "description":  description,
                    "tx_hash":      ext_id,
                    "format":       "wirex_nzd_statement",
                })
                continue

            # Card spend
            if raw_type.lower() == "card payment" and amount < 0:
                events.append({
                    "event_tag":  "tax:expense",
                    "date_iso":   date_iso,
                    "raw_date":   raw_date,
                    "vendor":     description or raw_type,
                    "amount_nzd": str(abs(amount)),
                    "reference":  reference,
                    "raw_type":   raw_type,
                    "format":     "wirex_nzd_statement",
                })
                continue

            # Top Up, positive Card Payment (refund), Balance rows — not taxable
            skipped.append(f"row {i+2}: {raw_type!r} not taxable")

        except Exception as exc:
            skipped.append(f"row {i+2}: parse error: {exc}")

    _log(f"wirex_nzd [{source}]: {len(events)} events, {len(skipped)} skipped from {len(rows)} rows")
    return events, skipped


def _parse_wirex_trade(rows: list[dict], source: str) -> tuple[list[dict], list[str]]:
    """Parse legacy Wirex trade CSV.

    Crypto rows → tax:crypto (from/to = wirex:account).
    Fiat rows → tax:expense.
    """
    events: list[dict] = []
    skipped: list[str] = []

    for i, row in enumerate(rows):
        try:
            raw_type   = (row.get("transaction type") or "").lower()
            raw_amount = row.get("amount") or "0"
            currency   = (row.get("currency") or "NZD").upper()
            raw_date   = row.get("date") or row.get("transaction date") or ""
            ext_id     = row.get("transaction id") or row.get("reference") or ""
            merchant   = row.get("merchant") or row.get("description") or ""

            if not raw_date or not ext_id:
                skipped.append(f"row {i+2}: missing date or id")
                continue

            date_iso = _normalise_ts(raw_date)
            if not date_iso:
                skipped.append(f"row {i+2}: unparseable date {raw_date!r}")
                continue

            amount = _clean_amount(str(raw_amount))
            if amount is None:
                skipped.append(f"row {i+2}: invalid amount {raw_amount!r}")
                continue

            reference = f"wirex:{ext_id}"

            if currency not in _FIAT_CURRENCIES:
                events.append({
                    "event_tag":    "tax:crypto",
                    "date_iso":     date_iso,
                    "raw_date":     raw_date,
                    "asset":        currency,
                    "amount":       str(amount),
                    "nzd_value":    "",
                    "from_address": "wirex:account",
                    "to_address":   "wirex:account",
                    "reference":    reference,
                    "raw_type":     raw_type,
                    "description":  merchant,
                    "tx_hash":      ext_id,
                    "format":       "wirex_trade",
                })
            else:
                events.append({
                    "event_tag":  "tax:expense",
                    "date_iso":   date_iso,
                    "raw_date":   raw_date,
                    "vendor":     merchant or raw_type,
                    "amount_nzd": str(amount),
                    "reference":  reference,
                    "raw_type":   raw_type,
                    "format":     "wirex_trade",
                })
        except Exception as exc:
            skipped.append(f"row {i+2}: parse error: {exc}")

    _log(f"wirex_trade [{source}]: {len(events)} events, {len(skipped)} skipped from {len(rows)} rows")
    return events, skipped


def _parse_easycrypto(rows: list[dict], source: str) -> tuple[list[dict], list[str]]:
    """Parse EasyCrypto orders CSV.

    Columns: Date, Order ID, Type, From symbol, To symbol, From amount,
             To amount, To address, To memo, Fiat Value, TXID, Network

    All rows are 'buy' (NZD → crypto), each row is a separate asset delivery.
    One Order ID may appear on multiple rows (same order buying ETH + BTC).

    NZD acquisition cost = From amount.
    Delivery address = To address (Director's wallet for that asset).

    Row fields: event_tag, date_iso, raw_date, asset, amount, nzd_value,
                from_address, to_address, reference, tx_hash, order_id, format
    """
    events: list[dict] = []
    skipped: list[str] = []

    for i, row in enumerate(rows):
        try:
            raw_date    = row.get("date", "").strip()
            order_id    = row.get("order id", "").strip()
            from_symbol = row.get("from symbol", "").strip().upper()
            to_symbol   = row.get("to symbol", "").strip().upper()
            raw_from    = row.get("from amount", "").strip()
            raw_to      = row.get("to amount", "").strip()
            to_address  = row.get("to address", "").strip()
            txid        = row.get("txid", "").strip()

            if not raw_date or not order_id:
                skipped.append(f"row {i+2}: missing date or order id")
                continue

            date_iso = _normalise_ts(raw_date)
            if not date_iso:
                skipped.append(f"row {i+2}: unparseable date {raw_date!r}")
                continue

            # from_symbol should be a fiat currency (NZD)
            nzd_amount = _clean_amount(raw_from)
            to_amount  = _clean_amount(raw_to)
            if to_amount is None or to_amount <= 0:
                skipped.append(f"row {i+2}: invalid to_amount {raw_to!r}")
                continue

            # Reference must include asset — one order can deliver multiple assets
            reference = f"easycrypto:{order_id}:{to_symbol}"

            events.append({
                "event_tag":    "tax:crypto",
                "date_iso":     date_iso,
                "raw_date":     raw_date,
                "asset":        to_symbol,
                "amount":       str(to_amount),
                "nzd_value":    str(nzd_amount) if nzd_amount is not None else "",
                "from_address": "easycrypto:account",
                "to_address":   to_address.lower() if to_address else "",
                "reference":    reference,
                "tx_hash":      txid,
                "order_id":     order_id,
                "format":       "easycrypto",
            })
        except Exception as exc:
            skipped.append(f"row {i+2}: parse error: {exc}")

    _log(f"easycrypto [{source}]: {len(events)} events, {len(skipped)} skipped from {len(rows)} rows")
    return events, skipped


def _parse_swyftx(rows: list[dict], source: str) -> tuple[list[dict], list[str]]:
    """Parse Swyftx/EasyCrypto legacy CSV.

    All rows → tax:crypto with swyftx:account as exchange addresses.
    """
    events: list[dict] = []
    skipped: list[str] = []

    for i, row in enumerate(rows):
        try:
            asset      = (
                row.get("asset code") or row.get("assetcode")
                or row.get("asset") or "NZD"
            ).upper()
            raw_amount = row.get("amount") or "0"
            order_type = (row.get("order type") or "").lower()
            raw_date   = row.get("date") or row.get("transaction date") or ""
            ext_id     = row.get("order id") or row.get("id") or ""

            if not raw_date or not ext_id:
                skipped.append(f"row {i+2}: missing date or id")
                continue

            date_iso = _normalise_ts(raw_date)
            if not date_iso:
                skipped.append(f"row {i+2}: unparseable date {raw_date!r}")
                continue

            amount = _clean_amount(str(raw_amount))
            if amount is None:
                skipped.append(f"row {i+2}: invalid amount {raw_amount!r}")
                continue

            events.append({
                "event_tag":    "tax:crypto",
                "date_iso":     date_iso,
                "raw_date":     raw_date,
                "asset":        asset,
                "amount":       str(abs(amount)),
                "nzd_value":    "",
                "from_address": "swyftx:account",
                "to_address":   "swyftx:account",
                "reference":    f"swyftx:{ext_id}",
                "tx_hash":      ext_id,
                "order_type":   order_type,
                "format":       "swyftx",
            })
        except Exception as exc:
            skipped.append(f"row {i+2}: parse error: {exc}")

    _log(f"swyftx [{source}]: {len(events)} events, {len(skipped)} skipped from {len(rows)} rows")
    return events, skipped


def _parse_etherscan(
    rows: list[dict], source: str, is_internal: bool
) -> tuple[list[dict], list[str]]:
    """Parse Etherscan standard or internal transaction export (2026 format).

    Standard columns:  Transaction Hash, Status, Method, Blockno, DateTime (UTC),
                       From, From_Nametag, To, To_Nametag, Amount, Value (USD), Txn Fee
    Internal columns:  Parent Transaction Hash, Status, Blockno, DateTime (UTC),
                       From, From_Nametag, To, To_Nametag, Amount, Value (USD)

    Amount field: "0.45600614 ETH" — split on last space.
    WEI amounts are normalised to ETH (1e-18 conversion) then dust-filtered.
    NZD value is NOT available in the export — caller must enrich via CoinGecko.

    Row fields: event_tag, date_iso, raw_date, tx_hash, from_address, to_address,
                asset, amount, nzd_value (always ""), method, format
    """
    fmt_name = "etherscan_internal" if is_internal else "etherscan_standard"
    hash_key = "parent transaction hash" if is_internal else "transaction hash"
    events: list[dict] = []
    skipped: list[str] = []

    for i, row in enumerate(rows):
        try:
            status = row.get("status", "").strip().lower()
            if status != "success":
                skipped.append(f"row {i+2}: status={status!r}")
                continue

            tx_hash = row.get(hash_key, "").strip()
            if not tx_hash:
                skipped.append(f"row {i+2}: missing tx hash")
                continue

            raw_date = row.get("datetime (utc)", "").strip()
            date_iso = _normalise_ts(raw_date)
            if not date_iso:
                skipped.append(f"row {i+2}: unparseable date {raw_date!r}")
                continue

            from_addr = row.get("from", "").strip().lower()
            to_addr   = row.get("to", "").strip().lower()

            raw_amount = row.get("amount", "").strip()
            if not raw_amount:
                skipped.append(f"row {i+2}: missing amount")
                continue
            parts = raw_amount.rsplit(" ", 1)
            try:
                amount_d = Decimal(parts[0].replace(",", ""))
            except InvalidOperation:
                skipped.append(f"row {i+2}: invalid amount {raw_amount!r}")
                continue
            asset = parts[1].upper() if len(parts) == 2 else "ETH"

            # Normalise WEI to ETH
            if asset == "WEI":
                amount_d = amount_d / Decimal("1000000000000000000")
                asset = "ETH"

            if amount_d == 0:
                skipped.append(f"row {i+2}: zero amount")
                continue
            if asset == "ETH" and amount_d < _DUST_THRESHOLD_ETH:
                skipped.append(f"row {i+2}: dust ({amount_d} ETH)")
                continue

            events.append({
                "event_tag":    "tax:crypto",
                "date_iso":     date_iso,
                "raw_date":     raw_date,
                "tx_hash":      tx_hash,
                "from_address": from_addr,
                "to_address":   to_addr,
                "asset":        asset,
                "amount":       str(amount_d),
                "nzd_value":    "",  # CoinGecko enrichment done by caller
                "method":       row.get("method", "").strip() if not is_internal else "",
                "format":       fmt_name,
            })
        except Exception as exc:
            skipped.append(f"row {i+2}: parse error: {exc}")

    _log(f"{fmt_name} [{source}]: {len(events)} events, {len(skipped)} skipped from {len(rows)} rows")
    return events, skipped


# ── Main command ───────────────────────────────────────────────────────────────

def cmd_parse_tax_csv(args):
    path   = args.path
    source = args.source or path.rsplit("/", 1)[-1]

    # Optional date range filter
    start_dt = _parse_iso(args.start_date) if args.start_date else None
    end_dt   = _parse_iso(args.end_date)   if args.end_date   else None
    if args.start_date and start_dt is None:
        _err(f"Invalid start_date: {args.start_date!r}")
    if args.end_date and end_dt is None:
        _err(f"Invalid end_date: {args.end_date!r}")

    # Fetch from Nextcloud
    try:
        r = requests.get(_dav_url(path), auth=_auth(), timeout=60)
    except Exception as exc:
        _err(f"WebDAV GET failed: {exc}")

    if r.status_code != 200:
        _err(f"GET {path} returned HTTP {r.status_code}", http_status=r.status_code)

    content = _decode_bytes(r.content)
    if not content.strip():
        _err(f"Empty file: {path}")

    delim = _sniff_delim(content[:4096])
    headers, rows = _read_rows(content, delim)

    _log(f"[{source}]: delim={delim!r} headers={headers[:10]} total_data_rows={len(rows)}")

    # Format detection — order matters: most-specific checks first
    if _is_receipts(headers):
        fmt = "receipts"
        events, parse_skipped = _parse_receipts(rows, source)
    elif _is_wirex_nzd(headers):
        fmt = "wirex_nzd_statement"
        events, parse_skipped = _parse_wirex_nzd(rows, source)
    elif _is_easycrypto(headers):
        fmt = "easycrypto"
        events, parse_skipped = _parse_easycrypto(rows, source)
    elif _is_wirex_trade(headers):
        fmt = "wirex_trade"
        events, parse_skipped = _parse_wirex_trade(rows, source)
    elif _is_swyftx(headers):
        fmt = "swyftx"
        events, parse_skipped = _parse_swyftx(rows, source)
    elif _is_etherscan_internal(headers):
        fmt = "etherscan_internal"
        events, parse_skipped = _parse_etherscan(rows, source, is_internal=True)
    elif _is_etherscan_standard(headers):
        fmt = "etherscan_standard"
        events, parse_skipped = _parse_etherscan(rows, source, is_internal=False)
    else:
        _log(f"[{source}]: unknown format — headers: {headers}")
        _err(f"Unknown CSV format — headers: {headers[:10]}", headers=headers)

    _log(f"[{source}]: detected format={fmt!r}")

    # Date range filter
    range_skipped: list[str] = []
    if start_dt is not None or end_dt is not None:
        filtered: list[dict] = []
        for ev in events:
            if _in_range(ev["date_iso"], start_dt, end_dt):
                filtered.append(ev)
            else:
                range_skipped.append(
                    f"{ev.get('date_iso', '?')} outside range "
                    f"[{args.start_date}..{args.end_date}]"
                )
        _log(f"[{source}]: date filter: {len(filtered)} in range, "
             f"{len(range_skipped)} out of range")
        events = filtered

    all_skipped = parse_skipped + range_skipped

    _out({
        "status":       "ok",
        "path":         path,
        "format":       fmt,
        "total_rows":   len(rows),
        "in_range_rows": len(events),
        "skipped":      len(all_skipped),
        "skip_reasons": all_skipped[:20],  # cap to avoid oversized payloads
        "rows":         events,
    })


def main():
    parser = argparse.ArgumentParser(description="Tax CSV parser for nanobot-01")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("parse_tax_csv")
    p.add_argument("--path",        required=True, help="Nextcloud file path")
    p.add_argument("--source",     default="",    help="Source label for logging")
    p.add_argument("--start_date", default="",    help="ISO8601 start date for range filter (inclusive)")
    p.add_argument("--end_date",   default="",    help="ISO8601 end date for range filter (inclusive)")

    args = parser.parse_args()
    if args.command == "parse_tax_csv":
        cmd_parse_tax_csv(args)
    else:
        _err(f"Unknown command: {args.command!r}")


if __name__ == "__main__":
    main()
