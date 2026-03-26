"""Sovereign self-check scheduler.

Runs a health check every 6 hours. Evaluates metrics against thresholds.
Anomalies are written to episodic memory and sent to Director via Telegram.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from monitoring.metrics import collect_all

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
THRESHOLDS = {
    "vram_used_mb_warning":          7500,    # > 7.5 GB VRAM used = warning
    "qdrant_total_points_warning":   1_000_000,  # 1M points — size proxy (no byte count from API)
    "external_unreachable_warning":  True,    # any external service unreachable = warning
    "soul_mismatch_critical":        True,    # any drift = critical
    "container_not_running_critical": True,   # any sovereign container not running = critical
}

SOVEREIGN_CONTAINERS = {
    "sovereign-core", "ollama", "ollama-embed", "qdrant", "qdrant-archive",
    "docker-broker", "gateway", "nanobot-01",
}  # whisper removed 2026-03-20 — migrated to node04 as a2a-whisper (172.16.201.4:8003)
   # a2a-browser + searxng removed 2026-03-19 (replaced by node04 172.16.201.4:8001)
   # ollama-embed added 2026-03-25 — CPU-only nomic-embed-text embedding service
   # qdrant-archive added 2026-03-25 — RAID-only sovereign collections (new architecture)

SELF_CHECK_INTERVAL = 6 * 3600   # 6 hours in seconds

# ── Telegram alert (same pattern as soul_guardian) ────────────────────────────

async def _notify_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Scheduler: Telegram credentials missing — skipping notification")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("Scheduler: Telegram notification failed: %s", e)


# ── Anomaly evaluation ────────────────────────────────────────────────────────

def evaluate_metrics(metrics: dict) -> list[dict]:
    """Return list of anomaly dicts: {severity, component, detail}."""
    anomalies = []

    # GPU VRAM
    gpu = metrics.get("gpu", {})
    if "vram_used_mb" in gpu:
        if gpu["vram_used_mb"] > THRESHOLDS["vram_used_mb_warning"]:
            anomalies.append({
                "severity": "warning",
                "component": "gpu",
                "detail": f"VRAM used {gpu['vram_used_mb']} MB exceeds 7500 MB threshold",
            })

    # Containers
    containers = metrics.get("containers", [])
    if not any("error" in c for c in containers):
        running = {c["name"] for c in containers if c.get("status") == "running"}
        for name in SOVEREIGN_CONTAINERS:
            if name not in running:
                anomalies.append({
                    "severity": "critical",
                    "component": f"container:{name}",
                    "detail": f"{name} is not running",
                })

    # Qdrant
    qdrant = metrics.get("qdrant", {})
    if "total_points" in qdrant:
        if qdrant["total_points"] > THRESHOLDS["qdrant_total_points_warning"]:
            anomalies.append({
                "severity": "warning",
                "component": "qdrant",
                "detail": f"Total vector points {qdrant['total_points']} exceeds threshold",
            })

    # External services
    external = metrics.get("external", {})
    for svc, info in external.items():
        if isinstance(info, dict) and not info.get("reachable", True):
            anomalies.append({
                "severity": "warning",
                "component": f"external:{svc}",
                "detail": f"{svc} unreachable",
            })

    return anomalies


# ── Episodic memory write ─────────────────────────────────────────────────────

async def _write_episodic(cog, anomalies: list[dict], metrics_summary: str) -> None:
    if not cog or not cog.qdrant:
        return
    outcome = "negative" if anomalies else "positive"
    anomaly_text = "; ".join(f"{a['severity'].upper()} {a['component']}: {a['detail']}"
                              for a in anomalies) or "none"
    content = (
        f"Self-check at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Outcome: {outcome}. Anomalies: {anomaly_text}. Snapshot: {metrics_summary}"
    )
    try:
        await cog.save_lesson(
            fact=content,
            user_input="scheduled_self_check",
            collection="episodic",
            memory_type="episodic",
            writer="sovereign-core",
            extra_metadata={"outcome": outcome, "anomaly_count": len(anomalies)},
        )
    except Exception as e:
        logger.warning("Scheduler: episodic memory write failed: %s", e)


# ── Main self-check ───────────────────────────────────────────────────────────

async def run_self_check(app_state) -> list[dict]:
    """Collect metrics, evaluate, alert if anomalies found. Returns anomaly list."""
    logger.info("Scheduler: running self-check")
    try:
        metrics = await collect_all(app_state)
    except Exception as e:
        logger.error("Scheduler: metrics collection failed: %s", e)
        return []

    anomalies = evaluate_metrics(metrics)

    # Metrics summary for memory
    ram  = metrics.get("ram", {})
    gpu  = metrics.get("gpu", {})
    olm  = metrics.get("ollama", {})
    metrics_summary = (
        f"RAM {ram.get('used_mb', '?')}/{ram.get('total_mb', '?')} MB, "
        f"VRAM {gpu.get('vram_used_mb', '?')} MB, "
        f"Ollama latency {olm.get('last_inference_latency_ms', '?')} ms"
    )

    # Write episodic memory (always — records positive health too)
    cog = getattr(app_state, "cog", None)
    await _write_episodic(cog, anomalies, metrics_summary)

    if anomalies:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"⚠️ *Sovereign self-check — {ts}*", ""]
        for a in anomalies:
            icon = "🔴" if a["severity"] == "critical" else "🟡"
            lines.append(f"{icon} *{a['component']}*: {a['detail']}")
        lines.append("\nAll anomalies written to episodic memory.")
        await _notify_telegram("\n".join(lines))
        logger.warning("Scheduler: %d anomalies detected", len(anomalies))
    else:
        logger.info("Scheduler: self-check clean — all within thresholds")

    return anomalies


# ── Background task ───────────────────────────────────────────────────────────

async def self_check_loop(app_state) -> None:
    """Asyncio background task — runs self-check every SELF_CHECK_INTERVAL seconds."""
    # Initial delay — let services fully initialise before first check
    await asyncio.sleep(60)
    while True:
        try:
            await run_self_check(app_state)
        except Exception as e:
            logger.error("Scheduler: self-check loop error: %s", e)
        await asyncio.sleep(SELF_CHECK_INTERVAL)


def start_scheduler(app_state) -> asyncio.Task:
    """Start the background self-check task. Call from lifespan after all services ready."""
    task = asyncio.create_task(self_check_loop(app_state))
    logger.info("Scheduler: self-check loop started (interval: %dh)", SELF_CHECK_INTERVAL // 3600)
    return task


# ── Hourly RAID archive sync ───────────────────────────────────────────────────

ARCHIVE_SYNC_INTERVAL = 3600  # 1 hour


async def archive_sync_loop(qdrant, ledger) -> None:
    """Asyncio background task — retained for API compatibility; sync is now a no-op.

    In the RAID-only architecture, sovereign collections live exclusively in qdrant-archive.
    sync_to_archive() returns 0. Durable writes happen at store() time (direct to archive_client).
    Promotion of working_memory entries happens via shutdown_promote() on clean exit.
    """
    await asyncio.sleep(ARCHIVE_SYNC_INTERVAL)
    while True:
        try:
            pushed = await qdrant.sync_to_archive()  # no-op in RAID-only architecture
            ts = datetime.now(timezone.utc).isoformat()
            logger.debug("Archive sync: no-op in RAID-only architecture (pushed=%d)", pushed)
            if ledger:
                ledger.append("archive_sync", "scheduled", {
                    "tier": "LOW",
                    "points_pushed": pushed,
                    "timestamp": ts,
                    "note": "no-op: sovereign collections are RAID-only",
                })
        except Exception as e:
            logger.error("Archive sync loop error: %s", e)
        await asyncio.sleep(ARCHIVE_SYNC_INTERVAL)


def start_archive_sync(qdrant, ledger) -> asyncio.Task:
    """Start the hourly RAID archive sync task. Call from lifespan after qdrant is ready."""
    task = asyncio.create_task(archive_sync_loop(qdrant, ledger))
    logger.info("Archive sync: hourly NVMe → RAID sync loop started")
    return task


# ── Re-export self-improvement observe loop ───────────────────────────────────
# Keeps main.py import surface narrow — all scheduling starts from this module.

def start_observe_loop(app_state) -> asyncio.Task:
    """Start the daily self-improvement observe loop. Call from lifespan."""
    from monitoring.self_improvement import start_observe_loop as _start
    return _start(app_state)
