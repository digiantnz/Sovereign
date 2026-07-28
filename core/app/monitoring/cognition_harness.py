"""Cognition Engine — RSS and email subject-relevance scoring for the
Weekday Morning Briefing.

Both replace (not append after) existing briefing steps — task_scheduler.py
steps don't share data with each other, each step is dispatched independently
and only its rendered summary is joined at the end, so a scoring step has to
fetch its own entries.

Many-to-many: every story is scored against every active subject (no
feed-to-subject membership filtering — 14 feeds span ai/crypto/macro without
a clean 1:1 mapping, and per-subject batch scoring is cheap enough that the
LLM can just judge relevance directly). One story can spawn thoughts for
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

from cognition.thoughts import spawn_thought
from cognition.subjects import list_active_subjects as _list_active_subjects
from cognition.subjects import _MAX_SUBJECT_MATCHES

logger = logging.getLogger(__name__)

_RSS_LIMIT = 20  # generous — batched scoring makes a wider net cheap


async def _score_stories_for_subject(cog, subject: dict, entries: list[dict]) -> dict[int, str]:
    """One LLM call — score every story against one subject's thesis.

    Returns {story_index: "relevant"|"borderline"|"ignore"}. Missing/unparsed
    indices default to "ignore" (fail closed — a scoring miss should not
    silently spawn an unreviewed thought).
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
- relevant: materially tests or changes the thesis — worth a research thought
- borderline: tangentially related, worth logging but not thought-worthy
- ignore: not relevant to this subject

Respond with JSON only — no preamble:
{{"scores": [{{"index": 0, "relevance": "relevant|borderline|ignore"}}, ...]}}"""

    try:
        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=90.0)
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


async def _log_borderline(qdrant, subject_id: str, story_title: str, trigger_source: str = "rss") -> None:
    try:
        from datetime import date
        await qdrant.store(
            collection="episodic",
            content=f"{trigger_source} item judged borderline-relevant to subject '{subject_id}': {story_title}",
            metadata={
                "type": "episodic", "event_type": "borderline_relevance",
                "subject": subject_id, "story_title": story_title,
                "trigger_source": trigger_source, "ts": date.today().isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("_log_borderline: failed for subject=%r: %s", subject_id, exc)


async def score_web_search_for_subjects(cog, nanobot, qdrant, results: list[dict]) -> None:
    """Fire-and-forget: score a web search's structured results against active
    Subjects, same scoring pattern as the RSS scorer. Called via asyncio.create_task
    right after a "search the web" call returns — never on the Director's response
    critical path. On a relevant match, spawns a real run_thought(); on borderline,
    logs to the subject's episodic trail. Silent on no matches (no brief to build —
    this isn't a scheduled digest, just an ambient trigger)."""
    logger.info("score_web_search_for_subjects: called with %d results", len(results) if results else 0)
    if not results:
        return
    entries = [
        {"title": r.get("title", "(no title)"), "summary": r.get("snippet", ""), "feed": r.get("url", "")}
        for r in results
    ]
    try:
        from cognition.subjects import find_relevant_subjects
        # One embed call, not one LLM call per subject — most searches aren't
        # relevant to most (or any) Subject; triage first, full-score only hits.
        triage_text = "\n".join(f"{e['title']} — {e['summary'][:150]}" for e in entries)
        subjects = await find_relevant_subjects(qdrant, triage_text)
        logger.info("score_web_search_for_subjects: triage found %d subject hits", len(subjects))
        for subject in subjects:
            subject_id = subject.get("subject", "")
            scores = await _score_stories_for_subject(cog, subject, entries)
            for idx, relevance in scores.items():
                if idx >= len(entries):
                    continue
                title = entries[idx]["title"]
                if relevance == "relevant":
                    await spawn_thought(qdrant, nanobot, cog, subject_id, "web_search", title)
                elif relevance == "borderline":
                    await _log_borderline(qdrant, subject_id, title, trigger_source="web_search")
    except Exception as exc:
        logger.warning("score_web_search_for_subjects: failed: %s", exc)


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
        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=90.0)
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
    subject, spawns thoughts for relevant matches, returns a brief summary.

    Phase 8a — two output buckets, Director always sees everything:
    subject-relevant stories lead (grouped, full thought/borderline detail),
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

    thoughts_spawned: list[str] = []
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
                task = await spawn_thought(qdrant, nanobot, cog, subject_id, "rss", title)
                if task is not None:
                    thoughts_spawned.append(f"{subject_id}: {title}")
            elif relevance == "borderline":
                await _log_borderline(qdrant, subject_id, title)
                borderline_count += 1

    remaining_indices = [i for i in range(len(entries)) if i not in matched_indices]
    digest = await _digest_remaining_stories(cog, entries, remaining_indices)

    lines = [f"{len(entries)} headlines scored against {len(subjects)} subject(s)."]
    if thoughts_spawned:
        lines.append(f"\n{len(thoughts_spawned)} thought(s) spawned:")
        lines += [f"• {c}" for c in thoughts_spawned]
    else:
        lines.append("No stories judged thought-worthy this run.")
    if borderline_count:
        lines.append(f"\n{borderline_count} borderline stories logged to subject episodic trails.")

    if digest["stories"]:
        lines.append(f"\nOther headlines ({len(digest['stories'])}):")
        for s in digest["stories"]:
            one_line = f" — {s['one_line']}" if s.get("one_line") else ""
            lines.append(f"• {s['title']} [{s['source']}]{one_line}")
    if digest["dropped"]:
        lines.append(f"\n({digest['dropped']} low-signal item(s) filtered)")

    # Same fix as score_email_by_subject below — "brief" is always non-empty
    # even on a no-op run, so an explicit "count" is needed for
    # notify_when="on_findings" to mean anything.
    return {
        "status": "ok", "brief": "\n".join(lines),
        "count": len(thoughts_spawned) + borderline_count,
    }


