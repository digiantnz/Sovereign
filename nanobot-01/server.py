"""Nanobot-01 bridge server — REST interface for Sovereign Core.

Translates Rex's {skill, action, params, context} dispatch format into
nanobot CLI tasks. Returns structured JSON results.

Rex talks to this server at http://nanobot-01:8080.
This server has no knowledge of secrets, governance, or sovereign memory.
It receives work. It returns results. That is all.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [nanobot-01] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="nanobot-01", docs_url=None, redoc_url=None)

SKILLS_DIR = os.environ.get("SKILLS_DIR", "/skills")
MEMORY_DIR = os.environ.get("MEMORY_DIR", "/memory")
WORKSPACE = os.environ.get("NANOBOT_WORKSPACE", "/workspace")
NANOBOT_CONFIG = os.environ.get("NANOBOT_CONFIG", "/workspace/.nanobot/config.json")
TASK_TIMEOUT = int(os.environ.get("NANOBOT_TASK_TIMEOUT", "25"))


class TaskRequest(BaseModel):
    skill: str
    action: str
    params: dict[str, Any] = {}
    context: dict[str, Any] = {}


def _build_prompt(req: TaskRequest) -> str:
    """Build a nanobot task prompt from Rex's dispatch format."""
    skill_path = os.path.join(SKILLS_DIR, req.skill, "SKILL.md")
    skill_body = ""
    if os.path.isfile(skill_path):
        try:
            with open(skill_path) as f:
                raw = f.read()
            # Strip YAML frontmatter — body starts after second ---
            parts = raw.split("---", 2)
            skill_body = parts[2].strip() if len(parts) >= 3 else raw.strip()
        except Exception as e:
            logger.warning(f"Could not read skill {req.skill}: {e}")

    lines = [
        f"TASK DISPATCH FROM SOVEREIGN CORE",
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
    logger.info(f"[{run_id}] Launching nanobot: skill prompt length {len(prompt)}")
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
                "status": "error",
                "exit_code": proc.returncode,
                "error": stderr[:500] or f"nanobot exited {proc.returncode}",
                "raw_stdout": stdout[:200] if stdout else "",
            }

        # Try to parse structured JSON from stdout
        if stdout:
            # nanobot may wrap output; find the last JSON object in output
            for candidate in reversed(stdout.split("\n")):
                candidate = candidate.strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    try:
                        parsed = json.loads(candidate)
                        logger.info(f"[{run_id}] nanobot returned structured JSON")
                        return {"status": "ok", "result": parsed}
                    except json.JSONDecodeError:
                        pass
            # Return raw text as result
            return {"status": "ok", "result": {"raw": stdout[:2000]}}

        return {"status": "ok", "result": {"raw": "(no output)"}}

    except subprocess.TimeoutExpired:
        logger.warning(f"[{run_id}] nanobot task timed out after {TASK_TIMEOUT}s")
        return {"status": "error", "error": f"task timed out after {TASK_TIMEOUT}s"}
    except FileNotFoundError:
        logger.error(f"[{run_id}] nanobot CLI not found — check container installation")
        return {"status": "error", "error": "nanobot CLI not found in PATH"}
    except Exception as e:
        logger.exception(f"[{run_id}] unexpected error running nanobot")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.get("/health")
async def health():
    """Health check — sovereign-core calls this before dispatching tasks."""
    config_exists = os.path.isfile(NANOBOT_CONFIG)
    skills_readable = os.path.isdir(SKILLS_DIR)
    # Quick nanobot CLI check
    try:
        proc = subprocess.run(["nanobot", "-v"], capture_output=True, text=True, timeout=5)
        nanobot_version = proc.stdout.strip() or proc.stderr.strip()
        nanobot_ok = proc.returncode == 0
    except Exception as e:
        nanobot_version = str(e)
        nanobot_ok = False

    status = "ok" if (config_exists and skills_readable and nanobot_ok) else "degraded"
    return {
        "status": status,
        "nanobot_version": nanobot_version,
        "nanobot_ok": nanobot_ok,
        "config_exists": config_exists,
        "skills_readable": skills_readable,
        "skills_dir": SKILLS_DIR,
        "memory_dir": MEMORY_DIR,
        "workspace": WORKSPACE,
    }


@app.post("/run")
async def run_task(req: TaskRequest):
    """Execute a sovereign skill task via nanobot.

    Called by NanobotAdapter in sovereign-core. Rex has already applied
    governance (MID tier minimum) before reaching this endpoint.
    No secrets are in the request. No governance decisions happen here.
    """
    run_id = str(uuid.uuid4())[:8]
    logger.info(f"[{run_id}] Received task: skill={req.skill} action={req.action}")

    prompt = _build_prompt(req)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: _run_nanobot(prompt, run_id))

    return JSONResponse(content={
        "run_id": run_id,
        "skill": req.skill,
        "action": req.action,
        **result,
    })
