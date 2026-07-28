"""Sharesies KiwiSaver Statement Ingestion Harness

Replaces the manually-maintained retirement-fund.md ledger. Statements land in
Nextcloud /portfolios/sharesies/ (Director uploads them); Rex parses them
deterministically (no LLM — label/value text is stable across all statements
observed 2023-2026) and stores structured data in Qdrant semantic memory.
Statements themselves stay in Nextcloud untouched, for tax/audit purposes.

Two document types, distinguished by filename:
  sharesies-kiwisaver-quarterly(-report)?-YYYY-MM-DD.pdf   -> "quarterly"
  sharesies-kiwisaver-annual-member-statement-YYYY-MM-DD.pdf -> "annual"

Both types can share the same period_end date (a March-quarter-end is also a
KiwiSaver-year-end, so an annual and a quarterly statement both dated e.g.
2026-03-31 are two DIFFERENT documents) — the Qdrant key includes
document_type to avoid one silently overwriting the other.

Quarterly reports do not give a single total-dollar fee figure (only a fund-
charge PERCENTAGE plus two dollar sub-components) — total_fees_nzd for a
quarterly record is transaction_fees + currency_conversion_fees, which
excludes the percentage-based underlying fund charge. Annual statements give
"Estimated total fees are $X" directly, which is used as-is.

Cost basis is not present in either statement type (checked against all 13
real files) — holdings only ever show current value at statement date.
Tickers are likewise never printed — `ticker` is always None here; joining
against a name->ticker table is deferred to a later layer per Director
instruction (2026-07-08), not built into this parser.

Public entry point: run_portfolio_ingest(cog, nanobot, qdrant) -> dict
"""

import logging
import re
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_STATEMENTS_PATH = "/portfolios/sharesies"

_FILENAME_RE = re.compile(
    r"sharesies-kiwisaver-(?:(?P<annual>annual-member-statement)|"
    r"quarterly(?:-report)?)-(?P<period>\d{4}-\d{2}-\d{2})\.pdf$",
    re.IGNORECASE,
)

# [ \t] (not \s) inside the label class — \s matches newlines too, which let a
# stray line-wrapped fragment from the PREVIOUS line bleed into the label
# (found live: "...administration char-\nges\nTransaction Fees $18.92" matched
# as label "rges\nTransaction Fees" instead of "Transaction Fees", silently
# losing the real fee figure). Keeping the label single-line is what makes
# this safe against arbitrary line-wrap points in the PDF text extraction.
_LABEL_VALUE_RE = re.compile(r"^([A-Za-z][A-Za-z \t'&/().]*?)[ \t]+(-?\$[\d,]+\.\d{2})[ \t]*$", re.MULTILINE)
_HOLDING_RE = re.compile(r"^(.+?)\s+\$([\d,]+\.\d{2})\s*$", re.MULTILINE)


def _to_float(dollar_str: str) -> float:
    neg = dollar_str.strip().startswith("-")
    digits = dollar_str.replace("-", "").replace("$", "").replace(",", "")
    val = float(digits)
    return -val if neg else val


def _extract_label_values(text: str) -> dict[str, float]:
    """Every "Label $X.XX" line in the document — includes holdings lines too
    (harmless noise; callers only look up the specific field names they need).
    Stable label text is what makes deterministic parsing possible here; if a
    future Sharesies statement changes wording, the specific .get() lookups
    below will start returning None rather than silently misparsing — surfaced
    via the "fields missing" check in parse_statement(), not a silent failure.
    """
    out: dict[str, float] = {}
    for label, value in _LABEL_VALUE_RE.findall(text):
        out[label.strip()] = _to_float(value)
    return out


def _extract_holdings(text: str, heading_pattern: str, closing_balance: float | None) -> list[dict]:
    m = re.search(
        rf"{heading_pattern}\n(.*?)(?:\n\. Other:|\nWhat you are on track|\nKey personnel|\Z)",
        text, re.S,
    )
    if not m:
        return []
    holdings = []
    for name, value in _HOLDING_RE.findall(m.group(1)):
        name = name.strip()
        if not name:
            continue
        value_nzd = _to_float(value)
        allocation_pct = (
            round(value_nzd / closing_balance * 100, 2)
            if closing_balance else None
        )
        holdings.append({
            "name": name, "ticker": None,
            "value_nzd": value_nzd, "allocation_pct": allocation_pct,
        })
    return holdings


def detect_statement_type(filename: str) -> str | None:
    m = _FILENAME_RE.search(filename)
    if not m:
        return None
    return "annual" if m.group("annual") else "quarterly"


