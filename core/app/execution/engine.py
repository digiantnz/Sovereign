import json
import os
from datetime import datetime, timezone

from adapters.broker import BrokerAdapter
from adapters.webdav import WebDAVAdapter
from adapters.caldav import CalDAVAdapter
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
    "browser":        "browser_live",
    "browser_config": "browser_live",
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
    "list_files":           {"domain": "webdav", "operation": "read",    "name": "file_list"},
    "read_file":            {"domain": "webdav", "operation": "read",    "name": "file_read"},
    "write_file":           {"domain": "webdav", "operation": "write",   "name": "file_write"},
    "delete_file":          {"domain": "webdav", "operation": "delete",  "name": "file_delete"},
    "create_folder":        {"domain": "webdav", "operation": "mkdir",   "name": "folder_create"},
    "list_files_recursive": {"domain": "webdav", "operation": "read",    "name": "file_list_recursive"},
    "read_files_recursive": {"domain": "webdav", "operation": "read",    "name": "file_read_recursive"},
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
    "read_feed":          {"domain": "feeds",   "operation": "read",   "name": "rss-digest"},
    # Memory intents
    "remember_fact":      {"domain": "memory",  "operation": "write"},
    # Memory Index Protocol — deterministic directory + exact-key retrieval (MIP v1.2)
    "memory_list_keys":    {"domain": "memory_index", "operation": "list_keys"},
    "memory_retrieve_key": {"domain": "memory_index", "operation": "retrieve_key"},
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
    # Self-diagnostic intents — all LOW, all read-only
    "read_audit_log":          {"domain": "docker", "operation": "read", "name": "host_file_read",
                                 "path": "/home/sovereign/audit/security-ledger.jsonl"},
    "memory_promotion_status": {"domain": "docker", "operation": "read", "name": "host_file_read",
                                 "path": "/home/sovereign/audit/memory-promotions.jsonl"},
    "soul_checksum_status":    {"domain": "docker", "operation": "read", "name": "docker_stats"},
    # System examination intents — devops_agent scope (all LOW, all read-only via broker)
    "inspect_container":  {"domain": "docker", "operation": "read", "name": "inspect_container"},
    "get_compose":        {"domain": "docker", "operation": "read", "name": "get_compose"},
    "read_host_file":     {"domain": "docker", "operation": "read", "name": "host_file_read"},
    "get_hardware":       {"domain": "docker", "operation": "read", "name": "get_hardware"},
    "list_processes":     {"domain": "docker", "operation": "read", "name": "list_processes"},
    "apt_check":          {"domain": "docker", "operation": "read", "name": "apt_check"},
    "systemctl_status":   {"domain": "docker", "operation": "read", "name": "systemctl_status"},
    "journalctl":         {"domain": "docker", "operation": "read", "name": "journalctl"},
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
    "schedule_task":      {"domain": "scheduler", "operation": "schedule", "name": "schedule_task"},
    "list_tasks":         {"domain": "scheduler", "operation": "list",     "name": "list_tasks"},
    "pause_task":         {"domain": "scheduler", "operation": "update",   "name": "pause_task"},
    "cancel_task":        {"domain": "scheduler", "operation": "update",   "name": "cancel_task"},
    "recall_last_briefing": {"domain": "scheduler", "operation": "recall", "name": "recall_last_briefing"},
    # Browser auth configuration — devops_agent scope (MID tier)
    "configure_browser_auth": {"domain": "browser_config", "operation": "configure_auth"},
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
    "apt_check": "LOW", "systemctl_status": "LOW", "journalctl": "LOW",
    "list_containers": "LOW", "get_logs": "LOW", "get_stats": "LOW",
    "list_files": "LOW", "navigate": "LOW", "read_file": "LOW", "search_files": "LOW",
    "list_files_recursive": "LOW", "read_files_recursive": "LOW",
    "fetch_email": "LOW", "search_email": "LOW", "fetch_message": "LOW",
    "mark_read": "LOW", "mark_unread": "LOW", "list_folders": "LOW", "list_inbox": "LOW",
    "read_feed": "LOW",
    "move_email": "MID",
    "list_calendars": "LOW", "list_events": "LOW",
    "delete_event": "MID", "update_event": "MID",
    "query": "LOW", "research": "LOW", "web_search": "LOW", "fetch_url": "LOW",
    "restart_container": "MID", "write_file": "MID", "send_email": "MID", "create_event": "MID",
    "create_task": "MID", "complete_task": "MID", "create_folder": "MID",
    "delete_file": "HIGH", "delete_email": "HIGH", "delete_task": "HIGH",
    "remember_fact": "LOW",
    "memory_list_keys": "LOW",
    "memory_retrieve_key": "LOW",
    # NOTE: skill_* tiers are governed by governance.json intent_tiers — not hardcoded here
    # Wallet tiers
    "wallet_read_config":     "LOW",
    "wallet_get_address":     "MID",
    "wallet_sign_message":    "MID",
    "wallet_propose_safe_tx": "HIGH",
    "wallet_get_proposals":   "MID",
    "wallet_get_btc_xpub":    "LOW",
    # Scheduler tiers — all LOW: Rex managing prospective memory is internal,
    # equivalent to writing in a notebook. No Director confirmation required.
    "schedule_task":        "LOW",
    "list_tasks":           "LOW",
    "pause_task":           "LOW",
    "cancel_task":          "LOW",
    "recall_last_briefing": "LOW",
    # Browser auth config — MID: writes to RAID governance config
    "configure_browser_auth": "MID",
    # Nanobot tiers — nanobot_run is MID (shell access), nanobot_health is LOW (read-only check)
    "nanobot_run":    "MID",
    "nanobot_health": "LOW",
    # Self-diagnostic read intents — LOW
    "read_audit_log":          "LOW",
    "memory_promotion_status": "LOW",
    "soul_checksum_status":    "LOW",
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
    import re as _re_pd
    for turn in reversed(turns[-2:]):
        combined = (turn.get("user", "") + " " + turn.get("assistant", "")).lower()
        if _re_pd.search(r'\b(email|inbox|subject|unread|sender|mail)\b', combined):
            return "email"
        if _re_pd.search(r'\b(container|docker|service|restarted|logs)\b', combined):
            return "docker"
        if _re_pd.search(r'\b(file|nextcloud|document|folder|webdav)\b', combined):
            return "file"
        if _re_pd.search(r'\b(calendar|event|schedule|appointment)\b', combined):
            return "calendar"
        if _re_pd.search(r'\b(skill|clawhub)\b|no skills found|skill search|candidates', combined):
            return "skills"
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
        # If the URL appears alongside a skill install/search verb, route to skill_install
        # so lifecycle.search() can fetch the SKILL.md directly from the URL.
        # Also catches "find/look for/search for skills...check here https://..." patterns.
        _raw_url = _url_match.group(0).rstrip(".,)")
        # clawhub.ai is always skill_search — it's a registry/browse page, never a direct SKILL.md
        # Check this BEFORE _has_skill_verb so "find skill ... clawhub.ai/..." doesn't misroute to install
        if "clawhub.ai" in _raw_url:
            _sk_q = user_input[:_url_match.start()].strip()
            return {
                "delegate_to": "devops_agent", "intent": "skill_search",
                "target": _sk_q or user_input, "tier": "LOW",
                "reasoning_summary": "clawhub.ai URL detected — route to skill_search (JS page, not fetchable)",
            }
        import re as _re_url_sk
        _has_skill_verb = (
            (_re_url_sk.search(r'\b(install|load|add)\b', u) and "skill" in u)
            or ("skill" in u and _re_url_sk.search(r'\b(find|look|search|browse|candidate)\b', u))
        )
        if _has_skill_verb:
            return {
                "delegate_to": "devops_agent", "intent": "skill_install",
                "target": user_input, "tier": "MID",
                "reasoning_summary": "Skill install from direct URL — deterministic pre-classifier",
            }
        return {
            "delegate_to": "research_agent", "intent": "fetch_url",
            "target": _raw_url, "tier": "LOW",
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

    # ── Sovereign RAID file reads — deterministic aliases ──────────────────
    # "read the as-built file" must never fall through to memory/query.
    # These are physical files on RAID mounted into sovereign-core — always read_host_file.
    _SOVEREIGN_FILE_ALIASES = {
        "as-built":            "/home/sovereign/docs/as-built.md",
        "as built":            "/home/sovereign/docs/as-built.md",
        "memory.md":           "/home/sovereign/memory/MEMORY.md",
        "memory file":         "/home/sovereign/memory/MEMORY.md",
        "sovereign-soul":      "/home/sovereign/personas/sovereign-soul.md",
        "sovereign soul":      "/home/sovereign/personas/sovereign-soul.md",
        "governance.json":     "/home/sovereign/governance/governance.json",
        "governance file":     "/home/sovereign/governance/governance.json",
        "audit log":           "/home/sovereign/audit/security-ledger.jsonl",
        "security ledger":     "/home/sovereign/audit/security-ledger.jsonl",
        "memory promotions":   "/home/sovereign/audit/memory-promotions.jsonl",
        "memory-promotions":   "/home/sovereign/audit/memory-promotions.jsonl",
        "promotion log":       "/home/sovereign/audit/memory-promotions.jsonl",
        "promotions log":      "/home/sovereign/audit/memory-promotions.jsonl",
        "skill dir":           "/home/sovereign/skills/",
        "skills directory":    "/home/sovereign/skills/",
    }
    _read_verbs = ("read", "show", "cat", "display", "open", "view", "list")
    _has_read_verb = any(v in u for v in _read_verbs)
    for _alias, _fpath in _SOVEREIGN_FILE_ALIASES.items():
        if _alias in u:
            return {
                "delegate_to": "devops_agent", "intent": "read_host_file",
                "target": _fpath, "tier": "LOW",
                "reasoning_summary": f"Known Sovereign RAID file alias '{_alias}' — deterministic read_host_file",
            }
    # Also catch explicit /home/sovereign/ or /docker/sovereign/ paths in the input
    import re as _re_path
    _path_match = _re_path.search(r'(/home/sovereign/|/docker/sovereign/)[\w./\-]+', user_input)
    if _path_match and _has_read_verb:
        return {
            "delegate_to": "devops_agent", "intent": "read_host_file",
            "target": _path_match.group(0), "tier": "LOW",
            "reasoning_summary": "Explicit RAID/NVMe path detected — deterministic read_host_file",
        }

    # ── Briefing recall — check episodic memory, never fabricate ──────────────
    _briefing_recall_kw = (
        "what was the briefing", "what was today's briefing", "morning briefing",
        "today's briefing", "yesterday's briefing", "last briefing",
        "did the briefing run", "briefing result", "show me the briefing",
        "what did the briefing say", "briefing summary",
    )
    if any(w in u for w in _briefing_recall_kw):
        return {
            "delegate_to": "devops_agent", "intent": "recall_last_briefing",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Briefing recall — query episodic memory, no fabrication",
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
        # One-shot reminder phrases — stored as PROSPECTIVE tasks in Qdrant
        "set a reminder", "set reminder", "remind me at", "remind me in",
        "remind me to", "reminder for", "add a reminder", "create a reminder",
        "schedule a reminder", "set an alarm", "remind me on",
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
                    "target": None, "tier": "LOW",
                    "reasoning_summary": "Task cancellation — deterministic pre-classifier",
                }
            if any(w in u for w in ("pause task", "suspend task")):
                return {
                    "delegate_to": "devops_agent", "intent": "pause_task",
                    "target": None, "tier": "LOW",
                    "reasoning_summary": "Task pause — deterministic pre-classifier",
                }
            return {
                "delegate_to": "devops_agent", "intent": "schedule_task",
                "target": None, "tier": "LOW",
                "reasoning_summary": "Scheduling request — no confirmation required (internal memory write)",
            }

    # ── System diagnostics — deterministic devops shortcuts ────────────────
    _os_update_kw = (
        "os update", "system update", "apt update", "apt upgrade",
        "check for updates", "package update", "kernel update",
        "upgradable packages", "available updates", "pending updates",
        "what needs updating", "what needs to be updated",
        "update check", "check updates",
    )
    if any(kw in u for kw in _os_update_kw):
        return {
            "delegate_to": "devops_agent", "intent": "apt_check",
            "target": None, "tier": "LOW",
            "reasoning_summary": "OS update check — deterministic pre-classifier",
        }
    # Guard: if the input is about skills/clawhub, don't misfire on "systemd" in a wish-list
    _is_skill_context = any(w in u for w in ("skill", "clawhub", "openclaw"))
    _systemctl_kw = ("systemctl", "service status", "is docker running", "is ssh running")
    _systemd_match = "systemd" in u and not _is_skill_context
    if any(kw in u for kw in _systemctl_kw) or _systemd_match:
        return {
            "delegate_to": "devops_agent", "intent": "systemctl_status",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Systemctl status — deterministic pre-classifier",
        }
    _journal_kw = ("journalctl", "journal log", "system journal", "systemd log")
    if any(kw in u for kw in _journal_kw):
        return {
            "delegate_to": "devops_agent", "intent": "journalctl",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Journal log — deterministic pre-classifier",
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
        "os update", "system update", "apt", "kernel", "package",
        "as-built", "as built", "memory.md", "governance",
        "browser auth", "auth profile", "auth for", "credentials for",
        "rss", "rss feed", "feed", "feeds", "news feed", "news stories",
        "memory index", "memory keys", "memory directory", "list my memories",
        "list all memories", "show my memories", "show memory",
        "retrieve memory", "fetch memory key", "get memory key",
        "what do you remember", "what's in memory", "what is in memory",
        "eth address", "wallet address", "safe address", "tailscale",
        "recall", "look up in memory", "retrieve from memory",
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
        "2025", "2026",  # explicit year reference → current-info query
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

    import re as _re_fp
    _has_file_path = bool(_re_fp.search(r"(?:^|\s)/[A-Za-z0-9_./-]+", user_input))
    if not prior_has_system and any(sig in u for sig in _time_signals) and not _has_file_path:
        # Time-sensitive query with no prior system context — route direct to browser/Grok.
        # DO NOT fall through to CEO LLM: the 8b model misroutes these when email/docker
        # history is present in context_window.
        # Exception: inputs containing a file path (e.g. /Notes/file.md) are never web searches.
        return {
            "delegate_to": "research_agent", "intent": "web_search",
            "target": user_input, "tier": "LOW",
            "reasoning_summary": "Time-sensitive query, no prior system context — direct to browser/Grok",
        }

    if not prior_has_system and not any(sig in u for sig in _system_signals):
        return {
            "delegate_to": "research_agent", "intent": "query",
            "target": None, "tier": "LOW",
            "reasoning_summary": "No system domain signals — conversational query",
        }

    # ── RSS feed — identified as system domain, must reach CEO LLM + PASS 3 ──
    # DO NOT short-circuit: "research" intent maps to ollama/query (short-circuit path),
    # which bypasses PASS 3 and the specialist that selects rss-digest via nanobot.
    # "rss" is already in _system_signals so the conversational guard won't fire.
    # The safety net below is also exempted for RSS so it falls through to CEO LLM.
    _rss_kw = (
        "rss feed", "rss feeds", "my feeds", "my rss", "news feed", "news feeds",
        "news stories from", "stories from the rss", "latest from my feeds",
        "what's in my feeds", "whats in my feeds", "from my feeds",
        "add a feed", "add feed", "subscribe to", "unsubscribe from",
        "list my feeds", "show my feeds",
    )
    _is_rss = "rss" in u or any(w in u for w in _rss_kw)
    if _is_rss:
        return {
            "delegate_to": "research_agent", "intent": "read_feed",
            "target": None, "tier": "LOW",
            "reasoning_summary": "RSS/feed query — direct nanobot rss-digest dispatch",
        }

    # Pronoun-only inputs — resolve against prior domain before pattern matching
    # Use word-boundary regex to avoid substring matches (e.g. "it" inside "with", "that" inside "that's")
    import re as _re_pr
    _is_pronoun_ref = bool(_re_pr.search(
        r'\b(they|them|those|these|it|that|all of them|all of those)\b', u
    )) and len(u.split()) <= 12

    # Email — explicit keyword or pronoun ref when prior domain was email
    _mail_kw = ("email", "emails", "inbox", "my mail", "any mail", "any emails", "messages", "unread", "mailbox", "mailboxes")
    _send_kw = ("send an", "send a", "reply to", "forward this", "write an email", "draft an email", "compose")
    _search_kw = ("search", "find", "look for", "filter", "from ", "containing", "subject:")
    _delete_kw = ("delete", "remove", "trash", "clear", "get rid")
    # Require "move" to be followed by email-related context, not standalone ("move on", "lets move")
    _move_kw = ("archive", "file away", "move to ", "move them", "move it", "move these", "move those", "move the email")
    _folder_kw = ("mailbox", "mailboxes", "mail folder", "mail folders", "imap folder", "what folders", "list folders")

    email_context = any(w in u for w in _mail_kw) or (_is_pronoun_ref and prior_domain == "email")
    if email_context:
        account = "business" if "business" in u else "personal" if "personal" in u else None
        if any(w in u for w in _folder_kw):
            return {
                "delegate_to": "business_agent", "intent": "list_folders",
                "target": account, "tier": "LOW",
                "reasoning_summary": "Email folder list — deterministic pre-classifier",
            }
        if any(w in u for w in _send_kw):
            return {
                "delegate_to": "business_agent", "intent": "send_email",
                "target": account, "tier": "MID",
                "reasoning_summary": "Email send — deterministic pre-classifier",
            }
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
        if any(w in u for w in _search_kw):
            return {
                "delegate_to": "business_agent", "intent": "search_email",
                "target": account, "tier": "LOW",
                "reasoning_summary": "Email search — deterministic pre-classifier",
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
        "write file", "write a file", "create file", "create a file",
        "create note", "create a note",
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
        # After a skill search, "install <name>" / "load <name>" routes to skill_install
        # even without the word "skill" — the prior_domain context makes the intent clear.
        or (prior_domain == "skills" and bool(_re_sk_pre.search(r"\b(install|load|add)\b\s+\S", u)))
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

    # "try again" / "try that again" — retry the previous skill search if context shows one
    _retry_kw = ("try again", "try that again", "search again", "look again", "retry")
    if any(w in u for w in _retry_kw) and prior_domain == "skills":
        return {
            "delegate_to": "devops_agent", "intent": "skill_search",
            "target": user_input, "tier": "LOW",
            "reasoning_summary": "Retry skill search — deterministic pre-classifier",
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
        # Strip action verb phrases left after removing trigger keywords
        _q = _re_sk.sub(r"^\s*(search\s+for|look\s+for|find|browse|get|show\s+me)\s+", "", _q, flags=_re_sk.IGNORECASE).strip()
        # Strip trailing source refs: "on clawhub", "from clawhub", "on the registry"
        _q = _re_sk.sub(r"\s+(on|from|at|in)\s+(clawhub|the\s+registry|github).*$", "", _q, flags=_re_sk.IGNORECASE).strip()
        # Strip leading/trailing filler words
        _q = _re_sk.sub(r"^\s*(on|for|about|a|an|the|me|some)\s+", "", _q).strip()
        return {
            "delegate_to": "devops_agent", "intent": "skill_search",
            "target": _q or user_input, "tier": "LOW",
            "reasoning_summary": "Skill registry search — deterministic pre-classifier",
        }

    # Browser auth profile management — deterministic fast-path
    _browser_auth_kw = (
        "configure browser auth", "add browser auth", "add auth for",
        "browser authentication", "configure auth profile", "add auth profile",
        "set up auth for", "add credentials for", "configure credentials for",
        "add bearer token for", "add basic auth for", "add api key for",
        "browser auth profile",
    )
    if any(w in u for w in _browser_auth_kw):
        return {
            "delegate_to": "devops_agent", "intent": "configure_browser_auth",
            "target": user_input, "tier": "MID",
            "reasoning_summary": "Browser auth profile configuration — deterministic pre-classifier",
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

    # Memory Index Protocol — list all keys (directory scan, no vector search)
    _mem_list_kw = (
        "list my memories", "list all memories", "show my memories",
        "show memory", "memory keys", "memory index", "memory directory",
        "what do you remember", "what's in memory", "what is in memory",
        "eth address", "wallet address", "safe address",
        "look up in memory", "retrieve from memory",
    )
    if any(w in u for w in _mem_list_kw):
        return {
            "delegate_to": "memory_agent", "intent": "memory_list_keys",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Memory directory request — MIP deterministic pre-classifier",
        }

    # Memory Index Protocol — retrieve by exact key
    _mem_retrieve_kw = (
        "retrieve memory", "fetch memory key", "get memory key", "retrieve memory key",
    )
    if any(w in u for w in _mem_retrieve_kw):
        # Extract key if present — look for {type}:{domain}:{slug} pattern
        import re as _re_mip
        _mip_key_re = _re_mip.search(r"\b([a-z]+:[a-z]+:[a-z][\w-]*)\b", u)
        _mip_key = _mip_key_re.group(1) if _mip_key_re else None
        return {
            "delegate_to": "memory_agent", "intent": "memory_retrieve_key",
            "target": _mip_key, "tier": "LOW",
            "reasoning_summary": "Memory key retrieval — MIP deterministic pre-classifier",
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

    _recursive_list_kw = (
        "list files recursively", "list all files", "list recursively",
        "recursive list", "recursive file list", "all files in",
        "show all files", "show files recursively",
    )
    _recursive_read_kw = (
        "read all files", "read files recursively", "read recursively",
        "read the folder", "read all notes", "read everything in",
        "ingest all files", "ingest folder", "read all files in",
    )
    if any(w in u for w in _recursive_read_kw) or (
        ("read" in u or "ingest" in u) and "recursive" in u
    ):
        return {
            "delegate_to": "business_agent", "intent": "read_files_recursive",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Recursive file read — deterministic pre-classifier",
        }
    if any(w in u for w in _recursive_list_kw) or (
        ("list" in u or "show" in u) and "recursive" in u
    ):
        return {
            "delegate_to": "business_agent", "intent": "list_files_recursive",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Recursive file list — deterministic pre-classifier",
        }

    _file_delete_kw = (
        "delete file", "delete the file", "delete a file",
        "remove file", "remove the file", "remove a file",
        "delete this file", "delete that file",
    )
    # Also catch "delete /path" or "remove /path" patterns (slash-prefixed path)
    import re as _re_fd
    _file_delete_path = bool(_re_fd.search(r'\b(delete|remove)\s+/[A-Za-z0-9_./-]+', u))
    if any(w in u for w in _file_delete_kw) or _file_delete_path or (
        prior_domain == "file" and any(w in u for w in ("delete", "remove", "trash"))
    ):
        return {
            "delegate_to": "business_agent", "intent": "delete_file",
            "target": None, "tier": "HIGH",
            "reasoning_summary": "File delete — deterministic pre-classifier",
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
            "target": None, "tier": "LOW",
            "reasoning_summary": "Task cancellation — deterministic pre-classifier",
        }

    _pause_task_kw = ("pause task", "pause the task", "suspend task", "hold task")
    if any(w in u for w in _pause_task_kw):
        return {
            "delegate_to": "devops_agent", "intent": "pause_task",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Task pause — deterministic pre-classifier",
        }

    # Safety net: prior context active but no domain keywords matched the current query.
    # Don't fall through to CEO LLM — the small 8b model re-uses prior context and misroutes.
    # Exceptions:
    #   - RSS queries: must reach CEO LLM + PASS 3 so specialist selects rss-digest via nanobot
    #   - Time signals: route to web_search (browser/Grok) not Ollama
    if prior_has_system:
        if _is_rss:
            pass  # fall through to CEO LLM — PASS 3 needed for rss-digest skill selection
        elif any(sig in u for sig in _time_signals):
            return {
                "delegate_to": "research_agent", "intent": "web_search",
                "target": user_input, "tier": "LOW",
                "reasoning_summary": "Time-sensitive query after system context — direct to browser/Grok, bypass CEO LLM",
            }
        else:
            return {
                "delegate_to": "research_agent", "intent": "query",
                "target": None, "tier": "LOW",
                "reasoning_summary": "Prior context active but no domain keywords — safe fallback to conversational query",
            }

    return None   # fall through to CEO LLM


# Safe fallback intent when CEO returns an unrecognised intent label.
# research_agent defaults to "query" (conversational) — NOT web_search.
# web_search is only triggered by explicit internet/web references in the routing rules.
_AGENT_DEFAULT_INTENT = {
    "docker_agent":   "list_containers",
    "devops_agent":   "get_stats",    # was missing — fallthrough caused query→fabrication
    "research_agent": "query",
    "business_agent": "list_files",
    "memory_agent":   "memory_list_keys",
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
        # FastAPI app.state — injected post-init so collect_all() can include soul_checksum
        self.app_state = None
        # MIP session tracking — True once memory_list_keys has been dispatched this boot.
        # Used to flag retrieve_key calls that skipped the mandatory list-first step.
        self._mip_listed_this_session = False

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
        # NOTE: Do NOT pass due_items through ceo_translate — the LLM fabricates briefing
        # content from vague prospective entries. Instead use the actual episodic run record.
        morning_briefing: str | None = None
        if not context_window and not pending_delegation and self.qdrant:
            try:
                if self.task_scheduler:
                    briefing_data = await self.task_scheduler.recall_last_briefing()
                    if briefing_data.get("status") == "ok":
                        morning_briefing = briefing_data.get("message", "")
                    elif briefing_data.get("status") == "error" and briefing_data.get("message"):
                        morning_briefing = briefing_data["message"]
                    # status == "not_found" → no morning_briefing; don't fabricate
                else:
                    # Task scheduler not yet available — fall back to due prospective items
                    # but only show count, never have LLM narrate them
                    due_items = await self.cog.get_due_prospective()
                    if due_items:
                        scheduled = [d for d in due_items if d.get("type") == "scheduled_task"]
                        if scheduled:
                            morning_briefing = (
                                f"{len(scheduled)} scheduled task(s) are due. "
                                "Use 'list tasks' to see them."
                            )
            except Exception:
                pass

            # Health brief — append to morning briefing
            try:
                from monitoring.metrics import collect_all
                from monitoring.scheduler import evaluate_metrics
                import asyncio as _asyncio
                metrics = await _asyncio.wait_for(
                    collect_all(getattr(self, 'app_state', None)), timeout=15.0)
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

        import asyncio as _asyncio
        import hashlib as _hashlib
        import time as _time_mod
        _t_total = _time_mod.monotonic()
        _PASS_TIMEOUT  = float(os.environ.get("PASS_TIMEOUT_SECONDS",  "30"))
        _TOTAL_TIMEOUT = float(os.environ.get("TOTAL_TIMEOUT_SECONDS", "120"))
        # Skill search/install involves GitHub API + multiple SKILL.md fetches + security review
        # — give it extra headroom beyond the default 120s
        _u_lower = user_input.lower()
        if any(w in _u_lower for w in ("skill", "clawhub", "openclaw")):
            _TOTAL_TIMEOUT = max(_TOTAL_TIMEOUT, 240.0)

        # Track whether the pre-LLM scanner already evaluated security
        _scanner_evaluated = False

        # ── Construct InternalMessage envelope ────────────────────────────────
        # The envelope carries routing metadata, context, and audit history
        # through all passes. Raw Director input is hashed here; never stored.
        from cognition.message import InternalMessage as _IMsgCls
        _session_id = str(id(context_window)) if context_window else ""
        _msg = _IMsgCls.create(
            director_input=user_input,
            session_id=_session_id,
            tier="LOW",  # updated after PASS 1 determines the real tier
        )

        # PASS 1 — Orchestrator Classification (skip if re-submitting confirmed delegation)
        if pending_delegation:
            delegation = pending_delegation
            confidence = delegation.pop("_memory_confidence", 1.0)
            gaps = delegation.pop("_memory_gaps", [])
        else:
            # Deterministic pre-classifier — catches cases the small LLM routinely misroutes
            quick = _quick_classify(user_input, context_window=context_window)
            if quick:
                delegation = quick
                confidence, gaps = 1.0, []
            else:
                _t_p1 = _time_mod.monotonic()
                try:
                    delegation = await _asyncio.wait_for(
                        self.cog.orchestrator_classify(user_input, context_window=context_window),
                        timeout=_PASS_TIMEOUT,
                    )
                except _asyncio.TimeoutError:
                    _rft = {"success": False, "outcome": "Classification timed out.", "detail": {},
                            "error": "PASS 1 timeout — please retry.", "next_action": None}
                    _dm = await self.cog.translator_pass(_rft)
                    return {"director_message": _dm, "confidence": 0.0, "gaps": []}
                except Exception as e:
                    _rft = {"success": False, "outcome": "Classification failed.", "detail": {},
                            "error": str(e), "next_action": None}
                    _dm = await self.cog.translator_pass(_rft)
                    return {"director_message": _dm, "confidence": 0.0, "gaps": []}
                self._log_pass(1, "orchestrator", user_input, delegation,
                               _time_mod.monotonic() - _t_p1)
                confidence = delegation.pop("_memory_confidence", 1.0)
                gaps = delegation.pop("_memory_gaps", [])

        agent = delegation.get("specialist") or delegation.get("delegate_to", "")

        # Normalise intent — map to a known key
        intent = delegation.get("intent", "")
        if intent not in INTENT_ACTION_MAP:
            intent = _AGENT_DEFAULT_INTENT.get(agent, "query")

        # Tier always derived deterministically — never trust the LLM
        tier = self.gov.get_intent_tier(intent) or INTENT_TIER_MAP.get(intent, "LOW")

        # ── Update envelope with PASS 1 results ──────────────────────────────
        _msg.envelope.tier = tier
        _msg.context.original_intent   = intent
        _msg.context.routing_rationale = delegation.get("reasoning_summary", "")
        _msg.set_payload({"intent": intent, "agent": agent, "delegation": delegation})
        _msg.append_pass(1, "orchestrator", 0.0, True)
        # Validate envelope before proceeding
        _env_errs = _msg.validate(1)
        if _env_errs:
            import logging as _lg
            _lg.getLogger(__name__).warning("PASS 1 envelope validation: %s", _env_errs)

        # Confidence gate — MID/HIGH only
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

        if confidence_acknowledged and pending_delegation:
            try:
                await self._log_ceo_confidence_override(user_input)
            except Exception:
                pass

        action = self._delegation_to_action({**delegation, "intent": intent})

        # Governance check
        try:
            rules = self.gov.validate(action, tier)
        except ValueError as e:
            # Normalize governance errors before passing to translator.
            # Raw text like "not in allowed_actions for tier LOW" causes the LLM to
            # hallucinate tier escalation ("available at higher tiers — Rex will allow it").
            # Hard block: governance failure is a hard stop, not an invitation to escalate.
            _gov_msg = "That action isn't permitted under the current governance policy."
            _rft = {"success": False, "outcome": _gov_msg, "detail": {},
                    "error": _gov_msg, "next_action": None}
            dm = await self.cog.translator_pass(_rft, tier=tier)
            return {"director_message": dm, "confidence": round(confidence, 3), "gaps": gaps,
                    "_governance_block": str(e)}

        # Confirmation gate
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

        # PASS 2 — Conditional security (tier=HIGH only; scanner path already evaluated above)
        # The pre-LLM scanner fires before PASS 1 (injection detection).
        # PASS 2 adds a second LLM-based evaluation for HIGH-tier actions regardless of scanner.
        # Skip PASS 2 when confirmed=True — explicit user double-confirmation IS the security gate.
        if tier == "HIGH" and not _scanner_evaluated and not security_confirmed and not confirmed:
            _t_p2 = _time_mod.monotonic()
            try:
                # Build a minimal scan result for the security agent
                class _HighTierScan:
                    flagged = True
                    categories = ["high_tier_action"]
                    matched_phrases = [intent]
                sec2 = await _asyncio.wait_for(
                    self.cog.security_evaluate(_HighTierScan(), user_input),
                    timeout=_PASS_TIMEOUT,
                )
            except Exception:
                sec2 = {"block": False, "risk_level": "low", "risk_categories": [],
                        "reasoning_summary": "Security PASS 2 unavailable — proceeding",
                        "required_mitigation": ""}
            _p2_dur = (_time_mod.monotonic() - _t_p2) * 1000
            self._log_pass(2, "security_agent", user_input, sec2,
                           _p2_dur / 1000)
            # Record PASS 2 clearance in envelope
            _clearance = "blocked" if sec2.get("block") else (
                "conditional" if sec2.get("required_mitigation") else "cleared"
            )
            try:
                _msg.set_security_clearance(_clearance)
                _msg.append_pass(2, "security_agent", _p2_dur, not sec2.get("block", False))
            except Exception:
                pass
            if sec2.get("block"):
                if self.ledger:
                    try:
                        self.ledger.append("pass2_security_block", "inbound", {
                            "intent": intent, "tier": tier,
                            "risk_level": sec2.get("risk_level", "high"),
                            "reasoning": sec2.get("reasoning_summary", ""),
                        })
                    except Exception:
                        pass
                _rft = {"success": False,
                        "outcome": f"Security review blocked this action: {sec2.get('reasoning_summary', '')}",
                        "detail": {}, "error": "Security block — HIGH tier action rejected.",
                        "next_action": None}
                dm = await self.cog.translator_pass(_rft, tier=tier)
                return {"director_message": dm, "confidence": round(confidence, 3), "gaps": gaps}

        # Short-circuit paths — all pass through translator (no raw output to Director)
        if action.get("domain") in ("ollama", "memory", "browser", "scheduler", "browser_config", "feeds", "memory_index"):
            if action.get("domain") == "ollama":
                conv_result = await self.cog.ask_conversational(user_input, context_window=context_window)
                conv_text = conv_result.get("response", "")
                exec_result = {"status": "ok", "response": conv_text}
                # Detect stated intentions and commit to memory (preserve existing Bug 4 fix)
                import re as _re_intent
                _intention_re = _re_intent.compile(
                    r"\bI('?ll| will)\s+(remember|store|save|add|note|keep|write that|make a note)\b"
                    r"|\bnoted\b"
                    r"|\bI've\s+(saved|stored|noted|added|written that)\b",
                    _re_intent.IGNORECASE,
                )
                if self.qdrant and _intention_re.search(conv_text):
                    try:
                        from datetime import date as _date, timedelta as _td
                        _monitor_re = _re_intent.compile(
                            r"\b(keep an eye|monitor|watch|alert me|check.*daily|daily.*check"
                            r"|notify me|remind me|track|follow up)\b",
                            _re_intent.IGNORECASE,
                        )
                        is_monitoring = bool(_monitor_re.search(user_input))
                        extra_meta = {"source": "stated_intention", "response_preview": conv_text[:120]}
                        if is_monitoring:
                            extra_meta["next_due"] = str(_date.today() + _td(days=1))
                        await self.cog.save_lesson(
                            f"Stated intention: {user_input[:250]}", user_input,
                            collection=PROSPECTIVE if is_monitoring else WORKING,
                            memory_type="prospective", writer="sovereign-core",
                            extra_metadata=extra_meta,
                        )
                    except Exception:
                        pass
                # Ollama response is already in Rex's voice — wrap as-is for translator
                _rft = {"success": True, "outcome": conv_text, "detail": {},
                        "error": None, "next_action": None}
            elif action.get("domain") == "feeds":
                # RSS/feed read — direct nanobot dispatch, no LLM planning needed
                exec_result = await self.nanobot.run("rss-digest", "get_entries", {"limit": 10})
                if exec_result is None:
                    exec_result = {"status": "error", "error": "nanobot returned None for rss-digest"}
                _rft = self._build_result_for_translator(intent, exec_result)
            else:
                exec_result = await self._dispatch(action, user_input,
                                                   delegation={**delegation, "intent": intent},
                                                   payload={"confirmed": confirmed},
                                                   security_confirmed=security_confirmed)
                _rft = self._build_result_for_translator(intent, exec_result)
            director_msg = await self.cog.translator_pass(_rft, tier=tier)
            result_dict = {
                "status": "ok", "intent": intent, "tier": tier, "agent": agent,
                "result": exec_result, "confidence": round(confidence, 3), "gaps": gaps,
                "director_message": director_msg,
            }
            if morning_briefing:
                result_dict["morning_briefing"] = morning_briefing
            return result_dict

        # ── PASS 3 outbound — Specialist plans the action ────────────────────
        # Confirmed continuations (skill_install confirm path): skip outbound reasoning —
        # the Director already reviewed and approved; proceed straight to execution.
        _confirmed_continuation = (
            confirmed
            and pending_delegation is not None
            and pending_delegation.get("_pending_load") is not None
        )
        _t3out_start = _time_mod.monotonic()
        if _confirmed_continuation:
            sp_out = {}
        else:
            try:
                sp_out = await _asyncio.wait_for(
                    self.cog.specialist_outbound(agent, delegation, user_input),
                    timeout=_PASS_TIMEOUT,
                )
            except _asyncio.TimeoutError:
                sp_out = {}
            except Exception as e:
                _rft = {"success": False, "outcome": "specialist_outbound_failed",
                        "detail": {}, "error": str(e), "next_action": None}
                _dm = await self.cog.translator_pass(_rft, tier=tier)
                return {"director_message": _dm, "confidence": round(confidence, 3), "gaps": gaps,
                        "status": "error", "intent": intent, "tier": tier, "agent": agent}
        _p3out_dur = (_time_mod.monotonic() - _t3out_start) * 1000
        self._log_pass(3, f"{agent}_outbound", delegation, sp_out,
                       _p3out_dur / 1000)
        # Record specialist's skill/operation selection in envelope context
        try:
            _msg.set_skill(
                sp_out.get("skill", ""),
                sp_out.get("operation", ""),
            )
            _msg.set_payload(sp_out)
            _msg.append_pass(3, f"{agent}_outbound", _p3out_dur, bool(sp_out))
            # Validate context has skill/operation for non-trivial operations
            if sp_out and action.get("domain") not in ("ollama", "memory"):
                _env_errs = _msg.validate(3, required_context_fields=["original_intent"])
                if _env_errs:
                    import logging as _lg
                    _lg.getLogger(__name__).warning("PASS 3 outbound envelope: %s", _env_errs)
        except Exception:
            pass

        # ── Execution (deterministic) ─────────────────────────────────────────
        try:
            execution_result = await _asyncio.wait_for(
                self._dispatch(
                    action, None,
                    delegation={**delegation, "intent": intent},
                    specialist=sp_out,
                    security_confirmed=security_confirmed,
                ),
                timeout=_PASS_TIMEOUT * 3,  # nanobot scripts can be slow
            )
        except _asyncio.TimeoutError:
            execution_result = {"status": "error", "error": "execution_timeout"}
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

        if execution_result is None:
            execution_result = {"status": "error",
                                "error": f"dispatch returned None for domain={action.get('domain')!r} op={action.get('operation')!r}"}

        # Early exit for governance confirmation gates — translator generates the prompt
        if execution_result.get("requires_confirmation") or execution_result.get("requires_double_confirmation"):
            _confirm_ctx = {
                "success": False, "outcome": "awaiting_confirmation",
                "detail": {
                    "action": execution_result.get("action", intent),
                    "summary": execution_result.get("summary", ""),
                    "review_decision": (execution_result.get("review_result") or {}).get("decision", ""),
                    "escalated": bool(execution_result.get("escalation_notice")),
                },
                "error": None, "next_action": "confirm_or_deny",
            }
            _dm = await self.cog.translator_pass(_confirm_ctx, tier=tier)
            result_dict = {
                "status": "ok", "intent": intent, "tier": tier, "agent": agent,
                "specialist_plan": sp_out, "result": execution_result,
                "confidence": round(confidence, 3), "gaps": gaps, "director_message": _dm,
            }
            for _gw_key in ("requires_confirmation", "requires_double_confirmation",
                            "pending_delegation", "summary", "escalation_notice"):
                if execution_result.get(_gw_key) is not None:
                    result_dict[_gw_key] = execution_result[_gw_key]
            if morning_briefing:
                result_dict["morning_briefing"] = morning_briefing
            return result_dict

        # Stamp execution_confirmed deterministically — LLM must never assert completion
        _exec_http = execution_result.get("http_status")
        if _exec_http is not None:
            _execution_confirmed = (
                execution_result.get("status") == "ok"
                and isinstance(_exec_http, int)
                and 200 <= _exec_http < 300
                and not execution_result.get("error")
            )
        else:
            _execution_confirmed = (
                (execution_result.get("status") == "ok"
                 or execution_result.get("success") is True)
                and not execution_result.get("error")
            )
        execution_result["execution_confirmed"] = _execution_confirmed

        # Memory cross-reference for file operations — Qdrant hits tagged _result_source="qdrant_memory"
        if (intent in ("list_files", "read_file")
                and execution_result.get("status") == "ok"
                and self.qdrant):
            try:
                _live_items = execution_result.get("items")
                _live_content = execution_result.get("content")
                _live_empty = (
                    (_live_items is not None and len(_live_items) == 0)
                    or (_live_content is not None and not str(_live_content).strip())
                )
                if _live_empty:
                    execution_result["_live_result_empty"] = True
                search_term = delegation.get("target") or user_input
                path_parts = [p for p in str(search_term).replace("/", " ").split() if len(p) > 2]
                search_q = " ".join(path_parts[:6]) if path_parts else user_input
                mem_hits = await self.qdrant.search_all_weighted(
                    search_q, query_type="knowledge", top_k=3
                )
                if mem_hits:
                    execution_result["memory_context"] = [
                        {"content": h.get("content", ""), "score": round(h.get("score", 0), 3),
                         "_result_source": "qdrant_memory"}
                        for h in mem_hits if h.get("score", 0) > 0.4
                    ]
            except Exception:
                pass

        # ── Security scan on nanobot results — all external data is untrusted ──
        # Nanobot results come from live external systems (IMAP, Nextcloud, etc.)
        # and could contain adversarial content injected by a compromised server.
        # Mark every nanobot result as [UNTRUSTED EXTERNAL CONTENT] until scanned.
        # specialist_inbound sees the annotation and treats the content accordingly.
        if execution_result.get("_trust") == "untrusted_external" and self.scanner:
            try:
                _nb_text = json.dumps(execution_result.get("result") or {})[:2000]
                _nb_scan = self.scanner.scan(_nb_text)
                if _nb_scan.flagged:
                    if self.ledger:
                        try:
                            self.ledger.append("nanobot_result_scan_flagged", "inbound", {
                                "intent": intent,
                                "categories": _nb_scan.categories,
                                "content_hash": _hashlib.sha256(_nb_text.encode()).hexdigest()[:16],
                            })
                        except Exception:
                            pass
                    execution_result["_untrusted_flagged"] = True
                    execution_result["_scan_categories"] = _nb_scan.categories
                execution_result["_trust"] = "scanned"
            except Exception:
                execution_result["_trust"] = "scan_error"

        # ── PASS 3 inbound — Specialist interprets the execution result ───────
        _t3in_start = _time_mod.monotonic()
        try:
            sp_in = await _asyncio.wait_for(
                self.cog.specialist_inbound(agent, delegation, sp_out, execution_result),
                timeout=_PASS_TIMEOUT,
            )
        except (_asyncio.TimeoutError, Exception) as _e3in:
            sp_in = {"success": _execution_confirmed, "outcome": "inbound_interpretation_unavailable",
                     "detail": execution_result, "anomaly": str(_e3in), "retry_with": None}
        _p3in_dur = (_time_mod.monotonic() - _t3in_start) * 1000
        self._log_pass(3, f"{agent}_inbound", execution_result, sp_in,
                       _p3in_dur / 1000)
        # Merge nanobot result into envelope and append PASS 3 inbound history
        try:
            _msg.merge_result({"sp_in": sp_in, "exec": execution_result})
            _msg.append_pass(3, f"{agent}_inbound", _p3in_dur, sp_in.get("success", False))
        except Exception:
            pass

        # Optional retry — one attempt if specialist requests it (not on confirmed continuations)
        if sp_in.get("retry_with") and not _confirmed_continuation:
            try:
                _retry_result = await _asyncio.wait_for(
                    self._dispatch(
                        action, None,
                        delegation={**delegation, "intent": intent, **sp_in["retry_with"]},
                        specialist=sp_out,
                        security_confirmed=security_confirmed,
                    ),
                    timeout=_PASS_TIMEOUT * 3,
                )
                if _retry_result:
                    execution_result = _retry_result
                    execution_result.setdefault("execution_confirmed", _execution_confirmed)
                    sp_in = await _asyncio.wait_for(
                        self.cog.specialist_inbound(agent, delegation, sp_out, execution_result),
                        timeout=_PASS_TIMEOUT,
                    )
            except Exception:
                pass  # keep original sp_in if retry fails

        # ── PASS 4 — Orchestrator evaluates result + makes memory decision ────
        _t4_start = _time_mod.monotonic()
        try:
            orch_eval = await _asyncio.wait_for(
                self.cog.orchestrator_evaluate(delegation, sp_in),
                timeout=_PASS_TIMEOUT,
            )
        except (_asyncio.TimeoutError, Exception) as _e4:
            orch_eval = {
                "approved": True,
                "feedback": f"Evaluation unavailable: {_e4}",
                "memory_action": "skip",
                "memory_payload": {},
                "result_for_translator": self._build_result_for_translator(intent, execution_result),
            }
        _p4_dur = (_time_mod.monotonic() - _t4_start) * 1000
        self._log_pass(4, "orchestrator_evaluate", sp_in, orch_eval,
                       _p4_dur / 1000)
        # Store orchestrator evaluation result + result_for_translator in envelope
        try:
            _msg.merge_result({"orch_eval": orch_eval,
                               "result_for_translator": orch_eval.get("result_for_translator")})
            _msg.append_pass(4, "orchestrator", _p4_dur, orch_eval.get("approved", True))
        except Exception:
            pass

        if not orch_eval.get("approved", True):
            _rft = {"success": False, "outcome": "plan_rejected",
                    "detail": {"feedback": orch_eval.get("feedback", "")},
                    "error": "Orchestrator rejected the plan.", "next_action": None}
            _dm = await self.cog.translator_pass(_rft, tier=tier)
            return {"director_message": _dm, "confidence": round(confidence, 3), "gaps": gaps,
                    "status": "error", "intent": intent, "tier": tier, "agent": agent}

        # ── Async memory write — never blocks the return path ─────────────────
        _memory_action = orch_eval.get("memory_action", "skip")
        _memory_payload = orch_eval.get("memory_payload") or {}
        if _memory_action and _memory_action != "skip" and self.qdrant:
            _asyncio.create_task(self._async_memory_write(
                _memory_action, _memory_payload, user_input, intent, _execution_confirmed,
            ))
        if self.qdrant:
            _asyncio.create_task(self._async_store_routing_decision(
                intent, agent, tier, _execution_confirmed,
            ))

        # ── PASS 5 — Translator (receives ONLY result_for_translator) ─────────
        _rft = (orch_eval.get("result_for_translator")
                or self._build_result_for_translator(intent, execution_result))

        # Diagnostic passthrough: the specialist_inbound and orchestrator_evaluate LLMs
        # both discard raw output from detail (schema shows {} and "keep it small").
        # For read/diagnostic intents the Director needs the actual output, not a summary.
        # Inject execution_result data deterministically — no LLM reliance.
        _DIAGNOSTIC_INTENTS = frozenset({
            "list_containers", "get_logs", "get_stats", "get_hardware", "list_processes",
            "read_host_file", "get_compose", "inspect_container", "systemctl_status",
            "journalctl", "apt_check", "github_read",
            "list_files", "read_file", "navigate", "search_files",
            "list_files_recursive", "read_files_recursive",
            "list_events", "list_calendars", "list_tasks", "recall_last_briefing",
            "fetch_email", "search_email", "fetch_message",
            "skill_audit", "skill_search",
            "configure_browser_auth",
            "read_feed",   # rss-digest entries — pass raw list, no LLM summarisation
            "research",
            "memory_list_keys", "memory_retrieve_key",  # MIP — pass structured index directly
        })
        # skill_search with no candidates — deterministic result, bypass translator invention
        if intent == "skill_search" and execution_result is not None:
            candidates = execution_result.get("candidates", [])
            if not candidates:
                _no_result_msg = (
                    execution_result.get("info")
                    or "No skills found matching your query. Try a more specific search or check GitHub directly."
                )
                _warn = execution_result.get("search_warning", "")
                _rft = {
                    "success": False,
                    "outcome": _no_result_msg,
                    "detail": {"search_warning": _warn} if _warn else {},
                    "error": _no_result_msg,
                    "next_action": None,
                }
            else:
                # Candidates found — strip skill_md bodies before translator sees them.
                # Full SKILL.md content confuses the small LLM (it reads the skill body
                # as instructions and hallucinates results). Pass metadata only.
                _slim = [
                    {k: v for k, v in c.items() if k not in ("skill_md", "raw_url")}
                    for c in candidates
                ]
                _rft = dict(_rft)
                _rft["success"] = True
                _rft["error"] = None
                _rft["detail"] = {"candidates": _slim}
                _rft["outcome"] = f"Found {len(candidates)} candidate skill(s)."

        elif intent in _DIAGNOSTIC_INTENTS and execution_result:
            _raw = {k: v for k, v in execution_result.items()
                    if not k.startswith("_")
                    and k not in ("status", "success", "error", "execution_confirmed",
                                  "request_id", "run_id", "elapsed_s", "node")}
            if _raw:
                _rft = dict(_rft)
                # Raw execution output takes precedence; preserve any LLM metadata
                _rft["detail"] = {**(_rft.get("detail") or {}), **_raw}
                # Clear LLM-generated outcome — it may contain wrong counts from the
                # truncated specialist_inbound view (lists capped at 5 items). Translator
                # renders detail directly via rule 8; empty outcome avoids misleading preamble.
                _rft["outcome"] = ""

        # Sanitise LLM-generated detail: strip internal keys before translator sees them
        _DETAIL_STRIP = frozenset({
            "success", "next_action", "approved", "feedback", "memory_action",
            "outcome", "agent", "delegate_to", "tier", "intent",
        })
        if isinstance(_rft.get("detail"), dict):
            _rft["detail"] = {
                k: v for k, v in _rft["detail"].items()
                if not k.startswith("_") and k not in _DETAIL_STRIP
            }
        _t5_start = _time_mod.monotonic()
        try:
            director_msg = await _asyncio.wait_for(
                self.cog.translator_pass(_rft, tier=tier),
                timeout=_PASS_TIMEOUT,
            )
        except (_asyncio.TimeoutError, Exception):
            director_msg = str(_rft.get("outcome", "Action completed."))
        _p5_dur = (_time_mod.monotonic() - _t5_start) * 1000
        self._log_pass(5, "translator", _rft, {"director_message": director_msg},
                       _p5_dur / 1000)
        # Append PASS 5 to history
        try:
            _msg.append_pass(5, "translator", _p5_dur, bool(director_msg))
        except Exception:
            pass

        result_dict = {
            "status": "ok",
            "intent": intent,
            "tier": tier,
            "agent": agent,
            "specialist_plan": sp_out,
            "result": execution_result,
            "confidence": round(confidence, 3),
            "gaps": gaps,
            "director_message": director_msg,
            "_envelope": _msg.envelope.to_dict(),
            "_history": [r.to_dict() for r in _msg.history],
        }
        # Promote gateway-critical fields to top level — gateway reads top-level only
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

    # ── Cognitive loop helpers ───────────────────────────────────────────────

    def _log_pass(self, pass_num: int, persona: str, input_data, output_data,
                  duration_s: float) -> None:
        """Audit hash per cognitive pass — structural hashes only, no raw content."""
        import hashlib as _hl, json as _js, logging as _lg
        _log = _lg.getLogger(__name__)
        try:
            _in_h = _hl.sha256(
                _js.dumps(input_data, default=str, sort_keys=True).encode()
            ).hexdigest()[:16]
            _out_h = _hl.sha256(
                _js.dumps(output_data, default=str, sort_keys=True).encode()
            ).hexdigest()[:16]
            _log.debug("PASS %d [%s] in=%s out=%s dur=%.2fs",
                       pass_num, persona, _in_h, _out_h, duration_s)
        except Exception:
            pass

    def _build_result_for_translator(self, intent: str, exec_result: dict) -> dict:
        """Convert raw adapter result → result_for_translator format for PASS 5."""
        if exec_result is None:
            return {"success": False, "outcome": "no_result", "detail": {},
                    "error": "dispatch returned None", "next_action": None}
        success = (
            (exec_result.get("status") == "ok" or exec_result.get("success") is True
             or exec_result.get("execution_confirmed") is True)
            and not exec_result.get("error")
        )
        detail = {k: v for k, v in exec_result.items()
                  if not k.startswith("_")
                  and k not in ("status", "success", "error", "execution_confirmed")}
        return {
            "success": success,
            "outcome": "ok" if success else "error",
            "detail": detail,
            "error": exec_result.get("error"),
            "next_action": None,
        }

    async def _async_memory_write(
        self, memory_action: str, memory_payload: dict, user_input: str,
        intent: str, execution_confirmed: bool,
    ) -> None:
        """Async memory write dispatched via asyncio.create_task() — never blocks return path."""
        _MUTATING_INTENTS = frozenset({
            "create_event", "create_task", "write_file", "send_email",
            "delete_file", "delete_email", "delete_task", "restart_container", "create_folder",
        })
        try:
            if not memory_payload.get("lesson"):
                return
            coll_decision = memory_payload.get("collection", "working_memory")
            mem_type = coll_decision if coll_decision in SOVEREIGN_COLLECTIONS else "lesson"
            extra_meta: dict = {}
            if mem_type == "episodic":
                extra_meta["outcome"] = memory_payload.get("outcome", "neutral")
            elif mem_type == "prospective":
                if memory_payload.get("next_due"):
                    extra_meta["next_due"] = memory_payload["next_due"]
                if intent in _MUTATING_INTENTS:
                    extra_meta["execution_confirmed"] = execution_confirmed
                    if not execution_confirmed:
                        extra_meta["outcome"] = "unconfirmed"
            elif mem_type == "relational":
                for f in ("concept_a", "concept_b", "shared", "diverges", "insight"):
                    if memory_payload.get(f):
                        extra_meta[f] = memory_payload[f]
            elif mem_type == "associative":
                for f in ("item_a_id", "item_b_id", "link_type"):
                    if memory_payload.get(f):
                        extra_meta[f] = memory_payload[f]
            await self.cog.save_lesson(
                memory_payload["lesson"], user_input,
                collection=WORKING,
                memory_type=mem_type,
                writer="sovereign-core",
                extra_metadata=extra_meta,
            )
        except Exception as _e:
            import logging as _lg
            _lg.getLogger(__name__).warning("async_memory_write failed: %s", _e)

    async def _async_store_routing_decision(
        self, intent: str, agent: str, tier: str, success: bool,
    ) -> None:
        """Store routing decision in episodic memory after PASS 4 approves."""
        try:
            lesson = (f"Routing: intent={intent!r} → agent={agent!r} "
                      f"tier={tier} success={success}")
            await self.cog.save_lesson(
                lesson, intent,
                collection=WORKING,
                memory_type="episodic",
                writer="sovereign-core",
                extra_metadata={"outcome": "positive" if success else "negative",
                                "routing": True},
            )
        except Exception:
            pass

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
                    metrics = await collect_all(getattr(self, 'app_state', None))
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
            if name == "apt_check":
                return await self.broker.exec_command("apt_check", {})
            if name == "systemctl_status":
                svc = container or (specialist or {}).get("service") or action.get("service", "docker")
                return await self.broker.exec_command("systemctl_status", {"service": svc})
            if name == "journalctl":
                sp = specialist or {}
                unit = sp.get("unit") or action.get("unit")
                lines = sp.get("lines") or action.get("lines", 50)
                params = {}
                if unit:
                    params["unit"] = unit
                params["lines"] = lines
                return await self.broker.exec_command("journalctl", params)
            return {"error": f"Unknown docker action: {name}"}

        if domain == "webdav":
            if name == "file_navigate":
                return await self.webdav.navigate(path)
            if name in ("file_list",):
                # RAID paths are mounted directly in sovereign-core — read via broker, not Nextcloud
                if path.startswith(("/home/sovereign/", "/docker/sovereign/")):
                    return await self.broker.read_host_file(path)
                return await self.webdav.list(path)
            if name == "file_read":
                # RAID paths are mounted in sovereign-core — route to broker hostfs, not Nextcloud WebDAV
                if path.startswith(("/home/sovereign/", "/docker/sovereign/")):
                    return await self.broker.read_host_file(path)
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
            if name == "file_list_recursive":
                return await self.nanobot.run(
                    "openclaw-nextcloud", "files_list_recursive", {"path": path}
                )
            if name == "file_read_recursive":
                return await self.nanobot.run(
                    "openclaw-nextcloud", "files_read_recursive", {"path": path}
                )

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
                    return nb.get("result") if nb.get("result") is not None else nb
                # Specialist may escalate a generic fetch_email to a targeted fetch_message
                # when they can identify the sender/subject from the user request.
                if sp.get("operation") == "fetch_message":
                    uid       = sp.get("uid", "")
                    from_addr = sp.get("from_addr", "")
                    subject   = sp.get("subject", "")
                    if uid or from_addr or subject:
                        nb = await self.nanobot.run(
                            _skill, f"fetch_message{_suf}",
                            {"uid": uid, "from_addr": from_addr, "subject": subject},
                        )
                        return nb.get("result") if nb.get("result") is not None else nb
                if name == "mail_list_inbox":
                    count = int(sp.get("count") or action.get("count", 50))
                    nb = await self.nanobot.run(_skill, f"fetch_unread{_suf}", {"limit": count})
                else:
                    count = int(sp.get("count") or action.get("count", 10))
                    nb = await self.nanobot.run(_skill, f"fetch_unread{_suf}", {"limit": count})
                return nb.get("result") if nb.get("result") is not None else nb

            if op == "fetch":
                uid       = sp.get("uid")       or action.get("uid", "")
                from_addr = sp.get("from_addr") or action.get("from_addr", "")
                subject   = sp.get("subject")   or action.get("subject", "")
                if not uid and not from_addr and not subject:
                    return {"error": "fetch_message requires uid, from_addr, or subject"}
                nb = await self.nanobot.run(
                    _skill, f"fetch_message{_suf}",
                    {"uid": uid, "from_addr": from_addr, "subject": subject},
                )
                return nb.get("result") if nb.get("result") is not None else nb

            if op == "search":
                criteria = sp.get("criteria") or action.get("criteria") or {}
                for key in ("subject", "from_addr", "since", "body"):
                    if action.get(key) and key not in criteria:
                        criteria[key] = action[key]
                # Flatten criteria dict to a query string for the broker imap search
                query_parts = [str(v) for v in criteria.values() if v]
                query = " ".join(query_parts) if query_parts else (sp.get("query") or action.get("query", ""))
                nb = await self.nanobot.run(_skill, f"search{_suf}", {"query": query, "limit": 10})
                result = nb.get("result") if nb.get("result") is not None else nb
                # Empty search is a valid result — stamp it so specialist_inbound doesn't mark failure
                if isinstance(result, dict) and result.get("messages") == [] and not result.get("error"):
                    result["_empty_search"] = True
                    result["status"] = "ok"
                return result

            if op == "flag":
                uid = sp.get("uid") or action.get("uid", "")
                if not uid:
                    return {"error": f"{name} requires a message UID"}
                if name == "mail_mark_read":
                    nb = await self.nanobot.run(_skill, "mark_read", {"uid": uid})
                else:
                    nb = await self.nanobot.run(_skill, "mark_unread", {"uid": uid})
                return nb.get("result") if nb.get("result") is not None else nb

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
                        "to": s.get("to") or action.get("to", ""),
                        "subject": s.get("subject") or action.get("subject", ""),
                        "body": draft or s.get("body") or action.get("body", ""),
                    }
                )
                return nb.get("result") if nb.get("result") is not None else nb

            return {"status": "error", "error": f"Unhandled mail operation: op={op!r} name={name!r}"}

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
                    or (delegation or {}).get("target")  # fallback: _quick_classify extracted query
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

        if domain == "memory_index":
            if not self.qdrant:
                return {"status": "error", "message": "Qdrant not available"}
            op = action.get("operation")

            if op == "list_keys":
                directory = await self.qdrant.list_all_keys()
                self._mip_listed_this_session = True  # Step 1 of MIP completed
                return {
                    "status": "ok",
                    "count": len(directory),
                    "directory": directory,
                }

            if op == "retrieve_key":
                # MIP protocol check: retrieve_key should always follow memory_list_keys.
                # Log a warning if the list-first step was skipped — do NOT block.
                if not self._mip_listed_this_session:
                    import logging as _mip_log
                    _mip_log.getLogger(__name__).warning(
                        "MIP protocol: memory_retrieve_key called without a prior "
                        "memory_list_keys this session — key may be guessed"
                    )
                    if self.ledger:
                        self.ledger.append("mip_protocol_warning", "memory_retrieve_key", {
                            "warning": "retrieve_key called without prior list_keys this session",
                            "key_requested": (action.get("key")
                                              or (delegation or {}).get("target", "")
                                              or (specialist or {}).get("key", "")),
                        })

                # Key from action field (set by _quick_classify target extraction),
                # specialist output, or raw prompt as last resort
                key = (action.get("key")
                       or (delegation or {}).get("target")
                       or (specialist or {}).get("key")
                       or (prompt or "").strip())
                if not key:
                    return {"status": "error", "message": "key required for memory_retrieve_key"}
                entry = await self.qdrant.retrieve_by_key(key)
                if entry is None:
                    return {
                        "status": "not_found",
                        "message": f"No memory entry with key '{key}' — use memory_list_keys to browse.",
                    }
                return {"status": "ok", "entry": entry}

            return {"error": f"Unknown memory_index operation: {op}"}

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
                import logging as _sched_log
                _sched_log.getLogger("sovereign.scheduler").info(
                    "[scheduler] schedule_task dispatch hit — tier=LOW, no security ledger write; prompt=%r",
                    (prompt or "")[:120],
                )
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
                # Store the task — LOW tier, no confirmation gate
                return await self.task_scheduler.store_task(parsed, human_confirmed=True)

            if name == "recall_last_briefing":
                return await self.task_scheduler.recall_last_briefing()

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

        if domain == "browser_config":
            import re as _re_bac
            import yaml as _bac_yaml
            _BAC_YAML_PATH = "/home/sovereign/governance/browser-auth-profiles.yaml"
            sp = specialist or {}

            # Deterministic parser: extract host, auth_type, env_var_names from prompt
            # (used in short-circuit path where specialist=None)
            _src = prompt or (delegation or {}).get("target", "") or ""
            _src_l = _src.lower()
            _host_det = (
                _re_bac.search(r'\b([a-z0-9.-]+\.[a-z]{2,})\b', _src_l)
            )
            _type_det = None
            for _t in ("headers", "bearer", "basic", "cookie"):
                if _t in _src_l:
                    _type_det = _t
                    break
            _env_vars_det = _re_bac.findall(r'\b([A-Z][A-Z0-9_]{2,})\b', _src)

            host = (sp.get("host") or action.get("host", "")
                    or (_host_det.group(1) if _host_det else "")).strip().lower()
            auth_type = (sp.get("auth_type") or sp.get("type") or
                         action.get("auth_type", "") or _type_det or "").strip().lower()

            if not host:
                return {"status": "error",
                        "error": "configure_browser_auth requires a hostname (e.g. api.github.com)"}
            if auth_type not in ("headers", "bearer", "basic", "cookie"):
                return {"status": "error",
                        "error": (f"Unknown auth type '{auth_type}' — "
                                  "supported: headers, bearer, basic, cookie")}

            from datetime import date as _bac_date
            profile_entry: dict = {
                "type": auth_type,
                "required_env": [],
                "added_by": "devops_agent",
                "added_at": _bac_date.today().isoformat(),
            }
            if sp.get("notes"):
                profile_entry["notes"] = sp["notes"]

            if auth_type == "headers":
                headers = sp.get("headers") or {}
                env_var = sp.get("env_var") or ((_env_vars_det[0]) if _env_vars_det else "")
                if not headers and env_var:
                    headers = {"Authorization": f"Bearer {{{env_var}}}"}
                if headers:
                    profile_entry["headers"] = headers
                    for _hv in headers.values():
                        for _m in _re_bac.findall(r'\{(\w+)\}', str(_hv)):
                            if _m not in profile_entry["required_env"]:
                                profile_entry["required_env"].append(_m)
            elif auth_type == "bearer":
                token_var = (sp.get("token_var") or sp.get("env_var")
                             or (_env_vars_det[0] if _env_vars_det else ""))
                if token_var:
                    profile_entry["token_var"] = token_var
                    profile_entry["required_env"].append(token_var)
            elif auth_type == "basic":
                username_var = (sp.get("username_var") or sp.get("user_var")
                                or (_env_vars_det[0] if _env_vars_det else ""))
                password_var = (sp.get("password_var") or sp.get("pass_var")
                                or (_env_vars_det[1] if len(_env_vars_det) > 1 else ""))
                if username_var:
                    profile_entry["username_var"] = username_var
                    profile_entry["required_env"].append(username_var)
                if password_var:
                    profile_entry["password_var"] = password_var
                    profile_entry["required_env"].append(password_var)
            elif auth_type == "cookie":
                cookie_var = (sp.get("cookie_var") or sp.get("env_var")
                              or (_env_vars_det[0] if _env_vars_det else ""))
                if cookie_var:
                    profile_entry["cookie_var"] = cookie_var
                    profile_entry["required_env"].append(cookie_var)

            env_vars_missing = [v for v in profile_entry["required_env"]
                                if not os.environ.get(v)]
            env_vars_present = [v for v in profile_entry["required_env"]
                                if os.environ.get(v)]

            # Write YAML profile to RAID
            try:
                try:
                    with open(_BAC_YAML_PATH) as _f:
                        _yaml_data = _bac_yaml.safe_load(_f) or {"profiles": {}}
                except FileNotFoundError:
                    _yaml_data = {"profiles": {}}
                if not isinstance(_yaml_data.get("profiles"), dict):
                    _yaml_data["profiles"] = {}
                _yaml_data["profiles"][host] = profile_entry
                with open(_BAC_YAML_PATH, "w") as _f:
                    _bac_yaml.dump(_yaml_data, _f, default_flow_style=False,
                                   allow_unicode=True, sort_keys=False)
            except Exception as _e:
                return {"status": "error",
                        "error": f"Failed to write browser-auth-profiles.yaml: {_e}"}

            # Hot-reload in-memory AUTH_PROFILES for env vars already present
            try:
                from execution.adapters import browser as _bm
                _bm.AUTH_PROFILES.update(_bm._load_auth_profiles_yaml())
            except Exception:
                pass

            result = {
                "status": "ok",
                "host": host,
                "auth_type": auth_type,
                "profile_written": True,
                "env_vars_present": env_vars_present,
                "env_vars_missing": env_vars_missing,
                "execution_confirmed": True,
            }
            if env_vars_missing:
                _lines = ["Add to secrets/browser.env:"]
                for _v in env_vars_missing:
                    _lines.append(f"  {_v}=<your_value>")
                _lines.append("Then restart sovereign-core to activate the profile.")
                result["setup_instructions"] = "\n".join(_lines)
                result["next_action"] = (
                    f"Add {', '.join(env_vars_missing)} to secrets/browser.env, "
                    "then restart sovereign-core."
                )
            else:
                result["next_action"] = (
                    "Restart sovereign-core to activate the new auth profile "
                    "(or it will activate on next restart — in-memory update applied)."
                )
            return result

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
