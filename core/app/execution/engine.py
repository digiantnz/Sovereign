import json
import os
from datetime import datetime, timezone

from adapters.broker import BrokerAdapter
from adapters.webdav import WebDAVAdapter
from adapters.caldav import CalDAVAdapter
from adapters.imap import IMAPAdapter
from adapters.smtp import SMTPAdapter
from adapters.nanobot import NanobotAdapter
from execution.adapters.github import GitHubAdapter
from execution.adapters.browser import BrowserAdapter
from execution.adapters.qdrant import (
    WORKING, SOVEREIGN_COLLECTIONS, CONFIDENCE_THRESHOLD,
    SEMANTIC, PROCEDURAL, EPISODIC, PROSPECTIVE, ASSOCIATIVE, RELATIONAL, META,
)

# Source tags — every adapter result is stamped with one of these so the
# cognition layer can always distinguish a live adapter result from Qdrant memory.
_DOMAIN_SOURCE = {
    "docker":     "broker_live",
    "webdav":     "webdav_live",
    "caldav":     "caldav_live",
    "ollama":     "ollama_live",
    "browser":    "browser_live",
    "security":   "github_live",
    "github":     "github_live",
    "skills":     "skills_live",
    "memory":     "qdrant_memory",
    "wallet":     "wallet_live",
    "scheduler":  "scheduler_live",
    "nanobot":    "nanobot_live",
}

# Maps CEO delegation intent → execution action dict
# target field from delegation fills "container", "path", "account" etc.
INTENT_ACTION_MAP = {
    # Docker agent intents
    "list_containers":    {"domain": "docker", "operation": "read",    "name": "docker_ps"},
    "get_logs":           {"domain": "docker", "operation": "read",    "name": "docker_logs"},
    "get_stats":          {"domain": "docker", "operation": "read",    "name": "docker_stats"},
    "restart_container":  {"domain": "docker", "operation": "restart", "name": "docker_restart"},
    # Business agent intents
    "list_files":         {"domain": "webdav", "operation": "read",    "name": "file_list"},
    "read_file":          {"domain": "webdav", "operation": "read",    "name": "file_read"},
    "write_file":         {"domain": "webdav", "operation": "write",   "name": "file_write"},
    "delete_file":        {"domain": "webdav", "operation": "delete",  "name": "file_delete"},
    "create_folder":      {"domain": "webdav", "operation": "mkdir",   "name": "folder_create"},
    "fetch_email":        {"domain": "mail",   "operation": "read",    "name": "mail_fetch_unread"},
    "search_email":       {"domain": "mail",   "operation": "search",  "name": "mail_search"},
    "move_email":         {"domain": "mail",   "operation": "move",    "name": "mail_move"},
    "delete_email":       {"domain": "mail",   "operation": "delete",  "name": "mail_delete"},
    "send_email":         {"domain": "mail",   "operation": "send",    "name": "mail_send"},
    "list_calendars":     {"domain": "caldav", "operation": "read",    "name": "calendar_read"},
    "create_event":       {"domain": "caldav", "operation": "write",   "name": "calendar_create"},
    "create_task":        {"domain": "caldav", "operation": "write",   "name": "task_create"},
    "delete_task":        {"domain": "caldav", "operation": "delete",  "name": "task_delete"},
    # Research agent intents
    "query":              {"domain": "ollama", "operation": "query"},
    "research":           {"domain": "ollama", "operation": "query"},
    "web_search":         {"domain": "browser", "operation": "search", "name": "browser_search"},
    "fetch_url":          {"domain": "browser", "operation": "fetch",  "name": "browser_fetch"},
    # Memory intents
    "remember_fact":      {"domain": "memory",  "operation": "write"},
    # GitHub intents — devops_agent scope (read also available to research_agent)
    # Protected operations (PAT modification, repo creation, visibility change) NOT exposed.
    "github_read":          {"domain": "github", "operation": "read",      "name": "github_read"},
    "github_push_doc":      {"domain": "github", "operation": "push_doc",  "name": "github_push_doc"},
    "github_push_soul":     {"domain": "github", "operation": "push_soul", "name": "github_push_soul"},
    "github_push_security": {"domain": "github", "operation": "push_sec",  "name": "github_push_security"},
    # Skill lifecycle intents — devops_agent scope
    "skill_search":  {"domain": "skills", "operation": "search"},
    "skill_review":  {"domain": "skills", "operation": "review"},
    "skill_load":    {"domain": "skills", "operation": "load"},
    "skill_audit":   {"domain": "skills", "operation": "audit"},
    "skill_install": {"domain": "skills", "operation": "install"},  # composite: search→review→load
    # Nanobot intents — delegated execution sidecar (MID tier minimum)
    "nanobot_run":    {"domain": "nanobot", "operation": "run",    "name": "nanobot_run"},
    "nanobot_health": {"domain": "nanobot", "operation": "health", "name": "nanobot_health"},
    # System examination intents — devops_agent scope (all LOW, all read-only via broker)
    "inspect_container":  {"domain": "docker", "operation": "read", "name": "inspect_container"},
    "get_compose":        {"domain": "docker", "operation": "read", "name": "get_compose"},
    "read_host_file":     {"domain": "docker", "operation": "read", "name": "host_file_read"},
    "get_hardware":       {"domain": "docker", "operation": "read", "name": "get_hardware"},
    "list_processes":     {"domain": "docker", "operation": "read", "name": "list_processes"},
    # WebDAV navigate — returns items with full paths (superset of list_files)
    "navigate":      {"domain": "webdav", "operation": "read",    "name": "file_navigate"},
    "search_files":  {"domain": "webdav", "operation": "search",  "name": "file_search"},
    # CalDAV extended
    "list_events":   {"domain": "caldav", "operation": "read",    "name": "calendar_list_events"},
    "complete_task": {"domain": "caldav", "operation": "write",   "name": "task_complete"},
    "delete_event":  {"domain": "caldav", "operation": "delete",  "name": "calendar_delete"},
    "update_event":  {"domain": "caldav", "operation": "write",   "name": "calendar_update"},
    # Mail extended
    "fetch_message": {"domain": "mail",   "operation": "fetch",   "name": "mail_fetch_one"},
    "mark_read":     {"domain": "mail",   "operation": "flag",    "name": "mail_mark_read"},
    "mark_unread":   {"domain": "mail",   "operation": "flag",    "name": "mail_mark_unread"},
    "list_folders":  {"domain": "mail",   "operation": "read",    "name": "mail_list_folders"},
    "list_inbox":    {"domain": "mail",   "operation": "read",    "name": "mail_list_inbox"},
    # Scheduler intents — devops_agent scope
    "schedule_task": {"domain": "scheduler", "operation": "schedule", "name": "schedule_task"},
    "list_tasks":    {"domain": "scheduler", "operation": "list",     "name": "list_tasks"},
    "pause_task":    {"domain": "scheduler", "operation": "update",   "name": "pause_task"},
    "cancel_task":   {"domain": "scheduler", "operation": "update",   "name": "cancel_task"},
    # Wallet intents — devops_agent scope (LOW/MID/HIGH tier)
    "wallet_read_config":     {"domain": "wallet", "operation": "read",    "name": "wallet_read_config"},
    "wallet_get_address":     {"domain": "wallet", "operation": "read",    "name": "wallet_get_address"},
    "wallet_sign_message":    {"domain": "wallet", "operation": "sign",    "name": "wallet_sign_message"},
    "wallet_propose_safe_tx": {"domain": "wallet", "operation": "propose", "name": "wallet_propose_safe_tx"},
    "wallet_get_proposals":   {"domain": "wallet", "operation": "read",    "name": "wallet_get_proposals"},
    "wallet_get_btc_xpub":    {"domain": "wallet", "operation": "read",    "name": "wallet_get_btc_xpub"},
}

# Tier required for each operation — deterministic, never from LLM
INTENT_TIER_MAP = {
    "inspect_container": "LOW", "get_compose": "LOW", "read_host_file": "LOW",
    "get_hardware": "LOW", "list_processes": "LOW",
    "list_containers": "LOW", "get_logs": "LOW", "get_stats": "LOW",
    "list_files": "LOW", "navigate": "LOW", "read_file": "LOW", "search_files": "LOW",
    "fetch_email": "LOW", "search_email": "LOW", "fetch_message": "LOW",
    "mark_read": "LOW", "mark_unread": "LOW", "list_folders": "LOW", "list_inbox": "LOW",
    "move_email": "MID",
    "list_calendars": "LOW", "list_events": "LOW",
    "delete_event": "MID", "update_event": "MID",
    "query": "LOW", "research": "LOW", "web_search": "LOW", "fetch_url": "LOW",
    "restart_container": "MID", "write_file": "MID", "send_email": "MID", "create_event": "MID",
    "create_task": "MID", "complete_task": "MID", "create_folder": "MID",
    "delete_file": "HIGH", "delete_email": "HIGH", "delete_task": "HIGH",
    "remember_fact": "LOW",
    # NOTE: skill_* tiers are governed by governance.json intent_tiers — not hardcoded here
    # Wallet tiers
    "wallet_read_config":     "LOW",
    "wallet_get_address":     "MID",
    "wallet_sign_message":    "MID",
    "wallet_propose_safe_tx": "HIGH",
    "wallet_get_proposals":   "MID",
    "wallet_get_btc_xpub":    "LOW",
    # Scheduler tiers
    "schedule_task": "MID",
    "list_tasks":    "LOW",
    "pause_task":    "MID",
    "cancel_task":   "MID",
    # Nanobot tiers — nanobot_run is MID (shell access), nanobot_health is LOW (read-only check)
    "nanobot_run":    "MID",
    "nanobot_health": "LOW",
    # GitHub — tiers enforced per governance policy
    "github_read":          "LOW",   # monitoring, releases, pending updates
    "github_push_doc":      "MID",   # standard docs and as-built updates
    "github_push_soul":     "HIGH",  # soul/constitution/governance docs — double confirmation
    "github_push_security": "HIGH",  # security pattern files — double confirmation
}

