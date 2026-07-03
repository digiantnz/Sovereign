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
import re
from datetime import date, datetime, timezone

import httpx

from config import cfg as _cfg

logger = logging.getLogger(__name__)

_CONFIDENCE_MAP: dict[str, float] = {"HIGH": 0.75, "MEDIUM": 0.50, "LOW": 0.25}

_HISTORY_CAP = 5
_HALF_LIFE_DAYS = 90

# Per-subject in Subject frontmatter/payload ("confidence_target"); this is the
# fallback when a subject hasn't set one. Not a global constant applied uniformly —
# e.g. retirement might reasonably be 0.85, a casual-interest subject 0.60.
_DEFAULT_CONFIDENCE_TARGET = 0.75

# Reminder baked into every Subject note (new or rewritten) — manual edits sit
# inert until reconciled; without this, a Director edit is easy to assume "just
# works" the way it would in a normal notes app (see the macro/ai note-drift
# incidents this session, both fixed by running "learn subject <id>").
_MANUAL_EDIT_NOTE = (
    "> Manual edits to this note are not automatically visible to Rex. "
    "Run `learn subject {subject_id}` after editing to reconcile changes into memory."
)


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
        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=60.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
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
    qdrant, subject_id: str, campaign_id: str | None,
    stop_reason: str, resolvable_gaps: list[str],
    confidence: float, target: float, iterations_used: int,
) -> None:
    """Episodic record of WHY a campaign stopped — distinct from the research-
    complete episodic entry (one campaign may run several research iterations,
    but there is exactly one stop-reason event). Fires regardless of whether
    the Director later approves or rejects the resulting Subject Update —
    this is an audit fact about the campaign, not the subject state change.
    This IS the campaign's permanent record — campaigns are Qdrant-only,
    no Nextcloud note."""
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
                "subject": subject_id, "campaign_id": campaign_id,
                "stop_reason": stop_reason, "resolvable_gaps": resolvable_gaps,
                "confidence": confidence, "confidence_target": target,
                "iterations_used": iterations_used, "ts": today,
            },
        )
    except Exception as exc:
        logger.warning("_log_campaign_stop_episodic: failed for %r: %s", subject_id, exc)


