"""Nanobot-01 bridge server — REST interface for Sovereign Core.

Translates Rex's {skill, action, params, context} dispatch format into
either a deterministic DSL operation (Stage 3) or a nanobot LLM task (fallback).

Stage 3 DSL path: if the skill's SKILL.md declares an operations: block in
its frontmatter AND the requested action is listed there, dispatch directly
using native Python (no Ollama, no nanobot CLI). path: "dsl" in response.

LLM path: existing nanobot agent --message flow. path: "llm" in response.

Rex talks to this server at http://nanobot-01:8080.
This server has no knowledge of secrets, governance, or sovereign memory.
It receives work. It returns results. That is all.
"""

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import uuid
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [nanobot-01] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="nanobot-01", docs_url=None, redoc_url=None)

SKILLS_DIR   = os.environ.get("SKILLS_DIR",   "/skills")
MEMORY_DIR   = os.environ.get("MEMORY_DIR",   "/memory")
WORKSPACE    = os.environ.get("NANOBOT_WORKSPACE", "/workspace")
NANOBOT_CONFIG = os.environ.get("NANOBOT_CONFIG", "/workspace/.nanobot/config.json")
TASK_TIMEOUT = int(os.environ.get("NANOBOT_TASK_TIMEOUT", "25"))

# ---------------------------------------------------------------------------
# Filesystem path allowlists — nanobot-01 can read/write these paths
# ---------------------------------------------------------------------------
_ALLOWED_RW = [
    pathlib.Path(WORKSPACE).resolve(),
    pathlib.Path(MEMORY_DIR).resolve(),
]
_ALLOWED_RO = [
    pathlib.Path(SKILLS_DIR).resolve(),
]

# Shell command allowlist — only these commands may be exec'd
_EXEC_ALLOWLIST = {
    "df", "du", "ls", "find", "cat", "head", "tail", "wc",
    "grep", "date", "pwd", "stat", "echo", "sort", "uniq",
    "python3", "cp", "mv", "mkdir", "rm", "chmod", "touch",
}

# DSL frontmatter cache: skill_name -> (mtime, operations_dict | None)
_dsl_cache: dict[str, tuple[float, dict | None]] = {}


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class TaskRequest(BaseModel):
    skill:   str
    action:  str
    params:  dict[str, Any] = {}
    context: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# DSL infrastructure
# ---------------------------------------------------------------------------

def _load_operations(skill_name: str) -> dict | None:
    """Load and cache the operations: DSL block from a skill's SKILL.md.

    Returns the operations dict (action -> spec) or None if not present.
    Result is mtime-invalidated so hot-reloads work without restart.
    """
    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        mtime = os.path.getmtime(skill_path)
    except OSError:
        return None

    cached = _dsl_cache.get(skill_name)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(skill_path) as f:
            raw = f.read()
        parts = raw.split("---", 2)
        if len(parts) < 3:
            _dsl_cache[skill_name] = (mtime, None)
            return None
        fm = yaml.safe_load(parts[1]) or {}
        ops = fm.get("sovereign", {}).get("operations")
        _dsl_cache[skill_name] = (mtime, ops)
        return ops
    except Exception as e:
        logger.warning(f"_load_operations({skill_name}): {e}")
        _dsl_cache[skill_name] = (mtime, None)
        return None


def _validate_params(op_spec: dict, params: dict) -> tuple[dict, list[str]]:
    """Type-coerce and validate params against op_spec.

    Returns (validated_params, errors). On any error, do not execute.
    """
    param_spec = op_spec.get("params", {})
    validated: dict[str, Any] = {}
    errors: list[str] = []

    for name, spec in param_spec.items():
        typ      = spec.get("type", "str")
        required = spec.get("required", True)
        default  = spec.get("default")

        if name in params:
            raw = params[name]
            try:
                if typ == "int":
                    validated[name] = int(raw)
                elif typ == "float":
                    validated[name] = float(raw)
                elif typ == "bool":
                    validated[name] = raw if isinstance(raw, bool) else str(raw).lower() in ("true", "1", "yes")
                elif typ == "dict":
                    if not isinstance(raw, dict):
                        errors.append(f"{name}: expected dict, got {type(raw).__name__}")
                    else:
                        validated[name] = raw
                elif typ == "list":
                    if not isinstance(raw, list):
                        errors.append(f"{name}: expected list, got {type(raw).__name__}")
                    else:
                        validated[name] = raw
                else:
                    validated[name] = str(raw)
            except (ValueError, TypeError) as e:
                errors.append(f"{name}: cannot coerce to {typ}: {e}")
        elif required and default is None:
            errors.append(f"{name}: required param missing")
        elif default is not None:
            validated[name] = default

    return validated, errors