_EMAIL_ACCOUNTS = ("personal", "business")
_EMAIL_LIMIT = 20  # per account


async def run_score_email_by_subject(cog, nanobot, qdrant, accounts: list[str] | None = None) -> dict:
    """Scores unread mail against active Subjects for priority + urgency.

    Two callers, same function (standing order #3 — one implementation):
    the Weekday Morning Briefing step (accounts=["business"], on the
    briefing's critical path — business IMAP is fast) and the standalone
    Personal Mailbox Scan task (accounts=["personal"], its own slower daily
    schedule — personal IMAP is much slower, 20-55s+, and was blocking the
    briefing before this split; see task #30 2026-07-03). `accounts=None`
    (default) scores both, for any future ad-hoc/manual call.

    Now also a Thought trigger (task #44, 2026-07-03) — reverses the earlier
    "prioritization only" decision. Every non-phishing Subject match calls
    observe_for_subject() (cognition/thoughts.py), the same generic gap-check
    entry point Portfolio uses — cheap for the common case (one
    evaluate_thought_iteration call), and only actually spawns a Thought when
    a genuine gap is found. This harness stays exactly as ignorant of what a "gap" is
    as Portfolio does — no gap-detection logic duplicated here. Phishing
    emails are excluded (not a genuine finding). `/learn from email <id>`
    remains available for folding a specific email in on demand regardless.

    Triage itself is still zero-LLM — embedding-only (find_relevant_subjects)
    for relevance, deterministic keyword/sender pattern matching
    (derive_urgency) for urgency. Metadata only (subject/sender), not body —
    fetching every body would be the same expensive mistake as scoring every
    Subject regardless of relevance, just moved one step earlier. The one
    new cost is observe_for_subject()'s own cheap gap-check call, made only
    for actual Subject matches (a small fraction of inbox volume), not per
    email.
    """
    import asyncio
    import datetime as _dt_mod
    import uuid

    from config import cfg as _cfg
    from cognition.subjects import (
        find_relevant_subjects, derive_urgency, get_priority_senders, detect_brand_mismatch,
        record_spam_sender, record_urgent_sender,
    )
    from cognition.thoughts import observe_for_subject
    from monitoring.email_event_suggest import (
        looks_like_calendar_candidate, detect_calendar_event, write_pending_suggestion,
    )

    # Bounds the (fetch body + LLM) cost of calendar-invite detection per run —
    # looks_like_calendar_candidate() is a cheap keyword pre-filter, but this
    # cap is the hard backstop in case a run has an unusually high hit rate.
    _MAX_CALENDAR_CHECKS = 5

    priority_senders = get_priority_senders()
    scan_accounts = accounts or _EMAIL_ACCOUNTS

    all_emails: list[dict] = []
    for account in scan_accounts:
        try:
            nb = await nanobot.run("nc-mail", "list_unread",
                                    {"account": account, "limit": _EMAIL_LIMIT, "unread_only": "false"})
            result = nb.get("result") if nb.get("result") is not None else nb
            msgs = result.get("messages", []) if isinstance(result, dict) else []
        except Exception as exc:
            logger.warning("run_score_email_by_subject: fetch failed for account=%r: %s", account, exc)
            msgs = []
        for m in msgs:
            m["account"] = account
        all_emails.extend(msgs)

    if not all_emails:
        return {"status": "ok", "brief": "No emails fetched this run.", "count": 0}

    urgent: list[dict] = []
    phishing: list[dict] = []
    deleted_spam: list[dict] = []
    relevant: list[dict] = []
    calendar_candidates: list[dict] = []
    routine_count = 0
    calendar_checks_used = 0

    for email in all_emails:
        subject_line = email.get("subject", "")
        sender = email.get("from", "")
        phishing_flagged = detect_brand_mismatch(subject_line, sender)
        urgency_label, _ = derive_urgency(
            subject_line, sender, priority_senders=priority_senders, phishing_flagged=phishing_flagged,
        )
        hits = await find_relevant_subjects(qdrant, f"{subject_line} — from {sender}")

        if phishing_flagged:
            # Urgency is deliberately capped at "low" by derive_urgency() above
            # (see its docstring) so this never also lands in "Urgent".
            # Excluded from observe_for_subject() below — not a genuine finding.
            #
            # Auto-delete exception (Director, 2026-07-07): detect_brand_mismatch()
            # is the one well-established, low-ambiguity spam signal in the system
            # (sender claims a known brand, sending domain doesn't match it) — the
            # classification itself IS the confirmation, so this bypasses
            # delete_email's normal HIGH-tier confirmation gate rather than
            # flagging-then-waiting-to-be-told-to-delete. Nextcloud Mail's delete
            # moves to that account's Trash (not instant permanent erase), so
            # there's still a recovery window. Toggle: cognition.spam_auto_delete.
            asyncio.create_task(record_spam_sender(qdrant, sender, subject_line, reason="brand_mismatch"))
            deleted = False
            if _cfg.cognition.spam_auto_delete:
                database_id = email.get("databaseId") or email.get("uid") or ""
                if database_id:
                    try:
                        del_nb = await nanobot.run("nc-mail", "delete_message", {
                            "account": email.get("account", "personal"), "database_id": database_id,
                        })
                        del_result = del_nb.get("result") if del_nb.get("result") is not None else del_nb
                        deleted = isinstance(del_result, dict) and not del_result.get("error")
                    except Exception as exc:
                        logger.warning("run_score_email_by_subject: spam auto-delete failed: %s", exc)
            if deleted:
                deleted_spam.append({**email, "matched_subjects": [h.get("subject") for h in hits]})
            else:
                phishing.append({**email, "matched_subjects": [h.get("subject") for h in hits]})
        elif urgency_label == "high":
            urgent.append({**email, "matched_subjects": [h.get("subject") for h in hits]})
            asyncio.create_task(record_urgent_sender(
                qdrant, sender, subject_line,
                reason="priority_sender" if sender in priority_senders else "urgency_keyword_match",
            ))
        elif hits:
            relevant.append({**email, "matched_subjects": [h.get("subject") for h in hits]})
        else:
            routine_count += 1

        if hits and not phishing_flagged:
            observation = f"Email: '{subject_line}' from {sender}"
            for h in hits[:_MAX_SUBJECT_MATCHES]:
                subject_id = h.get("subject", "")
                if subject_id:
                    await observe_for_subject(qdrant, nanobot, cog, subject_id, "email", observation)

        # Calendar-invite suggestion (Director, 2026-07-07) — cheap keyword
        # pre-filter first; body fetch + detect_calendar_event()'s LLM call
        # only for the small fraction that clears it, never for phishing.
        # Suggests only — never auto-creates the event.
        if (not phishing_flagged and calendar_checks_used < _MAX_CALENDAR_CHECKS
                and looks_like_calendar_candidate(subject_line, sender)):
            calendar_checks_used += 1
            database_id = email.get("databaseId") or email.get("uid") or ""
            body_text = ""
            if database_id:
                try:
                    fb = await nanobot.run("nc-mail", "fetch_message", {
                        "account": email.get("account", "personal"), "database_id": database_id,
                    })
                    fb_result = fb.get("result") if fb.get("result") is not None else fb
                    body_text = fb_result.get("body", "") if isinstance(fb_result, dict) else ""
                except Exception as exc:
                    logger.warning("run_score_email_by_subject: body fetch failed for calendar check: %s", exc)
            if body_text:
                try:
                    ev = await detect_calendar_event(
                        cog, subject_line, sender, body_text, _dt_mod.date.today().isoformat(),
                    )
                except Exception as exc:
                    logger.warning("run_score_email_by_subject: calendar detection failed: %s", exc)
                    ev = None
                if ev:
                    _acct = email.get("account", "personal")
                    _suggestion_id = str(uuid.uuid5(
                        uuid.NAMESPACE_URL, f"calendar_suggestion:{_acct}:{database_id}",
                    ))
                    calendar_candidates.append({**email, **ev, "_suggestion_id": _suggestion_id})
                    asyncio.create_task(write_pending_suggestion(
                        qdrant, _suggestion_id, ev, _acct, subject_line, sender,
                    ))

    lines = [f"{len(all_emails)} email(s) across {len(scan_accounts)} account(s)."]
    if urgent:
        lines.append(f"\n⚠️ Urgent ({len(urgent)}):")
        for e in urgent:
            tag = f" [{', '.join(e['matched_subjects'])}]" if e["matched_subjects"] else ""
            lines.append(f"• {e.get('subject','(no subject)')} — {e.get('from','')} ({e.get('account')}){tag}")
    if deleted_spam:
        lines.append(f"\n🗑️ Deleted as spam ({len(deleted_spam)}) — sender name doesn't match its domain:")
        for e in deleted_spam:
            lines.append(f"• {e.get('subject','(no subject)')} — {e.get('from','')} ({e.get('account')})")
    if phishing:
        lines.append(f"\n🎣 Possible phishing, not deleted ({len(phishing)}) — sender name doesn't match its "
                      f"domain (delete failed or spam_auto_delete is off):")
        for e in phishing:
            lines.append(f"• {e.get('subject','(no subject)')} — {e.get('from','')} ({e.get('account')})")
    if relevant:
        lines.append(f"\nSubject-relevant ({len(relevant)}):")
        for e in relevant:
            lines.append(f"• {e.get('subject','(no subject)')} — {e.get('from','')} "
                         f"({e.get('account')}) [{', '.join(e['matched_subjects'])}]")
    if calendar_candidates:
        lines.append(f"\n📅 Possible calendar event(s) ({len(calendar_candidates)}) — not created, "
                      f"reply \"book it\" (or name the event) any time to confirm:")
        for e in calendar_candidates:
            when = f"{e.get('date','?')} {e.get('start_time','')}".strip()
            lines.append(f"• {e.get('title', e.get('subject','(no subject)'))} — {when} "
                         f"({e.get('account')}) — \"{e.get('source_excerpt','')}\"")
    if routine_count:
        lines.append(f"\n{routine_count} routine email(s) — no Subject match, not urgent.")

    # count of NOTABLE items only (not routine_count) — this is what
    # task_scheduler._result_has_content() gates notify_when="on_findings" on.
    # Without this, "brief" (always non-empty, even all-routine) would make
    # has_content always True regardless of notify_when — found live
    # 2026-07-03 as spam once Personal Mailbox Scan moved to 15-min cadence.
    return {
        "status": "ok", "brief": "\n".join(lines),
        "count": len(urgent) + len(phishing) + len(deleted_spam) + len(relevant) + len(calendar_candidates),
    }
