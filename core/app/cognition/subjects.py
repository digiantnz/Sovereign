"""Cognition Engine — Subject confidence helpers + Subject Update (MID-tier HITL).

A Subject's confidence is a recency-weighted rolling average across campaign
outputs, not a single decayed scalar — half-life weighting requires knowing
each prior value's age at computation time, so the raw dated history has to
survive between campaigns (persisted on the Subject Nextcloud note and the
semantic:subject:<id> Qdrant payload as `confidence_history`).

Design principle: Qdrant is canonical, Nextcloud is the human-readable window.
If the Nextcloud notes disappeared, Rex's understanding stays intact in Qdrant.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_CONFIDENCE_MAP: dict[str, float] = {"HIGH": 0.75, "MEDIUM": 0.50, "LOW": 0.25}

_HISTORY_CAP = 5
_HALF_LIFE_DAYS = 90

# Per-subject in Subject frontmatter/payload ("confidence_target"); this is the
# fallback when a subject hasn't set one. Not a global constant applied uniformly —
# e.g. retirement might reasonably be 0.85, a casual-interest subject 0.60.
_DEFAULT_CONFIDENCE_TARGET = 0.75


def confidence_to_score(label: str) -> float:
    """Map a research harness categorical confidence string to a 0-1 score.

    Unrecognised labels default to MEDIUM (0.50) — mirrors the research
    harness's own fallback behaviour on parse failure.
    """
    return _CONFIDENCE_MAP.get((label or "").upper(), 0.50)


def rolling_confidence(
    history: list[dict], new_label: str, today: date,
) -> tuple[float, list[dict]]:
    """Fold a new campaign confidence into the subject's rolling average.

    Args:
        history: [{"date": "YYYY-MM-DD", "score": float}, ...], newest last,
            capped at _HISTORY_CAP entries. Empty on a subject's first campaign.
        new_label: "HIGH" | "MEDIUM" | "LOW" from the just-completed campaign.
        today: date to stamp the new entry with.

    Returns:
        (confidence, updated_history) — confidence is the value to store on
        the subject; updated_history is what to persist for next time.

    First campaign (history == []): no averaging — the single data point IS
    the confidence.
    """
    new_score = confidence_to_score(new_label)
    new_entry = {"date": today.isoformat(), "score": new_score}

    if not history:
        return new_score, [new_entry]

    updated = (history + [new_entry])[-_HISTORY_CAP:]

    weighted_sum = 0.0
    weight_total = 0.0
    for entry in updated:
        age_days = (today - date.fromisoformat(entry["date"])).days
        weight = 0.5 ** (age_days / _HALF_LIFE_DAYS)
        weighted_sum += weight * entry["score"]
        weight_total += weight

    confidence = weighted_sum / weight_total if weight_total else new_score
    return confidence, updated


async def _notify_telegram(message: str) -> None:
    """Direct Telegram POST — matches the pattern already duplicated per-module
    in task_scheduler.py and soul_guardian.py rather than introducing a new
    shared utility for a single existing 10-line helper."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as exc:
        logger.warning("subjects: Telegram notification failed: %s", exc)


async def get_subject(qdrant, subject_id: str) -> dict | None:
    """Fetch the full semantic:subject:<id> payload — the canonical registry
    entry (not the Nextcloud note). Includes note_id for Nextcloud updates."""
    return await qdrant.retrieve_by_key(f"semantic:subject:{subject_id}")


_PENDING_FLAG = "_cognition_pending_update"


def get_confidence_target(subject: dict) -> float:
    """Per-subject confidence_target, defaulting to _DEFAULT_CONFIDENCE_TARGET
    when unset. Never a global constant applied uniformly."""
    t = subject.get("confidence_target")
    return float(t) if t else _DEFAULT_CONFIDENCE_TARGET