async def propose_subject_update(
    qdrant, subject_id: str, campaign_id: str | None,
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
        qdrant, subject_id, campaign_id, stop_reason, resolvable_gaps,
        confidence_to_score(new_label), target, iterations_used,
    )

    proposal = {
        "subject_id":             subject_id,
        "campaign_id":            campaign_id,
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

    # Keyed by (subject_id, campaign_id) — not subject_id alone. Multiple campaigns
    # for the same subject can complete in close succession once RSS/web search/
    # /learn all fire campaigns; a subject_id-only key means the second proposal's
    # upsert silently replaces the first before the Director ever sees it. Each
    # campaign now gets its own point, so nothing is lost — see _list_pending_updates.
    campaign_key_part = campaign_id or "single"
    await qdrant.store(
        collection="working_memory",
        content=f"Cognition Engine pending Subject Update — {subject_id} ({campaign_key_part})",
        metadata={
            _PENDING_FLAG:  True,
            "_key":         f"cognition:pending_update:{subject_id}:{campaign_key_part}",
            "subject_id":   subject_id,
            "campaign_id":  campaign_id,
            "proposal":     proposal,
            "ts":           datetime.now(timezone.utc).isoformat(),
        },
    )

    others_pending = len(await _list_pending_updates(qdrant, subject_id)) - 1

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
    if others_pending > 0:
        lines += [f"({others_pending} more campaign(s) for {subject_id} also awaiting review — "
                   f"reply again after this one to see the next.)"]
    await _notify_telegram("\n".join(lines))


async def _list_pending_updates(qdrant, subject_id: str) -> list[dict]:
    """All pending proposals for a subject, newest first. Several can coexist —
    see the keying note in propose_subject_update()."""
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        points, _ = await qdrant.client.scroll(
            collection_name="working_memory",
            scroll_filter=Filter(must=[
                FieldCondition(key=_PENDING_FLAG, match=MatchValue(value=True)),
                FieldCondition(key="subject_id", match=MatchValue(value=subject_id)),
            ]),
            limit=20, with_payload=True, with_vectors=False,
        )
        entries = [p.payload for p in points]
        entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return entries
    except Exception as exc:
        logger.warning("_list_pending_updates: failed for %r: %s", subject_id, exc)
        return []


async def read_pending_update(qdrant, subject_id: str) -> dict | None:
    """Most recent pending proposal for a subject, if any (see _list_pending_updates
    for the multi-proposal case — approve/reject always acts on the newest)."""
    pending = await _list_pending_updates(qdrant, subject_id)
    return pending[0] if pending else None


async def _clear_pending_update(qdrant, subject_id: str, campaign_id: str | None = None) -> None:
    """Clears one proposal by campaign_id if given, else every proposal for the
    subject (legacy fallback for callers that predate per-campaign keying)."""
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        must = [
            FieldCondition(key=_PENDING_FLAG, match=MatchValue(value=True)),
            FieldCondition(key="subject_id", match=MatchValue(value=subject_id)),
        ]
        if campaign_id:
            must.append(FieldCondition(key="campaign_id", match=MatchValue(value=campaign_id)))
        await qdrant.client.delete(
            collection_name="working_memory",
            points_selector=Filter(must=must),
        )
    except Exception as exc:
        logger.warning("_clear_pending_update: failed for %r: %s", subject_id, exc)


async def apply_subject_update(qdrant, nanobot, subject_id: str, approved: bool) -> dict:
    """Apply or discard a pending Subject Update proposal.

    Called from the Director's approve/reject reply. On approve: writes the
    Nextcloud Subject Note, the semantic:subject:<id> Qdrant upsert, and the
    research semantic + episodic entries (subject-tagged — see
    research_harness.py _write_research_semantic). On reject: nothing
    changes. Campaigns are Qdrant-only (no Nextcloud note) — the episodic
    campaign_stop entry written in propose_subject_update() is already the
    campaign's permanent record either way, so there is no note to archive.
    """
    pending = await read_pending_update(qdrant, subject_id)
    if not pending:
        return {"status": "error", "error": f"No pending update for subject {subject_id!r}."}

    proposal = pending.get("proposal", {})
    campaign_id = proposal.get("campaign_id")
    today = date.today().isoformat()

    if not approved:
        await _clear_pending_update(qdrant, subject_id, campaign_id=campaign_id)
        remaining = len(await _list_pending_updates(qdrant, subject_id))
        return {
            "status": "ok", "action": "rejected", "subject_id": subject_id,
            "pending_remaining": remaining,
        }

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
            f"{_MANUAL_EDIT_NOTE.format(subject_id=subject_id)}\n\n"
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
            confidence=confidence_label, sources_ok=[], note_id=campaign_id,
            subject=subject_id,
        )
    except Exception as exc:
        logger.warning("apply_subject_update: research semantic/episodic write failed for %r: %s", subject_id, exc)

    await _clear_pending_update(qdrant, subject_id, campaign_id=campaign_id)
    remaining = len(await _list_pending_updates(qdrant, subject_id))
    return {
        "status": "ok", "action": "approved", "subject_id": subject_id,
        "old_confidence": proposal.get("old_confidence"), "new_confidence": new_confidence,
        "pending_remaining": remaining,
    }


