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

BROKER_URL         = os.environ.get("BROKER_URL", "http://docker-broker:8088")
OLLAMA_URL         = os.environ.get("OLLAMA_URL", "http://ollama:11434")
SOV_WALLET_URL     = os.environ.get("SOV_WALLET_URL", "http://sov-wallet:3001")
QDRANT_URL         = os.environ.get("QDRANT_URL", "http://qdrant-archive:6333")
# QDRANT_URL points to qdrant-archive (all 7 sovereign RAID collections).
# QDRANT_WM_URL points to the working_memory qdrant container (tmpfs).
QDRANT_WM_URL      = os.environ.get("QDRANT_WM_URL", "http://qdrant:6333")
WEBDAV_URL         = os.environ.get("WEBDAV_URL", "http://nextcloud:80/remote.php/dav/")
TELEGRAM_URL       = "https://api.telegram.org"
GROK_URL           = "https://api.x.ai/v1"
GEMINI_URL         = "https://generativelanguage.googleapis.com"
GROQ_URL           = "https://api.groq.com"
OPENROUTER_URL     = "https://openrouter.ai/api/v1"
OLLAMA_CLOUD_URL   = "https://ollama.com"

AUDIT_PATH    = "/home/sovereign/audit/security-ledger.jsonl"
SOVEREIGN_CONTAINERS = [
    "sovereign-core", "ollama", "whisper", "qdrant",
    "docker-broker", "gateway",
    "nextcloud", "nc-db", "nc-redis", "nginx",
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


def collect_temps() -> dict:
    """Read CPU and NVMe temps from hwmon sysfs. Paths are stable on this host."""
    result = {}
    sensors = {
        "cpu_c":  "/sys/class/hwmon/hwmon1/temp1_input",   # k10temp Tctl (AMD Ryzen)
        "nvme_c": "/sys/class/hwmon/hwmon0/temp1_input",   # NVMe composite
    }
    for key, path in sensors.items():
        try:
            result[key] = int(open(path).read().strip()) // 1000
        except Exception:
            result[key] = None
    return result


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
    """Get loaded models and last-inference latency.

    Generation probe uses a 6s timeout. qwen2.5:32b can take 30s+ when the GPU
    is already processing a real request; timing out returns status='busy' rather
    than blocking the entire /metrics endpoint.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r_tags = await client.get(f"{ollama_url}/api/tags")
            r_tags.raise_for_status()
            models = [m["name"] for m in r_tags.json().get("models", [])]

            # Latency probe — short timeout so busy GPU doesn't block /metrics
            t0 = time.monotonic()
            try:
                r_gen = await client.post(
                    f"{ollama_url}/api/generate",
                    json={"model": models[0] if models else "qwen2.5:32b-instruct-q4_K_M",
                          "prompt": "/no_think\n1", "stream": False},
                    timeout=6.0,
                )
                latency_ms = round((time.monotonic() - t0) * 1000, 1)
                inference_ok = r_gen.status_code == 200
                inference_status = "ok"
            except (httpx.TimeoutException, httpx.ReadTimeout):
                latency_ms = None
                inference_ok = True   # model is loaded; GPU is just busy
                inference_status = "busy"
        return {
            "models": models,
            "last_inference_latency_ms": latency_ms,
            "inference_ok": inference_ok,
            "inference_status": inference_status,
        }
    except Exception as e:
        return {"error": str(e)}


async def collect_qdrant(
        archive_url: str = QDRANT_URL,
        wm_url: str = QDRANT_WM_URL,
) -> dict:
    """Get point counts per collection from both qdrant instances.

    Returns {collection_name: {points_count: N}} matching the dashboard
    renderHeatmap() expectation. Queries qdrant-archive (7 RAID collections)
    and qdrant (working_memory, tmpfs).
    """
    result: dict = {}
    for url, label in [(archive_url, "archive"), (wm_url, "working")]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{url}/collections")
                r.raise_for_status()
                colls = r.json().get("result", {}).get("collections", [])
                for c in colls:
                    name = c["name"]
                    r2 = await client.get(f"{url}/collections/{name}")
                    info = r2.json().get("result", {})
                    result[name] = {"points_count": info.get("points_count", 0)}
        except Exception as e:
            if label == "working":
                result.setdefault("working_memory", {"points_count": 0, "error": str(e)})
    return result


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


async def collect_wallet(wallet_url: str = SOV_WALLET_URL) -> dict:
    """Query sov-wallet /health for chain connectivity state."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{wallet_url}/health")
            r.raise_for_status()
            data = r.json()
        chains = data.get("chains", {})
        failing = int(data.get("chains_failing", sum(1 for c in chains.values() if not c.get("connected", True))))
        return {"reachable": True, "chains": chains, "chains_failing": failing}
    except Exception as e:
        return {"reachable": False, "error": str(e), "chains_failing": 0}