def _period_end(filename: str) -> str | None:
    m = _FILENAME_RE.search(filename)
    return m.group("period") if m else None


def _parse_quarterly(text: str) -> dict:
    fields = _extract_label_values(text)

    opening_m = re.search(r"Opening Balance on[^\n$]*\$([\d,]+\.\d{2})", text)
    closing_m = re.search(r"Closing Balance on[^\n$]*\$([\d,]+\.\d{2})", text)
    opening = _to_float(opening_m.group(1)) if opening_m else None
    closing = _to_float(closing_m.group(1)) if closing_m else None

    transaction_fees = fields.get("Transaction Fees", 0.0)
    currency_fees    = fields.get("Currency conversion fees", 0.0)

    return {
        "opening_balance_nzd": opening,
        "closing_balance_nzd": closing,
        # NOTE: this is a 12-month trailing figure per the statement's own text
        # ("Contributions and withdrawals for the 12 month period to..."), NOT
        # this quarter alone — do not sum four quarters expecting an annual total.
        "member_contributions_nzd":   None,
        "employer_contributions_nzd": None,
        "government_contribution_nzd": None,
        "investment_returns_nzd":     None,
        "total_contributions_trailing_12mo_nzd": fields.get("Total contributions"),
        "total_withdrawals_trailing_12mo_nzd":   fields.get("Total withdrawals"),
        # Excludes the percentage-based "Total fund charges (estimate) X%" —
        # quarterly reports never give that as a dollar figure.
        "total_fees_nzd": round(transaction_fees + currency_fees, 2),
        "pir_tax_paid_nzd": None,
        "holdings": _extract_holdings(
            text, r"What is your KiwiSaver portfolio invested in\?", closing,
        ),
    }


def _parse_annual(text: str) -> dict:
    fields = _extract_label_values(text)

    balance_m = re.search(
        r"IRD number[^\n]*\n\$([\d,]+\.\d{2})\s+(-?)\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})",
        text,
    )
    opening = closing = None
    if balance_m:
        opening = _to_float(balance_m.group(1))
        closing = _to_float(balance_m.group(4))

    total_fees_m = re.search(r"Estimated total fees are \$([\d,]+\.\d{2})", text)

    return {
        "opening_balance_nzd": opening,
        "closing_balance_nzd": closing,
        "member_contributions_nzd":    fields.get("Member contributions"),
        "employer_contributions_nzd":  fields.get("Employer contributions"),
        "government_contribution_nzd": fields.get("Government contributions"),
        "investment_returns_nzd":      fields.get("Investment returns"),
        "total_contributions_trailing_12mo_nzd": None,
        "total_withdrawals_trailing_12mo_nzd":   None,
        "total_fees_nzd": _to_float(total_fees_m.group(1)) if total_fees_m else None,
        "pir_tax_paid_nzd": fields.get("Tax at your PIR"),
        "holdings": _extract_holdings(text, r"How your money is invested", closing),
    }


def parse_statement(text: str, document_type: str) -> dict:
    return _parse_quarterly(text) if document_type == "quarterly" else _parse_annual(text)


def _missing_fields(parsed: dict, document_type: str) -> list[str]:
    """Which expected fields came back None — surfaced to the Director instead
    of silently storing gaps, in case a future statement changes wording."""
    required = ["opening_balance_nzd", "closing_balance_nzd"]
    if document_type == "annual":
        required += ["member_contributions_nzd", "employer_contributions_nzd", "total_fees_nzd"]
    else:
        required += ["total_contributions_trailing_12mo_nzd", "total_fees_nzd"]
    return [f for f in required if parsed.get(f) is None]


