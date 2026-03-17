"""
Brave Search API adapter (secondary backend).
Requires BRAVE_API_KEY in environment.
Free tier: 2000 queries/month.
"""
import httpx
import config

_BASE = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT = 10.0


async def search(query: str, locale: str = "en-US", max_results: int = 10) -> list[dict]:
    """Returns list of {title, url, snippet}. Empty list on error or unconfigured."""
    if not config.BRAVE_API_KEY:
        return []

    country = locale.split("-")[-1].upper() if "-" in locale else "US"
    params = {
        "q": query,
        "count": min(max_results, 20),
        "country": country,
        "search_lang": locale.split("-")[0],
        "safesearch": "off",
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": config.BRAVE_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(_BASE, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return results

    except Exception:
        return []
