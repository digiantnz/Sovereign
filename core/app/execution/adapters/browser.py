"""
BrowserAdapter — calls a2a-browser service for web search and fetch.
Communicates via ai_net (a2a-browser is dual-homed ai_net + browser_net).
Auth: X-API-Key shared secret from A2A_SHARED_SECRET env var.

Wire format: A2A JSON-RPC 3.0 via POST /run (preferred).
Response unwrapped from A2A envelope; data field returned as before for
downstream compatibility. Legacy POST /search / POST /fetch retired 2026-03-19.

Credential-aware fetch:
AUTH_PROFILES maps hostname → headers dict. Matching headers are injected
into the browser/fetch payload so a2a-browser can pass them upstream.
Credentials are read from env vars at import time — never hardcoded.
"""
import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sovereign_a2a import A2AResponse  # static methods only — never instantiated

_BASE_URL = os.environ.get("A2A_BROWSER_URL", "http://a2a-browser:8001")
_SECRET = os.environ.get("A2A_SHARED_SECRET", "")
_TIMEOUT = 200.0

# ── Auth profiles — host-keyed header sets ────────────────────────────────────
# type: "headers" bundles Authorization + any API-specific headers in one block.
# Add entries here as new authenticated endpoints are needed.
# Credentials sourced from env vars only — never hardcoded.
_GITHUB_PAT = os.environ.get("GITHUB_PAT", "")

AUTH_PROFILES: dict[str, dict] = {}

if _GITHUB_PAT:
    AUTH_PROFILES["api.github.com"] = {
        "type": "headers",
        "headers": {
            "Authorization": f"Bearer {_GITHUB_PAT}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    }


def _build_fetch_payload(url: str, extract: str) -> dict:
    """Build browser/fetch payload, attaching auth headers if a profile matches the host."""
    payload: dict = {"url": url, "extract": extract}
    host = urlparse(url).netloc.split(":")[0]  # strip port if present
    profile = AUTH_PROFILES.get(host)
    if profile and profile.get("type") == "headers":
        payload["headers"] = profile["headers"]
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

        Auth headers are attached automatically if AUTH_PROFILES contains an
        entry for the URL's hostname (e.g. api.github.com → GitHub PAT headers).
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