async def ingest_statement_file(qdrant, nanobot, file_info: dict) -> dict | None:
    """Parse + store one statement. Returns a summary dict if newly ingested
    (or changed), None if this exact file version was already ingested —
    idempotent by (document_type, period_end) key, and by source file
    modified-timestamp so an unchanged statement is never silently
    re-notified on every /portfolio ingest run."""
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue

    filename = file_info.get("name", "")
    path = file_info.get("path", "")
    modified = file_info.get("modified")
    document_type = detect_statement_type(filename)
    if not document_type:
        return None
    period_end = _period_end(filename)
    _key = f"semantic:portfolio:sharesies:{document_type}:{period_end}"

    existing, _ = await qdrant.archive_client.scroll(
        collection_name="semantic",
        scroll_filter=Filter(must=[FieldCondition(key="_key", match=MatchValue(value=_key))]),
        limit=1, with_payload=True,
    )
    if existing and existing[0].payload.get("source_modified") == modified:
        return None  # unchanged since last ingest — nothing to do

    res = await nanobot.run("pypdf", "extract_text", {"path": path})
    result = res.get("result") if res.get("result") is not None else res
    text = result.get("text", "") if isinstance(result, dict) else ""
    if not text:
        logger.warning("portfolio_ingest: no text extracted from %r", filename)
        return None

    parsed = parse_statement(text, document_type)
    missing = _missing_fields(parsed, document_type)
    if missing:
        logger.warning("portfolio_ingest: %r missing fields %s — stored anyway", filename, missing)

    content_summary = (
        f"Sharesies KiwiSaver {document_type} statement, period ending {period_end}: "
        f"opening ${parsed['opening_balance_nzd']}, closing ${parsed['closing_balance_nzd']}, "
        f"fees ${parsed['total_fees_nzd']}, {len(parsed['holdings'])} holdings."
    )
    # qdrant.store() with an explicit _key is idempotent — same key derives the
    # same point ID (self.sovereign_id("mip_key", _key)), so re-ingesting the
    # same statement overwrites cleanly rather than accumulating duplicates.
    await qdrant.store(
        content=content_summary,
        metadata={
            "domain": "portfolio", "subject": "retirement",
            "_key": _key, "document_type": document_type, "period_end": period_end,
            "source_filename": filename, "source_modified": modified,
            **parsed, "missing_fields": missing,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        },
        collection="semantic",
    )
    return {
        "period_end": period_end, "document_type": document_type,
        "closing_balance_nzd": parsed["closing_balance_nzd"],
        "opening_balance_nzd": parsed["opening_balance_nzd"],
        "total_fees_nzd": parsed["total_fees_nzd"],
        "missing_fields": missing,
    }


async def run_portfolio_ingest(cog, nanobot, qdrant) -> dict:
    """/portfolio ingest — scan Nextcloud /portfolios/sharesies/, parse and
    store any new or changed statements, reply with a summary. Synchronous
    (parsing + a few Qdrant writes for ~13 files takes seconds, not minutes —
    unlike the deep 6-agent analysis harness, this doesn't need a background
    task + separate Telegram push; the director_message returned here IS the
    notification, delivered over the same Telegram round-trip. Does not
    trigger portfolio analysis — that's a separate, explicit
    /portfolio retirement command."""
    list_res = await nanobot.run("sovereign-nextcloud-fs", "fs_list_recursive", {"path": _STATEMENTS_PATH})
    result = list_res.get("result") if list_res.get("result") is not None else list_res
    items = result.get("items", []) if isinstance(result, dict) else []
    pdfs = [i for i in items if isinstance(i, dict) and i.get("name", "").lower().endswith(".pdf")]

    if not pdfs:
        return {
            "status": "ok", "requires_confirmation": False,
            "director_message": f"No statement files found in {_STATEMENTS_PATH}.",
        }

    ingested, unrecognised = [], []
    for f in pdfs:
        if not detect_statement_type(f.get("name", "")):
            unrecognised.append(f.get("name", ""))
            continue
        summary = await ingest_statement_file(qdrant, nanobot, f)
        if summary:
            ingested.append(summary)

    if not ingested:
        msg = f"No new or changed Sharesies statements ({len(pdfs)} on file, all already ingested)."
        if unrecognised:
            msg += f"\n{len(unrecognised)} unrecognised filename(s): {', '.join(unrecognised)}"
        return {"status": "ok", "requires_confirmation": False, "director_message": msg}

    ingested.sort(key=lambda r: r["period_end"])
    latest = ingested[-1]
    lines = [f"📊 Ingested {len(ingested)} Sharesies statement(s):"]
    for r in ingested:
        flag = " ⚠️ incomplete" if r["missing_fields"] else ""
        lines.append(
            f"• {r['period_end']} ({r['document_type']}) — closing "
            f"${r['closing_balance_nzd']:,.2f}, fees ${r['total_fees_nzd']:,.2f}{flag}"
        )
    lines.append(f"\nMost recent: {latest['period_end']} — balance ${latest['closing_balance_nzd']:,.2f}")
    if unrecognised:
        lines.append(f"\n{len(unrecognised)} unrecognised filename(s): {', '.join(unrecognised)}")

    return {
        "status": "ok", "requires_confirmation": False,
        "director_message": "\n".join(lines),
    }
