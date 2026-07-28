"""Rex-trics — deterministic hallucination + performance flag counters, plus
Phase 1 per-interaction retention logging (Rex-trics Log).

Three checks/logs live at their natural existing chokepoints, not here:
  - execution_mismatch  → cognition/engine.py orchestrator_evaluate() (PASS 4)
                            "Rex-trics Check 1"
  - unverified_claim     → cognition/engine.py translator_pass() (PASS 5 — the one
                            function every path, including all short-circuits, already
                            funnels through) "Rex-trics Check 2"
  - Rex-trics Log         → same translator_pass() chokepoint as Check 2 — one
                            per-interaction record (ts/intent/domain/outcome/reason/
                            latency_ms) per call, zero detection logic of its own.

This module only owns the counter/log plumbing + the weekly report reader. No new
Qdrant collection — storage lives in META, split across two sub-keys per date:
  meta:rextrics:daily:{date}:counters — aggregate flag counters (Check 1/2 + response
                                          count), unchanged shape from before this phase
  meta:rextrics:daily:{date}:entries  — Rex-trics Log: appended list of per-interaction
                                          records, added this phase
Same deterministic-cursor/read-modify-write-upsert pattern used by
run_structural_loop's meta:memory-synthesis:structural-cursor — including the same
accepted lost-increment/lost-append risk (no atomic primitive on this Qdrant client).
"""
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Same namespace UUID already used for other meta: cursor/counter points.
_NAMESPACE = uuid.UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d")
_ZERO_VEC = [0.0] * 768
_META = "meta"

_COUNTER_FIELDS = ("total_responses", "execution_mismatches", "unverified_claims_stripped")

# routing_source values (set by execution/engine.py's PASS 1 dispatch) that mean
# "no deterministic fast path fired — classification fell back to the LLM,
# cleanly (llm_pass1) or via the unrecognised-intent safety net (fallback_map)".
# Both bucket as outcome="fallback" — orthogonal to whether execution then succeeded.
_FALLBACK_ROUTING_SOURCES = frozenset({"llm_pass1", "fallback_map"})


def _counters_key(date_str: str) -> str:
    return f"meta:rextrics:daily:{date_str}:counters"


def _entries_key(date_str: str) -> str:
    return f"meta:rextrics:daily:{date_str}:entries"


_LAST_REPORT_KEY = "meta:rextrics:last_report"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _increment(qdrant, field: str) -> None:
    """Fire-and-forget read-modify-write increment of one counter field for today.

    Same accepted-risk pattern as other fire-and-forget counters in this codebase
    (e.g. spam/urgent sender counts) — no atomic increment primitive on this
    Qdrant client, and contention is rare enough (single-Director system) that a
    lost increment is an acceptable tradeoff against added complexity.
    """
    if not qdrant:
        return
    date_str = _today()
    key = _counters_key(date_str)
    try:
        from qdrant_client.models import PointStruct
        existing = await qdrant.retrieve_by_key(key) or {}
        counts = {f: existing.get(f, 0) for f in _COUNTER_FIELDS}
        counts[field] = counts.get(field, 0) + 1
        point_id = str(uuid.uuid5(_NAMESPACE, key))
        now = datetime.now(timezone.utc).isoformat()
        await qdrant.archive_client.upsert(
            collection_name=_META,
            points=[PointStruct(
                id=point_id,
                vector=_ZERO_VEC,
                payload={"_key": key, "date": date_str, **counts, "last_updated": now},
            )],
        )
    except Exception as exc:
        logger.warning("rextrics: counter increment failed (field=%s): %s", field, exc)


async def record_response(qdrant) -> None:
    """Called once per translator_pass() invocation — every Director-facing response."""
    await _increment(qdrant, "total_responses")