def _normalise_dt(value: str, default_year: int = 2026) -> str:
    """Normalise a freeform datetime string to ISO 8601 YYYY-MM-DDTHH:MM:SS.

    Accepts: ISO strings (with/without T), "YYYY-MM-DD HH:MM", natural language
    like "Monday 16th March at 10AM NZST", or split date+time concatenated as
    "DATE TIME" (caller should join them with a space before passing).

    Returns empty string if parsing fails — caller decides whether that is fatal.
    Timezone abbreviations NZDT/NZST/NZT/UTC/GMT are stripped; the resulting
    datetime is naive (CalDAVAdapter formats it without TZID, which is correct
    for Nextcloud floating-time events displayed in the server's local timezone).
    """
    import re as _re
    from datetime import datetime as _dt

    if not value or not isinstance(value, str):
        return ""
    v = value.strip()

    # Strip known timezone abbreviations (they are NZ-local anyway; CalDAV stores floating)
    v = _re.sub(r'\s*\b(NZDT|NZST|NZT|UTC|GMT)\b', '', v, flags=_re.IGNORECASE).strip()

    # Strip ordinal suffixes: 1st 2nd 3rd 4th ... 31st
    v = _re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', v, flags=_re.IGNORECASE)

    # Normalise "at NNam/pm" → "NN am/pm" so strptime %I%p works cleanly
    v = _re.sub(r'\bat\s+', ' ', v).strip()

    # Already ISO-like: "YYYY-MM-DD HH:MM..." or "YYYY-MM-DDTHH:MM..."
    _iso_m = _re.match(r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)', v)
    if _iso_m:
        date_part = _iso_m.group(1)
        time_part = _iso_m.group(2)
        if len(time_part) == 5:
            time_part += ":00"
        return f"{date_part}T{time_part}"

    # strptime candidates — ordered from most to least specific
    _fmts = [
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%A %d %B %Y %I:%M%p", "%A %d %B %Y %I%p",
        "%A %d %B %Y %H:%M",
        "%d %B %Y %I:%M%p",   "%d %B %Y %I%p",
        "%d %B %Y %H:%M",     "%d %B %Y",
        "%B %d %Y %I:%M%p",   "%B %d %Y %I%p",
        "%B %d, %Y %I:%M%p",  "%B %d, %Y %I%p",
        "%A %d %B %I:%M%p",   "%A %d %B %I%p",    # no year
        "%d %B %I:%M%p",      "%d %B %I%p",        # no year
        "%B %d %I:%M%p",      "%B %d %I%p",        # no year
        "%d/%m/%Y %H:%M",     "%m/%d/%Y %H:%M",
    ]
    for fmt in _fmts:
        try:
            parsed = _dt.strptime(v, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=default_year)
            return parsed.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue

    return ""


def _infer_prior_domain(context_window) -> str | None:
    """
    Scan the last 2-3 turns of conversation history to identify the active domain.
    Returns a domain hint string or None.
    context_window: list of {user, assistant} dicts or a single dict (legacy).
    """
    if not context_window:
        return None
    turns = context_window if isinstance(context_window, list) else [context_window]
    # Check last 2 turns (most recent signal wins)
    for turn in reversed(turns[-2:]):
        combined = (turn.get("user", "") + " " + turn.get("assistant", "")).lower()
        if any(w in combined for w in ("email", "inbox", "subject", "unread", "sender", "mail")):
            return "email"
        if any(w in combined for w in ("container", "docker", "service", "restarted", "logs")):
            return "docker"
        if any(w in combined for w in ("file", "nextcloud", "document", "folder", "webdav")):
            return "file"
        if any(w in combined for w in ("calendar", "event", "schedule", "appointment")):
            return "calendar"
    return None


