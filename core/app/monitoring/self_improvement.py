"""Sovereign Self-Improvement Harness.

Two modes:
  OBSERVE — runs daily. Aggregates inputs from all monitoring sources:
    skill success/failure rates (episodic), prospective task execution rates,
    container/GPU/RAM health, soul guardian events, audit log anomalies,
    ClawSec pending-log staleness (security/pending/ age > 7 days).
    Stores aggregated observation as episodic entry with pattern analysis.
    Compares against baseline using statistical anomaly detection.

  PROPOSE — triggered by observe when patterns warrant corrective action:
    - same skill/intent failing >3 times in rolling 7-day window
    - prospective task with status=active that has never executed
    - ClawSec fetch log stale (security/pending/ oldest entry > 7 days)
    - memory collection anomalies
    - resource thresholds exceeded (persistent soft anomaly)
    - hard failure events (immediate trigger)
    Generates structured proposal stored in prospective memory.
    Notifies Director for approval — Rex never self-modifies without approval.

Baseline: stored in semantic memory (key: baseline:self_improvement:{metric}).
  Updated weekly using rolling 30-day average.
  Up to BASELINE_MAX_VERSIONS versions kept so baseline drift is visible.

Anomaly classes:
  hard  — always anomalous regardless of baseline (401 errors, soul guardian
           fires, container crashes, validation failures). Immediate proposal.
  soft  — deviation from baseline >STDDEV_MULTIPLIER std deviations.
           Requires SOFT_ANOMALY_CYCLES_REQUIRED consecutive cycles before proposal.

Recovery: when a previously anomalous metric returns within baseline range,
  write a recovery episodic entry and close any unactioned proposal.

WM session key: self_improvement:session
"""

import asyncio
import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone, timedelta

import httpx
from qdrant_client.models import Filter, FieldCondition, MatchValue

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

OBSERVE_INTERVAL_S       = 24 * 3600          # daily observe cycle
BASELINE_UPDATE_DAYS     = 7                  # weekly baseline update
SOFT_ANOMALY_CYCLES      = 3                  # consecutive cycles before soft proposal
MIN_BASELINE_SAMPLES     = 5                  # minimum samples before soft anomaly detection
SKILL_FAILURE_THRESHOLD  = 3                  # same skill/intent failures in 7d → proposal
BASELINE_MAX_VERSIONS    = 5                  # semantic entries kept per metric (version history)
STDDEV_MULTIPLIER        = 2.0                # deviation threshold for soft anomaly

# Hard failure event types (always generate proposal regardless of baseline)
HARD_FAILURE_EVENTS = frozenset({
    "401_auth", "soul_guardian_fire", "validation_gate_failure",
    "container_crash", "nanobot_result_scan_flagged",
})

# Metric names tracked in baseline
BASELINE_METRIC_NAMES = [
    "inference_latency_p50_ms",
    "gpu_vram_pct",
    "ram_pct",
    "audit_entries_24h",
    "prospective_task_exec_rate",
    "container_running_count",
    "wallet_chains_failing",
    "external_unreachable_count",
]

# Services that generate an immediate proposal when unreachable (hard path).
# Others contribute only to the soft external_unreachable_count baseline metric.
_CRITICAL_EXTERNAL_SERVICES = frozenset({"a2a_browser"})

MONITORED_REPOS = [
    # ClawSec injection patterns repo — check for new releases
    ("digiantnz/Sovereign", "sovereign-core"),
]


# ── Telegram notification ─────────────────────────────────────────────────────

