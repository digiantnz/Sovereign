"""
BrowserAdapter — calls a2a-browser service for web search.
Communicates via ai_net (a2a-browser is dual-homed ai_net + browser_net).
Auth: X-API-Key shared secret from A2A_SHARED_SECRET env var.
"""
import os
import httpx

_BASE_URL = os.environ.get("A2A_BROWSER_URL", "http://a2a-browser:8001")
_SECRET = os.environ.get("A2A_SHARED_SECRET", "")
_TIMEOUT = 200.0


class BrowserAdapter:
    def __init__(self):
        self._base = _BASE_URL.rstrip("/")
        self._headers = {"X-API-Key": _SECRET}

    async def search(self, query: str, locale: str = "en-US",
                     return_format: str = "full", test_mode: bool = False) -> dict:
        """
        POST /search to a2a-browser. Returns enriched JSON schema.
        """
        if not _SECRET:
            return {"status": "error", "message": "A2A_SHARED_SECRET not configured"}

        payload = {
            "query": query,
            "locale": locale,
            "return_format": return_format,
            "test_mode": test_mode,
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self._base}/search",
                    json=payload,
                    headers=self._headers,
                )
                r.raise_for_status()
                return {"status": "ok", "data": r.json()}
        except httpx.HTTPStatusError as e:
            return {"status": "error", "message": f"a2a-browser HTTP {e.response.status_code}"}
        except Exception as e:
            return {"status": "error", "message": f"a2a-browser unreachable: {e}"}

    async def fetch(self, url: str, extract: str = "text") -> dict:
        """
        POST /fetch to a2a-browser. Returns rendered page content.
        """
        if not _SECRET:
            return {"status": "error", "message": "A2A_SHARED_SECRET not configured"}

        payload = {"url": url, "extract": extract}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{self._base}/fetch",
                    json=payload,
                    headers=self._headers,
                )
                r.raise_for_status()
                return {"status": "ok", "data": r.json()}
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
