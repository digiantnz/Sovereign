"""Sovereign self-monitoring — metrics collection.

Collects health data from all stack components. Called by /metrics endpoint
and the scheduled self-check. No LLM involvement — fully deterministic.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta

import httpx

BROKER_URL    = os.environ.get("BROKER_URL", "http://docker-broker:8088")
OLLAMA_URL    = os.environ.get("OLLAMA_URL", "http://ollama:11434")
QDRANT_URL    = os.environ.get("QDRANT_URL", "http://qdrant:6333")
WEBDAV_URL    = os.environ.get("WEBDAV_URL", "http://nextcloud:80/remote.php/dav/")
TELEGRAM_URL  = "https://api.telegram.org"
GROK_URL      = "https://api.x.ai/v1"

AUDIT_PATH    = "/home/sovereign/audit/security-ledger.jsonl"
SOVEREIGN_CONTAINERS = [
    "sovereign-core", "ollama", "whisper", "qdrant",
    "docker-broker", "gateway",
    "nextcloud", "nc-db", "nc-redis", "nextcloud-rp",
    "nanobot-01",
]  # a2a-browser + searxng removed 2026-03-19 (replaced by node04 172.16.201.4:8001)


async def _reachable(url: str, timeout: float = 5.0) -> tuple[bool, float]:
    """HEAD request to url. Returns (reachable, latency_ms)."""
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.head(url)
        return True, round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return False, -1.0


async def collect_containers(broker_url: str = BROKER_URL) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{broker_url}/containers/json",
                                 headers={"X-Trust-Level": "low"})
            r.raise_for_status()
            raw = r.json()
        # Normalise to simple status records
        result = []
        for c in raw:
            names = c.get("Names", [])
            name  = names[0].lstrip("/") if names else c.get("Id", "?")[:12]
            result.append({
                "name":   name,
                "status": c.get("State", c.get("Status", "unknown")),
                "image":  c.get("Image", ""),
            })
        return result
    except Exception as e:
        return [{"error": str(e)}]


async def collect_gpu(broker_url: str = BROKER_URL) -> dict:
    """Query broker GPU endpoint (runs nvidia-smi inside ollama container)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{broker_url}/system/gpu",
                                 headers={"X-Trust-Level": "low"})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


def collect_host_memory() -> dict:
    """Parse /proc/meminfo for RAM stats. Available inside container."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total_mb = info.get("MemTotal", 0) // 1024
        avail_mb = info.get("MemAvailable", 0) // 1024
        used_mb  = total_mb - avail_mb
        return {
            "total_mb": total_mb,
            "used_mb":  used_mb,
            "free_mb":  avail_mb,
            "percent":  round(used_mb / total_mb * 100, 1) if total_mb else 0,
        }
    except Exception as e:
        return {"error": str(e)}


async def collect_ollama(ollama_url: str = OLLAMA_URL) -> dict:
    """Get loaded models and last-inference latency."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r_tags = await client.get(f"{ollama_url}/api/tags")
            r_tags.raise_for_status()
            models = [m["name"] for m in r_tags.json().get("models", [])]

            # Latency probe — minimal generation
            t0 = time.monotonic()
            r_gen = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": models[0] if models else "llama3.1:8b-instruct-q4_K_M",
                      "prompt": "1", "stream": False},
                timeout=30.0,
            )
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            inference_ok = r_gen.status_code == 200
        return {
            "models": models,
            "last_inference_latency_ms": latency_ms,
            "inference_ok": inference_ok,
        }
    except Exception as e:
        return {"error": str(e)}


