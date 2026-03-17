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

import httpx
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


class TranslateRequest(BaseModel):
    content: str          # raw SKILL.md text (any format)
    name:    str = ""     # hint for skill name if not parseable from content


# ---------------------------------------------------------------------------
# Skill format translation — OpenClaw → Sovereign DSL
# ---------------------------------------------------------------------------

def _parse_skill_md(content: str) -> tuple[dict, str]:
    """Split raw skill content into (frontmatter_dict, body). Returns ({}, content) on failure."""
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        fm = {}
    return fm, parts[2].strip()


def _detect_skill_category(fm: dict, body: str) -> str:
    """Infer skill category from frontmatter metadata and body content."""
    # OpenClaw: metadata.openclaw.requires.env tells us what credentials are needed
    oc_meta = (fm.get("metadata") or {}).get("openclaw") or {}
    req_env = (oc_meta.get("requires") or {}).get("env") or []
    env_str = " ".join(req_env).lower()
    body_lower = (body or "").lower()

    if any(k in env_str for k in ("imap_host", "smtp_host")):
        return "imap_smtp"
    if any(k in env_str for k in ("nextcloud_url", "nextcloud_user")):
        return "nextcloud"

    # Explicit OpenClaw category field
    oc_category = (oc_meta.get("category") or "").lower()
    if oc_category in ("rss", "feeds", "rss_feeds"):
        return "rss_feeds"

    # Fallback: check body for strong signals
    name = (fm.get("name") or "").lower()
    desc = (fm.get("description") or "").lower()
    hints = name + " " + desc + " " + body_lower[:500]
    if "imap" in hints and "smtp" in hints:
        return "imap_smtp"
    if "nextcloud" in hints and ("caldav" in hints or "webdav" in hints):
        return "nextcloud"
    # RSS/feeds signals: feed binary, feedparser, rss/atom feed patterns
    rss_signals = ("feed get entries", "feed get entry", "feed cli", "rss digest",
                   "rss feed", "atom feed", "feedparser", "rss-digest")
    if any(s in hints for s in rss_signals):
        return "rss_feeds"
    if "rss" in name or "feed" in name or "digest" in name:
        return "rss_feeds"
    return "unknown"


def _translate_imap_smtp() -> dict:
    """Generate Sovereign sovereign: block for an IMAP/SMTP community skill.

    Operations use python3_exec with imap_check.py / smtp_send.py scripts
    deployed at workspace/skills/<name>/scripts/.
    No broker dependency — nanobot-01 handles all email operations natively.
    """
    return {
        "specialists": ["business_agent"],
        "tier_required": "LOW",
        "adapter_deps": ["nanobot"],
        "description": "IMAP email read/search/flag and SMTP send via Python scripts. Personal and business accounts.",
        "operations": {
            "fetch_unread": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "check", "--account", "business"],
                "tier": "LOW",
                "params": {
                    "limit": {"type": "int", "required": False, "default": 10},
                },
                "returns": "{messages: [{uid, from, subject, date}], count}",
            },
            "fetch_unread_personal": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "check", "--account", "personal"],
                "tier": "LOW",
                "params": {
                    "limit": {"type": "int", "required": False, "default": 10},
                },
                "returns": "{messages: [{uid, from, subject, date}], count}",
            },
            "fetch_message": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "fetch", "--account", "business"],
                "tier": "LOW",
                "params": {
                    "uid": {"type": "str", "required": True},
                },
                "returns": "{uid, from, subject, date, body}",
            },
            "fetch_message_personal": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "fetch", "--account", "personal"],
                "tier": "LOW",
                "params": {
                    "uid": {"type": "str", "required": True},
                },
                "returns": "{uid, from, subject, date, body}",
            },
            "search": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "search", "--account", "business"],
                "tier": "LOW",
                "params": {
                    "query":     {"type": "str", "required": False, "default": ""},
                    "from_addr": {"type": "str", "required": False, "default": ""},
                    "subject":   {"type": "str", "required": False, "default": ""},
                    "since":     {"type": "str", "required": False, "default": ""},
                    "limit":     {"type": "int", "required": False, "default": 20},
                },
                "returns": "{messages: [{uid, from, subject, date}], count}",
            },
            "search_personal": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "search", "--account", "personal"],
                "tier": "LOW",
                "params": {
                    "query":     {"type": "str", "required": False, "default": ""},
                    "from_addr": {"type": "str", "required": False, "default": ""},
                    "subject":   {"type": "str", "required": False, "default": ""},
                    "since":     {"type": "str", "required": False, "default": ""},
                    "limit":     {"type": "int", "required": False, "default": 20},
                },
                "returns": "{messages: [{uid, from, subject, date}], count}",
            },
            "mark_read": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "mark-read", "--account", "business"],
                "tier": "LOW",
                "params": {
                    "uid": {"type": "str", "required": True},
                },
                "returns": "{status, uid}",
            },
            "mark_unread": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "mark-unread", "--account", "personal"],
                "tier": "LOW",
                "params": {
                    "uid": {"type": "str", "required": True},
                },
                "returns": "{status, uid}",
            },
            "list_folders": {
                "tool": "python3_exec",
                "script": "scripts/imap_check.py",
                "args": ["--command", "list-mailboxes", "--account", "personal"],
                "tier": "LOW",
                "params": {},
                "returns": "{mailboxes: [...], count}",
            },
            "send_email": {
                "tool": "python3_exec",
                "script": "scripts/smtp_send.py",
                "args": ["--account", "business"],
                "tier": "MID",
                "params": {
                    "to":      {"type": "str", "required": True},
                    "subject": {"type": "str", "required": True},
                    "body":    {"type": "str", "required": True},
                    "cc":      {"type": "str", "required": False, "default": ""},
                },
                "returns": "{status, message_id}",
            },
            "send_email_personal": {
                "tool": "python3_exec",
                "script": "scripts/smtp_send.py",
                "args": ["--account", "personal"],
                "tier": "MID",
                "params": {
                    "to":      {"type": "str", "required": True},
                    "subject": {"type": "str", "required": True},
                    "body":    {"type": "str", "required": True},
                    "cc":      {"type": "str", "required": False, "default": ""},
                },
                "returns": "{status, message_id}",
            },
        },
    }


