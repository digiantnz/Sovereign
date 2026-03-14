"""NanobotAdapter — dispatch execution tasks to nanobot-01 sidecar.

Nanobot-01 is a delegated execution node on ai_net. It has shell and
filesystem tools enabled, no web access, no channels, no secrets.

Rex never executes shell commands directly. Rex delegates to nanobots.
Rex enforces governance (MID tier minimum) BEFORE calling this adapter.

All results are structured dicts — never raises to callers.
All calls are logged to the audit ledger.

Stage 3 DSL dispatch:
  For tool: imap | webdav | caldav — adapter classes are called directly here in
  sovereign-core. nanobot-01 has no credentials, so these ops never cross the wire.
  For tool: filesystem | exec — forwarded to nanobot-01 which handles them natively.
  For unknown/no DSL ops — forwarded to nanobot-01 for LLM fallback.

REST API: POST http://nanobot-01:8080/run
  Request:  {skill, action, params, context}
  Response: {run_id, skill, action, status, result, path} | {status, error}
"""

import logging
import os
import time
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

NANOBOT_NODES = {
    "nanobot-01": os.environ.get("NANOBOT_01_URL", "http://nanobot-01:8080"),
}

TASK_TIMEOUT = 30.0  # seconds — must exceed nanobot's internal 25s timeout
HEALTH_TIMEOUT = 5.0

_SKILLS_DIR = os.environ.get("SKILLS_DIR", "/home/sovereign/skills")

# mtime-invalidated DSL cache: skill_name -> (mtime_float, operations_dict | None)
_dsl_cache: dict[str, tuple[float, dict | None]] = {}

# Tools handled natively by this adapter (sovereign-core proxy — minimal set)
_NATIVE_TOOLS = {"browser"}

# Tools forwarded to nanobot-01 (nanobot-01 is the primary skill execution environment)
# imap/smtp/webdav/caldav: nanobot-01 has credential env vars (Phase 1) — handles directly
# python3_exec: Bash(python3:*) OpenClaw format — nanobot-01 python3 runtime
_REMOTE_TOOLS = {"filesystem", "exec", "python3_exec",
                 "imap", "smtp", "webdav", "caldav"}

# broker_exec commands that are legitimate system/OS calls — route to broker
# Everything else labelled broker_exec routes to nanobot-01 instead
SYSTEM_COMMANDS = frozenset({
    # Docker operations (broker holds docker.sock)
    "docker_ps", "docker_logs", "docker_stats", "docker_restart",
    "docker_inspect", "docker_exec",
    # OS read-only (whitelisted in commands-policy.yaml)
    "uname", "df", "free", "ps", "nvidia_smi",
    "systemctl_status", "journalctl", "apt_check",
})


def _load_skill_dsl(skill: str) -> dict | None:
    """Load and cache the operations: block from a SKILL.md frontmatter.

    Returns a dict of {action_name: op_spec} or None if no operations block.
    Cache is invalidated on mtime change — no container restart required.
    """
    skill_path = os.path.join(_SKILLS_DIR, skill, "SKILL.md")
    try:
        mtime = os.path.getmtime(skill_path)
    except OSError:
        return None

    cached_mtime, cached_ops = _dsl_cache.get(skill, (None, None))
    if cached_mtime == mtime:
        return cached_ops

    try:
        with open(skill_path, "r") as f:
            content = f.read()
    except OSError:
        _dsl_cache[skill] = (mtime, None)
        return None

    # Parse YAML frontmatter between the two --- delimiters
    if not content.startswith("---"):
        _dsl_cache[skill] = (mtime, None)
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        _dsl_cache[skill] = (mtime, None)
        return None

    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning(f"nanobot DSL: YAML parse error in {skill}: {e}")
        _dsl_cache[skill] = (mtime, None)
        return None

    ops = None
    if isinstance(fm, dict):
        sovereign = fm.get("sovereign", {}) or {}
        ops = sovereign.get("operations") or None

    _dsl_cache[skill] = (mtime, ops)
    return ops