def _quick_classify(user_input: str, context_window=None) -> dict | None:
    """
    Deterministic pre-classifier for unambiguous domain keywords.
    Runs before the LLM. Uses context_window (last 2-3 turns) to resolve
    pronoun references like "they/them/those" against the active domain.
    Returns a delegation dict or None (fall through to CEO LLM).
    """
    u = user_input.lower()
    prior_domain = _infer_prior_domain(context_window)

    # ── URL fetch — explicit https?:// present in input ────────────────────
    import re as _re
    _url_match = _re.search(r'https?://[^\s"\']+', user_input)
    if _url_match:
        # If the URL appears alongside a skill install verb, route to skill_install
        # so lifecycle.search() can fetch the SKILL.md directly from the URL.
        import re as _re_url_sk
        _has_skill_verb = _re_url_sk.search(r'\b(install|load|add)\b', u) and "skill" in u
        if _has_skill_verb:
            return {
                "delegate_to": "devops_agent", "intent": "skill_install",
                "target": user_input, "tier": "MID",
                "reasoning_summary": "Skill install from direct URL — deterministic pre-classifier",
            }
        return {
            "delegate_to": "research_agent", "intent": "fetch_url",
            "target": _url_match.group(0).rstrip(".,)"), "tier": "LOW",
            "reasoning_summary": "Explicit URL detected — deterministic fetch_url",
        }

    # ── Calendar events — must precede scheduler and file-write checks ────
    # These phrases are unambiguous calendar operations and must never be stolen
    # by the scheduler ("create a task") or WebDAV ("create a note/document") paths.
    _cal_create_kw = (
        "add to my calendar", "add to the calendar", "add to calendar",
        "put on my calendar", "put on the calendar", "put in my calendar",
        "create an event", "create event", "add an event", "add event",
        "new event", "new calendar event",
        "schedule a meeting", "schedule meeting", "book a meeting", "book meeting",
        "schedule an appointment", "book an appointment", "book appointment",
        "create an appointment", "create appointment",
        "add a meeting", "add meeting", "add appointment",
        "create a calendar entry", "calendar entry",
        "make an appointment", "make appointment",
    )
    _cal_delete_kw = (
        "delete event", "delete the event", "delete calendar event",
        "remove event", "remove the event", "cancel the event",
        "remove from calendar", "delete from calendar",
        "delete from my calendar", "remove from my calendar",
    )
    _cal_update_kw = (
        "update event", "update the event", "reschedule event", "reschedule the event",
        "move the event", "change the event", "edit the event", "edit event",
        "change the time of", "move the meeting", "reschedule the meeting",
        "reschedule my meeting", "change my appointment",
    )
    if any(w in u for w in _cal_create_kw):
        return {
            "delegate_to": "business_agent", "intent": "create_event",
            "target": None, "tier": "MID",
            "reasoning_summary": "Calendar event creation — deterministic pre-classifier",
        }
    if any(w in u for w in _cal_delete_kw):
        return {
            "delegate_to": "business_agent", "intent": "delete_event",
            "target": None, "tier": "MID",
            "reasoning_summary": "Calendar event deletion — deterministic pre-classifier",
        }
    if any(w in u for w in _cal_update_kw):
        return {
            "delegate_to": "business_agent", "intent": "update_event",
            "target": None, "tier": "MID",
            "reasoning_summary": "Calendar event update — deterministic pre-classifier",
        }

    # ── Scheduler quick-check — before conversational guard ────────────────
    # Scheduling requests often contain no system-domain signals ("search daily",
    # "monitor weekly") so they must be tested before the conversational guard fires.
    _sched_early = (
        "schedule a task", "schedule this", "set up a task", "create a task",
        "set a schedule", "run every", "run daily", "run weekly", "run hourly",
        "every morning at", "every evening at", "every weekday at",
        "every monday", "every tuesday", "every wednesday", "every thursday",
        "every friday", "every saturday", "every sunday",
        "daily briefing", "weekly briefing", "morning briefing at", "briefing at",
        "give me a briefing every", "give me a weekly", "give me a daily",
        "remind me every", "check every", "monitor every", "monitor daily",
        "alert me every", "alert me when", "notify me every", "notify me when",
        "search daily for", "search weekly for", "search every", "search daily",
        "search weekly", "weekday briefing", "recurring task",
        "every day at", "every week at", "every hour",
        "list tasks", "show tasks", "scheduled tasks", "active tasks",
        "what's scheduled", "whats scheduled",
    )
    for _sk in _sched_early:
        if _sk in u:
            # Further discriminate: list vs schedule vs pause/cancel
            if any(w in u for w in ("list tasks", "show tasks", "scheduled tasks",
                                    "active tasks", "what's scheduled", "whats scheduled")):
                return {
                    "delegate_to": "devops_agent", "intent": "list_tasks",
                    "target": "active", "tier": "LOW",
                    "reasoning_summary": "List scheduled tasks — deterministic pre-classifier",
                }
            if any(w in u for w in ("cancel task", "stop task", "delete task", "remove task")):
                return {
                    "delegate_to": "devops_agent", "intent": "cancel_task",
                    "target": None, "tier": "MID",
                    "reasoning_summary": "Task cancellation — deterministic pre-classifier",
                }
            if any(w in u for w in ("pause task", "suspend task")):
                return {
                    "delegate_to": "devops_agent", "intent": "pause_task",
                    "target": None, "tier": "MID",
                    "reasoning_summary": "Task pause — deterministic pre-classifier",
                }
            return {
                "delegate_to": "devops_agent", "intent": "schedule_task",
                "target": None, "tier": "MID",
                "reasoning_summary": "Scheduling request — deterministic pre-classifier",
            }

    # ── Conversational/personal guard ──────────────────────────────────────
    # If no system-domain signal is present, route to query immediately.
    # Prevents personal/lifestyle statements (buying shirts, weekend plans, etc.)
    # from being misrouted to file/email/docker operations by the small LLM.
    _system_signals = (
        "email", "inbox", "mail", "nextcloud", "file", "folder", "document",
        "calendar", "event", "schedule", "appointment",
        "docker", "container", "restart", "logs", "service",
        "search the web", "look online", "search online", "find on the internet",
        "web search", "look up online", "internet",
        "remember", "store", "memoris", "memoriz", "note that", "don't forget",
        "shopping list", "grocery list", "to-do list", "todo list", "wish list",
        "to my list", "on my list", "to the list", "on the list",
        "github", "repo", "commit", "push to", "sovereign repo",
        "skill", "clawhub", "openclaw",
    )
    prior_has_system = prior_domain is not None

    # ── Web search — direct to browser, bypass specialist + evaluate passes ──
    # Explicit web search intent → domain:browser short-circuit (no LLM passes needed).
    # Time-sensitive variants fall through to CEO so it can pick up live-data context.
    _web_search_kw = (
        "search the web", "search the internet", "search online", "look online",
        "find on the internet", "find online", "look it up online", "look up online",
        "web search", "google", "search for", "search up",
        "look it up", "look up", "find out about", "find information",
        "what do you know about", "find me information", "look for information",
        "search for information", "find articles", "find news about",
    )
    _time_signals = (
        "right now", "at the moment", "live", "real-time", "real time",
        "current", "currently", "today", "latest", "what is it at", "what's it at",
        "how much is it", "what's the", "what is the", "how is the",
    )
    # Explicit web-search phrases always route direct-to-browser, regardless of time signals.
    # Time signal guard only applies below for implicit live-data queries (no explicit search verb).
    _explicit_web_kw = (
        "search the web", "search the internet", "search online", "look online",
        "find on the internet", "find online", "look it up online", "look up online",
        "web search for", "google for", "google this", "search for information about",
        "find me information about", "find information about", "look for information about",
        "find articles about", "find news about", "find out about",
    )
    if any(sig in u for sig in _explicit_web_kw):
        query = user_input
        for phrase in sorted(_explicit_web_kw, key=len, reverse=True):
            if phrase in u:
                idx = u.index(phrase) + len(phrase)
                remainder = user_input[idx:].strip().lstrip("for ").strip()
                if remainder:
                    query = remainder
                break
        return {
            "delegate_to": "research_agent", "intent": "web_search",
            "target": query, "tier": "LOW",
            "reasoning_summary": "Explicit web search phrase — deterministic direct-to-browser",
        }

    if not prior_has_system and any(sig in u for sig in _time_signals):
        return None  # fall through to CEO LLM — likely web_search

    if not prior_has_system and not any(sig in u for sig in _system_signals):
        return {
            "delegate_to": "research_agent", "intent": "query",
            "target": None, "tier": "LOW",
            "reasoning_summary": "No system domain signals — conversational query",
        }

    # Pronoun-only inputs — resolve against prior domain before pattern matching
    _pronouns = ("they", "them", "those", "these", "it", "that", "all of them", "all of those")
    _is_pronoun_ref = any(w in u for w in _pronouns) and len(u.split()) <= 12

    # Email — explicit keyword or pronoun ref when prior domain was email
    _mail_kw = ("email", "emails", "inbox", "my mail", "any mail", "any emails", "messages", "unread")
    _send_kw = ("send an", "send a", "reply to", "forward this", "write an email", "draft an email", "compose")
    _delete_kw = ("delete", "remove", "trash", "clear", "get rid")
    _move_kw = ("move", "archive", "file away")

    email_context = any(w in u for w in _mail_kw) or (_is_pronoun_ref and prior_domain == "email")
    if email_context and not any(w in u for w in _send_kw):
        account = "business" if "business" in u else "personal" if "personal" in u else None
        if any(w in u for w in _delete_kw):
            return {
                "delegate_to": "business_agent", "intent": "delete_email",
                "target": account, "tier": "HIGH",
                "reasoning_summary": "Email delete — deterministic pre-classifier (pronoun resolved from context)",
            }
        if any(w in u for w in _move_kw):
            return {
                "delegate_to": "business_agent", "intent": "move_email",
                "target": account, "tier": "MID",
                "reasoning_summary": "Email move/archive — deterministic pre-classifier (pronoun resolved from context)",
            }
        return {
            "delegate_to": "business_agent", "intent": "fetch_email",
            "target": account, "tier": "LOW",
            "reasoning_summary": "Email fetch — deterministic pre-classifier",
        }

    # Self-diagnostic — requests about Sovereign's own health, performance, or internal state
    _self_diag_kw = (
        "how are you running", "how are you performing", "how are you doing",
        "your health", "your status", "your performance", "your memory",
        "system health", "system status", "system performance",
        "self check", "self-check", "self diagnostic", "self-diagnostic",
        "check yourself", "check your", "how is your", "how is the system",
        "are you healthy", "are you ok", "resource usage", "resource utilization",
        "vram", "gpu usage", "gpu utilization", "memory usage", "cpu usage",
        "how much memory", "how much vram", "what is your status",
        "shortcomings", "your toolset", "your capabilities", "internal state",
        "monitor yourself", "monitor your", "diagnos",
    )
    if any(w in u for w in _self_diag_kw):
        return {
            "delegate_to": "devops_agent", "intent": "get_stats",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Self-diagnostic request — deterministic pre-classifier",
        }

    # Docker / containers — only for list/status queries, not action verbs
    _docker_kw = ("docker", "container", "containers", "running services",
                  "what services", "which services", "what's running", "whats running")
    _docker_action_kw = ("restart", "stop", "start", "kill", "remove", "delete", "update",
                         "rebuild", "redeploy", "logs", "log")
    docker_context = any(w in u for w in _docker_kw) or (_is_pronoun_ref and prior_domain == "docker")
    if docker_context and not any(w in u for w in _docker_action_kw):
        return {
            "delegate_to": "devops_agent", "intent": "list_containers",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Container status — deterministic pre-classifier",
        }

    # Nextcloud file operations — deterministic fast-path before LLM
    _write_kw = (
        "write file", "write a file", "create note", "create a note",
        "create document", "create a document", "save to nextcloud",
        "save a file", "write to nextcloud", "put a file", "new note",
        "new document", "add a note", "make a note", "write note",
    )
    _read_kw = (
        "read file", "read the file", "open file", "open the file",
        "show me the file", "get the file", "fetch the file",
        "show me the contents", "show the contents", "contents of the file",
        "what does the file", "what does this file", "what's in the file",
        "what is in the file", "read the document", "open the document",
        "show me the document", "get the document",
    )
    _list_kw = (
        "list files", "list my files", "list nextcloud", "show files",
        "what files", "show me my files",
        "show me the files", "what's on nextcloud", "whats on nextcloud",
        # "what's in" and "whats in" removed — too broad (matches "what's in my fridge" etc.)
    )

    # GitHub — deterministic fast-path for clear read/push intents
    _github_read_kw = (
        "github releases", "check releases", "pending updates", "security updates",
        "github status", "check github", "what's in the repo", "whats in the repo",
        "sovereign repo status",
    )
    if any(w in u for w in _github_read_kw):
        return {
            "delegate_to": "devops_agent", "intent": "github_read",
            "target": None, "tier": "LOW",
            "reasoning_summary": "GitHub read/monitor — deterministic pre-classifier",
        }

    # Skill lifecycle — deterministic fast-path
    _skill_audit_kw = (
        "list skills", "show skills", "what skills", "skill audit",
        "skills installed", "check skill integrity", "installed skills",
    )
    if any(w in u for w in _skill_audit_kw):
        return {
            "delegate_to": "devops_agent", "intent": "skill_audit",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Skill audit — deterministic pre-classifier",
        }
    # Composite skill_install: "install a skill for X", "get me a skill that does X"
    # Must be checked BEFORE skill_search so "install" takes the right path.
    _skill_install_kw = (
        "install a skill", "install skill", "install the skill",
        "get me a skill", "get a skill", "load a skill", "load skill",
        "add a skill", "add skill", "set up a skill",
    )
    import re as _re_sk_pre
    _skill_install_match = (
        any(w in u for w in _skill_install_kw)
        or bool(_re_sk_pre.search(r"\b(install|load|add)\b.{0,40}\bskill\b", u))
    )
    if _skill_install_match:
        import re as _re_sk_i
        # Try "install [the|a] <skill-name> skill" pattern first — extracts the skill name
        _name_match = _re_sk_i.search(
            r"\b(?:install|load|add|get me|get|set up)\b\s+(?:the\s+|a\s+)?(.+?)\s+skill\b",
            u, _re_sk_i.IGNORECASE
        )
        if _name_match:
            _q_i = _name_match.group(1).strip().strip(".,!?")
        else:
            _q_i = _re_sk_i.sub(
                r"(install|load|add|get me|get|set up)\s+(a\s+|the\s+)?skill(\s+for|\s+that|\s+to|\s+which)?",
                "", u, flags=_re_sk_i.IGNORECASE
            ).strip(" :,")
        return {
            "delegate_to": "devops_agent", "intent": "skill_install",
            "target": _q_i or user_input, "tier": "MID",
            "reasoning_summary": "Skill install (search→review→load sequence) — deterministic pre-classifier",
        }

    _skill_search_kw = (
        "clawhub", "skill registry", "search for skills", "find a skill",
        "find skills", "look for skills", "browse skills",
    )
    if any(w in u for w in _skill_search_kw):
        # Extract the search query: everything after the trigger keyword
        import re as _re_sk
        _q = _re_sk.sub(
            r"(clawhub|skill registry|search for skills?|find a? ?skills?|look for skills?|browse skills?)",
            "", u, flags=_re_sk.IGNORECASE
        ).strip(" :,")
        return {
            "delegate_to": "devops_agent", "intent": "skill_search",
            "target": _q or user_input, "tier": "LOW",
            "reasoning_summary": "Skill registry search — deterministic pre-classifier",
        }

    # Memory / reminder intents — explicit storage requests the LLM often misses.
    # Use regex for flexible patterns like "add milk to my shopping list".
    import re as _re_mem
    _memory_re = _re_mem.compile(
        r"\b(remember\s+(that|this)|don'?t\s+forget|note\s+(that|this)|"
        r"memoris[e]?|memoriz[e]?|store\s+(this|that)|keep\s+in\s+mind|"
        r"save\s+(to|on)\s+(my\s+)?(list|shopping|grocery|todo|to-do)|"
        r"(add|put|place)\s+.{1,30}\s+(to|on)\s+(my\s+)?(shopping|grocery|todo|to-do|wish|)\s*(list)|"
        r"(add|put)\s+(it|that|this)\s+(to|on)\s+(my\s+)?(list|shopping)|"
        r"to\s+my\s+(shopping|grocery|todo|to-do|wish)\s*list)",
        _re_mem.IGNORECASE,
    )
    if _memory_re.search(u):
        return {
            "delegate_to": "research_agent", "intent": "remember_fact",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Memory/reminder request — deterministic pre-classifier",
        }

    _mkdir_kw = (
        "create a folder", "create folder", "make a folder", "make folder",
        "new folder", "mkdir", "create a directory", "make a directory",
    )
    if any(w in u for w in _mkdir_kw):
        return {
            "delegate_to": "business_agent", "intent": "create_folder",
            "target": None, "tier": "MID",
            "reasoning_summary": "Folder creation — deterministic pre-classifier",
        }

    if any(w in u for w in _write_kw):
        return {
            "delegate_to": "business_agent", "intent": "write_file",
            "target": None, "tier": "MID",
            "reasoning_summary": "File write — deterministic pre-classifier",
        }
    if any(w in u for w in _read_kw):
        return {
            "delegate_to": "business_agent", "intent": "read_file",
            "target": None, "tier": "LOW",
            "reasoning_summary": "File read — deterministic pre-classifier",
        }
    if any(w in u for w in _list_kw):
        return {
            "delegate_to": "business_agent", "intent": "list_files",
            "target": "/", "tier": "LOW",
            "reasoning_summary": "File list — deterministic pre-classifier",
        }

    # Scheduler — remaining task management intents
    _list_tasks_kw = (
        "list tasks", "show tasks", "my tasks", "scheduled tasks", "list scheduled",
        "what tasks", "active tasks", "what's scheduled", "whats scheduled",
        "what do i have scheduled", "show scheduled",
    )
    if any(w in u for w in _list_tasks_kw):
        return {
            "delegate_to": "devops_agent", "intent": "list_tasks",
            "target": "active", "tier": "LOW",
            "reasoning_summary": "List scheduled tasks — deterministic pre-classifier",
        }

    _cancel_task_kw = ("cancel task", "cancel the task", "stop task", "delete task",
                       "remove task", "disable task")
    if any(w in u for w in _cancel_task_kw):
        return {
            "delegate_to": "devops_agent", "intent": "cancel_task",
            "target": None, "tier": "MID",
            "reasoning_summary": "Task cancellation — deterministic pre-classifier",
        }

    _pause_task_kw = ("pause task", "pause the task", "suspend task", "hold task")
    if any(w in u for w in _pause_task_kw):
        return {
            "delegate_to": "devops_agent", "intent": "pause_task",
            "target": None, "tier": "MID",
            "reasoning_summary": "Task pause — deterministic pre-classifier",
        }

    return None   # fall through to CEO LLM


# Safe fallback intent when CEO returns an unrecognised intent label.
# research_agent defaults to "query" (conversational) — NOT web_search.
# web_search is only triggered by explicit internet/web references in the routing rules.
_AGENT_DEFAULT_INTENT = {
    "docker_agent":   "list_containers",
    "research_agent": "query",
    "business_agent": "list_files",
}

AUDIT_PATH = "/home/sovereign/audit/memory-promotions.jsonl"


