"""
DuckDuckGo search adapter.
Primary: duckduckgo_search Python library (httpx-based, no browser required).
Fallback: Playwright headless Chromium on DDG HTML endpoint (session-isolated).
"""
import asyncio
from typing import Optional
import config

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]
_ua_idx = 0


def _next_ua() -> str:
    global _ua_idx
    ua = _USER_AGENTS[_ua_idx % len(_USER_AGENTS)]
    _ua_idx += 1
    return ua


async def search(query: str, max_results: int = 10, browser=None) -> list[dict]:
    """
    Returns list of dicts: {title, url, snippet}.
    Tries library first, falls back to Playwright if available.
    """
    results = await _search_library(query, max_results)
    if not results and browser is not None:
        results = await _search_playwright(query, max_results, browser)
    return results


async def _search_library(query: str, max_results: int) -> list[dict]:
    """Use duckduckgo_search DDGS (httpx-based, no browser)."""
    try:
        from ddgs import DDGS
        loop = asyncio.get_event_loop()
        # DDGS().text() is sync — run in executor to avoid blocking the event loop
        raw = await loop.run_in_executor(
            None,
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in raw
        ]
    except Exception:
        return []


async def _search_playwright(query: str, max_results: int, browser) -> list[dict]:
    """Playwright fallback — new context per call for session isolation."""
    import urllib.parse
    context = None
    try:
        context = await browser.new_context(
            user_agent=_next_ua(),
            locale="en-US",
            ignore_https_errors=False,
        )
        page = await context.new_page()
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        results = await page.evaluate("""() => {
            const items = document.querySelectorAll('.result__title a, .result__snippet');
            const out = [];
            let i = 0;
            while (i < items.length - 1) {
                const titleEl = items[i];
                const snippetEl = items[i + 1];
                if (titleEl && titleEl.tagName === 'A') {
                    out.push({
                        title: titleEl.innerText.trim(),
                        url: titleEl.href,
                        snippet: snippetEl ? snippetEl.innerText.trim() : ''
                    });
                }
                i += 2;
            }
            return out.slice(0, """ + str(max_results) + """);
        }""")
        return results or []
    except Exception:
        return []
    finally:
        if context:
            await context.close()
