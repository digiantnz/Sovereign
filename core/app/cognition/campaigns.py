"""Cognition Engine — Campaign creation and lifecycle.

A Campaign is a bounded, temporary research effort triggered by a
subject-relevant event (RSS story or a conversational turn PASS 1 matched
to a known Subject). Stored as a Nextcloud Note (category="campaign"),
not a file — see the Cognition Engine MVP plan.

run_campaign() is the single entry point for spawning AND running a
campaign end-to-end — both the RSS scorer (monitoring path) and the PASS 4
async check (conversational path) call this same function so campaign
logic never drifts between the two callers. (create_campaign() alone only
writes the note — a caller that used only create_campaign() would leave
the campaign stuck at "planning" forever, since nothing else picks it up.)

Lifecycle: planning -> research (run_research_headless) -> evaluate
(goal-seeking against the subject's confidence_target, not a fixed
iteration count) -> [research again if worth it] -> propose Subject Update
(MID-tier HITL, cognition/subjects.py) -> archived. Fully synchronous
within one call — no cross-run resumption.
"""
from __future__ import annotations

import logging
from datetime import date

from cognition.subjects import (
    get_subject, get_confidence_target, evaluate_campaign_iteration,
    propose_subject_update,
)

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 3


async def create_campaign(
    qdrant, nanobot,
    subject_id: str, trigger_source: str, trigger_summary: str,
) -> str | None:
    """Write a new campaign Note (category="campaign", status="planning").

    Args:
        subject_id:      e.g. "crypto" — must match an existing semantic:subject:<id>
        trigger_source:  "rss" | "conversation"
        trigger_summary: one-line human-readable description of what triggered this

    Returns the new note's ID (str), or None on failure. Never raises.
    """
    today = date.today().isoformat()
    title = f"{today}-{subject_id}-{trigger_source}"

    content = (
        "---\n"
        "type: campaign\n"
        f"subject: {subject_id}\n"
        "status: planning\n"
        f"trigger: {trigger_source}\n"
        f"trigger_summary: \"{trigger_summary}\"\n"
        f"created: {today}\n"
        "budget:\n"
        f"  max_iterations: {_MAX_ITERATIONS}\n"
        "  current_iteration: 0\n"
        "---\n\n"
        f"Campaign for subject **{subject_id}**, triggered by {trigger_source}.\n\n"
        f"Trigger: {trigger_summary}\n"
    )

    try:
        nb = await nanobot.run("openclaw-nextcloud", "notes_create", {
            "title":    title,
            "content":  content,
            "category": "campaign",
        })
        result = nb.get("result") if nb.get("result") is not None else nb
        note_id = None
        if isinstance(result, dict):
            note_id = result.get("id") or result.get("note_id")
        logger.info(
            "create_campaign: subject=%r trigger=%r note_id=%s",
            subject_id, trigger_source, note_id,
        )
        return str(note_id) if note_id else None
    except Exception as exc:
        logger.warning("create_campaign: failed for subject=%r: %s", subject_id, exc)
        return None


async def run_campaign(
    qdrant, nanobot, cog,
    subject_id: str, trigger_source: str, trigger_summary: str,
) -> dict:
    """Create and run a campaign to completion, then hand off to Subject
    Update (Phase 7 — proposes, does not apply; Director approval required).

    Goal-seeking, not iteration-exhausting: max_iterations=3 is a ceiling,
    not a fixed count. Each iteration is evaluated against the subject's own
    confidence_target (default 0.75, per-subject) — a campaign can terminate
    after 1 iteration if confidence already clears the target, or continue
    up to the ceiling if the evaluator judges another pass would resolve
    specific open questions (not merely "budget remains").
    """
    from monitoring.research_harness import run_research_headless

    subject = await get_subject(qdrant, subject_id)
    if not subject:
        logger.warning("run_campaign: unknown subject %r", subject_id)
        return {"status": "error", "error": f"unknown subject {subject_id!r}"}

    note_id = await create_campaign(qdrant, nanobot, subject_id, trigger_source, trigger_summary)
    thesis = subject.get("thesis", "")
    confidence_target = get_confidence_target(subject)

    question = (
        f"Current thesis: {thesis}\n"
        f"Trigger: {trigger_summary}\n"
        f"Does this new information materially change the thesis for {subject_id}? "
        "Focus on thesis impact, not price prediction."
    )

    last_result: dict = {}
    target_met = False
    resolvable_gaps: list[str] = []
    stop_reason = "budget_exhausted"
    iterations_used = 0

    for iteration in range(1, _MAX_ITERATIONS + 1):
        iterations_used = iteration
        last_result = await run_research_headless(cog, nanobot, qdrant, question)

        evaluation = await evaluate_campaign_iteration(
            cog, thesis, confidence_target, last_result, iteration, _MAX_ITERATIONS,
        )
        target_met = evaluation["target_met"]
        resolvable_gaps = evaluation["resolvable_gaps"]
        stop_reason = evaluation["stop_reason"]

        if not evaluation["iterate"]:
            break  # target_met, no_resolvable_gaps, or budget_exhausted — stop
        question = evaluation["next_question"]

    logger.info(
        "run_campaign: subject=%r trigger=%r iterations=%d stop_reason=%s",
        subject_id, trigger_source, iterations_used, stop_reason,
    )

    await propose_subject_update(
        qdrant, subject_id, note_id, trigger_source, trigger_summary, last_result,
        confidence_target=confidence_target, target_met=target_met,
        resolvable_gaps=resolvable_gaps, stop_reason=stop_reason,
        iterations_used=iterations_used,
    )

    return {
        "status": "ok", "subject_id": subject_id, "campaign_note_id": note_id,
        "iterations": iterations_used, "target_met": target_met, "stop_reason": stop_reason,
    }