async def _probe_gemini() -> tuple[str, dict]:
    key = os.environ.get("GEMINI_API_KEY", "")
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"{GEMINI_URL}/v1beta/models",
                params={"key": key} if key else {},
            )
        ok = r.status_code < 500
        return "gemini_api", {"reachable": ok, "status_code": r.status_code,
                               "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                               "key_set": bool(key)}
    except Exception as e:
        return "gemini_api", {"reachable": False, "error": str(e), "key_set": bool(key)}


async def _probe_groq() -> tuple[str, dict]:
    key = os.environ.get("GROQ_API_KEY", "")
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"{GROQ_URL}/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
        ok = r.status_code < 500
        return "groq_api", {"reachable": ok, "status_code": r.status_code,
                             "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                             "key_set": bool(key)}
    except Exception as e:
        return "groq_api", {"reachable": False, "error": str(e), "key_set": bool(key)}


async def _probe_openrouter() -> tuple[str, dict]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"{OPENROUTER_URL}/models",
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
        ok = r.status_code < 500
        return "openrouter_api", {"reachable": ok, "status_code": r.status_code,
                                   "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                                   "key_set": bool(key)}
    except Exception as e:
        return "openrouter_api", {"reachable": False, "error": str(e), "key_set": bool(key)}


async def _probe_ollama_cloud() -> tuple[str, dict]:
    key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"{OLLAMA_CLOUD_URL}/api/tags",
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
        ok = r.status_code < 500
        return "ollama_cloud_api", {"reachable": ok, "status_code": r.status_code,
                                     "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                                     "key_set": bool(key)}
    except Exception as e:
        return "ollama_cloud_api", {"reachable": False, "error": str(e), "key_set": bool(key)}


async def collect_external_reachability() -> dict:
    """Probe external services concurrently. Total time = slowest single probe."""
    results = await asyncio.gather(
        _probe_grok(),
        _probe_webdav(),
        _probe_telegram(),
        _probe_gemini(),
        _probe_groq(),
        _probe_openrouter(),
        _probe_ollama_cloud(),
        _reachable("https://api.anthropic.com", timeout=6.0),
        _reachable("http://172.16.201.4:8001/health", timeout=5.0),
        _reachable("http://172.16.201.4:8003/health", timeout=5.0),
        return_exceptions=True,
    )
    grok, webdav, telegram, gemini, groq, openrouter, ollama_cloud, claude_res, browser_res, whisper_res = results
    checks = {}
    for r in (grok, webdav, telegram, gemini, groq, openrouter, ollama_cloud):
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

    (containers, gpu, ollama_info, qdrant_info, external, wallet) = await asyncio.gather(
        collect_containers(),
        collect_gpu(),
        collect_ollama(),
        collect_qdrant(),
        collect_external_reachability(),
        collect_wallet(),
        return_exceptions=False,
    )

    ram = collect_host_memory()
    temps = collect_temps()
    audit = collect_audit_count()

    soul_status = {}
    if app_state:
        soul_status = {
            "guardian": "active",
            "soul_checksum": getattr(app_state, "soul_checksum", None),
        }

    iq = getattr(app_state, "inference_queue", None) if app_state else None
    if iq is not None:
        ollama_info["queue_depth"] = iq.queue_depth()
        ollama_info["queue_busy"]  = iq.is_busy()
        ollama_info["current_job"] = iq.current_job()

    exec_engine = getattr(app_state, "exec", None) if app_state else None
    loop_state = getattr(exec_engine, "_loop_state", {"active": False}) if exec_engine else {"active": False}

    return {
        "timestamp":    ts,
        "containers":   containers,
        "gpu":          gpu,
        "memory":       ram,
        "ollama":       ollama_info,
        "qdrant":       qdrant_info,
        "audit":        audit,
        "soul_guardian": soul_status,
        "external":     external,
        "wallet":       wallet,
        "temps":        temps,
        "loop_state":   loop_state,
    }
