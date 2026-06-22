"""Receipt capture harness — photo → Gemini Vision → TaxEvent → Director confirm → Qdrant.

Session flag: _receipt_capture_checkpoint
Session key:  receipt_capture:session

Flow:
  1. Director sends photo with /receipt caption → handle_receipt_capture()
     - Gemini Vision extracts fields; parse + validate; write WM checkpoint
     - Return confirmation summary to Director
  2. Director replies 'yes' → handle_receipt_confirm('yes', ...)
     - Write TaxEvent to semantic/archive (RAID-durable)
     - Non-blocking Nextcloud photo upload
     - Clear checkpoint; confirm with point_id
  2b. Director replies with corrections → handle_receipt_confirm('vendor is X', ...)
     - Patch stored fields; re-write checkpoint; re-present summary
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from tax_harness.models import TaxEvent, make_tax_id, resolve_tax_year

logger = logging.getLogger(__name__)

_CHECKPOINT_FLAG = "_receipt_capture_checkpoint"
_CHECKPOINT_KEY  = "receipt_capture:session"

_EXTRACTION_PROMPT = """\
You are a receipt data extraction assistant. Analyse the receipt image and extract fields.
Return ONLY valid JSON — no preamble, no markdown fences, no explanation.

Required JSON structure:
{
  "vendor": "merchant name exactly as shown on receipt",
  "date": "YYYY-MM-DD",
  "amount_nzd": 45.50,
  "currency": "NZD",
  "reference": "invoice or order number if visible, null if not present",
  "category_hint": "one of: office_supplies, software, hardware, hosting, travel, meals, other"
}