async def record_flag(qdrant, ledger, category: str, claim_text: str = "") -> None:
    """Increment the flag counter + write a hash-only episodic entry.

    category: 'execution_mismatch' or 'unverified_claim'.
    claim_text is hashed, never stored raw — consistent with InternalMessage's
    hash-not-raw pattern for content that may carry DCL-sensitive material.
    """
    field = "execution_mismatches" if category == "execution_mismatch" else "unverified_claims_stripped"
    await _increment(qdrant, field)
    if not qdrant:
        return
    from execution.adapters.qdrant import EPISODIC
    ts = datetime.now(timezone.utc).isoformat()
    claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()[:16] if claim_text else ""
    try:
        await qdrant.store(
            content=f"Rex-trics flag [{category}] at {ts}.",
            metadata={
                "type": "rextrics_flag",
                "hallucination_flag": category,
                "category": category,
                "claim_hash": claim_hash,
                "verified": False,
                "ts": ts,
                "pass": "PASS4",
                "_key": f"episodic:rextrics:{category}:{ts}",
            },
            collection=EPISODIC,
            writer="sovereign-core",
        )
    except Exception as exc:
        logger.warning("rextrics: episodic log failed: %s", exc)


def _normalize_outcome(result: dict) -> tuple[str, str | None]:
    """Deterministic success/fail classification from a result_for_translator-shaped dict.

    Checks both return conventions used across _dispatch_inner's ~36 domains — the
    'status' idiom (ok/error) and the 'success' idiom (True/False); see
    core/app/CLAUDE.md's _dispatch_inner note on the two coexisting shapes. Anything
    matching neither convention defaults to fail/unnormalized_response rather than
    guessing — a call site whose result carries neither key is itself a finding
    worth surfacing in the fail-reason report, not something to paper over.

    fallback is NOT decided here — it's a routing property (did classification reach
    a deterministic fast path or fall back to the LLM), orthogonal to whether
    execution then succeeded. See _FALLBACK_ROUTING_SOURCES / record_entry().
    """
    if not isinstance(result, dict):
        return "fail", "unnormalized_response"
    if "status" in result:
        if result["status"] == "ok":
            return "success", None
        if result["status"] == "error":
            reason = result.get("error") or result.get("outcome") or result.get("message") or "unknown_error"
            return "fail", str(reason)[:300]
        return "fail", "unnormalized_response"
    if "success" in result:
        if result["success"] is True:
            return "success", None
        if result["success"] is False:
            reason = result.get("error") or result.get("outcome") or result.get("message") or "unknown_error"
            return "fail", str(reason)[:300]
        return "fail", "unnormalized_response"
    return "fail", "unnormalized_response"


async def record_entry(
    qdrant,
    intent_classified: str | None,
    domain_routed: str | None,
    routing_source: str | None,
    result: dict,
    latency_ms: float | None,
) -> None:
    """Rex-trics Log — append one per-interaction record to today's :entries list.

    Fired unconditionally from translator_pass() — the same chokepoint
    record_response() already uses, so every path (including all short-circuits)
    is covered with zero new call sites to maintain. Read-modify-write upsert,
    same accepted-risk pattern as _increment() above.
    """
    if not qdrant:
        return
    if routing_source in _FALLBACK_ROUTING_SOURCES:
        outcome, reason = "fallback", None
    else:
        outcome, reason = _normalize_outcome(result)
    date_str = _today()
    key = _entries_key(date_str)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "intent_classified": intent_classified,
        "domain_routed": domain_routed,
        "outcome": outcome,
        "reason": reason,
        "latency_ms": latency_ms,
    }
    try:
        from qdrant_client.models import PointStruct
        existing = await qdrant.retrieve_by_key(key) or {}
        entries = list(existing.get("entries", []))
        entries.append(entry)
        point_id = str(uuid.uuid5(_NAMESPACE, key))
        now = datetime.now(timezone.utc).isoformat()
        await qdrant.archive_client.upsert(
            collection_name=_META,
            points=[PointStruct(
                id=point_id,
                vector=_ZERO_VEC,
                payload={"_key": key, "date": date_str, "entries": entries, "last_updated": now},
            )],
        )
    except Exception as exc:
        logger.warning("rextrics: entry log failed: %s", exc)


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