class ExecutionEngine:
    def __init__(self, gov, cog, qdrant=None, scanner=None, guardrail=None, ledger=None):
        self.gov = gov
        self.cog = cog
        self.qdrant = qdrant
        self.scanner = scanner
        self.guardrail = guardrail
        self.ledger = ledger
        self.broker = BrokerAdapter()
        self.webdav = WebDAVAdapter()
        self.caldav = CalDAVAdapter()
        self.github = GitHubAdapter()
        self.browser = BrowserAdapter()
        self.nanobot = NanobotAdapter(ledger=ledger)
        from execution.adapters.wallet import WalletControlAdapter
        self.wallet_control = WalletControlAdapter(ledger=ledger)
        # Skill lifecycle manager — instantiated lazily to avoid circular imports
        self._skill_lifecycle = None
        # Task scheduler — injected post-init from main.py to avoid circular imports
        self.task_scheduler = None

    # ── Direct structured query (/query endpoint) ────────────────────────
    async def handle_request(self, payload):
        action = payload.get("action", {})
        tier = payload.get("tier", "LOW")
        prompt = payload.get("prompt")
        confirmed = payload.get("confirmed", False)

        try:
            rules = self.gov.validate(action, tier)
        except ValueError as e:
            return {"error": str(e)}

        if not confirmed:
            if rules.get("requires_double_confirmation"):
                return {"requires_double_confirmation": True, "action": action, "tier": tier}
            if rules.get("requires_confirmation"):
                return {"requires_confirmation": True, "action": action, "tier": tier}

        security_confirmed = payload.get("security_confirmed", False)
        return await self._dispatch(action, prompt, payload=payload,
                                    security_confirmed=security_confirmed)

    async def _safe_translate(self, user_input: str, result: dict, tier: str = "LOW") -> str:
        """Always returns a Director-facing string. Never surfaces raw technical output."""
        # Deterministic error guard — never pass error results to the LLM translator.
        # The LLM may hallucinate success when given an error dict; this is an invariant:
        # Sovereign must never claim success without a confirmed 2xx HTTP response.
        _is_failure = (
            result.get("error")
            or result.get("status") in ("error", "unconfigured", "partial")
            or result.get("success") is False
        )
        if _is_failure:
            http_status = result.get("http_status")
            err_detail  = result.get("error") or result.get("message") or "unknown error"
            if http_status:
                return f"That operation failed — upstream returned HTTP {http_status}: {err_detail}"
            return f"That operation failed: {err_detail}"

        try:
            msg = await self.cog.ceo_translate(user_input, result, tier=tier)
            if msg and msg.strip():
                return msg.strip()
        except Exception:
            pass
        # Shape-aware hard fallback — never expose raw error internals or technical fields
        if result.get("error"):
            return "I wasn't able to complete that. Please try again or rephrase your request."
        if "items" in result:
            items = result["items"]
            path = result.get("path", "/")
            names = ", ".join(i.get("name", "") for i in items[:8] if i.get("name"))
            suffix = f" (and {len(items)-8} more)" if len(items) > 8 else ""
            return f"Here's what I found at {path}: {names}{suffix}."
        if "content" in result:
            return f"Here are the contents of {result.get('path', 'that file')}:\n\n{result['content'][:3000]}"
        if "messages" in result:
            msgs = result["messages"]
            return f"You have {len(msgs)} unread message(s)." if msgs else "No unread messages."
        if "containers" in result:
            return "\n".join(
                f"{c.get('name',['?'])[0].lstrip('/') if isinstance(c.get('name'),list) else c.get('name','?')}: {c.get('status','')}"
                for c in result["containers"]
            )
        if result.get("status") == "ok":
            return "Done."
        return "I wasn't able to complete that. Please try again."

    # ── Natural language chat (/chat endpoint) ───────────────────────────
    async def handle_chat(self, user_input: str, pending_delegation: dict = None,
                          confirmed: bool = False,
                          confidence_acknowledged: bool = False,
                          security_confirmed: bool = False,
                          context_window=None) -> dict:
        # PROSPECTIVE + HEALTH SESSION-START CHECK — fires on first message of a new session
        # (no prior context, no pending delegation = fresh session)
        morning_briefing: str | None = None
        if not context_window and not pending_delegation and self.qdrant:
            try:
                due_items = await self.cog.get_due_prospective()
                if due_items:
                    briefing_result = {
                        "status": "ok",
                        "domain": "prospective",
                        "due_items": due_items,
                    }
                    morning_briefing = await self.cog.ceo_translate(
                        "morning briefing — items due today", briefing_result
                    )
            except Exception:
                pass

            # Health brief — append to morning briefing
            try:
                from monitoring.metrics import collect_all
                from monitoring.scheduler import evaluate_metrics
                import asyncio as _asyncio
                metrics = await _asyncio.wait_for(collect_all(), timeout=15.0)
                anomalies = evaluate_metrics(metrics)
                health_result = {
                    "status": "ok",
                    "domain": "health",
                    "anomalies": anomalies,
                    "metrics_summary": {
                        "containers_running": sum(
                            1 for c in metrics.get("containers", [])
                            if c.get("status") == "running"
                        ),
                        "vram_used_mb": metrics.get("gpu", {}).get("vram_used_mb"),
                        "ram_percent": metrics.get("ram", {}).get("percent"),
                        "ollama_ok": metrics.get("ollama", {}).get("inference_ok"),
                    },
                }
                health_brief = await self.cog.ceo_translate(
                    "health status summary for morning briefing — plain English, first person, "
                    "no bullet lists; one sentence if healthy; only mention issues if present",
                    health_result,
                )
                if health_brief:
                    if morning_briefing:
                        morning_briefing = morning_briefing + "\n\n" + health_brief
                    else:
                        morning_briefing = health_brief
            except Exception:
                pass

        # SECURITY LAYER — pre-LLM inbound scan (skip if already security-confirmed)
        if not security_confirmed and self.scanner and not pending_delegation:
            scan = self.scanner.scan(user_input)
            if scan.flagged:
                try:
                    sec = await self.cog.security_evaluate(scan, user_input)
                except Exception:
                    sec = {"block": False, "risk_level": "medium",
                           "risk_categories": scan.categories,
                           "reasoning_summary": "Security LLM evaluation failed — proceeding with caution",
                           "required_mitigation": ""}
                if sec.get("block"):
                    if self.ledger:
                        try:
                            self.ledger.append("scanner_block", "inbound", {
                                "categories": scan.categories,
                                "risk_level": sec.get("risk_level", "high"),
                                "input_preview": user_input[:200],
                            })
                        except Exception:
                            pass
                    return {
                        "error": "Security block",
                        "risk_level": sec.get("risk_level", "high"),
                        "risk_categories": sec.get("risk_categories", scan.categories),
                        "reasoning": sec.get("reasoning_summary", ""),
                    }
                # Not blocked — wrap content as untrusted for downstream context
                mitigation = sec.get("required_mitigation", "")
                if mitigation:
                    user_input = f"[UNTRUSTED CONTENT — sanitized]\n{user_input}"

        # PASS 1 — CEO Classification (skip if re-submitting a confirmed delegation)
        if pending_delegation:
            delegation = pending_delegation
            # Restore confidence/gaps that were stashed in pending_delegation
            confidence = delegation.pop("_memory_confidence", 1.0)
            gaps = delegation.pop("_memory_gaps", [])
        else:
            # Deterministic pre-classifier — catches cases the small LLM routinely misroutes
            quick = _quick_classify(user_input, context_window=context_window)
            if quick:
                delegation = quick
                confidence, gaps = 1.0, []
            else:
                try:
                    delegation = await self.cog.ceo_classify(user_input, context_window=context_window)
                except Exception as e:
                    return {"error": f"CEO classification failed: {e}",
                            "confidence": 0.0, "gaps": []}
                confidence = delegation.pop("_memory_confidence", 1.0)
                gaps = delegation.pop("_memory_gaps", [])

        agent = delegation.get("delegate_to", "")

        # Normalise intent — CEO returns free-form labels; map to a known key
        intent = delegation.get("intent", "")
        if intent not in INTENT_ACTION_MAP:
            intent = _AGENT_DEFAULT_INTENT.get(agent, "query")

        # Tier is always derived deterministically — never trust the LLM
        # governance.json intent_tiers takes precedence over the local map
        tier = self.gov.get_intent_tier(intent) or INTENT_TIER_MAP.get(intent, "LOW")

        # Confidence gate — only for MID/HIGH tier actions where uncertain memory
        # could drive a wrong consequential decision. Conversational (LOW/ollama) flows through.
        if (not confidence_acknowledged
                and not pending_delegation
                and tier in ("MID", "HIGH")
                and 0.0 < confidence < CONFIDENCE_THRESHOLD):
            return {
                "requires_confidence_acknowledgement": True,
                "confidence": round(confidence, 3),
                "gaps": gaps,
                "pending_delegation": {
                    **delegation,
                    "intent": intent,
                    "_memory_confidence": confidence,
                    "_memory_gaps": gaps,
                },
                "message": (
                    f"Memory confidence {confidence:.2f} < {CONFIDENCE_THRESHOLD}. "
                    "Re-submit with confidence_acknowledged=true to proceed."
                ),
            }

        # CEO override logging when user acknowledges low confidence
        if confidence_acknowledged and pending_delegation:
            try:
                await self._log_ceo_confidence_override(user_input)
            except Exception:
                pass

        # Build the action dict with the normalised intent
        action = self._delegation_to_action({**delegation, "intent": intent})

        # Governance check
        try:
            rules = self.gov.validate(action, tier)
        except ValueError as e:
            dm = await self._safe_translate(user_input, {"error": str(e)}, tier=tier)
            return {"director_message": dm, "confidence": round(confidence, 3), "gaps": gaps}

        # Confirmation gate — skip only when user has explicitly confirmed (confirmed=True).
        # confidence_acknowledged re-submissions still require confirmation for MID/HIGH.
        if not confirmed:
            if rules.get("requires_double_confirmation"):
                return {
                    "requires_double_confirmation": True,
                    "pending_delegation": {**delegation, "intent": intent},
                    "summary": delegation.get("reasoning_summary", ""),
                    "confidence": round(confidence, 3),
                    "gaps": gaps,
                }
            if rules.get("requires_confirmation"):
                return {
                    "requires_confirmation": True,
                    "pending_delegation": {**delegation, "intent": intent},
                    "summary": delegation.get("reasoning_summary", ""),
                    "confidence": round(confidence, 3),
                    "gaps": gaps,
                }

        # Short-circuit for simple domains — no specialist/evaluation passes needed
        if action.get("domain") in ("ollama", "memory", "browser", "scheduler"):
            if action.get("domain") == "ollama":
                result = await self.cog.ask_conversational(user_input, context_window=context_window)
                exec_result = {"status": "ok", "response": result.get("response", "")}
                # Conversational responses are already Director-facing plain text.
                # Skip ceo_translate to avoid double-processing and corruption.
                director_msg = result.get("response", "")
                # Bug 4 fix: detect stated intentions in conversational responses and commit to
                # working memory immediately. When Sovereign says "I'll remember X" it must do so.
                import re as _re_intent
                _intention_re = _re_intent.compile(
                    r"\bI('?ll| will)\s+(remember|store|save|add|note|keep|write that|make a note)\b"
                    r"|\bnoted\b"
                    r"|\bI've\s+(saved|stored|noted|added|written that)\b",
                    _re_intent.IGNORECASE,
                )
                if self.qdrant and _intention_re.search(director_msg):
                    try:
                        from datetime import date as _date, timedelta as _td
                        # Detect monitoring/alerting commitments — these need a next_due
                        # so they surface in morning briefings. Default: tomorrow.
                        _monitor_re = _re_intent.compile(
                            r"\b(keep an eye|monitor|watch|alert me|check.*daily|daily.*check"
                            r"|notify me|remind me|track|follow up)\b",
                            _re_intent.IGNORECASE,
                        )
                        is_monitoring = bool(_monitor_re.search(user_input))
                        extra_meta = {
                            "source": "stated_intention",
                            "response_preview": director_msg[:120],
                        }
                        if is_monitoring:
                            extra_meta["next_due"] = str(_date.today() + _td(days=1))
                        await self.cog.save_lesson(
                            f"Stated intention: {user_input[:250]}",
                            user_input,
                            collection=PROSPECTIVE if is_monitoring else WORKING,
                            memory_type="prospective",
                            writer="sovereign-core",
                            extra_metadata=extra_meta,
                        )
                    except Exception:
                        pass
            else:
                exec_result = await self._dispatch(action, user_input,
                                                    payload={"confirmed": confirmed},
                                                    security_confirmed=security_confirmed)
                director_msg = await self._safe_translate(user_input, exec_result, tier=tier)
            result_dict = {
                "status": "ok",
                "intent": intent,
                "tier": tier,
                "agent": agent,
                "result": exec_result,
                "confidence": round(confidence, 3),
                "gaps": gaps,
                "director_message": director_msg,
            }
            if morning_briefing:
                result_dict["morning_briefing"] = morning_briefing
            return result_dict

        # Short-circuit for confirmed continuations — when Director has already reviewed
        # a proposed action (e.g. skill_install search+review) and confirmed=True with
        # a _pending_load stash, there is no new reasoning to do. Skip PASS 2+3 to
        # avoid 2× Ollama overhead (~80s) on what is a mechanical "proceed" decision.
        _confirmed_continuation = (
            confirmed
            and pending_delegation is not None
            and pending_delegation.get("_pending_load") is not None
        )
        if _confirmed_continuation:
            specialist_output = {}  # no new specialist input needed
        else:
            # PASS 2 — Specialist Reasoning (action-oriented domains only)
            try:
                specialist_output = await self.cog.specialist_reason(agent, delegation, user_input)
            except Exception as e:
                dm = await self._safe_translate(user_input, {"error": f"Specialist reasoning failed: {e}"}, tier=tier)
                return {"director_message": dm, "confidence": round(confidence, 3), "gaps": gaps}

            # PASS 3 — CEO Evaluation
            try:
                evaluation = await self.cog.ceo_evaluate(user_input, delegation, specialist_output)
            except Exception as e:
                dm = await self._safe_translate(user_input, {"error": f"CEO evaluation failed: {e}"}, tier=tier)
                return {"director_message": dm, "confidence": round(confidence, 3), "gaps": gaps}

            if not evaluation.get("approved", False):
                feedback = evaluation.get("feedback", "")
                dm = await self._safe_translate(user_input, {"error": "plan_rejected", "feedback": feedback}, tier=tier)
                return {"director_message": dm, "confidence": round(confidence, 3), "gaps": gaps}

        # PASS 4 — Execution (deterministic)
        try:
            execution_result = await self._dispatch(
                action, None,
                delegation={**delegation, "intent": intent},
                specialist=specialist_output,
                security_confirmed=security_confirmed,
            )
        except Exception as e:
            import httpx as _httpx
            if isinstance(e, _httpx.HTTPStatusError):
                status_code = e.response.status_code
                url = str(e.request.url)
                err_msg = (f"Nextcloud returned {status_code} — path not found or access denied."
                           if status_code == 404
                           else f"Upstream service error {status_code} at {url}")
                execution_result = {"status": "error", "error": err_msg, "http_status": status_code}
            else:
                execution_result = {"status": "error", "error": str(e)}

        # PASS 4.5 — Memory cross-reference for file operations
        # After a successful file list/read, search Qdrant for related knowledge.
        # IMPORTANT: Qdrant hits are stored memory — NOT live adapter results.
        # Every hit is tagged _result_source="qdrant_memory" so the translator
        # cannot confuse them with data that came directly from WebDAV/IMAP/CalDAV.
        if (intent in ("list_files", "read_file")
                and execution_result.get("status") == "ok"
                and self.qdrant):
            try:
                # Flag when the live adapter returned empty data — the translator must
                # say so explicitly before mentioning any memory context.
                _live_items = execution_result.get("items")
                _live_content = execution_result.get("content")
                _live_empty = (
                    (_live_items is not None and len(_live_items) == 0)
                    or (_live_content is not None and not str(_live_content).strip())
                )
                if _live_empty:
                    execution_result["_live_result_empty"] = True

                search_term = delegation.get("target") or user_input
                # Build a meaningful search string from path + user input
                path_parts = [p for p in str(search_term).replace("/", " ").split() if len(p) > 2]
                search_q = " ".join(path_parts[:6]) if path_parts else user_input
                mem_hits = await self.qdrant.search_all_weighted(
                    search_q, query_type="knowledge", top_k=3
                )
                if mem_hits:
                    execution_result["memory_context"] = [
                        {
                            "content": h.get("content", ""),
                            "score": round(h.get("score", 0), 3),
                            "_result_source": "qdrant_memory",
                        }
                        for h in mem_hits if h.get("score", 0) > 0.4
                    ]
            except Exception:
                pass

        # PASS 5 — Memory Decision (non-fatal if it fails)
        try:
            # Deterministic execution confirmation check — computed BEFORE the LLM call so the
            # LLM sees execution_confirmed in the result dict it reasons over.
            # The LLM must never be trusted to assert completion; we stamp it from real data.
            _exec_http = execution_result.get("http_status")
            if _exec_http is not None:
                # HTTP adapter (WebDAV, CalDAV, broker, browser) — require 2xx
                _execution_confirmed = (
                    execution_result.get("status") == "ok"
                    and isinstance(_exec_http, int)
                    and 200 <= _exec_http < 300
                    and not execution_result.get("error")
                )
            else:
                # Non-HTTP adapter (SMTP, IMAP, wallet signing) — trust status/success fields
                _execution_confirmed = (
                    (execution_result.get("status") == "ok"
                     or execution_result.get("success") is True)
                    and not execution_result.get("error")
                )

            # Stamp onto the result dict so the LLM sees authoritative confirmation state
            execution_result["execution_confirmed"] = _execution_confirmed

            # Intents that make real mutations — require confirmed success
            # before a prospective entry can be marked as anything other than "unconfirmed".
            _MUTATING_INTENTS = frozenset({
                "create_event", "create_task", "write_file", "send_email",
                "delete_file", "delete_email", "delete_task",
                "restart_container", "create_folder",
            })

            memory = await self.cog.ceo_memory_decision(user_input, execution_result)
            if memory.get("store") and memory.get("lesson"):
                coll_decision = memory.get("collection", "working_memory")
                mem_type = coll_decision if coll_decision in SOVEREIGN_COLLECTIONS else "lesson"

                # Build collection-specific metadata from richer schema fields
                extra_meta: dict = {}
                if mem_type == "episodic":
                    extra_meta["outcome"] = memory.get("outcome", "neutral")
                elif mem_type == "prospective":
                    if memory.get("next_due"):
                        extra_meta["next_due"] = memory["next_due"]
                    # For mutating intents, stamp execution_confirmed deterministically.
                    # If the HTTP call was not confirmed with a 2xx, override outcome to
                    # "unconfirmed" so the entry remains visible as a pending intention —
                    # regardless of what the LLM's memory decision contains.
                    if intent in _MUTATING_INTENTS:
                        extra_meta["execution_confirmed"] = _execution_confirmed
                        if not _execution_confirmed:
                            extra_meta["outcome"] = "unconfirmed"
                elif mem_type == "relational":
                    for f in ("concept_a", "concept_b", "shared", "diverges", "insight"):
                        if memory.get(f):
                            extra_meta[f] = memory[f]
                elif mem_type == "associative":
                    for f in ("item_a_id", "item_b_id", "link_type"):
                        if memory.get(f):
                            extra_meta[f] = memory[f]

                # Stage in working_memory with type tag; shutdown_promote moves to sovereign
                await self.cog.save_lesson(
                    memory["lesson"], user_input,
                    collection=WORKING,
                    memory_type=mem_type,
                    writer="sovereign-core",
                    extra_metadata=extra_meta,
                )
        except Exception:
            pass

        # CEO Agent translation — mandatory on all Director-bound messages
        # For requires_confirmation responses, pass a simplified context so the
        # translator generates a confirmation prompt rather than an error message.
        if execution_result.get("requires_confirmation") or execution_result.get("requires_double_confirmation"):
            _confirm_ctx = {
                "status": "awaiting_director_confirmation",
                "action": execution_result.get("action", intent),
                "summary": execution_result.get("summary", ""),
                "review_decision": (execution_result.get("review_result") or {}).get("decision", ""),
                "escalated": bool(execution_result.get("escalation_notice")),
            }
            director_msg = await self._safe_translate(user_input, _confirm_ctx, tier=tier)
        else:
            director_msg = await self._safe_translate(user_input, execution_result, tier=tier)
        result_dict = {
            "status": "ok",
            "intent": intent,
            "tier": tier,
            "agent": agent,
            "specialist_plan": specialist_output,
            "result": execution_result,
            "confidence": round(confidence, 3),
            "gaps": gaps,
            "director_message": director_msg,
        }
        # Promote gateway-critical fields to top level if present in execution_result.
        # Gateway reads data.get("requires_confirmation") / data.get("pending_delegation")
        # from the top-level response — they must not be buried under "result".
        for _gw_key in ("requires_confirmation", "requires_double_confirmation",
                        "pending_delegation", "summary", "escalation_notice"):
            if execution_result.get(_gw_key) is not None:
                result_dict[_gw_key] = execution_result[_gw_key]
        if morning_briefing:
            result_dict["morning_briefing"] = morning_briefing
        return result_dict

    # ── Delegation → action dict ─────────────────────────────────────────
    def _get_lifecycle(self):
        """Lazy init SkillLifecycleManager — avoids circular import at module load time."""
        if self._skill_lifecycle is None:
            from skills.lifecycle import SkillLifecycleManager
            # Resolve guardian from app state if available
            guardian = None
            try:
                import fastapi as _fa
                from starlette.testclient import _get_testclient  # noqa: F401
            except Exception:
                pass
            self._skill_lifecycle = SkillLifecycleManager(
                scanner=self.scanner,
                cog=self.cog,
                browser=self.browser,
                ledger=self.ledger,
                guardian=guardian,  # injected via set_guardian() after lifespan init
            )
        return self._skill_lifecycle

    def set_guardian(self, guardian) -> None:
        """Inject SoulGuardian into the lifecycle manager post-lifespan init."""
        if self._skill_lifecycle is not None:
            self._skill_lifecycle.guardian = guardian

    def set_task_scheduler(self, scheduler) -> None:
        """Inject TaskScheduler post-lifespan init."""
        self.task_scheduler = scheduler

    def set_credential_proxy(self, proxy) -> None:
        """Inject CredentialProxy post-lifespan init — passed to NanobotAdapter."""
        self.credential_proxy = proxy
        if self.nanobot:
            self.nanobot.set_credential_proxy(proxy)

    def _delegation_to_action(self, delegation: dict) -> dict:
        intent = delegation.get("intent", "")
        target = delegation.get("target") or ""
        base = INTENT_ACTION_MAP.get(intent, {"domain": "ollama", "operation": "query"})
        action = dict(base)
        # Inject target into the right field based on domain
        if action["domain"] == "docker" and target:
            action["container"] = target
        elif action["domain"] in ("webdav", "caldav"):
            if target:
                action["path"] = target
            # path defaults to "/" — will be overridden in _dispatch if specialist provides one
        elif action["domain"] == "mail" and target:
            action["account"] = target
        elif action["domain"] == "browser":
            if action.get("operation") == "fetch" and target:
                action["url"] = target
            elif action.get("operation") == "search" and target:
                action["query"] = target
        return action

    # ── Adapter dispatch ─────────────────────────────────────────────────
    async def _dispatch(self, action: dict, prompt: str = None,
                        delegation: dict = None, specialist: dict = None,
                        payload: dict = None, security_confirmed: bool = False) -> dict:
        """Wrapper that calls _dispatch_inner and stamps _result_source on every result."""
        result = await self._dispatch_inner(
            action, prompt, delegation, specialist, payload, security_confirmed
        )
        if isinstance(result, dict) and "_result_source" not in result:
            domain = action.get("domain", "")
            # Mail distinguishes read (IMAP) from send (SMTP) at the operation level
            if domain == "mail":
                source = "smtp_live" if action.get("operation") == "send" else "imap_live"
            else:
                source = _DOMAIN_SOURCE.get(domain, "unknown_adapter")
            result["_result_source"] = source
        return result

    async def _dispatch_inner(self, action: dict, prompt: str = None,
                              delegation: dict = None, specialist: dict = None,
                              payload: dict = None, security_confirmed: bool = False) -> dict:
        domain = action.get("domain")
        name = action.get("name", "")
        operation = action.get("operation", "")
        container = action.get("container")
        path = action.get("path", "/")
        payload = payload or {}
        confirmed = payload.get("confirmed", False)

        # For webdav ops: if path is still default "/", try extracting from specialist output.
        # Check specialist.path first (direct file path field), then fall back to target.
        # IMPORTANT: specialist.path is the primary field for file paths — target is for
        # container names and is only used as last resort for webdav.
        if domain == "webdav" and path == "/" and specialist:
            sp_path   = specialist.get("path", "")
            sp_target = specialist.get("target", "")
            source = sp_path or sp_target
            if source and isinstance(source, str):
                import re as _re
                # Strip trailing descriptions like " (on Nextcloud)" or " (file)"
                clean = _re.split(r"\s+[\(\[]", source)[0].strip()
                if clean and clean != "/":
                    path = clean if clean.startswith("/") else f"/{clean}"

        # GUARDRAIL — pre-execution check (skip if already security-confirmed)
        if not security_confirmed and self.guardrail:
            content_repr = repr(action) + " " + repr(prompt or "") + " " + repr(specialist or {})
            grail = self.guardrail.evaluate(domain, operation, content_repr[:2000], tool_name=name)
            if grail.decision == "block":
                return {"error": "Guardrail blocked", "rules": grail.matched_rules, "reason": grail.reason}
            if grail.decision == "confirm":
                return {
                    "requires_security_confirmation": True,
                    "rules": grail.matched_rules,
                    "reason": grail.reason,
                    "pending_action": action,
                }


        if domain == "docker":
            if name == "docker_ps":
                containers = await self.broker.list_containers()
                return {"status": "ok", "containers": [
                    {"name": c["Names"], "state": c["State"], "status": c["Status"]}
                    for c in containers
                ]}
            if name == "docker_logs":
                logs = await self.broker.get_logs(container, tail=int(action.get("tail", 50)))
                return {"status": "ok", "container": container, "logs": logs}
            if name == "docker_stats":
                if not container:
                    # Self-diagnostic — no specific container: return full system metrics
                    from monitoring.metrics import collect_all
                    metrics = await collect_all()
                    return {"status": "ok", "domain": "health", "metrics": metrics}
                stats = await self.broker.get_stats(container)
                return {"status": "ok", "container": container, "stats": stats}
            if name == "docker_restart":
                return await self.broker.restart(container)
            if name == "inspect_container":
                if not container:
                    return {"error": "inspect_container requires a container name as target"}
                return await self.broker.inspect_container(container)
            if name == "get_compose":
                return await self.broker.get_compose()
            if name == "host_file_read":
                # container field carries the path for this intent
                file_path = container or action.get("path", "")
                if not file_path:
                    return {"error": "read_host_file requires a file/directory path as target"}
                return await self.broker.read_host_file(file_path)
            if name == "get_hardware":
                return await self.broker.get_hardware()
            if name == "list_processes":
                return await self.broker.get_processes()
            return {"error": f"Unknown docker action: {name}"}

        if domain == "webdav":
            if name == "file_navigate":
                return await self.webdav.navigate(path)
            if name in ("file_list",):
                return await self.webdav.list(path)
            if name == "file_read":
                return await self.webdav.read(path)
            if name == "folder_create":
                return await self.webdav.mkdir(path)
            if name == "file_search":
                sp = specialist or {}
                query = sp.get("query") or action.get("query") or prompt or ""
                search_path = sp.get("path") or action.get("path", "/")
                if not query:
                    return {"error": "search_files requires a query term"}
                return await self.webdav.search(query, search_path)
            if name == "file_write":
                sp = specialist or {}
                content = (
                    sp.get("content")           # preferred — full content field
                    or sp.get("draft_content")  # legacy alias
                    or sp.get("content_preview")# fallback (may be truncated)
                    or action.get("content", "")
                )
                # Last resort: extract "content: ..." from user prompt
                if not content and prompt:
                    import re as _re
                    m = _re.search(r"(?:content|text|body)\s*[:=]\s*['\"]?(.+)", prompt, _re.IGNORECASE)
                    if m:
                        content = m.group(1).strip().strip("'\"")
                return await self.webdav.write(path, content)
            if name == "file_delete":
                return await self.webdav.delete(path)

        if domain == "caldav":
            if name == "calendar_read":
                return await self.caldav.list_calendars()
            if name == "calendar_create":
                d = delegation or {}
                sp = specialist or {}

                def _sp_dt(*keys) -> str:
                    """Try multiple field names in specialist output, return first non-empty."""
                    for k in keys:
                        v = sp.get(k) or action.get(k)
                        if v and isinstance(v, str) and v.strip():
                            return v.strip()
                    return ""

                cal_summary     = sp.get("summary")     or action.get("summary")     or d.get("intent", "")
                cal_calendar    = sp.get("calendar")    or action.get("calendar", "personal")
                cal_description = sp.get("description") or action.get("description", "")
                import uuid as _uuid
                cal_uid = sp.get("uid") or action.get("uid") or str(_uuid.uuid4())

                # ── Datetime extraction — accept any reasonable field name ──
                # Specialist may output: start, start_time, datetime, when, date_time,
                # event_start, scheduled_at, begin, date, at, or split date+time fields.
                _start_raw = _sp_dt(
                    "start", "start_time", "start_datetime", "datetime",
                    "when", "date_time", "event_start", "scheduled_at", "begin",
                )
                # If still empty, try combining separate date + time fields
                if not _start_raw:
                    _date_part = _sp_dt("date", "event_date", "start_date")
                    _time_part = _sp_dt("time", "event_time", "start_time", "at")
                    if _date_part and _time_part:
                        _start_raw = f"{_date_part} {_time_part}"
                    elif _date_part:
                        _start_raw = _date_part  # date-only; normaliser will give midnight

                # Last resort: if specialist output has any field whose value looks like
                # a date/time string, try to normalise it. Covers cases where the LLM
                # puts the datetime in a wrong-named field like "content" or "draft_content".
                if not _start_raw:
                    for _fb_key in ("content", "draft_content", "target", "description"):
                        _fb_val = sp.get(_fb_key, "")
                        if _fb_val and isinstance(_fb_val, str) and _normalise_dt(_fb_val):
                            _start_raw = _fb_val
                            break

                _end_raw = _sp_dt(
                    "end", "end_time", "end_datetime", "end_date",
                    "until", "finish", "event_end",
                )

                cal_start = _normalise_dt(_start_raw)
                cal_end   = _normalise_dt(_end_raw) if _end_raw else ""

                if not cal_start:
                    return {
                        "error": "create_event requires a start time — please specify date and time",
                        "specialist_fields_checked": list(sp.keys()),
                        "raw_start_value": _start_raw or None,
                    }
                if not cal_end:
                    cal_end = cal_start  # point event if end omitted

                return await self.caldav.create_event(
                    calendar=cal_calendar,
                    uid=cal_uid,
                    summary=cal_summary,
                    start=cal_start,
                    end=cal_end,
                    description=cal_description,
                )
            if name == "task_create":
                d = delegation or {}
                sp = specialist or {}
                import uuid as _uuid
                task_summary     = sp.get("summary")     or action.get("summary")     or d.get("intent", "")
                task_due         = sp.get("due")         or action.get("due",         "")
                task_start       = sp.get("start")       or action.get("start",       "")
                task_calendar    = sp.get("calendar")    or action.get("calendar",    "tasks")
                task_description = sp.get("description") or action.get("description", "")
                task_status      = sp.get("status")      or action.get("status",      "NEEDS-ACTION")
                task_uid         = sp.get("uid")         or action.get("uid")         or str(_uuid.uuid4())
                return await self.caldav.create_task(
                    calendar=task_calendar,
                    uid=task_uid,
                    summary=task_summary,
                    due=task_due,
                    start=task_start,
                    description=task_description,
                    status=task_status,
                )
            if name == "calendar_list_events":
                sp = specialist or {}
                evt_calendar  = sp.get("calendar") or action.get("calendar", "personal")
                evt_from_date = sp.get("from_date") or action.get("from_date", "")
                evt_to_date   = sp.get("to_date")   or action.get("to_date",   "")
                return await self.caldav.list_events(evt_calendar, evt_from_date, evt_to_date)
            if name == "task_complete":
                sp = specialist or {}
                comp_calendar = sp.get("calendar") or action.get("calendar", "tasks")
                comp_uid      = sp.get("uid")      or action.get("uid", "")
                if not comp_uid:
                    return {"error": "complete_task requires a UID — please specify which task"}
                return await self.caldav.complete_task(comp_calendar, comp_uid)
            if name == "calendar_update":
                sp = specialist or {}
                import uuid as _uuid
                upd_calendar    = sp.get("calendar")    or action.get("calendar", "personal")
                upd_uid         = sp.get("uid")         or action.get("uid", "")
                upd_summary     = sp.get("summary")     or action.get("summary", "")
                upd_description = sp.get("description") or action.get("description", "")
                _upd_start_raw = (sp.get("start") or sp.get("start_time") or
                                  sp.get("datetime") or sp.get("when") or action.get("start", ""))
                _upd_end_raw   = (sp.get("end")   or sp.get("end_time")   or action.get("end", ""))
                upd_start = _normalise_dt(_upd_start_raw) if _upd_start_raw else ""
                upd_end   = _normalise_dt(_upd_end_raw)   if _upd_end_raw   else ""
                if not upd_uid:
                    return {"error": "update_event requires a UID — list events first to obtain one"}
                return await self.caldav.update_event(
                    calendar=upd_calendar, uid=upd_uid,
                    summary=upd_summary, start=upd_start, end=upd_end,
                    description=upd_description,
                )
            if name in ("task_delete", "calendar_delete"):
                sp = specialist or {}
                del_calendar = sp.get("calendar") or action.get("calendar", "personal")
                del_uid      = sp.get("uid")      or action.get("uid", "")
                if not del_uid:
                    return {"error": f"{name} requires a UID — please specify which item to delete"}
                if name == "task_delete":
                    return await self.caldav.delete_task(del_calendar, del_uid)
                return await self.caldav.delete_event(del_calendar, del_uid)

        if domain == "mail":
            # All mail ops route through imap-smtp-email community skill → broker_exec (DSL path).
            # Python IMAPAdapter / SMTPAdapter are bypassed — community Node.js scripts are authoritative.
            sp = specialist or {}
            account = sp.get("account") or action.get("account", "personal")
            _suf = "" if account == "business" else "_personal"
            op = action.get("operation")
            _skill = "imap-smtp-email"

            if op == "read":
                if name == "mail_list_folders":
                    nb = await self.nanobot.run(_skill, "list_mailboxes", {})
                elif name == "mail_list_inbox":
                    count = int(sp.get("count") or action.get("count", 50))
                    nb = await self.nanobot.run(_skill, f"fetch_unread{_suf}", {"limit": count})
                else:
                    count = int(sp.get("count") or action.get("count", 10))
                    nb = await self.nanobot.run(_skill, f"fetch_unread{_suf}", {"limit": count})
                return nb.get("result", nb)

            if op == "fetch":
                uid = sp.get("uid") or action.get("uid", "")
                if not uid:
                    return {"error": "fetch_message requires a message UID"}
                nb = await self.nanobot.run(_skill, f"fetch_message{_suf}", {"uid": uid})
                return nb.get("result", nb)

            if op == "search":
                criteria = sp.get("criteria") or action.get("criteria") or {}
                for key in ("subject", "from_addr", "since", "body"):
                    if action.get(key) and key not in criteria:
                        criteria[key] = action[key]
                # Flatten criteria dict to a query string for the broker imap search
                query_parts = [str(v) for v in criteria.values() if v]
                query = " ".join(query_parts) if query_parts else (sp.get("query") or action.get("query", ""))
                nb = await self.nanobot.run(_skill, f"search{_suf}", {"query": query, "limit": 10})
                return nb.get("result", nb)

            if op == "flag":
                uid = sp.get("uid") or action.get("uid", "")
                if not uid:
                    return {"error": f"{name} requires a message UID"}
                if name == "mail_mark_read":
                    nb = await self.nanobot.run(_skill, "mark_read", {"uid": uid})
                else:
                    nb = await self.nanobot.run(_skill, "mark_unread", {"uid": uid})
                return nb.get("result", nb)

            if op == "move":
                return {"status": "error", "error": "Email move not available — no broker command. Use mark_read to flag instead."}

            if op == "delete":
                return {"status": "error", "error": "Email delete not available via community skill — no broker command yet."}

            if op == "send":
                s = specialist or {}
                draft = s.get("draft_content", "")
                nb = await self.nanobot.run(
                    _skill, f"send_email{_suf}",
                    {
                        "to": action.get("to", ""),
                        "subject": action.get("subject", delegation.get("intent", "") if delegation else ""),
                        "body": draft or action.get("body", ""),
                    }
                )
                return nb.get("result", nb)

        if domain == "ollama":
            if not prompt:
                return {"error": "prompt required"}
            result = await self.cog.ask_local(prompt)
            return {"status": "ok", "model": result.get("model"), "response": result.get("response")}

        if domain == "browser":
            if operation == "fetch":
                url = action.get("url") or prompt or ""
                if not url:
                    return {"error": "url required for browser fetch"}
                result = await self.browser.fetch(
                    url=url,
                    extract=action.get("extract", "text"),
                )
                if result.get("status") == "ok":
                    data = result.get("data", {})
                    return {
                        "url": data.get("url", ""),
                        "title": data.get("title", ""),
                        "content": data.get("content", ""),
                        "content_length": data.get("content_length", 0),
                        "fetch_sha256": data.get("fetch_sha256", ""),
                    }
                return result

            # action["query"] set by _delegation_to_action when quick_classify extracts the query;
            # prefer it over the full prompt (which may include "search the web for..." prefix).
            query = action.get("query") or prompt or ""
            if not query:
                return {"error": "query required for browser search"}
            result = await self.browser.search(
                query=query,
                locale=action.get("locale", payload.get("locale", "en-US")),
                return_format=action.get("return_format", "full"),
                test_mode=payload.get("test_mode", False),
            )
            # Synthesise a human-readable response so the gateway can render it
            if result.get("status") == "ok":
                import urllib.parse as _up
                enriched = result.get("data", {})
                # Log signed ACK — proves sovereign-core received and accepted this specific result
                if self.ledger:
                    try:
                        self.ledger.append("browser_search_ack", "execution", {
                            "query": query,
                            "result_sha256": enriched.get("result_sha256", ""),
                            "backend_used": enriched.get("backend_used", ""),
                            "result_count": len(enriched.get("results", [])),
                        })
                    except Exception:
                        pass
                synth = enriched.get("sovereign_synthesis", {})
                top_results = enriched.get("results", [])
                nav = enriched.get("ai_navigation", {})
                parts = []
                summary = synth.get("summary", "")
                if summary:
                    parts.append(summary)
                if top_results:
                    parts.append("\nTop sources:")
                    for r in top_results[:5]:
                        title = r.get("title", "")
                        url = r.get("url", "")
                        if title and url:
                            host = _up.urlparse(url).netloc
                            parts.append(f"• {title} — {host}")
                # follow_up_queries are AI navigation metadata — not Director-facing content
                result["response"] = "\n".join(parts)
            return result

        if domain == "security":
            op = action.get("operation")
            if op == "check_updates":
                new_releases = await self.github.check_releases()
                new_advisories = await self.github.fetch_advisory_feed()
                pending = await self.github.get_pending_updates()
                return {
                    "status": "ok",
                    "new_releases": new_releases,
                    "new_advisories": new_advisories,
                    "pending_count": len(pending),
                    "pending": pending[:10],
                }
            if op == "read":
                pending = await self.github.get_pending_updates()
                return {"status": "ok", "pending": pending}
            return {"error": f"Unknown security operation: {op}"}

        if domain == "github":
            op = action.get("operation", "")
            sp = specialist or {}

            # ── READ — releases, pending updates, repo status ────────────
            if op == "read":
                try:
                    releases = await self.github.check_releases()
                    pending  = await self.github.get_pending_updates()
                    return {
                        "status":        "ok",
                        "new_releases":  releases,
                        "pending_count": len(pending),
                        "pending":       pending[:5],
                    }
                except Exception as e:
                    return {"error": f"GitHub read failed: {e}"}

            # ── PUSH — doc / soul / security files ───────────────────────
            if op in ("push_doc", "push_soul", "push_sec"):
                repo_path = (sp.get("repo_path") or action.get("repo_path", "")).strip()
                source_path = (sp.get("source_path") or action.get("source_path", "")).strip()
                commit_message = (
                    sp.get("commit_message")
                    or action.get("commit_message")
                    or f"Sovereign update: {repo_path}"
                )
                if not repo_path:
                    return {"error": "repo_path required — the repo-relative destination path"}

                # Hard block: PAT modification / repo management never allowed via any op
                _blocked_paths = ("secrets/", ".github/", "settings", "CODEOWNERS")
                if any(b in repo_path for b in _blocked_paths):
                    return {"error": f"Blocked: {repo_path} is outside Sovereign's permitted write scope"}

                # Resolve content: read from RAID source_path if provided
                if source_path:
                    try:
                        with open(source_path) as f:
                            content = f.read()
                    except Exception as e:
                        return {"error": f"Cannot read source file {source_path}: {e}"}
                elif sp.get("content"):
                    content = sp["content"]
                else:
                    return {"error": "source_path or content required for GitHub push"}

                # Sanitize governance.json before pushing — strip internal paths
                if "governance.json" in repo_path:
                    import re as _gre
                    content = _gre.sub(r'/home/sovereign/[^\"\s,]+', '<RAID_PATH>', content)
                    content = _gre.sub(r'/docker/sovereign/[^\"\s,]+', '<NVME_PATH>', content)
                    try:
                        import json as _json
                        data = _json.loads(content)
                        if "security" in data:
                            data["security"]["advisory_feed_url"] = "<ADVISORY_FEED_URL>"
                            data["security"]["releases_url"] = "<RELEASES_URL>"
                        content = _json.dumps(data, indent=2)
                    except Exception:
                        pass  # leave as-is if JSON parse fails

                result = await self.github.push_file(
                    path=repo_path,
                    content=content,
                    message=commit_message,
                )
                if result.get("status") == "ok":
                    # Audit the push
                    if self.ledger:
                        try:
                            self.ledger.append("github_push", "execution", {
                                "repo_path": repo_path,
                                "commit_sha": result.get("sha", "")[:16],
                                "operation": op,
                            })
                        except Exception:
                            pass
                return result

            return {"error": f"Unknown github operation: {op}"}

        if domain == "skills":
            lifecycle = self._get_lifecycle()
            op = action.get("operation", "")
            sp = specialist or {}

            if op == "search":
                query = (
                    sp.get("search_query")
                    or action.get("query")
                    or prompt
                    or ""
                )
                certified_only = action.get("certified_only", True)
                if isinstance(certified_only, str):
                    certified_only = certified_only.lower() != "false"
                return await lifecycle.search(
                    query=query,
                    certified_only=certified_only,
                    limit=int(action.get("limit", 10)),
                )

            if op == "review":
                slug = sp.get("slug") or action.get("slug") or ""
                skill_md = sp.get("skill_md") or action.get("skill_md") or ""
                certified = bool(action.get("certified", True))
                if not skill_md:
                    return {"error": "skill_md content required for review"}
                return await lifecycle.review(
                    slug=slug,
                    skill_md_content=skill_md,
                    certified=certified,
                )

            if op == "load":
                skill_name = sp.get("name") or action.get("name") or ""
                skill_md = sp.get("skill_md") or action.get("skill_md") or ""
                review_result = action.get("review_result") or {}
                if not skill_name or not skill_md:
                    return {"error": "name and skill_md required for skill load"}
                if not review_result:
                    return {"error": "review_result required — skill must be reviewed before loading"}
                proposed_by = (delegation or {}).get("delegate_to", "devops_agent")
                reason = (
                    action.get("reason")
                    or sp.get("reasoning_summary")
                    or (delegation or {}).get("reasoning_summary")
                    or "Director requested skill installation."
                )
                return await lifecycle.load(
                    name=skill_name,
                    skill_md_content=skill_md,
                    review_result=review_result,
                    confirmed=confirmed,
                    specialist_overrides=action.get("specialist_overrides"),
                    tier_override=action.get("tier_override"),
                    clawhub_slug=action.get("clawhub_slug"),
                    clawhub_certified=bool(action.get("clawhub_certified", False)),
                    proposed_by=proposed_by,
                    reason=reason,
                )

            if op == "audit":
                return lifecycle.audit()

            if op == "install":
                # Composite 3-step flow: search → review → load
                # Short-circuit: if Director already confirmed and pending_load is stashed in
                # delegation, skip search+review and go straight to load.
                _dl_pending = (delegation or {}).get("_pending_load")
                if confirmed and _dl_pending:
                    return await lifecycle.load(
                        name=_dl_pending.get("name", ""),
                        skill_md_content=_dl_pending.get("skill_md", ""),
                        review_result=_dl_pending.get("review_result", {}),
                        confirmed=True,
                        clawhub_slug=_dl_pending.get("clawhub_slug"),
                        clawhub_certified=bool(_dl_pending.get("clawhub_certified", False)),
                        proposed_by=(delegation or {}).get("delegate_to", "devops_agent"),
                        reason="Director confirmed skill installation after review.",
                    )

                # Step 1: search for candidates
                # If the original request contained a direct URL (in delegation.target),
                # pass it straight to lifecycle.search() to trigger the direct URL shortcut.
                # Specialist may strip the URL and return a clean search query — we must
                # preserve the URL so lifecycle.py can fetch SKILL.md without SearXNG.
                import re as _re_install_url
                _delegation_target = (delegation or {}).get("target", "")
                _direct_url_match = _re_install_url.search(r'https?://\S+', _delegation_target)
                if _direct_url_match:
                    query = _direct_url_match.group(0).rstrip(".,)")
                else:
                    query = (
                        sp.get("search_query")
                        or action.get("query")
                        or prompt
                        or ""
                    )
                search_result = await lifecycle.search(query=query, certified_only=True, limit=5)

                # Step 2: review the top candidate that has skill_md content
                top = next(
                    (c for c in search_result.get("candidates", []) if c.get("skill_md")),
                    None,
                )
                if not top:
                    return {
                        "status": "no_candidates",
                        "message": "No skills found matching your query. Try a more specific search.",
                        "search_result": search_result,
                        "next_step": "Refine your search query and try again.",
                    }

                review_result = await lifecycle.review(
                    slug=top.get("slug", "unknown"),
                    skill_md_content=top["skill_md"],
                    certified=bool(top.get("certified", False)),
                )

                if review_result.get("decision") == "block":
                    return {
                        "status": "blocked",
                        "message": "The security review returned a BLOCK verdict. This skill cannot be installed.",
                        "review_result": review_result,
                        "slug": top.get("slug"),
                    }

                # Present findings to Director — require explicit confirmation before load
                _pending_load = {
                    "name": top.get("slug"),
                    "skill_md": top["skill_md"],
                    "review_result": review_result,
                    "clawhub_slug": top.get("slug"),
                    "clawhub_certified": bool(top.get("certified", False)),
                }
                _decision = review_result.get("decision", "review")
                _escalated = review_result.get("escalate_to_director", False)
                _summary = (
                    f"Install skill '{top.get('slug', 'unknown')}' "
                    f"from {top.get('github_url', 'unknown source')}.\n"
                    f"Security review: {_decision.upper()}"
                    + (" — ESCALATED (see reasons below)" if _escalated else "") + ".\n"
                    + (
                        f"Escalation reasons: {'; '.join(review_result.get('escalation_reasons', []))}.\n"
                        if _escalated else ""
                    )
                    + "Reply yes to install or no to cancel."
                )
                resp = {
                    "status": "awaiting_director_confirmation",
                    "requires_confirmation": True,
                    "tier": "MID",
                    "action": "skill_install",
                    "summary": _summary,
                    "candidate": {
                        "slug": top.get("slug"),
                        "summary": top.get("summary"),
                        "github_url": top.get("github_url"),
                    },
                    "review_result": review_result,
                    # pending_delegation: gateway stores and re-sends on confirm; must contain
                    # intent + _pending_load so engine resumes at the confirmed load step.
                    "pending_delegation": {
                        "delegate_to": "devops_agent",
                        "intent": "skill_install",
                        "_pending_load": _pending_load,
                    },
                }
                if _escalated:
                    resp["escalation_notice"] = (
                        "This skill was flagged for Director review. "
                        f"Reasons: {'; '.join(review_result.get('escalation_reasons', []))}. "
                        "Director must acknowledge this escalation before load."
                    )

                if confirmed:
                    # Director confirmed — resume load using _pending_load from delegation
                    # (_delegation_to_action strips custom fields so read from delegation directly)
                    pending = (delegation or {}).get("_pending_load") or _pending_load
                    return await lifecycle.load(
                        name=pending.get("name", top.get("slug", "")),
                        skill_md_content=pending.get("skill_md", top["skill_md"]),
                        review_result=pending.get("review_result", review_result),
                        confirmed=True,
                        clawhub_slug=pending.get("clawhub_slug"),
                        clawhub_certified=bool(pending.get("clawhub_certified", False)),
                        proposed_by=(delegation or {}).get("delegate_to", "devops_agent"),
                        reason="Director confirmed skill installation after review.",
                    )

                return resp

            return {"error": f"Unknown skills operation: {op}"}

        if domain == "memory":
            op = action.get("operation")

            if op == "search":
                query = prompt or action.get("query", "")
                if action.get("collection"):
                    results = await self.qdrant.search(
                        query, collection=action["collection"]
                    )
                else:
                    results = await self.qdrant.search_all_sovereign(query)
                return {"status": "ok", "results": results}

            if op == "promote":
                point_id = action.get("point_id")
                if not point_id:
                    return {"status": "error", "message": "point_id required for promote"}
                try:
                    promoted = await self.qdrant.promote(
                        point_id,
                        target_collection=action.get("target_collection"),
                        writer=action.get("writer", "sovereign-core"),
                        human_confirmed=confirmed,
                    )
                except PermissionError as e:
                    return {"status": "error", "message": str(e)}
                return {"status": "ok", "promoted": promoted}

            if op in ("read", "write", "store"):
                import re
                fact = re.sub(
                    r"^(please\s+)?(remember|store|memorise|memorize|note|save|keep in mind)\s+(that\s+)?",
                    "", prompt or "", flags=re.IGNORECASE,
                ).strip(" .,")
                if not fact:
                    return {"status": "error", "message": "Nothing to remember — fact was empty."}
                coll = action.get("collection", WORKING)
                mem_type = action.get("type", "lesson")
                writer = action.get("writer", "sovereign-core")
                try:
                    point_id = await self.cog.save_lesson(
                        fact, prompt or "",
                        collection=coll,
                        memory_type=mem_type,
                        writer=writer,
                        human_confirmed=confirmed,
                    )
                except PermissionError as e:
                    return {"status": "error", "message": str(e)}
                return {"status": "ok", "message": f"Stored: {fact}", "point_id": point_id}

            return {"error": f"Unknown memory operation: {op}"}

        if domain == "wallet":
            if name == "wallet_read_config":
                import json as _j
                _wc = "/home/sovereign/governance/wallet-config.json"
                try:
                    return {"status": "ok", "config": _j.loads(open(_wc).read())}
                except FileNotFoundError:
                    return {"status": "error", "error": "wallet-config.json not found"}
                except Exception as _e:
                    return {"status": "error", "error": str(_e)}
            if name == "wallet_get_address":
                return await self.wallet_control.get_address()
            if name == "wallet_sign_message":
                message = action.get("message", delegation.get("message", "") if delegation else "")
                return await self.wallet_control.sign_message(message)
            if name == "wallet_propose_safe_tx":
                sp = specialist or {}
                return await self.wallet_control.propose_safe_transaction(
                    to      = action.get("to",      sp.get("to",      "")),
                    value   = int(action.get("value", sp.get("value",  0))),
                    data    = action.get("data",    sp.get("data",    "0x")),
                    purpose = action.get("purpose", sp.get("purpose", "")),
                )
            if name == "wallet_get_proposals":
                return await self.wallet_control.get_pending_proposals()
            if name == "wallet_get_btc_xpub":
                return await self.wallet_control.get_btc_xpub()
            return {"error": f"Unknown wallet action: {name}"}

        if domain == "scheduler":
            if not self.task_scheduler:
                return {"status": "error", "error": "Task scheduler not initialised"}

            if name == "list_tasks":
                status_f = (delegation or {}).get("target") or "active"
                return await self.task_scheduler.list_tasks(status_filter=status_f)

            if name in ("pause_task", "cancel_task"):
                task_id = (specialist or {}).get("task_id") or action.get("task_id", "")
                if not task_id:
                    # Try extracting from user input via delegation target
                    task_id = (delegation or {}).get("target", "")
                if not task_id:
                    return {"status": "error",
                            "error": f"{name} requires a task_id — use list_tasks to find it"}
                new_status = "paused" if name == "pause_task" else "cancelled"
                return await self.task_scheduler.update_task_status(task_id, new_status)

            if name == "schedule_task":
                # Parse the NL request into a structured TaskDefinition
                # prompt carries the original user text in short-circuit dispatch path
                parsed = await self.task_scheduler.parse_task_nl(prompt or "")
                if parsed.get("error"):
                    return {"status": "error", "error": parsed["error"]}
                if parsed.get("needs_clarification"):
                    return {
                        "status": "needs_clarification",
                        "question": parsed.get("clarification_question", ""),
                        "message": f"I need a bit more detail: {parsed.get('clarification_question', '')}",
                    }
                # Capability check — warn Director if adapters are missing
                steps = parsed.get("steps", [])
                cap = self.task_scheduler.check_capabilities(steps)
                if not cap["capable"]:
                    return {
                        "status": "blocked",
                        "missing_capabilities": cap["missing"],
                        "blockers": cap["blockers"],
                        "error": (
                            f"Cannot schedule this task — required adapters not configured: "
                            f"{', '.join(cap['missing'])}. "
                            f"Please configure: {'; '.join(cap['blockers'])}"
                        ),
                    }
                # Store the task (confirmed=True because user passed the MID gate)
                return await self.task_scheduler.store_task(parsed, human_confirmed=confirmed)

            return {"error": f"Unknown scheduler action: {name}"}

        if domain == "nanobot":
            # Delegated execution — MID tier enforced upstream (governance.json intent_tiers)
            # No secrets in params — specialist may not pass credentials to nanobot
            if name == "nanobot_health":
                node = (specialist or {}).get("node") or "nanobot-01"
                return await self.nanobot.health(node=node)

            if name == "nanobot_run":
                nb_skill   = (specialist or {}).get("skill")   or (delegation or {}).get("target", "")
                nb_action  = (specialist or {}).get("action")  or ""
                nb_params  = (specialist or {}).get("params")  or {}
                nb_context = (specialist or {}).get("context") or {}
                nb_node    = (specialist or {}).get("node")    or "nanobot-01"
                if not nb_skill:
                    return {"status": "error", "error": "nanobot_run requires a skill name"}
                # Safety: strip any key that looks like a credential from params
                _safe_params = {k: v for k, v in nb_params.items()
                                if not any(s in k.lower() for s in
                                           ("password", "secret", "token", "key", "apikey",
                                            "credential", "auth", "passwd"))}
                return await self.nanobot.run(
                    skill=nb_skill,
                    action=nb_action,
                    params=_safe_params,
                    context=nb_context,
                    node=nb_node,
                )

            return {"error": f"Unknown nanobot action: {name}"}

        return {"error": f"Unknown domain: {domain}"}

    # ── CEO confidence override logging ──────────────────────────────────
    async def _log_ceo_confidence_override(self, user_input: str):
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "ceo_confidence_override",
                "input": user_input[:200],
            }) + "\n")
        await self.qdrant.store(
            content=f"CEO acknowledged low-confidence memory gate for: {user_input[:150]}",
            metadata={
                "type": "episodic",
                "outcome": "neutral",
                "learned": "CEO acknowledged and overrode confidence gate",
            },
            collection=WORKING,
            writer="sovereign-core",
        )
