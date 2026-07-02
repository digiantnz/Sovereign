"""Cognition Engine — RSS subject-relevance scoring.

Replaces the read_feed step in the Weekday Morning Briefing task (does not
append after it — task_scheduler.py steps don't share data with each other,
each step is dispatched independently and only its rendered summary is
joined at the end, so a scoring step has to fetch its own RSS entries).

Many-to-many: every story is scored against every active subject (no
feed-to-subject membership filtering — 14 feeds span ai/crypto/macro without
a clean 1:1 mapping, and per-subject batch scoring is cheap enough that the
LLM can just judge relevance directly). One story can spawn campaigns for
more than one subject (e.g. a Fed-rate story relevant to both crypto and
macro).

Scoring is batched per-subject (one LLM call scores ALL stories against that
subject's thesis) rather than per-story-per-subject — M calls instead of
N×M, and still lets the LLM judge each story individually within that call.
"""
from __future__ import annotations

import json
import logging
import re

from cognition.campaigns import run_campaign

logger = logging.getLogger(__name__)

_RSS_LIMIT = 20  # generous — batched scoring makes a wider net cheap


async def _list_active_subjects(qdrant) -> list[dict]:
    """Enumerate all semantic:subject:<id> entries — deterministic scroll,
    not vector search, since we want every active subject, not top-K."""
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        from execution.adapters.qdrant import SEMANTIC
        points, _ = await qdrant.archive_client.scroll(
            collection_name=SEMANTIC,
            scroll_filter=Filter(must=[
                FieldCondition(key="domain", match=MatchValue(value="subject")),
                FieldCondition(key="status", match=MatchValue(value="active")),
            ]),
            limit=50, with_payload=True, with_vectors=False,
        )
        return [dict(p.payload or {}) for p in points]
    except Exception as exc:
        logger.warning("_list_active_subjects: failed: %s", exc)
        return []


