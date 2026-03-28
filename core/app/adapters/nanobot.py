"""NanobotAdapter — dispatch execution tasks to nanobot-01 sidecar.

Nanobot-01 is a delegated execution node on ai_net. It has shell and
filesystem tools enabled, no web access, no channels, no secrets.

Rex never executes shell commands directly. Rex delegates to nanobots.
Rex enforces governance (MID tier minimum) BEFORE calling this adapter.

All results are structured dicts — never raises to callers.
All calls are logged to the audit ledger.

Stage 3 DSL dispatch:
  For tool: browser — BrowserAdapter called directly in sovereign-core (a2a-browser proxy).
  For tool: broker_exec — BrokerAdapter called directly (SYSTEM_COMMANDS only).
  For tool: python3_exec | filesystem | exec — forwarded to nanobot-01 with credential token.
  For unknown/no DSL ops — forwarded to nanobot-01 for LLM fallback.
  imap/webdav/caldav are now python3_exec scripts in nanobot-01 workspace (OC-S6).

REST API: POST http://nanobot-01:8080/run
  Request:  {skill, action, params, context}  (legacy)
            {skill, operation, payload, request_id, timeout_ms, context}  (new contract)
  Response contract: {request_id, skill, operation, success, status_code, data, raw_error}
  Backward-compat fields also present: {run_id, action, status, path}
"""

import logging
import os
import time
import uuid
from typing import Any

import httpx
import yaml
from sovereign_a2a import A2AMessage, A2AErrorCodes, A2AResponse

logger = logging.getLogger(__name__)

NANOBOT_NODES = {
    "nanobot-01": os.environ.get("NANOBOT_01_URL", "http://nanobot-01:8080"),
}

# Shared secret — must match NANOBOT_SHARED_SECRET in nanobot-01 (secrets/nanobot.env)
_NANOBOT_SECRET: str = os.environ.get("NANOBOT_SHARED_SECRET", "")


def _auth_headers() -> dict:
    """Return X-API-Key header dict if secret is configured, else empty."""
    if _NANOBOT_SECRET:
        return {"X-API-Key": _NANOBOT_SECRET}
    return {}

# Cached agent cards per node — populated at startup and refreshed on each _forward() response
_agent_card_cache: dict[str, dict] = {}