async def _notify_director(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("SIHarness: Telegram credentials missing — skipping notification")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("SIHarness: Telegram notification failed: %s", e)


# ── WM session helpers ────────────────────────────────────────────────────────

async def _load_si_session(qdrant) -> dict:
    """Scroll working_memory for the SI harness session. Returns session dict or fresh default."""
    try:
        from execution.adapters.qdrant import WORKING
        offset = None
        while True:
            result, next_offset = await qdrant.client.scroll(
                collection_name=WORKING,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in result:
                p = dict(r.payload or {})
                if p.get("_self_improvement_session"):
                    return p
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.warning("SIHarness: _load_si_session failed: %s", e)
    # Default fresh session
    return {
        "_self_improvement_session": True,
        "cycle_count": 0,
        "last_observe_ts": None,
        "last_baseline_update_ts": None,
        "consecutive_anomaly_cycles": {},   # metric_name → count
        "active_anomalies": {},              # metric_name → {class, first_detected_ts, proposal_id}
        "closed_proposals": [],             # proposal_ids that have been actioned/recovered
    }


async def _save_si_session(qdrant, session: dict) -> None:
    """Replace the SI session checkpoint in working_memory."""
    try:
        from execution.adapters.qdrant import WORKING
        # Delete existing
        offset = None
        to_delete = []
        while True:
            result, next_offset = await qdrant.client.scroll(
                collection_name=WORKING,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in result:
                if (r.payload or {}).get("_self_improvement_session"):
                    to_delete.append(r.id)
            if next_offset is None:
                break
            offset = next_offset
        if to_delete:
            await qdrant.client.delete(
                collection_name=WORKING,
                points_selector=to_delete,
            )
        # Write fresh session
        await qdrant.store(
            content="self_improvement:session",
            metadata={**session, "_self_improvement_session": True, "type": "self_improvement_session"},
            collection=WORKING,
        )
    except Exception as e:
        logger.warning("SIHarness: _save_si_session failed: %s", e)


# ── Baseline management ───────────────────────────────────────────────────────

async def _load_baseline_entries(qdrant, metric_name: str) -> list[dict]:
    """Scroll semantic memory for all baseline entries for a given metric.
    Returns list of payload dicts sorted by timestamp desc (newest first).
    """
    from execution.adapters.qdrant import SEMANTIC
    try:
        result, _ = await qdrant.archive_client.scroll(
            collection_name=SEMANTIC,
            scroll_filter=Filter(must=[
                FieldCondition(key="_baseline_metric", match=MatchValue(value=True)),
                FieldCondition(key="metric_name",      match=MatchValue(value=metric_name)),
            ]),
            limit=20,
            with_payload=True,
            with_vectors=False,
        )
        entries = [dict(r.payload or {}) for r in result]
        # Sort newest first
        entries.sort(key=lambda x: x.get("baseline_ts", ""), reverse=True)
        return entries
    except Exception as e:
        logger.warning("SIHarness: _load_baseline_entries(%s) failed: %s", metric_name, e)
        return []


async def _load_baseline(qdrant) -> dict:
    """Load the current (newest) baseline for all tracked metrics.
    Returns {metric_name: {mean, variance, std, count, last_updated}}.
    """
    baseline = {}
    for metric in BASELINE_METRIC_NAMES:
        entries = await _load_baseline_entries(qdrant, metric)
        if entries:
            baseline[metric] = entries[0]  # newest version
    return baseline


def _update_stats(existing: dict | None, new_value: float) -> dict:
    """Update rolling statistics with a new sample (Welford's online algorithm).
    Caps count at 30 (one month of daily samples) to maintain rolling window.
    Returns updated {mean, variance, std, count}.
    """
    if existing is None or existing.get("count", 0) == 0:
        return {"mean": new_value, "variance": 0.0, "std": 0.0, "count": 1}

    n = min(existing.get("count", 0), 29)  # cap at 29 so adding this gives max 30
    mean = existing.get("mean", new_value)
    variance = existing.get("variance", 0.0)

    # Welford's online update
    n_new = n + 1
    delta = new_value - mean
    new_mean = mean + delta / n_new
    delta2 = new_value - new_mean
    new_variance = (variance * n + delta * delta2) / n_new
    new_std = math.sqrt(max(new_variance, 0.0))
    return {"mean": new_mean, "variance": new_variance, "std": new_std, "count": n_new}


async def _save_baseline(qdrant, metric_name: str, stats: dict) -> None:
    """Write a new baseline version to semantic memory.
    Keeps at most BASELINE_MAX_VERSIONS entries per metric (prunes oldest).
    """
    from execution.adapters.qdrant import SEMANTIC
    ts = datetime.now(timezone.utc).isoformat()
    try:
        # Write new version
        content = (
            f"Self-improvement baseline: {metric_name}. "
            f"mean={stats['mean']:.3f} std={stats['std']:.3f} "
            f"count={stats['count']} as of {ts[:10]}"
        )
        await qdrant.store(
            content=content,
            metadata={
                "_baseline_metric": True,
                "metric_name": metric_name,
                "mean": stats["mean"],
                "variance": stats["variance"],
                "std": stats["std"],
                "count": stats["count"],
                "baseline_ts": ts,
                "type": "baseline",
                "_key": f"baseline:self_improvement:{metric_name}:v{ts[:10]}",
            },
            collection=SEMANTIC,
            writer="sovereign-core",
        )
        # Prune old versions beyond BASELINE_MAX_VERSIONS
        entries = await _load_baseline_entries(qdrant, metric_name)
        if len(entries) > BASELINE_MAX_VERSIONS:
            to_prune = entries[BASELINE_MAX_VERSIONS:]
            for entry in to_prune:
                pid = entry.get("point_id")
                if pid:
                    try:
                        await qdrant.archive_client.delete(
                            collection_name=SEMANTIC,
                            points_selector=[pid],
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.warning("SIHarness: _save_baseline(%s) failed: %s", metric_name, e)


# ── Metrics collection ────────────────────────────────────────────────────────

async def _collect_skill_stats(qdrant) -> dict:
    """Scroll episodic memory for execution log entries in the last 7 and 30 days.
    Returns {intent: {success_7d, fail_7d, success_30d, fail_30d, success_rate_7d}}.
    """
    from execution.adapters.qdrant import EPISODIC
    stats: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    cutoff_7d  = (now - timedelta(days=7)).isoformat()
    cutoff_30d = (now - timedelta(days=30)).isoformat()
    try:
        offset = None
        while True:
            result, next_offset = await qdrant.archive_client.scroll(
                collection_name=EPISODIC,
                scroll_filter=Filter(must=[
                    FieldCondition(key="_exec_log", match=MatchValue(value=True)),
                ]),
                limit=200,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in result:
                p = dict(r.payload or {})
                ts  = p.get("timestamp", "")
                intent  = p.get("intent", "unknown")
                success = p.get("success", False)
                if ts < cutoff_30d:
                    continue
                if intent not in stats:
                    stats[intent] = {"s7": 0, "f7": 0, "s30": 0, "f30": 0}
                bucket = stats[intent]
                if success:
                    bucket["s30"] += 1
                    if ts >= cutoff_7d:
                        bucket["s7"] += 1
                else:
                    bucket["f30"] += 1
                    if ts >= cutoff_7d:
                        bucket["f7"] += 1
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.warning("SIHarness: _collect_skill_stats failed: %s", e)

    # Compute success rates
    result_stats = {}
    for intent, b in stats.items():
        total_7d = b["s7"] + b["f7"]
        result_stats[intent] = {
            "success_7d":   b["s7"],
            "fail_7d":      b["f7"],
            "success_30d":  b["s30"],
            "fail_30d":     b["f30"],
            "success_rate_7d": round(b["s7"] / total_7d, 3) if total_7d > 0 else 1.0,
        }
    return result_stats


async def _collect_prospective_stats(qdrant) -> dict:
    """Check prospective tasks: active tasks that have never executed.
    Returns {never_executed: [task_ids], execution_rate: float}.
    """
    from execution.adapters.qdrant import PROSPECTIVE
    try:
        # Scroll all active prospective tasks
        p_result, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=[
                FieldCondition(key="status", match=MatchValue(value="active")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )

        if not p_result:
            return {"never_executed": [], "execution_rate": 1.0, "active_count": 0}

        # Use last_run from PROSPECTIVE payload — set by the scheduler after every execution.
        # Prior approach cross-joined PROSPECTIVE point UUIDs against EPISODIC payload task_ids,
        # which never matched (qdrant.store() generates its own point UUID independent of the
        # task_id field), causing every active task to always appear as never-executed.
        never_executed = []
        for r in p_result:
            payload = dict(r.payload or {})
            if not payload.get("last_run"):
                never_executed.append({
                    "task_id": payload.get("task_id") or str(r.id),
                    "title": payload.get("title", "unknown"),
                })

        active_count = len(p_result)
        exec_rate = 1.0 - (len(never_executed) / active_count) if active_count else 1.0
        return {
            "never_executed": never_executed,
            "execution_rate": round(exec_rate, 3),
            "active_count": active_count,
        }
    except Exception as e:
        logger.warning("SIHarness: _collect_prospective_stats failed: %s", e)
        return {"never_executed": [], "execution_rate": 1.0, "active_count": 0}


async def _collect_scheduler_health(qdrant) -> dict:
    """Detect stuck scheduled tasks and task_id collisions in PROSPECTIVE.

    Stuck: active scheduled_task with next_due > 2 days in the past and run_count == 0.
    Collision: a task_id that appears in PROSPECTIVE under multiple entry types
    (e.g. scheduled_task + improvement_proposal) — causes _find_point_id to update the
    wrong entry, leaving the task looping forever.
    Returns {stuck: [...], collisions: [...]}.
    """
    from execution.adapters.qdrant import PROSPECTIVE
    threshold = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    stuck, collisions = [], []
    try:
        items, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=[
                FieldCondition(key="type",   match=MatchValue(value="scheduled_task")),
                FieldCondition(key="status", match=MatchValue(value="active")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        for item in items:
            pl = dict(item.payload or {})
            task_id = pl.get("task_id") or str(item.id)
            nd = pl.get("next_due", "")
            rc = pl.get("run_count") or 0
            title = pl.get("title", "unknown")[:60]
            if nd and nd < threshold and rc == 0:
                stuck.append({"task_id": task_id, "title": title, "next_due": nd})
            # Check for collision: any other PROSPECTIVE entry sharing this task_id
            siblings, _ = await qdrant.archive_client.scroll(
                collection_name=PROSPECTIVE,
                scroll_filter=Filter(must=[
                    FieldCondition(key="task_id", match=MatchValue(value=task_id)),
                ]),
                limit=5,
                with_payload=True,
                with_vectors=False,
            )
            other_types = [
                s.payload.get("type") for s in siblings
                if s.payload and s.payload.get("type") != "scheduled_task"
            ]
            if other_types:
                collisions.append({
                    "task_id": task_id,
                    "title": title,
                    "conflicting_types": other_types,
                })
    except Exception as e:
        logger.warning("SIHarness: _collect_scheduler_health failed: %s", e)
    return {"stuck": stuck, "collisions": collisions}


def _check_security_pending(pending_dir: str = "/home/sovereign/security/pending") -> dict:
    """Inspect security/pending/ for ClawSec fetch logs.

    Returns {count, oldest_age_days, newest_fetch_at} based on fetch-*.json log files.
    Falls back to file mtime when fetched_at field is absent.
    """
    import glob
    result = {"count": 0, "oldest_age_days": 0.0, "newest_fetch_at": None}
    if not os.path.exists(pending_dir):
        return result
    now = datetime.now(timezone.utc)
    ages: list[float] = []
    newest_ts: str | None = None
    for fpath in glob.glob(os.path.join(pending_dir, "*.json")):
        try:
            with open(fpath) as f:
                data = json.load(f)
            fetched_at = data.get("fetched_at", "")
            if fetched_at:
                age_days = (now - datetime.fromisoformat(fetched_at)).days
                if newest_ts is None or fetched_at > newest_ts:
                    newest_ts = fetched_at
            else:
                age_days = (now.timestamp() - os.path.getmtime(fpath)) / 86400
        except Exception:
            age_days = (now.timestamp() - os.path.getmtime(fpath)) / 86400
        ages.append(age_days)
    result["count"] = len(ages)
    result["oldest_age_days"] = max(ages) if ages else 0.0
    result["newest_fetch_at"] = newest_ts
    return result


async def _collect_audit_hard_failures(ledger) -> list[dict]:
    """Scan the security ledger for hard failure events in the last 24 hours.
    Returns list of {event_type, detail, ts} dicts.
    """
    audit_path = "/home/sovereign/audit/security-ledger.jsonl"
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    hard_failures = []
    try:
        if not os.path.exists(audit_path):
            return []
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("ts", "")
                    if ts < cutoff:
                        continue
                    event_type = entry.get("event_type", "")
                    if event_type in HARD_FAILURE_EVENTS:
                        hard_failures.append({
                            "event_type": event_type,
                            "detail": str(entry.get("detail", entry))[:300],
                            "ts": ts,
                        })
                    # Also detect 401 auth errors from HTTP status in detail
                    detail_str = str(entry.get("detail", ""))
                    if "401" in detail_str and "auth" in detail_str.lower():
                        hard_failures.append({
                            "event_type": "401_auth",
                            "detail": detail_str[:300],
                            "ts": ts,
                        })
                except Exception:
                    pass
    except Exception as e:
        logger.warning("SIHarness: _collect_audit_hard_failures failed: %s", e)
    return hard_failures


def _collect_external_failures(sys_metrics: dict) -> list[dict]:
    """Inspect external reachability data already collected by collect_all().

    Returns hard-failure trigger dicts for critical services that are unreachable.
    Non-critical services contribute only to the external_unreachable_count metric.
    """
    external = sys_metrics.get("external", {})
    failures = []
    for service, status in external.items():
        if not isinstance(status, dict):
            continue
        if status.get("reachable", True):
            continue
        if service in _CRITICAL_EXTERNAL_SERVICES:
            failures.append({
                "type": "external_service_down",
                "service": service,
                "error": status.get("error", "unreachable"),
            })
    return failures


# ── Anomaly detection ─────────────────────────────────────────────────────────

def _is_soft_anomaly(metric_name: str, value: float, baseline_entry: dict) -> tuple[bool, float]:
    """Check if a value is a soft anomaly (>STDDEV_MULTIPLIER std from mean).
    Returns (is_anomaly, deviation_score).
    Requires MIN_BASELINE_SAMPLES samples before reporting.
    """
    if not baseline_entry:
        return False, 0.0
    count = baseline_entry.get("count", 0)
    if count < MIN_BASELINE_SAMPLES:
        return False, 0.0
    mean = baseline_entry.get("mean", value)
    std  = baseline_entry.get("std", 0.0)
    if std < 1e-6:   # essentially zero variance — no useful baseline yet
        return False, 0.0
    deviation = abs(value - mean) / std
    return deviation > STDDEV_MULTIPLIER, round(deviation, 2)


def _classify_anomaly(
    metric_name: str, value: float, baseline_entry: dict,
    session: dict,
) -> dict | None:
    """Classify a metric reading. Returns anomaly dict or None if normal.
    Dict: {class: "hard"|"soft", metric, value, baseline_mean, deviation, ready_to_propose}.
    """
    is_soft, deviation = _is_soft_anomaly(metric_name, value, baseline_entry)
    if not is_soft:
        return None
    consecutive = session.get("consecutive_anomaly_cycles", {}).get(metric_name, 0)
    ready = (consecutive + 1) >= SOFT_ANOMALY_CYCLES
    return {
        "class":         "soft",
        "metric":        metric_name,
        "value":         value,
        "baseline_mean": baseline_entry.get("mean", 0.0),
        "baseline_std":  baseline_entry.get("std", 0.0),
        "deviation":     deviation,
        "consecutive":   consecutive + 1,
        "ready_to_propose": ready,
    }


# ── Proposal writer ───────────────────────────────────────────────────────────

async def _write_proposal(
    qdrant, trigger: str, observation_summary: str,
    root_cause_hypothesis: str, proposed_action: str,
    required_tier: str, expected_outcome: str,
    dedup_fields: dict | None = None,
) -> str:
    """Store a structured improvement proposal in prospective memory.
    dedup_fields: dict of extra payload fields used by _existing_pending_proposal
                  (e.g. {"task_id": ..., "intent": ..., "event_type": ..., "metric": ...})
    Returns the proposal_id (UUID).
    """
    from execution.adapters.qdrant import PROSPECTIVE
    proposal_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    content = (
        f"Self-improvement proposal [{trigger}]: {observation_summary[:200]}. "
        f"Proposed action: {proposed_action[:200]}."
    )
    metadata = {
        "type": "improvement_proposal",
        "trigger": trigger,
        "observation_summary": observation_summary,
        "root_cause_hypothesis": root_cause_hypothesis,
        "proposed_corrective_action": proposed_action,
        "required_tier": required_tier,
        "expected_outcome": expected_outcome,
        "proposal_status": "pending_approval",
        "proposal_id": proposal_id,
        "created_ts": ts,
        "_key": f"prospective:self_improvement:proposal:{proposal_id}",
        "status": "pending_approval",
    }
    if dedup_fields:
        metadata.update(dedup_fields)
    try:
        await qdrant.store(
            content=content,
            metadata=metadata,
            collection=PROSPECTIVE,
            writer="sovereign-core",
        )
    except Exception as e:
        logger.warning("SIHarness: _write_proposal failed: %s", e)
    return proposal_id


# ── Recovery detection ────────────────────────────────────────────────────────

async def _check_recovery(qdrant, cog, session: dict, metrics_snapshot: dict, baseline: dict) -> list[str]:
    """Detect metrics that were anomalous last cycle but are now within baseline.
    Writes recovery episodic entries and returns list of recovered metric names.
    """
    from execution.adapters.qdrant import EPISODIC, PROSPECTIVE
    recovered = []
    active_anomalies = session.get("active_anomalies", {})
    ts = datetime.now(timezone.utc).isoformat()

    for metric_name, anomaly_info in list(active_anomalies.items()):
        current_val = metrics_snapshot.get(metric_name)
        if current_val is None:
            continue
        baseline_entry = baseline.get(metric_name)
        is_soft, _ = _is_soft_anomaly(metric_name, float(current_val), baseline_entry or {})
        if not is_soft:
            # Recovered — log episodic entry
            recovered.append(metric_name)
            _bm = baseline_entry.get("mean", 0.0) if baseline_entry else 0.0
            recovery_content = (
                f"Self-improvement recovery: {metric_name} returned to baseline. "
                f"Value={current_val:.3f} baseline_mean={_bm:.3f}. "
                f"Anomaly was active since {anomaly_info.get('first_detected_ts', 'unknown')[:10]}."
            )
            try:
                await cog.save_lesson(
                    recovery_content, "self_improvement:recovery",
                    collection=EPISODIC,
                    memory_type="episodic",
                    writer="sovereign-core",
                    extra_metadata={
                        "metric_name": metric_name,
                        "outcome": "positive",
                        "event_type": "anomaly_recovery",
                        "recovered_ts": ts,
                        "proposal_id": anomaly_info.get("proposal_id"),
                    },
                )
            except Exception as e:
                logger.warning("SIHarness: recovery episodic write failed: %s", e)

            # Close unactioned proposal if one exists
            proposal_id = anomaly_info.get("proposal_id")
            if proposal_id:
                try:
                    p_result, _ = await qdrant.archive_client.scroll(
                        collection_name=PROSPECTIVE,
                        scroll_filter=Filter(must=[
                            FieldCondition(key="proposal_id", match=MatchValue(value=proposal_id)),
                        ]),
                        limit=5,
                        with_payload=True,
                        with_vectors=False,
                    )
                    for r in p_result:
                        payload = dict(r.payload or {})
                        if payload.get("proposal_status") == "pending_approval":
                            await qdrant.archive_client.set_payload(
                                collection_name=PROSPECTIVE,
                                payload={"proposal_status": "auto_closed_recovery", "closed_ts": ts},
                                points=[r.id],
                            )
                except Exception as e:
                    logger.warning("SIHarness: proposal close on recovery failed: %s", e)

    return recovered


# ── Memory ceiling monitor ────────────────────────────────────────────────────

async def check_memory_ceiling(qdrant) -> dict:
    """Estimate qdrant-archive storage across all 7 sovereign collections.

    Estimation: points_count × (768 × 4 bytes for float32 vectors) + 512 bytes payload overhead
    per point. Compares against cfg.memory.qdrant_ceiling_gb × cfg.memory.cull_trigger_pct.

    If below threshold: logs result to META collection, no further action.
    If at or above threshold: logs to META + sends Telegram alert to Director.

    Never culls. Warns only. Director manually triggers /curate on receipt of warning.

    # Future cull logic when Director triggers /curate:
    # 1. Scroll all qdrant-archive collections
    # 2. Sort by: last_updated ASC, _weight ASC (oldest + least useful first)
    # 3. Historical entries (status="historical") naturally sort to top of cull list
    #    due to weight=1.0 (no MRFL increments after marking historical)
    # 4. Delete bottom N entries until storage drops below cull_trigger_pct * 0.70
    # 5. Log all culled MIP keys to episodic before deletion
    # 6. Notify Director with summary
    """
    from config import cfg as _cfg
    from execution.adapters.qdrant import SOVEREIGN_COLLECTIONS, META
    from datetime import datetime, timezone as _tz

    _VECTOR_DIM     = 768        # nomic-embed-text — fixed for Qdrant lifetime
    _BYTES_PER_VEC  = _VECTOR_DIM * 4   # float32
    _PAYLOAD_BYTES  = 512        # rough per-point payload overhead estimate
    _BYTES_PER_PT   = _BYTES_PER_VEC + _PAYLOAD_BYTES   # 3584 bytes per point

    ceiling_gb      = float(getattr(getattr(_cfg, "memory", object()), "qdrant_ceiling_gb", 500))
    cull_trigger    = float(getattr(getattr(_cfg, "memory", object()), "cull_trigger_pct", 0.80))
    ceiling_bytes   = ceiling_gb * (1024 ** 3)
    trigger_bytes   = ceiling_bytes * cull_trigger

    now_iso         = datetime.now(_tz.utc).isoformat()
    collection_sizes: dict[str, int] = {}
    total_points    = 0

    for coll in SOVEREIGN_COLLECTIONS:
        try:
            info = await qdrant.archive_client.get_collection(collection_name=coll)
            pts  = getattr(info, "points_count", None) or 0
            collection_sizes[coll] = pts
            total_points += pts
        except Exception as exc:
            logger.warning("check_memory_ceiling: failed to query collection %s: %s", coll, exc)
            collection_sizes[coll] = -1

    estimated_bytes = total_points * _BYTES_PER_PT
    estimated_gb    = estimated_bytes / (1024 ** 3)
    trigger_pct     = (estimated_bytes / ceiling_bytes * 100) if ceiling_bytes else 0.0
    at_ceiling      = estimated_bytes >= trigger_bytes

    result = {
        "total_points":     total_points,
        "estimated_gb":     round(estimated_gb, 3),
        "ceiling_gb":       ceiling_gb,
        "cull_trigger_pct": cull_trigger,
        "trigger_pct":      round(trigger_pct, 1),
        "at_ceiling":       at_ceiling,
        "collection_sizes": collection_sizes,
        "checked_at":       now_iso,
    }

    # Log to META (atomic set_payload pattern)
    try:
        from qdrant_client.models import Filter as _F, FieldCondition as _FC, MatchValue as _MV
        _existing, _ = await qdrant.archive_client.scroll(
            collection_name=META,
            scroll_filter=_F(must=[_FC(key="_key", match=_MV(value="meta:memory-ceiling:state"))]),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        _meta_payload = {
            "_key":         "meta:memory-ceiling:state",
            "type":         "meta",
            "last_updated": now_iso,
            **result,
        }
        if _existing:
            await qdrant.archive_client.set_payload(
                collection_name=META,
                payload=_meta_payload,
                points=[str(_existing[0].id)],
            )
        else:
            await qdrant.store(
                collection=META,
                content=f"Memory ceiling monitor state as of {now_iso}.",
                metadata=_meta_payload,
            )
    except Exception as exc:
        logger.warning("check_memory_ceiling: META write failed: %s", exc)

    if at_ceiling:
        _pct_str = f"{trigger_pct:.1f}"
        _msg = (
            f"⚠️ *Memory ceiling warning*: Qdrant archive at {_pct_str}% "
            f"of {ceiling_gb:.0f} GB ceiling "
            f"(~{estimated_gb:.1f} GB estimated, {total_points:,} points). "
            f"Manual cull required — send /curate when ready."
        )
        logger.warning("check_memory_ceiling: %s", _msg.replace("*", ""))
        await _notify_director(_msg)
    else:
        logger.info(
            "check_memory_ceiling: archive ~%.2f GB (%d total points, %.1f%% of ceiling) — OK",
            estimated_gb, total_points, trigger_pct,
        )

    return result


# ── Main observe function ─────────────────────────────────────────────────────

async def observe(qdrant, cog, ledger, app_state=None) -> dict:
    """Run one observe cycle. Returns summary dict with anomalies and triggers.

    Steps:
    1. Load WM session state
    2. Collect all monitoring inputs
    3. Load baseline; establish if first run
    4. Detect anomalies (hard + soft)
    5. Check recovery for active anomalies
    6. Store episodic observation entry
    7. Update baseline if weekly interval elapsed
    8. Update WM session
    9. Trigger propose() for any ready-to-propose conditions
    """
    logger.info("SIHarness: starting observe cycle")
    ts_now = datetime.now(timezone.utc).isoformat()

    # 1. Load session
    session = await _load_si_session(qdrant)
    session["cycle_count"] = session.get("cycle_count", 0) + 1

    # 2. Collect monitoring inputs
    from monitoring.metrics import collect_all
    try:
        sys_metrics = await collect_all(app_state)
    except Exception as e:
        logger.warning("SIHarness: metrics collection failed: %s", e)
        sys_metrics = {}

    skill_stats = await _collect_skill_stats(qdrant)
    prospective_stats = await _collect_prospective_stats(qdrant)
    scheduler_health = await _collect_scheduler_health(qdrant)
    hard_failures = await _collect_audit_hard_failures(ledger)
    external_failures = _collect_external_failures(sys_metrics)

    # 2.5. Memory ceiling check — non-blocking; logs to META; alerts Director if near ceiling
    try:
        await check_memory_ceiling(qdrant)
    except Exception as e:
        logger.warning("SIHarness: memory ceiling check failed (non-fatal): %s", e)

    # 2.6. ClawSec pending check — count + oldest age in security/pending/
    security_pending: dict = {}
    try:
        security_pending = _check_security_pending()
    except Exception as e:
        logger.warning("SIHarness: security pending check failed: %s", e)

    # Build normalised metrics snapshot (scalar values only — for baseline comparison)
    ram  = sys_metrics.get("ram",  {})
    gpu  = sys_metrics.get("gpu",  {})
    olm  = sys_metrics.get("ollama", {})
    ctrs = sys_metrics.get("containers", [])
    aud  = sys_metrics.get("audit", {})
    ext  = sys_metrics.get("external", {})

    gpu_vram_pct = 0.0
    if gpu.get("vram_total_mb") and gpu.get("vram_used_mb"):
        gpu_vram_pct = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100, 1)

    running_count = len([c for c in ctrs if isinstance(c, dict) and c.get("status") == "running"])
    wallet = sys_metrics.get("wallet", {})
    unreachable_count = float(sum(
        1 for s in ext.values()
        if isinstance(s, dict) and not s.get("reachable", True)
    ))

    metrics_snapshot = {
        "inference_latency_p50_ms": float(olm.get("last_inference_latency_ms") or 0),
        "gpu_vram_pct":             gpu_vram_pct,
        "ram_pct":                  float(ram.get("percent") or 0),
        "audit_entries_24h":        float(aud.get("last_24h_entries") or 0),
        "prospective_task_exec_rate": float(prospective_stats.get("execution_rate") or 1.0),
        "container_running_count":  float(running_count),
        "wallet_chains_failing":    float(wallet.get("chains_failing", 0)),
        "external_unreachable_count": unreachable_count,
    }

    # 3. Load (or establish) baseline
    baseline = await _load_baseline(qdrant)
    is_first_run = len(baseline) < len(BASELINE_METRIC_NAMES)

    if is_first_run:
        logger.info("SIHarness: first run — establishing initial baseline")
        for metric, value in metrics_snapshot.items():
            stats = _update_stats(None, value)
            await _save_baseline(qdrant, metric, stats)
        baseline = await _load_baseline(qdrant)

    # 4. Detect soft anomalies
    soft_anomalies = []
    consecutive_map = session.get("consecutive_anomaly_cycles", {})
    for metric, value in metrics_snapshot.items():
        anomaly = _classify_anomaly(metric, value, baseline.get(metric, {}), session)
        if anomaly:
            consecutive_map[metric] = anomaly["consecutive"]
            soft_anomalies.append(anomaly)
        else:
            # Reset consecutive count if within baseline
            consecutive_map.pop(metric, None)
    session["consecutive_anomaly_cycles"] = consecutive_map

    # 5. Check recovery for previously active anomalies
    recovered = await _check_recovery(qdrant, cog, session, metrics_snapshot, baseline)
    active_anomalies = session.get("active_anomalies", {})
    for metric in recovered:
        active_anomalies.pop(metric, None)
    session["active_anomalies"] = active_anomalies

    # 6. Identify skill failures exceeding threshold
    skill_failure_triggers = []
    for intent, st in skill_stats.items():
        if st.get("fail_7d", 0) >= SKILL_FAILURE_THRESHOLD:
            skill_failure_triggers.append({
                "intent": intent,
                "fail_7d": st["fail_7d"],
                "success_rate_7d": st["success_rate_7d"],
            })

    # 7. Identify prospective tasks never executed
    never_exec_triggers = prospective_stats.get("never_executed", [])

    # Auto-close stale proposals for tasks that have since run or been cancelled/completed
    _never_exec_ids = {t["task_id"] for t in never_exec_triggers}
    await _auto_close_resolved_never_exec_proposals(qdrant, _never_exec_ids)

    # 8. Build observation content for episodic storage
    stuck_tasks    = scheduler_health.get("stuck", [])
    task_collisions = scheduler_health.get("collisions", [])
    anomaly_count = len(hard_failures) + len(soft_anomalies)
    trigger_count = (
        len(skill_failure_triggers) + len(never_exec_triggers)
        + len(stuck_tasks) + len(task_collisions) + len(external_failures)
    )

    gpu_str = f"VRAM {gpu_vram_pct:.1f}%" if gpu_vram_pct else "GPU N/A"
    wallet_chains_failing = int(metrics_snapshot.get("wallet_chains_failing", 0))
    wallet_str = (
        f"wallet chains failing: {wallet_chains_failing}" if wallet_chains_failing > 0
        else "wallet chains: ok"
    )
    unreachable_names = [
        svc for svc, s in ext.items()
        if isinstance(s, dict) and not s.get("reachable", True)
    ]
    ext_str = (
        f"external unreachable: {', '.join(unreachable_names)}" if unreachable_names
        else "external services: ok"
    )
    _sp_count = security_pending.get("count", 0)
    _sp_oldest = security_pending.get("oldest_age_days", 0.0)
    sp_str = (
        f"security pending: {_sp_count} items (oldest {_sp_oldest:.0f}d)"
        if _sp_count > 0 else "security pending: none"
    )
    obs_content = (
        f"Self-improvement observe cycle {session['cycle_count']} at {ts_now[:16]} UTC. "
        f"System: RAM {ram.get('percent','?')}% {gpu_str}, "
        f"Ollama latency {olm.get('last_inference_latency_ms','?')}ms, "
        f"containers running {running_count}, {wallet_str}, {ext_str}. "
        f"Skills: {len(skill_stats)} intents tracked, "
        f"{len(skill_failure_triggers)} failing (>={SKILL_FAILURE_THRESHOLD}/7d). "
        f"Prospective: {prospective_stats.get('active_count',0)} active tasks, "
        f"{len(never_exec_triggers)} never executed, "
        f"{len(stuck_tasks)} stuck (overdue+run_count=0), "
        f"{len(task_collisions)} task_id collisions. "
        f"Hard failures last 24h: {len(hard_failures)}. "
        f"External service failures: {len(external_failures)}. "
        f"Soft anomalies: {len(soft_anomalies)}. "
        f"{sp_str}. "
        f"Proposal triggers: {trigger_count}."
    )

    # Write episodic observation entry
    try:
        await cog.save_lesson(
            obs_content, "self_improvement:observe",
            collection="episodic",
            memory_type="episodic",
            writer="sovereign-core",
            extra_metadata={
                "event_type": "si_observation",
                "cycle_count": session["cycle_count"],
                "anomaly_count": anomaly_count,
                "hard_failure_count": len(hard_failures),
                "soft_anomaly_count": len(soft_anomalies),
                "skill_failure_triggers": len(skill_failure_triggers),
                "never_exec_triggers": len(never_exec_triggers),
                "outcome": "negative" if (anomaly_count > 0 or trigger_count > 0) else "positive",
            },
        )
    except Exception as e:
        logger.warning("SIHarness: episodic observation write failed: %s", e)

    # Also write anomaly history entries for each detected anomaly
    for anomaly in soft_anomalies:
        try:
            ah_content = (
                f"Anomaly [{anomaly['class']}] {anomaly['metric']}: "
                f"value={anomaly['value']:.3f} baseline_mean={anomaly['baseline_mean']:.3f} "
                f"deviation={anomaly['deviation']:.1f}σ cycle={anomaly['consecutive']}/{SOFT_ANOMALY_CYCLES}"
            )
            await cog.save_lesson(
                ah_content, "self_improvement:anomaly",
                collection="episodic",
                memory_type="episodic",
                writer="sovereign-core",
                extra_metadata={
                    "event_type": "si_anomaly_history",
                    "metric_name": anomaly["metric"],
                    "baseline_value": anomaly["baseline_mean"],
                    "observed_value": anomaly["value"],
                    "deviation": anomaly["deviation"],
                    "anomaly_class": anomaly["class"],
                    "proposal_generated": anomaly["ready_to_propose"],
                    "outcome": "negative",
                },
            )
        except Exception:
            pass

    # 9. Update baseline (weekly)
    last_baseline_ts = session.get("last_baseline_update_ts")
    needs_baseline_update = (
        last_baseline_ts is None
        or (datetime.now(timezone.utc) - datetime.fromisoformat(last_baseline_ts)).days >= BASELINE_UPDATE_DAYS
    )
    if needs_baseline_update and not is_first_run:
        logger.info("SIHarness: weekly baseline update")
        for metric, value in metrics_snapshot.items():
            existing = baseline.get(metric)
            new_stats = _update_stats(existing, value)
            await _save_baseline(qdrant, metric, new_stats)
        session["last_baseline_update_ts"] = ts_now

    # 10. Collect all proposal triggers
    proposal_triggers = []

    # Hard failure triggers (immediate)
    for hf in hard_failures:
        proposal_triggers.append({
            "type": "hard_failure",
            "event_type": hf["event_type"],
            "detail": hf["detail"],
            "ts": hf["ts"],
        })

    # External service down triggers (critical services only — others via soft baseline)
    for ef in external_failures:
        proposal_triggers.append({
            "type": "external_service_down",
            "service": ef["service"],
            "error": ef["error"],
        })

    # Skill failure triggers
    for sf in skill_failure_triggers:
        proposal_triggers.append({
            "type": "skill_failure_pattern",
            "intent": sf["intent"],
            "fail_7d": sf["fail_7d"],
            "success_rate_7d": sf["success_rate_7d"],
        })

    # Prospective never-executed triggers
    for ne in never_exec_triggers:
        proposal_triggers.append({
            "type": "prospective_never_executed",
            "task_id": ne["task_id"],
            "title": ne["title"],
        })

    # Scheduler stuck-task triggers
    for st in stuck_tasks:
        proposal_triggers.append({
            "type": "scheduler_stuck_task",
            "task_id": st["task_id"],
            "title": st["title"],
            "next_due": st["next_due"],
        })

    # Scheduler task_id collision triggers
    for tc in task_collisions:
        proposal_triggers.append({
            "type": "scheduler_task_id_collision",
            "task_id": tc["task_id"],
            "title": tc["title"],
            "conflicting_types": tc["conflicting_types"],
        })

    # ClawSec staleness trigger — count > 0 and oldest > 7 days
    _CLAWSEC_STALE_DAYS = 7
    if _sp_count > 0 and _sp_oldest > _CLAWSEC_STALE_DAYS:
        proposal_triggers.append({
            "type":            "clawsec_stale",
            "pending_count":   _sp_count,
            "oldest_age_days": _sp_oldest,
        })

    # Soft anomaly triggers (only if ready after SOFT_ANOMALY_CYCLES consecutive cycles)
    for anomaly in soft_anomalies:
        if anomaly["ready_to_propose"]:
            proposal_triggers.append({
                "type": "soft_anomaly",
                "metric": anomaly["metric"],
                "value": anomaly["value"],
                "baseline_mean": anomaly["baseline_mean"],
                "deviation": anomaly["deviation"],
            })
            # Track as active anomaly
            session["active_anomalies"][anomaly["metric"]] = {
                "class": "soft",
                "first_detected_ts": ts_now,
                "proposal_id": None,  # will be filled after propose()
            }

    # Update session
    session["last_observe_ts"] = ts_now
    await _save_si_session(qdrant, session)

    # 11. Generate proposals if triggers found
    proposal_ids = []
    if proposal_triggers:
        proposal_ids = await propose(qdrant, cog, ledger, proposal_triggers)

    # Update session with proposal IDs for soft anomalies
    if proposal_ids:
        # Match proposals back to active anomaly records (soft anomaly proposals)
        soft_trigger_count = sum(1 for t in proposal_triggers if t["type"] == "soft_anomaly")
        for i, t in enumerate([t for t in proposal_triggers if t["type"] == "soft_anomaly"]):
            if i < len(proposal_ids):
                metric = t["metric"]
                if metric in session.get("active_anomalies", {}):
                    session["active_anomalies"][metric]["proposal_id"] = proposal_ids[i]
        await _save_si_session(qdrant, session)

    summary = {
        "cycle": session["cycle_count"],
        "ts": ts_now,
        "hard_failures": len(hard_failures),
        "soft_anomalies": len(soft_anomalies),
        "skill_failure_triggers": len(skill_failure_triggers),
        "never_exec_triggers": len(never_exec_triggers),
        "recovered_metrics": recovered,
        "proposals_generated": len(proposal_ids),
        "proposal_triggers": len(proposal_triggers),
    }
    logger.info(
        "SIHarness: observe cycle %d complete — %d triggers, %d proposals",
        session["cycle_count"], len(proposal_triggers), len(proposal_ids),
    )
    return summary


# ── Propose function ──────────────────────────────────────────────────────────

async def _auto_close_resolved_never_exec_proposals(qdrant, current_never_exec_ids: set) -> int:
    """Close pending prospective_never_executed proposals for tasks that have since run or been removed."""
    from execution.adapters.qdrant import PROSPECTIVE
    try:
        items, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=[
                FieldCondition(key="type",            match=MatchValue(value="improvement_proposal")),
                FieldCondition(key="trigger",         match=MatchValue(value="prospective_never_executed")),
                FieldCondition(key="proposal_status", match=MatchValue(value="pending_approval")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        closed = 0
        ts = datetime.now(timezone.utc).isoformat()
        for item in items:
            task_id = (item.payload or {}).get("task_id")
            if task_id and task_id not in current_never_exec_ids:
                await qdrant.archive_client.set_payload(
                    collection_name=PROSPECTIVE,
                    payload={"proposal_status": "auto_closed_recovery", "closed_ts": ts},
                    points=[item.id],
                )
                closed += 1
        if closed:
            logger.info("SIHarness: auto-closed %d stale never_exec proposals", closed)
        return closed
    except Exception as e:
        logger.warning("SIHarness: _auto_close_resolved_never_exec_proposals failed: %s", e)
        return 0


async def _existing_pending_proposal(qdrant, trigger_type: str, dedup_key: str | None) -> bool:
    """Return True if a pending_approval proposal with the same trigger + dedup_key exists.

    dedup_key: task_id for prospective_never_executed, intent for skill_failure_pattern,
               event_type for hard_failure, metric for soft_anomaly.
    Prevents the observe loop from generating duplicate proposals each daily cycle.
    """
    from execution.adapters.qdrant import PROSPECTIVE
    try:
        must = [
            FieldCondition(key="trigger",         match=MatchValue(value=trigger_type)),
            FieldCondition(key="proposal_status", match=MatchValue(value="pending_approval")),
        ]
        if dedup_key:
            # Map trigger type to the payload field that holds the dedup value
            _key_field = {
                "prospective_never_executed": "task_id",
                "skill_failure_pattern":      "intent",
                "hard_failure":               "event_type",
                "soft_anomaly":               "metric",
                "external_service_down":      "service",
                "clawsec_stale":              "clawsec_key",
            }.get(trigger_type)
            if _key_field:
                must.append(FieldCondition(key=_key_field, match=MatchValue(value=dedup_key)))
        items, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=must),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(items) > 0
    except Exception as e:
        logger.warning("SIHarness: dedup check failed: %s", e)
        return False  # fail-open: write proposal if check errors


async def propose(qdrant, cog, ledger, triggers: list[dict]) -> list[str]:
    """Generate structured improvement proposals for the given triggers.
    Stores each in prospective memory and notifies Director.
    Skips triggers that already have a pending_approval proposal (dedup).
    Returns list of generated proposal_ids.
    """
    if not triggers:
        return []

    proposal_ids = []
    notification_lines = ["*🧠 Rex Self-Improvement Proposals*", ""]

    for trigger in triggers:
        ttype = trigger.get("type")

        if ttype == "hard_failure":
            event = trigger.get("event_type", "unknown")
            detail = trigger.get("detail", "")
            obs = f"Hard failure event '{event}' detected in audit log: {detail[:200]}"
            hypothesis = f"A hard system failure occurred ({event}). Likely root cause: {detail[:150]}"
            action = _suggest_action_for_hard_failure(event)
            tier = "MID"
            outcome = f"Eliminate recurrence of {event} events; restore stable operation."

        elif ttype == "external_service_down":
            service = trigger.get("service", "unknown")
            error   = trigger.get("error", "unreachable")
            obs = f"External service '{service}' is unreachable (error: {error})."
            hypothesis = (
                f"'{service}' failed its health probe. Possible causes: "
                "service crash, network partition, or node04 offline."
            )
            action = (
                f"Check '{service}' health endpoint manually. "
                f"If node04-hosted: SSH to node04 and inspect docker logs. "
                f"If network issue: verify VLAN 172.16.201.0/24 routing from sovereign host."
            )
            tier = "LOW"
            outcome = f"'{service}' health probe returns reachable on next observe cycle."

        elif ttype == "skill_failure_pattern":
            intent = trigger.get("intent", "unknown")
            fail_7d = trigger.get("fail_7d", 0)
            rate = trigger.get("success_rate_7d", 0.0)
            obs = (f"Intent '{intent}' has failed {fail_7d} times in the last 7 days "
                   f"(success rate: {rate:.0%}).")
            hypothesis = (f"'{intent}' is failing repeatedly. Possible causes: "
                          "adapter misconfiguration, external service degradation, "
                          "or skill logic error.")
            action = (f"Review audit log for '{intent}' failures. Consider: "
                      "re-testing the skill, checking adapter credentials, "
                      "or installing an updated skill via the skill-install harness.")
            tier = "MID"
            outcome = f"Restore '{intent}' to >90% success rate over 7 days."

        elif ttype == "prospective_never_executed":
            task_id = trigger.get("task_id", "unknown")
            title = trigger.get("title", "unknown task")
            obs = f"Prospective task '{title}' (ID: {task_id}) has never executed despite active status."
            hypothesis = ("Task was approved but its schedule may be malformed, "
                          "its required adapters may be unavailable, or its "
                          "next_due date may not be advancing correctly.")
            action = (f"Review task '{title}' via list_tasks. "
                      "Check schedule expression, next_due date, and required adapter availability. "
                      "Consider pausing and recreating if schedule is malformed.")
            tier = "LOW"
            outcome = f"Task '{title}' executes successfully on its next scheduled due date."

        elif ttype == "clawsec_stale":
            count    = trigger.get("pending_count", 0)
            oldest   = trigger.get("oldest_age_days", 0.0)
            obs = (
                f"ClawSec security/pending/ has {count} item(s), "
                f"oldest is {oldest:.0f} days old (threshold: 7 days). "
                "Live dynamic injection patterns may be stale."
            )
            hypothesis = (
                "clawsec_update has not been run recently, or the feed was not reachable. "
                "Dynamic scanner patterns loaded from clawsec_dynamic.yaml may be outdated."
            )
            action = (
                "Run clawsec_update to fetch the live ClawSec feed and refresh "
                "clawsec_dynamic.yaml. This is a MID-tier operation."
            )
            tier = "MID"
            outcome = "clawsec_dynamic.yaml is refreshed; security/pending/ has a recent fetch log."

        elif ttype == "soft_anomaly":
            metric = trigger.get("metric", "unknown")
            value = trigger.get("value", 0.0)
            mean  = trigger.get("baseline_mean", 0.0)
            dev   = trigger.get("deviation", 0.0)
            obs = (f"Metric '{metric}' has deviated {dev:.1f} standard deviations from baseline "
                   f"for {SOFT_ANOMALY_CYCLES} consecutive observe cycles "
                   f"(current: {value:.2f}, baseline mean: {mean:.2f}).")
            hypothesis = _hypothesis_for_metric(metric, value, mean)
            action = _suggest_action_for_metric(metric, value, mean)
            tier = "LOW"
            outcome = f"Metric '{metric}' returns within 1 standard deviation of baseline mean."

        else:
            continue

        # Dedup: skip if a pending_approval proposal already exists for this trigger
        _dedup_key = {
            "prospective_never_executed": trigger.get("task_id"),
            "skill_failure_pattern":      trigger.get("intent"),
            "hard_failure":               trigger.get("event_type"),
            "soft_anomaly":               trigger.get("metric"),
            "external_service_down":      trigger.get("service"),
            "clawsec_stale":              "clawsec",
        }.get(ttype)
        if await _existing_pending_proposal(qdrant, ttype, _dedup_key):
            logger.info("SIHarness: skipping duplicate proposal for %s / %s", ttype, _dedup_key)
            continue

        # Write proposal to prospective memory
        pid = await _write_proposal(
            qdrant, trigger=ttype,
            observation_summary=obs,
            root_cause_hypothesis=hypothesis,
            proposed_action=action,
            required_tier=tier,
            expected_outcome=outcome,
            dedup_fields={_key_field: _dedup_key} if (_key_field := {
                "prospective_never_executed": "task_id",
                "skill_failure_pattern":      "intent",
                "hard_failure":               "event_type",
                "soft_anomaly":               "metric",
                "external_service_down":      "service",
                "clawsec_stale":              "clawsec_key",
            }.get(ttype)) and _dedup_key else None,
        )
        proposal_ids.append(pid)

        icon = "🔴" if ttype == "hard_failure" else "🟡"
        notification_lines.append(f"{icon} *{ttype.replace('_', ' ').title()}*")
        notification_lines.append(f"   {obs[:150]}")
        notification_lines.append(f"   Action: {action[:100]}")
        notification_lines.append(f"   Tier: {tier} | Proposal: `{pid[:8]}...`")
        notification_lines.append("")

    if proposal_ids:
        # Proposals stored silently — Director reviews on demand via self_improve_proposals
        logger.info("SIHarness: %d proposals generated and stored in prospective memory (no Telegram push)", len(proposal_ids))

    return proposal_ids


def _suggest_action_for_hard_failure(event_type: str) -> str:
    actions = {
        "401_auth":               "Check and rotate credentials for the affected adapter. Review secrets/.",
        "soul_guardian_fire":     "Inspect drift in soul/persona/governance files. Review security-ledger.jsonl.",
        "validation_gate_failure":"Review failed validation step in audit log. Identify which skill step failed.",
        "container_crash":        "Check container logs via get_logs. Review compose.yml resource limits.",
        "nanobot_result_scan_flagged": "Review flagged nanobot result in audit log. Check external source integrity.",
    }
    return actions.get(event_type, f"Review audit log for details on '{event_type}'. Investigate root cause.")


def _hypothesis_for_metric(metric: str, value: float, mean: float) -> str:
    direction = "elevated" if value > mean else "degraded"
    hypotheses = {
        "inference_latency_p50_ms": f"Ollama inference latency is {direction}. GPU may be under pressure or model weights have changed.",
        "gpu_vram_pct":             f"GPU VRAM usage is {direction}. A process may be holding VRAM or model size has increased.",
        "ram_pct":                  f"RAM usage is {direction}. A memory leak or accumulation of working_memory may be occurring.",
        "audit_entries_24h":        f"Audit log volume is {direction}. Elevated activity may indicate unusual operation patterns or errors.",
        "prospective_task_exec_rate": f"Prospective task execution rate is {direction}. Scheduled tasks may be failing or not firing.",
        "container_running_count":  f"Running container count is {direction}. A container may have crashed or been added/removed.",
        "wallet_chains_failing":    f"Wallet chain connectivity is {direction}. One or more chain watchers (ETH/BTC/Lightning) are failing to reach their node endpoints.",
        "external_unreachable_count": f"Number of unreachable external services is {direction}. One or more external dependencies (a2a_browser, a2a_whisper, grok_api, claude_api, nextcloud_webdav, telegram) have become unreachable.",
    }
    return hypotheses.get(metric, f"Metric '{metric}' is {direction} vs baseline. Unknown root cause — investigation required.")


def _suggest_action_for_metric(metric: str, value: float, mean: float) -> str:
    suggestions = {
        "inference_latency_p50_ms": "Check Ollama GPU stats. Consider restarting Ollama if latency is severe.",
        "gpu_vram_pct":             "Check docker stats for Ollama. Restart Ollama to release VRAM if stuck.",
        "ram_pct":                  "Check container RAM usage. Consider restarting sovereign-core if usage is abnormal.",
        "audit_entries_24h":        "Review security-ledger.jsonl for unusual event patterns in last 24h.",
        "prospective_task_exec_rate": "Run list_tasks and review next_due dates. Check task scheduler logs.",
        "container_running_count":  "Run list_containers to identify which container(s) are not running.",
        "external_unreachable_count": "Run get_hardware or check dashboard external reachability section. For node04 services (a2a_browser, a2a_whisper): SSH to node04 and inspect docker logs. For API services: check network connectivity from sovereign host.",
        "wallet_chains_failing":    "Check sov-wallet /health for chain status. Inspect docker logs sov-wallet for connectivity errors. Check node endpoints (BTC: Start9, ETH: node01/node02, Lightning: BTCPay).",
    }
    return suggestions.get(metric, f"Investigate '{metric}' anomaly. Check relevant logs and system state.")


# ── Query functions (for dispatch handler) ───────────────────────────────────

async def list_pending_proposals(qdrant) -> dict:
    """Return all pending improvement proposals (status=pending_approval)."""
    from execution.adapters.qdrant import PROSPECTIVE
    try:
        result, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=[
                FieldCondition(key="type",            match=MatchValue(value="improvement_proposal")),
                FieldCondition(key="proposal_status", match=MatchValue(value="pending_approval")),
            ]),
            limit=50,
            with_payload=True,
            with_vectors=False,
        )
        proposals = []
        for r in result:
            p = dict(r.payload or {})
            proposals.append({
                "proposal_id":    p.get("proposal_id", str(r.id)),
                "trigger":        p.get("trigger"),
                "observation":    p.get("observation_summary", "")[:200],
                "action":         p.get("proposed_corrective_action", "")[:200],
                "required_tier":  p.get("required_tier"),
                "created_ts":     p.get("created_ts", "")[:19],
                "status":         p.get("proposal_status"),
            })
        proposals.sort(key=lambda x: x.get("created_ts", ""), reverse=True)
        return {"status": "ok", "count": len(proposals), "proposals": proposals}
    except Exception as e:
        logger.warning("SIHarness: list_pending_proposals failed: %s", e)
        return {"status": "error", "error": str(e)}


async def dismiss_proposals(qdrant, proposal_ids: list | None = None) -> dict:
    """Dismiss pending improvement proposals. If proposal_ids is None/empty, dismiss all pending."""
    from execution.adapters.qdrant import PROSPECTIVE
    try:
        items, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=[
                FieldCondition(key="type",            match=MatchValue(value="improvement_proposal")),
                FieldCondition(key="proposal_status", match=MatchValue(value="pending_approval")),
            ]),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        ts = datetime.now(timezone.utc).isoformat()
        dismissed = 0
        for item in items:
            p = item.payload or {}
            pid = p.get("proposal_id", str(item.id))
            pid_short = pid[:8] if len(pid) >= 8 else pid
            if (
                not proposal_ids
                or pid in proposal_ids
                or pid_short in proposal_ids
            ):
                await qdrant.archive_client.set_payload(
                    collection_name=PROSPECTIVE,
                    payload={"proposal_status": "dismissed_by_director", "closed_ts": ts},
                    points=[item.id],
                )
                dismissed += 1
        logger.info("SIHarness: Director dismissed %d proposals", dismissed)
        return {"status": "ok", "dismissed": dismissed}
    except Exception as e:
        logger.warning("SIHarness: dismiss_proposals failed: %s", e)
        return {"status": "error", "error": str(e)}


async def get_baseline_report(qdrant) -> dict:
    """Return a summary of current baseline metrics."""
    baseline = await _load_baseline(qdrant)
    report = {}
    for metric, entry in baseline.items():
        report[metric] = {
            "mean":         round(entry.get("mean", 0.0), 3),
            "std":          round(entry.get("std", 0.0), 3),
            "count":        entry.get("count", 0),
            "last_updated": entry.get("baseline_ts", "unknown")[:10],
        }
    return {
        "status": "ok",
        "baseline_metrics": len(report),
        "report": report,
    }


async def run_manual_observe(qdrant, cog, ledger, app_state=None) -> dict:
    """Manually trigger one observe cycle. Returns summary."""
    return await observe(qdrant, cog, ledger, app_state)


# ── Background task management ────────────────────────────────────────────────

async def observe_loop(app_state) -> None:
    """Asyncio background task — runs observe cycle every OBSERVE_INTERVAL_S seconds."""
    # Initial delay: let all services fully initialise before first observe
    await asyncio.sleep(120)
    while True:
        try:
            qdrant = getattr(app_state, "qdrant", None)
            cog    = getattr(app_state, "cog",    None)
            ledger = getattr(app_state, "ledger", None)
            if qdrant and cog:
                await observe(qdrant, cog, ledger, app_state)
            else:
                logger.warning("SIHarness: qdrant or cog not available — skipping observe cycle")
        except Exception as e:
            logger.error("SIHarness: observe loop error: %s", e)
        await asyncio.sleep(OBSERVE_INTERVAL_S)


def start_observe_loop(app_state) -> asyncio.Task:
    """Start the self-improvement observe background task."""
    task = asyncio.create_task(observe_loop(app_state))
    logger.info("SIHarness: daily observe loop started (interval: %dh)", OBSERVE_INTERVAL_S // 3600)
    return task