def _check_path(path_str: str, allow_write: bool = False) -> tuple[pathlib.Path | None, str | None]:
    """Resolve path and verify it is inside the allowed directory tree."""
    try:
        p = pathlib.Path(path_str).resolve()
    except Exception as e:
        return None, f"invalid path: {e}"

    rw_ok = any(str(p).startswith(str(base)) for base in _ALLOWED_RW)
    ro_ok = any(str(p).startswith(str(base)) for base in _ALLOWED_RO)

    if allow_write and not rw_ok:
        return p, f"write not permitted outside {[str(b) for b in _ALLOWED_RW]}"
    if not rw_ok and not ro_ok:
        return p, f"path not in allowed directories (rw: {[str(b) for b in _ALLOWED_RW]}, ro: {[str(b) for b in _ALLOWED_RO]})"
    return p, None


def _dispatch_filesystem(action: str, params: dict, run_id: str) -> dict:
    """Native Python filesystem dispatch — no subprocess, no Ollama."""
    path_str = params.get("path", WORKSPACE)

    if action == "list":
        p, err = _check_path(path_str)
        if err:
            return {"status": "error", "path": "dsl", "error": err}
        if not p.is_dir():
            return {"status": "error", "path": "dsl", "error": f"not a directory: {path_str}"}
        items = []
        for child in sorted(p.iterdir()):
            s = child.stat()
            items.append({
                "name":     child.name,
                "path":     str(child),
                "type":     "dir" if child.is_dir() else "file",
                "size":     s.st_size,
                "modified": s.st_mtime,
            })
        logger.info(f"[{run_id}] DSL filesystem.list({path_str}) → {len(items)} items")
        return {"status": "ok", "path": "dsl", "items": items, "count": len(items)}

    elif action == "read":
        p, err = _check_path(path_str)
        if err:
            return {"status": "error", "path": "dsl", "error": err}
        if not p.is_file():
            return {"status": "error", "path": "dsl", "error": f"not a file: {path_str}"}
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            logger.info(f"[{run_id}] DSL filesystem.read({path_str}) → {len(content)} chars")
            return {"status": "ok", "path": "dsl", "content": content, "size": len(content)}
        except Exception as e:
            return {"status": "error", "path": "dsl", "error": f"read failed: {e}"}

    elif action == "write":
        content = params.get("content", "")
        p, err = _check_path(path_str, allow_write=True)
        if err:
            return {"status": "error", "path": "dsl", "error": err}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            logger.info(f"[{run_id}] DSL filesystem.write({path_str}) → {len(content)} chars")
            return {"status": "ok", "path": "dsl", "written": True, "size": len(content)}
        except Exception as e:
            return {"status": "error", "path": "dsl", "error": f"write failed: {e}"}

    elif action == "append":
        content = params.get("content", "")
        p, err = _check_path(path_str, allow_write=True)
        if err:
            return {"status": "error", "path": "dsl", "error": err}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"[{run_id}] DSL filesystem.append({path_str}) → {len(content)} chars appended")
            return {"status": "ok", "path": "dsl", "appended": True, "size": len(content)}
        except Exception as e:
            return {"status": "error", "path": "dsl", "error": f"append failed: {e}"}

    else:
        return {"status": "error", "path": "dsl", "error": f"unknown filesystem action: {action!r}"}


