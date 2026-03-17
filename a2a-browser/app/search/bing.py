"""
Bing Web Search API adapter (tertiary backend).
Requires BING_API_KEY in environment.
"""
import httpx
import config

_BASE = "https://api.bing.microsoft.com/v7.0/search"
_TIMEOUT = 10.0


async def search(query: str, locale: str = "en-US", max_results: int = 10) -> list[dict]:
    """Returns list of {title, url, snippet}. Empty list on error or unconfigured."""
    if not config.BING_API_KEY:
        return []

    market = locale if "-" in locale else f"{locale}-US"
    params = {
        "q": query,
        "count": min(max_results, 50),
        "mkt": market,
        "safeSearch": "Off",
        "responseFilter": "Webpages",
    }
    headers = {
        "Ocp-Apim-Subscription-Key": config.BING_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(_BASE, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()

        results = []
        for item in data.get("webPages", {}).get("value", []):
            results.append({
                "title": item.get("name", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", ""),
            })
        return results

    except Exception:
        return []