async def collect_qdrant(qdrant_url: str = QDRANT_URL) -> dict:
    """Get point counts and on-disk storage per collection."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{qdrant_url}/collections")
            r.raise_for_status()
            colls = r.json().get("result", {}).get("collections", [])
            details = []
            total_points = 0
            for c in colls:
                name = c["name"]
                r2 = await client.get(f"{qdrant_url}/collections/{name}")
                info = r2.json().get("result", {})
                points = info.get("points_count", 0)
                total_points += points
                details.append({"collection": name, "points": points})
        return {"collections": details, "total_points": total_points}
    except Exception as e:
        return {"error": str(e)}


def collect_audit_count(audit_path: str = AUDIT_PATH) -> dict:
    """Count audit log entries in the last 24 hours."""
    try:
        if not os.path.exists(audit_path):
            return {"last_24h_entries": 0}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        count = 0
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("ts", "")
                    if ts:
                        dt = datetime.fromisoformat(ts)
                        if dt > cutoff:
                            count += 1
                except Exception:
                    pass
        return {"last_24h_entries": count}
    except Exception as e:
        return {"error": str(e)}


async def _probe_grok() -> tuple[str, dict]:
    grok_key = os.environ.get("GROK_API_KEY", "")
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=6.0) as client:
            gr = await client.head(
                f"{GROK_URL}/models",
                headers={"Authorization": f"Bearer {grok_key}"} if grok_key else {},
            )
        grok_ok = gr.status_code < 500
        return "grok_api", {"reachable": grok_ok, "status_code": gr.status_code,
                             "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:
        return "grok_api", {"reachable": False, "error": str(e)}


async def _probe_webdav() -> tuple[str, dict]:
    webdav_user = os.environ.get("WEBDAV_USER", "")
    webdav_pass = os.environ.get("WEBDAV_PASS", "")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.request("PROPFIND", WEBDAV_URL,
                                     auth=(webdav_user, webdav_pass) if webdav_user else None,
                                     headers={"Depth": "0"})
            ok = r.status_code in (207, 200, 401)
            return "nextcloud_webdav", {"reachable": ok, "status_code": r.status_code}
    except Exception as e:
        return "nextcloud_webdav", {"reachable": False, "error": str(e)}


async def _probe_telegram() -> tuple[str, dict]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        ok, lat = await _reachable(f"https://api.telegram.org/bot{token}/getMe", timeout=6.0)
    else:
        ok, lat = False, -1.0
    return "telegram", {"reachable": ok, "latency_ms": lat}


async def collect_external_reachability() -> dict:
    """Probe external services concurrently. Total time = slowest single probe."""
    results = await asyncio.gather(
        _probe_grok(),
        _probe_webdav(),
        _probe_telegram(),
        _reachable("https://api.anthropic.com", timeout=6.0),
        _reachable("http://172.16.201.4:8001/health", timeout=5.0),
        _reachable("http://172.16.201.4:8003/health", timeout=5.0),
        return_exceptions=True,
    )
    grok, webdav, telegram, claude_res, browser_res, whisper_res = results
    checks = {}
    for r in (grok, webdav, telegram):
        if isinstance(r, tuple):
            checks[r[0]] = r[1]
    def _ok_lat(r, key):
        if isinstance(r, tuple): checks[key] = {"reachable": r[0], "latency_ms": r[1]}
        else: checks[key] = {"reachable": False, "error": str(r)}
    _ok_lat(claude_res,  "claude_api")
    _ok_lat(browser_res, "a2a_browser")
    _ok_lat(whisper_res, "a2a_whisper")
    return checks


async def collect_all(app_state=None) -> dict:
    """Collect all metrics concurrently. app_state from FastAPI app.state (optional)."""
    ts = datetime.now(timezone.utc).isoformat()

    (containers, gpu, ollama_info, qdrant_info, external) = await asyncio.gather(
        collect_containers(),
        collect_gpu(),
        collect_ollama(),
        collect_qdrant(),
        collect_external_reachability(),
        return_exceptions=False,
    )

    ram = collect_host_memory()
    audit = collect_audit_count()

    soul_status = {}
    if app_state:
        soul_status = {
            "guardian": "active",
            "soul_checksum": getattr(app_state, "soul_checksum", None),
        }

    return {
        "timestamp":    ts,
        "containers":   containers,
        "gpu":          gpu,
        "ram":          ram,
        "ollama":       ollama_info,
        "qdrant":       qdrant_info,
        "audit":        audit,
        "soul_guardian": soul_status,
        "external":     external,
    }
