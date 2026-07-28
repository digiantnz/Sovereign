"""Cognition Engine — Thought creation and lifecycle.

A Thought is a bounded, temporary research effort triggered by a
subject-relevant event (RSS story, web search result, email, or a
conversational turn PASS 1 matched to a known Subject). Named "thought"
(renamed from "campaign" 2026-07-03 — the marketing-flavored original name
never fit a cognitive-loop concept describing Rex noticing something and
following up on it) rather than a marketing-style "campaign".

Architecture: thoughts are Qdrant-only — no Nextcloud Notes. A thought is
audit trail, not human-readable content the Director browses; it's tracked
in working_memory while running (a lightweight checkpoint, cleared on
completion) and in episodic memory once it stops (see
cognition/subjects.py's _log_thought_stop_episodic). The Director sees
thoughts via the Telegram proposal notification, not Nextcloud. Only two
Nextcloud write paths remain in the Cognition Engine: Subject notes
(Director-readable synthesis) and research outputs (pre-existing pattern,
unchanged). This keeps Nextcloud clean as more trigger sources (RSS, web
search, email) fire thoughts at volume.

run_thought() is the single entry point for spawning AND running a
thought end-to-end — every trigger source calls this same function so
thought logic never drifts between callers.

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
    get_subject, get_confidence_target, get_epistemic_status, evaluate_thought_iteration,
    resolve_thought_outcome, count_thoughts_today,
)

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 2  # Director, 2026-07-07 — was 3; applies to every Thought source

# Director, 2026-07-08 — the "2 thoughts per subject max" half of the fan-out
# cap that was never actually built; only the per-event _MAX_SUBJECT_MATCHES
# fan-out (cognition/subjects.py) existed. This is what stops a Subject from
# accumulating unbounded same-day Thoughts across many separate trigger
# events (RSS, email, web search, /learn reconciliation, chat triage) — the
# root cause of the "property"/"ai" thought-spam spiral, compounding with the
# Learning Harness self-feeding-on-its-own-Subject-notes bug fixed the same
# session (monitoring/learning_harness.py's _SKIP_CATEGORIES).
_MAX_THOUGHTS_PER_SUBJECT_PER_DAY = 2

# Strong references for fire-and-forget asyncio.create_task() calls — see
# ExecutionEngine._fire_and_forget in execution/engine.py for why this exists
# (reproduced live 2026-07-03: an unreferenced task was garbage-collected
# mid-execution). No long-lived object to hang a method off here either.
_background_tasks: set = set()


async def spawn_thought(qdrant, nanobot, cog, subject_id: str, trigger_source: str,
                         trigger_summary: str) -> "asyncio.Task | None":
    """Fire a Thought in the background — never awaited by the caller.

    Single place this fire-and-forget boilerplate lives (Director, 2026-07-07:
    RSS scoring used to `await run_thought(...)` directly, story-by-story,
    subject-by-subject — the one trigger source that blocked on full Thought
    completion instead of running concurrently in the background like email/
    chat/learn already do via observe_for_subject()). Reuse this instead of
    reimplementing the create_task/_background_tasks/add_done_callback trio
    at a new call site.

    Returns None (does not spawn) if subject_id has already reached
    _MAX_THOUGHTS_PER_SUBJECT_PER_DAY thought_stop records today — checked
    here, the one place a Thought actually gets created, so every trigger
    source gets the cap for free rather than each caller re-checking it.
    """
    already_today = await count_thoughts_today(qdrant, subject_id, _MAX_THOUGHTS_PER_SUBJECT_PER_DAY)
    if already_today >= _MAX_THOUGHTS_PER_SUBJECT_PER_DAY:
        logger.info(
            "spawn_thought: subject=%r already has %d Thought(s) today (cap %d) — "
            "not spawning (trigger_source=%r)",
            subject_id, already_today, _MAX_THOUGHTS_PER_SUBJECT_PER_DAY, trigger_source,
        )
        return None
    task = asyncio.create_task(run_thought(
        qdrant, nanobot, cog, subject_id, trigger_source, trigger_summary,
    ))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _write_thought_checkpoint(
    qdrant, thought_id: str, subject_id: str,
    trigger_source: str, trigger_summary: str,
    status: str, iteration: int = 0,
) -> None:
    """working_memory checkpoint — runtime visibility into an in-flight
    thought. Ephemeral by design (tmpfs); not required for resumption
    since run_thought() is fully synchronous within one call."""
    try:
        await qdrant.store(
            collection="working_memory",
            content=f"Thought {thought_id} ({subject_id}) — {status}",
            metadata={
                "_cognition_thought": True,
                "_key":            f"cognition:thought:{thought_id}",
                "thought_id":       thought_id,
                "subject_id":       subject_id,
                "trigger_source":   trigger_source,
                "trigger_summary":  trigger_summary,
                "status":           status,
                "iteration":        iteration,
                "ts":               datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("_write_thought_checkpoint: failed for %r: %s", thought_id, exc)


async def _clear_thought_checkpoint(qdrant, thought_id: str) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        await qdrant.client.delete(
            collection_name="working_memory",
            points_selector=Filter(must=[
                FieldCondition(key="_cognition_thought", match=MatchValue(value=True)),
                FieldCondition(key="thought_id", match=MatchValue(value=thought_id)),
            ]),
        )
    except Exception as exc:
        logger.warning("_clear_thought_checkpoint: failed for %r: %s", thought_id, exc)


def _new_thought_id(subject_id: str, trigger_source: str) -> str:
    """Deterministic-ish, human-scannable, collision-free id. A single
    scoring run can spawn several same-day thoughts for one subject (e.g.
    6 RSS stories all judged relevant to crypto in one run) — the date+
    subject+source prefix alone is not unique, hence the short suffix."""
    today = date.today().isoformat()
    suffix = uuid.uuid4().hex[:8]
    return f"{today}-{subject_id}-{trigger_source}-{suffix}"


# Crash-recovery for Thoughts (task #41, 2026-07-03). run_thought() is fully
# synchronous within one call with no cross-run resumption — its only
# runtime state is a working_memory checkpoint, and working_memory is wiped
# on every restart by design (setup() recreates it fresh at boot). That means
# a checkpoint alone can never reveal an interrupted Thought after a restart
# — by the time the new process starts, the checkpoint that would have shown
# it is already gone. The fix is a second, DURABLE marker in `meta`
# (survives restarts, same collection the structural-synthesis cursor uses)
# written at start and cleared at normal completion. Anything still marked
# in-flight at the next boot was, by definition, killed mid-run — this
# doesn't resume it (no cross-run resumption still holds), it just stops the
# loss from being silent.
async def _write_thought_inflight_marker(qdrant, thought_id: str, subject_id: str, trigger_source: str) -> None:
    try:
        await qdrant.store(
            collection="meta",
            content=f"Thought in-flight: {thought_id} ({subject_id})",
            metadata={
                "type": "meta",
                "_key": f"meta:thought_inflight:{thought_id}",
                "thought_id": thought_id,
                "subject_id": subject_id,
                "trigger_source": trigger_source,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("_write_thought_inflight_marker: failed for %r: %s", thought_id, exc)


async def _clear_thought_inflight_marker(qdrant, thought_id: str) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        await qdrant.archive_client.delete(
            collection_name="meta",
            points_selector=Filter(must=[
                FieldCondition(key="thought_id", match=MatchValue(value=thought_id)),
            ]),
        )
    except Exception as exc:
        logger.warning("_clear_thought_inflight_marker: failed for %r: %s", thought_id, exc)


async def sweep_orphaned_thoughts(qdrant) -> dict:
    """Startup-time check (called once from main.py's lifespan, after qdrant
    is ready) — any meta:thought_inflight:* marker still present means that
    Thought was running when the process last stopped and never got to
    clean up after itself. Logs an episodic record, notifies the Director,
    and clears the marker (nothing to resume — see module docstring).
    Returns {"orphans_found": int}."""
    found = 0
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchText
        points, _ = await qdrant.archive_client.scroll(
            collection_name="meta",
            scroll_filter=Filter(must=[
                FieldCondition(key="_key", match=MatchText(text="meta:thought_inflight:")),
            ]),
            limit=50, with_payload=True, with_vectors=False,
        )
        for pt in points:
            pl = pt.payload or {}
            thought_id = pl.get("thought_id", "?")
            subject_id = pl.get("subject_id", "?")
            started_at = pl.get("started_at", "?")
            found += 1
            try:
                await qdrant.store(
                    collection="episodic",
                    content=(
                        f"Thought {thought_id} for subject '{subject_id}' was interrupted by a "
                        f"restart (started {started_at}, never completed) — lost, not resumed."
                    ),
                    metadata={
                        "type": "episodic", "event_type": "thought_orphaned",
                        "subject": subject_id, "thought_id": thought_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as exc:
                logger.warning("sweep_orphaned_thoughts: episodic write failed for %r: %s", thought_id, exc)
            await _clear_thought_inflight_marker(qdrant, thought_id)
        if found:
            from cognition.subjects import _notify_telegram
            lines = [f"⚠️ {found} Thought(s) were interrupted by the last restart, not resumed:"]
            for pt in points:
                pl = pt.payload or {}
                lines.append(f"• {pl.get('subject_id', '?')} ({pl.get('thought_id', '?')})")
            await _notify_telegram("\n".join(lines))
    except Exception as exc:
        logger.warning("sweep_orphaned_thoughts: failed (non-fatal): %s", exc)
    return {"orphans_found": found}


async def run_thought(
    qdrant, nanobot, cog,
    subject_id: str, trigger_source: str, trigger_summary: str,
) -> dict:
    """Run a thought to completion, then resolve it against the Subject
    (`resolve_thought_outcome()` — decides and applies in one call, no
    Director approval gate).

    Goal-seeking, not iteration-exhausting: max_iterations=3 is a ceiling,
    not a fixed count. Each iteration is evaluated against a fixed
    iteration-quality bar (`_ITERATION_QUALITY_BAR` in subjects.py — "did
    this search turn up enough corroborated material"), NOT the Subject's
    own confidence_target — those are different questions (see
    subjects.py's module docstring). A thought can terminate after 1
    iteration if this pass's sources were good enough, or continue up to the
    ceiling if the evaluator judges another pass would resolve specific open
    questions (not merely "budget remains").
    """
    from monitoring.research_harness import run_research_headless

    subject = await get_subject(qdrant, subject_id)
    if not subject:
        logger.warning("run_thought: unknown subject %r", subject_id)
        return {"status": "error", "error": f"unknown subject {subject_id!r}"}

    # Refuted Subjects are paused pending Director review (2026-07-06) — a
    # core assumption already failed once; running more automatic research
    # against the same thesis before the Director has acted on it (reviewed,
    # created a successor, or manually reactivated) isn't useful, it's just
    # more Thoughts spent on a thesis already known to be broken.
    if get_epistemic_status(subject) == "refuted":
        logger.info("run_thought: subject=%r is refuted — skipping, pending Director review", subject_id)
        return {"status": "ok", "subject_id": subject_id, "skipped": "refuted"}

    thought_id = _new_thought_id(subject_id, trigger_source)
    thesis = subject.get("thesis", "")
    confidence_target = get_confidence_target(subject)

    await _write_thought_inflight_marker(qdrant, thought_id, subject_id, trigger_source)

    question = (
        f"Current thesis: {thesis}\n"
        f"Trigger: {trigger_summary}\n"
        f"Does this new information materially change the thesis for {subject_id}? "
        "Focus on thesis impact, not price prediction."
    )

    last_result: dict = {}
    quality_met = False
    resolvable_gaps: list[str] = []
    stop_reason = "budget_exhausted"
    iterations_used = 0

    await _write_thought_checkpoint(
        qdrant, thought_id, subject_id, trigger_source, trigger_summary, status="running",
    )

    for iteration in range(1, _MAX_ITERATIONS + 1):
        iterations_used = iteration
        await _write_thought_checkpoint(
            qdrant, thought_id, subject_id, trigger_source, trigger_summary,
            status="running", iteration=iteration,
        )
        last_result = await run_research_headless(cog, nanobot, qdrant, question)

        evaluation = await evaluate_thought_iteration(
            cog, thesis, last_result, iteration, _MAX_ITERATIONS,
        )
        quality_met = evaluation["quality_met"]
        resolvable_gaps = evaluation["resolvable_gaps"]
        stop_reason = evaluation["stop_reason"]

        if not evaluation["iterate"]:
            break  # quality_met, no_resolvable_gaps, or budget_exhausted — stop
        question = evaluation["next_question"]

    logger.info(
        "run_thought: subject=%r trigger=%r iterations=%d stop_reason=%s",
        subject_id, trigger_source, iterations_used, stop_reason,
    )

    await resolve_thought_outcome(
        qdrant, nanobot, cog, subject_id, thought_id, trigger_source, trigger_summary, last_result,
        confidence_target=confidence_target, quality_met=quality_met,
        resolvable_gaps=resolvable_gaps, stop_reason=stop_reason,
        iterations_used=iterations_used,
    )
    await _clear_thought_checkpoint(qdrant, thought_id)
    await _clear_thought_inflight_marker(qdrant, thought_id)

    return {
        "status": "ok", "subject_id": subject_id, "thought_id": thought_id,
        "iterations": iterations_used, "quality_met": quality_met, "stop_reason": stop_reason,
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
    evaluate_thought_iteration() itself would be reaching into an internal
    implementation detail that isn't its concern.

    Cheap by design for the common case: one evaluate_thought_iteration()
    call, LLM cost only. A full thought only fires (fire-and-forget) when a
    genuine gap is found — this function's caller never spawns thoughts
    speculatively just by calling it.

    observation_summary: what the caller found — prose, not a research
    result. This function does the confidence/research_result shimming
    internally so callers don't need to know that shape either.

    Returns {"status": "ok", "gap_found": bool, "thought_id": str | None}.
    Never raises — a broken observation source shouldn't be able to take
    down whatever produced it.
    """
    try:
        subject = await get_subject(qdrant, subject_id)
        if not subject:
            logger.warning("observe_for_subject: unknown subject %r", subject_id)
            return {"status": "error", "error": f"unknown subject {subject_id!r}"}

        pseudo_research_result = {
            # No natural source-quality equivalent for an external
            # observation (health_score, urgency, etc. all measure something
            # else) — MEDIUM is an inert placeholder; force_evaluate=True
            # below means it's never actually compared against the iteration
            # quality bar. The LLM gap-check judges substance directly.
            "confidence": "MEDIUM",
            "telegram_summary": [observation_summary],
        }
        gap_eval = await evaluate_thought_iteration(
            cog, subject.get("thesis", ""),
            pseudo_research_result, iterations_used=0, max_iterations=1,
            force_evaluate=True,
        )
        if not gap_eval["resolvable_gaps"]:
            # Visibility fix (Director, 2026-07-05): this branch previously returned
            # silently — a Subject that matched the triage threshold but was then
            # judged "no genuine gap" left zero trace, indistinguishable in the logs
            # from a Subject that never matched at all (see find_relevant_subjects()'s
            # matching companion log). Now both halves of "why didn't X update" are
            # answerable from container logs instead of guessed at.
            logger.info(
                "observe_for_subject: matched subject=%r (source=%r) but no resolvable "
                "gap found — not spawning a thought", subject_id, trigger_source,
            )
            return {"status": "ok", "gap_found": False, "thought_id": None}

        # Lead with what was actually found, then the specific unknowns — a
        # trigger of only extracted questions gives the research agent no
        # grounding for what prompted them.
        trigger_summary = (
            f"{trigger_source.title()} finding: {observation_summary} "
            f"Open questions: {'; '.join(gap_eval['resolvable_gaps'])}"
        )
        logger.info("observe_for_subject: gap found for subject=%r (source=%r) — spawning thought",
                    subject_id, trigger_source)
        await spawn_thought(qdrant, nanobot, cog, subject_id, trigger_source, trigger_summary)
        return {"status": "ok", "gap_found": True, "thought_id": None}  # thought_id
        # not known yet — run_thought generates it internally and this call
        # doesn't await the thought to completion to learn it
    except Exception as exc:
        logger.warning("observe_for_subject: failed for subject=%r (non-fatal): %s", subject_id, exc)
        return {"status": "error", "error": str(exc)}