def _translate_nextcloud() -> dict:
    """Generate Sovereign sovereign: block for an OpenClaw Nextcloud community skill.

    Operations use python3_exec with nextcloud.py script deployed at
    workspace/skills/<name>/scripts/. nanobot-01 must be on business_net
    to reach http://nextcloud directly.
    No broker dependency.
    """
    return {
        "specialists": ["business_agent"],
        "tier_required": "LOW",
        "adapter_deps": ["nanobot"],
        "description": "Nextcloud calendar, tasks, and files via Python CalDAV/WebDAV scripts.",
        "operations": {
            "calendar_list": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "calendar_list"],
                "tier": "LOW",
                "params": {},
                "returns": "{calendars: [{name, url}], count}",
            },
            "calendar_create": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "calendar_create"],
                "tier": "MID",
                "params": {
                    "title":       {"type": "str", "required": True},
                    "start":       {"type": "str", "required": True},
                    "end":         {"type": "str", "required": False, "default": ""},
                    "description": {"type": "str", "required": False, "default": ""},
                    "calendar":    {"type": "str", "required": False, "default": "personal"},
                },
                "returns": "{status, uid}",
            },
            "calendar_delete": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "calendar_delete"],
                "tier": "MID",
                "params": {
                    "uid":      {"type": "str", "required": True},
                    "calendar": {"type": "str", "required": False, "default": "personal"},
                },
                "returns": "{status, uid}",
            },
            "tasks_list": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "tasks_list"],
                "tier": "LOW",
                "params": {
                    "calendar": {"type": "str", "required": False, "default": "tasks"},
                },
                "returns": "{tasks: [{uid, summary, status, due}], count}",
            },
            "tasks_create": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "tasks_create"],
                "tier": "MID",
                "params": {
                    "summary":     {"type": "str", "required": True},
                    "due":         {"type": "str", "required": False, "default": ""},
                    "description": {"type": "str", "required": False, "default": ""},
                    "calendar":    {"type": "str", "required": False, "default": "tasks"},
                },
                "returns": "{status, uid}",
            },
            "tasks_complete": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "tasks_complete"],
                "tier": "MID",
                "params": {
                    "uid":      {"type": "str", "required": True},
                    "calendar": {"type": "str", "required": False, "default": "tasks"},
                },
                "returns": "{status, uid}",
            },
            "files_list": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "files_list"],
                "tier": "LOW",
                "params": {
                    "path": {"type": "str", "required": False, "default": "/"},
                },
                "returns": "{files: [{name, path, type, size}], count}",
            },
            "files_search": {
                "tool": "python3_exec",
                "script": "scripts/nextcloud.py",
                "args": ["--command", "files_search"],
                "tier": "LOW",
                "params": {
                    "query": {"type": "str", "required": True},
                    "path":  {"type": "str", "required": False, "default": "/"},
                },
                "returns": "{files: [{name, path, size}], count}",
            },
        },
    }


