"""Cognition Engine — Subject confidence helpers + Subject Update (MID-tier HITL).

Redesigned 2026-07-06 (Director + CC design pass) around a distinction that
was previously conflated under one "confidence" name in three different
places:

1. **`confidence`** (Subject field) — cumulative epistemic support for the
   thesis: does the evidence gathered so far, on balance, support or
   contradict it? Moves via `apply_confidence_delta()` — a bounded step per
   Thought, weighted by that Thought's *stance* (supports/contradicts/
   neutral) and *stance_strength*, not by how many sources a search happened
   to return.
2. **`label_to_weight()`** (was `confidence_to_score()`) — a per-Thought
   research-synthesis label (HIGH/MEDIUM/LOW = "how corroborated were the
   sources I found this pass"). This is a *trust weight* on how much this
   Thought's stance reading should move the needle, and separately the
   iteration-control loop's own "did I find enough to stop digging" bar. It
   is NOT the thesis confidence itself — that conflation (averaging this
   label directly into the Subject's confidence) was the root cause of every
   Thought stalling at exactly 50%/MEDIUM, since `_synthesise_topic()` never
   even computed it from real source quality until the 2026-07-05 fix, and
   even after that fix it was still measuring the wrong thing entirely.
3. **`confidence_target`** — a materiality-driven decision threshold ("how
   much accumulated support before this thesis counts as validated/
   actionable"), compared only against the Subject's accumulated
   `confidence`. Per-iteration "did this search find enough to stop digging"
   uses a separate fixed `_ITERATION_QUALITY_BAR` — using the Subject's
   validation target for that too, previously, meant a high-stakes subject's
   iteration loop chased an unrelated bar every research pass.

Recording new evidence (updating `knowns`/`open_questions`, moving
`confidence`) no longer requires `confidence_target` to be cleared — that
was gating two unrelated questions ("should we update our belief" vs "is our
belief now strong enough to act on") behind one check, which meant strong
supporting evidence could be discarded outright just because a stale/
miscalibrated target hadn't been met that round. The only real gate on
whether a Thought's finding gets applied now is novelty (did this add
anything genuinely new) — see `resolve_thought_outcome()`.

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

_LABEL_WEIGHTS: dict[str, float] = {"HIGH": 0.75, "MEDIUM": 0.50, "LOW": 0.25}

_HISTORY_CAP = 5

# Iteration-control only — "did this research pass turn up enough corroborated
# material to stop digging," a fixed bar unrelated to any Subject's own
# confidence_target (see module docstring point 3).
_ITERATION_QUALITY_BAR = 0.50

# Stance-weighted confidence movement (module docstring point 1).
_MAX_CONFIDENCE_STEP = 0.15   # a maximally strong/corroborated/novel Thought's ceiling swing
_CONFIDENCE_FLOOR     = 0.05  # never fully certain it's false — always room to update
_CONFIDENCE_CEILING   = 0.95  # never fully certain it's true — same reasoning
_STANCE_DIRECTION: dict[str, int] = {"supports": 1, "contradicts": -1, "neutral": 0}

# Time-based staleness decay (unchanged mechanism, unrelated to the above) —
# each 30-day-stale tick pulls confidence this fraction of the way back to
# neutral (0.5). Calendar-driven, not a per-Thought outcome.
_DECAY_PULL_FRACTION = 0.15

# Epistemic status thresholds (2026-07-06, Director + CC design pass — Popperian
# framing: confirmation and disconfirmation are NOT symmetric. No amount of
# supporting evidence proves a thesis, but a single core assumption failing
# refutes it outright). See resolve_thought_outcome() for the transition logic.
#
# "Core" vs "peripheral" isn't a new tag on open_questions — it's already the
# existing distinction between questions_closed (a resolution of one of the
# Subject's own enumerated load-bearing assumptions — "core" by construction,
# per create_subject()'s own docstring: "an open question and an assumption
# are the same underlying thing") and the general thesis_stance/stance_strength
# (a finding's overall bearing on the thesis, not tied to a specific assumption
# — "peripheral," moves confidence smoothly via apply_confidence_delta()).
#
# Minimum independent applied-Thought count before a thesis can be called
# "corroborated" — a single supporting Thought clearing confidence_target
# isn't the same claim as ten independent ones agreeing.
_MIN_EVIDENCE_WEIGHT = 3

# Per-subject in Subject frontmatter/payload ("confidence_target"); this is the
# fallback when a subject hasn't set one. Not a global constant applied uniformly —
# e.g. retirement might reasonably be 0.85, a casual-interest subject 0.60.
_DEFAULT_CONFIDENCE_TARGET = 0.75


def _subject_stub_fields(subject: dict) -> dict:
    """Schema v1.3 stub fields (Director + analyst design pass, 2026-07-04) —
    pass-through only, never computed here. Every write site that upserts a
    Subject's semantic:subject:<id> record must re-include these or they're
    silently dropped (qdrant.store() replaces the whole payload). A caller that
    actually computes a new value for one of these (e.g. resolve_thought_outcome's
    risk_flags/evidence_ratio) overrides the relevant key after spreading this in.
    Qdrant-only except evidence_ratio/thesis_components, which render into the Note
    frontmatter too (2026-07-04) — risk_flags also renders, but to a body section
    (## Thesis Risks), not frontmatter. epistemic_confidence/composite_trust/
    thesis_edit_history/last_reviewed/last_consolidated remain Qdrant-only.

    `succeeds` (2026-07-06): lineage — the subject_id this one was created to
    replace, if any (frozen at creation like original_thesis, never recomputed).
    Absent (None) for the 8+ Subjects that predate the corroborated/refuted
    successor-thesis flow."""
    return {
        "thesis_components": subject.get("thesis_components"),
        "original_thesis_components": subject.get("original_thesis_components"),
        "epistemic_confidence": subject.get("epistemic_confidence"),
        "evidence_ratio": subject.get("evidence_ratio"),
        "composite_trust": subject.get("composite_trust"),
        "risk_flags": subject.get("risk_flags", []),
        "thesis_edit_history": subject.get("thesis_edit_history", []),
        "last_reviewed": subject.get("last_reviewed"),
        "last_consolidated": subject.get("last_consolidated"),
        "succeeds": subject.get("succeeds"),
    }

# Reminder baked into every Subject note (new or rewritten) — manual edits sit
# inert until reconciled; without this, a Director edit is easy to assume "just
# works" the way it would in a normal notes app (see the macro/ai note-drift
# incidents this session, both fixed by running "learn subject <id>").
_MANUAL_EDIT_NOTE = (
    "> Manual edits to this note are not automatically visible to Rex. "
    "Run `learn subject {subject_id}` after editing to reconcile changes into memory."
)


def label_to_weight(label: str) -> float:
    """Map a research harness categorical label (HIGH/MEDIUM/LOW — "how
    corroborated were this Thought's sources") to a 0-1 trust weight.

    Unrecognised labels default to MEDIUM (0.50) — mirrors the research
    harness's own fallback behaviour on parse failure. This is NOT a thesis
    confidence score (see module docstring) — it's how much a given
    Thought's stance reading should be trusted to move the Subject's
    confidence, and separately the bar `evaluate_thought_iteration()` checks
    to decide whether a research pass turned up enough to stop digging.
    """
    return _LABEL_WEIGHTS.get((label or "").upper(), 0.50)


def apply_confidence_delta(
    old_confidence: float, stance: str, stance_strength: float, quality_weight: float,
    today: date,
) -> tuple[float, dict]:
    """Move a Subject's confidence by a bounded step in the direction the new
    evidence actually points, not by averaging in a per-Thought source-count
    label (see module docstring — that was the root conflation).

    Args:
        old_confidence: Subject's current confidence (0-1).
        stance: "supports" | "contradicts" | "neutral" — this Thought's
            finding relative to the thesis, as judged by assess_thought_quality().
        stance_strength: 0-1 — how strongly the finding bears in that direction.
        quality_weight: label_to_weight() of this Thought's own source-quality
            label — a weak/sparse-source Thought moves the needle less even if
            its stance reading is confident.
        today: date to stamp the history entry with.

    Returns (new_confidence, history_entry) — history_entry is an audit-trail
    dict (stance/strength/weight/delta/resulting value), appended to the
    Subject's confidence_history for the Nextcloud note and capped at
    _HISTORY_CAP. Neutral stance (or stance_strength 0) produces delta=0 —
    "we learned something, but it doesn't bear on whether the thesis is
    true" is a legitimate, common outcome, not an error.
    """
    direction = _STANCE_DIRECTION.get(stance, 0)
    delta = direction * max(0.0, min(1.0, stance_strength)) * quality_weight * _MAX_CONFIDENCE_STEP
    new_confidence = round(max(_CONFIDENCE_FLOOR, min(_CONFIDENCE_CEILING, old_confidence + delta)), 3)
    entry = {
        "date": today.isoformat(), "stance": stance,
        "stance_strength": round(stance_strength, 2), "quality_weight": quality_weight,
        "delta": round(new_confidence - old_confidence, 3), "confidence": new_confidence,
    }
    return new_confidence, entry


def decay_confidence_towards_neutral(old_confidence: float, today: date) -> tuple[float, dict]:
    """Calendar-driven staleness decay (task #17) — pulls confidence a fixed
    fraction of the way back to neutral (0.5) per stale tick. Unrelated to
    apply_confidence_delta()'s per-Thought mechanism — no Thought ran, so
    there's no stance/quality to weigh, just "nothing has been checked in a
    while, conviction should soften toward uncertainty."
    """
    new_confidence = round(old_confidence + (0.5 - old_confidence) * _DECAY_PULL_FRACTION, 3)
    entry = {"date": today.isoformat(), "stance": "decay", "stance_strength": 0.0,
              "quality_weight": 0.0, "delta": round(new_confidence - old_confidence, 3),
              "confidence": new_confidence}
    return new_confidence, entry


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


async def list_active_subjects(qdrant) -> list[dict]:
    """Enumerate all semantic:subject:<id> entries — deterministic scroll,
    not vector search, since we want every active subject, not top-K.
    Canonical location (moved here from monitoring/cognition_harness.py
    2026-07-03 — that module already imports several subjects.py primitives;
    this one belongs at the same layer, and living there would have made
    decay_stale_subjects() import cognition_harness.py, a circular import)."""
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        from execution.adapters.qdrant import SEMANTIC
        points, _ = await qdrant.archive_client.scroll(
            collection_name=SEMANTIC,
            scroll_filter=Filter(must=[
                FieldCondition(key="domain", match=MatchValue(value="subject")),
                FieldCondition(key="status", match=MatchValue(value="active")),
            ]),
            limit=50, with_payload=True, with_vectors=False,
        )
        return [dict(p.payload or {}) for p in points]
    except Exception as exc:
        logger.warning("list_active_subjects: failed: %s", exc)
        return []


async def get_subject_news_keywords(qdrant) -> list[str]:
    """Search keywords from high-level Subjects only, for Subject-bound news
    search (news_harness.py, 2026-07-04). "High-level" = no `_` in the subject
    id — Matt's explicit call: `{parent}_{focus}` sub-focus Subjects
    (crypto_btc, crypto_revenue, ai_ops, macro_inflation, ...) are too narrow
    for general news search and would just multiply query volume; only the
    parent Subjects (crypto, ai, macro, retirement, property) qualify.

    Reuses list_active_subjects() (the canonical enumeration) rather than a
    separate scroll — single source of truth per CLAUDE.md standing order #2/3.
    Keywords come from each Subject's `search_keywords` field (set at creation
    or backfilled), not extracted from thesis prose — thesis text isn't
    reliable to mine for search terms without an extra LLM call.
    """
    subjects = await list_active_subjects(qdrant)
    keywords: list[str] = []
    seen = set()
    for s in subjects:
        subject_id = s.get("subject") or ""
        if not subject_id or "_" in subject_id:
            continue
        for kw in (s.get("search_keywords") or []):
            if kw not in seen:
                seen.add(kw)
                keywords.append(kw)
    return keywords



def get_confidence_target(subject: dict) -> float:
    """Per-subject confidence_target, defaulting to _DEFAULT_CONFIDENCE_TARGET
    when unset. Never a global constant applied uniformly."""
    t = subject.get("confidence_target")
    return float(t) if t else _DEFAULT_CONFIDENCE_TARGET


def get_epistemic_status(subject: dict) -> str:
    """Popperian status distinct from `status` (which just gates whether a
    Subject is in the active investigation rotation): "investigating" |
    "corroborated" | "refuted". Defaults "investigating" for legacy Subjects
    predating this field (2026-07-06)."""
    return subject.get("epistemic_status") or "investigating"


async def find_subject_by_reference(qdrant, ref: str) -> dict | None:
    """Resolve a loosely-worded Director reference ("bitcoin", "the crypto
    exit plan") to a Subject record. Deterministic, no LLM — exact subject_id
    match first, then substring match against subject_id (both directions),
    same shape as find_active_by_title()/find_pending_task_by_phrase()
    elsewhere in the codebase. Returns the first/best match or None."""
    # Common colloquial names don't share a substring with the ticker-based
    # subject_id (crypto_btc, crypto_eth) — resolve these first.
    _ALIASES = {"bitcoin": "btc", "ethereum": "eth", "ether": "eth"}
    ref_norm = ref.strip().lower().replace(" ", "_")
    for word, alias in _ALIASES.items():
        if word in ref_norm:
            ref_norm = ref_norm.replace(word, alias)
    subjects = await list_active_subjects(qdrant)
    for s in subjects:
        if s.get("subject", "") == ref_norm:
            return s
    ref_words = ref_norm.split("_")
    for s in subjects:
        sid = s.get("subject", "")
        if ref_norm in sid or sid in ref_norm:
            return s
        if any(w in sid for w in ref_words if len(w) >= 4):
            return s
    return None


def format_subject_list(subjects: list[dict]) -> str:
    """Director-facing one-line-per-Subject summary (Director, 2026-07-07:
    "make Rex subject aware"). No LLM — pure formatting over already-loaded
    Subject payloads."""
    if not subjects:
        return "No active Subjects."
    lines = [f"{len(subjects)} active Subject(s):"]
    for s in sorted(subjects, key=lambda x: x.get("subject", "")):
        sid = s.get("subject", "?")
        conf = s.get("confidence")
        target = get_confidence_target(s)
        conf_str = f"{conf*100:.0f}%" if isinstance(conf, (int, float)) else "?"
        status = get_epistemic_status(s)
        n_open = len(s.get("open_questions") or [])
        lines.append(
            f"• {sid} — confidence {conf_str} (target {target*100:.0f}%), "
            f"{status}, {n_open} open question(s)"
        )
    return "\n".join(lines)


def format_subject_detail(subject: dict) -> str:
    """Full interrogation view of one Subject — thesis, confidence/target,
    epistemic status, evidence weight, open questions, risk flags, and the
    most recent knowns. Director, 2026-07-07: "able to interrogate findings"."""
    sid = subject.get("subject", "?")
    conf = subject.get("confidence")
    conf_str = f"{conf*100:.0f}%" if isinstance(conf, (int, float)) else "?"
    target = get_confidence_target(subject)
    status = get_epistemic_status(subject)
    ew = subject.get("evidence_weight") or {}
    lines = [
        f"Subject: {sid}",
        f"Thesis: {subject.get('thesis', '(none)')}",
        f"Confidence: {conf_str} (target {target*100:.0f}%) — {status}",
        f"Evidence weight: {ew.get('supports', 0)} supports / "
        f"{ew.get('contradicts', 0)} contradicts / {ew.get('neutral', 0)} neutral",
    ]
    last_thought = subject.get("last_thought")
    if last_thought:
        lines.append(f"Last Thought: {last_thought}")
    open_qs = subject.get("open_questions") or []
    if open_qs:
        lines.append("\nOpen questions:")
        lines += [f"• {q}" for q in open_qs]
    risk_flags = subject.get("risk_flags") or []
    if risk_flags:
        lines.append("\nRisk flags (contradicting evidence):")
        lines += [f"• {r}" for r in risk_flags]
    knowns = subject.get("knowns") or []
    if knowns:
        lines.append(f"\nMost recent knowns (of {len(knowns)}):")
        lines += [f"• {k}" for k in knowns[-3:]]
    return "\n".join(lines)


async def evaluate_thought_iteration(
    cog, subject_thesis: str,
    research_result: dict, iterations_used: int, max_iterations: int,
    force_evaluate: bool = False,
) -> dict:
    """Goal-seeking evaluate step — run after each research iteration.

    Decides whether to search again WITHIN this one Thought — "did this pass
    turn up enough corroborated material, or is it worth digging further."
    Uses the fixed `_ITERATION_QUALITY_BAR`, not any Subject's own
    confidence_target (see module docstring point 3) — this loop has no
    concept of thesis validation, only "was this search good enough to stop."

    Two conditions must BOTH be true to iterate again: this pass's source
    quality below the bar, AND the LLM evaluator identifies specific
    resolvable gaps a further pass could plausibly answer. Below-bar alone is
    not enough — burning iteration budget on a question the research harness
    can't resolve is wasted spend, so a below-bar result with no resolvable
    gaps stops the thought rather than exhausting the ceiling.

    force_evaluate: skip the quality-bar short-circuit and always ask the LLM
    for resolvable gaps. Used by observe_for_subject() — its pseudo
    research_result has no real source-quality reading (there's no search to
    grade, just an external observation), so the quality bar is meaningless
    there; what it actually wants is this function's "is there a genuine gap
    worth a full Thought" judgment, unconditionally.

    Returns (schema fixed — feeds the Director notification and episodic log,
    so Rex knows why a thought stopped, not just that it did):
        quality_score:   float — this iteration's source-quality weight
        quality_met:      bool — did this pass clear the iteration bar
        resolvable_gaps: list[str]
        iterate:         bool
        stop_reason:     "quality_met" | "gaps_remain" | "no_resolvable_gaps" | "budget_exhausted"
    """
    quality_score = label_to_weight(research_result.get("confidence", "MEDIUM"))
    quality_met = quality_score >= _ITERATION_QUALITY_BAR
    budget_remains = iterations_used < max_iterations

    if quality_met and not force_evaluate:
        return {
            "quality_score": quality_score, "quality_met": True,
            "resolvable_gaps": [], "iterate": False, "stop_reason": "quality_met",
        }

    if not budget_remains:
        return {
            "quality_score": quality_score, "quality_met": False,
            "resolvable_gaps": [], "iterate": False, "stop_reason": "budget_exhausted",
        }

    prompt = f"""You are evaluating one research iteration inside an ongoing Subject thought.

Subject thesis: {subject_thesis}
This iteration's source quality: {quality_score:.0%} ({research_result.get('confidence', 'MEDIUM')})
Research summary:
{chr(10).join('- ' + b for b in (research_result.get('telegram_summary') or []))}

The sources gathered so far were sparse or mixed. Identify specific, resolvable gaps —
questions a further focused research pass could plausibly answer. If the remaining
uncertainty isn't something more research would resolve (diminishing returns), say so
explicitly.

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
        iterate = bool(resolvable_gaps)  # both conditions already true: below bar + gaps found
        return {
            "quality_score":   quality_score,
            "quality_met":     False,
            "resolvable_gaps": resolvable_gaps,
            "iterate":         iterate,
            "stop_reason":     "gaps_remain" if iterate else "no_resolvable_gaps",
            "next_question":   data.get("next_question", "") if iterate else "",
        }
    except Exception as exc:
        logger.warning("evaluate_thought_iteration: failed, treating as no resolvable gaps: %s", exc)
        return {
            "quality_score": quality_score, "quality_met": False, "resolvable_gaps": [],
            "iterate": False, "stop_reason": "no_resolvable_gaps",
        }


# Quality gate thresholds (2026-07-03) — Director's framing: confidence alone
# measures certainty, not whether understanding actually improved. A Thought
# can report high confidence on pure restatement of what was already known.
# novelty_score below this is treated as "nothing genuinely new" regardless
# of confidence. 0.3 is a deliberately low bar — this is a floor against
# pure restatement, not a demand for a breakthrough.
_NOVELTY_THRESHOLD = 0.3


async def assess_thought_quality(cog, subject: dict, trigger_summary: str, research_result: dict) -> dict:
    """One LLM call, run unconditionally on every completed Thought (changed
    2026-07-06 — previously skipped on a confidence-miss on the theory that a
    result already "not applied" wasn't worth reading; under the current
    design nothing is discarded based on a per-iteration source-quality
    label, so this is the only place that reads what a Thought actually
    found). A thin/failed research pass naturally scores near-zero novelty
    and neutral stance here rather than needing a special-cased skip.

    Returns {"novelty_score": 0-1, "questions_closed": [...],
    "thesis_stance": "supports"|"contradicts"|"neutral", "stance_strength": 0-1}.

    novelty_score: is there anything genuinely new here, or pure restatement
    of existing knowns — the only real gate on whether resolve_thought_outcome
    applies anything at all (see module docstring — recording evidence no
    longer depends on clearing confidence_target).

    thesis_stance/stance_strength: this Thought's OVERALL bearing on the
    thesis — independent of whether it closed a specific enumerated open
    question. A finding can clearly support or contradict a thesis without
    matching any listed open_question verbatim; gating the confidence update
    on questions_closed alone would miss that. This is what
    apply_confidence_delta() actually moves confidence by.

    questions_closed: which specific open_questions this finding resolves —
    {"question": "...", "resolution": "...", "stance": "supports"|"contradicts"}.
    question is matched back against open_questions verbatim-ish (substring
    match) when applied — an approximate match is fine, an over-cautious
    non-match just leaves the question open, not lost. resolution is a
    factual statement suitable for promotion into knowns — a closed question
    becomes a fact, it doesn't just vanish (bug found 2026-07-04).
    """
    open_questions = subject.get("open_questions") or []
    knowns = subject.get("knowns") or []
    prompt = f"""You are assessing whether a completed research thought actually added understanding,
and what it means for the thesis.

Subject thesis: {subject.get('thesis', '')}
Existing knowns: {knowns}
Existing open questions: {open_questions}

New finding (trigger: {trigger_summary}):
{chr(10).join('- ' + b for b in (research_result.get('telegram_summary') or []))}

Score novelty 0-1: how much of this is genuinely new information not already covered by the
existing knowns, versus a restatement/reconfirmation of what was already known? 0 = pure
restatement, 1 = substantially new.

Judge this finding's OVERALL bearing on the thesis, independent of the specific open questions
below: does it support the thesis, contradict it, or is it neutral (informative but doesn't bear
on whether the thesis is true)? Score how strongly (0-1; 0 = doesn't move the needle at all).

List which of the existing open questions (verbatim or near-verbatim from the list above) this
finding actually resolves — not "makes progress on," actually closes. For each one, state the
actual resolution as a factual statement (not a restatement of the question) — this becomes a
known fact about the subject, so phrase it as one. Also state whether that resolution SUPPORTS
the thesis or CONTRADICTS it. Empty list if none resolved.

Respond with JSON only — no preamble:
{{"novelty_score": 0.0, "thesis_stance": "supports|contradicts|neutral", "stance_strength": 0.0,
  "questions_closed": [{{"question": "...", "resolution": "...", "stance": "supports|contradicts"}}]}}"""

    try:
        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=60.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        novelty_score = float(data.get("novelty_score", 0.0))
        stance = data.get("thesis_stance")
        stance = stance if stance in ("supports", "contradicts", "neutral") else "neutral"
        stance_strength = float(data.get("stance_strength", 0.0))
        questions_closed = [
            {
                "question": str(c.get("question", "")),
                "resolution": str(c.get("resolution", "")),
                "stance": c.get("stance") if c.get("stance") in ("supports", "contradicts") else "supports",
            }
            for c in (data.get("questions_closed") or [])
            if isinstance(c, dict) and c.get("question") and c.get("resolution")
        ]
        return {
            "novelty_score": max(0.0, min(1.0, novelty_score)),
            "thesis_stance": stance,
            "stance_strength": max(0.0, min(1.0, stance_strength)),
            "questions_closed": questions_closed,
        }
    except Exception as exc:
        logger.warning("assess_thought_quality: failed, treating as no novelty/no resolution: %s", exc)
        return {"novelty_score": 0.0, "thesis_stance": "neutral", "stance_strength": 0.0, "questions_closed": []}


_STOP_REASON_LABELS = {
    "quality_met":        "sufficient source material found",
    "gaps_remain":        "stopped mid-budget with gaps still open (unexpected — should have iterated)",
    "no_resolvable_gaps": "stopped — remaining uncertainty judged not resolvable by further research",
    "budget_exhausted":   "iteration budget exhausted",
}


async def score_memory_signals(qdrant, text: str, source: str = "") -> dict:
    """Canonical memory-signal function (task #27, 2026-07-03) — impact +
    urgency for any piece of memory-bound content, not Thought-specific.
    Reuses the exact same primitives every other trigger source already
    uses (find_relevant_subjects + derive_impact + derive_urgency) — no new
    thresholds, no new vocabulary, per Director's explicit call to stick
    with the live code rather than invent parallel definitions.

    No composite "priority" score — impact and urgency stay separate axes
    (Director's call: a composite reopens exactly the "monitor-down alert is
    high urgency, low impact, and that's supposed to happen" problem the
    two-axis design already solved). What to DO about a given combination is
    a policy decision for whatever reads these signals later, not baked into
    the signal itself.

    One embed call (find_relevant_subjects, no LLM) — same cost as every
    other triage call in the system, not a new expense.
    """
    hits = await find_relevant_subjects(qdrant, text[:2000])
    impact_label, _ = derive_impact(hits)
    urgency_label, _ = derive_urgency(text, source)
    return {"impact": impact_label, "urgency": urgency_label}


async def _log_thought_stop_episodic(
    qdrant, subject_id: str, thought_id: str | None,
    stop_reason: str, resolvable_gaps: list[str],
    quality_score: float, iterations_used: int,
    signals: dict | None = None,
) -> None:
    """Episodic record of WHY a thought's iteration loop stopped — distinct
    from the research-complete episodic entry (one thought may run several
    research iterations, but there is exactly one stop-reason event), and
    distinct from whether the finding was applied to the Subject (that's a
    separate outcome, see resolve_thought_outcome's own semantic/episodic
    write on the apply path). This IS the thought's permanent record —
    thoughts are Qdrant-only, no Nextcloud note.

    quality_score: this iteration's source-quality weight (label_to_weight()
    of the research result's HIGH/MEDIUM/LOW label) vs the fixed
    _ITERATION_QUALITY_BAR — NOT the Subject's thesis confidence (see module
    docstring point 2). "Why did the search loop stop," not "did this change
    what we believe."

    signals: impact/urgency from score_memory_signals() (task #27), stamped
    unconditionally — even a Thought whose conclusion didn't get applied
    still produced a real finding worth marking for later surfacing."""
    try:
        today = date.today().isoformat()
        metadata = {
            "type": "episodic", "event_type": "thought_stop",
            "subject": subject_id, "thought_id": thought_id,
            "stop_reason": stop_reason, "resolvable_gaps": resolvable_gaps,
            "quality_score": quality_score, "quality_bar": _ITERATION_QUALITY_BAR,
            "iterations_used": iterations_used, "ts": today,
        }
        if signals:
            metadata["impact"] = signals.get("impact")
            metadata["urgency"] = signals.get("urgency")
        await qdrant.store(
            collection="episodic",
            content=(
                f"Thought for subject '{subject_id}' stopped after {iterations_used} "
                f"iteration(s): {stop_reason} (source quality {quality_score:.0%} vs "
                f"{_ITERATION_QUALITY_BAR:.0%} bar)."
            ),
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("_log_thought_stop_episodic: failed for %r: %s", subject_id, exc)


async def count_thoughts_today(qdrant, subject_id: str, cap: int) -> int:
    """How many Thoughts have already stopped today for this subject — the
    daily repetition cap's (cognition/thoughts.py's spawn_thought()) source of
    truth. Counts thought_stop episodic records (already written by every
    completed Thought via _log_thought_stop_episodic above), not in-flight
    Thoughts — a soft anti-spam cap, not a hard concurrency limit. `cap`
    bounds the scroll limit (only need to know if the count has reached it,
    not the exact total)."""
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue
    today = date.today().isoformat()
    try:
        points, _ = await qdrant.archive_client.scroll(
            collection_name="episodic",
            scroll_filter=Filter(must=[
                FieldCondition(key="event_type", match=MatchValue(value="thought_stop")),
                FieldCondition(key="subject", match=MatchValue(value=subject_id)),
                FieldCondition(key="ts", match=MatchValue(value=today)),
            ]),
            limit=cap + 1,
            with_payload=False,
        )
        return len(points)
    except Exception as exc:
        logger.warning("count_thoughts_today: failed for %r: %s", subject_id, exc)
        return 0  # fail open — a broken count check must not silently block all Thoughts


async def resolve_thought_outcome(
    qdrant, nanobot, cog, subject_id: str, thought_id: str | None,
    trigger_source: str, trigger_summary: str, research_result: dict,
    confidence_target: float | None = None,
    quality_met: bool = True, resolvable_gaps: list[str] | None = None,
    stop_reason: str = "quality_met", iterations_used: int = 1,
) -> dict:
    """Resolve a completed Thought against its Subject — no Director approval
    gate (removed 2026-07-03, Director's call: "approve X" for every Thought
    conclusion doesn't scale once Thoughts run unattended, and reviewing them
    out of arrival order risked exactly the kind of mess a human-in-the-loop
    step is supposed to prevent, not cause).

    Redesigned 2026-07-06 (see module docstring) — the prior "three-axis
    gate" conflated two different questions behind one check: "should we
    record this evidence" and "is the thesis now validated." Recording no
    longer depends on confidence_target at all:

    The only gate on whether a Thought's finding gets applied is **novelty**
    — assess_thought_quality()'s novelty_score >= _NOVELTY_THRESHOLD (did
    this add genuinely new information, not restatement). `quality_met`
    (this iteration's source-quality vs the fixed _ITERATION_QUALITY_BAR,
    computed by evaluate_thought_iteration) and questions_closed are
    informational/bookkeeping now, not gates — a Thought with sparse sources
    can still deliver a real, novel finding worth recording, and a Thought
    can clearly support or contradict the thesis without closing one of the
    specific enumerated open_questions.

    Novelty passes -> applies: confidence moves via apply_confidence_delta()
        (stance-weighted, bounded step — NOT averaged from source-richness,
        see module docstring), Nextcloud Subject Note rewrite, the
        semantic:subject:<id> Qdrant upsert, and the research semantic +
        episodic entries (subject-tagged). Telegram notification is purely
        informational — no reply expected.

    Novelty fails -> nothing about the Subject changes at all (no write —
        there is nothing to write; confidence, thesis, knowns all stay
        exactly as they were). This is the ordinary, frequent case once
        several Subjects are active and news repeats itself — not a failure
        needing a compensating control. Still logged to episodic either way
        (via _log_thought_stop_episodic) so there's a permanent record of
        what was tried.

    No self-tightening/decay-on-miss mechanism anymore (that mechanism is
    deleted, not just re-tuned — see module docstring and CLAUDE.md history
    for why it existed and why it doesn't need to under this design): it was
    built to compensate for confidence being capped at a per-thought label's
    ceiling (0.75) while confidence_target climbed unboundedly toward 0.95,
    making high targets structurally unreachable. Under the new model,
    confidence is a genuine accumulator with its own ceiling (_CONFIDENCE_CEILING
    0.95) that moves through repeated real evidence, not a rolling average of
    a capped per-thought label — the unreachability problem that motivated
    decay doesn't arise the same way, so patching its trigger condition
    further would just be preserving a workaround for a bug that's now fixed
    at the source.
    """
    subject = await get_subject(qdrant, subject_id)
    if not subject:
        logger.warning("resolve_thought_outcome: unknown subject %r", subject_id)
        return {"status": "error", "error": f"unknown subject {subject_id!r}"}

    today = date.today()
    today_iso = today.isoformat()
    history = subject.get("confidence_history") or []
    source_quality_label = research_result.get("confidence", "MEDIUM")
    quality_weight = label_to_weight(source_quality_label)
    old_confidence = subject.get("confidence", 0.5)
    target = confidence_target if confidence_target is not None else get_confidence_target(subject)
    resolvable_gaps = resolvable_gaps or []
    telegram_summary = research_result.get("telegram_summary") or []
    full_report = research_result.get("full_report", "")
    old_thesis = subject.get("thesis", "")

    # Memory signals (task #27) — computed once here, stamped onto every
    # memory write this call produces (episodic thought_stop below, plus the
    # research semantic/episodic pair on the apply path further down). Not
    # stamped onto the Subject's own semantic:subject:<id> record — that's
    # canonical current-state, not a "finding" to score for relevance against
    # Subjects (would be circular).
    signals = await score_memory_signals(qdrant, trigger_summary, trigger_source)

    await _log_thought_stop_episodic(
        qdrant, subject_id, thought_id, stop_reason, resolvable_gaps,
        quality_weight, iterations_used,
        signals=signals,
    )

    # Always assessed now — nothing is discarded based on quality_met
    # anymore, so there's no "would be wasted spend" case left to skip.
    existing_open_questions = subject.get("open_questions") or []
    quality = await assess_thought_quality(cog, subject, trigger_summary, research_result)
    novelty_score = quality["novelty_score"]
    thesis_stance = quality["thesis_stance"]
    stance_strength = quality["stance_strength"]
    questions_closed = quality["questions_closed"]
    novelty_ok = novelty_score >= _NOVELTY_THRESHOLD

    if not novelty_ok:
        reason_label = f"not novel enough ({novelty_score:.2f} < {_NOVELTY_THRESHOLD})"

        # Notification-fatigue fix (Director, 2026-07-06: "the failures are a
        # bit spammy... just the notification of thoughts that actually
        # changed things would be better"). Nothing changed here by
        # definition (no write happened) — the episodic record above is the
        # permanent audit trail. Only push to Telegram when independently
        # urgent; otherwise Director learns about it next time they ask.
        if signals.get("urgency") == "high":
            summary = "\n".join(f"• {b}" for b in telegram_summary[:3])
            lines = [
                f"⚠️ *URGENT* — 🧠 *Thought stopped — {subject_id}* ({reason_label})",
                f"Confidence unchanged at {old_confidence:.0%} (target {target:.0%})",
                "",
                summary,
            ]
            if resolvable_gaps:
                lines += ["", "Open questions:"] + [f"- {q}" for q in resolvable_gaps]
            await _notify_telegram("\n".join(lines))

        return {
            "status": "ok", "action": "not_applied", "subject_id": subject_id,
            "quality_met": quality_met, "novelty_score": novelty_score,
            "questions_closed": questions_closed, "confidence_target": target,
            "signals": signals,
        }

    # Novelty passes — apply. Confidence moves by a bounded, stance-weighted
    # step (module docstring) rather than an averaged source-richness label.
    new_confidence, history_entry = apply_confidence_delta(
        old_confidence, thesis_stance, stance_strength, quality_weight, today,
    )
    updated_history = (history + [history_entry])[-_HISTORY_CAP:]

    # Questions the assessor confirmed closed come off open_questions
    # (approximate substring match — an over-cautious non-match just means
    # the question, still open, is untouched, not lost) and their resolution
    # is promoted into knowns — a closed question becomes a fact, it doesn't
    # just vanish (bug found and fixed 2026-07-04: it previously did just
    # vanish).
    note_id = subject.get("note_id")
    _closed_question_texts = [c.get("question", "") for c in questions_closed]
    _closed_resolutions = [
        (c["resolution"] if c.get("stance", "supports") == "supports"
         else f"[CONTRADICTS THESIS] {c['resolution']}")
        for c in questions_closed if c.get("resolution")
    ]
    open_questions = [
        q for q in existing_open_questions
        if not any(closed.lower() in q.lower() or q.lower() in closed.lower()
                   for closed in _closed_question_texts)
    ]
    knowns = list(subject.get("knowns", [])) + _closed_resolutions

    # risk_flags (schema v1.3, 2026-07-04) — a view over contradicting knowns, not a
    # separate pool. A new contradiction appends; a flag clears when a new SUPPORTING
    # resolution substring-matches it (same approximate-match convention already used
    # for closing open_questions above), or via Director resync/edit.
    _new_risk_flags = [c["resolution"] for c in questions_closed
                        if c.get("stance") == "contradicts" and c.get("resolution")]
    _new_supporting = [c["resolution"] for c in questions_closed
                        if c.get("stance", "supports") == "supports" and c.get("resolution")]
    risk_flags = [
        rf for rf in (subject.get("risk_flags") or [])
        if not any(sup.lower() in rf.lower() or rf.lower() in sup.lower() for sup in _new_supporting)
    ] + _new_risk_flags

    # evidence_ratio (final formula, 2026-07-04) — supporting knowns only in the
    # numerator; risk_flags deliberately excluded from the denominator since it's
    # a derived view over knowns, not a separate pool (counting it too would be
    # double-counting the same fact).
    _supporting_knowns_count = len([k for k in knowns if not str(k).startswith("[CONTRADICTS THESIS]")])
    evidence_ratio = round(_supporting_knowns_count / max(1, len(knowns) + len(open_questions)), 3)

    # Popperian epistemic status (2026-07-06 — see module docstring and
    # CLAUDE.md's Cognition Engine section). Every open_question is already
    # defined as one of the thesis's own load-bearing assumptions
    # (create_subject()'s own docstring) — so every questions_closed entry IS
    # a falsification attempt by construction, not a separately-tagged
    # "core" subset: "supports" means the attempt to break the thesis
    # failed (survived — corroborating, per Popper, never "proved"),
    # "contradicts" means it succeeded (refuted). Asymmetric on purpose — one
    # successful falsification outweighs any number of survived attempts
    # elsewhere; this is a hard trigger, not something the smooth
    # apply_confidence_delta() step above should be left to average out.
    old_epistemic_status = get_epistemic_status(subject)
    refuting_closures = [c for c in questions_closed if c.get("stance") == "contradicts"]

    # evidence_weight — how many independent applied Thoughts have actually
    # been weighed, split by direction. Distinct from confidence itself:
    # confidence at 75% from one Thought and 75% from ten agreeing Thoughts
    # are not equally trustworthy, and this is the field that tells them
    # apart (task from the 2026-07-06 "how do we know it's robust" discussion).
    evidence_weight = dict(subject.get("evidence_weight") or {"supports": 0, "contradicts": 0, "neutral": 0})
    evidence_weight[thesis_stance] = evidence_weight.get(thesis_stance, 0) + 1
    total_evidence_weight = sum(evidence_weight.values())

    if refuting_closures:
        new_epistemic_status = "refuted"
    elif (
        not open_questions and new_confidence >= target and not risk_flags
        and total_evidence_weight >= _MIN_EVIDENCE_WEIGHT
    ):
        new_epistemic_status = "corroborated"
    else:
        new_epistemic_status = "investigating"

    summary_lines = "\n".join(f"- {b}" for b in telegram_summary)
    risks_section = (
        "\n## Thesis Risks\n" + "\n".join(f"- {r}" for r in risk_flags) + "\n"
        if risk_flags else ""
    )

    if note_id:
        note_content = (
            "---\n"
            "type: subject\n"
            f"subject: {subject_id}\n"
            "status: active\n"
            f"epistemic_status: {new_epistemic_status}\n"
            f"confidence: {new_confidence:.2f}\n"
            f"last_updated: {today_iso}\n"
            f"last_thought: {today_iso}\n"
            f"confidence_history: {json.dumps(updated_history)}\n"
            f"confidence_target: {target}\n"
            f"evidence_ratio: {evidence_ratio}\n"
            f"evidence_weight: {json.dumps(evidence_weight)}\n"
            f"thesis_components: {json.dumps(subject.get('thesis_components'))}\n"
            "---\n\n"
            f"{_MANUAL_EDIT_NOTE.format(subject_id=subject_id)}\n\n"
            f"## Thesis\n{old_thesis}\n\n"
            f"## Latest Thought Update ({today_iso})\n"
            f"Trigger: {trigger_summary}\n\n"
            f"{summary_lines}\n"
            f"{risks_section}"
        )
        try:
            await nanobot.run("openclaw-nextcloud", "notes_update", {
                "note-id": note_id, "content": note_content,
            })
        except Exception as exc:
            logger.warning("resolve_thought_outcome: notes_update failed for %r: %s", subject_id, exc)

    try:
        await qdrant.store(
            collection="semantic",
            content=f"Subject: {subject_id}\nThesis: {old_thesis}\nLatest update: {trigger_summary}",
            metadata={
                "type": "semantic", "domain": "subject",
                "_key": f"semantic:subject:{subject_id}",
                "subject": subject_id, "status": "active",
                "epistemic_status": new_epistemic_status,
                "confidence": new_confidence,
                "confidence_history": updated_history,
                "confidence_target": target,
                "evidence_weight": evidence_weight,
                "thesis": old_thesis,
                "original_thesis": subject.get("original_thesis", old_thesis),
                "open_questions": open_questions,
                "knowns": knowns,
                "note_id": note_id,
                "last_thought": today_iso,
                **_subject_stub_fields(subject),
                "risk_flags": risk_flags,
                "evidence_ratio": evidence_ratio,
            },
        )
    except Exception as exc:
        logger.warning("resolve_thought_outcome: semantic upsert failed for %r: %s", subject_id, exc)

    try:
        from monitoring.research_harness import _write_research_semantic, _write_episodic
        await _write_research_semantic(
            qdrant, topic=trigger_summary, domain_scope="general",
            note_id=None, note_title="", full_report=full_report,
            confidence=source_quality_label, report_date=today_iso,
            subject=subject_id, signals=signals,
        )
        await _write_episodic(
            qdrant, topic=trigger_summary, domain_scope="general",
            confidence=source_quality_label, sources_ok=[], note_id=thought_id,
            subject=subject_id, signals=signals,
        )
    except Exception as exc:
        logger.warning("resolve_thought_outcome: research semantic/episodic write failed for %r: %s", subject_id, exc)

    urgent_prefix = "⚠️ *URGENT* — " if signals.get("urgency") == "high" else ""
    delta_pct = round((new_confidence - old_confidence) * 100)
    delta_str = f"{delta_pct:+d}pp" if delta_pct else "no change"
    summary = "\n".join(f"• {b}" for b in telegram_summary[:3])
    lines = [
        f"{urgent_prefix}🧠 *Thought complete — {subject_id}*",
        f"Confidence: {old_confidence:.0%} → {new_confidence:.0%} ({delta_str}, target {target:.0%}) "
        f"— {thesis_stance}, strength {stance_strength:.0%}, source {source_quality_label}",
        "",
        summary,
    ]
    await _notify_telegram("\n".join(lines))

    # Generational checkpoint — fires once, only on the transition itself, and
    # always (not urgency-gated) since these are rare and significant by
    # construction, not the routine-miss spam the notification fix upstream
    # addressed. refuted stops all future automatic Thoughts for this Subject
    # (see run_thought()'s guard) — Director review is mandatory, not optional.
    if new_epistemic_status != old_epistemic_status:
        if new_epistemic_status == "refuted":
            failed = refuting_closures[0]  # asymmetric — the first is sufficient, lead with it
            await _notify_telegram(
                f"🚫 *Thesis refuted — {subject_id}*\n"
                f"Core assumption failed: \"{failed.get('question', '')}\"\n"
                f"Resolution: {failed.get('resolution', '')}\n\n"
                "This subject is paused — no further automatic research will run "
                "until you review and create a successor thesis, or manually "
                "reactivate it."
            )
        elif new_epistemic_status == "corroborated":
            await _notify_telegram(
                f"🎓 *Thesis corroborated — {subject_id}*\n"
                f"Confidence {new_confidence:.0%} (target {target:.0%}), "
                f"{total_evidence_weight} independent Thoughts weighed "
                f"({evidence_weight.get('supports', 0)} support, "
                f"{evidence_weight.get('contradicts', 0)} contradict, "
                f"{evidence_weight.get('neutral', 0)} neutral) — no open questions "
                "or unresolved risks remaining."
            )
        elif old_epistemic_status == "corroborated":
            await _notify_telegram(
                f"⚠️ *{subject_id}* no longer considered settled — confidence or "
                "open questions changed since corroboration."
            )

        if new_epistemic_status in ("refuted", "corroborated"):
            try:
                await propose_successor_thesis(
                    qdrant, nanobot, cog,
                    {**subject, "subject": subject_id, "thesis": old_thesis, "knowns": knowns,
                     "risk_flags": risk_flags, "confidence": new_confidence},
                    reason=new_epistemic_status,
                )
            except Exception as exc:
                logger.warning(
                    "resolve_thought_outcome: propose_successor_thesis failed for %r: %s",
                    subject_id, exc,
                )

    return {
        "status": "ok", "action": "applied", "subject_id": subject_id,
        "old_confidence": old_confidence, "new_confidence": new_confidence,
        "epistemic_status": new_epistemic_status, "evidence_weight": evidence_weight,
        "novelty_score": novelty_score,
        "thesis_stance": thesis_stance, "stance_strength": stance_strength,
        "questions_closed": questions_closed, "signals": signals,
    }


async def propose_successor_thesis(qdrant, nanobot, cog, subject: dict, reason: str) -> dict:
    """Draft a candidate successor thesis when a Subject reaches a generational
    checkpoint (corroborated or refuted) — never auto-created (Director's
    explicit call, 2026-07-06): drafting a NEW thesis from accumulated knowns/
    risk_flags is abductive (inference to the best explanation), a
    qualitatively different reasoning mode from the confirmatory testing
    (does this fact support or contradict a claim already on paper) that
    resolve_thought_outcome() does — the same reasoning the Director already
    applied when the routine per-Thought approval gate was removed 2026-07-03:
    aggregating existing signal is safe to automate, a generative judgment
    call that could go wrong in a novel, hard-to-bound way isn't.

    Always local (cog.ask_local(), never _routing_decision()) — matches every
    other LLM call already in this module (assess_thought_quality,
    evaluate_thought_iteration) and the portfolio-harness precedent (2026-05-20
    DCL finding: unmatched thesis/knowns-shaped content silently falls back to
    WORKSPACE_INTERNAL, which IS externally-eligible — forcing local is the
    deliberate override, not relying on DCL's fallback for this kind of
    content). See CLAUDE.md's provider-routing section for the general rule
    this is an intentional exception to.

    falsification_conditions are drafted FIRST and open_questions are derived
    1:1 from them (Director's framing, 2026-07-06: open_questions exist to be
    refuted, not proven — every one should be a live attempt to break the new
    thesis, not a separate brainstorm of "things to look into").

    Writes a `subject_proposal` to PROSPECTIVE (status=pending_approval,
    mirrors the SI-Harness proposal pattern in monitoring/self_improvement.py
    — same shape, no new mechanism) and notifies Telegram with the draft.
    Director confirms via the EXISTING "approve <phrase>" path
    (execution/engine.py's `activate_pending` dispatch — extended, not
    duplicated, to branch on `type: subject_proposal` vs a scheduled task).
    Never raises — a failed draft shouldn't take down the Thought that
    triggered it; the caller already wraps this in a try/except too, this is
    a second layer for the same reason.
    """
    try:
        subject_id = subject.get("subject", "")
        thesis = subject.get("thesis", "")
        knowns = subject.get("knowns", [])
        risk_flags = subject.get("risk_flags", [])
        confidence = subject.get("confidence", 0.5)

        prompt = f"""You are drafting a candidate successor thesis for a Cognition Engine Subject
that just reached a generational checkpoint: {reason}.

Original thesis: {thesis}
Confidence reached: {confidence:.0%}
Knowns accumulated: {knowns}
Unresolved risk flags (contradicting findings): {risk_flags}

A well-formed thesis has five parts:
- claim: the crisp assertion being tested, stripped of justification
- mechanism: WHY the claim should be true — the causal story
- time_horizon: the period over which it should resolve (without this a thesis can never be wrong)
- materiality: why this is worth tracking — what decision depends on it
- falsification_conditions: what would prove this WRONG — specific, checkable conditions

Draft a NEW thesis that is closer to reality given everything above — if reason is "refuted",
the new thesis must NOT depend on the assumption that just failed; if reason is "corroborated",
the new thesis should sharpen or extend into what's now worth asking next, not just restate what's
already settled.

Then derive open_questions DIRECTLY from your falsification_conditions — exactly one question per
condition. Each open_question MUST be a closed yes/no question with a crisp, checkable resolution
(e.g. "Has X exceeded Y by {{date}}?", not "What factors influence X?") — an open-ended question
has no clean pass/fail and can't actually be closed one way or the other. Do not brainstorm
separate questions; every open_question must be a yes/no operationalization of a
falsification_condition. These exist to be refuted, not proven — surviving repeated attempts to
trigger them is what corroborates a thesis, nothing here should ever "prove" anything.

Suggest a new_subject_id: lowercase, underscores, following the same lineage convention as the
current subject id (e.g. if splitting narrower, {{parent}}_{{focus}}; if a genuine successor
generation, a clear variant of {subject_id}).

Respond with JSON only — no preamble:
{{"new_subject_id": "...", "claim": "...", "mechanism": "...", "time_horizon": "...",
  "materiality": "...", "falsification_conditions": ["...", "..."],
  "open_questions": ["...", "..."], "rationale": "one paragraph connecting this to what was learned"}}"""

        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=60.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}

        new_subject_id = str(data.get("new_subject_id") or f"{subject_id}_v2").strip().lower().replace(" ", "_")
        falsification_conditions = [str(c) for c in (data.get("falsification_conditions") or [])]
        open_questions = [str(q) for q in (data.get("open_questions") or [])]
        thesis_components = {
            "claim": data.get("claim", ""), "mechanism": data.get("mechanism", ""),
            "time_horizon": data.get("time_horizon", ""), "materiality": data.get("materiality", ""),
            "falsification_conditions": falsification_conditions,
        }
        draft_thesis = " ".join(
            s for s in [data.get("claim", ""), data.get("mechanism", ""),
                        data.get("time_horizon", ""), data.get("materiality", "")] if s
        )
        rationale = data.get("rationale", "")

        today = date.today().isoformat()
        await qdrant.store(
            collection="prospective",
            content=f"Subject proposal: {new_subject_id} (succeeds {subject_id}, {reason})",
            metadata={
                "type": "subject_proposal", "status": "pending_approval",
                "title": f"New Subject Proposal: {new_subject_id}",
                "new_subject_id": new_subject_id, "succeeds": subject_id, "reason": reason,
                "draft_thesis": draft_thesis, "draft_thesis_components": thesis_components,
                "draft_open_questions": open_questions, "rationale": rationale,
                "_key": f"prospective:subject_proposal:{new_subject_id}",
                "created_ts": today,
            },
        )

        oq_lines = "\n".join(f"- {q}" for q in open_questions) or "(none)"
        fc_lines = "\n".join(f"- {c}" for c in falsification_conditions) or "(none)"
        await _notify_telegram(
            f"📋 *Proposed successor thesis for {subject_id}* (succeeds, {reason})\n\n"
            f"*{new_subject_id}*\n{draft_thesis}\n\n"
            f"Falsification conditions:\n{fc_lines}\n\n"
            f"Open questions:\n{oq_lines}\n\n"
            f"_{rationale}_\n\n"
            f'Reply "approve subject {new_subject_id}" to create it, or edit and ask me to '
            "redraft."
        )
        return {"status": "ok", "new_subject_id": new_subject_id, "succeeds": subject_id}
    except Exception as exc:
        logger.warning("propose_successor_thesis: failed for %r: %s", subject.get("subject"), exc)
        return {"status": "error", "error": str(exc)}


async def create_subject_from_proposal(qdrant, nanobot, proposal: dict) -> dict:
    """Action a Director-confirmed subject_proposal (see propose_successor_thesis()).

    Called from execution/engine.py's `activate_pending` dispatch — the SAME
    "approve <phrase>" path task_activate already uses (`find_pending_task_by_phrase()`
    doesn't check `type`, so it matches a subject_proposal's title exactly like a
    scheduled task's; the dispatch branches on `proposal.get("type")` to call this
    instead of `TaskScheduler.activate_task()`). No new NL routing was added for
    this — reuses the existing regex, intent, and lookup wholesale.

    proposal: the dict returned by find_pending_task_by_phrase() — the raw
    PROSPECTIVE payload plus `point_id`.
    """
    try:
        result = await create_subject(
            qdrant, nanobot,
            subject_id=proposal["new_subject_id"],
            thesis=proposal.get("draft_thesis", ""),
            open_questions=proposal.get("draft_open_questions") or [],
            thesis_components=proposal.get("draft_thesis_components"),
            succeeds=proposal.get("succeeds"),
        )
        if result.get("status") == "ok":
            try:
                await qdrant.archive_client.set_payload(
                    collection_name="prospective",
                    payload={"status": "actioned"},
                    points=[proposal["point_id"]],
                )
            except Exception as exc:
                logger.warning("create_subject_from_proposal: failed to mark proposal actioned: %s", exc)
            await _notify_telegram(
                f"✅ Created *{proposal['new_subject_id']}* (succeeds {proposal.get('succeeds', '?')})."
            )
        return result
    except Exception as exc:
        logger.warning("create_subject_from_proposal: failed for %r: %s", proposal.get("new_subject_id"), exc)
        return {"status": "error", "error": str(exc)}


# Time-based decay (task #17, 2026-07-03; mechanism updated 2026-07-06 to use
# decay_confidence_towards_neutral() instead of rolling_confidence(), which no
# longer exists — see module docstring). Folds a periodic "nothing new
# happened" pull toward neutral (0.5) into any Subject that's gone quiet.
# Unrelated to the per-Thought apply_confidence_delta() mechanism above — no
# Thought ran, so there's no stance/quality to weigh, just calendar staleness
# softening conviction toward uncertainty.
_DECAY_STALE_DAYS = 30       # no confidence_history entry newer than this -> decay applies
_DECAY_ALERT_THRESHOLD = 0.30  # Director-notify when decayed confidence drops below this


async def _check_subject_drift(nanobot, subject_id: str, subject: dict) -> bool:
    """Drift detection (schema v1.3, 2026-07-04) — compares the Nextcloud Note's
    modified timestamp (Unix seconds) against Qdrant's last_updated (ISO8601),
    both parsed UTC-aware to avoid a silent offset bug. Detection only — does
    not decide which side wins, just warns. Director resolves via "learn
    subject <id>" (resync_subject_from_note) if the Note edit should win.
    Returns True if drift was found (and a warning sent)."""
    note_id = subject.get("note_id")
    qdrant_updated_raw = subject.get("last_updated")
    if not note_id or not qdrant_updated_raw:
        return False
    try:
        nb = await nanobot.run("openclaw-nextcloud", "notes_read", {"note-id": note_id})
        result = nb.get("result") if nb.get("result") is not None else nb
        note_modified_unix = result.get("modified") if isinstance(result, dict) else None
    except Exception as exc:
        logger.warning("_check_subject_drift: notes_read failed for %r: %s", subject_id, exc)
        return False
    if not note_modified_unix:
        return False
    try:
        note_modified = datetime.fromtimestamp(int(note_modified_unix), tz=timezone.utc)
        qdrant_updated = datetime.fromisoformat(str(qdrant_updated_raw).replace("Z", "+00:00"))
        if qdrant_updated.tzinfo is None:
            qdrant_updated = qdrant_updated.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OSError):
        return False
    if note_modified > qdrant_updated:
        await _notify_telegram(
            f"⚠️ *Drift detected — {subject_id}*\n"
            "The Nextcloud Note was edited more recently than the canonical record "
            "was last updated.\n"
            f"Note modified: {note_modified.isoformat()}\n"
            f"Qdrant last updated: {qdrant_updated.isoformat()}\n"
            f'Say "learn subject {subject_id}" to reconcile if the Note edit should win.'
        )
        return True
    return False


async def decay_stale_subjects(qdrant, nanobot) -> dict:
    """Daily check (scheduled task, see task_scheduler.seed_subject_decay_task) —
    folds one neutral confidence_history entry into any active Subject whose
    most recent entry is >= _DECAY_STALE_DAYS old, i.e. no Thought has run for
    it in that window. Qdrant-only (no Nextcloud note rewrite) — a decay tick
    has no narrative to add to the note; Qdrant remains canonical per this
    module's design principle, the note just goes stale on confidence display
    until the next real Thought writes a fresh one.

    Also runs drift detection (schema v1.3) for every active Subject regardless
    of staleness — cheap (one Note read, no LLM), piggybacked onto this existing
    daily sweep rather than a separate scheduled task.

    Returns {"decayed": [...subject_ids], "alerted": [...subject_ids below threshold],
    "drifted": [...subject_ids with a Note newer than Qdrant]}.
    """
    today = date.today()
    decayed: list[str] = []
    alerted: list[str] = []
    drifted: list[str] = []
    subjects = await list_active_subjects(qdrant)

    for subject in subjects:
        subject_id = subject.get("subject", "")
        if await _check_subject_drift(nanobot, subject_id, subject):
            drifted.append(subject_id)
        history = subject.get("confidence_history") or []
        if not history:
            continue  # never had a Thought yet — nothing to decay
        try:
            last_entry_date = date.fromisoformat(history[-1]["date"])
        except (KeyError, ValueError):
            continue
        days_stale = (today - last_entry_date).days
        if days_stale < _DECAY_STALE_DAYS:
            continue

        old_confidence = subject.get("confidence", 0.5)
        new_confidence, decay_entry = decay_confidence_towards_neutral(old_confidence, today)
        updated_history = (history + [decay_entry])[-_HISTORY_CAP:]

        try:
            await qdrant.store(
                collection="semantic",
                content=f"Subject: {subject_id}\nThesis: {subject.get('thesis', '')}",
                metadata={
                    "type": "semantic", "domain": "subject",
                    "_key": f"semantic:subject:{subject_id}",
                    "subject": subject_id, "status": "active",
                    "confidence": new_confidence,
                    "confidence_history": updated_history,
                    "confidence_target": get_confidence_target(subject),
                    "thesis": subject.get("thesis", ""),
                    "original_thesis": subject.get("original_thesis", subject.get("thesis", "")),
                    "open_questions": subject.get("open_questions", []),
                    "knowns": subject.get("knowns", []),
                    "note_id": subject.get("note_id"),
                    "last_thought": subject.get("last_thought"),
                    **_subject_stub_fields(subject),
                },
            )
            decayed.append(subject_id)
        except Exception as exc:
            logger.warning("decay_stale_subjects: write failed for %r: %s", subject_id, exc)
            continue

        if new_confidence < _DECAY_ALERT_THRESHOLD:
            alerted.append(subject_id)
            await _notify_telegram(
                f"⚠️ *{subject_id}* confidence decayed to {new_confidence:.0%} "
                f"(no Thought activity in {days_stale} days, was {old_confidence:.0%})."
            )

    return {"decayed": decayed, "alerted": alerted, "drifted": drifted}


async def resync_subject_from_note(qdrant, nanobot, cog, subject_id: str) -> dict:
    """Reconcile a Subject's Nextcloud note back into the canonical Qdrant record.

    Manual edits to a Subject note (the Director pasting in outside analysis —
    e.g. an old Grok conversation) never reach Qdrant on their own: get_subject()
    only ever reads Qdrant, and resolve_thought_outcome() overwrites the note
    FROM Qdrant on every auto-applied Thought. Without this, hand-edited content
    is functionally inert and gets silently discarded on the next apply. This is
    the supported fix: read the note, distill it against the subject's current
    thesis/open_questions/knowns (an LLM merge, not a raw overwrite — pasted
    content is typically dense and multi-topic), upsert the reconciled result
    into Qdrant, then rewrite the note back to the clean distilled version so it
    stops drifting from what Rex actually knows.

    Lightweight — does not touch confidence/confidence_history (reserved for
    actual thought-driven research) and does not spawn a thought. No HITL gate:
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
    last_thought = subject.get("last_thought")

    # thesis_edit_history (schema v1.3, 2026-07-04) — an audit entry only when the
    # thesis text actually changed; this sidesteps deciding whether an edit is a
    # "good" evolution, it just records that one happened, when, and by whom.
    thesis_edit_history = list(subject.get("thesis_edit_history") or [])
    if new_thesis != current_thesis:
        thesis_edit_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "editor": "director",
            "summary": "resync_subject_from_note: thesis text changed via Note edit",
            "approved": True,
        })

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
                # original_thesis is deliberately NOT set to new_thesis here — this is the
                # one path where thesis is expected to change (Director hand-edited the
                # Note), and original_thesis exists specifically to survive that unchanged.
                "original_thesis": subject.get("original_thesis", current_thesis),
                "open_questions": new_open_questions,
                "knowns": new_knowns,
                "note_id": note_id,
                "last_thought": last_thought,
                **_subject_stub_fields(subject),
                "thesis_edit_history": thesis_edit_history,
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
        f"last_thought: {last_thought or 'null'}\n"
        f"confidence_history: {json.dumps(confidence_history)}\n"
        f"confidence_target: {confidence_target}\n"
        f"evidence_ratio: {subject.get('evidence_ratio') if subject.get('evidence_ratio') is not None else 'null'}\n"
        f"thesis_components: {json.dumps(subject.get('thesis_components'))}\n"
        "---\n\n"
        f"{_MANUAL_EDIT_NOTE.format(subject_id=subject_id)}\n\n"
        f"## Thesis\n{new_thesis}\n\n"
        f"## Open Questions\n{oq_lines}\n\n"
        f"## Knowns\n{kn_lines}\n\n"
        "[Narrative updates post-thought go here.]\n"
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

# Fan-out cap (Director, 2026-07-07): a single trigger event (one email, one
# chat message, one /learn document) can match several Subjects at once via
# find_relevant_subjects(). Each match spawns its own observe_for_subject()
# call and, if a gap is found, its own concurrent background Thought — so an
# uncapped fan-out means one broadly-relevant event can pile several Thoughts
# onto the GPU/inference queue simultaneously. Callers that fan out over
# find_relevant_subjects() hits to spawn observations should slice to this
# many highest-scoring matches (hits are already ordered by score descending).
_MAX_SUBJECT_MATCHES = 2


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
        # Visibility fix (Director, 2026-07-05): "otherwise I'd say Rex can't/didn't
        # see macro_inflation" — a Subject that never clears this threshold leaves
        # zero trace anywhere, indistinguishable from one that matched and was then
        # judged not worth a gap-check. Log every triage call's actual hit list
        # (subject + score) so a "why didn't X match" question can be answered from
        # logs instead of guessed at.
        logger.info(
            "find_relevant_subjects: %d hit(s) — %s",
            len(hits),
            ", ".join(f"{h.get('subject','?')}={h['_triage_score']:.3f}" for h in hits) or "(none)",
        )
        return hits
    except Exception as exc:
        logger.warning("find_relevant_subjects: failed (non-fatal): %s", exc)
        return []


def derive_impact(hits: list) -> tuple[str, float]:
    """ITIL-style Impact — how broadly significant is this item to what's
    currently being tracked, independent of how time-sensitive it is (see
    derive_urgency() for the Urgency axis; Impact x Urgency = Priority in the
    ITIL model, but this codebase keeps the two axes separate rather than
    summing them — see both docstrings). First-class Cognition Engine
    primitive (generalized 2026-07-03, was email-scoped `derive_priority`) —
    works for any input source triaged via find_relevant_subjects(): email,
    RSS, web search, system/GPU/validator events, portfolio alerts. `hits` is
    always a find_relevant_subjects() result list, whatever the source.

    A free byproduct of find_relevant_subjects(), not a separate
    classification pass. Subjects represent what the Director is actively
    tracking, so content that lands on several at once is more broadly
    significant than content that grazes one, which in turn matters more than
    content that matches none.

    Deliberately a DIFFERENT metric from a Subject's `confidence` — confidence
    is epistemic (how sure Rex is a thesis/fact is true, built up over
    thoughts); impact is relevance-breadth (how much THIS item matters to
    what's being tracked, a one-shot triage-time signal). They happen to
    share the same three-tier vocabulary and 0.25/0.5/0.75 numeric scale (via
    label_to_weight(), reused rather than duplicated) purely for
    consistency — never write an impact score into a Subject's own
    `confidence` field, they answer different questions.

    0 hits -> ("low", 0.25), 1 hit -> ("medium", 0.5), 2+ hits -> ("high", 0.75).
    Deliberately crude — this is a relevance-breadth signal, not an urgency
    detector (a single burning-platform alert is still "medium" by this
    measure; the two are different questions, see derive_urgency()).
    """
    n = len(hits)
    label = "low" if n == 0 else "medium" if n == 1 else "high"
    return label, label_to_weight(label.upper())


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


_EMAIL_ADDR_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def _normalise_flagged_sender(sender: str) -> str:
    """Extract just the email address from a 'Display Name <addr>' sender
    string, lowercased — the dedup key for record_spam_sender/record_urgent_
    sender. Falls back to the raw sender string if no address-shaped
    substring is found."""
    m = _EMAIL_ADDR_RE.search(sender or "")
    return (m.group(0) if m else (sender or "")).strip().lower()


async def _record_flagged_sender(qdrant, sender: str, subject_line: str,
                                  domain: str, reason: str) -> dict:
    """Shared dedup-by-sender memory write for record_spam_sender() and
    record_urgent_sender() (Director, 2026-07-07 — "wrap some learning and
    memories around spam" / "urgent and phishing should be creating their
    own memories as well"). Before this, a flagged email only ever showed up
    once in a Telegram digest and left no trace — repeat senders now
    accumulate a count + recent subject lines instead of re-flagging cold
    every time. Never raises — a failed memory write shouldn't break the
    scan that found it.
    """
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue

    key_sender = _normalise_flagged_sender(sender)
    if not key_sender:
        return {"status": "error", "error": "empty sender"}
    _key = f"semantic:{domain}:{key_sender}"
    today = date.today().isoformat()

    try:
        existing, _ = await qdrant.archive_client.scroll(
            collection_name="semantic",
            scroll_filter=Filter(must=[FieldCondition(key="_key", match=MatchValue(value=_key))]),
            limit=1, with_payload=True, with_vectors=False,
        )
    except Exception as exc:
        logger.warning("_record_flagged_sender: lookup failed for %r (%s): %s", key_sender, domain, exc)
        existing = []

    if existing:
        point = existing[0]
        payload = point.payload or {}
        count = int(payload.get("count", 0)) + 1
        recent = payload.get("recent_subjects") or []
        if subject_line and subject_line not in recent:
            recent = ([subject_line] + recent)[:10]
        try:
            await qdrant.archive_client.set_payload(
                collection_name="semantic",
                payload={
                    "count": count, "recent_subjects": recent, "last_seen": today,
                    "last_reason": reason, "last_updated": datetime.now(timezone.utc).isoformat(),
                },
                points=[point.id],
            )
        except Exception as exc:
            logger.warning("_record_flagged_sender: update failed for %r (%s): %s", key_sender, domain, exc)
        return {"status": "ok", "sender": key_sender, "count": count, "new": False}

    try:
        await qdrant.store(
            collection="semantic",
            content=f"{domain.replace('_', ' ').title()}: {sender}\nReason: {reason}\nSubject: {subject_line}",
            metadata={
                "type": "semantic", "domain": domain,
                "_key": _key, "sender": key_sender, "raw_sender": sender,
                "count": 1, "recent_subjects": [subject_line] if subject_line else [],
                "first_seen": today, "last_seen": today, "last_reason": reason,
            },
        )
    except Exception as exc:
        logger.warning("_record_flagged_sender: create failed for %r (%s): %s", key_sender, domain, exc)
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "sender": key_sender, "count": 1, "new": True}


async def record_spam_sender(qdrant, sender: str, subject_line: str, reason: str = "brand_mismatch") -> dict:
    """Persist a phishing/brand-mismatch-flagged sender — the running "spam
    list." See _record_flagged_sender() for the shared dedup mechanics."""
    return await _record_flagged_sender(qdrant, sender, subject_line, "spam_sender", reason)


async def record_urgent_sender(qdrant, sender: str, subject_line: str, reason: str) -> dict:
    """Persist a sender whose mail scored 'high' urgency — builds a picture
    of who actually sends time-sensitive mail over time (a candidate signal
    for later tightening priority_senders), distinct from the spam list.
    See _record_flagged_sender() for the shared dedup mechanics."""
    return await _record_flagged_sender(qdrant, sender, subject_line, "urgent_sender", reason)


def derive_urgency(
    text: str, source: str = "", priority_senders: list[str] | None = None,
    phishing_flagged: bool = False,
) -> tuple[str, float]:
    """ITIL-style Urgency — does this need attention now, independent of how
    broadly significant it is (see derive_impact() for the Impact side). A
    monitor-down alert is high urgency and near-certainly low impact (nothing
    to learn) — the two axes are meant to diverge, not agree, that's the
    point of tracking them separately.

    First-class Cognition Engine primitive (generalized 2026-07-03, was
    email-scoped) — `text` is whatever the item's headline-equivalent is
    (email subject line, RSS/web-search title, system event description,
    portfolio alert summary) and `source` is whatever names where it came
    from (email sender, RSS feed name, system component, adapter name). Any
    input source can be scored through this one function rather than each
    building its own urgency pass — see CLAUDE.md standing order #2/#3.

    Caveat for non-email sources: `priority_senders` (a Director-maintained
    email-address allowlist) and the alerting-sender regex
    (`_URGENCY_SENDER_PATTERN_RE`, which specifically looks for an "@") will
    simply never match a non-email `source` — that's fine, not a bug; those
    two signals fall out and the keyword-only signal (`_URGENCY_KEYWORDS_RE`
    against `text`) still applies. A future system-event caller that wants an
    equivalent "trusted source" allowlist should get its own config list
    (e.g. `cognition.priority_system_sources`) passed in via a differently-
    named param, not overload `priority_senders` with non-email values.

    phishing_flagged (caller-computed — see detect_brand_mismatch(), and
    future body/URL-based signals; email-specific, callers scoring non-email
    sources simply never pass it) DOWNGRADES rather than adds, and takes
    precedence over everything else, including a priority-sender match: a
    phishing email borrowing urgent language should read as LESS urgent, not
    more, since prompting quick action is exactly the attacker's goal.
    Otherwise: a source on the Director-maintained priority list (see
    get_priority_senders()) is "high", full stop. Otherwise: keyword hit in
    `text` AND an alerting-style source pattern -> "high". Either alone ->
    "medium". Neither -> "low". Same 0.25/0.5/0.75 numeric scale as impact
    for consistency (see derive_impact()'s docstring on why that's shared
    vocabulary, not a shared meaning).

    priority_senders defaults to get_priority_senders() (config-backed) when
    not passed explicitly — callers scoring many items in one run should
    fetch it once and pass it through rather than re-reading per item.

    Does not do date-comparison (a "deadline" mentioning a specific date
    doesn't get checked against today) — keyword-only for now, flagged as a
    known gap rather than half-built.
    """
    if phishing_flagged:
        return "low", label_to_weight("LOW")

    if priority_senders is None:
        priority_senders = get_priority_senders()
    if priority_senders:
        source_lower = (source or "").lower()
        if any(ps.lower() in source_lower for ps in priority_senders):
            return "high", label_to_weight("HIGH")

    keyword_hit = bool(_URGENCY_KEYWORDS_RE.search(text or ""))
    source_hit = bool(_URGENCY_SENDER_PATTERN_RE.search(source or ""))
    if keyword_hit and source_hit:
        label = "high"
    elif keyword_hit or source_hit:
        label = "medium"
    else:
        label = "low"
    return label, label_to_weight(label.upper())


async def score_and_fold_subjects(
    qdrant, cog, text: str, source_label: str, subjects: list[dict] | None = None,
) -> list[dict]:
    """Score arbitrary content (a /learn source — inline text, a fetched URL, or
    an email body) against active Subjects and fold any genuinely new fact
    straight into knowns. Lightweight only: no thought spawn, no confidence
    change, no HITL gate — the Director explicitly fed this content to /learn,
    so a second approval step would be redundant.

    Triaged (see find_relevant_subjects) — only Subjects that survive the
    cheap embedding pre-filter get the expensive full-read LLM call. Not
    every Subject, every time. Pass a pre-computed `subjects` (e.g. from a
    triage pass the caller already ran, to derive_impact() from the same
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
                    "original_thesis": subject.get("original_thesis", thesis),
                    "open_questions": subject.get("open_questions", []),
                    "knowns": current_knowns + new_knowns,
                    "note_id": subject.get("note_id"),
                    "last_thought": subject.get("last_thought"),
                    **_subject_stub_fields(subject),
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
    thesis_components: dict | None = None,
    search_keywords: list[str] | None = None,
    succeeds: str | None = None,
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

    search_keywords: optional short list of literal search terms for this
    Subject (e.g. ["Bitcoin", "Ethereum", "cryptocurrency"]) — deliberately
    separate from `thesis` prose, which isn't reliable to extract keywords
    from algorithmically without an extra LLM call. Consumed by
    get_subject_news_keywords() (2026-07-04) to make news search
    Subject-bound — high-level Subjects only (Matt's call: `{parent}_{focus}`
    sub-focus Subjects are too narrow for general news search and would
    just multiply query volume).

    Thesis form (Director, 2026-07-04): a well-formed thesis names the claim,
    the mechanism (why it should be true), the time horizon (over what period
    it should resolve — without this a thesis can never be wrong), and why it
    materially matters. `open_questions` is rendered as "## Assumptions" in
    the Note — Director's call: an open question and an assumption are the
    same underlying thing (an unresolved, low-confidence, load-bearing premise
    the thesis depends on), just phrased as a question vs. a claim; the param
    name stays `open_questions` internally since that's what the gate logic
    (evaluate_thought_iteration, assess_thought_quality, resolve_thought_outcome)
    already reads everywhere — renaming the field itself is a separate, wider
    change not made here. The template also carries a permanent "## Issues"
    section — this is a stub, not a working mechanism: nothing in code
    reviews/rewords a thesis or promotes assumptions on a schedule. See the
    Issues text itself for what's still unresolved.

    Popperian framing (2026-07-06, see module docstring): open_questions
    exist to be refuted, not proven — each one should be an operationalized
    falsification_condition from `thesis_components`, phrased as a closed
    yes/no test, not a general "things to look into" list. This isn't
    enforced here (create_subject() stays deterministic, no LLM call) —
    `propose_successor_thesis()` is the one concrete path that builds
    open_questions this way today; a Director hand-authoring a Subject
    should follow the same principle, just not code-checked yet (Phase 2
    validation, not started, per the Issues stub).

    succeeds: lineage — the subject_id this one replaces, when created via
    the corroborated/refuted successor-thesis flow. None for a first-
    generation Subject. Frozen at creation, never recomputed (same pattern
    as original_thesis).
    """
    open_questions = open_questions or []
    knowns = knowns or []
    search_keywords = search_keywords or []
    target = confidence_target if confidence_target is not None else _DEFAULT_CONFIDENCE_TARGET
    today = date.today().isoformat()

    oq_lines = "\n".join(f"- {q}" for q in open_questions) or "(none)"
    kn_lines = "\n".join(f"- {k}" for k in knowns) or "(none)"
    issues_text = (
        "1. Confidence rollup undefined — if a thesis is ever decomposed into separately-"
        "confident sub-claims, there's no rule yet for combining them into the one scalar "
        "confidence the rest of the system (apply_confidence_delta, calendar decay, "
        "notification gate) depends on.\n"
        "2. RESOLVED 2026-07-06 — confidence now moves via a bounded, stance-weighted step "
        "per Thought (apply_confidence_delta) rather than averaging per-Thought source-"
        "richness labels; evidence_ratio (knowns-vs-open-questions) is tracked separately as "
        "a distinct, non-competing view. See cognition/subjects.py module docstring.\n"
        "3. Thesis-mutability paradox — if thesis text is ever edited to absorb confirmed "
        "facts, confidence risks becoming tautological (a thesis rewritten to match the "
        "evidence will trivially score high confidence in what it now says). original_thesis "
        "preserves an anchor against this, but no editing trigger, process, or safeguard "
        "against drift is defined.\n"
        "4. Per-question stance tagging (supports/contradicts) on newly-promoted knowns is "
        "still just a text marker — the OVERALL Thought stance (thesis_stance/stance_strength) "
        "now drives the confidence delta, but the per-question tag on an individual closed "
        "question doesn't independently weight anything.\n"
        "5. No retrofit — original_thesis and stance tagging only apply going forward; "
        "pre-existing knowns/thesis history isn't backfilled with either.\n"
        "6. Attribution problem — any future per-sub-claim confidence tracking requires "
        "correctly attributing each Thought's finding to the right sub-claim, not just the "
        "Subject as a whole; that's a classification problem, not a schema one.\n"
        "7. Knowns-list unbounded growth — nothing consolidates or prunes overlapping/"
        "superseded facts as they accumulate over months.\n"
        "8. Review cadence undecided — daily piggyback on the decay check, a fixed calendar, "
        "or manual-only each trade cost against thesis freshness differently.\n"
    )
    succeeds_line = f"succeeds: {succeeds}\n" if succeeds else ""
    note_content = (
        "---\n"
        "type: subject\n"
        f"subject: {subject_id}\n"
        "status: active\n"
        "epistemic_status: investigating\n"
        "confidence: 0.50\n"
        f"last_updated: {today}\n"
        "last_thought: null\n"
        "confidence_history: []\n"
        f"confidence_target: {target}\n"
        "evidence_ratio: null\n"
        'evidence_weight: {"supports": 0, "contradicts": 0, "neutral": 0}\n'
        f"{succeeds_line}"
        f"thesis_components: {json.dumps(thesis_components)}\n"
        "---\n\n"
        f"{_MANUAL_EDIT_NOTE.format(subject_id=subject_id)}\n\n"
        f"## Thesis\n{thesis}\n\n"
        f"## Assumptions\n{oq_lines}\n\n"
        f"## Knowns\n{kn_lines}\n\n"
        "## Issues (open design questions, unresolved as of 2026-07-04 — not a working "
        "mechanism, see create_subject() docstring in cognition/subjects.py)\n"
        f"{issues_text}\n"
        "[Narrative updates post-thought go here.]\n"
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
                "epistemic_status": "investigating",
                "confidence": 0.5,
                "confidence_history": [],
                "confidence_target": target,
                "evidence_weight": {"supports": 0, "contradicts": 0, "neutral": 0},
                "thesis": thesis,
                # original_thesis (stub, Director 2026-07-04): frozen at creation, never
                # touched again by any write site below — an anchor for what was actually
                # being tested, independent of however "thesis" itself may later evolve.
                "original_thesis": thesis,
                "open_questions": open_questions,
                "knowns": knowns,
                "search_keywords": search_keywords,
                "note_id": str(note_id) if note_id else None,
                "last_thought": None,
                **_subject_stub_fields({}),
                "succeeds": succeeds,  # overrides _subject_stub_fields({})'s None — frozen at creation
                "thesis_components": thesis_components,
                # original_thesis_components frozen alongside original_thesis — absent
                # (None) if the Director didn't supply components at creation; Phase 2's
                # backfill is what fills these in for Subjects created without them.
                "original_thesis_components": thesis_components,
            },
        )
    except Exception as exc:
        logger.warning("create_subject: semantic write failed for %r: %s", subject_id, exc)
        return {"status": "error", "error": f"qdrant write failed: {exc}", "note_id": note_id}

    return {"status": "ok", "subject_id": subject_id, "note_id": note_id}