def _validate_dsl_params(op_spec: dict, params: dict) -> tuple[dict, list[str]]:
    """Type-coerce and validate params against op_spec.

    Returns (validated_params, errors). If errors is non-empty the caller
    should reject the request before any execution.
    """
    param_schema: dict = op_spec.get("params", {}) or {}
    validated: dict = {}
    errors: list[str] = []

    for name, spec in param_schema.items():
        if isinstance(spec, str):
            # shorthand: "str" / "int" etc.
            spec = {"type": spec}

        required = spec.get("required", False)
        default = spec.get("default")
        expected_type = spec.get("type", "str")

        raw = params.get(name)

        if raw is None:
            if required:
                errors.append(f"missing required param: {name!r}")
                continue
            if default is not None:
                validated[name] = default
            continue

        # Type coercion
        try:
            if expected_type == "str":
                validated[name] = str(raw)
            elif expected_type == "int":
                validated[name] = int(raw)
            elif expected_type == "float":
                validated[name] = float(raw)
            elif expected_type == "bool":
                if isinstance(raw, bool):
                    validated[name] = raw
                else:
                    validated[name] = str(raw).lower() in ("true", "1", "yes")
            elif expected_type == "list":
                validated[name] = list(raw) if not isinstance(raw, list) else raw
            elif expected_type == "dict":
                validated[name] = dict(raw) if not isinstance(raw, dict) else raw
            else:
                validated[name] = raw  # unknown type — pass through
        except (TypeError, ValueError) as e:
            errors.append(f"param {name!r}: cannot coerce {raw!r} to {expected_type}: {e}")

    # Pass through any extra params not in schema (e.g. account selector)
    for k, v in params.items():
        if k not in validated and k not in errors:
            validated[k] = v

    return validated, errors


async def _dispatch_dsl_native(tool: str, action: str, params: dict, op_spec: dict | None = None) -> dict:
    """Dispatch a DSL operation using local adapter classes.

    Called for tool: imap | webdav | caldav | broker_exec | browser.
    Returns structured result dict. Never raises.
    """
    try:
        if tool == "imap":
            from adapters.imap import IMAPAdapter
            account = params.pop("account", "personal")
            adapter = IMAPAdapter(account=account)
            return await _call_imap(adapter, action, params)

        elif tool == "webdav":
            from adapters.webdav import WebDAVAdapter
            adapter = WebDAVAdapter()
            return await _call_webdav(adapter, action, params)

        elif tool == "caldav":
            from adapters.caldav import CalDAVAdapter
            adapter = CalDAVAdapter()
            return await _call_caldav(adapter, action, params)

        elif tool == "broker_exec":
            from adapters.broker import BrokerAdapter
            broker = BrokerAdapter()
            # op_spec['action'] names the command in commands-policy.yaml.
            # Outer `action` (the DSL operation key) is used as fallback.
            command_name = (op_spec or {}).get("action", action)
            # Only true system/OS calls go to broker — application-level commands
            # (imap_*, smtp_*, nc_*, feeds) are rejected here; callers should use
            # tool: python3_exec or tool: imap/smtp/webdav/caldav instead.
            if command_name not in SYSTEM_COMMANDS:
                logger.warning(
                    "nanobot: broker_exec '%s' is not a system command — "
                    "use tool: python3_exec or a native tool instead. "
                    "Routing to broker anyway (backward compat — update SKILL.md).",
                    command_name,
                )
            return await broker.exec_command(command_name, params)

        elif tool == "browser":
            from adapters.browser import BrowserAdapter
            browser = BrowserAdapter()
            return await _call_browser(browser, action, params)

        else:
            return {"status": "error", "error": f"_dispatch_dsl_native: unsupported tool {tool!r}"}

    except Exception as e:
        logger.exception(f"_dispatch_dsl_native: {tool}.{action} raised {type(e).__name__}")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


