"""NanobotAdapter — dispatch execution tasks to nanobot-01 sidecar.

Nanobot-01 is a delegated execution node on ai_net. It has shell and
filesystem tools enabled, no web access, no channels, no secrets.

Rex never executes shell commands directly. Rex delegates to nanobots.
Rex enforces governance (MID tier minimum) BEFORE calling this adapter.

All results are structured dicts — never raises to callers.
All calls are logged to the audit ledger.

REST API: POST http://nanobot-01:8080/run
  Request:  {skill, action, params, context}
  Response: {run_id, skill, action, status, result} | {status, error}
"""

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NANOBOT_NODES = {
    "nanobot-01": os.environ.get("NANOBOT_01_URL", "http://nanobot-01:8080"),
}

TASK_TIMEOUT = 30.0  # seconds — must exceed nanobot's internal 25s timeout
HEALTH_TIMEOUT = 5.0


class NanobotAdapter:
    """Adapter for delegating execution tasks to nanobot sidecar nodes.

    Governance invariant: caller (ExecutionEngine) MUST have validated MID tier
    or higher before reaching this adapter. This adapter does not re-validate
    governance — it trusts that the caller has done so and passes the result
    to the audit ledger for the record.
    """

    def __init__(self, ledger=None):
        self._ledger = ledger

    def _log(self, event_type: str, payload: dict) -> None:
        if self._ledger:
            try:
                self._ledger.log(event_type=event_type, **payload)
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
        """Dispatch a task to a nanobot node.

        MID tier minimum — caller MUST have confirmed governance before calling.
        No secrets in params or context — governance layer enforces this upstream.

        Args:
            skill:   Skill name matching a /skills/<name>/SKILL.md on the node
            action:  Action within the skill (e.g. "summarise", "analyse")
            params:  Task-specific parameters (no secrets, no credentials)
            context: Ambient context from Rex (session_id, agent, etc.)
            node:    Nanobot node name (default: nanobot-01)

        Returns:
            Structured dict with status, run_id, result or error.
            Never raises.
        """
        params = params or {}
        context = context or {}
        t0 = time.monotonic()

        try:
            base_url = self._base_url(node)
        except ValueError as e:
            return {"status": "error", "error": str(e), "node": node}

        payload = {"skill": skill, "action": action, "params": params, "context": context}

        self._log("nanobot_dispatch", {
            "node": node,
            "skill": skill,
            "action": action,
            "params_keys": list(params.keys()),
        })

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
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
        except httpx.TimeoutException:
            return {
                "status": "error",
                "node": node,
                "skill": skill,
                "action": action,
                "error": f"task timed out after {TASK_TIMEOUT}s",
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
        except Exception as e:
            logger.exception(f"NanobotAdapter.run: unexpected error dispatching to {node}")
            return {
                "status": "error",
                "node": node,
                "skill": skill,
                "action": action,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
