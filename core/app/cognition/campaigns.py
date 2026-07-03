"""Cognition Engine — Campaign creation and lifecycle.

A Campaign is a bounded, temporary research effort triggered by a
subject-relevant event (RSS story, web search result, email, or a
conversational turn PASS 1 matched to a known Subject).

Architecture: campaigns are Qdrant-only — no Nextcloud Notes. A campaign is
audit trail, not human-readable content the Director browses; it's tracked
in working_memory while running (a lightweight checkpoint, cleared on
completion) and in episodic memory once it stops (see
cognition/subjects.py's _log_campaign_stop_episodic). The Director sees
campaigns via the Telegram proposal notification, not Nextcloud. Only two
Nextcloud write paths remain in the Cognition Engine: Subject notes
(Director-readable synthesis) and research outputs (pre-existing pattern,
unchanged). This keeps Nextcloud clean as more trigger sources (RSS, web
search, email) fire campaigns at volume.

run_campaign() is the single entry point for spawning AND running a
campaign end-to-end — every trigger source calls this same function so
campaign logic never drifts between callers.

Lifecycle: running -> research (run_research_headless) -> evaluate
(goal-seeking against the subject's confidence_target, not a fixed
iteration count) -> [research again if worth it] -> propose Subject Update
(MID-tier HITL, cognition/subjects.py). Fully synchronous within one call —
no cross-run resumption.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timezone

from cognition.subjects import (
    get_subject, get_confidence_target, evaluate_campaign_iteration,
    propose_subject_update,
)

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 3

# Strong references for fire-and-forget asyncio.create_task() calls — see
# ExecutionEngine._fire_and_forget in execution/engine.py for why this exists
# (reproduced live 2026-07-03: an unreferenced task was garbage-collected
# mid-execution). No long-lived object to hang a method off here either.
_background_tasks: set = set()


async def _write_campaign_checkpoint(
    qdrant, campaign_id: str, subject_id: str,
    trigger_source: str, trigger_summary: str,
    status: str, iteration: int = 0,
) -> None:
    """working_memory checkpoint — runtime visibility into an in-flight
    campaign. Ephemeral by design (tmpfs); not required for resumption
    since run_campaign() is fully synchronous within one call."""
    try:
        await qdrant.store(
            collection="working_memory",
            content=f"Campaign {campaign_id} ({subject_id}) — {status}",
            metadata={
                "_cognition_campaign": True,
                "_key":            f"cognition:campaign:{campaign_id}",
                "campaign_id":      campaign_id,
                "subject_id":       subject_id,
                "trigger_source":   trigger_source,
                "trigger_summary":  trigger_summary,
                "status":           status,
                "iteration":        iteration,
                "ts":               datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("_write_campaign_checkpoint: failed for %r: %s", campaign_id, exc)


async def _clear_campaign_checkpoint(qdrant, campaign_id: str) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        await qdrant.client.delete(
            collection_name="working_memory",
            points_selector=Filter(must=[
                FieldCondition(key="_cognition_campaign", match=MatchValue(value=True)),
                FieldCondition(key="campaign_id", match=MatchValue(value=campaign_id)),
            ]),
        )
    except Exception as exc:
        logger.warning("_clear_campaign_checkpoint: failed for %r: %s", campaign_id, exc)


def _new_campaign_id(subject_id: str, trigger_source: str) -> str:
    """Deterministic-ish, human-scannable, collision-free id. A single
    scoring run can spawn several same-day campaigns for one subject (e.g.
    6 RSS stories all judged relevant to crypto in one run) — the date+
    subject+source prefix alone is not unique, hence the short suffix."""
    today = date.today().isoformat()
    suffix = uuid.uuid4().hex[:8]
    return f"{today}-{subject_id}-{trigger_source}-{suffix}"


async def run_campaign(
    qdrant, nanobot, cog,
    subject_id: str, trigger_source: str, trigger_summary: str,
) -> dict:
    """Run a campaign to completion, then hand off to Subject Update
    (Phase 7 — proposes, does not apply; Director approval required).

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

    campaign_id = _new_campaign_id(subject_id, trigger_source)
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

    await _write_campaign_checkpoint(
        qdrant, campaign_id, subject_id, trigger_source, trigger_summary, status="running",
    )

    for iteration in range(1, _MAX_ITERATIONS + 1):
        iterations_used = iteration
        await _write_campaign_checkpoint(
            qdrant, campaign_id, subject_id, trigger_source, trigger_summary,
            status="running", iteration=iteration,
        )
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
        qdrant, subject_id, campaign_id, trigger_source, trigger_summary, last_result,
        confidence_target=confidence_target, target_met=target_met,
        resolvable_gaps=resolvable_gaps, stop_reason=stop_reason,
        iterations_used=iterations_used,
    )
    await _clear_campaign_checkpoint(qdrant, campaign_id)

    return {
        "status": "ok", "subject_id": subject_id, "campaign_id": campaign_id,
        "iterations": iterations_used, "target_met": target_met, "stop_reason": stop_reason,
    }


