"""Sovereign News Harness — run_news_brief()

Fetches news from RSS, Grok, and browser search in parallel; deduplicates;
synthesises into a single brief via one local Ollama call.
"""

import asyncio
import json
import logging
import re
import string
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_TOPICS = ["technology", "AI", "cryptocurrency", "Ethereum", "New Zealand"]

_FALLBACK_PREFS = (
    "Matt's news interests: technology and open source (Hacker News), "
    "artificial intelligence and LLMs, cryptocurrency particularly Ethereum and "
    "Rocket Pool staking, New Zealand local news and current events, "
    "cybersecurity and infosec. "
    "Prefer: substantive technical items over hype; NZ relevance where available. "
    "Avoid: celebrity/entertainment, sports (unless NZ), pure marketing."
)


# ── Deduplication helpers ──────────────────────────────────────────────────

def _normalise_title(title: str) -> set:
    """Lowercase, strip punctuation, return set of words (≥3 chars)."""
    title = title.lower()
    title = title.translate(str.maketrans("", "", string.punctuation))
    return {w for w in title.split() if len(w) >= 3}


def _is_duplicate(title: str, seen: list[set], threshold: float = 0.60) -> bool:
    """Return True if ≥60% of this title's words overlap with any already-seen title."""
    words = _normalise_title(title)
    if not words:
        return False
    for seen_words in seen:
        if not seen_words:
            continue
        overlap = len(words & seen_words) / len(words)
        if overlap >= threshold:
            return True
    return False


# ── Individual source fetchers ─────────────────────────────────────────────

async def _fetch_rss(nanobot) -> tuple[list[dict], str | None]:
    """Fetch RSS entries. Returns (items, error_or_None)."""
    try:
        nb = await nanobot.run("rss-digest", "get_entries", {"limit": 20})
        # Accept both flat and nested result shapes
        result = nb.get("result") if nb.get("result") is not None else nb
        entries = []
        if isinstance(result, list):
            entries = result
        elif isinstance(result, dict):
            # Common keys: entries, items, data
            for key in ("entries", "items", "data", "results"):
                if isinstance(result.get(key), list):
                    entries = result[key]
                    break
        items = []
        for e in entries:
            title = e.get("title") or e.get("name") or ""
            summary = e.get("summary") or e.get("description") or e.get("content") or ""
            if title:
                items.append({"title": title, "summary": str(summary)[:300], "source": "rss"})
        logger.info("news_harness: RSS fetched %d entries → %d items", len(entries), len(items))
        return items, None
    except Exception as exc:
        logger.warning("news_harness: RSS source failed: %s", exc)
        return [], str(exc)


async def _fetch_grok(cog, topics: list[str]) -> tuple[list[dict], str | None]:
    """Ask Grok for current news on Matt's preferred topics."""
    try:
        topic_str = ", ".join(topics)
        prompt = (
            f"Give me the top 8–10 current news headlines (today or this week) on these topics: "
            f"{topic_str}. "
            "Format your response as a JSON array of objects with keys 'title' and 'summary'. "
            "Each summary should be 1–2 sentences. Return ONLY the JSON array — no other text."
        )
        result = await cog.ask_grok(prompt, agent="research_agent")
        raw = result.get("response", "") if isinstance(result, dict) else str(result)
        # Extract JSON array from response
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            logger.warning("news_harness: Grok response had no JSON array")
            return [], "no JSON array in response"
        items_raw = json.loads(match.group(0))
        items = []
        for entry in items_raw:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title") or ""
            summary = entry.get("summary") or ""
            if title:
                items.append({"title": str(title), "summary": str(summary)[:300], "source": "grok"})
        logger.info("news_harness: Grok returned %d items", len(items))
        return items, None
    except Exception as exc:
        logger.warning("news_harness: Grok source failed: %s", exc)
        return [], str(exc)


async def _fetch_browser(nanobot, topics: list[str]) -> tuple[list[dict], str | None]:
    """Search via sovereign-browser for current news."""
    try:
        query = " ".join(topics[:3]) + " news today"
        nb = await nanobot.run(
            "sovereign-browser", "search",
            {"query": query, "return_format": "full"},
        )
        result = nb.get("result") if nb.get("result") is not None else nb
        raw_results = []
        if isinstance(result, list):
            raw_results = result
        elif isinstance(result, dict):
            for key in ("results", "items", "data", "entries"):
                if isinstance(result.get(key), list):
                    raw_results = result[key]
                    break
        items = []
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            title = r.get("title") or ""
            content = r.get("content") or r.get("snippet") or r.get("description") or ""
            if title:
                items.append({"title": str(title), "summary": str(content)[:300], "source": "browser"})
        logger.info("news_harness: browser search returned %d items", len(items))
        return items, None
    except Exception as exc:
        logger.warning("news_harness: browser source failed: %s", exc)
        return [], str(exc)


# ── Deduplication ──────────────────────────────────────────────────────────

def _deduplicate(all_items: list[dict]) -> tuple[list[dict], int]:
    """Remove near-duplicate titles. Returns (deduped_items, removed_count)."""
    seen: list[set] = []
    unique = []
    removed = 0
    for item in all_items:
        title = item.get("title", "")
        if _is_duplicate(title, seen):
            removed += 1
            continue
        seen.append(_normalise_title(title))
        unique.append(item)
    return unique, removed


# ── Synthesis ─────────────────────────────────────────────────────────────

