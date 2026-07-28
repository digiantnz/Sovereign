"""Sovereign News Harness — run_news_brief()

Fetches news from RSS and browser search (default topics + Subject-bound
keywords) in parallel; deduplicates; synthesises into a single brief via one
local Ollama call. Grok dropped 2026-07-04 — its live web search was
deprecated by xAI, so asking it for "current headlines" answered from stale
training knowledge instead of anything real; RSS + browser are both grounded
sources, Grok added nothing but risk of fabricated "current" items.
"""

import asyncio
import logging
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


async def _fetch_browser(nanobot, topics: list[str], source_tag: str = "browser") -> tuple[list[dict], str | None]:
    """Search via sovereign-browser for current news.

    source_tag lets callers distinguish which topic set produced which items
    (e.g. "browser" for default/preference topics vs "browser_subjects" for
    Subject-bound search) without duplicating this function.
    """
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
                items.append({"title": str(title), "summary": str(content)[:300], "source": source_tag})
        logger.info("news_harness: browser search (%s) returned %d items", source_tag, len(items))
        return items, None
    except Exception as exc:
        logger.warning("news_harness: browser source (%s) failed: %s", source_tag, exc)
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

async def _synthesise(cog, items: list[dict], prefs_text: str, user_input: str = "") -> str:
    """One LLM call to synthesise all items into a 5–8 bullet brief."""
    numbered = "\n".join(
        f"{i+1}. {item['title']} — {item['summary']}"
        for i, item in enumerate(items[:30])
    )
    body = (
        f"Director preferences: {prefs_text}\n\n"
        "Here are today's news items from multiple sources:\n"
        f"{numbered}\n\n"
        "Synthesise ONLY the items listed above into a concise news brief of 5–8 bullet points. "
        "Use only the titles and summaries provided — do not add information from your training data. "
        "Weight items toward the Director's stated preferences. "
        "Write each bullet as one clear sentence. "
        "Do NOT include source names, URLs, or metadata — only the synthesised content. "
        "Start each bullet with •"
    )
    _decision = cog._routing_decision(body, user_input=user_input, task_type="llm_generate")
    if _decision["use_external"]:
        _dispatch_map = {
            "grok":           cog.ask_grok,
            "gemini":         cog.ask_gemini,
            "groq_inference": cog.ask_groq_inf,
            "openrouter":     cog.ask_openrouter,
            "ollama_cloud":   cog.ask_ollama_cloud,
        }
        _fn = _dispatch_map.get(_decision["provider"], cog.ask_grok)
        result = await _fn(body, agent="research_agent", routing_decision=_decision)
        return result.get("response", "") if isinstance(result, dict) else str(result)
    # /no_think suppresses qwen3 extended reasoning for this extraction-only step
    from adapters.inference_queue import InferenceQueue
    result = await cog.ask_local(
        "/no_think\n" + body, priority=InferenceQueue.NORMAL, timeout=180.0
    )
    if result.get("status") == "llm_timeout":
        logger.warning("news_harness: synthesis timed out")
        return ""
    raw = result.get("response", "") if isinstance(result, dict) else str(result)
    logger.debug("news_harness: synthesis produced %d chars", len(raw))
    return raw


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
    Fetch news from RSS and browser (default topics + Subject-bound keywords)
    in parallel; dedup; synthesise.

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

    # ── 1b. Subject-bound keywords (2026-07-04) — high-level Subjects only
    #        (crypto/ai/macro/retirement/property, not their _sub-focus
    #        children — Matt's call, narrower Subjects would just multiply
    #        query volume for marginal benefit).
    subject_keywords: list[str] = []
    try:
        from cognition.subjects import get_subject_news_keywords
        subject_keywords = await get_subject_news_keywords(qdrant)
    except Exception as exc:
        logger.warning("news_harness: subject keyword fetch failed: %s", exc)

    # ── 2. Parallel fetch — RSS, default-topic browser search, and (if any
    #      high-level Subjects have search_keywords) a second Subject-bound
    #      browser search. Grok dropped (see module docstring).
    tasks = [
        asyncio.create_task(_fetch_rss(nanobot)),
        asyncio.create_task(_fetch_browser(nanobot, topics)),
    ]
    if subject_keywords:
        tasks.append(asyncio.create_task(_fetch_browser(nanobot, subject_keywords, source_tag="browser_subjects")))

    results = await asyncio.gather(*tasks)
    rss_items,     rss_err     = results[0]
    browser_items, browser_err = results[1]
    subject_items,  subject_err = results[2] if len(results) > 2 else ([], None)

    sources_ok     = []
    sources_failed = []

    if rss_err is None:
        sources_ok.append("rss")
    else:
        sources_failed.append(f"rss: {rss_err}")

    if browser_err is None:
        sources_ok.append("browser")
    else:
        sources_failed.append(f"browser: {browser_err}")

    if subject_keywords:
        if subject_err is None:
            sources_ok.append("browser_subjects")
        else:
            sources_failed.append(f"browser_subjects: {subject_err}")

    all_items = rss_items + browser_items + subject_items

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
    brief = await _synthesise(cog, deduped, prefs_text, user_input=user_input)

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