async def evaluate_campaign_iteration(
    cog, subject_thesis: str, confidence_target: float,
    research_result: dict, iterations_used: int, max_iterations: int,
) -> dict:
    """Goal-seeking evaluate step — run after each research iteration.

    Two conditions must BOTH be true to iterate again: confidence below
    target, AND the LLM evaluator identifies specific resolvable gaps a
    further pass could plausibly answer. Below-target alone is not enough —
    burning iteration budget on a question the research harness can't
    resolve is wasted spend, so a below-target result with no resolvable
    gaps stops the campaign rather than exhausting the ceiling.

    Returns (schema fixed — feeds the Director notification and episodic log,
    so Rex knows why a campaign stopped, not just that it did):
        confidence:      float — this iteration's score
        target_met:      bool
        resolvable_gaps: list[str]
        iterate:         bool
        stop_reason:     "target_met" | "gaps_remain" | "no_resolvable_gaps" | "budget_exhausted"
    """
    current_score = confidence_to_score(research_result.get("confidence", "MEDIUM"))
    target_met = current_score >= confidence_target
    budget_remains = iterations_used < max_iterations

    if target_met:
        return {
            "confidence": current_score, "target_met": True,
            "resolvable_gaps": [], "iterate": False, "stop_reason": "target_met",
        }

    if not budget_remains:
        return {
            "confidence": current_score, "target_met": False,
            "resolvable_gaps": [], "iterate": False, "stop_reason": "budget_exhausted",
        }

    prompt = f"""You are evaluating one research iteration inside an ongoing Subject campaign.

Subject thesis: {subject_thesis}
Confidence target: {confidence_target:.0%}
This iteration's confidence: {current_score:.0%} ({research_result.get('confidence', 'MEDIUM')})
Research summary:
{chr(10).join('- ' + b for b in (research_result.get('telegram_summary') or []))}

The target was not met. Identify specific, resolvable gaps — questions a further
focused research pass could plausibly answer. If the remaining uncertainty isn't
something more research would resolve (diminishing returns), say so explicitly.

Respond with JSON only — no preamble:
{{"resolvable_gaps": ["...", "..."], "next_question": "..."}}

resolvable_gaps=[] if none identified (diminishing returns). next_question only
needed if resolvable_gaps is non-empty."""

    try:
        import json as _json
        import re as _re
        result = await cog.ask_local(prompt, timeout=60.0)
        raw = result.get("response", "")
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        data = _json.loads(m.group(0)) if m else {}
        resolvable_gaps = data.get("resolvable_gaps") or []
        iterate = bool(resolvable_gaps)  # both conditions already true: below target + gaps found
        return {
            "confidence":      current_score,
            "target_met":      False,
            "resolvable_gaps": resolvable_gaps,
            "iterate":         iterate,
            "stop_reason":     "gaps_remain" if iterate else "no_resolvable_gaps",
            "next_question":   data.get("next_question", "") if iterate else "",
        }
    except Exception as exc:
        logger.warning("evaluate_campaign_iteration: failed, treating as no resolvable gaps: %s", exc)
        return {
            "confidence": current_score, "target_met": False, "resolvable_gaps": [],
            "iterate": False, "stop_reason": "no_resolvable_gaps",
        }


_STOP_REASON_LABELS = {
    "target_met":         "target reached",
    "gaps_remain":        "stopped mid-budget with gaps still open (unexpected — should have iterated)",
    "no_resolvable_gaps": "stopped — remaining uncertainty judged not resolvable by further research",
    "budget_exhausted":   "iteration budget exhausted",
}