def _dispatch_exec(params: dict, run_id: str) -> dict:
    """Native subprocess dispatch with strict command allowlist."""
    command = params.get("command", "").strip()
    timeout = int(params.get("timeout", 20))

    if not command:
        return {"status": "error", "path": "dsl", "error": "empty command"}

    cmd0 = os.path.basename(command.split()[0])
    if cmd0 not in _EXEC_ALLOWLIST:
        return {
            "status": "error", "path": "dsl",
            "error": f"command '{cmd0}' not in exec allowlist — allowed: {sorted(_EXEC_ALLOWLIST)}",
        }

    logger.info(f"[{run_id}] DSL exec: {command[:120]}")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE,
        )
        return {
            "status":    "ok",
            "path":      "dsl",
            "exit_code": proc.returncode,
            "stdout":    proc.stdout[:4000],
            "stderr":    proc.stderr[:500],
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "path": "dsl", "error": f"exec timed out after {timeout}s"}
    except Exception as e:
        return {"status": "error", "path": "dsl", "error": f"exec failed: {e}"}


def _dispatch_dsl(op_spec: dict, params: dict, run_id: str) -> dict:
    """Route a validated DSL operation to the correct native handler."""
    tool   = op_spec.get("tool", "").lower()
    action = op_spec.get("action", "").lower()

    if tool == "filesystem":
        return _dispatch_filesystem(action, params, run_id)
    elif tool == "exec":
        return _dispatch_exec(params, run_id)
    else:
        return {
            "status": "error", "path": "dsl",
            "error": f"tool '{tool}' not handled by nanobot-01 (must be filesystem or exec); "
                     f"sovereign adapters (imap/webdav/caldav) are dispatched by NanobotAdapter directly",
        }


# ---------------------------------------------------------------------------
# LLM path — unchanged from Stage 2
# ---------------------------------------------------------------------------

def _build_prompt(req: TaskRequest) -> str:
    """Build a nanobot task prompt from Rex's dispatch format."""
    skill_path = os.path.join(SKILLS_DIR, req.skill, "SKILL.md")
    skill_body = ""
    if os.path.isfile(skill_path):
        try:
            with open(skill_path) as f:
                raw = f.read()
            parts = raw.split("---", 2)
            skill_body = parts[2].strip() if len(parts) >= 3 else raw.strip()
        except Exception as e:
            logger.warning(f"Could not read skill {req.skill}: {e}")

    lines = [
        "TASK DISPATCH FROM SOVEREIGN CORE",
        f"Skill: {req.skill}",
        f"Action: {req.action}",
        f"Parameters: {json.dumps(req.params, indent=2)}",
    ]
    if req.context:
        lines.append(f"Context: {json.dumps(req.context, indent=2)}")
    if skill_body:
        lines.append(f"\nSkill Reference:\n{skill_body[:2000]}")
    lines.append(
        "\nExecute the requested action. "
        "Respond with a JSON object containing: status (ok|error), result (your output), "
        "and optionally notes (observations or caveats)."
    )
    return "\n".join(lines)


