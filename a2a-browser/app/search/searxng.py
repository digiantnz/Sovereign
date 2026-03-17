"""
SearXNG search adapter.
Calls the self-hosted SearXNG JSON API — aggregates Google, Bing, DDG,
Startpage, Wikipedia, StackOverflow in a single request.
Returns [] on any failure so the router falls through to the next backend.
"""
import httpx
import config

_TIMEOUT = 20.0
_ENGINES = "google,bing,duckduckgo,startpage,wikipedia"


async def search(query: str, locale: str, max_results: int) -> list[dict]:
    if not config.SEARXNG_URL:
        return []
    lang = locale[:2].lower() if locale else "en"
    params = {
        "q": query,
        "format": "json",
        "language": lang,
        "categories": "general",
        "engines": _ENGINES,
        "pageno": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{config.SEARXNG_URL}/search", params=params)
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("results", [])[:max_results]:
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("content", "")).strip()
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results
    except Exception:
        return []