def _translate_rss_feeds() -> dict:
    """Generate Sovereign sovereign: block for RSS/Atom feed skills (rss-digest compatible).

    Operations use python3_exec with feeds.py script deployed at
    workspace/skills/rss-digest/scripts/feeds.py.
    feedparser + httpx are pre-installed in nanobot-01 requirements.txt.
    Feed subscriptions stored in /workspace/feeds/subscriptions.json (persistent).
    """
    return {
        "specialists": ["research_agent", "business_agent"],
        "tier_required": "LOW",
        "adapter_deps": ["nanobot"],
        "description": "RSS/Atom feed reader — fetch, search, and manage feed subscriptions.",
        "operations": {
            "get_entries": {
                "tool": "python3_exec",
                "script": "scripts/feeds.py",
                "args": ["get-entries"],
                "tier": "LOW",
                "params": {
                    "limit": {"type": "int", "required": False, "default": 20},
                    "category": {"type": "str", "required": False, "default": ""},
                },
                "returns": "{entries: [{title, feed, url, date, summary}], count}",
            },
            "get_entry": {
                "tool": "python3_exec",
                "script": "scripts/feeds.py",
                "args": ["get-entry"],
                "tier": "LOW",
                "params": {
                    "url": {"type": "str", "required": True},
                },
                "returns": "{url, content, word_count}",
            },
            "add_feed": {
                "tool": "python3_exec",
                "script": "scripts/feeds.py",
                "args": ["add-feed"],
                "tier": "MID",
                "params": {
                    "name": {"type": "str", "required": True},
                    "url":  {"type": "str", "required": True},
                    "category": {"type": "str", "required": False, "default": "general"},
                },
                "returns": "{status, action, name, url}",
            },
            "list_feeds": {
                "tool": "python3_exec",
                "script": "scripts/feeds.py",
                "args": ["list-feeds"],
                "tier": "LOW",
                "params": {},
                "returns": "{feeds: [{name, url, category}], count}",
            },
            "search": {
                "tool": "python3_exec",
                "script": "scripts/feeds.py",
                "args": ["search"],
                "tier": "LOW",
                "params": {
                    "query": {"type": "str", "required": True},
                    "limit": {"type": "int", "required": False, "default": 10},
                },
                "returns": "{entries: [{title, feed, url, date, summary}], count, query}",
            },
        },
    }


def _audit_skill_deps(fm: dict, body: str, name: str) -> dict:
    """Inspect an unknown-category skill and determine what would be needed to emulate it.

    Returns a dict: {can_emulate: bool, reason: str, steps: list[str], missing: list[str]}
    Used to generate the advisory message when nanobot cannot handle a skill automatically.
    """
    oc_meta = (fm.get("metadata") or {}).get("openclaw") or {}
    req_env  = (oc_meta.get("requires") or {}).get("env") or []
    req_bins = (oc_meta.get("requires") or {}).get("bins") or []
    body_lower = body.lower()

    missing = []
    steps = []

    # Check binary dependencies
    for b in req_bins:
        if b not in ("node", "npm", "python3", "sh", "bash"):
            missing.append(f"binary: {b}")
            steps.append(f"Install '{b}' in the broker or nanobot container")

    # Check environment variables that don't map to known Sovereign vars
    known_mappings = {
        "imap_host", "imap_user", "imap_pass", "smtp_host", "smtp_user", "smtp_pass",
        "nextcloud_url", "nextcloud_user", "nextcloud_token",
        "webdav_user", "webdav_pass", "webdav_base",
    }
    for env in req_env:
        if env.lower() not in known_mappings:
            missing.append(f"env: {env}")
            steps.append(f"Add '{env}' to broker environment (secrets file + compose env_file)")

    # Check for runtime dependencies mentioned in body
    if "docker" in body_lower and "docker.sock" in body_lower:
        missing.append("docker.sock access")
        steps.append("Broker already has docker.sock — broker_exec __container_exec__ can be used")

    if "npm install" in body_lower or "node_modules" in body_lower:
        steps.append("Add npm packages from skill's package.json to broker/package.json and rebuild broker")

    if not missing and not steps:
        # Unknown format but no obvious gaps — might work via LLM path
        can_emulate = True
        reason = (
            f"Skill '{name}' has no known category mapping but has no obvious missing dependencies. "
            "It will fall through to the nanobot LLM path. "
            "Results may vary — if it fails, review the skill body and add explicit operations: DSL."
        )
    else:
        can_emulate = False
        reason = (
            f"Skill '{name}' cannot be fully emulated without development work. "
            f"nanobot-01 detected {len(missing)} missing requirement(s) that need Director action."
        )

    return {
        "can_emulate": can_emulate,
        "reason": reason,
        "steps": steps,
        "missing": missing,
    }


