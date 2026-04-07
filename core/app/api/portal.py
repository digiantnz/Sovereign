"""Sovereign Management Portal — API endpoints.

Provides read-only introspective endpoints for the portal dashboard and
for Rex to report on his own capabilities via natural language.

All endpoints are LOW tier, read-only, and append to the audit ledger.
No writes to any sovereign collection. No secrets in scope.
"""

import asyncio
import json
import logging
import os
import re
import struct
from typing import AsyncGenerator

import httpx
import yaml
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────────

SKILLS_DIR       = "/home/sovereign/skills"
GOVERNANCE_PATH  = "/app/governance/governance.json"
DASHBOARD_PATH   = "/home/sovereign/portal/sovereign-portal.html"
BROKER_URL       = os.environ.get("BROKER_URL", "http://docker-broker:8088")
LOG_CONTAINERS   = ["sovereign-core", "gateway", "nanobot-01"]
LOG_TAIL_EACH    = 34   # 34 × 3 containers ≈ 100 initial lines
LOG_HEARTBEAT_S  = 25.0

SOVEREIGN_COLLECTIONS = frozenset({
    "semantic", "episodic", "prospective", "procedural",
    "associative", "relational", "meta",
})

# ── Skill scanning ─────────────────────────────────────────────────────────────

def _executor_from_deps(deps: list) -> str:
    """Derive human-readable executor string from SKILL.md sovereign.adapter_deps."""
    if not deps:
        return "unknown"
    if "nanobot" in deps:
        return "python3_exec → nanobot-01"
    return " + ".join(deps)


