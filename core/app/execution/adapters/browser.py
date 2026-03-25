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
  AUTH_PROFILES is populated from two sources (merged at startup):
    1. Hardcoded env var guards below (backward compat)
    2. RAID YAML config at /home/sovereign/governance/browser-auth-profiles.yaml
       — managed by configure_browser_auth intent; YAML takes precedence.
"""
import copy
import logging
import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sovereign_a2a import A2AResponse  # static methods only — never instantiated

_BASE_URL = os.environ.get("A2A_BROWSER_URL", "http://a2a-browser:8001")
_SECRET = os.environ.get("A2A_SHARED_SECRET", "")
_TIMEOUT = 200.0
_YAML_PATH = "/home/sovereign/governance/browser-auth-profiles.yaml"

_log = logging.getLogger(__name__)


def _resolve_tokens(obj):
    """Recursively resolve {VAR_NAME} tokens in strings using os.environ."""
    if isinstance(obj, str):
        try:
            return obj.format_map(os.environ)
        except KeyError:
            return obj
    if isinstance(obj, dict):
        return {k: _resolve_tokens(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_tokens(v) for v in obj]
    return obj


def _load_auth_profiles_yaml() -> dict:
    """Load browser auth profiles from RAID YAML config.

    Resolves {VAR_NAME} tokens from os.environ. Skips profiles with missing
    required_env vars (logs a warning). Returns resolved profiles dict.
    Called at module load and after configure_browser_auth writes a new profile.
    """
    try:
        import yaml as _yaml
    except ImportError:
        _log.warning("pyyaml not installed — browser-auth-profiles.yaml not loaded")
        return {}
    try:
        with open(_YAML_PATH) as _f:
            _data = _yaml.safe_load(_f)
        if not isinstance(_data, dict) or not _data.get("profiles"):
            return {}
        _resolved = {}
        for _host, _profile in _data["profiles"].items():
            if not isinstance(_profile, dict):
                continue
            _required = _profile.get("required_env") or []
            _missing = [_v for _v in _required if not os.environ.get(_v)]
            if _missing:
                _log.warning(
                    "browser-auth-profiles.yaml: host %s skipped — missing env vars: %s",
                    _host, _missing,
                )
                continue
            # Strip metadata keys; deep-copy to avoid mutating the parsed dict
            _clean = {k: copy.deepcopy(v) for k, v in _profile.items()
                      if k not in ("required_env", "added_by", "added_at", "notes")}
            _resolved[_host] = _resolve_tokens(_clean)
        return _resolved
    except FileNotFoundError:
        return {}
    except Exception as _e:
        _log.warning("browser-auth-profiles.yaml load error: %s", _e)
        return {}


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

# ── Dynamic profiles from RAID YAML — loaded at startup ──────────────────────
# YAML profiles merge over hardcoded entries (YAML takes precedence).
# configure_browser_auth intent writes to this YAML file; sovereign-core restart
# re-runs this block to activate newly added profiles.
AUTH_PROFILES.update(_load_auth_profiles_yaml())


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
                # A2AResponse.get_result() returns {success, status_code, data: {url, title, content}}
                # Unwrap the inner "data" field so callers get a flat {url, title, content} dict.
                _a2a_result = A2AResponse.get_result(body)
                _inner = _a2a_result.get("data", _a2a_result) if isinstance(_a2a_result, dict) else {}
                return {"status": "ok", "data": _inner}
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