async def resync_subject_from_note(qdrant, nanobot, cog, subject_id: str) -> dict:
    """Reconcile a Subject's Nextcloud note back into the canonical Qdrant record.

    Manual edits to a Subject note (the Director pasting in outside analysis —
    e.g. an old Grok conversation) never reach Qdrant on their own: get_subject()
    only ever reads Qdrant, and apply_subject_update() overwrites the note FROM
    Qdrant on every campaign approval. Without this, hand-edited content is
    functionally inert and gets silently discarded on the next approval. This is
    the supported fix: read the note, distill it against the subject's current
    thesis/open_questions/knowns (an LLM merge, not a raw overwrite — pasted
    content is typically dense and multi-topic), upsert the reconciled result
    into Qdrant, then rewrite the note back to the clean distilled version so it
    stops drifting from what Rex actually knows.

    Lightweight — does not touch confidence/confidence_history (reserved for
    actual campaign-driven research) and does not spawn a campaign. No HITL gate:
    the Director explicitly triggered this by naming the subject.
    """
    subject = await get_subject(qdrant, subject_id)
    if not subject:
        return {"status": "error", "error": f"unknown subject {subject_id!r}"}

    note_id = subject.get("note_id")
    if not note_id:
        return {"status": "error", "error": f"subject {subject_id!r} has no linked Nextcloud note"}

    try:
        nb = await nanobot.run("openclaw-nextcloud", "notes_read", {"note-id": note_id})
        nb_result = nb.get("result") if nb.get("result") is not None else nb
        note_content = nb_result.get("content", "") if isinstance(nb_result, dict) else ""
    except Exception as exc:
        logger.warning("resync_subject_from_note: notes_read failed for %r: %s", subject_id, exc)
        return {"status": "error", "error": f"could not read note: {exc}"}

    if not note_content.strip():
        return {"status": "error", "error": "note is empty"}

    current_thesis = subject.get("thesis", "")
    current_open_questions = subject.get("open_questions", [])
    current_knowns = subject.get("knowns", [])

    prompt = f"""You are reconciling a Subject's Nextcloud note back into its canonical record.

Current thesis: {current_thesis}
Current open questions: {current_open_questions}
Current knowns: {current_knowns}

The Director may have pasted outside analysis (e.g. from another AI conversation) directly into
the note below. Distill it: merge anything genuinely new or more current into the thesis/open_
questions/knowns, drop redundant or now-superseded content, keep the result concise (thesis 1-3
sentences, not a dump of every scenario). Do not fabricate — only use what's actually present in
the note content below.

Note content:
{note_content[:6000]}

Respond with JSON only — no preamble:
{{"thesis": "...", "open_questions": ["...", "..."], "knowns": ["...", "..."]}}"""

    try:
        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=90.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception as exc:
        logger.warning("resync_subject_from_note: LLM reconciliation failed for %r: %s", subject_id, exc)
        return {"status": "error", "error": f"reconciliation failed: {exc}"}

    new_thesis = data.get("thesis") or current_thesis
    new_open_questions = data.get("open_questions") or current_open_questions
    new_knowns = data.get("knowns") or current_knowns

    today = date.today().isoformat()
    confidence = subject.get("confidence", 0.5)
    confidence_history = subject.get("confidence_history") or []
    confidence_target = get_confidence_target(subject)
    last_campaign = subject.get("last_campaign")

    # Qdrant upsert — must re-include every field, store() replaces the whole payload
    try:
        await qdrant.store(
            collection="semantic",
            content=f"Subject: {subject_id}\nThesis: {new_thesis}",
            metadata={
                "type": "semantic", "domain": "subject",
                "_key": f"semantic:subject:{subject_id}",
                "subject": subject_id, "status": "active",
                "confidence": confidence,
                "confidence_history": confidence_history,
                "confidence_target": confidence_target,
                "thesis": new_thesis,
                "open_questions": new_open_questions,
                "knowns": new_knowns,
                "note_id": note_id,
                "last_campaign": last_campaign,
            },
        )
    except Exception as exc:
        logger.warning("resync_subject_from_note: semantic upsert failed for %r: %s", subject_id, exc)
        return {"status": "error", "error": f"qdrant upsert failed: {exc}"}

    # Rewrite the note back to the clean distilled version
    oq_lines = "\n".join(f"- {q}" for q in new_open_questions) or "(none)"
    kn_lines = "\n".join(f"- {k}" for k in new_knowns) or "(none)"
    note_body = (
        "---\n"
        "type: subject\n"
        f"subject: {subject_id}\n"
        "status: active\n"
        f"confidence: {confidence:.2f}\n"
        f"last_updated: {today}\n"
        f"last_campaign: {last_campaign or 'null'}\n"
        f"confidence_history: {json.dumps(confidence_history)}\n"
        f"confidence_target: {confidence_target}\n"
        "---\n\n"
        f"{_MANUAL_EDIT_NOTE.format(subject_id=subject_id)}\n\n"
        f"## Thesis\n{new_thesis}\n\n"
        f"## Open Questions\n{oq_lines}\n\n"
        f"## Knowns\n{kn_lines}\n\n"
        "[Narrative updates post-campaign go here.]\n"
    )
    try:
        await nanobot.run("openclaw-nextcloud", "notes_update", {
            "note-id": note_id, "content": note_body,
        })
    except Exception as exc:
        logger.warning("resync_subject_from_note: notes_update failed for %r: %s", subject_id, exc)

    try:
        await qdrant.store(
            collection="episodic",
            content=f"Subject '{subject_id}' resynced from its Nextcloud note (Director-triggered).",
            metadata={
                "type": "episodic", "event_type": "subject_resync",
                "subject": subject_id, "ts": today,
            },
        )
    except Exception as exc:
        logger.warning("resync_subject_from_note: episodic write failed for %r: %s", subject_id, exc)

    return {
        "status": "ok", "action": "resynced", "subject_id": subject_id,
        "thesis": new_thesis, "open_questions": new_open_questions, "knowns": new_knowns,
    }