def _scan_skills() -> list[dict]:
    """Scan /home/sovereign/skills/ and return a structured list of skill definitions.

    Reads each skill's SKILL.md frontmatter to extract name, executor,
    specialists, tier_required, and operations list.
    """
    if not os.path.isdir(SKILLS_DIR):
        return []
    results = []
    for skill_dir in sorted(os.listdir(SKILLS_DIR)):
        skill_md = os.path.join(SKILLS_DIR, skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            with open(skill_md) as f:
                content = f.read()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        sov  = fm.get("sovereign", {}) if isinstance(fm.get("sovereign"), dict) else {}
        name = fm.get("name", skill_dir)
        deps = sov.get("adapter_deps", [])
        ops_raw = sov.get("operations", {})
        ops  = list(ops_raw.keys()) if isinstance(ops_raw, dict) else []
        results.append({
            "name":         name,
            "executor":     _executor_from_deps(deps),
            "specialists":  sov.get("specialists", []),
            "tier_required": sov.get("tier_required", "LOW"),
            "ops":          ops,
        })
    return results


# ── Harness status ─────────────────────────────────────────────────────────────

# Static manifest of all known harnesses.
# flag: Qdrant working_memory payload flag field — set to True on checkpoint save.
HARNESS_DEFS = [
    {
        "key":    "developer_harness",
        "name":   "Developer Harness",
        "flag":   "_developer_harness_checkpoint",
        "phases": ["Analyse", "Classify", "Plan", "HITL Approve", "Execute"],
        "trigger": "Nightly 14:00 UTC",
        "hitl":   True,
    },
    {
        "key":    "self_improvement",
        "name":   "Self-Improvement Harness",
        "flag":   "_self_improvement_session",
        "phases": ["Observe", "Aggregate", "Anomaly Detect", "Propose", "Director Approve", "Execute"],
        "trigger": "Daily / failure-triggered",
        "hitl":   True,
    },
    {
        "key":    "skill_harness",
        "name":   "Skill Install Harness",
        "flag":   "_skill_harness_checkpoint",
        "phases": ["Search", "LLM Select", "Scan", "Install"],
        "trigger": "Director-initiated (/install)",
        "hitl":   True,
    },
    {
        "key":    "pm_harness",
        "name":   "Project Management Harness",
        "flag":   None,     # PLANNED — not yet implemented
        "phases": ["Scope", "Design", "Director Approve", "Build", "Test", "Ship"],
        "trigger": "Director-initiated",
        "hitl":   True,
    },
]


async def _get_harness_sessions(qdrant) -> dict[str, dict]:
    """Scroll working_memory for all harness checkpoint records.

    Returns {flag_key: payload_dict} for each checkpoint found.
    Uses unfiltered scroll + Python-side flag check (consistent with
    existing DevHarness._load_checkpoint pattern).
    """
    sessions: dict[str, dict] = {}
    known_flags = {h["flag"] for h in HARNESS_DEFS if h["flag"]}
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
                p = r.payload or {}
                for flag in known_flags:
                    if p.get(flag) and flag not in sessions:
                        sessions[flag] = dict(p)
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.warning("portal /harnesses: working_memory scroll failed: %s", e)
    return sessions


async def _get_dev_harness_prospective(qdrant) -> dict | None:
    """Scroll PROSPECTIVE (archive) for the nightly dev-harness task record.

    Identifies the task by presence of a step with intent='dev_analyse'
    and params.trigger='nightly'. Returns the PROSPECTIVE payload or None.
    """
    try:
        from execution.adapters.qdrant import PROSPECTIVE
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        offset = None
        while True:
            result, next_offset = await qdrant.archive_client.scroll(
                collection_name=PROSPECTIVE,
                scroll_filter=Filter(must=[
                    FieldCondition(key="type", match=MatchValue(value="prospective")),
                ]),
                limit=50,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in result:
                p = r.payload or {}
                steps = p.get("steps", [])
                for step in steps:
                    if (isinstance(step, dict)
                            and step.get("intent") == "dev_analyse"
                            and step.get("params", {}).get("trigger") == "nightly"):
                        return p
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.warning("portal /harnesses: PROSPECTIVE dev-harness scroll failed: %s", e)
    return None


# ── Docker log streaming ───────────────────────────────────────────────────────

async def _stream_container_logs(
    container: str,
    queue: "asyncio.Queue[str]",
    tail: int = LOG_TAIL_EACH,
) -> None:
    """Stream Docker container logs into the shared queue.

    Implements the full Docker multiplexed log stream parser (RFC 8-byte header):
      bytes 0:   stream type (1=stdout, 2=stderr; discarded — both shown)
      bytes 1-3: 0x00 padding
      bytes 4-7: frame payload size (big-endian uint32)
      bytes 8…:  frame_size bytes of UTF-8 log text

    Permitted by docker-policy.yaml: GET:/containers/*/logs in trust.levels.low.allow.
    Named policy intent: log_tail_sovereign (see named_commands section).
    """
    url    = f"{BROKER_URL}/containers/{container}/logs"
    params = {"follow": "1", "stdout": "1", "stderr": "1", "tail": str(tail)}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
        ) as client:
            async with client.stream(
                "GET", url, params=params, headers={"X-Trust-Level": "low"}
            ) as response:
                if response.status_code != 200:
                    await queue.put(f"[{container}] error: HTTP {response.status_code}")
                    return
                buf = b""
                async for chunk in response.aiter_bytes():
                    buf += chunk
                    # Parse complete frames from buffer
                    while len(buf) >= 8:
                        frame_size = struct.unpack(">I", buf[4:8])[0]
                        if len(buf) < 8 + frame_size:
                            break   # incomplete frame — accumulate more data
                        frame_bytes = buf[8:8 + frame_size]
                        buf         = buf[8 + frame_size:]
                        text = frame_bytes.decode("utf-8", errors="replace").rstrip("\n")
                        if text:
                            for line in text.splitlines():
                                if line.strip():
                                    await queue.put(f"[{container}] {line}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("portal /logs/stream: %s stream ended: %s", container, e)
        try:
            await queue.put(f"[{container}] stream ended")
        except Exception:
            pass


async def _sse_log_generator(request: Request) -> AsyncGenerator[str, None]:
    """Async generator for the SSE log stream endpoint.

    Opens three parallel httpx streaming connections to the broker Docker API
    proxy, one per container. Lines are merged into a single asyncio.Queue
    and yielded as SSE events. A 25-second heartbeat keeps the connection alive
    through proxies and firewalls. All tasks are cancelled on client disconnect.
    """
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    tasks = [
        asyncio.create_task(_stream_container_logs(c, queue))
        for c in LOG_CONTAINERS
    ]
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                line = await asyncio.wait_for(queue.get(), timeout=LOG_HEARTBEAT_S)
                yield f"data: {line}\n\n"
            except asyncio.TimeoutError:
                yield "data: [heartbeat]\n\n"
    finally:
        for t in tasks:
            t.cancel()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _audit(request: Request, event: str, stage: str = "portal") -> None:
    """Append LOW-tier portal read event to the audit ledger (best-effort)."""
    try:
        ledger = request.app.state.ledger
        if ledger:
            ledger.append(event, stage, {"tier": "LOW", "source": "portal"})
    except Exception:
        pass


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/skills")
async def get_skills(request: Request):
    """Return the currently loaded skill list with executor, specialists, and ops.

    Scans /home/sovereign/skills/*/SKILL.md at request time for live data.
    Cross-references app.state.skill_summary (startup scan) to mark loaded status.
    """
    _audit(request, "portal_read", "skills")
    skill_summary = getattr(getattr(request.app, "state", None), "skill_summary", {}) or {}
    loaded_names  = {name for names in skill_summary.values() for name in names}
    skills = _scan_skills()
    for s in skills:
        s["loaded"] = s["name"] in loaded_names
    return {"skills": skills}


@router.get("/harnesses")
async def get_harnesses(request: Request):
    """Return harness session state from working_memory, supplemented by PROSPECTIVE for dev-harness.

    Working memory is ephemeral — session state is only present if the harness ran
    this session. last_run is null across container restarts unless supplemented
    from the PROSPECTIVE task scheduler entry (dev-harness only).
    """
    _audit(request, "portal_read", "harnesses")
    qdrant = getattr(getattr(request.app, "state", None), "qdrant", None)
    sessions: dict[str, dict] = {}
    dev_prospective: dict | None = None
    if qdrant:
        sessions        = await _get_harness_sessions(qdrant)
        dev_prospective = await _get_dev_harness_prospective(qdrant)

    harnesses = []
    for h in HARNESS_DEFS:
        flag = h.get("flag")
        cp   = sessions.get(flag) if flag else None

        if cp:
            status            = "RUNNING"
            last_phase        = cp.get("current_step")
            # SI harness uses last_observe_ts; others use last_checkpoint_ts
            last_run          = cp.get("last_checkpoint_ts") or cp.get("last_observe_ts")
        elif h["key"] == "developer_harness" and dev_prospective:
            last_phase = None
            last_run   = dev_prospective.get("last_run")
            status     = "SCHEDULED" if dev_prospective.get("status") == "active" else "PLANNED"
        else:
            status, last_phase, last_run = "PLANNED", None, None

        next_due = dev_prospective.get("next_due") if (h["key"] == "developer_harness" and dev_prospective) else None

        harnesses.append({
            "key":        h["key"],
            "name":       h["name"],
            "status":     status,
            "last_run":   last_run,
            "last_phase": last_phase,
            "next_due":   next_due,
            "phases":     h["phases"],
            "trigger":    h["trigger"],
            "hitl":       h.get("hitl", True),
        })
    return {"harnesses": harnesses}


@router.get("/governance")
async def get_governance(request: Request):
    """Return the full parsed governance.json — tiers, intent_tiers, nanobots, cognition, specialists.

    No transformation. Returns the file as-is from the RAID read-only mount.
    """
    _audit(request, "portal_read", "governance")
    try:
        with open(GOVERNANCE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return JSONResponse({"error": "governance.json not found"}, status_code=404)
    except Exception as e:
        logger.warning("portal /governance: read failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/memory/preview")
async def memory_preview(
    request: Request,
    collection: str = Query(..., description="Sovereign collection name"),
    limit: int      = Query(default=20, ge=1, le=50),
):
    """Return the top N points from a RAID sovereign collection via scroll().

    Returns raw payload fields; content is truncated to 200 chars at the API
    layer (the portal tooltip renders a further 80-char snippet in the UI).
    score is null — scroll() has no relevance ranking.

    # TODO: add ?query=<text> parameter for vector-ranked preview via search()
    """
    if collection not in SOVEREIGN_COLLECTIONS:
        return JSONResponse(
            {"error": f"Unknown collection '{collection}'. Valid: {sorted(SOVEREIGN_COLLECTIONS)}"},
            status_code=400,
        )
    _audit(request, "portal_read", "memory_preview")
    qdrant = getattr(getattr(request.app, "state", None), "qdrant", None)
    if not qdrant:
        return JSONResponse({"error": "Qdrant adapter not available"}, status_code=503)
    try:
        results, _ = await qdrant.archive_client.scroll(
            collection_name=collection,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        points = []
        for r in results:
            p = dict(r.payload or {})
            # Truncate content at API layer — UI tooltip applies its own 80-char limit
            if isinstance(p.get("content"), str) and len(p["content"]) > 200:
                p["content"] = p["content"][:200] + "…"
            points.append({"id": str(r.id), "score": None, "payload": p})
        return {"collection": collection, "points": points}
    except Exception as e:
        logger.warning("portal /memory/preview: scroll failed on '%s': %s", collection, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/logs/stream")
async def logs_stream(request: Request):
    """SSE endpoint: streaming Docker logs from sovereign-core, gateway, nanobot-01.

    Routes through broker Docker API proxy using the existing
    GET:/containers/*/logs permission (trust.levels.low.allow in docker-policy.yaml).
    Named policy intent: log_tail_sovereign (see docker-policy.yaml named_commands).

    Tier: LOW — read-only, introspective only, never execution.
    Audit label: introspective (not execution).
    """
    try:
        ledger = request.app.state.ledger
        if ledger:
            ledger.append("portal_log_stream", "introspective", {
                "tier":       "LOW",
                "containers": LOG_CONTAINERS,
                "source":     "portal",
            })
    except Exception:
        pass
    return StreamingResponse(
        _sse_log_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@router.get("/dashboard")
async def dashboard():
    """Serve sovereign-portal.html from the RAID read-only portal mount.

    Portal HTML is at /home/sovereign/portal/sovereign-portal.html,
    mounted into sovereign-core as :ro per compose.yml.
    """
    if not os.path.isfile(DASHBOARD_PATH):
        return JSONResponse(
            {"error": "Dashboard not found", "path": DASHBOARD_PATH},
            status_code=404,
        )
    return FileResponse(DASHBOARD_PATH, media_type="text/html")