async def _score_stories_for_subject(cog, subject: dict, entries: list[dict]) -> dict[int, str]:
    """One LLM call — score every story against one subject's thesis.

    Returns {story_index: "relevant"|"borderline"|"ignore"}. Missing/unparsed
    indices default to "ignore" (fail closed — a scoring miss should not
    silently spawn an unreviewed campaign).
    """
    subject_id = subject.get("subject", "")
    thesis = subject.get("thesis", "")
    stories_block = "\n".join(
        f"{i}. {e.get('title', '(no title)')} — {(e.get('summary') or '')[:150]}"
        for i, e in enumerate(entries)
    )
    prompt = f"""Subject: {subject_id}
Current thesis: {thesis}

Score each story below for relevance to this subject's ongoing thesis.

Stories:
{stories_block}

For each story index, respond with:
- relevant: materially tests or changes the thesis — worth a research campaign
- borderline: tangentially related, worth logging but not campaign-worthy
- ignore: not relevant to this subject

Respond with JSON only — no preamble:
{{"scores": [{{"index": 0, "relevance": "relevant|borderline|ignore"}}, ...]}}"""

    try:
        result = await cog.ask_local(prompt, timeout=90.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        scores = {}
        for item in data.get("scores", []):
            idx = item.get("index")
            rel = item.get("relevance", "ignore")
            if isinstance(idx, int) and rel in ("relevant", "borderline", "ignore"):
                scores[idx] = rel
        return scores
    except Exception as exc:
        logger.warning("_score_stories_for_subject: failed for subject=%r: %s", subject_id, exc)
        return {}


async def _log_borderline(qdrant, subject_id: str, story_title: str) -> None:
    try:
        from datetime import date
        await qdrant.store(
            collection="episodic",
            content=f"RSS story judged borderline-relevant to subject '{subject_id}': {story_title}",
            metadata={
                "type": "episodic", "event_type": "borderline_relevance",
                "subject": subject_id, "story_title": story_title,
                "ts": date.today().isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("_log_borderline: failed for subject=%r: %s", subject_id, exc)


async def _digest_remaining_stories(cog, entries: list[dict], remaining_indices: list[int]) -> dict:
    """Phase 8a — lightweight single LLM pass over stories that matched no
    subject. Not full news_brief-style narrative synthesis (too expensive
    for stories Rex already judged low-relevance) and not a flat title list
    either (too raw — half would be noise the Director has to mentally
    filter). Filters obvious noise and produces one line per remaining story.

    Returns {"stories": [{"title", "source", "one_line"}], "dropped": int}.
    Never raises — falls back to an untouched flat list (dropped=0) on any
    LLM/parse failure, so a digest failure never means silent discards.
    """
    if not remaining_indices:
        return {"stories": [], "dropped": 0}

    stories_block = "\n".join(
        f"{i}. {entries[i].get('title', '(no title)')} — {(entries[i].get('summary') or '')[:150]} "
        f"[source: {entries[i].get('feed', 'unknown')}]"
        for i in remaining_indices
    )
    prompt = f"""These stories were judged not relevant to any active subject Rex is tracking.

Stories:
{stories_block}

Do two things only:
1. Filter obvious noise — duplicates, low-signal filler, bare press releases.
2. For each remaining story, write one concise line capturing what it's about.

Respond with JSON only — no preamble:
{{"stories": [{{"index": 0, "one_line": "..."}}, ...]}}

Only include indices worth keeping — omit the noise you filtered."""

    fallback = {
        "stories": [
            {"title": entries[i].get("title", "(no title)"), "source": entries[i].get("feed", ""), "one_line": ""}
            for i in remaining_indices
        ],
        "dropped": 0,
    }
    try:
        result = await cog.ask_local(prompt, timeout=90.0)
        raw = result.get("response", "")
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else None
        if data is None:
            return fallback
        kept = []
        for item in data.get("stories", []):
            idx = item.get("index")
            if not isinstance(idx, int) or idx not in remaining_indices:
                continue
            kept.append({
                "title":    entries[idx].get("title", "(no title)"),
                "source":   entries[idx].get("feed", ""),
                "one_line": item.get("one_line", ""),
            })
        return {"stories": kept, "dropped": len(remaining_indices) - len(kept)}
    except Exception as exc:
        logger.warning("_digest_remaining_stories: failed, falling back to flat list: %s", exc)
        return fallback


async def run_score_rss_by_subject(cog, nanobot, qdrant) -> dict:
    """Scheduler step entry point. Fetches RSS, scores against every active
    subject, spawns campaigns for relevant matches, returns a brief summary.

    Phase 8a — two output buckets, Director always sees everything:
    subject-relevant stories lead (grouped, full campaign/borderline detail),
    remaining stories follow as a lightweight noise-filtered digest. Nothing
    is silently discarded — the digest's `dropped` count surfaces how many
    the noise filter removed.

    Returns {"status": "ok", "brief": "..."} — matches the shape
    task_scheduler.py's _format_step_content() already recognises via
    res.get("brief") (the news_brief harness's shape), so the scheduled
    task's Telegram notification renders this with zero scheduler changes.
    """
    entries_result = await nanobot.run("rss-digest", "get_entries", {"limit": _RSS_LIMIT})
    result = entries_result.get("result") if entries_result.get("result") is not None else entries_result
    entries = result.get("entries", []) if isinstance(result, dict) else []
    if not entries:
        return {"status": "ok", "brief": "No RSS entries fetched this run."}

    subjects = await _list_active_subjects(qdrant)
    if not subjects:
        return {"status": "ok", "brief": f"{len(entries)} headlines fetched — no active subjects to score against."}

    campaigns_spawned: list[str] = []
    borderline_count = 0
    matched_indices: set[int] = set()

    for subject in subjects:
        subject_id = subject.get("subject", "")
        scores = await _score_stories_for_subject(cog, subject, entries)
        for idx, relevance in scores.items():
            if idx >= len(entries):
                continue
            if relevance in ("relevant", "borderline"):
                matched_indices.add(idx)
            story = entries[idx]
            title = story.get("title", "(no title)")
            if relevance == "relevant":
                await run_campaign(qdrant, nanobot, cog, subject_id, "rss", title)
                campaigns_spawned.append(f"{subject_id}: {title}")
            elif relevance == "borderline":
                await _log_borderline(qdrant, subject_id, title)
                borderline_count += 1

    remaining_indices = [i for i in range(len(entries)) if i not in matched_indices]
    digest = await _digest_remaining_stories(cog, entries, remaining_indices)

    lines = [f"{len(entries)} headlines scored against {len(subjects)} subject(s)."]
    if campaigns_spawned:
        lines.append(f"\n{len(campaigns_spawned)} campaign(s) spawned:")
        lines += [f"• {c}" for c in campaigns_spawned]
    else:
        lines.append("No stories judged campaign-worthy this run.")
    if borderline_count:
        lines.append(f"\n{borderline_count} borderline stories logged to subject episodic trails.")

    if digest["stories"]:
        lines.append(f"\nOther headlines ({len(digest['stories'])}):")
        for s in digest["stories"]:
            one_line = f" — {s['one_line']}" if s.get("one_line") else ""
            lines.append(f"• {s['title']} [{s['source']}]{one_line}")
    if digest["dropped"]:
        lines.append(f"\n({digest['dropped']} low-signal item(s) filtered)")

    return {"status": "ok", "brief": "\n".join(lines)}