TASK_TIMEOUT = 65.0  # seconds — personal inbox IMAP sync can take 55s+; must exceed nanobot subprocess timeout
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
    """Dispatch a DSL operation that must run natively in sovereign-core.

    Called only when tool in _NATIVE_TOOLS (currently: browser) or tool == broker_exec.
    imap/webdav/caldav are no longer native — they are forwarded to nanobot-01 via python3_exec.
    Returns structured result dict. Never raises.
    """
    try:
        if tool == "broker_exec":
            from adapters.broker import BrokerAdapter
            broker = BrokerAdapter()
            # op_spec['action'] names the command in commands-policy.yaml.
            # Outer `action` (the DSL operation key) is used as fallback.
            command_name = (op_spec or {}).get("action", action)
            # Only true system/OS calls go to broker — application-level commands
            # (imap_*, smtp_*, nc_*, feeds) are rejected here; callers should use
            # tool: python3_exec instead.
            if command_name not in SYSTEM_COMMANDS:
                logger.warning(
                    "nanobot: broker_exec '%s' is not a system command — "
                    "use tool: python3_exec instead. "
                    "Routing to broker anyway (backward compat — update SKILL.md).",
                    command_name,
                )
            return await broker.exec_command(command_name, params)

        elif tool == "browser":
            from execution.adapters.browser import BrowserAdapter
            browser = BrowserAdapter()
            return await _call_browser(browser, action, params)

        else:
            return {"status": "error", "error": f"_dispatch_dsl_native: unsupported tool {tool!r}"}

    except Exception as e:
        logger.exception(f"_dispatch_dsl_native: {tool}.{action} raised {type(e).__name__}")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


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
                r = await client.get(f"{url}/health", headers=_auth_headers())
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

    async def run_upload(self, filename: str, content_bytes: bytes,
                         mime_type: str, size: int,
                         node: str = "nanobot-01") -> dict:
        """Upload a binary file to nanobot-01 via multipart POST /upload.

        Used for Telegram attachment uploads where binary content is too large
        for _dispatch_python3_exec CLI args (Linux ARG_MAX ~2MB).
        nanobot saves to workspace/tmp/, calls nc_fs.py telegram_upload, cleans up.
        """
        import time as _time
        t0 = _time.monotonic()
        try:
            base_url = self._base_url(node)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{base_url}/upload",
                    headers=_auth_headers(),
                    files={
                        "file": (filename, content_bytes, mime_type),
                    },
                    data={
                        "filename":  filename,
                        "mime_type": mime_type,
                        "size":      str(size),
                    },
                )
            elapsed = _time.monotonic() - t0
            if r.status_code != 200:
                return {"status": "error",
                        "error": f"nanobot /upload returned HTTP {r.status_code}",
                        "http_status": r.status_code}
            body = r.json()
            self._log("nanobot_upload", {
                "node": node, "filename": filename, "size": size,
                "ok": body.get("status") == "ok", "elapsed_s": round(elapsed, 2),
            })
            return body
        except Exception as e:
            logger.error("NanobotAdapter.run_upload: %s", e)
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    async def fetch_capabilities(self, node: str = "nanobot-01") -> dict | None:
        """Fetch and cache agent_card from nanobot /capabilities endpoint.

        Called at sovereign-core startup. Failure is non-fatal — logs warning and continues.
        Cache is refreshed on every successful _forward() response that includes an agent_card.
        """
        try:
            base_url = self._base_url(node)
        except ValueError:
            return None
        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                r = await client.get(f"{base_url}/capabilities", headers=_auth_headers())
            if r.status_code == 200:
                body = r.json()
                card = A2AResponse.get_agent_card(body)
                if not card:
                    # /capabilities may also return the card at result.agent_card
                    card = (A2AResponse.get_result(body) or {}).get("agent_card")
                if card:
                    _agent_card_cache[node] = card
                    logger.info(
                        "nanobot capabilities: node=%s skills=%s",
                        node, card.get("skills", []),
                    )
                return card
            logger.warning("fetch_capabilities %s: HTTP %s", node, r.status_code)
        except Exception as e:
            logger.warning("fetch_capabilities %s: %s", node, e)
        return None

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
        """Forward request to nanobot-01 via A2A JSON-RPC 3.0."""
        try:
            base_url = self._base_url(node)
        except ValueError as e:
            return {
                "status": "error", "error": str(e),
                "node": node, "path": path, "_trust": "untrusted_external",
            }

        request_id = context.get("request_id") or str(uuid.uuid4())[:8]

        # Build A2A 3.0 request — agents never construct raw dicts
        a2a_request = A2AMessage.request(
            method=f"{skill}/{action}",
            params={"skill": skill, "operation": action, "payload": params},
            id=request_id,
            metadata={
                "context_hints": {
                    "tier":            context.get("tier", "LOW"),
                    "retry_strategy":  "correct_payload",
                    "timeout_ms":      int(TASK_TIMEOUT * 1000),
                }
            },
        )

        try:
            async with httpx.AsyncClient(timeout=TASK_TIMEOUT) as client:
                r = await client.post(f"{base_url}/run", json=a2a_request, headers=_auth_headers())

            elapsed = round(time.monotonic() - t0, 2)

            if r.status_code != 200:
                result = {
                    "status": "error", "success": False,
                    "node": node, "skill": skill, "action": action,
                    "http_status": r.status_code,
                    "error": f"nanobot returned {r.status_code}",
                    "response_body": r.text[:300],
                    "path": path, "elapsed_s": elapsed,
                    # Untrusted even on HTTP-level failures — content is external
                    "_trust": "untrusted_external",
                }
                self._log("nanobot_result", {**result, "ok": False})
                return result

            body = r.json()

            # Extract A2A metadata — available to specialist_inbound, never reaches translator
            hints = A2AResponse.get_hints(body)
            agent_card = A2AResponse.get_agent_card(body)
            if agent_card:
                _agent_card_cache[node] = agent_card

            # ── Error response ────────────────────────────────────────────────
            if A2AResponse.is_error(body):
                err = A2AResponse.get_error(body) or {}
                err_data = err.get("data") or {}
                result = {
                    "status": "error", "success": False,
                    "node": node, "skill": skill, "action": action,
                    "run_id": body.get("id", request_id),
                    "http_status": r.status_code,
                    "error":      err.get("message", "nanobot error"),
                    "raw_error":  err.get("message"),
                    "error_code": err.get("code"),
                    "status_code": err_data.get("status_code"),
                    "path": hints.get("execution_path", path),
                    "elapsed_s": elapsed,
                    "_trust":         "untrusted_external",
                    "_context_hints": hints,
                }
                self._log("nanobot_result", {
                    "node": node, "skill": skill, "action": action,
                    "run_id": request_id, "ok": False,
                    "path": result["path"], "elapsed_s": elapsed,
                    "error_code": err.get("code"),
                })
                return result

            # ── Success response ──────────────────────────────────────────────
            r_data = A2AResponse.get_result(body) or {}

            # data field: prefer r_data["data"], then whole result minus contract wrapper keys
            _body_result = r_data.get("data")
            if _body_result is None:
                _wrapper = {"success", "status_code", "data", "raw_error"}
                _body_result = {k: v for k, v in r_data.items() if k not in _wrapper}
                if not _body_result:
                    # Legacy flat response (python3_exec scripts) — strip meta fields
                    _meta = {"run_id", "request_id", "skill", "action", "operation",
                             "path", "elapsed_s", "status", "error", "raw_error",
                             "success", "status_code"}
                    _body_result = {k: v for k, v in body.items() if k not in _meta}

            contract_success = r_data.get("success", True)
            legacy_status = "ok" if contract_success else "error"

            result = {
                "status":      legacy_status,
                "success":     contract_success,
                "status_code": r_data.get("status_code"),
                "node":        node,
                "skill":       skill,
                "action":      action,
                "run_id":      body.get("id", request_id),
                "http_status": r.status_code,
                "result":      _body_result,
                "path":        hints.get("execution_path", body.get("path", path)),
                "elapsed_s":   elapsed,
                # All nanobot results are untrusted until scanned in handle_chat()
                "_trust":         "untrusted_external",
                "_context_hints": hints,
            }
            raw_err = r_data.get("raw_error")
            if raw_err:
                result["error"]     = raw_err
                result["raw_error"] = raw_err

            self._log("nanobot_result", {
                "node": node, "skill": skill, "action": action,
                "run_id": result.get("run_id"), "ok": result["status"] == "ok",
                "path": result["path"], "elapsed_s": elapsed,
            })
            return result

        except httpx.ConnectError:
            return {
                "status": "error", "success": False,
                "node": node, "skill": skill, "action": action,
                "error": f"connection refused — is {node} running on ai_net?",
                "error_code": A2AErrorCodes.ADAPTER_UNAVAILABLE,
                "path": path, "elapsed_s": round(time.monotonic() - t0, 2),
                "_trust": "untrusted_external",
            }
        except httpx.TimeoutException:
            return {
                "status": "error", "success": False,
                "node": node, "skill": skill, "action": action,
                "error": f"task timed out after {TASK_TIMEOUT}s",
                "error_code": A2AErrorCodes.TIMEOUT,
                "path": path, "elapsed_s": round(time.monotonic() - t0, 2),
                "_trust": "untrusted_external",
            }
        except Exception as e:
            logger.exception("NanobotAdapter._forward: unexpected error dispatching to %s", node)
            return {
                "status": "error", "success": False,
                "node": node, "skill": skill, "action": action,
                "error": f"{type(e).__name__}: {e}",
                "error_code": A2AErrorCodes.SERVER_ERROR,
                "path": path, "elapsed_s": round(time.monotonic() - t0, 2),
                "_trust": "untrusted_external",
            }