# Default triage threshold — empirically calibrated 2026-07-03 against 6 real
# emails (nomic-embed-text, cosine similarity vs Subject thesis embeddings).
# 0.55 (the original guess) barely filtered anything: a grocery-loyalty
# balance notification and an unrelated NZ political newsletter both scored
# 2-3 "hits" above it (top scores 0.59-0.61) — pure embedding-space noise,
# not real relevance. Genuinely on-topic content scored 0.65-0.70 at the top.
# 0.62 sits in the gap between the false-positive ceiling and the true-
# positive floor observed in that test set. Still more permissive than PASS
# 1's conversational-routing use (0.72, a single best-match decision) — a
# false positive here only costs one extra LLM call; a false negative means
# a genuinely relevant Subject never gets considered at all. Revisit if a
# wider test set shows the gap sitting elsewhere.
_TRIAGE_THRESHOLD = 0.62


async def find_relevant_subjects(
    qdrant, text: str, threshold: float = _TRIAGE_THRESHOLD, limit: int = 20,
) -> list[dict]:
    """Canonical Subject-relevance triage — one embed call + one Qdrant vector
    search, no LLM. The single place this operation happens; every caller that
    needs "which Subjects does this content relate to" goes through here
    (PASS 1 conversational routing, /learn fold-in, web search trigger, RSS
    scorer if it adopts triage later) rather than each reimplementing its own
    embed+filter+threshold — see CLAUDE.md standing order #2/#3.

    Most content is relevant to zero or one Subject, not all of them — scoring
    every Subject with a full LLM call regardless assumes the opposite (every
    piece of content is a trove of information for every Subject). This
    triages first: only Subjects whose thesis embedding is actually close to
    the content proceed to any further (expensive) processing. Returns full
    subject payloads (not just IDs), ordered by score descending (Qdrant's
    natural order), so callers don't need a second lookup.
    """
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        vector = await qdrant._embed(text[:2000])
        resp = await qdrant.archive_client.query_points(
            collection_name="semantic",
            query=vector,
            query_filter=Filter(must=[
                FieldCondition(key="domain", match=MatchValue(value="subject")),
                FieldCondition(key="status", match=MatchValue(value="active")),
            ]),
            limit=limit,
            score_threshold=threshold,
            with_payload=True,
        )
        hits = []
        for p in resp.points:
            payload = dict(p.payload or {})
            payload["_triage_score"] = round(p.score, 4)
            hits.append(payload)
        return hits
    except Exception as exc:
        logger.warning("find_relevant_subjects: failed (non-fatal): %s", exc)
        return []


def derive_priority(hits: list) -> tuple[str, float]:
    """Very basic priority/usefulness tag for anything the Cognition Engine
    triages against Subjects — a free byproduct of find_relevant_subjects(),
    not a separate classification pass. Subjects represent what the Director
    is actively tracking, so content that lands on several at once is more
    broadly significant than content that grazes one, which in turn matters
    more than content that matches none.

    Deliberately a DIFFERENT metric from a Subject's `confidence` — confidence
    is epistemic (how sure Rex is a thesis/fact is true, built up over
    campaigns); priority is relevance-breadth (how much THIS content matters
    to what's being tracked, a one-shot triage-time signal). They happen to
    share the same three-tier vocabulary and 0.25/0.5/0.75 numeric scale (via
    confidence_to_score(), reused rather than duplicated) purely for
    consistency — never write a priority score into a Subject's own
    `confidence` field, they answer different questions.

    0 hits -> ("low", 0.25), 1 hit -> ("medium", 0.5), 2+ hits -> ("high", 0.75).
    Deliberately crude — this is a relevance-breadth signal, not an urgency
    detector (a single burning-platform alert is still "medium" by this
    measure; the two are different questions, see CLAUDE.md's related note
    on operational alerting vs. Cognition Engine relevance).
    """
    n = len(hits)
    label = "low" if n == 0 else "medium" if n == 1 else "high"
    return label, confidence_to_score(label.upper())