async def _synthesise(cog, items: list[dict], prefs_text: str, use_grok: bool = False) -> str:
    """One LLM call to synthesise all items into a 5–8 bullet brief."""
    numbered = "\n".join(
        f"{i+1}. {item['title']} — {item['summary']}"
        for i, item in enumerate(items[:30])
    )
    prompt = (
        f"Director preferences: {prefs_text}\n\n"
        "Here are today's news items from multiple sources:\n"
        f"{numbered}\n\n"
        "Synthesise these into a concise news brief of 5–8 bullet points. "
        "Weight items toward the Director's stated preferences. "
        "Write each bullet as one clear sentence. "
        "Do NOT include source names, URLs, or metadata — only the synthesised content. "
        "Start each bullet with •"
    )
    if use_grok:
        result = await cog.ask_grok(prompt, agent="research_agent")
        return result.get("response", "") if isinstance(result, dict) else str(result)
    result = await cog.ask_local(prompt)
    return result.get("response", "") if isinstance(result, dict) else str(result)


# ── Episodic write (non-blocking) ──────────────────────────────────────────

async def _write_episodic(qdrant, sources_ok: list, sources_failed: list,
                           item_count: int, dedup_removed: int) -> None:
    """Write a run record to episodic memory. Called via asyncio.create_task()."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        await qdrant.store(
            collection="episodic",
            content=(
                f"News brief run at {ts}. "
                f"Sources succeeded: {sources_ok}. "
                f"Sources failed: {sources_failed}. "
                f"Items after dedup: {item_count}. "
                f"Duplicates removed: {dedup_removed}."
            ),
            metadata={
                "type": "episodic",
                "event_type": "news_brief_run",
                "sources_ok": sources_ok,
                "sources_failed": sources_failed,
                "item_count": item_count,
                "dedup_removed": dedup_removed,
                "ts": ts,
            },
        )
        logger.debug("news_harness: episodic record written")
    except Exception as exc:
        logger.warning("news_harness: episodic write failed (non-blocking): %s", exc)


# ── Main entry point ───────────────────────────────────────────────────────

async def run_news_brief(cog, nanobot, qdrant, user_input: str = "") -> dict:
    """
    Fetch news from RSS, Grok, and browser in parallel; dedup; synthesise.

    Returns:
        {
            "status": "ok" | "partial" | "no_results",
            "brief": "<synthesised text>",
            "sources_ok": [...],
            "sources_failed": [...],
            "item_count": N,
            "dedup_removed": N,
        }
    """
    # ── 1. Load news preferences from semantic memory ──────────────────────
    prefs_text = _FALLBACK_PREFS
    topics = _DEFAULT_TOPICS
    try:
        pref_entry = await qdrant.retrieve_by_key("semantic:preferences:news")
        if pref_entry and pref_entry.get("content"):
            prefs_text = pref_entry["content"]
            # Extract topic words from preferences for query strings
            # (keep default topics as fallback; preference text is used verbatim in synthesis)
            logger.debug("news_harness: loaded news preferences from semantic memory")
        else:
            logger.debug("news_harness: semantic:preferences:news not found, using defaults")
    except Exception as exc:
        logger.warning("news_harness: preference retrieval failed: %s", exc)

    # ── 2. Parallel fetch from all three sources ───────────────────────────
    rss_task     = asyncio.create_task(_fetch_rss(nanobot))
    grok_task    = asyncio.create_task(_fetch_grok(cog, topics))
    browser_task = asyncio.create_task(_fetch_browser(nanobot, topics))

    rss_items,     rss_err     = await rss_task
    grok_items,    grok_err    = await grok_task
    browser_items, browser_err = await browser_task

    sources_ok     = []
    sources_failed = []

    if rss_err is None:
        sources_ok.append("rss")
    else:
        sources_failed.append(f"rss: {rss_err}")

    if grok_err is None:
        sources_ok.append("grok")
    else:
        sources_failed.append(f"grok: {grok_err}")

    if browser_err is None:
        sources_ok.append("browser")
    else:
        sources_failed.append(f"browser: {browser_err}")

    all_items = rss_items + grok_items + browser_items

    if not all_items:
        asyncio.create_task(_write_episodic(qdrant, sources_ok, sources_failed, 0, 0))
        return {
            "status": "no_results",
            "brief": "No news items could be retrieved from any source.",
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "item_count": 0,
            "dedup_removed": 0,
        }

    # ── 3. Deduplicate ─────────────────────────────────────────────────────
    deduped, dedup_removed = _deduplicate(all_items)

    # ── 4. Synthesise via single LLM call ──────────────────────────────────
    _use_grok = any(kw in user_input.lower() for kw in ("use grok", "ask grok", "via grok"))
    brief = await _synthesise(cog, deduped, prefs_text, use_grok=_use_grok)

    if not brief.strip():
        brief = "Could not synthesise news brief — synthesis returned an empty response."

    # ── 5. Episodic record (non-blocking) ──────────────────────────────────
    asyncio.create_task(
        _write_episodic(qdrant, sources_ok, sources_failed, len(deduped), dedup_removed)
    )

    overall_status = "ok" if len(sources_ok) == 3 else ("partial" if sources_ok else "no_results")

    return {
        "status": overall_status,
        "brief": brief,
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
        "item_count": len(deduped),
        "dedup_removed": dedup_removed,
        "result_for_translator": brief,
    }
