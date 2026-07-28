"""Grounded calendar-event detection for a fetched email body (task #28).

One deterministic LLM call, run against the *actual* fetched message text —
never a paraphrase from conversation history. This exists specifically to
avoid the failure mode observed live: Rex confirming "I've booked a table"
without a real create_event call ever succeeding, and inventing a
restaurant name/time that were never in the source text. source_excerpt is
a verbatim quote so the Director can visually cross-check the extraction
before any calendar write happens.
"""
import json
import logging
import re
from datetime import datetime, timezone

from config import cfg as _cfg

logger = logging.getLogger(__name__)

# Cheap, zero-LLM pre-filter for the proactive mailbox scan (cognition_harness.py's
# run_score_email_by_subject) — that scan only has subject/sender metadata for
# every email and must not fetch a body + run detect_calendar_event()'s LLM call
# on all of them (same cost discipline as the rest of that harness: metadata-only
# triage, LLM only for the small fraction that clears a deterministic filter
# first). Deliberately broad/cheap — false positives just cost one body fetch +
# one LLM call, which detect_calendar_event() itself will then say "no" to;
# false negatives silently miss a real invite, which is the worse failure mode.
#
# Director-editable via sovereign-config.yaml (cognition.calendar_keywords),
# same pattern as urgency_keywords/known_brands — "at a minimum" (2026-07-07),
# not a fixed list; extend it as misses turn up rather than hardcoding more here.
def _build_calendar_candidate_re() -> re.Pattern:
    keywords = _cfg.cognition.calendar_keywords
    return re.compile(r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b', re.IGNORECASE)


_CALENDAR_CANDIDATE_RE = _build_calendar_candidate_re()


def looks_like_calendar_candidate(subject_line: str, sender: str = "") -> bool:
    """Deterministic keyword pass over subject/sender only — no body needed.
    Gate before fetching the body and calling detect_calendar_event()."""
    return bool(_CALENDAR_CANDIDATE_RE.search(f"{subject_line} {sender}"))


async def detect_calendar_event(cog, subject_line: str, sender: str,
                                 body_text: str, today_iso: str) -> dict | None:
    """Returns None if no genuine date/time commitment is stated in body_text,
    else {"title","date","start_time","end_time","location","source_excerpt"}."""
    if not body_text or not body_text.strip():
        return None

    from adapters.inference_queue import InferenceQueue

    prompt = f"""Today's date is {today_iso}.

An email was just read. Does it describe a SPECIFIC date/time commitment —
a restaurant reservation, meeting, appointment, or booking confirmation —
with an actual date and time stated or clearly implied in the text below?

Email subject: {subject_line}
Email from: {sender}
Email body:
{body_text[:3000]}

Rules:
- Only report an event if the date/time and what it's for are ACTUALLY STATED in the text above.
- Never invent or guess a title, location, or time that is not present in the text.
- If the email is advertising, a newsletter, or has no real date/time commitment, return found=false.
- source_excerpt must be a verbatim short quote (under 200 characters) from the body above
  that contains the date/time/place — not a paraphrase or summary.

Respond with JSON only — no preamble:
{{"found": false}}
or
{{"found": true, "title": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM or empty", "location": "...", "source_excerpt": "..."}}"""

    try:
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=45.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        if not data.get("found"):
            return None
        # Grounding check — if the model couldn't produce a real date/time/excerpt,
        # treat it as no-event rather than let a partial guess through.
        if not data.get("date") or not data.get("start_time") or not data.get("source_excerpt"):
            return None
        return {
            "title": (data.get("title") or "Event from email")[:200],
            "date": data["date"],
            "start_time": data["start_time"],
            "end_time": data.get("end_time") or "",
            "location": (data.get("location") or "")[:200],
            "source_excerpt": data["source_excerpt"][:200],
        }
    except Exception as exc:
        logger.warning("detect_calendar_event: failed, skipping suggestion: %s", exc)
        return None


# ── Durable pending-suggestion store (2026-07-23) ───────────────────────────
# A calendar suggestion surfaced by the proactive mailbox scan is sent straight
# to Telegram via task_scheduler.py's _notify_telegram — a direct Bot API call
# that never touches the gateway's SessionStore, so it never enters
# context_window. A later "book it" (or a fresh "create a calendar event for
# X" naming the same thing) therefore has nothing in context to ground on —
# observed live producing a fabricated date. Fix: a durable record in `meta`
# (same pattern as thoughts.py's inflight marker) written the moment the scan
# detects a candidate, checked by _quick_classify's event-confirm path when no
# in-context [event:{...}] tag is found, and cleared only once the event is
# actually created — so it survives any amount of intervening chat/notification
# traffic and can't be silently lost or double-booked.
def _suggestion_key(suggestion_id: str) -> str:
    return f"meta:calendar_suggestion:{suggestion_id}"


async def write_pending_suggestion(qdrant, suggestion_id: str, ev: dict,
                                    account: str, subject_line: str, sender: str) -> None:
    try:
        await qdrant.store(
            collection="meta",
            content=f"Calendar suggestion pending: {ev.get('title','')} — {ev.get('date','')} {ev.get('start_time','')}",
            metadata={
                "type": "meta",
                "_key": _suggestion_key(suggestion_id),
                "suggestion_id": suggestion_id,
                "status": "pending",
                "title": ev.get("title", ""),
                "date": ev.get("date", ""),
                "start_time": ev.get("start_time", ""),
                "end_time": ev.get("end_time", ""),
                "location": ev.get("location", ""),
                "source_excerpt": ev.get("source_excerpt", ""),
                "account": account,
                "email_subject": subject_line,
                "email_sender": sender,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("write_pending_suggestion: failed for %r: %s", suggestion_id, exc)


async def find_pending_suggestion(qdrant, match_text: str = "") -> dict | None:
    """Most relevant still-pending suggestion. Prefers one whose title/email
    subject/sender shares a real (>=4 char) word with match_text; falls back
    to the most recently created pending suggestion if nothing matches (or
    match_text is empty) — mirrors the ALL/word-overlap patterns used
    elsewhere for title matching, not a full NLP resolution."""
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchText
        points, _ = await qdrant.archive_client.scroll(
            collection_name="meta",
            scroll_filter=Filter(must=[
                FieldCondition(key="_key", match=MatchText(text="meta:calendar_suggestion:")),
            ]),
            limit=50, with_payload=True, with_vectors=False,
        )
        pending = [pt.payload for pt in points if (pt.payload or {}).get("status") == "pending"]
        if not pending:
            return None
        if match_text:
            words = {w.lower() for w in re.findall(r'[A-Za-z]{4,}', match_text)}
            def _hit(p):
                blob = f"{p.get('title','')} {p.get('email_subject','')} {p.get('email_sender','')}".lower()
                return any(w in blob for w in words)
            matched = [p for p in pending if _hit(p)]
            if matched:
                pending = matched
        pending.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        return pending[0]
    except Exception as exc:
        logger.warning("find_pending_suggestion: failed: %s", exc)
        return None


async def clear_pending_suggestion(qdrant, suggestion_id: str) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        await qdrant.archive_client.delete(
            collection_name="meta",
            points_selector=Filter(must=[
                FieldCondition(key="suggestion_id", match=MatchValue(value=suggestion_id)),
            ]),
        )
    except Exception as exc:
        logger.warning("clear_pending_suggestion: failed for %r: %s", suggestion_id, exc)