# Cheap, deterministic — no LLM, no embedding. Urgency is a fundamentally
# different signal from priority (relevance-breadth via Subject triage):
# Subjects encode topics, not time-sensitivity, so embedding-similarity
# can't produce this for free the way it does for priority. An LLM call
# per item would reintroduce the exact per-item cost problem fixed earlier
# tonight (score_and_fold_subjects, web search trigger) — so this stays
# pattern-matching only. Narrower than an LLM classifier (will miss novel
# phrasings) but free, fast, and won't silently balloon briefing cost as
# inbox volume grows.
#
# Both the keyword list and the priority-sender list are Director-editable
# via the dashboard (/config, /config/fields) — see the "cognition" section
# in /home/sovereign/governance/sovereign-config.yaml. Edits take effect on
# next sovereign-core restart (config/loader.py loads once at startup, same
# as every other config.yaml-backed value in this system — not a new
# limitation introduced here).
_URGENCY_SENDER_PATTERN_RE = re.compile(
    r'\b(alert|monitor|noreply|no-reply|notification|security|admin)@',
    re.IGNORECASE,
)


def _build_urgency_keywords_re() -> re.Pattern:
    keywords = _cfg.cognition.urgency_keywords
    return re.compile(r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b', re.IGNORECASE)


_URGENCY_KEYWORDS_RE = _build_urgency_keywords_re()


def get_priority_senders() -> list[str]:
    """Director-maintainable high-priority sender list — config.yaml-backed
    (cognition.priority_senders), edited via the dashboard. Was a live Qdrant
    entry (semantic:cognition:priority_senders) briefly on 2026-07-03, before
    migrating here for dashboard visibility on the same evening — that Qdrant
    key is no longer read. No longer async: config is loaded once at startup,
    reading it is just attribute access."""
    return _cfg.cognition.priority_senders or []


def detect_brand_mismatch(subject_line: str, sender: str, body: str = "") -> bool:
    """Phishing signal — does the sender's display name claim a known brand
    while the actual sending domain doesn't match that brand's legitimate
    domain(s)? e.g. "PayPal Support <noreply@totally-not-paypal.xyz>". The
    single strongest deterministic phishing tell available from metadata
    alone — no body fetch needed, matches the cost discipline of the rest
    of the email-scoring pipeline (no LLM, no per-email round-trip).

    `body` is accepted but unused today — reserved for future signals that
    need full message content (suspicious links, generic-greeting detection,
    reply-to mismatch) so this function's callers and signature don't need
    to change when those land; they'll likely be separate sibling functions
    (e.g. detect_suspicious_links(body)) combined by the caller into one
    overall phishing_flagged bool, same pattern as this function is combined
    with future ones — not folded into this one function growing new params.

    known_brands is Director-editable via the dashboard (cognition.
    known_brands in sovereign-config.yaml) — a starter list of commonly
    impersonated brands, not an exhaustive one; add to it as misses turn up.
    """
    sender_lower = (sender or "").lower()
    known_brands = _cfg.cognition.known_brands or []
    for brand in known_brands:
        name = (brand.get("name") or "").lower()
        domains = [d.lower() for d in (brand.get("domains") or [])]
        if not name or not domains:
            continue
        if name in sender_lower and not any(d in sender_lower for d in domains):
            return True
    return False


def derive_urgency(
    subject_line: str, sender: str = "", priority_senders: list[str] | None = None,
    phishing_flagged: bool = False,
) -> tuple[str, float]:
    """Very basic urgency tag — does this need attention now, independent of
    whether it's worth remembering afterward (an ITIL-style Impact/Urgency
    split; see derive_priority() for the Impact side and CLAUDE.md's related
    note). A monitor-down alert is high urgency and near-certainly low
    priority (nothing to learn) — the two axes are meant to diverge, not
    agree, that's the point of tracking them separately.

    phishing_flagged (caller-computed — see detect_brand_mismatch(), and
    future body/URL-based signals) DOWNGRADES rather than adds, and takes
    precedence over everything else, including a priority-sender match: a
    phishing email borrowing urgent language should read as LESS urgent, not
    more, since prompting quick action is exactly the attacker's goal.
    Otherwise: a sender on the Director-maintained priority list (see
    get_priority_senders()) is "high", full stop. Otherwise: keyword hit in
    the subject line AND an alerting-style sender pattern -> "high". Either
    alone -> "medium". Neither -> "low". Same 0.25/0.5/0.75 numeric scale as
    priority for consistency (see derive_priority()'s docstring on why
    that's shared vocabulary, not a shared meaning).

    priority_senders defaults to get_priority_senders() (config-backed) when
    not passed explicitly — callers scoring many emails in one run should
    fetch it once and pass it through rather than re-reading per email.

    Does not do date-comparison (a "deadline" mentioning a specific date
    doesn't get checked against today) — keyword-only for now, flagged as a
    known gap rather than half-built.
    """
    if phishing_flagged:
        return "low", confidence_to_score("LOW")

    if priority_senders is None:
        priority_senders = get_priority_senders()
    if priority_senders:
        sender_lower = (sender or "").lower()
        if any(ps.lower() in sender_lower for ps in priority_senders):
            return "high", confidence_to_score("HIGH")

    keyword_hit = bool(_URGENCY_KEYWORDS_RE.search(subject_line or ""))
    sender_hit = bool(_URGENCY_SENDER_PATTERN_RE.search(sender or ""))
    if keyword_hit and sender_hit:
        label = "high"
    elif keyword_hit or sender_hit:
        label = "medium"
    else:
        label = "low"
    return label, confidence_to_score(label.upper())


async def score_and_fold_subjects(
    qdrant, cog, text: str, source_label: str, subjects: list[dict] | None = None,
) -> list[dict]:
    """Score arbitrary content (a /learn source — inline text, a fetched URL, or
    an email body) against active Subjects and fold any genuinely new fact
    straight into knowns. Lightweight only: no campaign spawn, no confidence
    change, no HITL gate — the Director explicitly fed this content to /learn,
    so a second approval step would be redundant.

    Triaged (see find_relevant_subjects) — only Subjects that survive the
    cheap embedding pre-filter get the expensive full-read LLM call. Not
    every Subject, every time. Pass a pre-computed `subjects` (e.g. from a
    triage pass the caller already ran, to derive_priority() from the same
    hits) to skip the duplicate embed call; omit it to triage internally.

    Returns [{"subject_id", "added": [...]}] for subjects that got an update —
    used to summarise the /learn result back to the Director.
    """
    folded: list[dict] = []
    if subjects is None:
        subjects = await find_relevant_subjects(qdrant, text)
    if not subjects:
        return folded

    for subject in subjects:
        subject_id = subject.get("subject", "")
        thesis = subject.get("thesis", "")
        current_knowns = subject.get("knowns", []) or []

        prompt = f"""Subject: {subject_id}
Current thesis: {thesis}
Current knowns: {current_knowns}

New content (from {source_label}):
{text[:4000]}

Is this content DIRECTLY relevant to this subject's thesis — not tangentially, not via a loose
topical association? Err toward "false" when in doubt: missing a fact costs nothing, but
recording an unrelated fact pollutes this subject's knowledge base.

Respond with JSON only — no preamble. If not directly relevant:
{{"relevant": false}}
If directly relevant (new fact, not a restatement of an existing known):
{{"relevant": true, "new_knowns": ["...", "..."]}}"""

        try:
            from adapters.inference_queue import InferenceQueue
            result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=60.0)
            raw = result.get("response", "")
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        except Exception as exc:
            logger.warning("score_and_fold_subjects: LLM failed for %r: %s", subject_id, exc)
            continue

        new_knowns = [k for k in (data.get("new_knowns") or []) if k not in current_knowns]
        if not data.get("relevant") or not new_knowns:
            continue

        try:
            await qdrant.store(
                collection="semantic",
                content=f"Subject: {subject_id}\nThesis: {thesis}",
                metadata={
                    "type": "semantic", "domain": "subject",
                    "_key": f"semantic:subject:{subject_id}",
                    "subject": subject_id, "status": "active",
                    "confidence": subject.get("confidence", 0.5),
                    "confidence_history": subject.get("confidence_history") or [],
                    "confidence_target": get_confidence_target(subject),
                    "thesis": thesis,
                    "open_questions": subject.get("open_questions", []),
                    "knowns": current_knowns + new_knowns,
                    "note_id": subject.get("note_id"),
                    "last_campaign": subject.get("last_campaign"),
                },
            )
            folded.append({"subject_id": subject_id, "added": new_knowns})
        except Exception as exc:
            logger.warning("score_and_fold_subjects: upsert failed for %r: %s", subject_id, exc)

    return folded