async def _log_campaign_stop_episodic(
    qdrant, subject_id: str, campaign_note_id: str | None,
    stop_reason: str, resolvable_gaps: list[str],
    confidence: float, target: float, iterations_used: int,
) -> None:
    """Episodic record of WHY a campaign stopped — distinct from the research-
    complete episodic entry (one campaign may run several research iterations,
    but there is exactly one stop-reason event). Fires regardless of whether
    the Director later approves or rejects the resulting Subject Update —
    this is an audit fact about the campaign, not the subject state change."""
    try:
        today = date.today().isoformat()
        await qdrant.store(
            collection="episodic",
            content=(
                f"Campaign for subject '{subject_id}' stopped after {iterations_used} "
                f"iteration(s): {stop_reason} (confidence {confidence:.0%} vs target {target:.0%})."
            ),
            metadata={
                "type": "episodic", "event_type": "campaign_stop",
                "subject": subject_id, "campaign_note_id": campaign_note_id,
                "stop_reason": stop_reason, "resolvable_gaps": resolvable_gaps,
                "confidence": confidence, "confidence_target": target,
                "iterations_used": iterations_used, "ts": today,
            },
        )
    except Exception as exc:
        logger.warning("_log_campaign_stop_episodic: failed for %r: %s", subject_id, exc)


async def propose_subject_update(
    qdrant, subject_id: str, campaign_note_id: str | None,
    trigger_source: str, trigger_summary: str, research_result: dict,
    confidence_target: float | None = None,
    target_met: bool = True, resolvable_gaps: list[str] | None = None,
    stop_reason: str = "target_met", iterations_used: int = 1,
) -> None:
    """Write a pending Subject Update proposal + notify Director + log the
    campaign's stop reasoning to episodic.

    MID-tier HITL — mirrors the existing Prospective Task Approval Flow
    exactly, just keyed to a campaign's working_memory entry instead of a
    PROSPECTIVE task. No new confirmation mechanism. Applied later by
    apply_subject_update() on the Director's "approve <subject>" /
    "reject <subject>" reply (see execution/engine.py _quick_classify).

    target_met/resolvable_gaps/stop_reason: when the campaign stopped without
    reaching confidence_target, the proposal — and the Telegram notification —
    surface that explicitly (achieved vs. target, why it stopped, remaining
    gaps) rather than presenting it as a clean success. stop_reason also
    drives the episodic log so Rex knows *why* a campaign stopped, not just
    that it did.

    Accepted risk (not solved here): working_memory is ephemeral tmpfs — a
    container restart before the Director replies loses this proposal
    silently, same as any other in-flight checkpoint today.
    """
    subject = await get_subject(qdrant, subject_id)
    if not subject:
        logger.warning("propose_subject_update: unknown subject %r", subject_id)
        return

    today = date.today()
    history = subject.get("confidence_history") or []
    new_label = research_result.get("confidence", "MEDIUM")
    new_confidence, updated_history = rolling_confidence(history, new_label, today)
    old_confidence = subject.get("confidence", 0.5)
    target = confidence_target if confidence_target is not None else get_confidence_target(subject)
    resolvable_gaps = resolvable_gaps or []

    await _log_campaign_stop_episodic(
        qdrant, subject_id, campaign_note_id, stop_reason, resolvable_gaps,
        confidence_to_score(new_label), target, iterations_used,
    )

    proposal = {
        "subject_id":             subject_id,
        "campaign_note_id":       campaign_note_id,
        "trigger_source":         trigger_source,
        "trigger_summary":        trigger_summary,
        "old_confidence":         old_confidence,
        "new_confidence":         new_confidence,
        "new_confidence_history": updated_history,
        "confidence_label":       new_label,
        "full_report":            research_result.get("full_report", ""),
        "telegram_summary":       research_result.get("telegram_summary") or [],
        "old_thesis":             subject.get("thesis", ""),
        "target_met":             target_met,
        "resolvable_gaps":        resolvable_gaps,
        "stop_reason":            stop_reason,
    }

    await qdrant.store(
        collection="working_memory",
        content=f"Cognition Engine pending Subject Update — {subject_id}",
        metadata={
            _PENDING_FLAG:  True,
            "_key":         f"cognition:pending_update:{subject_id}",
            "subject_id":   subject_id,
            "proposal":     proposal,
            "ts":           datetime.now(timezone.utc).isoformat(),
        },
    )

    delta_pct = round((new_confidence - old_confidence) * 100)
    delta_str = f"{delta_pct:+d}pp" if delta_pct else "no change"
    summary = "\n".join(f"• {b}" for b in proposal["telegram_summary"][:3])
    if target_met:
        header = f"🧠 *Campaign complete — {subject_id}*"
    else:
        header = f"🧠 *Campaign stopped — {subject_id}* ({_STOP_REASON_LABELS.get(stop_reason, stop_reason)})"
    lines = [
        header,
        f"Confidence: {old_confidence:.0%} → {new_confidence:.0%} ({delta_str}, target {target:.0%})",
        "",
        summary,
    ]
    if not target_met and resolvable_gaps:
        lines += ["", "Open questions:"] + [f"- {q}" for q in resolvable_gaps]
    lines += ["", f"Reply *approve {subject_id}* or *reject {subject_id}*."]
    await _notify_telegram("\n".join(lines))


