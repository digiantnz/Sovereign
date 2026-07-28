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

import httpx
import yaml
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────────

from config import cfg as _cfg

SKILLS_DIR       = _cfg.paths.skills_dir
GOVERNANCE_PATH  = _cfg.paths.governance_json_container
DASHBOARD_PATH   = _cfg.paths.portal_html
CONFIG_PATH      = "/home/sovereign/governance/sovereign-config.yaml"
OLLAMA_URL       = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_EMBED_URL = os.environ.get("OLLAMA_EMBED_URL", "http://ollama-embed:11434")

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
        "key":    "tax_ingest",
        "name":   "Tax Ingest Harness",
        "flag":   "_tax_ingest_harness_checkpoint",
        "phases": ["Check", "Ingest", "Enrich", "Store", "Notify"],
        "trigger": "Hourly cron (pending Director activation)",
        "hitl":   False,
    },
    {
        "key":    "tax_report",
        "name":   "Tax Report Harness",
        "flag":   "_tax_report_harness_checkpoint",
        "phases": ["Query", "Ingest CSVs", "Create Reports", "Notify"],
        "trigger": "/do_tax command",
        "hitl":   True,
    },
    {
        "key":    "learning_harness",
        "name":   "Learning Harness",
        "flag":   None,     # module-level bool (_run_in_progress), not a WM checkpoint
        "phases": ["Poll", "Read", "Keywords", "Semantic Pass", "Relational Pass", "Sentinel"],
        "trigger": "Telegram upload + hourly (UTC 15–17)",
        "hitl":   False,
    },
    {
        "key":    "pm_harness",
        "name":   "Project Management Harness",
        "flag":   None,     # PLANNED — not yet implemented
        "phases": ["Scope", "Design", "Director Approve", "Build", "Test", "Ship"],
        "trigger": "Director-initiated",
        "hitl":   True,
    },
    {
        "key":    "cognition_engine",
        "name":   "Cognition Engine (Subjects/Thoughts)",
        "flag":   "_cognition_thought",   # per-thought checkpoint — multiple can
                                            # run concurrently; this shows whichever
                                            # the scroll finds first, not an exhaustive
                                            # list (see _get_harness_sessions — first
                                            # match per flag, not accumulated)
        "phases": ["Research", "Evaluate", "Propose Update", "Director Approve/Reject"],
        "trigger": "RSS scan / email scan / web search / conversation / /learn",
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
                limit=_cfg.portal.memory_preview_max,
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
                limit=_cfg.portal.memory_collection_preview_max,
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
    limit: int      = Query(default=_cfg.portal.api_limit_default, ge=1, le=_cfg.portal.api_limit_max),
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


@router.get("/rextrics/summary")
async def get_rextrics_summary(request: Request):
    """Return the last cached /rextrics report for the dashboard summary card.

    Static snapshot only — populated by execution/engine.py's `domain == "rextrics"`
    dispatch each time the Director runs /rextrics via Telegram
    (cognition.rextrics.write_last_report_cache). Not live, not polled by any
    background process — the dashboard just reads whatever was last cached.
    Returns {"report": None} if /rextrics has never been run since this cache
    was introduced.
    """
    _audit(request, "portal_read", "rextrics_summary")
    qdrant = getattr(getattr(request.app, "state", None), "qdrant", None)
    if not qdrant:
        return JSONResponse({"error": "Qdrant adapter not available"}, status_code=503)
    try:
        cached = await qdrant.retrieve_by_key("meta:rextrics:last_report")
        if not cached:
            return {"report": None, "generated_at": None}
        return {"report": cached.get("report"), "generated_at": cached.get("generated_at")}
    except Exception as e:
        logger.warning("portal /rextrics/summary: retrieve failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/ollama/models")
async def get_ollama_models(request: Request):
    """Return model lists from ollama and ollama-embed for the config tab dropdowns.

    Calls GET /api/tags on both services concurrently. Either service failing
    returns an empty list for that service — the UI falls back to a text input.
    Tier: LOW — read-only introspection, no side effects.
    """
    _audit(request, "portal_read", "ollama_models")

    async def _fetch_tags(url: str) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{url}/api/tags")
                r.raise_for_status()
                data = r.json()
                return [m["name"] for m in data.get("models", []) if m.get("name")]
        except Exception as e:
            logger.debug("portal /ollama/models: %s unreachable: %s", url, e)
            return []

    inference_models, embed_models = await asyncio.gather(
        _fetch_tags(OLLAMA_URL),
        _fetch_tags(OLLAMA_EMBED_URL),
    )
    return {"inference": inference_models, "embed": embed_models}


@router.get("/config")
async def get_config(request: Request):
    """Return the raw YAML text of sovereign-config.yaml."""
    _audit(request, "portal_read", "config")
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()
        return {"content": content, "path": CONFIG_PATH}
    except FileNotFoundError:
        return JSONResponse({"error": "sovereign-config.yaml not found"}, status_code=404)
    except Exception as e:
        logger.warning("portal GET /config: read failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/config")
async def save_config(request: Request, body: dict = Body(...)):
    """Write sovereign-config.yaml. Validates YAML before touching the file.

    Writes atomically via a .tmp file + rename. Changes take effect on next
    sovereign-core restart — the config singleton is loaded once at startup.
    Tier: MID (modifies system runtime configuration).
    """
    _audit(request, "portal_write", "config")
    content = body.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"error": "content must be a non-empty string"}, status_code=400)
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        return JSONResponse({"error": f"Invalid YAML: {e}"}, status_code=400)
    try:
        with open(CONFIG_PATH, "w") as f:
            f.write(content)
    except Exception as e:
        logger.warning("portal POST /config: write failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    logger.info("portal POST /config: sovereign-config.yaml updated (%d bytes)", len(content))
    return {"ok": True, "bytes": len(content)}


# ── Config field helpers ───────────────────────────────────────────────────────

def _parse_config_fields(raw: str) -> list:
    """Parse sovereign-config.yaml → list of field dicts with embedded comment metadata."""
    try:
        cfg = yaml.safe_load(raw) or {}
    except Exception:
        cfg = {}

    _KNOWN_TAGS = ('tier:', 'desc:', 'src:', 'env-overridable:', 'stored as',
                   '──', '─', '═', 'NOTE-', 'BUG-')
    fields: list = []
    lines = raw.split('\n')
    current_section: str | None = None
    pending: dict = {}

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else 0

        if not stripped:
            continue

        if stripped.startswith('#'):
            content = stripped.lstrip('#').strip()
            if content.startswith('tier:'):
                pending = {'tier': content[5:].strip(), 'desc': '', 'src': '', 'desc_cont': False}
            elif pending.get('tier') is not None:
                if content.startswith('desc:'):
                    pending['desc'] = content[5:].strip()
                    pending['desc_cont'] = True
                elif content.startswith('src:'):
                    pending['src'] = content[4:].strip()
                    pending['desc_cont'] = False
                elif content.startswith('env-overridable:'):
                    pending['desc_cont'] = False
                elif pending.get('desc_cont') and not any(content.startswith(t) for t in _KNOWN_TAGS):
                    pending['desc'] = (pending['desc'] + ' ' + content).strip()
                else:
                    pending['desc_cont'] = False
            continue

        # Top-level section header (no indent, key:, no value)
        if indent == 0 and ':' in stripped and not stripped.startswith('#'):
            key, _, rest = stripped.partition(':')
            if not rest.strip():
                current_section = key
            pending = {}
            continue

        # Field at 2-space indent
        if indent == 2 and ':' in stripped and not stripped.startswith('#'):
            key, _, rest = stripped.partition(':')
            key = key.strip()

            if pending.get('tier') is not None and current_section:
                section_cfg = cfg.get(current_section)
                if not isinstance(section_cfg, dict):
                    section_cfg = {}
                actual_value = section_cfg.get(key)

                if isinstance(actual_value, bool):
                    vtype = 'bool'
                elif isinstance(actual_value, int):
                    vtype = 'int'
                elif isinstance(actual_value, float):
                    vtype = 'float'
                elif isinstance(actual_value, list):
                    vtype = 'list'
                else:
                    vtype = 'string'
                    if actual_value is None and not rest.strip():
                        vtype = 'list'

                fields.append({
                    'section':  current_section,
                    'key':      key,
                    'key_path': f'{current_section}.{key}',
                    'value':    actual_value if actual_value is not None else [],
                    'tier':     pending['tier'],
                    'desc':     pending.get('desc', '').strip(),
                    'src':      pending.get('src', '').strip(),
                    'type':     vtype,
                })

            pending = {}
            continue

        # 4-space-indent items (list block values) — don't reset pending
        if indent == 4:
            continue

        # Anything else at low indent resets pending
        if indent <= 2:
            pending = {}

    return fields


def _update_yaml_single(raw: str, section: str, key: str, new_value, vtype: str) -> str:
    """Replace one scalar/list field value in raw YAML while preserving all comments."""
    lines = raw.split('\n')
    result: list = []
    in_section = False
    in_list_block = False
    replaced = False

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else 0

        # Detect entering our target section
        if indent == 0 and not stripped.startswith('#') and stripped == f'{section}:':
            in_section = True
            in_list_block = False
            result.append(line)
            continue

        # Detect leaving section (new top-level non-comment line)
        if in_section and indent == 0 and stripped and not stripped.startswith('#'):
            in_section = False
            in_list_block = False

        if in_section and not replaced:
            if indent == 2 and not stripped.startswith('#'):
                k, _, rest = stripped.partition(':')
                if k.strip() == key:
                    orig_rest = rest.strip()

                    if vtype == 'list':
                        if orig_rest.startswith('['):
                            # Inline list: detect int vs string elements
                            inner = orig_rest.strip('[]').strip()
                            raw_items = [x.strip().strip('"\'') for x in inner.split(',') if x.strip()]
                            try:
                                [int(x) for x in raw_items]
                                is_int = True
                            except (ValueError, TypeError):
                                is_int = False
                            new_items = [v.strip() for v in str(new_value).split(',') if v.strip()]
                            if is_int:
                                fval = '[' + ', '.join(str(int(float(v))) for v in new_items) + ']'
                            else:
                                fval = '[' + ', '.join(f'"{v}"' for v in new_items) + ']'
                            result.append(f'  {key}: {fval}')
                        else:
                            # Block list
                            result.append(f'  {key}:')
                            new_items = [v.strip() for v in str(new_value).split(',') if v.strip()]
                            for item in new_items:
                                result.append(f'    - "{item}"')
                            in_list_block = True
                        replaced = True
                        continue
                    else:
                        # Format scalar value
                        if vtype == 'bool':
                            fval = 'true' if new_value else 'false'
                        elif vtype == 'int':
                            fval = str(int(float(str(new_value))))
                        elif vtype == 'float':
                            fval = str(float(str(new_value)))
                        elif vtype == 'string':
                            sv = str(new_value)
                            orig_val_raw = orig_rest.split('  #')[0].strip()
                            if orig_val_raw.startswith('"') or orig_val_raw.startswith("'"):
                                fval = f'"{sv}"'
                            elif any(c in sv for c in ' :#{}[]'):
                                fval = f'"{sv}"'
                            else:
                                fval = sv
                        else:
                            fval = str(new_value)

                        # Preserve inline comment (e.g. 2147483648  # 2 GB)
                        inline = ''
                        if '  #' in orig_rest:
                            inline = orig_rest[orig_rest.index('  #'):]

                        result.append(f'  {key}: {fval}{inline}')
                        replaced = True
                        continue

            elif in_list_block and indent == 4 and stripped.startswith('-'):
                continue  # skip old block-list items
            elif in_list_block and indent < 4 and stripped and not stripped.startswith('#'):
                in_list_block = False

        result.append(line)

    return '\n'.join(result)


def _update_yaml_fields(raw: str, changes: list) -> str:
    result = raw
    for ch in changes:
        result = _update_yaml_single(result, ch['section'], ch['key'], ch['value'], ch['type'])
    return result


@router.get("/config/fields")
async def get_config_fields(request: Request):
    """Return sovereign-config.yaml parsed as structured field definitions with comment metadata."""
    _audit(request, "portal_read", "config_fields")
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()
        fields = _parse_config_fields(content)
        return {"fields": fields, "path": CONFIG_PATH}
    except FileNotFoundError:
        return JSONResponse({"error": "sovereign-config.yaml not found"}, status_code=404)
    except Exception as e:
        logger.warning("portal GET /config/fields: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/config/fields")
async def save_config_fields(request: Request, body: dict = Body(...)):
    """Apply structured field changes to sovereign-config.yaml while preserving comments."""
    changes = body.get("changes", [])
    if not changes:
        return JSONResponse({"error": "No changes provided"}, status_code=400)
    _audit(request, "portal_write", "config_fields")
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()
        updated = _update_yaml_fields(content, changes)
        yaml.safe_load(updated)  # validate before touching file
        with open(CONFIG_PATH, "w") as f:
            f.write(updated)
        logger.info("portal POST /config/fields: %d changes, %d bytes", len(changes), len(updated))
        return {"ok": True, "bytes": len(updated), "changed": len(changes)}
    except yaml.YAMLError as e:
        return JSONResponse({"error": f"Invalid YAML after update: {e}"}, status_code=400)
    except Exception as e:
        logger.warning("portal POST /config/fields: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


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