async def create_subject(
    qdrant, nanobot, subject_id: str, thesis: str,
    open_questions: list[str] | None = None, knowns: list[str] | None = None,
    confidence_target: float | None = None,
) -> dict:
    """Bootstrap a new Subject — the Nextcloud note (Director-readable) and the
    canonical semantic:subject:<id> Qdrant entry, created together so note_id is
    known and cross-linked from the start. The only place a Subject should be
    created — single source of truth, per CLAUDE.md standing order #2 (the 5
    original Subjects predate this function and were bootstrapped ad-hoc).

    Scope check before calling this: `thesis` should answer ONE question, not
    several stitched together (e.g. "crypto market direction" is one Subject;
    "crypto yield optimization" is a different one — not a paragraph inside the
    first). See CLAUDE.md "Cognition Engine — Subject scope, principle" — a
    conflated thesis costs more AND scores relevance worse on every future call
    against it, it isn't a cost/quality tradeoff you can pick a side of. When
    unsure whether something is a new Subject or belongs in an existing thesis,
    prefer a new Subject — narrow is the cheaper failure mode.

    Naming: when a Subject splits off a broader one, use `{parent}_{focus}` —
    `crypto` -> `crypto_revenue`/`crypto_tech`, `ai` -> `ai_ops`. Keep the
    parent's name as the prefix; the id alone should tell you the lineage.
    """
    open_questions = open_questions or []
    knowns = knowns or []
    target = confidence_target if confidence_target is not None else _DEFAULT_CONFIDENCE_TARGET
    today = date.today().isoformat()

    oq_lines = "\n".join(f"- {q}" for q in open_questions) or "(none)"
    kn_lines = "\n".join(f"- {k}" for k in knowns) or "(none)"
    note_content = (
        "---\n"
        "type: subject\n"
        f"subject: {subject_id}\n"
        "status: active\n"
        "confidence: 0.50\n"
        f"last_updated: {today}\n"
        "last_campaign: null\n"
        "confidence_history: []\n"
        f"confidence_target: {target}\n"
        "---\n\n"
        f"{_MANUAL_EDIT_NOTE.format(subject_id=subject_id)}\n\n"
        f"## Thesis\n{thesis}\n\n"
        f"## Open Questions\n{oq_lines}\n\n"
        f"## Knowns\n{kn_lines}\n\n"
        "[Narrative updates post-campaign go here.]\n"
    )

    try:
        nb = await nanobot.run("openclaw-nextcloud", "notes_create", {
            "title": subject_id, "content": note_content, "category": "subject",
        })
        result = nb.get("result") if nb.get("result") is not None else nb
        note_id = result.get("id") or result.get("note_id") if isinstance(result, dict) else None
    except Exception as exc:
        logger.warning("create_subject: notes_create failed for %r: %s", subject_id, exc)
        return {"status": "error", "error": f"note creation failed: {exc}"}

    try:
        await qdrant.store(
            collection="semantic",
            content=f"Subject: {subject_id}\nThesis: {thesis}",
            metadata={
                "type": "semantic", "domain": "subject",
                "_key": f"semantic:subject:{subject_id}",
                "subject": subject_id, "status": "active",
                "confidence": 0.5,
                "confidence_history": [],
                "confidence_target": target,
                "thesis": thesis,
                "open_questions": open_questions,
                "knowns": knowns,
                "note_id": str(note_id) if note_id else None,
                "last_campaign": None,
            },
        )
    except Exception as exc:
        logger.warning("create_subject: semantic write failed for %r: %s", subject_id, exc)
        return {"status": "error", "error": f"qdrant write failed: {exc}", "note_id": note_id}

    return {"status": "ok", "subject_id": subject_id, "note_id": note_id}