def _translate_skill_content(fm: dict, body: str, name: str) -> tuple[dict, dict | None]:
    """Deterministic translation of community skill content to Sovereign DSL format.

    Returns (sovereign_block, advisory | None).

    advisory is set when the skill cannot be fully emulated — it contains:
      {can_emulate: bool, reason: str, steps: list[str], missing: list[str]}
    Lifecycle.py surfaces the advisory in the install response so Sovereign can
    inform the Director of what development work is needed.

    Known categories: imap_smtp, nextcloud → full emulation, no advisory.
    Unknown category → advisory returned, minimal sovereign block, LLM path fallback.
    """
    category = _detect_skill_category(fm, body)
    logger.info(f"translate: skill={name!r} category={category}")

    if category == "imap_smtp":
        return _translate_imap_smtp(), None

    elif category == "nextcloud":
        return _translate_nextcloud(), None

    elif category == "rss_feeds":
        return _translate_rss_feeds(), None

    else:
        advisory = _audit_skill_deps(fm, body, name)
        logger.info(
            f"translate: skill={name!r} unknown category — "
            f"can_emulate={advisory['can_emulate']} missing={advisory['missing']}"
        )
        sovereign_block = {
            "specialists": ["research_agent"],
            "tier_required": "LOW",
            "adapter_deps": [],
            "description": fm.get("description", f"Community skill: {name}"),
            "operations": {},  # LLM fallback — nanobot agent reads skill body
        }
        return sovereign_block, advisory


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


def _dispatch_exec(params: dict, run_id: str, extra_env: dict | None = None) -> dict:
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

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    logger.info(f"[{run_id}] DSL exec: {command[:120]}")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE,
            env=env,
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