def _run_nanobot(prompt: str, run_id: str) -> dict:
    """Run nanobot CLI with the constructed prompt. Returns structured dict."""
    cmd = [
        "nanobot", "agent",
        "--message", prompt,
        "--workspace", WORKSPACE,
        "--config", NANOBOT_CONFIG,
        "--no-markdown",
        "--no-logs",
    ]
    logger.info(f"[{run_id}] LLM path: launching nanobot (prompt {len(prompt)} chars)")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TASK_TIMEOUT,
            cwd=WORKSPACE,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode != 0:
            logger.warning(f"[{run_id}] nanobot exit {proc.returncode}: {stderr[:200]}")
            return {
                "status":     "error",
                "path":       "llm",
                "exit_code":  proc.returncode,
                "error":      stderr[:500] or f"nanobot exited {proc.returncode}",
                "raw_stdout": stdout[:200] if stdout else "",
            }

        if stdout:
            for candidate in reversed(stdout.split("\n")):
                candidate = candidate.strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    try:
                        parsed = json.loads(candidate)
                        logger.info(f"[{run_id}] LLM path: returned structured JSON")
                        return {"status": "ok", "path": "llm", "result": parsed}
                    except json.JSONDecodeError:
                        pass
            return {"status": "ok", "path": "llm", "result": {"raw": stdout[:2000]}}

        return {"status": "ok", "path": "llm", "result": {"raw": "(no output)"}}

    except subprocess.TimeoutExpired:
        logger.warning(f"[{run_id}] nanobot task timed out after {TASK_TIMEOUT}s")
        return {"status": "error", "path": "llm", "error": f"task timed out after {TASK_TIMEOUT}s"}
    except FileNotFoundError:
        logger.error(f"[{run_id}] nanobot CLI not found — check container installation")
        return {"status": "error", "path": "llm", "error": "nanobot CLI not found in PATH"}
    except Exception as e:
        logger.exception(f"[{run_id}] unexpected error running nanobot")
        return {"status": "error", "path": "llm", "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check — includes DSL skill summary."""
    config_exists   = os.path.isfile(NANOBOT_CONFIG)
    skills_readable = os.path.isdir(SKILLS_DIR)

    try:
        proc = subprocess.run(["nanobot", "-v"], capture_output=True, text=True, timeout=5)
        nanobot_version = proc.stdout.strip() or proc.stderr.strip()
        nanobot_ok = proc.returncode == 0
    except Exception as e:
        nanobot_version = str(e)
        nanobot_ok = False

    # Summarise DSL-enabled skills
    dsl_skills: dict[str, list[str]] = {}
    try:
        for skill_name in os.listdir(SKILLS_DIR):
            ops = _load_operations(skill_name)
            if ops:
                dsl_skills[skill_name] = list(ops.keys())
    except Exception:
        pass

    status = "ok" if (config_exists and skills_readable and nanobot_ok) else "degraded"
    return {
        "status":          status,
        "nanobot_version": nanobot_version,
        "nanobot_ok":      nanobot_ok,
        "config_exists":   config_exists,
        "skills_readable": skills_readable,
        "skills_dir":      SKILLS_DIR,
        "memory_dir":      MEMORY_DIR,
        "workspace":       WORKSPACE,
        "dsl_skills":      dsl_skills,
        "dsl_operations_total": sum(len(v) for v in dsl_skills.values()),
    }


@app.post("/run")
async def run_task(req: TaskRequest):
    """Execute a sovereign skill task.

    Stage 3: checks SKILL.md operations: DSL first.
      - If action matches DSL with tool=filesystem|exec → native Python dispatch (no Ollama).
      - If action matches DSL with tool=imap|webdav|caldav → error (those are sovereign-side).
      - If no DSL match → LLM path via nanobot agent CLI.

    Called by NanobotAdapter in sovereign-core. Governance already applied upstream.
    No secrets in the request. No governance decisions happen here.
    """
    run_id = str(uuid.uuid4())[:8]
    logger.info(f"[{run_id}] task: skill={req.skill} action={req.action}")

    # --- Stage 3: DSL dispatch path ---
    operations = _load_operations(req.skill)
    if operations and req.action in operations:
        op_spec = operations[req.action]
        tool    = op_spec.get("tool", "")

        validated, errors = _validate_params(op_spec, req.params)
        if errors:
            logger.warning(f"[{run_id}] DSL param validation failed: {errors}")
            return JSONResponse(content={
                "run_id": run_id, "skill": req.skill, "action": req.action,
                "status": "error", "step": "param_validation", "errors": errors, "path": "dsl",
            })

        logger.info(f"[{run_id}] DSL path: tool={tool} action={op_spec.get('action')}")
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _dispatch_dsl(op_spec, validated, run_id))
        return JSONResponse(content={
            "run_id": run_id, "skill": req.skill, "action": req.action, **result,
        })

    # --- LLM fallback path ---
    logger.info(f"[{run_id}] LLM path: no DSL match for {req.skill}/{req.action}")
    prompt = _build_prompt(req)
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: _run_nanobot(prompt, run_id))
    return JSONResponse(content={
        "run_id": run_id, "skill": req.skill, "action": req.action, **result,
    })