async def read_pending_update(qdrant, subject_id: str) -> dict | None:
    """Read a pending proposal for a subject, if one exists."""
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        points, _ = await qdrant.client.scroll(
            collection_name="working_memory",
            scroll_filter=Filter(must=[
                FieldCondition(key=_PENDING_FLAG, match=MatchValue(value=True)),
                FieldCondition(key="subject_id", match=MatchValue(value=subject_id)),
            ]),
            limit=1, with_payload=True, with_vectors=False,
        )
        return points[0].payload if points else None
    except Exception as exc:
        logger.warning("read_pending_update: failed for %r: %s", subject_id, exc)
        return None


async def _clear_pending_update(qdrant, subject_id: str) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        await qdrant.client.delete(
            collection_name="working_memory",
            points_selector=Filter(must=[
                FieldCondition(key=_PENDING_FLAG, match=MatchValue(value=True)),
                FieldCondition(key="subject_id", match=MatchValue(value=subject_id)),
            ]),
        )
    except Exception as exc:
        logger.warning("_clear_pending_update: failed for %r: %s", subject_id, exc)


async def apply_subject_update(qdrant, nanobot, subject_id: str, approved: bool) -> dict:
    """Apply or discard a pending Subject Update proposal.

    Called from the Director's approve/reject reply. On approve: writes the
    Nextcloud Subject Note, the semantic:subject:<id> Qdrant upsert, the
    research semantic entry (subject-tagged — see research_harness.py
    _write_research_semantic), the episodic event, and archives the campaign
    note. On reject: archives the campaign note only, nothing else changes.
    """
    pending = await read_pending_update(qdrant, subject_id)
    if not pending:
        return {"status": "error", "error": f"No pending update for subject {subject_id!r}."}

    proposal = pending.get("proposal", {})
    campaign_note_id = proposal.get("campaign_note_id")
    today = date.today().isoformat()

    if not approved:
        if campaign_note_id:
            await _archive_campaign_note(nanobot, campaign_note_id, status="rejected")
        await _clear_pending_update(qdrant, subject_id)
        return {"status": "ok", "action": "rejected", "subject_id": subject_id}

    subject = await get_subject(qdrant, subject_id)
    note_id = subject.get("note_id") if subject else None
    # Fields that must survive the upsert unchanged — qdrant.store() REPLACES the
    # whole payload (not a merge), so anything not explicitly re-included here is
    # silently dropped on every approved update. confidence_target in particular
    # is a Director-set value (not always the 0.75 default) that must not be lost.
    confidence_target = get_confidence_target(subject) if subject else _DEFAULT_CONFIDENCE_TARGET
    open_questions     = (subject or {}).get("open_questions", [])
    knowns             = (subject or {}).get("knowns", [])
    new_confidence = proposal["new_confidence"]
    new_history    = proposal["new_confidence_history"]
    summary_lines  = "\n".join(f"- {b}" for b in proposal.get("telegram_summary", []))

    # 1. Nextcloud Subject Note — narrative update
    if note_id:
        note_content = (
            "---\n"
            "type: subject\n"
            f"subject: {subject_id}\n"
            "status: active\n"
            f"confidence: {new_confidence:.2f}\n"
            f"last_updated: {today}\n"
            f"last_campaign: {today}\n"
            f"confidence_history: {json.dumps(new_history)}\n"
            f"confidence_target: {confidence_target}\n"
            "---\n\n"
            f"## Thesis\n{proposal.get('old_thesis', '')}\n\n"
            f"## Latest Campaign Update ({today})\n"
            f"Trigger: {proposal.get('trigger_summary', '')}\n\n"
            f"{summary_lines}\n"
        )
        try:
            await nanobot.run("openclaw-nextcloud", "notes_update", {
                "note-id": note_id, "content": note_content,
            })
        except Exception as exc:
            logger.warning("apply_subject_update: notes_update failed for %r: %s", subject_id, exc)

    # 2. semantic:subject:<id> Qdrant upsert (idempotent by key — overwrites in place)
    try:
        await qdrant.store(
            collection="semantic",
            content=(
                f"Subject: {subject_id}\nThesis: {proposal.get('old_thesis', '')}\n"
                f"Latest update: {proposal.get('trigger_summary', '')}"
            ),
            metadata={
                "type": "semantic", "domain": "subject",
                "_key": f"semantic:subject:{subject_id}",
                "subject": subject_id, "status": "active",
                "confidence": new_confidence,
                "confidence_history": new_history,
                "confidence_target": confidence_target,
                "thesis": proposal.get("old_thesis", ""),
                "open_questions": open_questions,
                "knowns": knowns,
                "note_id": note_id,
                "last_campaign": today,
            },
        )
    except Exception as exc:
        logger.warning("apply_subject_update: semantic upsert failed for %r: %s", subject_id, exc)

    # 3. Research semantic entry + episodic event — subject-tagged (additive metadata,
    #    no existing write path changes — see research_harness.py _write_research_semantic/_write_episodic)
    try:
        from monitoring.research_harness import _write_research_semantic, _write_episodic
        confidence_label = proposal.get("confidence_label", "MEDIUM")
        await _write_research_semantic(
            qdrant, topic=proposal.get("trigger_summary", subject_id), domain_scope="general",
            note_id=None, note_title="", full_report=proposal.get("full_report", ""),
            confidence=confidence_label, report_date=today,
            subject=subject_id,
        )
        await _write_episodic(
            qdrant, topic=proposal.get("trigger_summary", subject_id), domain_scope="general",
            confidence=confidence_label, sources_ok=[], note_id=campaign_note_id,
            subject=subject_id,
        )
    except Exception as exc:
        logger.warning("apply_subject_update: research semantic/episodic write failed for %r: %s", subject_id, exc)

    # 4. Archive campaign note
    if campaign_note_id:
        await _archive_campaign_note(nanobot, campaign_note_id, status="updated")

    await _clear_pending_update(qdrant, subject_id)
    return {
        "status": "ok", "action": "approved", "subject_id": subject_id,
        "old_confidence": proposal.get("old_confidence"), "new_confidence": new_confidence,
    }


async def _archive_campaign_note(nanobot, campaign_note_id: str, status: str) -> None:
    """Flip a campaign note's status field to archived (in place — no folder move)."""
    try:
        nb = await nanobot.run("openclaw-nextcloud", "notes_read", {"note-id": campaign_note_id})
        result = nb.get("result") if nb.get("result") is not None else nb
        content = result.get("content", "") if isinstance(result, dict) else ""
        if "status: " in content:
            import re
            content = re.sub(r"status: \w+", f"status: archived ({status})", content, count=1)
        await nanobot.run("openclaw-nextcloud", "notes_update", {
            "note-id": campaign_note_id, "content": content,
        })
    except Exception as exc:
        logger.warning("_archive_campaign_note: failed for %r: %s", campaign_note_id, exc)