async def _call_imap(adapter, action: str, params: dict) -> dict:
    """Route an IMAP DSL action to the IMAPAdapter."""
    if action == "fetch_unread":
        return await adapter.fetch_unread(max_messages=params.get("max_messages", 20))
    elif action == "list_inbox":
        return await adapter.list_inbox(max_messages=params.get("max_messages", 50))
    elif action == "fetch_message":
        return await adapter.fetch_message(uid=params["uid"])
    elif action == "search":
        # criteria is a dict: {subject, from_addr, since, body} (any subset)
        criteria = params.get("criteria") or {k: v for k, v in params.items() if k != "account"}
        return await adapter.search(criteria=criteria)
    elif action == "move_message":
        return await adapter.move_message(uid=params["uid"], destination=params["destination"])
    elif action == "delete_message":
        return await adapter.delete_message(uid=params["uid"])
    elif action == "mark_read":
        return await adapter.mark_read(uid=params["uid"])
    elif action == "mark_unread":
        return await adapter.mark_unread(uid=params["uid"])
    elif action == "list_folders":
        return await adapter.list_folders()
    else:
        return {"status": "error", "error": f"imap: unknown action {action!r}"}


async def _call_webdav(adapter, action: str, params: dict) -> dict:
    """Route a WebDAV DSL action to the WebDAVAdapter."""
    if action == "list":
        return await adapter.list(path=params.get("path", "/"))
    elif action == "read":
        return await adapter.read(path=params["path"])
    elif action == "write":
        return await adapter.write(path=params["path"], content=params["content"])
    elif action == "delete":
        return await adapter.delete(path=params["path"])
    elif action == "mkdir":
        return await adapter.mkdir(path=params["path"])
    elif action == "search":
        return await adapter.search(query=params["query"], path=params.get("path", "/"))
    else:
        return {"status": "error", "error": f"webdav: unknown action {action!r}"}


def _make_uid() -> str:
    import uuid
    return str(uuid.uuid4())


async def _call_caldav(adapter, action: str, params: dict) -> dict:
    """Route a CalDAV DSL action to the CalDAVAdapter."""
    if action == "list_calendars":
        return await adapter.list_calendars()
    elif action == "list_events":
        return await adapter.list_events(
            calendar=params.get("calendar", "personal"),
            from_date=params.get("from_date"),
            to_date=params.get("to_date"),
        )
    elif action == "create_event":
        return await adapter.create_event(
            calendar=params.get("calendar", "personal"),
            uid=params.get("uid") or _make_uid(),
            summary=params["summary"],
            start=params["start"],
            end=params["end"],
            description=params.get("description", ""),
        )
    elif action == "delete_event":
        return await adapter.delete_event(
            calendar=params.get("calendar", "personal"),
            uid=params["uid"],
        )
    elif action == "create_task":
        return await adapter.create_task(
            calendar=params.get("calendar", "tasks"),
            uid=params.get("uid") or _make_uid(),
            summary=params["summary"],
            due=params.get("due"),
            start=params.get("start"),
            description=params.get("description"),
            status=params.get("status", "NEEDS-ACTION"),
        )
    elif action == "complete_task":
        return await adapter.complete_task(
            calendar=params.get("calendar", "tasks"),
            uid=params["uid"],
        )
    elif action == "delete_task":
        return await adapter.delete_task(
            calendar=params.get("calendar", "tasks"),
            uid=params["uid"],
        )
    else:
        return {"status": "error", "error": f"caldav: unknown action {action!r}"}


async def _call_browser(adapter, action: str, params: dict) -> dict:
    """Route a browser DSL action to the BrowserAdapter."""
    if action == "search":
        return await adapter.search(
            query=params["query"],
            locale=params.get("locale", "en-NZ"),
            return_format=params.get("return_format", "standard"),
        )
    elif action == "fetch":
        return await adapter.fetch(
            url=params["url"],
            extract=params.get("extract", "text"),
        )
    else:
        return {"status": "error", "error": f"browser: unknown action {action!r}"}