async def get_weekly_report(qdrant) -> dict:
    """Read the last 14 daily counters + 7 days of Rex-trics Log entries.

    Counters: split into this-week vs prior-week, summed by category — unchanged
    from before this phase. Log: read-time-only aggregation over the last 7 days'
    :entries lists (outcome rates, active days, latency percentiles, top fail
    reasons) — nothing pre-aggregated or stored, computed fresh on every read.
    Merged into one combined report so /rextrics stays a single command.
    """
    if not qdrant:
        return {"error": "qdrant unavailable"}
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(14)]
    daily = []
    for d in days:
        entry = await qdrant.retrieve_by_key(_counters_key(d)) or {}
        daily.append({"date": d, **{f: entry.get(f, 0) for f in _COUNTER_FIELDS}})

    this_week, prior_week = daily[:7], daily[7:14]

    def _totals(rows: list[dict]) -> dict:
        return {f: sum(r[f] for r in rows) for f in _COUNTER_FIELDS}

    tw, pw = _totals(this_week), _totals(prior_week)

    # ── Rex-trics Log stats — last 7 days of :entries ─────────────────────────
    all_entries: list[dict] = []
    active_days = 0
    for d in [row["date"] for row in this_week]:
        day_record = await qdrant.retrieve_by_key(_entries_key(d)) or {}
        day_entries = day_record.get("entries", [])
        if day_entries:
            active_days += 1
        all_entries.extend(day_entries)

    total = len(all_entries)
    outcome_counts = {"success": 0, "fail": 0, "fallback": 0}
    fail_reasons: dict[str, int] = {}
    latencies: list[float] = []
    for e in all_entries:
        oc = e.get("outcome")
        if oc in outcome_counts:
            outcome_counts[oc] += 1
        if oc == "fail" and e.get("reason"):
            fail_reasons[e["reason"]] = fail_reasons.get(e["reason"], 0) + 1
        if isinstance(e.get("latency_ms"), (int, float)):
            latencies.append(e["latency_ms"])
    latencies.sort()
    top_fail_reasons = sorted(fail_reasons.items(), key=lambda kv: kv[1], reverse=True)[:3]

    log_stats = {
        "total_interactions": total,
        "success_rate":   round(outcome_counts["success"]  / total, 3) if total else None,
        "fail_rate":      round(outcome_counts["fail"]     / total, 3) if total else None,
        "fallback_rate":  round(outcome_counts["fallback"] / total, 3) if total else None,
        "active_days": active_days,
        "active_days_out_of": 7,
        "median_latency_ms": _percentile(latencies, 0.5),
        "p90_latency_ms":    _percentile(latencies, 0.9),
        "top_fail_reasons": [{"reason": r, "count": c} for r, c in top_fail_reasons],
    }

    return {
        "this_week": tw,
        "prior_week": pw,
        "execution_mismatches_trend": tw["execution_mismatches"] - pw["execution_mismatches"],
        "unverified_claims_stripped_trend": tw["unverified_claims_stripped"] - pw["unverified_claims_stripped"],
        "daily": this_week,
        "log": log_stats,
    }


async def write_last_report_cache(qdrant, report: dict) -> None:
    """Cache the latest /rextrics report for the dashboard's static summary panel.

    Director-triggered only (called from the `/rextrics` dispatch path) — the
    dashboard reads this key on its normal refresh cycle but the underlying
    data only changes when the Director next asks Rex-trics a question.
    Overwrite-only, no read-modify-write: this is a single latest-snapshot
    record, not an accumulating list like the counters/entries keys above.
    """
    if not qdrant:
        return
    try:
        from qdrant_client.models import PointStruct
        point_id = str(uuid.uuid5(_NAMESPACE, _LAST_REPORT_KEY))
        now = datetime.now(timezone.utc).isoformat()
        await qdrant.archive_client.upsert(
            collection_name=_META,
            points=[PointStruct(
                id=point_id,
                vector=_ZERO_VEC,
                payload={"_key": _LAST_REPORT_KEY, "report": report, "generated_at": now},
            )],
        )
    except Exception as exc:
        logger.warning("rextrics: last-report cache write failed: %s", exc)