async def observe_for_subject(
    qdrant, nanobot, cog, subject_id: str, trigger_source: str, observation_summary: str,
) -> dict:
    """Single reusable entry point: "here's something I found, you decide if
    it's a gap." Callers (portfolio analysis, and any future source — RSS/
    email/web search already have their own dedicated triage paths, but
    anything that produces a one-shot finding rather than a stream of items
    fits here) call this UNCONDITIONALLY, every time, with no pre-check of
    their own. The gap-decision logic belongs here, in the Cognition Engine,
    not duplicated into every caller — a caller that had to import
    evaluate_campaign_iteration() itself would be reaching into an internal
    implementation detail that isn't its concern.

    Cheap by design for the common case: one evaluate_campaign_iteration()
    call (no LLM cost if the Subject's confidence already meets target — see
    its own short-circuit). A full campaign only fires (fire-and-forget) when
    a genuine gap is found — this function's caller never spawns campaigns
    speculatively just by calling it.

    observation_summary: what the caller found — prose, not a research
    result. This function does the confidence/research_result shimming
    internally so callers don't need to know that shape either.

    Returns {"status": "ok", "gap_found": bool, "campaign_id": str | None}.
    Never raises — a broken observation source shouldn't be able to take
    down whatever produced it.
    """
    try:
        subject = await get_subject(qdrant, subject_id)
        if not subject:
            logger.warning("observe_for_subject: unknown subject %r", subject_id)
            return {"status": "error", "error": f"unknown subject {subject_id!r}"}

        pseudo_research_result = {
            # No natural research-confidence equivalent for an external
            # observation (health_score, urgency, etc. all measure something
            # else) — MEDIUM is a neutral default; the LLM gap-check judges
            # substance, not a self-reported number that doesn't apply here.
            "confidence": "MEDIUM",
            "telegram_summary": [observation_summary],
        }
        gap_eval = await evaluate_campaign_iteration(
            cog, subject.get("thesis", ""), get_confidence_target(subject),
            pseudo_research_result, iterations_used=0, max_iterations=1,
        )
        if not gap_eval["resolvable_gaps"]:
            return {"status": "ok", "gap_found": False, "campaign_id": None}

        # Lead with what was actually found, then the specific unknowns — a
        # trigger of only extracted questions gives the research agent no
        # grounding for what prompted them.
        trigger_summary = (
            f"{trigger_source.title()} finding: {observation_summary} "
            f"Open questions: {'; '.join(gap_eval['resolvable_gaps'])}"
        )
        logger.info("observe_for_subject: gap found for subject=%r (source=%r) — spawning campaign",
                    subject_id, trigger_source)
        task = asyncio.create_task(run_campaign(
            qdrant, nanobot, cog, subject_id, trigger_source, trigger_summary,
        ))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"status": "ok", "gap_found": True, "campaign_id": None}  # campaign_id
        # not known yet — run_campaign generates it internally and this call
        # doesn't await the campaign to completion to learn it
    except Exception as exc:
        logger.warning("observe_for_subject: failed for subject=%r (non-fatal): %s", subject_id, exc)
        return {"status": "error", "error": str(exc)}