class NanobotAdapter:
    """Adapter for delegating execution tasks to nanobot sidecar nodes.

    Governance invariant: caller (ExecutionEngine) MUST have validated MID tier
    or higher before reaching this adapter. This adapter does not re-validate
    governance — it trusts that the caller has done so and passes the result
    to the audit ledger for the record.

    Dispatch order for run():
      1. Load skill DSL. If action is defined in DSL:
         a. Validate params.
         b. If tool in _NATIVE_TOOLS: call adapter directly in sovereign-core (browser only).
         c. If tool in _REMOTE_TOOLS or broker_exec non-system: forward to nanobot-01.
      2. No DSL or action not in DSL: forward to nanobot-01 for LLM path.

    Phase 2 credential delegation:
      Before _forward(), issue a one-time token from CredentialProxy for any
      credential_services declared in the op_spec. Token is passed in context
      and redeemed by nanobot-01 via POST sovereign-core:8000/credential_proxy.
    """

    def __init__(self, ledger=None):
        self._ledger = ledger
        self._credential_proxy = None

    def set_credential_proxy(self, proxy) -> None:
        self._credential_proxy = proxy

    def _log(self, event_type: str, payload: dict) -> None:
        if self._ledger:
            try:
                self._ledger.append(event_type=event_type, stage="nanobot_dispatch", data=payload)
            except Exception as e:
                logger.warning(f"NanobotAdapter: ledger log failed: {e}")

    def _base_url(self, node: str) -> str:
        url = NANOBOT_NODES.get(node)
        if not url:
            raise ValueError(f"Unknown nanobot node: {node!r}. Registered: {list(NANOBOT_NODES)}")
        return url.rstrip("/")

    async def health(self, node: str = "nanobot-01") -> dict:
        """Check nanobot node health. LOW-tier read — no governance gating needed."""
        try:
            url = self._base_url(node)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                r = await client.get(f"{url}/health")
            if r.status_code == 200:
                return {"status": "ok", "node": node, "http_status": r.status_code, **r.json()}
            return {
                "status": "error",
                "node": node,
                "http_status": r.status_code,
                "error": f"health returned {r.status_code}",
                "response_body": r.text[:300],
            }
        except httpx.ConnectError:
            return {"status": "error", "node": node, "error": f"connection refused — is {node} running?"}
        except httpx.TimeoutException:
            return {"status": "error", "node": node, "error": f"health check timed out after {HEALTH_TIMEOUT}s"}
        except Exception as e:
            return {"status": "error", "node": node, "error": f"{type(e).__name__}: {e}"}

    async def run(
        self,
        skill: str,
        action: str,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        node: str = "nanobot-01",
    ) -> dict:
        """Dispatch a task to a nanobot node or native adapter (Stage 3 DSL).

        MID tier minimum — caller MUST have confirmed governance before calling.
        No secrets in params or context — governance layer enforces this upstream.

        Args:
            skill:   Skill name matching a /skills/<name>/SKILL.md
            action:  Action within the skill (e.g. "fetch_unread", "list")
            params:  Task-specific parameters (no secrets, no credentials)
            context: Ambient context from Rex (session_id, agent, etc.)
            node:    Nanobot node name (default: nanobot-01)

        Returns:
            Structured dict with status, run_id (or None for native), result or error.
            Always includes path: "dsl_native" | "dsl_remote" | "llm"
            Never raises.
        """
        params = params or {}
        context = context or {}
        t0 = time.monotonic()

        self._log("nanobot_dispatch", {
            "node": node,
            "skill": skill,
            "action": action,
            "params_keys": list(params.keys()),
        })

        # ── Stage 3: DSL intercept ──────────────────────────────────────────
        operations = _load_skill_dsl(skill)
        if operations and action in operations:
            op_spec = operations[action]
            tool = op_spec.get("tool", "")

            validated, errors = _validate_dsl_params(op_spec, dict(params))
            if errors:
                result = {
                    "status": "error",
                    "skill": skill,
                    "action": action,
                    "error": f"param validation failed: {'; '.join(errors)}",
                    "path": "dsl_native" if tool in _NATIVE_TOOLS else "dsl_remote",
                    "elapsed_s": round(time.monotonic() - t0, 2),
                }
                self._log("nanobot_result", {**result, "ok": False})
                return result

            if tool in _NATIVE_TOOLS:
                # Call adapter classes directly — no HTTP to nanobot-01
                native_result = await _dispatch_dsl_native(tool, action, validated, op_spec=op_spec)
                elapsed = round(time.monotonic() - t0, 2)
                result = {
                    "status": native_result.get("status", "ok"),
                    "node": "sovereign-core",
                    "skill": skill,
                    "action": action,
                    "run_id": None,
                    "result": native_result,
                    "path": "dsl_native",
                    "elapsed_s": elapsed,
                }
                if native_result.get("error"):
                    result["error"] = native_result["error"]
                self._log("nanobot_result", {
                    "node": "sovereign-core",
                    "skill": skill,
                    "action": action,
                    "run_id": None,
                    "ok": result["status"] == "ok",
                    "path": "dsl_native",
                    "elapsed_s": elapsed,
                })
                return result

            # tool in _REMOTE_TOOLS (or broker_exec non-system) — forward to nanobot-01
            # Phase 2: if op_spec declares credential_services, issue a one-time token
            # and inject it into context so nanobot-01 can redeem via /credential_proxy.
            fwd_context = dict(context)
            credential_services = op_spec.get("credential_services") or []
            if credential_services and self._credential_proxy:
                token = self._credential_proxy.issue(credential_services)
                if token:
                    fwd_context["session_token"] = token
                    fwd_context["credential_proxy_url"] = "http://sovereign-core:8000/credential_proxy"
                    logger.debug("NanobotAdapter: issued credential token for %s", credential_services)

            return await self._forward(
                skill, action, validated, fwd_context, node, t0, path="dsl_remote"
            )

        # ── LLM fallback — no DSL match ─────────────────────────────────────
        return await self._forward(skill, action, params, context, node, t0, path="llm")

    async def _forward(
        self,
        skill: str,
        action: str,
        params: dict,
        context: dict,
        node: str,
        t0: float,
        path: str = "llm",
    ) -> dict:
        """Forward request to nanobot-01 via HTTP."""
        try:
            base_url = self._base_url(node)
        except ValueError as e:
            return {"status": "error", "error": str(e), "node": node, "path": path}

        payload = {"skill": skill, "action": action, "params": params, "context": context}

        try:
            async with httpx.AsyncClient(timeout=TASK_TIMEOUT) as client:
                r = await client.post(f"{base_url}/run", json=payload)

            elapsed = round(time.monotonic() - t0, 2)
            http_status = r.status_code

            if http_status != 200:
                result = {
                    "status": "error",
                    "node": node,
                    "skill": skill,
                    "action": action,
                    "http_status": http_status,
                    "error": f"nanobot returned {http_status}",
                    "response_body": r.text[:300],
                    "path": path,
                    "elapsed_s": elapsed,
                }
                self._log("nanobot_result", {**result, "ok": False})
                return result

            body = r.json()
            result = {
                "status": body.get("status", "ok"),
                "node": node,
                "skill": skill,
                "action": action,
                "run_id": body.get("run_id"),
                "http_status": http_status,
                "result": body.get("result"),
                "path": body.get("path", path),
                "elapsed_s": elapsed,
            }
            if body.get("error"):
                result["error"] = body["error"]

            self._log("nanobot_result", {
                "node": node,
                "skill": skill,
                "action": action,
                "run_id": result.get("run_id"),
                "ok": result["status"] == "ok",
                "path": result["path"],
                "elapsed_s": elapsed,
            })
            return result

        except httpx.ConnectError:
            return {
                "status": "error",
                "node": node,
                "skill": skill,
                "action": action,
                "error": f"connection refused — is {node} running on ai_net?",
                "path": path,
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
        except httpx.TimeoutException:
            return {
                "status": "error",
                "node": node,
                "skill": skill,
                "action": action,
                "error": f"task timed out after {TASK_TIMEOUT}s",
                "path": path,
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
        except Exception as e:
            logger.exception(f"NanobotAdapter._forward: unexpected error dispatching to {node}")
            return {
                "status": "error",
                "node": node,
                "skill": skill,
                "action": action,
                "error": f"{type(e).__name__}: {e}",
                "path": path,
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
