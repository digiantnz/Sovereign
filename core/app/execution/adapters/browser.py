"""
BrowserAdapter — calls a2a-browser service for web search and fetch.
Communicates via ai_net (a2a-browser is dual-homed ai_net + browser_net).
Auth: X-API-Key shared secret from A2A_SHARED_SECRET env var.

Wire format: A2A JSON-RPC 3.0 via POST /run.
Response unwrapped from A2A envelope; data field returned for downstream compat.

Credential-aware fetch (AUTH_PROFILES):
  Host-keyed map of AuthBlock dicts. Matched profile is passed as
  payload["auth"] — a2a-browser's _validate_auth() + _build_context_kwargs()
  translates each type into the appropriate Playwright credential mechanism.

  Supported types (mirrors a2a-browser AuthBlock):
    "headers"  — arbitrary headers dict (API keys, Accept, versioning headers)
    "bearer"   — token only (Authorization: Bearer <token>)
    "basic"    — username + password (HTTP Basic auth)
    "cookie"   — cookies dict passed to Playwright context

  Credentials sourced from env vars only — never hardcoded.
  Only add an AUTH_PROFILES entry when the required env var(s) are set.
"""
import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sovereign_a2a import A2AResponse  # static methods only — never instantiated

_BASE_URL = os.environ.get("A2A_BROWSER_URL", "http://a2a-browser:8001")
_SECRET = os.environ.get("A2A_SHARED_SECRET", "")
_TIMEOUT = 200.0

# ── Auth profiles — host-keyed AuthBlock dicts ───────────────────────────────
# Each entry is passed verbatim as payload["auth"] to the a2a-browser fetch
# handler. a2a-browser validates the type and applies credentials in Playwright.
# Credentials never reach nanobot, Ollama, or the LLM pipeline.

_GITHUB_PAT     = os.environ.get("GITHUB_PAT", "")
_INTERNAL_USER  = os.environ.get("INTERNAL_USER", "")
_INTERNAL_PASS  = os.environ.get("INTERNAL_PASS", "")

AUTH_PROFILES: dict[str, dict] = {}

# GitHub API — type: headers (bundles PAT + Accept + versioning in one block)
if _GITHUB_PAT:
    AUTH_PROFILES["api.github.com"] = {
        "type": "headers",
        "headers": {
            "Authorization": f"Bearer {_GITHUB_PAT}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    }

# Example: HTTP Basic auth for internal services
# Uncomment and add INTERNAL_USER / INTERNAL_PASS to secrets/browser.env
# if _INTERNAL_USER and _INTERNAL_PASS:
#     AUTH_PROFILES["internal.example.com"] = {
#         "type": "basic",
#         "username": _INTERNAL_USER,
#         "password": _INTERNAL_PASS,
#     }

# To add a new profile:
#   1. Add env var(s) to secrets/browser.env
#   2. Read them above with os.environ.get(...)
#   3. Add an AUTH_PROFILES entry guarded by the env var being set
#   Supported types: "headers" | "bearer" | "basic" | "cookie"


def _build_fetch_payload(url: str, extract: str) -> dict:
    """Build browser/fetch payload, attaching auth block if a profile matches the host.

    The entire AuthBlock dict is passed as payload["auth"] — a2a-browser handles
    all type-specific credential application in its Playwright context layer.
    """
    payload: dict = {"url": url, "extract": extract}
    host = urlparse(url).netloc.split(":")[0]  # strip port if present
    profile = AUTH_PROFILES.get(host)
    if profile:
        payload["auth"] = profile
    return payload


class BrowserAdapter:
    def __init__(self):
        self._base = _BASE_URL.rstrip("/")
        self._headers = {"X-API-Key": _SECRET, "Content-Type": "application/json"}

    async def search(self, query: str, locale: str = "en-NZ",
                     return_format: str = "full", test_mode: bool = False) -> dict:
        """POST /run — A2A JSON-RPC 3.0 browser/search."""
        if not _SECRET:
            return {"status": "error", "message": "A2A_SHARED_SECRET not configured"}

        body = {
            "jsonrpc": "3.0",
            "id": str(uuid4()),
            "method": "browser/search",
            "params": {
                "payload": {
                    "query": query,
                    "locale": locale,
                    "return_format": return_format,
                    "test_mode": test_mode,
                }
            },
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self._base}/run",
                    json=body,
                    headers=self._headers,
                )
                r.raise_for_status()
                body = r.json()
                if A2AResponse.is_error(body):
                    err = A2AResponse.get_error(body) or {}
                    return {"status": "error", "message": err.get("message", "a2a-browser error")}
                return {"status": "ok", "data": A2AResponse.get_result(body)}
        except httpx.HTTPStatusError as e:
            return {"status": "error", "message": f"a2a-browser HTTP {e.response.status_code}"}
        except Exception as e:
            return {"status": "error", "message": f"a2a-browser unreachable: {e}"}

    async def fetch(self, url: str, extract: str = "text") -> dict:
        """POST /run — A2A JSON-RPC 3.0 browser/fetch.

        Auth is attached automatically when AUTH_PROFILES has an entry for the
        URL's hostname. a2a-browser applies credentials in its Playwright layer;
        auth_applied: bool in the response confirms it was used.
        """
        if not _SECRET:
            return {"status": "error", "message": "A2A_SHARED_SECRET not configured"}

        body = {
            "jsonrpc": "3.0",
            "id": str(uuid4()),
            "method": "browser/fetch",
            "params": {
                "payload": _build_fetch_payload(url, extract)
            },
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self._base}/run",
                    json=body,
                    headers=self._headers,
                )
                r.raise_for_status()
                body = r.json()
                if A2AResponse.is_error(body):
                    err = A2AResponse.get_error(body) or {}
                    return {"status": "error", "message": err.get("message", "a2a-browser error")}
                return {"status": "ok", "data": A2AResponse.get_result(body)}
        except httpx.HTTPStatusError as e:
            return {"status": "error", "message": f"a2a-browser HTTP {e.response.status_code}"}
        except Exception as e:
            return {"status": "error", "message": f"a2a-browser unreachable: {e}"}

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base}/health", headers=self._headers)
                return r.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
