"""
Backend rotation and per-backend rate limiting.
Priority: searxng (if configured) → ddg → brave → bing.
Rate limit: 10 requests/minute per backend (token bucket).
"""
import asyncio
from time import monotonic
from typing import Optional

import config
from search import ddg, brave, bing, searxng


class _RateLimiter:
    def __init__(self, rate: int, per: float = 60.0):
        self._rate = float(rate)
        self._per = per
        self._tokens = float(rate)
        self._last = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * (self._rate / self._per))
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class SearchRouter:
    def __init__(self, browser=None):
        self._browser = browser
        self._enabled = config.enabled_backends()
        self._limiters: dict[str, _RateLimiter] = {
            b: _RateLimiter(config.RATE_LIMIT) for b in ["searxng", "ddg", "brave", "bing"]
        }

    def set_browser(self, browser):
        self._browser = browser

    async def search(self, query: str, locale: str = "en-US") -> tuple[list[dict], str]:
        """
        Try backends in priority order until one succeeds.
        SearXNG (if configured) → DDG → Brave → Bing.
        Returns (results, backend_name). Raises RuntimeError if all fail/rate-limited.
        """
        for backend in self._enabled:
            limiter = self._limiters[backend]
            if not await limiter.acquire():
                continue   # rate limited — try next
            results = await self._call(backend, query, locale)
            if results:
                return results, backend
        raise RuntimeError("All search backends exhausted (rate-limited or failed)")

    async def _call(self, backend: str, query: str, locale: str) -> list[dict]:
        max_r = config.MAX_RESULTS
        if backend == "searxng":
            return await searxng.search(query, locale, max_r)
        if backend == "ddg":
            return await ddg.search(query, max_r, browser=self._browser)
        if backend == "brave":
            return await brave.search(query, locale, max_r)
        if backend == "bing":
            return await bing.search(query, locale, max_r)
        return []