def _redeem_credential_token(context: dict) -> dict:
    """Redeem a session_token from sovereign-core's /credential_proxy.

    Returns a dict of env var name → value to inject into the subprocess env.
    Returns empty dict if no token present or redemption fails.
    """
    token = context.get("session_token")
    proxy_url = context.get("credential_proxy_url", "http://sovereign-core:8000/credential_proxy")
    if not token:
        return {}
    try:
        r = httpx.post(proxy_url, json={"token": token}, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "ok":
                return data.get("credentials", {})
            logger.warning("_redeem_credential_token: proxy returned error: %s", data.get("error"))
        else:
            logger.warning("_redeem_credential_token: HTTP %s from %s", r.status_code, proxy_url)
    except Exception as e:
        logger.warning("_redeem_credential_token: %s — %s", proxy_url, e)
    return {}


def _dispatch_python3_exec(skill: str, op_spec: dict, params: dict, run_id: str,
                           context: dict | None = None) -> dict:
    """Run a python3 script from the skill's scripts/ workspace directory.

    op_spec fields:
      script: path relative to skill workspace dir, e.g. "scripts/feeds.py"
      args:   list of fixed CLI args, e.g. ["--command", "check", "--account", "business"]
    Additional params are passed as --key value flags.
    Credentials available as env vars (Phase 1 static mounts in compose.yml).
    Uses shell=False + arg list to handle param values with spaces correctly.
    Script JSON output is parsed and merged directly into the response.
    """
    script_rel = op_spec.get("script", "")
    if not script_rel:
        return {"status": "error", "path": "dsl_python3",
                "error": "python3_exec: no 'script' in op_spec"}

    skill_dir   = os.path.join(WORKSPACE, "skills", skill)
    script_path = os.path.normpath(os.path.join(skill_dir, script_rel))

    # Path traversal guard
    if not script_path.startswith(os.path.normpath(WORKSPACE)):
        return {"status": "error", "path": "dsl_python3",
                "error": f"python3_exec: path traversal rejected: {script_path}"}

    if not os.path.isfile(script_path):
        return {"status": "error", "path": "dsl_python3",
                "error": f"python3_exec: script not found at {script_path} — "
                         "deploy scripts/ to workspace/skills/<name>/ at install time"}

    # Build command as a list (shell=False) so param values with spaces are safe
    fixed_args   = [str(a) for a in op_spec.get("args", [])]
    dynamic_args = []
    for k, v in params.items():
        if k not in ("command", "timeout", "script"):
            dynamic_args.extend([f"--{k}", str(v)])

    cmd_list = ["python3", script_path] + fixed_args + dynamic_args
    timeout  = int(params.get("timeout", 30))

    # Phase 2: redeem credential token if provided — inject as subprocess env vars
    extra_env: dict[str, str] = {}
    if context:
        extra_env = _redeem_credential_token(context)
        if extra_env:
            logger.info("[%s] python3_exec: redeemed credential token (%d vars injected)",
                        run_id, len(extra_env))

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    logger.info("[%s] python3_exec: %s %s", run_id, script_path, fixed_args)
    try:
        proc = subprocess.run(
            cmd_list,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE,
            env=env,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode != 0:
            # Try to parse JSON error from stdout before falling back to stderr
            if stdout:
                try:
                    parsed = json.loads(stdout)
                    return {"path": "dsl_python3", **parsed}
                except json.JSONDecodeError:
                    pass
            return {
                "status": "error", "path": "dsl_python3",
                "exit_code": proc.returncode,
                "error": stderr[:500] or f"script exited {proc.returncode}",
                "stdout": stdout[:500],
            }

        if stdout:
            try:
                parsed = json.loads(stdout)
                # Merge script JSON output directly into response
                return {"path": "dsl_python3", **parsed}
            except json.JSONDecodeError:
                pass

        return {
            "status": "ok", "path": "dsl_python3",
            "exit_code": proc.returncode,
            "stdout": stdout[:4000],
            "stderr": stderr[:200],
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "path": "dsl_python3",
                "error": f"script timed out after {timeout}s"}
    except Exception as e:
        return {"status": "error", "path": "dsl_python3",
                "error": f"python3_exec failed: {e}"}


def _dispatch_dsl(op_spec: dict, params: dict, run_id: str, skill: str = "",
                  context: dict | None = None) -> dict:
    """Route a validated DSL operation to the correct native handler.

    nanobot-01 is the primary execution environment for all OpenClaw/ClawhHub skills.
    Supported tools: filesystem, exec, python3_exec.
    """
    tool   = op_spec.get("tool", "").lower()
    action = op_spec.get("action", "").lower()

    if tool == "filesystem":
        return _dispatch_filesystem(action, params, run_id)
    elif tool == "exec":
        return _dispatch_exec(params, run_id)
    elif tool == "python3_exec":
        return _dispatch_python3_exec(skill, op_spec, params, run_id, context=context)
    else:
        return {
            "status": "error", "path": "dsl",
            "error": f"tool '{tool}' not handled natively. "
                     f"Supported: filesystem, exec, python3_exec.",
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

@app.post("/translate")
async def translate_skill(req: TranslateRequest):
    """Translate an external community skill (e.g. OpenClaw format) into Sovereign DSL format.

    Called by SkillLifecycleManager.load() when incoming SKILL.md lacks a sovereign: block.

    Response schema:
      {
        "status": "ok" | "needs_development",
        "name": str,
        "sovereign": {specialists, tier_required, adapter_deps, description, operations},
        "advisory": null | {can_emulate, reason, steps, missing}  -- set when needs_development
      }

    Deterministic for known categories (imap_smtp, nextcloud) → status: "ok", full operations DSL.
    Unknown categories → status: "needs_development" if missing deps, advisory explains what's needed.
    If unknown but no obvious gaps → status: "ok" with empty operations (LLM fallback path).

    Lifecycle.py surfaces any advisory in the install response so Sovereign can advise the Director.
    """
    fm, body = _parse_skill_md(req.content)
    name = req.name or fm.get("name", "unknown")
    sovereign_block, advisory = _translate_skill_content(fm, body, name)

    status = "ok"
    if advisory and not advisory.get("can_emulate", True):
        status = "needs_development"

    logger.info(
        f"translate: name={name!r} status={status} "
        f"specialists={sovereign_block.get('specialists')} "
        f"ops={list((sovereign_block.get('operations') or {}).keys())}"
    )
    return JSONResponse(content={
        "status": status,
        "name": name,
        "sovereign": sovereign_block,
        "advisory": advisory,
    })


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

    nanobot-01 is the primary execution environment for all OpenClaw/ClawhHub skills.
    DSL dispatch order:
      - tool=filesystem → native Python pathlib dispatch (no subprocess)
      - tool=exec       → allowlisted subprocess
      - tool=python3_exec → python3 script from workspace/skills/<name>/scripts/
      - no DSL match    → LLM path via nanobot agent CLI

    Called by NanobotAdapter in sovereign-core. Governance already applied upstream.
    Credentials available as env vars (Phase 1). No governance decisions here.
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
        loop     = asyncio.get_event_loop()
        _skill   = req.skill
        _context = req.context
        result = await loop.run_in_executor(
            None, lambda: _dispatch_dsl(op_spec, validated, run_id, skill=_skill, context=_context)
        )
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
