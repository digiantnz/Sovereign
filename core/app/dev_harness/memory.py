"""
Dev-Harness memory integration — Step 9.

Three write functions called from harness.py at phase boundaries:
  write_episodic_analysis  — after Phase 1 completes (always, fire-and-forget)
  update_meta_state        — after Phase 3 completes (gate final, plan visible to Director)
  write_semantic_fix       — after verify completes WITH gate==APPROVE only (verify_passed=True)

All three are invoked via asyncio.create_task() — never block the cognitive loop.

Four explicit constraints (per Director brief):
  1. Recurrence tracking: recurrence_counts derived from gate_history tail (last 5),
     NOT from a separate counter. The rolling history IS the source of truth.
  2. Procedural write gate: no direct PROCEDURAL writes. If BLOCK/ESCALATE count
     >= 3 in last 5 sessions, write a Type B PROSPECTIVE entry for Director review.
     Sovereign never self-modifies procedural memory from harness findings.
  3. Confidence score: recency-weighted from actual gate_history — not hardcoded.
     Linear weight: oldest entry weight=0, most recent weight=1. Gate scores:
     approve=1.0, revise=0.6, block=0.2, escalate=0.0.
  4. Semantic writes: ONLY on verify_passed=True (Phase 4 re-run gate==APPROVE).
     Phase 3 Director approval alone does NOT trigger a semantic write.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.adapters.qdrant import QdrantAdapter

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_META_KEY          = "meta:dev-harness:state"  # MIP key: type:domain:slug
_GATE_HISTORY_MAX  = 20   # rolling window kept in meta entry
_RECURRENCE_WINDOW = 5    # tail length used to derive recurrence_counts
_RECURRENCE_THRESH = 3    # BLOCK+ESCALATE count in window → Type B prospective alert

# Confidence score per gate outcome (constraint 3)
_GATE_SCORES: dict[str, float] = {
    "approve":  1.0,
    "revise":   0.6,
    "block":    0.2,
    "escalate": 0.0,
}


# ── Public API ─────────────────────────────────────────────────────────────────

async def write_episodic_analysis(
    qdrant: "QdrantAdapter",
    session_id_short: str,
    trigger: str,
    gate_decision: str,
    total_score: int,
    finding_count: int,
    severity_breakdown: dict,
    tool_errors: list,
) -> None:
    """Write a timestamped episodic record for this Phase 1 run.

    Called from run_phase1 after checkpoint save, via asyncio.create_task().
    Never raises — episodic write failure must not block Phase 1 return.
    """
    if not qdrant:
        return
    try:
        from execution.adapters.qdrant import EPISODIC
        now = datetime.now(timezone.utc).isoformat()
        sev = severity_breakdown or {}
        content = (
            f"Dev-Harness analysis run {session_id_short}: "
            f"gate={gate_decision} score={total_score} "
            f"findings={finding_count} trigger={trigger}"
        )
        await qdrant.store(
            content=content,
            metadata={
                "type":               "dev_analysis_run",
                "domain":             "dev_harness",
                "session_id_short":   session_id_short,
                "trigger":            trigger,
                "gate_decision":      gate_decision,
                "total_score":        total_score,
                "finding_count":      finding_count,
                "severity_breakdown": sev,
                "tool_errors":        tool_errors or [],
                "timestamp":          now,
            },
            collection=EPISODIC,
        )
        logger.info(
            "DevHarness memory: episodic written — session=%s gate=%s",
            session_id_short, gate_decision,
        )
    except Exception as exc:
        logger.warning("DevHarness memory: write_episodic_analysis failed: %s", exc)


async def update_meta_state(
    qdrant: "QdrantAdapter",
    session_id_short: str,
    gate_decision: str,
    total_score: int,
) -> None:
    """Atomic read-modify-write of the meta:dev-harness:state entry.

    Reads the existing entry (if any) via retrieve_by_key(), modifies in memory,
    then either patches it via archive_client.set_payload() (no re-embedding) or
    creates a new entry via store() on first use.

    Constraint 1: recurrence_counts derived exclusively from gate_history tail (last 5).
    Constraint 2: no PROCEDURAL write. Recurrence alert → PROSPECTIVE only.
    Constraint 3: confidence_score is recency-weighted from actual gate_history.
    """
    if not qdrant:
        return
    try:
        from execution.adapters.qdrant import META, PROSPECTIVE
        now  = datetime.now(timezone.utc).isoformat()
        gate = gate_decision.lower()

        # ── Read existing meta entry (exact-key lookup, no vector search) ──
        existing = await qdrant.retrieve_by_key(_META_KEY)

        if existing:
            point_id     = existing["point_id"]
            run_count    = int(existing.get("run_count", 0)) + 1
            gate_history: list[str] = list(existing.get("gate_history", []))
        else:
            point_id     = None
            run_count    = 1
            gate_history = []

        # ── Append gate; cap rolling window at _GATE_HISTORY_MAX ──────────
        gate_history.append(gate)
        if len(gate_history) > _GATE_HISTORY_MAX:
            gate_history = gate_history[-_GATE_HISTORY_MAX:]

        # ── Constraint 1: recurrence_counts from tail only ─────────────────
        tail = gate_history[-_RECURRENCE_WINDOW:]
        recurrence_counts: dict[str, int] = {}
        for g in tail:
            recurrence_counts[g] = recurrence_counts.get(g, 0) + 1

        # ── Constraint 3: recency-weighted confidence score ────────────────
        confidence_score = _weighted_confidence(gate_history)

        # ── Payload delta (all fields updated together — atomically) ───────
        patch = {
            "_key":              _META_KEY,   # re-stamp to be explicit; harmless no-op
            "run_count":         run_count,
            "gate_history":      gate_history,
            "recurrence_counts": recurrence_counts,
            "confidence_score":  confidence_score,
            "last_session":      session_id_short,
            "last_score":        total_score,
            "last_updated":      now,
        }

        if existing and point_id:
            # Field-level patch — no delete/re-insert, no re-embedding
            await qdrant.archive_client.set_payload(
                collection_name=META,
                payload=patch,
                points=[point_id],
            )
            logger.info(
                "DevHarness memory: meta state patched — run=%d confidence=%.2f gate=%s",
                run_count, confidence_score, gate,
            )
        else:
            # First write — create the entry (store() generates MIP key from _key in metadata)
            content = (
                f"Dev-Harness meta state: {run_count} analysis runs. "
                f"Last gate: {gate}. Confidence: {confidence_score:.2f}."
            )
            await qdrant.store(
                content=content,
                metadata={
                    "type":   "meta",
                    "domain": "dev-harness",
                    **patch,
                },
                collection=META,
            )
            logger.info(
                "DevHarness memory: meta state created — run=1 confidence=%.2f",
                confidence_score,
            )

        # ── Constraint 2: Type B PROSPECTIVE on recurrence (NO PROCEDURAL) ─
        bad_gates = {"block", "escalate"}
        bad_count = sum(recurrence_counts.get(g, 0) for g in bad_gates)
        if bad_count >= _RECURRENCE_THRESH:
            dominant = max(bad_gates, key=lambda g: recurrence_counts.get(g, 0))
            alert_content = (
                f"Dev-Harness recurrence alert: {bad_count}/{_RECURRENCE_WINDOW} "
                f"BLOCK/ESCALATE in last {_RECURRENCE_WINDOW} sessions. "
                f"Dominant: {dominant.upper()}. Confidence: {confidence_score:.2f}. "
                f"Director review recommended."
            )
            await qdrant.store(
                content=alert_content,
                metadata={
                    "type":                    "dev_recurrence_alert",
                    "domain":                  "dev_harness",
                    "_dev_recurrence_alert":   True,
                    "bad_count":               bad_count,
                    "dominant_gate":           dominant,
                    "recurrence_counts":       recurrence_counts,
                    "confidence_score":        confidence_score,
                    "status":                  "pending_director_approval",
                    "timestamp":               now,
                },
                collection=PROSPECTIVE,
            )
            logger.warning(
                "DevHarness memory: recurrence alert — %d/%d bad gates, dominant=%s",
                bad_count, _RECURRENCE_WINDOW, dominant,
            )

    except Exception as exc:
        logger.warning("DevHarness memory: update_meta_state failed: %s", exc)


async def write_semantic_fix(
    qdrant: "QdrantAdapter",
    session_id_short: str,
    original_gate: str,
    verify_gate: str,
    finding_count_before: int,
    finding_count_after: int,
    severity_breakdown_after: dict,
) -> None:
    """Write a semantic memory entry recording a confirmed fix.

    Constraint 4: called ONLY when verify_passed=True (verify Phase 1 gate==APPROVE).
    This is NOT called on Phase 3 Director approval — the verify re-run is
    the confirmation gate that a fix actually resolved the findings.
    Never raises.
    """
    if not qdrant:
        return
    try:
        from execution.adapters.qdrant import SEMANTIC
        now          = datetime.now(timezone.utc).isoformat()
        fixed_count  = max(0, finding_count_before - finding_count_after)
        content = (
            f"Dev-Harness fix confirmed — session {session_id_short}: "
            f"original gate {original_gate.upper()} → verify gate APPROVE. "
            f"{fixed_count} findings resolved ({finding_count_after} remaining)."
        )
        await qdrant.store(
            content=content,
            metadata={
                "type":                     "dev_fix_confirmed",
                "domain":                   "dev_harness",
                "session_id_short":         session_id_short,
                "original_gate":            original_gate,
                "verify_gate":              verify_gate,
                "finding_count_before":     finding_count_before,
                "finding_count_after":      finding_count_after,
                "findings_resolved":        fixed_count,
                "severity_breakdown_after": severity_breakdown_after or {},
                "timestamp":                now,
            },
            collection=SEMANTIC,
        )
        logger.info(
            "DevHarness memory: semantic fix written — session=%s resolved=%d remaining=%d",
            session_id_short, fixed_count, finding_count_after,
        )
    except Exception as exc:
        logger.warning("DevHarness memory: write_semantic_fix failed: %s", exc)


# ── Internal ───────────────────────────────────────────────────────────────────

def _weighted_confidence(gate_history: list[str]) -> float:
    """Recency-weighted mean of gate scores.

    Linear weight: index 0 (oldest) → 0.0, last entry → 1.0.
    Single-entry history → weight 1.0.
    Returns float in [0.0, 1.0], rounded to 4 decimal places.
    Unknown gate values default to 0.5 (neutral).
    """
    n = len(gate_history)
    if n == 0:
        return 0.5  # neutral default — no history
    if n == 1:
        return float(_GATE_SCORES.get(gate_history[0].lower(), 0.5))

    total_weight  = 0.0
    weighted_sum  = 0.0
    for i, g in enumerate(gate_history):
        w             = i / (n - 1)          # 0.0 for oldest, 1.0 for most recent
        score         = _GATE_SCORES.get(g.lower(), 0.5)
        weighted_sum += w * score
        total_weight += w

    return round(weighted_sum / total_weight, 4) if total_weight > 0.0 else 0.5