Rules:
- date MUST be YYYY-MM-DD (ISO 8601). Never DD-MM-YYYY.
- amount_nzd MUST be a number, not a string.
- reference and category_hint may be null if not determinable.
"""


# ── JSON parsing helpers ───────────────────────────────────────────────────────

def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1
        end   = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        text = "\n".join(lines[start:end]).strip()
    return text


def _confirmation_summary(vendor: str, date_str: str, amount_d: Decimal,
                           currency: str, category_hint, reference: str, tax_year: str) -> str:
    return (
        f"*Receipt extracted:*\n"
        f"Vendor: {vendor}\n"
        f"Date: {date_str}\n"
        f"Amount: ${amount_d:.2f} {currency}\n"
        f"Category: {category_hint or 'unclassified'}\n"
        f"Reference: {reference}\n"
        f"Tax year: FY{tax_year}\n\n"
        "Correct? Reply 'yes' to confirm, or reply with corrections."
    )


# ── Public entry points ────────────────────────────────────────────────────────

async def handle_receipt_capture(
    image_b64: str,
    mime_type: str,
    qdrant,
    dcl,
    gemini_adapter,
    ledger=None,
) -> dict:
    """Extract receipt fields via Gemini Vision; write WM checkpoint; return confirmation summary."""
    from adapters.gemini import GeminiUnavailableError

    # DCL gate on the text prompt — image bytes are not classified
    dcl_result = dcl.prepare(_EXTRACTION_PROMPT, agent="receipt_capture", provider="gemini")
    if dcl_result.blocked:
        if ledger:
            try:
                dcl.log_call(dcl_result, ledger)
            except Exception:
                pass
        return {
            "status": "error",
            "director_message": "Receipt extraction blocked by DCL — content sensitivity exceeded threshold.",
        }

    # Gemini Vision call
    try:
        raw = await gemini_adapter.generate_with_image(
            prompt=dcl_result.content,
            image_b64=image_b64,
            mime_type=mime_type,
        )
    except GeminiUnavailableError as exc:
        logger.warning("receipt_capture: Gemini unavailable: %s", exc)
        return {
            "status": "error",
            "director_message": f"Gemini unavailable — cannot extract receipt data. ({exc})",
        }
    except Exception as exc:
        logger.warning("receipt_capture: Gemini call failed: %s", exc)
        return {
            "status": "error",
            "director_message": f"Gemini vision call failed: {exc}",
        }

    if ledger:
        try:
            dcl.log_call(dcl_result, ledger, output_tokens=raw.get("output_tokens", 0))
        except Exception:
            pass

    if raw.get("status") == "error" or not raw.get("response"):
        return {
            "status": "error",
            "director_message": "Gemini returned no response — please retake the photo in better lighting and try again.",
        }

    # Parse JSON — strip fences first
    raw_text = raw["response"]
    try:
        parsed = json.loads(_strip_json_fences(raw_text))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("receipt_capture: JSON parse failed: %s | raw: %.200s", exc, raw_text)
        return {
            "status": "parse_error",
            "director_message": "Could not parse receipt data from the image — please retake the photo and try again.",
        }

    # Field extraction with fallbacks
    vendor   = (parsed.get("vendor") or "Unknown Vendor").strip()
    date_str = parsed.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    currency = (parsed.get("currency") or "NZD").strip().upper()
    category_hint = parsed.get("category_hint")

    # Null-reference fallback — prevents UUID5 collision across all null-reference receipts
    ts_slug   = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    raw_ref   = parsed.get("reference")
    reference = raw_ref if raw_ref else f"photo:{ts_slug}:{vendor.lower().replace(' ', '_')[:20]}"

    # Amount via Decimal(str()) — safe from float representation errors
    try:
        amount_d = Decimal(str(parsed.get("amount_nzd") or 0))
        if amount_d <= 0:
            raise ValueError("amount must be positive")
    except (InvalidOperation, ValueError, TypeError) as exc:
        logger.warning("receipt_capture: amount parse failed: %s", exc)
        return {
            "status": "parse_error",
            "director_message": (
                f"Could not parse receipt amount (got: {parsed.get('amount_nzd')!r}). "
                "Please retake the photo and try again."
            ),
        }

    amount_nzd_str = f"${amount_d:.2f} NZD"

    # Parse date → ISO8601 UTC timestamp
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
    timestamp = dt.isoformat().replace("+00:00", "Z")

    tax_year = resolve_tax_year(timestamp)
    point_id = make_tax_id(reference)

    event = TaxEvent(
        id=point_id,
        event_tag="tax:expense",
        timestamp=timestamp,
        tax_year=tax_year,
        source="receipt_camera",
        reference=reference,
        nzd_value=amount_nzd_str,
        vendor=vendor,
        amount_nzd=amount_nzd_str,
        metadata={"category_hint": category_hint} if category_hint else {},
    )

    checkpoint = {
        _CHECKPOINT_FLAG: True,
        "session_id":      str(uuid.uuid4()),
        "current_step":    "pending_confirmation",
        "vendor":          vendor,
        "date":            date_str,
        "amount_nzd":      str(amount_d),
        "currency":        currency,
        "category_hint":   category_hint,
        "reference":       reference,
        "tax_year":        tax_year,
        "timestamp":       timestamp,
        "point_id":        point_id,
        "image_b64":       image_b64,
        "mime_type":       mime_type,
        "tax_event_payload": event.to_qdrant_payload(),
    }

    try:
        await qdrant.store(
            content=f"Receipt capture pending confirmation: {vendor} ${amount_d:.2f} {date_str}",
            collection="working_memory",
            metadata=checkpoint,
        )
    except Exception as exc:
        logger.warning("receipt_capture: checkpoint write failed: %s", exc)

    return {
        "status": "awaiting_confirm",
        "director_message": _confirmation_summary(
            vendor, date_str, amount_d, currency, category_hint, reference, tax_year
        ),
    }


async def handle_receipt_confirm(
    user_input: str,
    qdrant,
    nanobot=None,
) -> dict:
    """Process Director reply — 'yes' writes to memory; anything else is treated as corrections."""
    checkpoint = await _load_checkpoint(qdrant)
    if not checkpoint:
        return {
            "status": "no_checkpoint",
            "director_message": "No receipt pending. Send a photo with /receipt as the caption.",
        }

    if user_input.strip().lower() in ("yes", "y", "confirm"):
        return await _confirm_and_store(checkpoint, qdrant, nanobot)

    return await _apply_corrections(user_input, checkpoint, qdrant)


# ── Checkpoint helpers ─────────────────────────────────────────────────────────

async def _load_checkpoint(qdrant) -> dict | None:
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        hits, _ = await qdrant.client.scroll(
            collection_name="working_memory",
            scroll_filter=Filter(must=[
                FieldCondition(key=_CHECKPOINT_FLAG, match=MatchValue(value=True)),
            ]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if hits:
            return dict(hits[0].payload or {})
    except Exception as exc:
        logger.warning("receipt: checkpoint load failed: %s", exc)
    return None


async def _clear_checkpoint(qdrant) -> None:
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, PointIdsList
        hits, _ = await qdrant.client.scroll(
            collection_name="working_memory",
            scroll_filter=Filter(must=[
                FieldCondition(key=_CHECKPOINT_FLAG, match=MatchValue(value=True)),
            ]),
            limit=50,
            with_payload=False,
            with_vectors=False,
        )
        if hits:
            await qdrant.client.delete(
                collection_name="working_memory",
                points_selector=PointIdsList(points=[h.id for h in hits]),
            )
    except Exception as exc:
        logger.warning("receipt: checkpoint clear failed: %s", exc)


# ── Confirmation store ─────────────────────────────────────────────────────────

async def _confirm_and_store(checkpoint: dict, qdrant, nanobot) -> dict:
    """Write TaxEvent to semantic archive; kick off photo upload; clear checkpoint."""
    point_id  = checkpoint["point_id"]
    tax_year  = checkpoint["tax_year"]
    vendor    = checkpoint["vendor"]
    date_str  = checkpoint["date"]
    amount_s  = checkpoint["amount_nzd"]
    reference = checkpoint["reference"]
    image_b64 = checkpoint.get("image_b64", "")
    mime_type = checkpoint.get("mime_type", "image/jpeg")

    tax_payload = checkpoint.get("tax_event_payload", {})
    # Inject stable _key so qdrant.store() skips LLM key-generation
    stable_key  = f"tax:expense:receipt:{point_id[:8]}"
    tax_payload.setdefault("_key",   stable_key)
    tax_payload.setdefault("sov_id", point_id)

    content = f"Receipt: {vendor} — ${amount_s} NZD on {date_str} (ref: {reference})"

    try:
        await qdrant.store(
            content=content,
            collection="semantic",
            metadata=tax_payload,
        )
    except Exception as exc:
        logger.error("receipt: Qdrant store failed: %s", exc)
        return {
            "status": "error",
            "director_message": f"Failed to save receipt to memory: {exc}",
        }

    # Non-blocking Nextcloud photo upload — failure is logged, never surfaced
    if nanobot and image_b64:
        asyncio.create_task(
            _upload_receipt_photo(
                nanobot=nanobot,
                image_b64=image_b64,
                mime_type=mime_type,
                tax_year=tax_year,
                date_str=date_str,
                vendor=vendor,
                point_id=point_id,
            )
        )

    await _clear_checkpoint(qdrant)

    return {
        "status": "confirmed",
        "director_message": (
            f"Receipt saved.\n"
            f"Vendor: {vendor} | Amount: ${amount_s} NZD | FY{tax_year} | ID: {point_id[:8]}"
        ),
    }


# ── Corrections ───────────────────────────────────────────────────────────────

async def _apply_corrections(user_input: str, checkpoint: dict, qdrant) -> dict:
    """Parse correction text, patch checkpoint fields, re-write WM, re-present summary."""
    vendor        = checkpoint["vendor"]
    date_str      = checkpoint["date"]
    amount_d      = Decimal(checkpoint["amount_nzd"])
    currency      = checkpoint["currency"]
    category_hint = checkpoint.get("category_hint")
    reference     = checkpoint["reference"]
    timestamp     = checkpoint["timestamp"]
    tax_year      = checkpoint["tax_year"]
    point_id      = checkpoint["point_id"]

    u = user_input.lower()

    # Amount: "$45.50" or "45.50" or "amount is 45.50"
    m = re.search(r"\$?([\d]+\.[\d]{1,2})\s*(?:nzd)?", u)
    if m:
        try:
            amount_d = Decimal(m.group(1))
        except InvalidOperation:
            pass

    # Date: YYYY-MM-DD
    m = re.search(r"(\d{4}-\d{2}-\d{2})", user_input)
    if m:
        try:
            dt       = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            timestamp = dt.isoformat().replace("+00:00", "Z")
            tax_year  = resolve_tax_year(timestamp)
        except ValueError:
            pass

    # Vendor: "vendor is X" / "merchant is X" / "from X" / "store is X"
    m = re.search(r"(?:vendor|merchant|supplier|from|store)\s+(?:is\s+)?(.+?)(?:\.|,|$)", u)
    if m:
        vendor = m.group(1).strip().title()

    # Category
    for cat in ("office_supplies", "software", "hardware", "hosting", "travel", "meals", "other"):
        if cat.replace("_", " ") in u or cat in u:
            category_hint = cat
            break

    amount_nzd_str = f"${amount_d:.2f} NZD"
    event = TaxEvent(
        id=point_id,
        event_tag="tax:expense",
        timestamp=timestamp,
        tax_year=tax_year,
        source="receipt_camera",
        reference=reference,
        nzd_value=amount_nzd_str,
        vendor=vendor,
        amount_nzd=amount_nzd_str,
        metadata={"category_hint": category_hint} if category_hint else {},
    )

    updated = {
        **checkpoint,
        "vendor":          vendor,
        "date":            date_str,
        "amount_nzd":      str(amount_d),
        "currency":        currency,
        "category_hint":   category_hint,
        "tax_year":        tax_year,
        "timestamp":       timestamp,
        "tax_event_payload": event.to_qdrant_payload(),
    }

    try:
        await qdrant.store(
            content=f"Receipt capture pending confirmation: {vendor} ${amount_d:.2f} {date_str}",
            collection="working_memory",
            metadata=updated,
        )
    except Exception as exc:
        logger.warning("receipt: correction checkpoint write failed: %s", exc)

    return {
        "status": "awaiting_confirm",
        "director_message": "Updated. " + _confirmation_summary(
            vendor, date_str, amount_d, currency, category_hint, reference, tax_year
        ),
    }


# ── Nextcloud photo upload (background task) ───────────────────────────────────

async def _upload_receipt_photo(
    nanobot,
    image_b64: str,
    mime_type: str,
    tax_year: str,
    date_str: str,
    vendor: str,
    point_id: str,
) -> None:
    """Upload receipt photo to Nextcloud. Non-blocking — all failures are logged only."""
    import base64
    try:
        safe_vendor  = re.sub(r"[^a-z0-9]+", "_", vendor.lower())[:20].strip("_")
        filename     = f"{date_str}_{safe_vendor}_{point_id[:8]}.jpg"
        target_path  = f"/Digiant/Tax/FY{tax_year}/{filename}"
        image_bytes  = base64.b64decode(image_b64)

        # Ensure folder exists
        try:
            await nanobot.run("sovereign-nextcloud-fs", "fs_mkdir",
                              {"path": f"/Digiant/Tax/FY{tax_year}"})
        except Exception:
            pass

        # Binary upload → /downloads/{filename}
        upload_result = await nanobot.run_upload(
            filename, image_bytes, mime_type, len(image_bytes)
        )
        uploads_ok = (
            upload_result.get("status") == "ok"
            or upload_result.get("success") is True
        )
        if not uploads_ok:
            raise RuntimeError(f"run_upload failed: {upload_result.get('error', upload_result)}")

        # Move from /downloads/ to target path
        move_result = await nanobot.run(
            "sovereign-nextcloud-fs", "fs_move",
            {"src": f"/downloads/{filename}", "dest": target_path},
        )
        move_ok = (
            (move_result.get("result") or move_result).get("status") == "ok"
            or move_result.get("success") is True
        )
        if move_ok:
            logger.info("receipt: photo uploaded to %s", target_path)
            return

        # Fallback: leave it in /downloads/ with raw name
        raw_name = f"receipt_{point_id[:8]}.jpg"
        await nanobot.run("sovereign-nextcloud-fs", "fs_move",
                          {"src": f"/downloads/{filename}",
                           "dest": f"/Digiant/Tax/FY{tax_year}/{raw_name}"})
        logger.info("receipt: photo saved as raw filename in FY%s folder", tax_year)

    except Exception as exc:
        logger.warning("receipt: photo upload failed (non-fatal): %s", exc)
