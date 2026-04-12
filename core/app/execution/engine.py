import json
import os
from datetime import datetime, timezone

from adapters.broker import BrokerAdapter


from adapters.nanobot import NanobotAdapter
from execution.adapters.github import GitHubAdapter
from execution.adapters.qdrant import (
    QdrantAdapter,
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
    "monitoring":  "monitoring_live",
    "notes":       "nanobot_live",
    "ncfs":     "nanobot_live",
    "ncingest": "nanobot_live",
    "dev_harness": "dev_harness_live",
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
    "list_notes":  {"domain": "notes", "operation": "read",   "name": "notes_list"},
    "read_note":   {"domain": "notes", "operation": "read",   "name": "notes_read"},
    "create_note": {"domain": "notes", "operation": "write",  "name": "notes_create"},
    "update_note": {"domain": "notes", "operation": "write",  "name": "notes_update"},
    "delete_note": {"domain": "notes", "operation": "delete", "name": "notes_delete"},
    # sovereign-nextcloud-fs — full filesystem navigation + OCS tagging
    # name matches governance allowed_actions; _dispatch_inner remaps to nanobot op names
    "ncfs_list":           {"domain": "ncfs", "operation": "read",   "name": "ncfs_list"},
    "ncfs_list_recursive": {"domain": "ncfs", "operation": "read",   "name": "ncfs_list_recursive"},
    "ncfs_read":           {"domain": "ncfs", "operation": "read",   "name": "ncfs_read"},
    "ncfs_move":           {"domain": "ncfs", "operation": "move",   "name": "ncfs_move"},
    "ncfs_copy":           {"domain": "ncfs", "operation": "copy",   "name": "ncfs_copy"},
    "ncfs_mkdir":          {"domain": "ncfs", "operation": "mkdir",  "name": "ncfs_mkdir"},
    "ncfs_delete":         {"domain": "ncfs", "operation": "delete", "name": "ncfs_delete"},
    "ncfs_tag":            {"domain": "ncfs", "operation": "tag",    "name": "ncfs_tag"},
    "ncfs_untag":          {"domain": "ncfs", "operation": "untag",  "name": "ncfs_untag"},
    "ncfs_search":         {"domain": "ncfs", "operation": "search", "name": "ncfs_search"},
    # sovereign-nextcloud-ingest — knowledge ingestion pipeline
    "ingest_file":    {"domain": "ncingest", "operation": "ingest",  "name": "ingest_file"},
    "ingest_folder":  {"domain": "ncingest", "operation": "ingest",  "name": "ingest_folder"},
    "ingest_status":  {"domain": "ncingest", "operation": "status",  "name": "ingest_status"},
    # cognitive skills — specialist synthesis, no external dispatch
    "session_wrap_up": {"domain": "session",       "operation": "wrap_up", "name": "session_wrap_up"},
    "memory_curate":   {"domain": "memory_curate", "operation": "curate",  "name": "memory_curate"},
    "fetch_email":        {"domain": "mail",   "operation": "read",    "name": "nc_list_unread"},
    "search_email":       {"domain": "mail",   "operation": "search",  "name": "nc_search"},
    "move_email":         {"domain": "mail",   "operation": "move",    "name": "nc_move"},
    "delete_email":       {"domain": "mail",   "operation": "delete",  "name": "nc_delete"},
    "send_email":         {"domain": "mail",   "operation": "send",    "name": "nc_send"},
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
    "news_brief":         {"domain": "news",    "operation": "brief"},
    # Tax ingest harness intents (Phase 1 — continuous hourly ingest)
    "tax_status":        {"domain": "tax", "operation": "status"},
    "tax_ingest_run":    {"domain": "tax", "operation": "run"},
    "tax_ingest_store":  {"domain": "tax", "operation": "store"},
    "tax_list_events":   {"domain": "tax", "operation": "list"},
    "tax_year_summary":  {"domain": "tax", "operation": "summary"},
    "tax_query":         {"domain": "tax", "operation": "query"},
    "tax_address_list":  {"domain": "tax", "operation": "addresses"},
    "tax_ingest_status": {"domain": "tax", "operation": "ingest_status"},
    # Tax report harness intents (Phase 2 — /do_tax report generation)
    "tax_report_query":  {"domain": "tax", "operation": "report_query"},
    "tax_report_ingest": {"domain": "tax", "operation": "report_ingest"},
    "tax_report_create": {"domain": "tax", "operation": "report_create"},
    "tax_report_notify": {"domain": "tax", "operation": "report_notify"},
    "tax_report_clear":  {"domain": "tax", "operation": "report_clear"},
    "tax_report_status": {"domain": "tax", "operation": "report_status"},
    # Memory intents
    "remember_fact":      {"domain": "memory",  "operation": "write"},
    "memory_recall":      {"domain": "memory",  "operation": "recall"},   # exact content search
    "memory_synthesise":  {"domain": "memory_synthesise", "operation": "synthesise"},
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
    "skill_search":            {"domain": "skills", "operation": "search"},          # harness step 1
    "skill_list_candidates":   {"domain": "skills", "operation": "list_candidates"}, # harness step 2
    "skill_review_candidate":  {"domain": "skills", "operation": "review_candidate"},# harness step 3
    "skill_review":            {"domain": "skills", "operation": "review"},          # legacy direct review
    "skill_load":              {"domain": "skills", "operation": "load"},
    "skill_audit":             {"domain": "skills", "operation": "audit"},
    "skill_install":           {"domain": "skills", "operation": "install"},         # harness step 4 / legacy composite
    "skill_clear_harness":     {"domain": "skills", "operation": "clear_harness"},   # wipe WM checkpoint
    # Nanobot intents — delegated execution sidecar (MID tier minimum)
    "nanobot_run":    {"domain": "nanobot", "operation": "run",    "name": "nanobot_run"},
    "nanobot_health": {"domain": "nanobot", "operation": "health", "name": "nanobot_health"},
    # Self-improvement harness intents — monitoring domain (LOW: all read/observe, no self-modification)
    "self_improve_observe":    {"domain": "monitoring", "operation": "observe"},
    "self_improve_proposals":  {"domain": "monitoring", "operation": "proposals"},
    "self_improve_baseline":   {"domain": "monitoring", "operation": "baseline"},
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
    "fetch_message": {"domain": "mail",   "operation": "fetch",   "name": "nc_fetch_message"},
    "mark_read":     {"domain": "mail",   "operation": "flag",    "name": "nc_mark_read"},
    "mark_unread":   {"domain": "mail",   "operation": "flag",    "name": "nc_mark_unread"},
    "list_folders":  {"domain": "mail",   "operation": "read",    "name": "nc_list_mailboxes"},
    "list_inbox":    {"domain": "mail",   "operation": "read",    "name": "nc_list_unread"},
    # Scheduler intents — devops_agent scope
    "schedule_task":      {"domain": "scheduler", "operation": "schedule", "name": "schedule_task"},
    "list_tasks":         {"domain": "scheduler", "operation": "list",     "name": "list_tasks"},
    "pause_task":         {"domain": "scheduler", "operation": "update",   "name": "pause_task"},
    "cancel_task":        {"domain": "scheduler", "operation": "update",   "name": "cancel_task"},
    "recall_last_briefing": {"domain": "scheduler", "operation": "recall", "name": "recall_last_briefing"},
    # Browser auth configuration — devops_agent scope (MID tier)
    "configure_browser_auth": {"domain": "browser_config", "operation": "configure_auth"},
    # Wallet intents — devops_agent scope (LOW/MID/HIGH tier)
    "wallet_read_config":     {"domain": "wallet",           "operation": "read",    "name": "wallet_read_config"},
    "wallet_get_address":     {"domain": "wallet",           "operation": "read",    "name": "wallet_get_address"},
    "wallet_sign_message":    {"domain": "wallet",           "operation": "sign",    "name": "wallet_sign_message"},
    "wallet_propose_safe_tx": {"domain": "wallet",           "operation": "propose", "name": "wallet_propose_safe_tx"},
    "wallet_get_proposals":   {"domain": "wallet",           "operation": "read",    "name": "wallet_get_proposals"},
    "wallet_get_btc_xpub":    {"domain": "wallet",           "operation": "read",    "name": "wallet_get_btc_xpub"},
    # Wallet watchlist intents — watch address management via Qdrant MIP
    "wallet_list_addresses":  {"domain": "wallet_watchlist", "operation": "list",    "name": "wallet_list_addresses"},
    "wallet_add_address":     {"domain": "wallet_watchlist", "operation": "add",     "name": "wallet_add_address"},
    "wallet_remove_address":  {"domain": "wallet_watchlist", "operation": "remove",  "name": "wallet_remove_address"},
    "wallet_update_address":  {"domain": "wallet_watchlist", "operation": "update",  "name": "wallet_update_address"},
    "wallet_check_address":   {"domain": "wallet_watchlist", "operation": "check",   "name": "wallet_check_address"},
    "wallet_portfolio":       {"domain": "wallet_watchlist", "operation": "check",   "name": "wallet_portfolio"},
    # Dev-Harness intents — devops_agent scope
    # Phase 1 (Analyse) and Phase 2→3 auto-chain are triggered by dev_analyse.
    # Phase 4 (Execute) is triggered by dev_approve after Director reviews Phase 3 plan.
    "dev_analyse": {"domain": "dev_harness", "operation": "analyse"},
    "dev_status":  {"domain": "dev_harness", "operation": "status"},
    "dev_approve": {"domain": "dev_harness", "operation": "approve"},  # triggers Phase 4
    "dev_reject":  {"domain": "dev_harness", "operation": "reject"},
    "dev_verify":  {"domain": "dev_harness", "operation": "verify"},
    "dev_clear":   {"domain": "dev_harness", "operation": "clear"},
    # Portal introspection
    "skill_status":   {"domain": "portal", "operation": "read", "name": "skill_status"},
    "harness_status": {"domain": "portal", "operation": "read", "name": "harness_status"},
}

# Tier required for each operation — deterministic, never from LLM
INTENT_TIER_MAP = {
    "inspect_container": "LOW", "get_compose": "LOW", "read_host_file": "LOW",
    "get_hardware": "LOW", "list_processes": "LOW",
    "apt_check": "LOW", "systemctl_status": "LOW", "journalctl": "LOW",
    "list_containers": "LOW", "get_logs": "LOW", "get_stats": "LOW",
    "list_files": "LOW", "navigate": "LOW", "read_file": "LOW", "search_files": "LOW",
    "list_files_recursive": "LOW", "read_files_recursive": "LOW",
    "list_notes": "LOW", "read_note": "LOW",
    "create_note": "MID", "update_note": "MID", "delete_note": "MID",
    # sovereign-nextcloud-fs tiers
    # All ncfs ops are LOW — Nextcloud is Director-trusted; no confirmation required for FS ops.
    # New files arriving in /downloads/ are still scanned before ingest (inline_scan gate).
    "ncfs_list": "LOW", "ncfs_list_recursive": "LOW", "ncfs_read": "LOW", "ncfs_search": "LOW",
    "ncfs_move": "LOW", "ncfs_copy": "LOW", "ncfs_mkdir": "LOW", "ncfs_delete": "LOW",
    "ncfs_tag": "LOW", "ncfs_untag": "LOW",
    # sovereign-nextcloud-ingest tiers — MID because memory write follows confirmation
    "ingest_file": "MID", "ingest_folder": "MID", "ingest_status": "LOW",
    "session_wrap_up": "LOW", "memory_curate": "LOW",
    "fetch_email": "LOW", "search_email": "LOW", "fetch_message": "LOW",
    "mark_read": "LOW", "mark_unread": "LOW", "list_folders": "LOW", "list_inbox": "LOW",
    "read_feed": "LOW",
    "news_brief": "LOW",
    "tax_status": "LOW", "tax_ingest_run": "LOW", "tax_ingest_store": "MID",
    "tax_list_events": "LOW", "tax_year_summary": "LOW",
    "tax_query": "LOW", "tax_address_list": "LOW", "tax_ingest_status": "LOW",
    "tax_report_query": "LOW", "tax_report_ingest": "LOW", "tax_report_create": "LOW",
    "tax_report_notify": "LOW", "tax_report_clear": "LOW", "tax_report_status": "LOW",
    "move_email": "MID",
    "list_calendars": "LOW", "list_events": "LOW",
    "delete_event": "MID", "update_event": "MID",
    "query": "LOW", "research": "LOW", "web_search": "LOW", "fetch_url": "LOW",
    "restart_container": "MID", "write_file": "MID", "send_email": "MID", "create_event": "MID",
    "create_task": "MID", "complete_task": "MID", "create_folder": "MID",
    "delete_file": "HIGH", "delete_email": "HIGH", "delete_task": "HIGH",
    "remember_fact": "LOW",
    "memory_recall": "LOW",
    "memory_list_keys": "LOW",
    "memory_retrieve_key": "LOW",
    "memory_synthesise": "LOW",
    # Skill harness tiers — explicit steps with validation gates + WM checkpoints
    "skill_search":           "LOW",   # search + write checkpoint
    "skill_list_candidates":  "LOW",   # read checkpoint
    "skill_review_candidate": "LOW",   # pre-scan + full review + update checkpoint
    "skill_clear_harness":    "LOW",   # wipe checkpoint
    "skill_install":          "HIGH",  # checkpoint-gated install — requires Director confirm
    # NOTE: other skill_* tiers are governed by governance.json intent_tiers
    # Wallet tiers
    "wallet_read_config":     "LOW",
    "wallet_get_address":     "LOW",
    "wallet_sign_message":    "MID",
    "wallet_propose_safe_tx": "HIGH",
    "wallet_get_proposals":   "MID",
    "wallet_get_btc_xpub":    "LOW",
    # Wallet watchlist tiers
    "wallet_list_addresses":  "LOW",
    "wallet_check_address":   "LOW",
    "wallet_portfolio":       "LOW",
    "wallet_add_address":     "MID",
    "wallet_remove_address":  "MID",
    "wallet_update_address":  "MID",
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
    # Self-improvement harness tiers — all LOW (observe/report only; proposals require Director approval)
    "self_improve_observe":    "LOW",
    "self_improve_proposals":  "LOW",
    "self_improve_baseline":   "LOW",
    # Self-diagnostic read intents — LOW
    "read_audit_log":          "LOW",
    "memory_promotion_status": "LOW",
    "soul_checksum_status":    "LOW",
    # GitHub — tiers enforced per governance policy
    "github_read":          "LOW",   # monitoring, releases, pending updates
    "github_push_doc":      "MID",   # standard docs and as-built updates
    "github_push_soul":     "HIGH",  # soul/constitution/governance docs — double confirmation
    "github_push_security": "HIGH",  # security pattern files — double confirmation
    # Dev-Harness tiers
    # dev_analyse/status/reject/verify/clear: LOW — deterministic, no Director confirmation
    # dev_approve: MID — Director confirms before Phase 4 (CC runsheet generation + prospective write)
    # NOTE: dev_approve is NOT HIGH — Phase 4 never self-modifies; it generates a runsheet for CC.
    # The broker trust level (medium for analysis scripts) is a separate concern from these tiers.
    "dev_analyse": "LOW",
    "dev_status":  "LOW",
    "dev_approve": "MID",
    "dev_reject":  "LOW",
    "dev_verify":  "LOW",
    "dev_clear":   "LOW",
    "skill_status":   "LOW",
    "harness_status": "LOW",
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

    # ── Session wrap-up — closure signals; checked FIRST, before all other guards ──
    # These are unambiguous — must never fall through to LLM or time_signals web_search.
    _closure_early_kw = (
        "that's all", "thats all", "goodbye", "good bye", "wrap up", "wrap-up",
        "signing off", "sign off", "end of session", "we're done", "done for today",
        "thanks bye", "catch you later", "until tomorrow", "close session",
        "session wrap", "closing out", "close out",
    )
    if any(w in u for w in _closure_early_kw):
        return {
            "delegate_to": "research_agent",
            "intent": "session_wrap_up",
            "target": user_input,
            "reasoning_summary": "Session closure — wrap-up synthesis",
        }

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
        # "remember/store/note this URL" — memory write, not a fetch
        _mem_url_kw = ("remember", "store", "note that", "don't forget", "memoris", "memoriz", "save this")
        if any(w in u for w in _mem_url_kw):
            return None  # fall through to CEO LLM — memory_agent will handle it
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
        # "Remember to check" / "check daily and report" patterns
        # These are scheduling requests masquerading as memory writes.
        "remember to check", "need to check daily", "check this daily",
        "check daily and report", "check it daily", "check daily for",
        "monitor this daily", "watch this daily", "track this daily",
        "keep an eye on this daily", "report when", "report if",
        "alert when", "notify when it", "let me know when it",
        "let me know when the", "tell me when it", "tell me when the",
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
        "my notes", "list notes", "show notes", "nextcloud notes", "notes list", "nc notes",
        "all notes", "the notes", "list the notes", "notes from nextcloud", "get notes",
        "create note", "create a note", "new note", "add a note", "write a note",
        "update note", "edit note", "change note",
        "delete note", "remove note",
        "read note", "open note",
        "docker", "container", "restart", "logs", "service",
        "search the web", "look online", "search online", "find on the internet",
        "web search", "look up online", "internet",
        "remember", "store", "memoris", "memoriz", "note that", "don't forget",
        "shopping list", "grocery list", "to-do list", "todo list", "wish list",
        "to my list", "on my list", "to the list", "on the list",
        "github", "repo", "commit", "push to", "sovereign repo",
        "skill", "clawhub", "openclaw",
        "candidate", "candidates", "review candidate", "install candidate",
        "clear skill", "clear candidates", "skill search",
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
        # Wallet watchlist
        "watchlist", "watch list", "watched addresses", "watching addresses",
        "watch this address", "add to watchlist", "stop watching", "unwatch",
        "update watchlist", "rename watchlist", "relabel watchlist",
        "update the watchlist", "change the label",
        "portfolio", "portfolio value", "portfolio worth", "wallet balance",
        "check address", "check wallet", "monitor this address",
        # Dev-Harness
        "dev analyse", "dev analysis", "dev harness", "dev status",
        "approve dev", "reject dev", "verify dev", "dev clear",
        "code analysis", "code quality", "run analysis", "harness analyse",
        # Session wrap-up closure signals
        "that's all", "thats all", "goodbye", "good bye", "wrap up", "wrap-up",
        "signing off", "sign off", "end of session", "we're done", "done for today",
        "thanks bye", "catch you later", "until tomorrow", "close session",
        "session wrap", "closing out", "close out",
        # Memory curation
        "curate memory", "curate working memory", "review working memory",
        "memory curation", "promote memory", "memory review", "review memories",
        "clean up memory", "cleanup memory",
        # Memory synthesis
        "synthesise memory", "synthesize memory", "memory synthesis",
        "run synthesis", "memory synthesise", "memory synthesize",
        # Portal introspection
        "skill status", "harness status", "what skills", "what harnesses",
        "list skills", "show skills", "installed skills", "loaded skills",
        "harness state", "show harnesses",
        # Tax harness
        "tax event", "tax events", "tax ingest", "tax harness", "tax status",
        "tax summary", "tax year", "tax report", "my taxes", "staking reward",
        "staking rewards", "disposal event", "income event", "wirex tax",
        "swyftx tax", "tax address", "taxable wallet", "tax wallet",
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

    # Note-suffix guard: "read the [title] note" / "delete the [title] note" —
    # title sits between the verb and "note", so keyword substring matching misses it.
    import re as _re_ns
    _note_suffix = bool(_re_ns.search(r'\bnote\b\s*$', u))

    # "Please" prefix signals a direct command/request — always pass the guard.
    _please_prefix = u.startswith("please ")
    if not prior_has_system and not any(sig in u for sig in _system_signals) and not _note_suffix and not _please_prefix:
        return {
            "delegate_to": "research_agent", "intent": "query",
            "target": None, "tier": "LOW",
            "reasoning_summary": "No system domain signals — conversational query",
        }

    # ── News brief — routes to news harness (RSS + Grok + browser, parallel) ──
    # Must be checked BEFORE the RSS block — overlapping keywords like "news feed" would
    # otherwise route to rss-digest only. Generic "news" requests → news harness.
    _news_kw = (
        "what's in the news", "whats in the news",
        "news brief", "news update", "news summary",
        "what's happening", "whats happening",
        "current events", "what's trending", "whats trending",
        "morning news", "today's news", "todays news",
        "latest headlines", "news today", "news briefing",
    )
    _is_news_brief = any(w in u for w in _news_kw)
    if _is_news_brief:
        return {
            "intent": "news_brief", "delegate_to": "research_agent",
            "tier": "LOW", "confidence": 0.90,
            "reasoning_summary": "News brief request — news harness",
        }

    # ── Tax harness — query / status / run ────────────────────────────────────
    _tax_kw = (
        "tax event", "tax events", "tax ingest", "tax harness",
        "tax summary", "tax year", "tax report", "tax status",
        "my taxes", "staking reward", "staking rewards",
        "disposal event", "income event", "wirex tax", "swyftx tax",
        "tax address", "taxable wallet", "tax wallet",
        "do tax", "run tax report", "tax ingest status",
    )
    _is_tax = any(w in u for w in _tax_kw)
    if _is_tax:
        # Pick the most specific sub-intent
        if any(w in u for w in ("run harness", "ingest run", "run tax", "trigger tax")):
            _tax_intent = "tax_ingest_run"
        elif any(w in u for w in ("store", "save tax", "persist")):
            _tax_intent = "tax_ingest_store"
        elif any(w in u for w in ("ingest status", "tax status check", "preflight", "pre-flight")):
            _tax_intent = "tax_ingest_status"
        elif any(w in u for w in ("list events", "show events", "all events", "list tax")):
            _tax_intent = "tax_list_events"
        elif any(w in u for w in ("summary", "year summary", "annual", "total")):
            _tax_intent = "tax_year_summary"
        elif any(w in u for w in ("address", "wallet address", "taxable wallet")):
            _tax_intent = "tax_address_list"
        elif any(w in u for w in ("query", "find event", "look up", "search tax")):
            _tax_intent = "tax_query"
        else:
            _tax_intent = "tax_status"
        return {
            "intent": _tax_intent, "delegate_to": "business_agent",
            "tier": "MID" if _tax_intent == "tax_ingest_store" else "LOW",
            "confidence": 0.88,
            "reasoning_summary": f"Tax harness request — {_tax_intent}",
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
        # If account not explicit in user_input, infer from context_window (e.g. user just
        # checked business email → follow-up "delete X" / "read X" should use business).
        if account is None and context_window:
            for _cw_turn in reversed((context_window or [])[-6:]):
                _cw_text = ""
                if isinstance(_cw_turn, dict):
                    _cw_text = (_cw_turn.get("user", "") + " " + _cw_turn.get("assistant", "")).lower()
                elif isinstance(_cw_turn, str):
                    _cw_text = _cw_turn.lower()
                if any(s in _cw_text for s in ("business email", "business inbox", "check my business", "my business")):
                    account = "business"
                    break
                if any(s in _cw_text for s in ("personal email", "personal inbox", "check my personal", "my personal")):
                    account = "personal"
                    break
        if any(w in u for w in _folder_kw):
            return {
                "delegate_to": "business_agent", "intent": "list_folders",
                "target": account, "tier": "LOW",
                "reasoning_summary": "Email folder list — deterministic pre-classifier",
            }
        if any(w in u for w in _send_kw):
            import re as _re_send
            _to_m = _re_send.search(r'\bto\s+([\w.+-]+@[\w.-]+\.[a-z]{2,})', user_input, _re_send.IGNORECASE)
            return {
                "delegate_to": "business_agent", "intent": "send_email",
                "target": account, "tier": "MID",
                "to": _to_m.group(1) if _to_m else "",
                # subject/body extracted by PASS 3 specialist from full user_input
                "reasoning_summary": "Email send — deterministic pre-classifier",
            }
        if any(w in u for w in _delete_kw):
            import re as _re_del
            _del_m = _re_del.search(r'\[id:(\d+)\]', u) or _re_del.search(r'\b(\d{4,6})\b', u)
            return {
                "delegate_to": "business_agent", "intent": "delete_email",
                "target": account, "tier": "HIGH",
                "database_id": _del_m.group(1) if _del_m else "",
                "reasoning_summary": "Email delete — deterministic pre-classifier (pronoun resolved from context)",
            }
        if any(w in u for w in _move_kw):
            import re as _re_mv
            _mv_m = _re_mv.search(r'\[id:(\d+)\]', u) or _re_mv.search(r'\b(\d{4,6})\b', u)
            return {
                "delegate_to": "business_agent", "intent": "move_email",
                "target": account, "tier": "MID",
                "database_id": _mv_m.group(1) if _mv_m else "",
                "reasoning_summary": "Email move/archive — deterministic pre-classifier (pronoun resolved from context)",
            }
        # Fetch a specific message by databaseId or id-tag — must come before _search_kw
        import re as _re_id
        _fetch_msg_kw = ("read email", "open email", "show email", "view email", "fetch email",
                         "read message", "open message", "show message", "view message", "fetch message",
                         "read the email", "open the email", "show me email", "email number",
                         "message number", "email id", "message id", "email #", "message #")
        _has_id_ref = bool(_re_id.search(r'\[id:\d+\]', u)) or any(w in u for w in _fetch_msg_kw)
        if _has_id_ref:
            # Extract databaseId from [id:XXXX] tag or bare 4-5 digit number
            _db_m = _re_id.search(r'\[id:(\d+)\]', u) or _re_id.search(r'\b(\d{4,6})\b', u)
            return {
                "delegate_to": "business_agent", "intent": "fetch_message",
                "target": account, "tier": "LOW",
                "database_id": _db_m.group(1) if _db_m else "",
                "reasoning_summary": "Email fetch by ID — deterministic pre-classifier",
            }
        if any(w in u for w in _search_kw):
            return {
                "delegate_to": "business_agent", "intent": "search_email",
                "target": account, "tier": "LOW",
                # query extracted by PASS 3 specialist from full user_input
                "reasoning_summary": "Email search — deterministic pre-classifier",
            }
        return {
            "delegate_to": "business_agent", "intent": "fetch_email",
            "target": account, "tier": "LOW",
            "reasoning_summary": "Email fetch — deterministic pre-classifier",
        }

    # Capability / tool diagnostics — "is X working?", "can you access X?", "do you have internet?"
    # These must NOT reach devops_agent or PASS 4 rejection. Route as LOW query to research_agent.
    _capability_kw = (
        "is your internet", "is your search", "is your browser", "is the internet",
        "is search working", "is browser working", "can you access the internet",
        "can you browse", "can you search", "do you have internet", "have internet access",
        "is your fetch", "is your web", "internet working", "search working",
        "can you use the internet", "can you go online", "can you visit",
        "are you able to browse", "are you able to search", "are you able to access",
    )
    if any(w in u for w in _capability_kw):
        return {
            "delegate_to": "research_agent", "intent": "query",
            "target": user_input, "tier": "LOW",
            "reasoning_summary": "Capability diagnostic — research_agent query fast-path",
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
        "create document", "create a document", "save to nextcloud",
        "save a file", "write to nextcloud", "put a file",
        "new document",
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
        "list files", "list the files", "list my files", "show files",
        "what files", "show me my files", "show me the files",
        # "what's in" and "whats in" removed — too broad (matches "what's in my fridge" etc.)
        "how many files", "how many templates", "how many documents", "how many items",
        "count files", "count templates", "count the files", "count the templates",
        "recount", "re-count",
    )
    # "how many X in /path/" — extract path deterministically
    import re as _re_lk
    _how_many_path_m = _re_lk.search(r'how many\s+\w+(?:\s+\w+)?\s+(?:are\s+)?(?:in|inside|under)\s+(/[/\w_.\- ]+)', u)
    if _how_many_path_m:
        _hm_path = _how_many_path_m.group(1).rstrip().rstrip('/')
        return {
            "delegate_to": "business_agent", "intent": "list_files",
            "target": (_hm_path if _hm_path.startswith("/") else f"/{_hm_path}"),
            "tier": "LOW",
            "reasoning_summary": "File count query — deterministic pre-classifier",
        }

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
    # NL skill install fallback — for plain-text "install a skill for X" requests.
    # Primary path is /install command (see gateway.py CommandHandler + _run_install_harness).
    # This fallback routes to the legacy composite flow for users who don't use the command.
    import re as _re_sk_pre
    _skill_install_kw = (
        "install a skill", "install skill", "install the skill",
        "get me a skill", "get a skill", "load a skill", "load skill",
        "add a skill", "add skill", "set up a skill",
    )
    _skill_install_match = (
        any(w in u for w in _skill_install_kw)
        or bool(_re_sk_pre.search(r"\b(install|load|add)\b.{0,40}\bskill\b", u))
        or (prior_domain == "skills" and bool(_re_sk_pre.search(r"\b(install|load|add)\b\s+\S", u)))
    )
    if _skill_install_match:
        return {
            "delegate_to": "devops_agent", "intent": "skill_install",
            "target": user_input, "tier": "MID",
            "reasoning_summary": "Skill install NL fallback — use /install for best experience",
        }

    _skill_search_kw = (
        "clawhub", "skill registry", "search for skills", "find a skill",
        "find skills", "look for skills", "browse skills",
    )
    if any(w in u for w in _skill_search_kw):
        return {
            "delegate_to": "devops_agent", "intent": "skill_search",
            "target": user_input, "tier": "LOW",
            "reasoning_summary": "Skill registry search — deterministic pre-classifier",
        }

    # Skill harness step routing — explicit step commands
    # These must precede the generic skill_install / skill_search blocks.
    import re as _re_sk_h
    _cand_num = _re_sk_h.search(r'\b([1-9]\d?)\b', u)  # candidate number if present
    _cand_id  = int(_cand_num.group(1)) if _cand_num else None

    if prior_domain == "skills" or "skill" in u or "candidate" in u:
        # "list candidates" / "what skills did we find" / "show candidates"
        if any(w in u for w in ("list candidates", "show candidates", "what skills did we find",
                                "what did we find", "skills found", "candidate list",
                                "what candidates", "show me the candidates")):
            return {
                "delegate_to": "devops_agent", "intent": "skill_list_candidates",
                "target": None, "tier": "LOW",
                "reasoning_summary": "Skill harness: list candidates from WM checkpoint",
            }
        # "review candidate N" / "review skill N" / "review number N"
        if _re_sk_h.search(r'\b(review|check|examine|assess)\b.{0,20}\b(candidate|skill|number|#)\b', u):
            return {
                "delegate_to": "devops_agent", "intent": "skill_review_candidate",
                "target": str(_cand_id or 1), "tier": "LOW",
                "reasoning_summary": f"Skill harness: review candidate {_cand_id or 1} from WM checkpoint",
            }
        # "install candidate N" / "install skill N" / "install number N" — harness-gated install
        if _cand_id and _re_sk_h.search(r'\b(install|load|add)\b', u):
            return {
                "delegate_to": "devops_agent", "intent": "skill_install",
                "target": str(_cand_id), "tier": "HIGH",
                "reasoning_summary": f"Skill harness: install candidate {_cand_id} (checkpoint-gated)",
            }
        # "clear skill search" / "clear skill session" / "reset skills"
        if any(w in u for w in ("clear skill", "reset skill", "clear session", "clear candidates",
                                "wipe skill search", "start over skill")):
            return {
                "delegate_to": "devops_agent", "intent": "skill_clear_harness",
                "target": None, "tier": "LOW",
                "reasoning_summary": "Skill harness: clear WM checkpoint",
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

    # "please remember X" without "that/this" — direct command, not a task reminder.
    # _please_prefix already passed the conversational guard; if the message contains
    # "remember" but NOT "remember to" (prospective task), treat it as a fact store.
    # Excludes "please remember to check/do/run X" which should route to scheduler.
    if _please_prefix and _re_mem.search(r"\bremember\b", u) and not _re_mem.search(r"\bremember\s+to\b", u, _re_mem.IGNORECASE):
        return {
            "delegate_to": "research_agent", "intent": "remember_fact",
            "target": None, "tier": "LOW",
            "reasoning_summary": "please+remember direct command — deterministic pre-classifier",
        }

    # Memory Index Protocol — list all keys (directory scan, no vector search)
    # ── Rex's own ETH address — deterministic read, no memory lookup ────────
    _rex_eth_kw = (
        "your eth address", "your ethereum address", "rex eth address",
        "rex's eth address", "rex's address", "your wallet address",
        "what is your address", "what's your address", "your address",
    )
    if any(w in u for w in _rex_eth_kw):
        return {
            "delegate_to": "devops_agent", "intent": "wallet_get_address",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Rex ETH address read — deterministic",
        }

    _mem_list_kw = (
        "list my memories", "list all memories", "show my memories",
        "show memory", "memory keys", "memory index", "memory directory",
        "what do you remember", "do you remember", "what's in memory", "what is in memory",
        "look up in memory", "retrieve from memory",
        # Address recall — "what is this address", "do you know this address", etc.
        "what is this address", "what address is this", "do you know this address",
        "do you know what this address", "what is that address", "what's this address",
        "which address is", "which wallet is",
        # Specific endpoint lookups — route to MIP not web_search
        "validator queue url", "validator queue link", "validatorqueue",
    )
    # 0x hex address in question context → exact content recall from semantic memory.
    # "do you remember 0x...", "what is this address 0x...", "do you know 0x..." etc.
    # Extracts the address and passes it as target for MatchText search — no LLM involved.
    import re as _re_addr
    _addr_match = _re_addr.search(r'\b(0x[0-9a-fA-F]{40})\b', u)
    _is_question = any(w in u for w in ("what", "which", "who", "do you", "is this", "is that", "know", "remember"))
    if _addr_match and _is_question:
        return {
            "delegate_to": "memory_agent", "intent": "memory_recall",
            "target": _addr_match.group(1), "tier": "LOW",
            "reasoning_summary": "0x address recall — deterministic MatchText search in semantic",
        }
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

    # ── Wallet watchlist intents ──────────────────────────────────────────────
    # Mutating ops (add/remove) checked BEFORE list — "add to the watchlist" contains
    # "watchlist" which would otherwise mis-route to wallet_list_addresses.
    _wallet_remove_kw = (
        "stop watching", "remove from watchlist", "unwatch", "stop monitoring address",
        "remove watch address", "remove address from", "delete from watchlist",
    )
    if any(w in u for w in _wallet_remove_kw):
        import re as _re_waddr
        _waddr_m = _re_waddr.search(r'\b(0x[0-9a-fA-F]{40}|bc1[a-z0-9]{25,90}|[13][a-zA-Z0-9]{25,34})\b', u)
        _waddr = _waddr_m.group(1) if _waddr_m else u
        return {
            "delegate_to": "business_agent", "intent": "wallet_remove_address",
            "target": _waddr, "tier": "MID",
            "reasoning_summary": "Remove address from watchlist (MID — requires confirmation)",
        }

    _wallet_add_kw = (
        "watch this address", "watch address", "add to watchlist", "add this wallet",
        "monitor this address", "add this address", "start watching",
    )
    if any(w in u for w in _wallet_add_kw):
        import re as _re_waddr2
        _waddr2_m = _re_waddr2.search(r'\b(0x[0-9a-fA-F]{40}|bc1[a-z0-9]{25,90}|[13][a-zA-Z0-9]{25,34})\b', u)
        _waddr2 = _waddr2_m.group(1) if _waddr2_m else ""
        # Extract label — patterns: "as [the] X", "called X", "named X", "label X"
        _lbl_m = _re_waddr2.search(
            r'(?:as\s+(?:the\s+)?|called\s+|named\s+|label(?:\s+it)?\s+)([A-Za-z0-9][A-Za-z0-9 \-_]+)',
            u,
        )
        _waddr2_label = _lbl_m.group(1).strip() if _lbl_m else ""
        return {
            "delegate_to": "business_agent", "intent": "wallet_add_address",
            "target": _waddr2, "label": _waddr2_label, "tier": "MID",
            "reasoning_summary": "Add address to watchlist (MID — requires confirmation)",
        }

    _wallet_update_kw = (
        "update watchlist", "rename watchlist", "relabel watchlist",
        "update the watchlist", "rename the watchlist", "relabel the watchlist",
        "update watchlist address", "rename watchlist address", "relabel watchlist address",
        "change the label", "change label", "update the label", "rename the address",
    )
    if any(w in u for w in _wallet_update_kw):
        import re as _re_waddr_upd
        _wupd_addr_m = _re_waddr_upd.search(
            r'\b(0x[0-9a-fA-F]{40}|bc1[a-z0-9]{25,90}|[13][a-zA-Z0-9]{25,34})\b', u,
        )
        _wupd_addr = _wupd_addr_m.group(1) if _wupd_addr_m else ""
        # Extract new label from "to '[label]'" or "to [label]"
        _wupd_lbl_m = _re_waddr_upd.search(r"\bto\s+['\"]?([A-Za-z0-9][A-Za-z0-9 \-_/]+?)['\"]?\s*$", u)
        _wupd_label = _wupd_lbl_m.group(1).strip() if _wupd_lbl_m else ""
        return {
            "delegate_to": "business_agent", "intent": "wallet_update_address",
            "target": _wupd_addr, "label": _wupd_label, "tier": "MID",
            "reasoning_summary": "Update watchlist address label (MID — requires confirmation)",
        }

    _wallet_list_kw = (
        "what addresses are being watched", "watched addresses", "watch list", "watchlist",
        "which addresses", "show watchlist", "list watched", "what wallets are watched",
        "watching addresses", "show watched addresses", "wallet watchlist",
    )
    if any(w in u for w in _wallet_list_kw):
        return {
            "delegate_to": "business_agent", "intent": "wallet_list_addresses",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Wallet watchlist query",
        }

    _wallet_portfolio_kw = (
        "portfolio", "portfolio value", "how much", "total value", "current value",
        "wallet balance", "total balance", "how much is my", "what's my portfolio",
        "whats my portfolio", "portfolio worth",
    )
    if any(w in u for w in _wallet_portfolio_kw) and any(c in u for c in ("eth", "btc", "crypto", "wallet", "portfolio", "nzd", "usd")):
        return {
            "delegate_to": "business_agent", "intent": "wallet_portfolio",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Wallet portfolio value query",
        }

    _wallet_check_kw = (
        "check address", "check wallet", "has .+ paid", "check balance of",
        "check this address",
    )
    if any(w in u for w in _wallet_check_kw):
        import re as _re_waddr3
        _waddr3_m = _re_waddr3.search(r'\b(0x[0-9a-fA-F]{40}|bc1[a-z0-9]{25,90}|[13][a-zA-Z0-9]{25,34})\b', u)
        _waddr3 = _waddr3_m.group(1) if _waddr3_m else u
        return {
            "delegate_to": "business_agent", "intent": "wallet_check_address",
            "target": _waddr3, "tier": "LOW",
            "reasoning_summary": "On-demand wallet address check",
        }

    _mkdir_kw = (
        "create a folder", "create folder", "make a folder", "make folder",
        "new folder", "mkdir", "create a directory", "make a directory",
    )
    if any(w in u for w in _mkdir_kw) and "nextcloud" not in u and "downloads" not in u:
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
    if any(w in u for w in _read_kw) and "nextcloud" not in u and "downloads" not in u:
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

    # ── Notes fast-path (Nextcloud Notes REST API) ──
    _notes_delete_kw = ("delete note", "delete the note", "remove note", "remove the note")
    _notes_update_kw = ("update note", "edit note", "change note", "modify note", "update the note")
    _notes_create_kw = ("create note", "new note", "add note", "write a note", "add a note", "create a note")
    _notes_read_kw   = ("read note", "open note", "show note", "get note", "view note")
    _notes_list_kw   = (
        "list notes", "show notes", "my notes", "all notes", "notes list",
        "view notes", "nextcloud notes", "nc notes",
        "the notes", "list the notes", "show the notes", "notes from nextcloud",
        "get notes", "get the notes",
    )
    if any(w in u for w in _notes_delete_kw):
        import re as _re_nid_del
        _nid_del_m = _re_nid_del.search(r'\b(\d+)\b', user_input)
        return {
            "delegate_to": "business_agent", "intent": "delete_note",
            "target": _nid_del_m.group(1) if _nid_del_m else None, "tier": "HIGH",
            "reasoning_summary": "Notes delete — deterministic pre-classifier",
        }
    if any(w in u for w in _notes_update_kw):
        import re as _re_nid_u
        _nid_um = _re_nid_u.search(r'\b(\d+)\b', user_input)
        return {
            "delegate_to": "business_agent", "intent": "update_note",
            "target": _nid_um.group(1) if _nid_um else None, "tier": "MID",
            "reasoning_summary": "Notes update — deterministic pre-classifier",
        }
    if any(w in u for w in _notes_create_kw):
        return {
            "delegate_to": "business_agent", "intent": "create_note",
            "target": None, "tier": "MID",
            "reasoning_summary": "Notes create — deterministic pre-classifier",
        }
    if any(w in u for w in _notes_read_kw):
        import re as _re_nid
        _nid_m = _re_nid.search(r'\b(\d+)\b', user_input)
        return {
            "delegate_to": "business_agent", "intent": "read_note",
            "target": _nid_m.group(1) if _nid_m else None, "tier": "LOW",
            "reasoning_summary": "Notes read — deterministic pre-classifier",
        }
    if any(w in u for w in _notes_list_kw):
        return {
            "delegate_to": "business_agent", "intent": "list_notes",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Notes list — deterministic pre-classifier",
        }
    # "read/delete/update the [title] note" — title sits between verb and "note";
    # _note_suffix was computed above near the conversational guard.
    if _note_suffix:
        # Extract the note title between the action verb and the trailing "note".
        # e.g. "read the NextCloud API note" → title="NextCloud API"
        # e.g. "delete the 'CC has the gay' note" → title="CC has the gay"
        import re as _re_ntitle
        _nt_m = _re_ntitle.search(
            r'\b(?:read|show|open|view|fetch|get|delete|remove|trash|update|edit|change|rename|modify)\s+(?:the\s+)?[\'"]?(.+?)[\'"]?\s+note\s*$',
            user_input, _re_ntitle.IGNORECASE
        )
        _extracted_title = _nt_m.group(1).strip() if _nt_m else None
        _note_suffix_delete = ("delete ", "remove ", "trash ")
        _note_suffix_update = ("update ", "edit ", "change ", "rename ", "modify ")
        _note_suffix_read   = ("read ", "show ", "open ", "view ", "fetch ", "get ")
        if any(v in u for v in _note_suffix_delete):
            return {
                "delegate_to": "business_agent", "intent": "delete_note",
                "target": _extracted_title, "tier": "HIGH",
                "reasoning_summary": "Notes delete by title — deterministic suffix classifier",
            }
        if any(v in u for v in _note_suffix_update):
            return {
                "delegate_to": "business_agent", "intent": "update_note",
                "target": _extracted_title, "tier": "MID",
                "reasoning_summary": "Notes update by title — deterministic suffix classifier",
            }
        if any(v in u for v in _note_suffix_read):
            return {
                "delegate_to": "business_agent", "intent": "read_note",
                "target": _extracted_title, "tier": "LOW",
                "reasoning_summary": "Notes read by title — deterministic suffix classifier",
            }

    # ── sovereign-nextcloud-fs ──────────────────────────────────────────
    _ncfs_list_kw   = (
        "list nextcloud", "show nextcloud", "what's on nextcloud", "whats on nextcloud",
        "list the downloads", "show the downloads", "what's in downloads", "whats in downloads",
        "what is in downloads", "what's in the downloads", "whats in the downloads",
        "in the downloads folder", "in downloads folder", "downloads folder",
        "list my nextcloud", "show me nextcloud", "browse nextcloud",
        "what files are on nextcloud", "what's in my nextcloud",
    )
    _ncfs_list_recursive_kw = (
        "full tree", "entire nextcloud", "all files on nextcloud",
        "everything on nextcloud", "recursive nextcloud", "nextcloud tree",
    )
    _ncfs_read_kw   = (
        "read from nextcloud", "read the file from nextcloud", "show me the file on nextcloud",
        "get the file from nextcloud", "open the file on nextcloud", "fetch from nextcloud",
        "read nextcloud file", "read the nextcloud file",
        "from downloads", "from the downloads",
    )
    _ncfs_mkdir_kw  = (
        "create folder on nextcloud", "create a folder on nextcloud",
        "make a folder on nextcloud", "make folder on nextcloud",
        "new folder on nextcloud", "mkdir on nextcloud",
    )
    _ncfs_delete_kw = (
        "delete from nextcloud", "delete the file on nextcloud", "delete the nextcloud file",
        "remove from nextcloud", "remove the file on nextcloud",
        "delete /", "remove /",  # path-style: "delete /path"
    )
    _ncfs_move_kw   = ("move the file", "move file", "rename file", "rename the file",
                        "move it to", "move this to", "move to")
    _ncfs_copy_kw   = ("copy the file", "copy file", "copy it to", "copy this to", "duplicate file",
                        "copy /", "copy the /")  # path-style: "copy /src to /dest"
    _ncfs_tag_kw    = ("tag the file", "tag this file", "tag it", "add tag", "label the file",
                        "tag /", "untag /")  # path-style: "tag /path with label"
    _ncfs_search_kw = ("search nextcloud for", "search files for", "find files named",
                        "find file named", "search for files")

    if any(w in u for w in _ncfs_list_recursive_kw):
        return {
            "delegate_to": "research_agent", "intent": "ncfs_list_recursive",
            "target": "/",
            "reasoning_summary": "Full Nextcloud tree — ncfs_list_recursive",
        }

    if any(w in u for w in _ncfs_list_kw):
        # Extract path hint — "downloads" folder or explicit /path
        _ncfs_path = "/downloads" if "download" in u else "/"
        import re as _re_ncfs
        _slash_m = _re_ncfs.search(r'(/[\w/_\-\.]+)', u)
        if _slash_m:
            _ncfs_path = _slash_m.group(1)
        return {
            "delegate_to": "research_agent", "intent": "ncfs_list",
            "target": _ncfs_path,
            "reasoning_summary": "Nextcloud directory listing — ncfs_list",
        }

    if any(w in u for w in _ncfs_read_kw):
        # Try to extract a path from the input
        import re as _re_ncfs_rd
        _rd_slash = _re_ncfs_rd.search(r'(/[\w/_\-\.]+)', u)
        _rd_fname = _re_ncfs_rd.search(r'(?:file\s+|read\s+)([\w\-\.]+\.\w+)', u, _re_ncfs_rd.IGNORECASE)
        if _rd_slash:
            _ncfs_rd_path = _rd_slash.group(1)
        elif _rd_fname and "download" in u:
            _ncfs_rd_path = "/downloads/" + _rd_fname.group(1)
        else:
            _ncfs_rd_path = u  # let specialist resolve
        return {
            "delegate_to": "research_agent", "intent": "ncfs_read",
            "target": _ncfs_rd_path,
            "reasoning_summary": "Read file from Nextcloud — ncfs_read",
        }

    if any(w in u for w in _ncfs_mkdir_kw):
        import re as _re_ncfs_mk
        _mk_slash = _re_ncfs_mk.search(r'(/[\w/_\-\.]+)', u)
        _mk_name  = _re_ncfs_mk.search(r'(?:called?|named?)\s+["\']?(\S+)["\']?', u, _re_ncfs_mk.IGNORECASE)
        _mk_path  = (_mk_slash.group(1) if _mk_slash else
                     ("/" + _mk_name.group(1).strip('/') if _mk_name else u))
        return {
            "delegate_to": "business_agent", "intent": "ncfs_mkdir",
            "target": _mk_path,
            "reasoning_summary": "Create Nextcloud folder — ncfs_mkdir",
        }

    if any(w in u for w in _ncfs_delete_kw):
        import re as _re_del_nc
        _del_slash = _re_del_nc.search(r'(/[\w/_\-\.]+)', u)
        _del_nc_path = _del_slash.group(1) if _del_slash else u
        return {
            "delegate_to": "business_agent", "intent": "ncfs_delete",
            "target": _del_nc_path,
            "reasoning_summary": "Delete from Nextcloud — ncfs_delete",
        }

    if any(w in u for w in _ncfs_move_kw):
        return {
            "delegate_to": "business_agent", "intent": "ncfs_move",
            "target": u,
            "reasoning_summary": "File move/rename — ncfs_move",
        }

    if any(w in u for w in _ncfs_copy_kw):
        return {
            "delegate_to": "business_agent", "intent": "ncfs_copy",
            "target": u,
            "reasoning_summary": "File copy — ncfs_copy",
        }

    if any(w in u for w in _ncfs_tag_kw):
        # Detect untag: "untag /" or "remove tag" patterns
        _is_untag = "untag /" in u or "remove tag" in u or "untag" in u.split()
        import re as _re_tag
        _tag_slash = _re_tag.search(r'(/[\w/_\-\.]+)', u)
        _tag_name_m = _re_tag.search(r'(?:with|label)\s+["\']?(\S+)["\']?', u, _re_tag.IGNORECASE)
        _tag_path = _tag_slash.group(1) if _tag_slash else u
        _tag_name = _tag_name_m.group(1) if _tag_name_m else ""
        return {
            "delegate_to": "business_agent",
            "intent": "ncfs_untag" if _is_untag else "ncfs_tag",
            "target": _tag_path,
            "tag": _tag_name,
            "reasoning_summary": "File untag — ncfs_untag" if _is_untag else "File tag — ncfs_tag",
        }

    if any(w in u for w in _ncfs_search_kw):
        return {
            "delegate_to": "business_agent", "intent": "ncfs_search",
            "target": u,
            "reasoning_summary": "File search — ncfs_search",
        }

    # ── sovereign-nextcloud-ingest ──────────────────────────────────────
    # ingest_status must be checked BEFORE ingest_kw — "ingest" substring fires on "ingest status"
    _ingest_status_kw = ("ingest status", "has this been reviewed", "check if reviewed",
                          "sovereign-reviewed", "check tags")
    if any(w in u for w in _ingest_status_kw):
        return {
            "delegate_to": "memory_agent", "intent": "ingest_status",
            "target": u,
            "reasoning_summary": "Ingest status check — OCS tags",
        }

    _ingest_kw = (
        "ingest", "review and remember", "add to memory", "remember the contents",
        "remember everything in", "ingest and remember", "read and remember",
        "review the file and remember", "review the folder and remember",
    )
    if any(w in u for w in _ingest_kw):
        _ingest_is_folder = any(w in u for w in ("folder", "directory", "everything in", "all files"))
        return {
            "delegate_to": "memory_agent",
            "intent": "ingest_folder" if _ingest_is_folder else "ingest_file",
            "target": u,
            "reasoning_summary": "Nextcloud ingest — ncingest pipeline",
        }

    # ── Session wrap-up ────────────────────────────────────────────────────
    _closure_kw = (
        "that's all", "thats all", "goodbye", "good bye", "wrap up", "wrap-up",
        "signing off", "sign off", "end of session", "we're done", "done for today",
        "thanks bye", "catch you later", "until tomorrow", "close session",
        "session wrap", "closing out", "close out",
    )
    if any(w in u for w in _closure_kw):
        return {
            "delegate_to": "research_agent",
            "intent": "session_wrap_up",
            "target": u,
            "reasoning_summary": "Session closure — wrap-up synthesis",
        }

    # ── Memory curation ────────────────────────────────────────────────────
    _memory_curate_kw = (
        "curate memory", "curate working memory", "review working memory",
        "memory curation", "promote memory", "memory review", "review memories",
        "clean up memory", "cleanup memory",
    )
    if any(w in u for w in _memory_curate_kw):
        return {
            "delegate_to": "memory_agent",
            "intent": "memory_curate",
            "target": u,
            "reasoning_summary": "Memory curation — promotion evaluation",
        }

    # ── Memory synthesis ────────────────────────────────────────────────────
    _memory_synth_kw = (
        "synthesise memory", "synthesize memory", "memory synthesis",
        "run synthesis", "memory synthesise", "memory synthesize",
    )
    if any(w in u for w in _memory_synth_kw):
        return {
            "delegate_to": "memory_agent",
            "intent": "memory_synthesise",
            "target": u,
            "reasoning_summary": "Memory synthesis — associative/relational pattern discovery",
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
        import re as _re_ct
        _ct_uuid = _re_ct.search(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            user_input, _re_ct.IGNORECASE
        )
        return {
            "delegate_to": "devops_agent", "intent": "cancel_task",
            "target": _ct_uuid.group(0) if _ct_uuid else None, "tier": "LOW",
            "reasoning_summary": "Task cancellation — deterministic pre-classifier",
        }

    _pause_task_kw = ("pause task", "pause the task", "suspend task", "hold task")
    if any(w in u for w in _pause_task_kw):
        import re as _re_pt
        _pt_uuid = _re_pt.search(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            user_input, _re_pt.IGNORECASE
        )
        return {
            "delegate_to": "devops_agent", "intent": "pause_task",
            "target": _pt_uuid.group(0) if _pt_uuid else None, "tier": "LOW",
            "reasoning_summary": "Task pause — deterministic pre-classifier",
        }

    # ── Self-improvement harness — proposals / baseline views ─────────────
    # Entry point is /selfimprove (Telegram command) — no NL entry keywords.
    # Mid-session views are retained so Director can query status after /selfimprove runs.
    _si_proposals_kw = (
        "improvement proposals", "pending proposals", "list proposals",
        "show proposals", "what proposals", "si proposals",
    )
    if any(w in u for w in _si_proposals_kw):
        return {
            "delegate_to": "devops_agent", "intent": "self_improve_proposals",
            "target": None, "tier": "LOW",
            "reasoning_summary": "List improvement proposals — deterministic pre-classifier",
        }

    _si_baseline_kw = (
        "show baseline", "baseline report", "si baseline",
    )
    if any(w in u for w in _si_baseline_kw):
        return {
            "delegate_to": "devops_agent", "intent": "self_improve_baseline",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Self-improvement baseline report — deterministic pre-classifier",
        }

    # ── Dev-Harness fast-paths ─────────────────────────────────────────────
    # All dev_* intents are deterministic — bypass CEO LLM + PASS 3.
    # "approve/reject/verify dev fix {id}" carry an 8-char hex session_id_short
    # extracted via regex; harness.py validates the session match.
    import re as _re_dh
    _dev_id_m = _re_dh.search(r'\b([0-9a-f]{8})\b', u)
    _dev_id   = _dev_id_m.group(1) if _dev_id_m else None

    # Entry point is /devcheck (Telegram command) — NL kept only for unambiguous dev-prefixed phrases.
    _dev_analyse_kw = (
        "dev analyse", "dev analyze", "dev analysis", "run dev analysis",
        "dev harness analyse", "dev harness analyze", "dev harness run",
        "run dev harness", "run the dev harness", "start dev harness",
    )
    if any(w in u for w in _dev_analyse_kw):
        return {
            "delegate_to": "devops_agent", "intent": "dev_analyse",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Dev-Harness Phase 1 trigger — deterministic pre-classifier",
        }

    _dev_status_kw = (
        "dev status", "dev harness status", "dev session", "harness status",
        "dev analysis status", "current dev session",
    )
    if any(w in u for w in _dev_status_kw):
        return {
            "delegate_to": "devops_agent", "intent": "dev_status",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Dev-Harness status — deterministic pre-classifier",
        }

    _dev_approve_kw = (
        "approve dev fix", "approve dev", "approve the dev fix",
        "approve harness fix", "confirm dev fix",
    )
    if any(w in u for w in _dev_approve_kw):
        return {
            "delegate_to": "devops_agent", "intent": "dev_approve",
            "target": _dev_id, "tier": "MID",
            "reasoning_summary": "Dev-Harness approve (Phase 4) — deterministic pre-classifier",
        }

    _dev_reject_kw = (
        "reject dev fix", "reject dev", "reject the dev fix",
        "reject harness fix", "cancel dev fix",
    )
    if any(w in u for w in _dev_reject_kw):
        return {
            "delegate_to": "devops_agent", "intent": "dev_reject",
            "target": _dev_id, "tier": "LOW",
            "reasoning_summary": "Dev-Harness reject — deterministic pre-classifier",
        }

    _dev_verify_kw = (
        "verify dev fix", "verify dev", "verify the dev fix",
        "verify harness fix", "recheck dev fix", "re-run dev analysis",
    )
    if any(w in u for w in _dev_verify_kw):
        return {
            "delegate_to": "devops_agent", "intent": "dev_verify",
            "target": _dev_id, "tier": "LOW",
            "reasoning_summary": "Dev-Harness verify (Phase 1 re-run) — deterministic pre-classifier",
        }

    _dev_clear_kw = (
        "dev clear", "clear dev", "clear dev harness", "clear dev session",
        "dev harness clear", "reset dev harness",
    )
    if any(w in u for w in _dev_clear_kw):
        return {
            "delegate_to": "devops_agent", "intent": "dev_clear",
            "target": None, "tier": "LOW",
            "reasoning_summary": "Dev-Harness clear — deterministic pre-classifier",
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


        self.github = GitHubAdapter()
        self.nanobot = NanobotAdapter(ledger=ledger)
        from execution.adapters.wallet import WalletControlAdapter
        self.wallet_control = WalletControlAdapter(ledger=ledger)
        # Skill lifecycle manager — instantiated lazily to avoid circular imports
        self._skill_lifecycle = None
        # Dev-Harness — instantiated lazily; broker + qdrant injected at construction time
        self._dev_harness = None
        # Task scheduler — injected post-init from main.py to avoid circular imports
        self.task_scheduler = None
        # FastAPI app.state — injected post-init so collect_all() can include soul_checksum
        self.app_state = None
        # MIP session tracking — True once memory_list_keys has been dispatched this boot.
        # Used to flag retrieve_key calls that skipped the mandatory list-first step.
        self._mip_listed_this_session = False

    # ── Universal item index (working_memory, zero-vector, session-scoped) ────
    #
    # Any Nextcloud item Rex retrieves (note, event, task, file, email) is written
    # here as a zero-vector point — no embed call, near-zero cost.  Lookups use
    # payload filter scroll, not vector search.
    #
    # item_type values: "note" | "event" | "task" | "file" | "email"

    _ITEM_INDEX_FLAG = "_item_index"
    _ZERO_VECTOR = [0.0] * 768

    async def _index_items(self, items: list, item_type: str) -> None:
        """Write a batch of retrieved items into the working_memory item index.

        Point IDs are QdrantAdapter.sovereign_id(item_type, item_id) — deterministic UUID5
        so the same item always maps to the same Qdrant point across all stores.
        The sov_id is also stored in the payload so it travels with the record.
        """
        if not items or not self.qdrant:
            return
        from qdrant_client.models import PointStruct as _PS
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc).isoformat()
        points = []
        for item in items:
            title = (item.get("title") or item.get("subject") or item.get("name") or "").strip()
            item_id = item.get("id") or item.get("uid") or item.get("item_id")
            if not title or item_id is None:
                continue
            _point_id = QdrantAdapter.sovereign_id(item_type, str(item_id))
            payload = {
                self._ITEM_INDEX_FLAG: True,
                "item_type": item_type,
                "title":     title,
                "title_lc":  title.lower(),
                "item_id":   str(item_id),
                "sov_id":    _point_id,
                "timestamp": _now,
            }
            for f in ("date", "calendar", "account", "from_addr", "sender"):
                if item.get(f):
                    payload[f] = item[f]
            points.append(_PS(id=_point_id, vector=self._ZERO_VECTOR, payload=payload))
        if points:
            try:
                await self.qdrant.client.upsert(collection_name="working_memory", points=points)
                logger.debug(
                    "item_index: upserted %d %s entries — sov_ids: %s",
                    len(points), item_type,
                    ", ".join(p.id for p in points[:5]) + ("…" if len(points) > 5 else ""),
                )
            except Exception as e:
                logger.warning("_index_items(%s): %s", item_type, e)

    async def _lookup_item(self, title: str, item_type: str) -> list:
        """Look up indexed items by title in working_memory.

        Returns list of matching payload dicts.  Empty list = nothing indexed yet.
        Match order: exact → title contains search → search contains title.
        """
        if not title or not self.qdrant:
            return []
        try:
            from qdrant_client.models import Filter as _F, FieldCondition as _FC, MatchValue as _MV
            points, _ = await self.qdrant.client.scroll(
                collection_name="working_memory",
                scroll_filter=_F(must=[
                    _FC(key=self._ITEM_INDEX_FLAG, match=_MV(value=True)),
                    _FC(key="item_type",           match=_MV(value=item_type)),
                ]),
                limit=500,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return []
        q = title.strip().lower()
        exact    = [p.payload for p in points if p.payload.get("title_lc") == q]
        if exact:
            return exact
        contains = [p.payload for p in points if q in (p.payload.get("title_lc") or "")]
        if contains:
            return contains
        return   [p.payload for p in points if (p.payload.get("title_lc") or "") in q and p.payload.get("title_lc")]

    async def _clear_item_index(self, item_type: str) -> None:
        """Remove all working_memory item index entries of a given type (call after create/delete)."""
        if not self.qdrant:
            return
        try:
            from qdrant_client.models import (Filter as _F, FieldCondition as _FC,
                                               MatchValue as _MV, FilterSelector as _FS)
            await self.qdrant.client.delete(
                collection_name="working_memory",
                points_selector=_FS(filter=_F(must=[
                    _FC(key=self._ITEM_INDEX_FLAG, match=_MV(value=True)),
                    _FC(key="item_type",           match=_MV(value=item_type)),
                ])),
            )
        except Exception as e:
            logger.warning("_clear_item_index(%s): %s", item_type, e)

    # ── Notes index helpers (built on universal item index) ───────────────

    async def _notes_get_or_build_index(self, force: bool = False) -> None:
        """Ensure the note index is populated in working_memory.

        If working_memory already has note entries and force=False, no-op.
        Otherwise fetches notes_list from nanobot and indexes them.
        """
        if not force:
            # Quick presence check — if any note entry exists, index is warm
            try:
                from qdrant_client.models import Filter as _F, FieldCondition as _FC, MatchValue as _MV
                pts, _ = await self.qdrant.client.scroll(
                    collection_name="working_memory",
                    scroll_filter=_F(must=[
                        _FC(key=self._ITEM_INDEX_FLAG, match=_MV(value=True)),
                        _FC(key="item_type",           match=_MV(value="note")),
                    ]),
                    limit=1, with_payload=False, with_vectors=False,
                )
                if pts:
                    return
            except Exception:
                pass
        try:
            nb = await self.nanobot.run("openclaw-nextcloud", "notes_list", {})
            notes_raw = (nb.get("result") or {}).get("notes") or nb.get("notes") or []
            await self._index_items(notes_raw, "note")
        except Exception:
            pass

    async def _notes_find_by_title(self, search_title: str) -> tuple:
        """Resolve a note title to an integer ID via the working_memory item index.

        Returns (id, None) on success or (None, error_message) on failure.
        """
        matches = await self._lookup_item(search_title, "note")
        if len(matches) == 1:
            return int(matches[0]["item_id"]), None
        if len(matches) > 1:
            titles = ", ".join(f"'{m['title']}'" for m in matches[:5])
            return None, f"Multiple notes match '{search_title}': {titles} — please be more specific"
        # Nothing in index — surface available titles
        try:
            from qdrant_client.models import Filter as _F, FieldCondition as _FC, MatchValue as _MV
            all_pts, _ = await self.qdrant.client.scroll(
                collection_name="working_memory",
                scroll_filter=_F(must=[
                    _FC(key=self._ITEM_INDEX_FLAG, match=_MV(value=True)),
                    _FC(key="item_type",           match=_MV(value="note")),
                ]),
                limit=8, with_payload=True, with_vectors=False,
            )
            available = ", ".join("'" + p.payload.get("title", "") + "'" for p in all_pts)
        except Exception:
            available = "unknown"
        return None, f"Note '{search_title}' not found. Available: {available}"

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

    # ── Telegram attachment upload (/attachment endpoint) ─────────────────
    async def handle_attachment(self, filename: str, content_b64: str,
                                mime_type: str, size: int, source: str) -> dict:
        """Download a Telegram attachment and upload to Nextcloud via nanobot-01.

        LOW tier — no confirmation required. Audit-logged.
        Destination: /downloads/{filename} (fixed folder, not user-configurable).
        Binary data is forwarded as multipart to nanobot's /upload endpoint to
        avoid Linux ARG_MAX limits in _dispatch_python3_exec CLI args.
        """
        import re as _re
        import base64

        _MAX_SIZE = 25 * 1024 * 1024  # 25 MB

        safe_name = _re.sub(r'[/\\<>:"|?*\x00-\x1f]', "_", filename)[:255] or "unknown"

        if size > _MAX_SIZE:
            return {"error": f"File too large: {size} bytes (max {_MAX_SIZE})", "status": "error"}

        try:
            content_bytes = base64.b64decode(content_b64)
        except Exception as e:
            return {"error": f"base64 decode failed: {e}", "status": "error"}

        if self.ledger:
            self.ledger.append("telegram_upload", "inbound", {
                "filename":  safe_name,
                "size":      size,
                "mime_type": mime_type,
                "source":    source,
            })

        result = await self.nanobot.run_upload(safe_name, content_bytes, mime_type, size)

        if result.get("status") != "ok" or result.get("error"):
            err = result.get("error") or f"upload failed"
            return {"error": err, "status": "error"}

        path = result.get("path", f"/downloads/{safe_name}")
        size_kb = round(size / 1024, 1)
        director_message = f"Uploaded {safe_name} → {path} ({size_kb} KB)"
        return {
            "status":           "ok",
            "path":             path,
            "filename":         safe_name,
            "size":             size,
            "director_message": director_message,
        }

    # ── Natural language chat (/chat endpoint) ───────────────────────────
    async def handle_chat(self, user_input: str, pending_delegation: dict = None,
                          confirmed: bool = False,
                          confidence_acknowledged: bool = False,
                          security_confirmed: bool = False,
                          context_window=None,
                          harness_cmd: str = None) -> dict:
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
                health_brief = await self.cog.translator_pass(
                    self._build_result_for_translator("health_status", health_result),
                    tier="LOW",
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

        # ── Harness command fast-path (/install, /skills, /selfimprove, etc.) ───
        # Explicit /commands bypass all NL routing — deterministic entry point.
        # On confirmed continuation, recover _harness_cmd from pending_delegation.
        _harness_cmd = harness_cmd or (pending_delegation or {}).get("_harness_cmd")
        if _harness_cmd == "install":
            return await self._run_install_harness(
                goal=user_input,
                confirmed=confirmed,
                delegation=pending_delegation or {},
                context_window=context_window,
            )
        if _harness_cmd == "skills":
            return await self._run_skills_browse(goal=user_input)
        if _harness_cmd == "selfimprove":
            return await self._run_selfimprove_harness()
        if _harness_cmd == "devcheck":
            return await self._run_devcheck_harness()
        if _harness_cmd == "portfolio":
            return await self._run_portfolio_harness()
        if _harness_cmd == "pm":
            return {"director_message": "PM harness not yet built. Pending Director approval of the PM-Harness proposal."}
        if _harness_cmd == "tax_report":
            return await self._run_tax_report_harness(
                user_input=user_input,
                confirmed=confirmed,
                delegation=pending_delegation or {},
            )

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
                delegation["_routing_source"] = "quick_classify"
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
            delegation["_routing_source"] = "fallback_map"

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

        # Deterministic email ID + account extraction for delete/move.
        # When no [id:XXXX] in user message, scan context_window for the matching email line.
        # Also infer account (business/personal) from recent user turns in context_window.
        # Runs before confirmation gate so values are baked into pending_delegation.
        if intent in ("delete_email", "move_email"):
            import re as _re_em
            _cw_turns = context_window if isinstance(context_window, list) else ([context_window] if context_window else [])
            _cw_text = "\n".join(t.get("assistant", "") + "\n" + t.get("user", "") for t in _cw_turns if isinstance(t, dict))

            # Extract database_id from context_window if not already in delegation
            if not delegation.get("database_id") and _cw_text and "[id:" in _cw_text:
                _stopwords_em = {"delete", "please", "email", "the", "this", "that",
                                 "move", "from", "to", "and", "for", "mail", "archive"}
                _kw_em = [w.lower() for w in _re_em.findall(r'[A-Za-z]{4,}', user_input)
                          if w.lower() not in _stopwords_em]
                _uid_re_em = _re_em.compile(r'\[id:(\d+)\]')
                for _cw_line in _cw_text.splitlines():
                    _id_m = _uid_re_em.search(_cw_line)
                    if _id_m and _kw_em and any(k in _cw_line.lower() for k in _kw_em):
                        delegation = {**delegation, "database_id": _id_m.group(1)}
                        break

            # Infer account from recent context_window user turns if not explicit in current message
            if not delegation.get("target") and _cw_turns:
                _acct_text = " ".join(t.get("user", "") for t in _cw_turns[-4:] if isinstance(t, dict)).lower()
                _acct_text += " " + user_input.lower()
                if "business" in _acct_text:
                    delegation = {**delegation, "target": "business"}
                elif "personal" in _acct_text:
                    delegation = {**delegation, "target": "personal"}

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
                    # _original_request preserved so specialist_outbound on confirmed
                    # continuation gets the real request ("delete X"), not "yes".
                    "pending_delegation": {**delegation, "intent": intent,
                                           "_original_request": user_input},
                    "summary": user_input,
                    "confidence": round(confidence, 3),
                    "gaps": gaps,
                }
            if rules.get("requires_confirmation"):
                return {
                    "requires_confirmation": True,
                    "pending_delegation": {**delegation, "intent": intent,
                                           "_original_request": user_input},
                    "summary": user_input,
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
        if action.get("domain") in ("ollama", "memory", "browser", "scheduler", "browser_config", "feeds", "memory_index", "wallet_watchlist", "wallet", "memory_synthesise"):
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
            # When the user confirmed a pending action (e.g. said "yes" to delete),
            # use the original request text — not "yes" — so the specialist has enough
            # context to build uid/from_addr/subject etc.
            _sp_user_input = delegation.get("_original_request") or user_input
            try:
                sp_out = await _asyncio.wait_for(
                    self.cog.specialist_outbound(agent, delegation, _sp_user_input,
                                                 context_window=context_window),
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
                    payload={"confirmed": confirmed},
                    security_confirmed=security_confirmed,
                ),
                timeout=_PASS_TIMEOUT * 6 if action.get("domain") == "skills" else _PASS_TIMEOUT * 3,  # skill search+review is slow; nanobot scripts are slow
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
                "success": True,   # Not a failure — a confirmation gate
                "outcome": "confirmation_required",
                "detail": {
                    "action": execution_result.get("action", intent),
                    "summary": execution_result.get("summary", ""),
                    "review_decision": (execution_result.get("review_result") or {}).get("decision", ""),
                    "escalated": bool(execution_result.get("escalation_notice")),
                    "please_confirm": "Reply yes to proceed or no to cancel.",
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

        # ── Deterministic working_memory write — every adapter result, success or failure ──
        # Awaited (not create_task) so it completes before any response reaches Telegram.
        # This is separate from PASS 4's LLM-decided memory writes; it fires unconditionally
        # so follow-up questions always have the raw result in context.
        if self.qdrant:
            await self._write_execution_episodic(intent, tier, execution_result, user_input)

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
        # When user has explicitly confirmed (esp. HIGH-tier double-confirm), the orchestrator
        # LLM cannot override that decision — confirmed=True IS the governance gate.
        # Prevents spurious PASS 4 rejections on email delete / HIGH-tier ops.
        if confirmed and orch_eval.get("approved") is False:
            orch_eval["approved"] = True
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
            "list_notes", "read_note", "create_note", "update_note", "delete_note",
            "fetch_email", "search_email", "fetch_message",
            "delete_email", "move_email", "send_email",
            "skill_audit", "skill_search",
            "skill_list_candidates", "skill_review_candidate", "skill_clear_harness",
            "configure_browser_auth",
            "self_improve_observe", "self_improve_proposals", "self_improve_baseline",
            "read_feed",   # rss-digest entries — pass raw list, no LLM summarisation
            "research",
            "memory_list_keys", "memory_retrieve_key",  # MIP — pass structured index directly
            "memory_recall",   # exact content search — found/not-found result passes directly
            # remember_fact intentionally excluded — raw execution_result leaks point_id/collection
            # to the translator; let PASS 4 → translator produce plain-English confirmation instead
            # Dev-Harness — all phases return structured dicts; bypass LLM summarisation
            "dev_analyse", "dev_status", "dev_approve", "dev_reject", "dev_verify", "dev_clear",
            # Tax harness — structured results + multi-turn prompts pass directly to Director
            "tax_status", "tax_ingest_run", "tax_ingest_store", "tax_list_events",
            "tax_year_summary", "tax_query", "tax_address_list", "tax_ingest_status",
            "tax_report_query", "tax_report_ingest", "tax_report_create",
            "tax_report_notify", "tax_report_clear", "tax_report_status",
            # Wallet — structured results, pass directly to translator
            "wallet_get_address", "wallet_read_config", "wallet_get_btc_xpub",
            "wallet_list_addresses", "wallet_portfolio", "wallet_check_address",
            "wallet_add_address", "wallet_remove_address", "wallet_update_address",
            # cognitive skills — specialist synthesis passes directly to translator
            "session_wrap_up", "memory_curate",
            # Memory synthesis — structured result passes directly to translator
            "memory_synthesise",
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

        elif intent == "skill_list_candidates" and execution_result is not None:
            # Harness: list candidates from WM checkpoint — strip skill_md, format for translator
            _h_cands = execution_result.get("candidates", [])
            if not _h_cands:
                _msg_lc = execution_result.get("message", "No candidates found.")
                _rft = {"success": False, "outcome": _msg_lc, "detail": {}, "error": _msg_lc, "next_action": None}
            else:
                _slim_lc = [
                    {k: v for k, v in c.items() if k not in ("skill_md", "raw_url")}
                    for c in _h_cands
                ]
                _rft = dict(_rft)
                _rft["success"] = True
                _rft["error"] = None
                _rft["detail"] = {
                    "candidates": _slim_lc,
                    "total": execution_result.get("total", len(_slim_lc)),
                    "instructions": execution_result.get("instructions", ""),
                }
                _rft["outcome"] = (
                    f"Found {len(_slim_lc)} candidate skill(s) in the current session. "
                    "Review a candidate with 'review candidate N'."
                )

        elif intent == "skill_review_candidate" and execution_result is not None:
            # Harness: review result — format verdict cleanly for translator
            _rv_result = execution_result.get("review_result", {})
            _rv_verdict = execution_result.get("verdict") or _rv_result.get("decision", "")
            _rv_slug = execution_result.get("slug", "")
            _rv_cid = execution_result.get("candidate_id", "")
            if execution_result.get("status") == "blocked":
                _rft = {
                    "success": False,
                    "outcome": execution_result.get("message", f"Candidate {_rv_cid} blocked."),
                    "detail": {"verdict": "block", "slug": _rv_slug,
                               "categories": execution_result.get("categories", [])},
                    "error": execution_result.get("message"),
                    "next_action": None,
                }
            elif execution_result.get("status") == "ok":
                _rft = dict(_rft)
                _rft["success"] = True
                _rft["error"] = None
                _rft["detail"] = {
                    "candidate_id": _rv_cid,
                    "slug": _rv_slug,
                    "verdict": _rv_verdict,
                    "risk_level": _rv_result.get("risk_level", ""),
                    "escalation_reasons": _rv_result.get("escalation_reasons", []),
                    "next_step": execution_result.get("next_step", ""),
                }
                _rft["outcome"] = (
                    f"Reviewed candidate {_rv_cid} ({_rv_slug}): verdict {_rv_verdict.upper()}."
                )

        elif intent == "wallet_add_address" and execution_result is not None:
            _wa_entry = execution_result.get("entry", {})
            _wa_meta  = _wa_entry.get("metadata", {})
            _wa_lbl   = _wa_entry.get("label", "")
            _wa_val   = _wa_entry.get("value", "")
            _wa_short = (_wa_val[:10] + "…") if len(_wa_val) > 10 else _wa_val
            _wa_chain = _wa_meta.get("chain", "eth")
            if execution_result.get("status") == "ok":
                _rft = {
                    "success": True,
                    "outcome": f"Added '{_wa_lbl}' ({_wa_short}, {_wa_chain}) to the watchlist.",
                    "detail": {"label": _wa_lbl, "address": _wa_val, "chain": _wa_chain},
                    "error": None, "next_action": None,
                }
            else:
                _rft = {
                    "success": False,
                    "outcome": "Failed to add address to watchlist.",
                    "detail": {}, "error": execution_result.get("error"), "next_action": None,
                }

        elif intent == "wallet_update_address" and execution_result is not None:
            _wu_entry = execution_result.get("entry", {})
            _wu_addr  = execution_result.get("address", "")
            _wu_lbl   = _wu_entry.get("label", "")
            _wu_short = (_wu_addr[:10] + "…") if len(_wu_addr) > 10 else _wu_addr
            if execution_result.get("status") == "ok":
                _rft = {
                    "success": True,
                    "outcome": f"Renamed {_wu_short} to '{_wu_lbl}'.",
                    "detail": {"address": _wu_addr, "label": _wu_lbl},
                    "error": None, "next_action": None,
                }
            else:
                _rft = {
                    "success": False,
                    "outcome": "Failed to update watchlist entry.",
                    "detail": {}, "error": execution_result.get("error"), "next_action": None,
                }

        elif intent == "wallet_remove_address" and execution_result is not None:
            _wr_addr   = (execution_result.get("target") or
                          execution_result.get("address", ""))
            _wr_removed = execution_result.get("removed", False)
            if execution_result.get("status") == "ok":
                _wr_short = (_wr_addr[:10] + "…") if len(_wr_addr) > 10 else _wr_addr
                _rft = {
                    "success": True,
                    "outcome": (f"Removed {_wr_short} from the watchlist."
                                if _wr_removed else "Address was not in the watchlist."),
                    "detail": {"address": _wr_addr, "removed": _wr_removed},
                    "error": None, "next_action": None,
                }
            else:
                _rft = {
                    "success": False,
                    "outcome": "Failed to remove address from watchlist.",
                    "detail": {}, "error": execution_result.get("error"), "next_action": None,
                }

        elif intent == "wallet_get_address" and execution_result is not None:
            _wga_addr = execution_result.get("address", "")
            if execution_result.get("status") == "ok" and _wga_addr:
                _rft = {
                    "success": True,
                    "outcome": f"Rex ETH address: {_wga_addr}",
                    "detail": {"address": _wga_addr,
                               "derivation_path": execution_result.get("derivation_path", "")},
                    "error": None, "next_action": None,
                }
            else:
                _rft = {
                    "success": False,
                    "outcome": "Wallet address not available.",
                    "detail": {}, "error": execution_result.get("error"), "next_action": None,
                }

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
                # Stamp success=True when we have real data and no error — prevents the
                # translator from misreading a missing "status" field as a failure.
                if not execution_result.get("error") and execution_result.get("status") != "error":
                    _rft["success"] = True
                    _rft["error"] = None

        # For mail write operations (delete/move/send), stamp a clear outcome so the translator
        # doesn't just describe the databaseId without context.  The nanobot strips "action"
        # (it's a wrapper field name) so the nc-mail "action: deleted" never arrives.
        if intent in ("delete_email", "move_email", "send_email") and _rft.get("success"):
            _rft = dict(_rft)
            _db_id = execution_result.get("databaseId", "")
            if intent == "delete_email":
                _rft["outcome"] = f"Email {_db_id} deleted." if _db_id else "Email deleted."
            elif intent == "move_email":
                _rft["outcome"] = f"Email {_db_id} moved." if _db_id else "Email moved."
            elif intent == "send_email":
                _rft["outcome"] = "Email sent."

        # Pre-format email message lists so the translator renders them correctly.
        # Without this the LLM renders each dict field as a separate bullet
        # (• From: ... • Subject: ... • Date: ... • Message-ID: ...) instead of
        # one line per message.  message_id is stripped — it's internal plumbing.
        if intent in ("fetch_email", "search_email", "list_inbox") and isinstance(_rft.get("detail"), dict):
            _msgs = _rft["detail"].get("messages")
            if isinstance(_msgs, list) and _msgs:
                import re as _re_date

                def _short_date(raw: str) -> str:
                    _m = _re_date.search(
                        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
                        raw or "",
                        _re_date.IGNORECASE,
                    )
                    return f"{_m.group(1)} {_m.group(2)}" if _m else (raw[:10] if raw else "")

                def _display_name(full_from: str) -> str:
                    """Extract 'Name' from 'Name <email@addr>'; return full string if no brackets."""
                    _mn = _re_date.match(r'^"?([^"<]+?)"?\s*<[^>]+>$', (full_from or "").strip())
                    return _mn.group(1).strip() if _mn else (full_from or "unknown")

                _lines = []
                _id_index: dict[str, str] = {}
                for _i, _em in enumerate(_msgs[:10], 1):
                    _sender  = _display_name(_em.get("from", _em.get("sender", "")))
                    _subject = _em.get("subject", "(no subject)")
                    _date    = _short_date(_em.get("date", ""))
                    _mid     = str(_em.get("databaseId", _em.get("uid", "")))
                    # Embed databaseId in the line so specialist_outbound can read it from context_window
                    _mid_tag = f" [id:{_mid}]" if _mid else ""
                    _lines.append(f"{_i}. {_sender} — {_subject} ({_date}){_mid_tag}")
                    if _mid:
                        _id_index[str(_i)] = _mid
                _rft = dict(_rft)
                _rft["detail"] = dict(_rft["detail"])
                _rft["detail"]["messages"] = "\n".join(_lines)
                _rft["detail"]["count"] = len(_lines)
                if _id_index:
                    _rft["detail"]["id_index"] = _id_index
                # Index emails in working_memory for cross-turn lookup
                _acct = action.get("account") or (delegation or {}).get("target") or "personal"
                _asyncio.create_task(self._index_items([
                    {"title": _em.get("subject", "(no subject)"),
                     "id": str(_em.get("databaseId", _em.get("uid", ""))),
                     "from_addr": _em.get("from", ""),
                     "date": _em.get("date", ""),
                     "account": _acct}
                    for _em in _msgs[:10] if _em.get("databaseId") or _em.get("uid")
                ], "email"))

        # Pre-format file listings deterministically so the translator cannot miscount.
        # Items is a list of dicts {name, type, size, last_modified} — convert to numbered string.
        if intent in ("list_files", "navigate", "list_files_recursive") and isinstance(_rft.get("detail"), dict):
            _items = _rft["detail"].get("items", [])
            if isinstance(_items, list):
                _fl_lines = []
                for _fi, _it in enumerate(_items, 1):
                    if isinstance(_it, dict):
                        _fn  = _it.get("name", "")
                        _ft  = _it.get("type", "")
                        _tag = " (folder)" if _ft in ("directory", "folder", "collection") else ""
                        _fl_lines.append(f"{_fi}. {_fn}{_tag}")
                    elif isinstance(_it, str):
                        _fl_lines.append(f"{_fi}. {_it}")
                _rft = dict(_rft)
                _rft["detail"] = dict(_rft["detail"])
                _rft["detail"]["items"] = "\n".join(_fl_lines) if _fl_lines else "(empty)"
                _rft["detail"]["count"] = len(_fl_lines)
                # Index files in working_memory
                _fp = action.get("path") or (specialist or {}).get("path") or "/"
                _asyncio.create_task(self._index_items([
                    {"title": _it.get("name", _it) if isinstance(_it, dict) else str(_it),
                     "id": _it.get("name", _it) if isinstance(_it, dict) else str(_it),
                     "date": _it.get("last_modified", "") if isinstance(_it, dict) else ""}
                    for _it in _items if _it
                ], "file"))

        # Pre-format notes list so translator renders a clean numbered list without
        # Unix timestamps (which trigger the fabrication guard when the LLM invents years).
        if intent in ("list_notes",) and isinstance(_rft.get("detail"), dict):
            _notes_raw = (_rft["detail"].get("result") or {}).get("notes")
            if isinstance(_notes_raw, list):
                _note_lines = []
                _note_id_index: dict[str, int] = {}
                for _ni, _note in enumerate(_notes_raw, 1):
                    _nt = _note.get("title", "(untitled)")
                    _nc = _note.get("category", "")
                    _nid = _note.get("id")
                    _cat_tag  = f" ({_nc})" if _nc else ""
                    _id_tag   = f" [id:{_nid}]" if _nid is not None else ""
                    _note_lines.append(f"{_ni}. {_nt}{_cat_tag}{_id_tag}")
                    if _nid is not None:
                        _note_id_index[str(_ni)] = _nid
                _rft = dict(_rft)
                _rft["detail"] = {
                    "notes": "\n".join(_note_lines) if _note_lines else "(no notes)",
                    "count": len(_note_lines),
                    "id_index": _note_id_index,
                }

        # ── Working-memory content_ref markers ───────────────────────────────
        # Rule: if a result has a stable ID → already indexed above as a full item.
        # If a result is a blob (no stable ID) → store a proper episodic entry so
        # shutdown_promote() can carry it to RAID. Marker only — not the blob itself.
        async def _store_content_ref(content: str, domain: str, extra: dict) -> None:
            try:
                await self.qdrant.store(
                    content=content,
                    metadata={"type": "episodic", "domain": domain,
                              "source": "content_ref", **extra},
                    collection="working_memory",
                    writer="sovereign-core",
                )
            except Exception as _e:
                logger.debug("content_ref store failed: %s", _e)

        if intent == "web_search":
            _ws_q     = (delegation or {}).get("target") or action.get("query", "web search")
            _ws_count = len(execution_result.get("results", [])) if isinstance(execution_result, dict) else 0
            asyncio.create_task(_store_content_ref(
                f"Web search: {_ws_q} ({_ws_count} results)",
                "research", {"query": _ws_q, "result_count": _ws_count}
            ))

        if intent in ("fetch_url", "browser_fetch"):
            _bu_url   = action.get("url") or (delegation or {}).get("target", "")
            _bu_title = (execution_result or {}).get("data", {}).get("title", "") if isinstance(execution_result, dict) else ""
            asyncio.create_task(_store_content_ref(
                f"Fetched URL: {_bu_title or _bu_url}",
                "research", {"url": _bu_url, "page_title": _bu_title}
            ))

        if intent == "read_file":
            _rf_path = action.get("path") or (specialist or {}).get("path") or (delegation or {}).get("target", "")
            asyncio.create_task(_store_content_ref(
                f"Read file: {_rf_path}",
                "filesystem", {"path": _rf_path}
            ))

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
                nanobot=self.nanobot,
                ledger=self.ledger,
                guardian=guardian,  # injected via set_guardian() after lifespan init
                qdrant=self.qdrant,
            )
        return self._skill_lifecycle

    def set_guardian(self, guardian) -> None:
        """Inject SoulGuardian into the lifecycle manager post-lifespan init."""
        if self._skill_lifecycle is not None:
            self._skill_lifecycle.guardian = guardian

    def _get_dev_harness(self):
        """Lazy init DevHarness — avoids import at module load time."""
        if self._dev_harness is None:
            from dev_harness.harness import DevHarness
            github_token = os.environ.get("GITHUB_TOKEN", "")
            self._dev_harness = DevHarness(
                broker=self.broker,
                qdrant=self.qdrant,
                github_token=github_token,
                cog=self.cog,  # required for DCL-gated Claude escalation in Phase 2
            )
        return self._dev_harness

    def set_task_scheduler(self, scheduler) -> None:
        """Inject TaskScheduler post-lifespan init."""
        self.task_scheduler = scheduler

    def set_credential_proxy(self, proxy) -> None:
        """Inject CredentialProxy post-lifespan init — passed to NanobotAdapter."""
        self.credential_proxy = proxy
        if self.nanobot:
            self.nanobot.set_credential_proxy(proxy)

    # ── Skill harness WM checkpoint helpers ─────────────────────────────────

    async def _skill_harness_load_checkpoint(self) -> dict | None:
        """Scroll working_memory for the harness checkpoint. Returns payload dict or None."""
        try:
            offset = None
            while True:
                result, next_offset = await self.qdrant.client.scroll(
                    collection_name=WORKING,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for r in result:
                    p = dict(r.payload or {})
                    if p.get("_skill_harness_checkpoint"):
                        return p
                if next_offset is None:
                    return None
                offset = next_offset
        except Exception as _e:
            logger.warning("_skill_harness_load_checkpoint failed: %s", _e)
            return None

    async def _skill_harness_save_checkpoint(self, checkpoint: dict) -> None:
        """Delete any existing harness checkpoint(s) then write a fresh one to working_memory."""
        try:
            # Collect and delete existing checkpoint points
            offset = None
            to_delete: list = []
            while True:
                result, next_offset = await self.qdrant.client.scroll(
                    collection_name=WORKING,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for r in result:
                    if (r.payload or {}).get("_skill_harness_checkpoint"):
                        to_delete.append(r.id)
                if next_offset is None:
                    break
                offset = next_offset
            if to_delete:
                await self.qdrant.client.delete(
                    collection_name=WORKING,
                    points_selector=to_delete,
                )
            # Embed and write new checkpoint
            await self.qdrant.store(
                content="skill_harness:checkpoint",
                metadata={**checkpoint, "_skill_harness_checkpoint": True, "type": "skill_harness_checkpoint"},
                collection=WORKING,
            )
        except Exception as _e:
            logger.warning("_skill_harness_save_checkpoint failed: %s", _e)

    # ── /install harness — autonomous skill acquisition ──────────────────────

    async def _install_select_best(self, goal: str, candidates: list) -> dict:
        """Select the best skill candidate for the stated goal.

        Deterministic first:
          - If exactly one certified candidate exists → return it directly (no LLM).
          - If all are certified or all uncertified → use LLM to pick best fit.
          - Fallback on LLM failure: first certified, else first.
        """
        import json as _json
        certified = [c for c in candidates if c.get("certified")]
        # Single certified match — deterministic, no LLM needed
        if len(certified) == 1:
            return certified[0]
        # Multiple candidates — use LLM to rank by goal fit; certified preferred
        pool = certified if certified else candidates
        slim = [
            {
                "index": i,
                "slug": c.get("slug", ""),
                "description": c.get("description") or c.get("summary", ""),
                "certified": c.get("certified", False),
            }
            for i, c in enumerate(pool)
        ]
        prompt = (
            f'The Director wants to: "{goal}"\n\n'
            f"Available skill candidates:\n{_json.dumps(slim, indent=2)}\n\n"
            "Select the BEST candidate that fulfils the goal. "
            'Return JSON only: {"selected_index": 0, "reasoning": "one sentence"}'
        )
        try:
            result = await self.cog.call_llm_json(prompt)
            idx = int(result.get("selected_index", 0))
            if 0 <= idx < len(pool):
                return pool[idx]
        except Exception as _e:
            logger.warning("_install_select_best LLM failed: %s — using fallback", _e)
        # Deterministic fallback: first certified, else first candidate
        return next((c for c in candidates if c.get("certified")), candidates[0])

    async def _skill_harness_clear_all(self) -> None:
        """Delete all harness checkpoint entries from working memory."""
        try:
            _offset, _to_del = None, []
            while True:
                _res, _nxt = await self.qdrant.client.scroll(
                    collection_name=WORKING, limit=100, offset=_offset,
                    with_payload=True, with_vectors=False,
                )
                for _r in _res:
                    if (_r.payload or {}).get("_skill_harness_checkpoint"):
                        _to_del.append(_r.id)
                if _nxt is None:
                    break
                _offset = _nxt
            if _to_del:
                await self.qdrant.client.delete(
                    collection_name=WORKING, points_selector=_to_del)
        except Exception as _e:
            logger.warning("_skill_harness_clear_all failed: %s", _e)

    async def _run_install_harness(
        self, goal: str, confirmed: bool, delegation: dict, context_window=None
    ) -> dict:
        """Autonomous /install harness.

        Phase A (fresh):  search → LLM selects best → checkpoint → confirm gate
        Phase B (confirmed): scanner → LLM review → install → clear
        """
        import uuid as _uuid, datetime as _dt, json as _json

        lifecycle = self._get_lifecycle()

        if not confirmed:
            # ── Phase A: search + select ────────────────────────────────────
            search_result = await lifecycle.search(
                query=goal, certified_only=False, limit=10
            )
            candidates = [c for c in search_result.get("candidates", []) if c.get("skill_md")]

            if not candidates:
                dm = await self.cog.translator_pass({
                    "success": False,
                    "outcome": (
                        f"No skills found for '{goal}'. "
                        "Try a more specific description or check GitHub directly."
                    ),
                    "detail": {}, "error": "no_candidates", "next_action": None,
                })
                return {"director_message": dm}

            # LLM selects best candidate for the stated goal
            selected = await self._install_select_best(goal, candidates)

            # Save checkpoint so Phase B can recover the candidate
            _ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            await self._skill_harness_save_checkpoint({
                "skill_name": "install-harness",
                "session_id": str(_uuid.uuid4()),
                "current_step": "awaiting_confirm",
                "step_results": {
                    "search": {
                        "query": goal,
                        "total_found": len(candidates),
                        "selected_slug": selected.get("slug"),
                        "ts": _ts,
                    }
                },
                "_selected_candidate": selected,
                "last_checkpoint_ts": _ts,
            })

            slug = selected.get("slug", "unknown")
            desc = selected.get("description") or selected.get("summary") or ""
            _certified = selected.get("certified")
            certified_label = (
                "OpenClaw registry (certified)" if _certified
                else "⚠ unverified GitHub source (not in OpenClaw registry)"
            )
            url = selected.get("github_url", "")

            # Include clawhub scan results if available
            _ch_scan = selected.get("clawhub_scan")
            _scan_lines = ""
            if _ch_scan:
                _llm = _ch_scan.get("llm") or {}
                _vt  = _ch_scan.get("vt") or {}
                if _llm.get("verdict"):
                    _scan_lines += f"\nOpenClaw scan: {_llm['verdict'].upper()}"
                    if _llm.get("summary"):
                        _scan_lines += f" — {_llm['summary'][:120]}"
                if _vt.get("verdict"):
                    _scan_lines += f"\nVirusTotal: {_vt['verdict'].upper()}"
            else:
                _scan_lines = "\nClawhub scan: not yet available"

            summary = (
                f"**{slug}**"
                + (f" — {desc}" if desc else "")
                + f"\nSource: {certified_label}"
                + (f"\n{url}" if url else "")
                + _scan_lines
                + f"\n\nInstall?"
            )

            return {
                "requires_confirmation": True,
                "summary": summary,
                "pending_delegation": {
                    "delegate_to": "devops_agent",
                    "intent": "skill_install",
                    "_harness_cmd": "install",
                    "_pending_install": selected,
                },
            }

        # ── Phase B: confirmed — scan → review → install ────────────────────
        selected = delegation.get("_pending_install")
        if not selected:
            cp = await self._skill_harness_load_checkpoint()
            selected = (cp or {}).get("_selected_candidate")

        if not selected or not selected.get("skill_md"):
            dm = await self.cog.translator_pass({
                "success": False,
                "outcome": "Install session expired — run /install again.",
                "detail": {}, "error": "no_session", "next_action": None,
            })
            return {"director_message": dm}

        slug = selected.get("slug", "unknown")
        skill_md = selected["skill_md"]

        # Clawhub external scan gate: block if VirusTotal or OpenClaw LLM flagged malicious.
        _ch = selected.get("clawhub_scan") or {}
        _ch_llm = _ch.get("llm") or {}
        _ch_vt  = _ch.get("vt") or {}
        _MALICIOUS = frozenset({"malicious", "suspicious", "dangerous"})
        _ch_block = []
        if _ch_llm.get("verdict", "").lower() in _MALICIOUS:
            _ch_block.append(f"OpenClaw: {_ch_llm['verdict']}")
        if _ch_vt.get("verdict", "").lower() in _MALICIOUS:
            _ch_block.append(f"VirusTotal: {_ch_vt['verdict']}")
        if _ch_block:
            await self._skill_harness_clear_all()
            dm = await self.cog.translator_pass({
                "success": False,
                "outcome": (
                    f"Clawhub scan blocked {slug}: {', '.join(_ch_block)}. "
                    "Installation cancelled."
                ),
                "detail": {"clawhub_scan": _ch},
                "error": "clawhub_scan_block", "next_action": None,
            })
            return {"director_message": dm}

        # Deterministic pre-scan: only unambiguous literal injection phrases hard-block.
        # The Director's "yes" at the confirm gate IS the security review for /install.
        # Regex patterns and sensitive-data keywords are too noisy on legitimate SDK docs
        # (e.g. a wallet dev kit that documents "ignore previous instructions" as an example
        # of what NOT to trust in user input would be blocked by any LLM reviewer given only
        # a 500-char content preview — it cannot distinguish documentation from attack).
        _INJECT_BLOCK = frozenset({
            "governance_bypass", "secret_exfiltration", "tool_escalation",
        })
        pre_scan = self.scanner.scan(skill_md)
        _hard_cats = [c for c in pre_scan.categories if c in _INJECT_BLOCK]
        if _hard_cats:
            await self._skill_harness_clear_all()
            dm = await self.cog.translator_pass({
                "success": False,
                "outcome": (
                    f"Scan blocked {slug}: {', '.join(_hard_cats)}. "
                    "Installation cancelled."
                ),
                "detail": {"categories": _hard_cats},
                "error": "scanner_block", "next_action": None,
            })
            return {"director_message": dm}

        # Director already confirmed — that is the security gate.
        # Pass an approved stub so lifecycle.load() records the decision correctly.
        _director_review = {
            "decision": "approve",
            "risk_level": "low",
            "escalate_to_director": False,
            "escalation_reasons": [],
            "scanner_categories": pre_scan.categories,
        }
        install_result = await lifecycle.load(
            name=slug,
            skill_md_content=skill_md,
            review_result=_director_review,
            confirmed=True,
            clawhub_slug=selected.get("slug"),
            clawhub_certified=bool(selected.get("certified", False)),
            proposed_by="devops_agent",
            reason=f"Director confirmed /install — goal: {goal}",
            raw_url=selected.get("raw_url"),
        )

        await self._skill_harness_clear_all()

        if install_result.get("status") == "installed":
            dm = await self.cog.translator_pass({
                "success": True,
                "outcome": f"Installed {slug}. Skill is ready to use.",
                "detail": install_result, "error": None, "next_action": None,
            })
        else:
            dm = await self.cog.translator_pass({
                "success": False,
                "outcome": (
                    f"Install failed for {slug}: "
                    f"{install_result.get('message', 'unknown error')}"
                ),
                "detail": install_result, "error": "install_failed", "next_action": None,
            })

        return {"director_message": dm}

    async def _run_skills_browse(self, goal: str) -> dict:
        """/skills <query> — search and list skills, no install."""
        lifecycle = self._get_lifecycle()
        search_result = await lifecycle.search(query=goal, certified_only=False, limit=10)
        candidates = search_result.get("candidates", [])
        if not candidates:
            dm = await self.cog.translator_pass({
                "success": False,
                "outcome": f"No skills found for '{goal}'.",
                "detail": {}, "error": "no_candidates", "next_action": None,
            })
            return {"director_message": dm}
        slim = [
            {k: v for k, v in c.items() if k not in ("skill_md", "raw_url")}
            for c in candidates
        ]
        dm = await self.cog.translator_pass({
            "success": True,
            "outcome": f"Found {len(slim)} skill(s) matching '{goal}'.",
            "detail": {"candidates": slim},
            "error": None, "next_action": "Use /install <goal> to install one.",
        })
        return {"director_message": dm}

    async def _run_selfimprove_harness(self) -> dict:
        """/selfimprove — run SI observe cycle then surface pending proposals."""
        from monitoring.self_improvement import run_manual_observe, list_pending_proposals
        _app_state = getattr(self, "app_state", None)
        # Observe: collect metrics, detect anomalies, generate proposals if needed
        observe_result = await run_manual_observe(self.qdrant, self.cog, self.ledger, _app_state)
        # Surface any pending proposals regardless of whether new ones were just created
        proposals_result = await list_pending_proposals(self.qdrant)
        proposals = proposals_result.get("proposals", [])
        _rft = {
            "success": True,
            "outcome": observe_result.get("summary", "Observe cycle complete."),
            "detail": {
                "observe": observe_result,
                "pending_proposals": proposals,
                "proposal_count": len(proposals),
            },
            "error": None,
            "next_action": (
                f"{len(proposals)} proposal(s) pending Director approval."
                if proposals else "No proposals pending."
            ),
        }
        dm = await self.cog.translator_pass(_rft)
        return {"director_message": dm}

    async def _run_devcheck_harness(self) -> dict:
        """/devcheck — run full dev harness analysis cycle."""
        dh = self._get_dev_harness()
        result = await dh.run_phase1(trigger="explicit")
        _rft = {
            "success": result.get("status") not in ("error",),
            "outcome": result.get("summary") or result.get("message") or "Dev analysis complete.",
            "detail": result,
            "error": result.get("error"),
            "next_action": result.get("next_action") or result.get("next_step"),
        }
        dm = await self.cog.translator_pass(_rft)
        return {"director_message": dm}

    async def _run_portfolio_harness(self) -> dict:
        """/portfolio — trigger snapshot and return current balances + value."""
        action = {"domain": "wallet_watchlist", "operation": "check", "name": "wallet_portfolio"}
        result = await self._dispatch_inner(action, delegation={}, payload={})
        _rft = {
            "success": result.get("status") not in ("error",),
            "outcome": "Portfolio snapshot complete.",
            "detail": result,
            "error": result.get("error"),
            "next_action": None,
        }
        dm = await self.cog.translator_pass(_rft)
        return {"director_message": dm}

    async def _run_tax_report_harness(
        self, user_input: str, confirmed: bool, delegation: dict
    ) -> dict:
        """/do_tax — advance the tax report harness state machine by one turn.

        Reads checkpoint from working_memory to determine which step to run.
        Passes user_input (CSV names or confirm reply) and confirmed flag.
        """
        from tax_harness.report_harness import TaxReportHarness
        tax_year = delegation.get("tax_year") or ""
        harness  = TaxReportHarness(self.cog, self.nanobot, self.qdrant, tax_year or None)
        result   = await harness.run(user_input=user_input, confirmed=confirmed)
        return {
            "status":           result.get("status", "ok"),
            "director_message": result.get("response", ""),
            "_translator_bypass": True,
        }

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
            # Route to the collection PASS 4 actually decided on.
            # coll_decision is read from memory_payload above; previously it was used only
            # for mem_type (metadata type field) while collection=WORKING was hardcoded,
            # causing every PASS 4 memory decision to land in ephemeral working_memory
            # regardless of the LLM's collection choice.  Fix: write directly to the
            # sovereign RAID collection when PASS 4 specifies one.
            _target_coll = coll_decision if coll_decision in SOVEREIGN_COLLECTIONS else WORKING
            await self.cog.save_lesson(
                memory_payload["lesson"], user_input,
                collection=_target_coll,
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

    async def _write_execution_episodic(
        self, intent: str, tier: str, execution_result: dict, user_input: str,
    ) -> None:
        """Deterministic episodic write to working_memory for every adapter execution.

        Always fires regardless of PASS 4 memory_action. Awaited (not create_task) so
        it completes before any response is sent to Telegram. Raw error detail is included
        on failures so follow-up questions can reference what actually happened.
        """
        try:
            success = execution_result.get("execution_confirmed", False)
            error = (execution_result.get("error") or execution_result.get("raw_error") or "")
            if success:
                lesson = f"Executed intent={intent!r} tier={tier} — success"
            else:
                error_detail = str(error)[:300] if error else "no error detail returned"
                lesson = f"Executed intent={intent!r} tier={tier} — FAILED: {error_detail}"
            await self.qdrant.save_lesson(
                lesson, user_input[:200],
                collection=WORKING,
                memory_type="episodic",
                writer="sovereign-core",
                extra_metadata={
                    "intent": intent,
                    "tier": tier,
                    "success": success,
                    "error": str(error)[:500] if error else None,
                    "outcome": "positive" if success else "negative",
                    "_exec_log": True,
                },
            )
        except Exception:
            pass  # never block the execution path on a memory write failure

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
            # Mail routes through Nextcloud Mail REST API (nc-mail skill)
            if domain == "mail":
                source = "nc_mail_live"
            else:
                source = _DOMAIN_SOURCE.get(domain, "unknown_adapter")
            result["_result_source"] = source

        # ── Outcome write-back — fire and forget, never blocks return path
        # Skip if result is a confirmation gate (no actual execution happened).
        if (
            isinstance(result, dict)
            and delegation
            and self.qdrant
            and not result.get("requires_security_confirmation")
            and not result.get("requires_confirmation")
        ):
            _intent = (
                delegation.get("intent")
                or (payload or {}).get("intent")
                or action.get("name", action.get("domain", ""))
            )
            if _intent:
                import asyncio as _asyncio_wb
                _asyncio_wb.create_task(self._outcome_write_back(
                    intent=_intent,
                    action=action,
                    result=result,
                    routing_source=delegation.get("_routing_source", "unknown"),
                    delegation=delegation,
                ))

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
            # WebDAV adapter removed (Phase 2 adapter removal) — all file ops now route through
            # nanobot-01 (sovereign-nextcloud-fs / openclaw-nextcloud python3_exec scripts).
            # Unwrap nanobot envelope so callers receive a flat dict matching the old adapter
            # shape, preserving _trust for the security scanner.
            def _nb_unwrap(nb):
                if nb.get("status") == "ok":
                    flat = nb.get("result") if nb.get("result") is not None else {}
                    return {"status": "ok", "_trust": nb.get("_trust", "untrusted_external"), **flat}
                return nb

            if name == "file_navigate":
                return _nb_unwrap(await self.nanobot.run(
                    "sovereign-nextcloud-fs", "fs_list", {"path": path}
                ))
            if name in ("file_list",):
                # RAID paths are mounted directly in sovereign-core — read via broker, not Nextcloud
                if path.startswith(("/home/sovereign/", "/docker/sovereign/")):
                    return await self.broker.read_host_file(path)
                return _nb_unwrap(await self.nanobot.run(
                    "sovereign-nextcloud-fs", "fs_list", {"path": path}
                ))
            if name == "file_read":
                # RAID paths are mounted in sovereign-core — route to broker hostfs, not Nextcloud WebDAV
                if path.startswith(("/home/sovereign/", "/docker/sovereign/")):
                    return await self.broker.read_host_file(path)
                return _nb_unwrap(await self.nanobot.run(
                    "sovereign-nextcloud-fs", "fs_read", {"path": path}
                ))
            if name == "folder_create":
                return _nb_unwrap(await self.nanobot.run(
                    "sovereign-nextcloud-fs", "fs_mkdir", {"path": path}
                ))
            if name == "file_search":
                sp = specialist or {}
                query = sp.get("query") or action.get("query") or prompt or ""
                search_path = sp.get("path") or action.get("path", "/")
                if not query:
                    return {"error": "search_files requires a query term"}
                return _nb_unwrap(await self.nanobot.run(
                    "sovereign-nextcloud-fs", "fs_search", {"query": query, "path": search_path}
                ))
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
                return _nb_unwrap(await self.nanobot.run(
                    "openclaw-nextcloud", "files_write", {"path": path, "content": content}
                ))
            if name == "file_delete":
                return _nb_unwrap(await self.nanobot.run(
                    "sovereign-nextcloud-fs", "fs_delete", {"path": path}
                ))
            if name == "file_list_recursive":
                return _nb_unwrap(await self.nanobot.run(
                    "openclaw-nextcloud", "files_list_recursive", {"path": path}
                ))
            if name == "file_read_recursive":
                return _nb_unwrap(await self.nanobot.run(
                    "openclaw-nextcloud", "files_read_recursive", {"path": path}
                ))

        if domain == "caldav":
            # CalDAV adapter removed (Phase 3 adapter removal) — all calendar/task ops now route
            # through nanobot-01 (openclaw-nextcloud python3_exec scripts).
            # _nb_unwrap: identical helper to the webdav block — flatten nanobot envelope.
            def _nb_unwrap(nb):
                if nb.get("status") == "ok":
                    flat = nb.get("result") if nb.get("result") is not None else {}
                    return {"status": "ok", "_trust": nb.get("_trust", "untrusted_external"), **flat}
                return nb

            if name == "calendar_read":
                return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "calendar_list", {}))
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

                # ── Date range preprocessing ───────────────────────────────────
                # Handle "6–7 June 2026" / "6-7 June 2026" style ranges the
                # specialist may output as a single start value.  Split into
                # separate start/end strings before _normalise_dt is called.
                import re as _re_rng
                if _start_raw and not _end_raw:
                    _rng_m = _re_rng.search(
                        r'^(\d{1,2})\s*[–\-]\s*(\d{1,2})\s+([A-Za-z]+(?:\s+\d{4})?)',
                        _start_raw.strip(),
                    )
                    if _rng_m:
                        _d1, _d2, _rest = _rng_m.group(1), _rng_m.group(2), _rng_m.group(3)
                        _start_raw = f"{_d1} {_rest}"
                        _end_raw   = f"{_d2} {_rest}"

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

                # Note: uid is generated by openclaw-nextcloud internally (not passed)
                return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "calendar_create", {
                    "title": cal_summary,
                    "start": cal_start,
                    "end": cal_end,
                    "description": cal_description,
                    "calendar": cal_calendar,
                }))
            if name == "task_create":
                d = delegation or {}
                sp = specialist or {}
                task_summary     = sp.get("summary")     or action.get("summary")     or d.get("intent", "")
                task_due         = sp.get("due")         or action.get("due",         "")
                task_calendar    = sp.get("calendar")    or action.get("calendar",    "tasks")
                task_description = sp.get("description") or action.get("description", "")
                return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "tasks_create", {
                    "summary": task_summary,
                    "due": task_due,
                    "description": task_description,
                    "calendar": task_calendar,
                }))
            if name == "calendar_list_events":
                sp = specialist or {}
                evt_calendar  = sp.get("calendar") or action.get("calendar", "personal")
                evt_from_date = sp.get("from_date") or action.get("from_date", "")
                evt_to_date   = sp.get("to_date")   or action.get("to_date",   "")
                _evts_nb = await self.nanobot.run("openclaw-nextcloud", "calendar_list_events", {
                    "calendar": evt_calendar,
                    "from_date": evt_from_date,
                    "to_date": evt_to_date,
                })
                _evts_result = _nb_unwrap(_evts_nb)
                _evts_raw = _evts_result.get("events") or []
                if _evts_raw:
                    _asyncio.create_task(self._index_items(
                        [{"title": e.get("summary",""), "uid": e.get("uid",""), "date": e.get("dtstart",""), "calendar": evt_calendar}
                         for e in _evts_raw if e.get("uid")],
                        "event"
                    ))
                return _evts_result
            if name == "task_complete":
                sp = specialist or {}
                comp_calendar = sp.get("calendar") or action.get("calendar", "tasks")
                comp_uid      = sp.get("uid")      or action.get("uid", "")
                if not comp_uid:
                    return {"error": "complete_task requires a UID — please specify which task"}
                return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "tasks_complete", {
                    "uid": comp_uid,
                    "calendar": comp_calendar,
                }))
            if name == "calendar_update":
                sp = specialist or {}
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
                return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "calendar_update", {
                    "uid": upd_uid,
                    "calendar": upd_calendar,
                    "title": upd_summary,
                    "start": upd_start,
                    "end": upd_end,
                    "description": upd_description,
                }))
            if name in ("task_delete", "calendar_delete"):
                sp = specialist or {}
                del_calendar = sp.get("calendar") or action.get("calendar", "personal")
                del_uid      = sp.get("uid")      or action.get("uid", "")
                if not del_uid:
                    return {"error": f"{name} requires a UID — please specify which item to delete"}
                if name == "task_delete":
                    return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "tasks_delete", {
                        "uid": del_uid,
                        "calendar": del_calendar,
                    }))
                return _nb_unwrap(await self.nanobot.run("openclaw-nextcloud", "calendar_delete", {
                    "uid": del_uid,
                    "calendar": del_calendar,
                }))

        if domain == "notes":
            sp = specialist or {}
            _skill_nc = "openclaw-nextcloud"

            # ── notes_list — also refreshes the session index ──────────────
            if name == "notes_list":
                nb = await self.nanobot.run(_skill_nc, "notes_list", {})
                _idx_raw = (nb.get("result") or {}).get("notes") or nb.get("notes") or []
                if _idx_raw:
                    await self._clear_item_index("note")
                    await self._index_items(_idx_raw, "note")
                return nb

            # ── Shared ID resolution: numeric ID first, then title lookup ──
            async def _resolve_note_id(note_id_val, search_title_val):
                """Return (id_str, None) or (None, error_message)."""
                if note_id_val:
                    if str(note_id_val).strip().lstrip('-').isdigit():
                        return str(note_id_val), None
                    search_title_val = search_title_val or str(note_id_val)
                if search_title_val:
                    await self._notes_get_or_build_index()
                    _nid, _err = await self._notes_find_by_title(search_title_val)
                    if _nid is not None:
                        return str(_nid), None
                    return None, _err
                return None, None

            # ── notes_read ─────────────────────────────────────────────────
            if name == "notes_read":
                import re as _re_nid_rd
                _orig_rd = (delegation or {}).get("_original_request", "") or ""
                _nid_rd_m = _re_nid_rd.search(r'\b(\d+)\b', _orig_rd)
                _raw_id = (sp.get("note_id") or sp.get("id") or action.get("note_id", "")
                           or (delegation or {}).get("target", "")
                           or (_nid_rd_m.group(1) if _nid_rd_m else ""))
                _search = sp.get("search_title") or sp.get("note_name") or ""
                note_id, err = await _resolve_note_id(_raw_id, _search)
                if not note_id:
                    return {"error": err or "read_note requires a note ID or title — please specify which note"}
                return await self.nanobot.run(_skill_nc, "notes_read", {"note-id": note_id})

            # ── notes_create ───────────────────────────────────────────────
            if name == "notes_create":
                nb = await self.nanobot.run(_skill_nc, "notes_create", {
                    "title":    sp.get("title")    or action.get("title", ""),
                    "content":  sp.get("content")  or action.get("content", ""),
                    "category": sp.get("category") or action.get("category", ""),
                })
                asyncio.create_task(self._clear_item_index("note"))
                return nb

            # ── notes_update ───────────────────────────────────────────────
            if name == "notes_update":
                import re as _re_nid_ud
                _orig_ud = (delegation or {}).get("_original_request", "") or ""
                _nid_ud_m = _re_nid_ud.search(r'\b(\d+)\b', _orig_ud)
                _raw_id = (sp.get("note_id") or sp.get("id") or action.get("note_id", "")
                           or (delegation or {}).get("target", "")
                           or (_nid_ud_m.group(1) if _nid_ud_m else ""))
                _search = sp.get("search_title") or sp.get("note_name") or ""
                note_id, err = await _resolve_note_id(_raw_id, _search)
                if not note_id:
                    return {"error": err or "update_note requires a note ID or title — please specify which note"}
                nb = await self.nanobot.run(_skill_nc, "notes_update", {
                    "note-id":  note_id,
                    "title":    sp.get("title")    or action.get("title", ""),
                    "content":  sp.get("content")  or action.get("content", ""),
                    "category": sp.get("category") or action.get("category", ""),
                })
                asyncio.create_task(self._clear_item_index("note"))
                return nb

            # ── notes_delete ───────────────────────────────────────────────
            if name == "notes_delete":
                import re as _re_nid_del
                _orig_del = (delegation or {}).get("_original_request", "") or ""
                _nid_del_m = _re_nid_del.search(r'\b(\d+)\b', _orig_del)
                _raw_id = (sp.get("note_id") or sp.get("id") or action.get("note_id", "")
                           or (delegation or {}).get("target", "")
                           or (_nid_del_m.group(1) if _nid_del_m else ""))
                _search = sp.get("search_title") or sp.get("note_name") or ""
                note_id, err = await _resolve_note_id(_raw_id, _search)
                if not note_id:
                    return {"error": err or "delete_note requires a note ID or title — please specify which note"}
                nb = await self.nanobot.run(_skill_nc, "notes_delete", {"note-id": note_id})
                asyncio.create_task(self._clear_item_index("note"))
                return nb

            return {"status": "error", "error": f"Unhandled notes operation: name={name!r}"}

        if domain == "ncfs":
            _skill_ncfs = "sovereign-nextcloud-fs"
            op = action.get("operation", "")
            sp  = specialist or {}
            _del_target = (delegation.get("target") if delegation else "") or ""
            # Only use delegation target as path if it looks like a filesystem path
            _del_path = _del_target if _del_target.startswith("/") else ""
            path = sp.get("path") or sp.get("target") or _del_path
            _target_txt = _del_target
            src  = sp.get("src")  or sp.get("source") or sp.get("from") or ""
            dest = sp.get("dest") or sp.get("destination") or sp.get("to") or ""
            # Fallback: extract src/dest from user input (e.g. "copy /a to /b")
            if not src or not dest:
                import re as _re_cp
                _cp_m = _re_cp.search(r'(/[\w/_\-\.]+)\s+to\s+(/[\w/_\-\.]+)', _target_txt)
                if _cp_m:
                    if not src:  src  = _cp_m.group(1)
                    if not dest: dest = _cp_m.group(2)
            tag  = (sp.get("tag") or sp.get("tag_name")
                    or (delegation.get("tag") if delegation else "")
                    or "sovereign-reviewed")
            query = (sp.get("query") or sp.get("search_query")
                     or sp.get("search_term") or sp.get("keyword") or "")
            # Fallback: extract query keyword from delegation target
            # e.g. "search nextcloud for pipeline" → "pipeline"
            if not query and _target_txt:
                import re as _re_q
                _qm = _re_q.search(r'\b(?:for|named?|called?)\s+(\S+)', _target_txt, _re_q.IGNORECASE)
                query = _qm.group(1).strip() if _qm else _target_txt

            # Map intent name → nanobot operation name
            _ncfs_op = {
                "ncfs_list": "fs_list", "ncfs_list_recursive": "fs_list_recursive",
                "ncfs_read": "fs_read", "ncfs_move": "fs_move", "ncfs_copy": "fs_copy",
                "ncfs_mkdir": "fs_mkdir", "ncfs_delete": "fs_delete",
                "ncfs_tag": "fs_tag", "ncfs_untag": "fs_untag", "ncfs_search": "fs_search",
            }.get(name)

            if name in ("ncfs_list", "ncfs_list_recursive"):
                return await self.nanobot.run(_skill_ncfs, _ncfs_op, {"path": path or "/"})
            if name == "ncfs_read":
                if not path:
                    return {"error": "ncfs_read requires a path"}
                return await self.nanobot.run(_skill_ncfs, _ncfs_op, {"path": path})
            if name in ("ncfs_move", "ncfs_copy"):
                if not src or not dest:
                    return {"error": f"{name} requires src and dest"}
                return await self.nanobot.run(_skill_ncfs, _ncfs_op, {"src": src, "dest": dest})
            if name in ("ncfs_mkdir", "ncfs_delete"):
                if not path:
                    return {"error": f"{name} requires a path"}
                return await self.nanobot.run(_skill_ncfs, _ncfs_op, {"path": path})
            if name in ("ncfs_tag", "ncfs_untag"):
                if not path:
                    return {"error": f"{name} requires a path"}
                return await self.nanobot.run(_skill_ncfs, _ncfs_op, {"path": path, "tag": tag})
            if name == "ncfs_search":
                if not query:
                    return {"error": "ncfs_search requires a query"}
                return await self.nanobot.run(_skill_ncfs, _ncfs_op,
                                              {"query": query, "path": path or "/"})

        if domain == "ncingest":
            _skill_ingest = "sovereign-nextcloud-ingest"
            sp   = specialist or {}
            path = sp.get("path") or sp.get("target") or (delegation.get("target") if delegation else None)
            memory_type = sp.get("memory_type") or "semantic"
            is_private = "private" in (path or "").lower()
            _force_local = is_private  # Private paths stay on local Ollama

            if name == "ingest_status":
                if not path:
                    return {"error": "ingest_status requires a path"}
                return await self.nanobot.run(_skill_ingest, "ingest_status", {"path": path})

            if name in ("ingest_file", "ingest_folder"):
                if not path:
                    return {"error": f"{name} requires a path"}

                op_name = "fetch_classify" if name == "ingest_file" else "fetch_classify_folder"
                nb = await self.nanobot.run(_skill_ingest, op_name, {"path": path})

                if not nb.get("status") == "ok":
                    return nb

                # Enforce Private flag: never route to external LLMs
                nb_private = nb.get("_private", False) or is_private
                if nb_private:
                    logger.info("ncingest: Private path %s — all LLM calls force_local", path)

                # Security scan gate — check inline_scan risk level
                if name == "ingest_file":
                    scan = nb.get("inline_scan", {})
                    if scan.get("risk_level") == "high":
                        if self.ledger:
                            self.ledger.append("ingest_blocked", "inbound", {
                                "path": path,
                                "reason": "inline_scan high risk",
                                "patterns": scan.get("patterns_found", []),
                            })
                        return {
                            "error": f"Ingest blocked: high-risk scan result — patterns: {scan.get('patterns_found')}",
                            "status": "error",
                            "path": path,
                        }

                    content = nb.get("content")
                    if not content or nb.get("binary"):
                        return {
                            "status": "ok",
                            "path": path,
                            "message": f"Binary file — metadata recorded, no text embedded",
                            "content_type": nb.get("content_type", ""),
                            "size": nb.get("size", 0),
                            "_private": nb_private,
                        }

                    resolved_type = sp.get("memory_type") or nb.get("suggested_memory_type", "semantic")
                    # ingest_file is MID tier — Director confirmed before we arrive here.
                    # human_confirmed=True is required for procedural writes.
                    await self.qdrant.store(
                        content,
                        metadata={
                            "type":          resolved_type,
                            "source":        "nextcloud",
                            "source_path":   path,
                            "content_type":  nb.get("content_type", "text/plain"),
                            "size":          nb.get("size", 0),
                            "_trusted_by":   "sovereign-core",
                            "_private":      nb_private,
                        },
                        collection=resolved_type,
                        human_confirmed=True,
                    )

                    # Tag file in Nextcloud as sovereign-reviewed
                    asyncio.create_task(
                        self.nanobot.run("sovereign-nextcloud-fs", "fs_tag",
                                         {"path": path, "tag": "sovereign-reviewed"})
                    )

                    return {
                        "status":       "ok",
                        "path":         path,
                        "collection":   resolved_type,
                        "size":         nb.get("size", 0),
                        "content_type": nb.get("content_type", ""),
                        "_private":     nb_private,
                    }

                # ingest_folder
                files = nb.get("files", [])
                ingested = 0
                blocked  = 0
                errors   = 0
                for f in files:
                    if f.get("status") != "ok":
                        errors += 1
                        continue
                    scan = f.get("inline_scan", {})
                    if scan.get("risk_level") == "high":
                        blocked += 1
                        continue
                    content = f.get("content")
                    if not content or f.get("binary"):
                        continue
                    fpath = f.get("path", path)
                    resolved_type = f.get("suggested_memory_type", "semantic")
                    f_private = f.get("_private", False) or is_private
                    await self.qdrant.store(
                        content,
                        metadata={
                            "type":         resolved_type,
                            "source":       "nextcloud",
                            "source_path":  fpath,
                            "content_type": f.get("content_type", "text/plain"),
                            "_trusted_by":  "sovereign-core",
                            "_private":     f_private,
                        },
                        collection=resolved_type,
                        human_confirmed=True,
                    )
                    asyncio.create_task(
                        self.nanobot.run("sovereign-nextcloud-fs", "fs_tag",
                                         {"path": fpath, "tag": "sovereign-reviewed"})
                    )
                    ingested += 1

                return {
                    "status":   "ok",
                    "path":     path,
                    "ingested": ingested,
                    "blocked":  blocked,
                    "errors":   errors,
                    "skipped":  nb.get("skipped", 0),
                    "_private": is_private,
                }

        # ── Cognitive skills — specialist synthesis, no external dispatch ──────
        if domain == "session":
            sp = specialist or {}
            return {
                "status":               "ok",
                "session_summary":      sp.get("session_summary", ""),
                "sign_off_message":     sp.get("sign_off_message", ""),
                "open_tasks":           sp.get("open_tasks", []),
                "memory_recommendation": sp.get("memory_recommendation", {}),
                "github_push_recommended": sp.get("github_push_recommended", False),
            }

        if domain == "memory_curate":
            sp = specialist or {}
            return {
                "status":     "ok",
                "promote":    sp.get("promote", False),
                "collection": sp.get("collection", ""),
                "content":    sp.get("content", ""),
                "confidence": sp.get("confidence", ""),
                "gap_actions": sp.get("gap_actions", []),
                "reasoning":  sp.get("reasoning", ""),
            }

        if domain == "mail":
            # All mail ops route through nc-mail community skill (Nextcloud Mail REST API).
            # Uses stable databaseId integers — no fragile IMAP UIDs, no account suffix hacks.
            sp = specialist or {}
            # Account resolution: specialist output → pre-classifier target → action default → personal
            _delegation_account = (delegation or {}).get("target")
            account = sp.get("account") or _delegation_account or action.get("account", "personal")
            op = action.get("operation")
            _skill_nc_mail = "nc-mail"

            # Personal inbox IMAP sync can take 55s+; pass timeout=59 so nanobot subprocess has headroom
            _NC_MAIL_TIMEOUT = 59

            def _unwrap_nb(nb_resp: dict) -> dict:
                """Return inner result dict, but preserve status/success from outer wrapper.
                Nanobot wraps script output in {status, success, result: <data>} — stripping
                the outer dict loses status/success needed for execution_confirmed stamping."""
                inner = nb_resp.get("result")
                if inner is not None and isinstance(inner, dict):
                    merged = {
                        "status": nb_resp.get("status", "ok"),
                        "success": nb_resp.get("success", True),
                    }
                    if nb_resp.get("error"):
                        merged["error"] = nb_resp["error"]
                    return {**merged, **inner}
                return nb_resp

            if op == "read":
                if name == "nc_list_mailboxes":
                    nb = await self.nanobot.run(_skill_nc_mail, "list_mailboxes", {
                        "account": account, "timeout": _NC_MAIL_TIMEOUT,
                    })
                    return _unwrap_nb(nb)
                count = int(sp.get("count") or action.get("count", 20))
                nb = await self.nanobot.run(_skill_nc_mail, "list_unread", {
                    "account": account, "limit": count, "timeout": _NC_MAIL_TIMEOUT,
                })
                return _unwrap_nb(nb)

            if op == "fetch":
                database_id = (sp.get("database_id") or sp.get("id") or sp.get("uid")
                               or (delegation or {}).get("database_id") or action.get("database_id", ""))
                from_addr   = sp.get("from_addr") or action.get("from_addr", "")
                subject     = sp.get("subject")   or action.get("subject", "")
                if not database_id and not from_addr and not subject:
                    return {"error": "fetch_message requires database_id, from_addr, or subject"}
                nb = await self.nanobot.run(
                    _skill_nc_mail, "fetch_message",
                    {"account": account, "database_id": database_id,
                     "from_addr": from_addr, "subject": subject, "timeout": _NC_MAIL_TIMEOUT},
                )
                return _unwrap_nb(nb)

            if op == "search":
                criteria = sp.get("criteria") or action.get("criteria") or {}
                for key in ("subject", "from_addr", "since", "body"):
                    if action.get(key) and key not in criteria:
                        criteria[key] = action[key]
                query_parts = [str(v) for v in criteria.values() if v]
                query = (" ".join(query_parts) if query_parts
                         else (sp.get("query") or action.get("query", "")
                               or (delegation or {}).get("query", "")))
                # Route search through list_unread with filter — NC Mail has no separate search endpoint
                nb = await self.nanobot.run(_skill_nc_mail, "list_unread", {
                    "account": account, "filter": query, "unread_only": "false",
                    "limit": 10, "timeout": _NC_MAIL_TIMEOUT,
                })
                result = _unwrap_nb(nb)
                if isinstance(result, dict) and result.get("messages") == [] and not result.get("error"):
                    result["_empty_search"] = True
                    result["status"] = "ok"
                return result

            if op == "flag":
                database_id = sp.get("database_id") or sp.get("id") or action.get("database_id", "")
                if not database_id:
                    return {"error": f"{name} requires a message database_id"}
                cmd = "mark_read" if name == "nc_mark_read" else "mark_unread"
                nb = await self.nanobot.run(_skill_nc_mail, cmd, {"database_id": database_id})
                return _unwrap_nb(nb)

            if op == "move":
                database_id   = (sp.get("database_id") or sp.get("id") or sp.get("uid")
                                 or (delegation or {}).get("database_id") or action.get("database_id", ""))
                from_addr     = sp.get("from_addr")   or action.get("from_addr", "")
                subject       = sp.get("subject")     or action.get("subject", "")
                dest_folder   = (sp.get("target_folder") or action.get("target_folder", "")
                                 or sp.get("folder", "") or action.get("folder", "Archive"))
                nb = await self.nanobot.run(
                    _skill_nc_mail, "move_message",
                    {"account": account, "database_id": database_id,
                     "from_addr": from_addr, "subject": subject, "dest_folder": dest_folder},
                )
                return _unwrap_nb(nb)

            if op == "delete":
                database_id = (sp.get("database_id") or sp.get("id") or sp.get("uid")
                               or (delegation or {}).get("database_id") or action.get("database_id", ""))
                from_addr   = sp.get("from_addr") or action.get("from_addr", "")
                subject     = sp.get("subject")   or action.get("subject", "")
                # If no database_id yet, search by sender/subject to resolve it before deleting.
                # This enables natural "delete the Real Vision email" without requiring the user
                # to quote the ID. The search uses supplied text; delete uses the resolved ID.
                if not database_id and (from_addr or subject):
                    _search_hint = from_addr or subject
                    _search_nb = await self.nanobot.run(
                        _skill_nc_mail, "list_unread",
                        {"account": account, "filter": _search_hint,
                         "unread_only": "false", "limit": 5, "timeout": _NC_MAIL_TIMEOUT},
                    )
                    _search_msgs = (_search_nb.get("messages") or []) if isinstance(_search_nb, dict) else []
                    for _smsg in _search_msgs:
                        _sfrom = (_smsg.get("from") or "")
                        _ssubj = (_smsg.get("subject") or "")
                        if (from_addr and from_addr.lower() in _sfrom.lower()) or \
                           (subject and subject.lower() in _ssubj.lower()):
                            database_id = str(_smsg.get("databaseId", ""))
                            break
                nb = await self.nanobot.run(
                    _skill_nc_mail, "delete_message",
                    {"account": account, "database_id": database_id,
                     "from_addr": from_addr, "subject": subject},
                )
                return _unwrap_nb(nb)

            if op == "send":
                s = specialist or {}
                _dlg = delegation or {}
                draft = s.get("draft_content", "")
                nb = await self.nanobot.run(
                    _skill_nc_mail, "send",
                    {
                        "account": account,
                        "to":      (s.get("to")      or _dlg.get("to")      or action.get("to", "")),
                        "subject": (s.get("subject") or _dlg.get("subject") or action.get("subject", "")),
                        "body":    (draft or s.get("body") or _dlg.get("body") or action.get("body", "")),
                    }
                )
                return _unwrap_nb(nb)

            return {"status": "error", "error": f"Unhandled mail operation: op={op!r} name={name!r}"}

        if domain == "ollama":
            if not prompt:
                return {"error": "prompt required"}
            # Wrap with Rex context so the model doesn't revert to its training persona.
            _rex_ctx = (
                "You are Rex, the Sovereign AI operated by Matt. "
                "Rex has internet access via browser tools, email (IMAP/SMTP), "
                "files (Nextcloud WebDAV), calendar (CalDAV), and sovereign memory (Qdrant). "
                "Rex does NOT say 'I don't have access to the internet' or 'I'm a text-based AI'. "
                "Answer as Rex. If you don't know the answer, say so plainly.\n\n"
            )
            # Only route to Grok/Claude when the Director explicitly requests it.
            # Auto-signal routing (latest/news/current/complexity) is intentionally NOT applied
            # here — those signals already govern PASS 2/3a LLM selection for planning.
            # Firing Grok automatically at execution would duplicate RSS, browser, and feed
            # paths that PASS 1 may have already chosen, overwhelming the 8b model with
            # redundant inputs.
            import re as _re_ext
            # Explicit Grok: direct requests only.
            # "trending" and "current events" removed — they now route to the news harness
            # via PASS 1 _quick_classify before reaching this code.
            # "latest/today/recent" intentionally excluded — too ambiguous.
            _EXPLICIT_GROK   = _re_ext.compile(
                r'\b(use grok|ask grok|via grok)\b',
                _re_ext.IGNORECASE,
            )
            _EXPLICIT_CLAUDE = _re_ext.compile(r'\b(use claude|ask claude|via claude)\b', _re_ext.IGNORECASE)
            if _EXPLICIT_GROK.search(prompt):
                routed = await self.cog.route_cognition(_rex_ctx + prompt, agent="research_agent",
                                                        provider="grok", user_input=prompt)
                return {"status": "ok", "model": "grok", "response": routed.get("response", ""),
                        "_routed_external": True, "_provider": "grok"}
            if _EXPLICIT_CLAUDE.search(prompt):
                routed = await self.cog.route_cognition(_rex_ctx + prompt, agent="research_agent",
                                                        provider="claude", user_input=prompt)
                return {"status": "ok", "model": "claude", "response": routed.get("response", ""),
                        "_routed_external": True, "_provider": "claude"}
            result = await self.cog.ask_local(_rex_ctx + prompt)
            return {"status": "ok", "model": result.get("model"), "response": result.get("response")}

        if domain == "news":
            from monitoring.news_harness import run_news_brief
            return await run_news_brief(self.cog, self.nanobot, self.qdrant, prompt or "")

        if domain == "tax":
            from tax_harness.harness import TaxIngestHarness
            _harness = TaxIngestHarness(self.cog, self.nanobot, self.qdrant)

            if operation == "run":
                result = await _harness.run()
                return {
                    "status":   result.get("status", "ok"),
                    "response": result.get("summary", "Tax ingest complete."),
                    "detail":   result,
                    "_translator_bypass": True,
                }

            if operation == "store":
                # Alias for run — Director-confirmed explicit store
                result = await _harness.run()
                return {
                    "status":   result.get("status", "ok"),
                    "response": result.get("summary", "Tax events stored."),
                    "detail":   result,
                    "_translator_bypass": True,
                }

            if operation == "status":
                return {
                    "status":   "ok",
                    "response": (
                        "Tax Ingest Harness: LIVE. "
                        "Hourly cron (0 * * * *) — pending_approval until Director activates. "
                        "Files: /Tax folder on Nextcloud. "
                        "Wallet events: wired via /wallet_event endpoint."
                    ),
                    "_translator_bypass": True,
                }

            if operation in ("list", "summary", "query", "addresses"):
                # Delegate to Qdrant semantic search for tax events
                tax_year = action.get("tax_year") or payload.get("tax_year") or ""
                query_str = (
                    f"tax events {tax_year}" if tax_year else "tax events income disposal"
                )
                try:
                    from qdrant_client.models import Filter, FieldCondition, MatchValue
                    hits = await self.qdrant.archive_client.search(
                        collection_name="semantic",
                        query_vector=await self.qdrant._embed(query_str),
                        query_filter=Filter(must=[
                            FieldCondition(key="domain", match=MatchValue(value="tax")),
                        ]),
                        limit=20,
                        with_payload=True,
                    )
                    events = [dict(h.payload or {}) for h in hits]
                    if tax_year:
                        events = [e for e in events if e.get("tax_year") == tax_year]
                    return {
                        "status":   "ok",
                        "response": f"Found {len(events)} tax event(s).",
                        "events":   events,
                        "_translator_bypass": True,
                    }
                except Exception as _te:
                    return {"status": "error", "error": str(_te)}

            if operation == "ingest_status":
                # Pre-flight check: counts for current FY from semantic + unprocessed files
                from .adapters.qdrant import QdrantAdapter as _QA
                from tax_harness.models import resolve_tax_year as _rty
                from datetime import datetime as _dt, timezone as _tz
                _now_ts  = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _cur_year = _rty(_now_ts)
                _year_int = int(_cur_year)
                _start = f"{_year_int - 1}-04-01T00:00:00Z"
                _end   = f"{_year_int}-03-31T23:59:59Z"
                try:
                    from qdrant_client.models import Filter, FieldCondition, MatchValue as _MV
                    _hits, _ = await self.qdrant.archive_client.scroll(
                        collection_name="semantic",
                        scroll_filter=Filter(must=[
                            FieldCondition(key="domain", match=_MV(value="tax")),
                            FieldCondition(key="type",   match=_MV(value="tax_event")),
                        ]),
                        limit=2000,
                        with_payload=True,
                        with_vectors=False,
                    )
                    from tax_harness.report_harness import _in_range as _ir
                    _in_fy     = [h for h in _hits if _ir((h.payload or {}).get("timestamp",""), _start, _end)]
                    _n_crypto  = sum(1 for h in _in_fy if (h.payload or {}).get("event_tag") == "tax:crypto")
                    _n_expense = sum(1 for h in _in_fy if (h.payload or {}).get("event_tag") == "tax:expense")
                    _n_unpriced = sum(1 for h in _in_fy if not (h.payload or {}).get("nzd_value"))
                    _dates     = sorted(
                        [(h.payload or {}).get("timestamp", "") for h in _in_fy if (h.payload or {}).get("timestamp")],
                        reverse=True,
                    )
                    _latest    = _dates[0][:10] if _dates else "none"
                    return {
                        "status":   "ok",
                        "response": (
                            f"Tax ingest status — FY{_cur_year} "
                            f"(01 Apr {_year_int-1} – 31 Mar {_year_int}):\n"
                            f"  tax:crypto events: {_n_crypto}\n"
                            f"  tax:expense events: {_n_expense}\n"
                            f"  unpriced (nzd_value null): {_n_unpriced}\n"
                            f"  most recent event: {_latest}\n"
                            f"Run /do_tax {_cur_year} to generate the report."
                        ),
                        "_translator_bypass": True,
                    }
                except Exception as _ise:
                    return {"status": "error", "error": str(_ise)}

            if operation in ("report_query", "report_ingest", "report_create",
                             "report_notify", "report_clear", "report_status"):
                # Delegate to report harness via _run_tax_report_harness
                _ty = action.get("tax_year") or payload.get("tax_year") or ""
                return await self._run_tax_report_harness(
                    user_input=user_input,
                    confirmed=confirmed,
                    delegation={"tax_year": _ty},
                )

            return {"status": "error", "error": f"unknown tax operation: {operation}"}

        if domain == "browser":
            if operation == "fetch":
                url = action.get("url") or prompt or ""
                if not url:
                    return {"error": "url required for browser fetch"}
                nb = await self.nanobot.run(
                    "sovereign-browser", "fetch",
                    {"url": url, "extract": action.get("extract", "text"), "timeout": 60},
                )
                if nb.get("status") == "ok":
                    # nb["result"] is the flat a2a-browser FetchResponse fields
                    flat = nb.get("result") if nb.get("result") is not None else nb
                    return {
                        "status": "ok",
                        "url": flat.get("url", ""),
                        "title": flat.get("title", ""),
                        "content": flat.get("content", ""),
                        "content_length": flat.get("content_length", 0),
                        "fetch_sha256": flat.get("fetch_sha256", ""),
                    }
                return nb

            # action["query"] set by _delegation_to_action when quick_classify extracts the query;
            # prefer it over the full prompt (which may include "search the web for..." prefix).
            query = action.get("query") or prompt or ""
            if not query:
                return {"error": "query required for browser search"}
            nb = await self.nanobot.run(
                "sovereign-browser", "search",
                {
                    "query":         query,
                    "locale":        action.get("locale", payload.get("locale", "en-US")),
                    "return_format": action.get("return_format", "full"),
                    "test_mode":     payload.get("test_mode", False),
                    "timeout":       60,
                },
            )
            # Synthesise a human-readable response so the gateway can render it
            if nb.get("status") == "ok":
                import urllib.parse as _up
                # nb["result"] is the flat a2a-browser SearchResponse fields
                enriched = nb.get("result") if nb.get("result") is not None else {}
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
                nb["response"] = "\n".join(parts)
            return nb

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
                _search_result = await lifecycle.search(
                    query=query,
                    certified_only=certified_only,
                    limit=int(action.get("limit", 10)),
                )
                # Harness checkpoint: write if ≥1 candidate has skill_md content
                _all_cands = _search_result.get("candidates", [])
                _valid_cands = [c for c in _all_cands if c.get("skill_md")]
                if _valid_cands:
                    import uuid as _uuid_sk, datetime as _dt_sk
                    _numbered = [{"id": i + 1, **c} for i, c in enumerate(_valid_cands)]
                    _now_sk = _dt_sk.datetime.now(_dt_sk.timezone.utc).isoformat()
                    await self._skill_harness_save_checkpoint({
                        "skill_name": "skill-harness",
                        "session_id": str(_uuid_sk.uuid4()),
                        "current_step": "search",
                        "step_results": {
                            "search": {
                                "query": query,
                                "candidates": _numbered,
                                "ts": _now_sk,
                            },
                        },
                        "last_checkpoint_ts": _now_sk,
                    })
                    _search_result["harness_checkpoint_written"] = True
                    _search_result["numbered_candidates"] = _numbered
                    # Replace candidates with checkpoint-aligned numbered list so
                    # display IDs match checkpoint IDs exactly. Non-fetchable candidates
                    # (no skill_md) are excluded — they can't be reviewed anyway.
                    _search_result["candidates"] = _numbered
                return _search_result

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
                    raw_url=action.get("raw_url"),
                )

            if op == "audit":
                return lifecycle.audit()

            if op == "list_candidates":
                _cp = await self._skill_harness_load_checkpoint()
                if not _cp:
                    return {
                        "status": "no_checkpoint",
                        "message": "No skill search in progress. Run a skill search first.",
                    }
                _cands = _cp.get("step_results", {}).get("search", {}).get("candidates", [])
                if not _cands:
                    return {
                        "status": "no_candidates",
                        "message": "Skill search completed but returned no valid candidates.",
                    }
                return {
                    "status": "ok",
                    "candidates": _cands,
                    "total": len(_cands),
                    "current_step": _cp.get("current_step"),
                    "instructions": (
                        "To review a candidate: 'review candidate N'. "
                        "To install after review: 'install candidate N'. "
                        "To start over: 'clear skill search'."
                    ),
                }

            if op == "review_candidate":
                import datetime as _dt_rv
                _cp = await self._skill_harness_load_checkpoint()
                if not _cp:
                    return {
                        "status": "no_checkpoint",
                        "message": "No skill search in progress. Run a skill search first.",
                    }
                _cands = _cp.get("step_results", {}).get("search", {}).get("candidates", [])
                if not _cands:
                    return {
                        "status": "no_candidates",
                        "message": "No candidates in checkpoint. Run a skill search first.",
                    }
                # Resolve candidate id from delegation target, action, or specialist
                _cid_raw = (
                    (delegation or {}).get("target")
                    or action.get("target")
                    or sp.get("candidate_id")
                    or "1"
                )
                try:
                    _cid = int(str(_cid_raw).strip())
                except (ValueError, TypeError):
                    _cid = 1
                _candidate = next((c for c in _cands if c.get("id") == _cid), None)
                if not _candidate:
                    return {
                        "status": "not_found",
                        "message": (
                            f"Candidate {_cid} not found. "
                            f"Available: {[c.get('id') for c in _cands]}"
                        ),
                    }
                _skill_md = _candidate.get("skill_md", "")
                _slug = _candidate.get("slug", f"candidate-{_cid}")
                if not _skill_md:
                    return {
                        "status": "error",
                        "message": f"Candidate {_cid} has no SKILL.md content to review.",
                    }
                # Gate 1: pre-scan — only unambiguous injection categories hard-block.
                # Sensitive data keywords (password, API_KEY, TOKEN=), destructive_commands,
                # exfiltration regex, and prompt_injection_regex (which fires on 'developer mode'
                # in legitimate blockchain/SDK docs) are advisory only — passed to LLM reviewer.
                # Matt directive 2026-03-28: contents of variables are secret, not the headings.
                _INJECT_HARD_BLOCK = frozenset({
                    # identity_override excluded — "ignore previous instructions" etc. appear
                    # legitimately in security documentation as examples of what to detect.
                    # prompt_injection_regex excluded — 'developer mode' fires on SDK docs.
                    "governance_bypass", "secret_exfiltration", "tool_escalation",
                })
                _pre_scan = self.scanner.scan(_skill_md)
                _ts_rv = _dt_rv.datetime.now(_dt_rv.timezone.utc).isoformat()
                _hard_cats_rv = [c for c in _pre_scan.categories if c in _INJECT_HARD_BLOCK]
                if _hard_cats_rv:
                    _updated = dict(_cp)
                    _sr = dict(_cp.get("step_results", {}))
                    _sr["review"] = {
                        "candidate_id": _cid, "slug": _slug,
                        "verdict": "block", "risk_level": "critical",
                        "pre_scan_blocked": True,
                        "pre_scan_categories": _hard_cats_rv,
                        "ts": _ts_rv,
                    }
                    _updated["step_results"] = _sr
                    _updated["current_step"] = "review"
                    _updated["last_checkpoint_ts"] = _ts_rv
                    await self._skill_harness_save_checkpoint(_updated)
                    return {
                        "status": "blocked",
                        "verdict": "block",
                        "candidate_id": _cid,
                        "message": (
                            f"Pre-scan blocked candidate {_cid} ({_slug}): "
                            "injection patterns detected."
                        ),
                        "categories": _hard_cats_rv,
                    }
                # Gate 2: full LLM security review
                _certified = bool(_candidate.get("certified", False))
                _review_result = await lifecycle.review(
                    slug=_slug,
                    skill_md_content=_skill_md,
                    certified=_certified,
                )
                _ts_rv2 = _dt_rv.datetime.now(_dt_rv.timezone.utc).isoformat()
                _updated2 = dict(_cp)
                _sr2 = dict(_cp.get("step_results", {}))
                _sr2["review"] = {
                    "candidate_id": _cid, "slug": _slug,
                    "verdict": _review_result.get("decision", "review"),
                    "risk_level": _review_result.get("risk_level", "unknown"),
                    "escalation_reasons": _review_result.get("escalation_reasons", []),
                    "escalate_to_director": _review_result.get("escalate_to_director", False),
                    "ts": _ts_rv2,
                }
                _updated2["step_results"] = _sr2
                _updated2["current_step"] = "review"
                _updated2["last_checkpoint_ts"] = _ts_rv2
                await self._skill_harness_save_checkpoint(_updated2)
                return {
                    "status": "ok",
                    "candidate_id": _cid,
                    "slug": _slug,
                    "review_result": _review_result,
                    "next_step": (
                        f"Reply 'install candidate {_cid}' to proceed with installation."
                        if _review_result.get("decision") != "block"
                        else "This skill is blocked and cannot be installed."
                    ),
                }

            if op == "clear_harness":
                try:
                    _offset_cl = None
                    _to_del: list = []
                    while True:
                        _cl_res, _cl_next = await self.qdrant.client.scroll(
                            collection_name=WORKING,
                            limit=100,
                            offset=_offset_cl,
                            with_payload=True,
                            with_vectors=False,
                        )
                        for _r in _cl_res:
                            if (_r.payload or {}).get("_skill_harness_checkpoint"):
                                _to_del.append(_r.id)
                        if _cl_next is None:
                            break
                        _offset_cl = _cl_next
                    if _to_del:
                        await self.qdrant.client.delete(
                            collection_name=WORKING,
                            points_selector=_to_del,
                        )
                    return {
                        "status": "ok",
                        "message": (
                            f"Skill harness session cleared. "
                            f"{len(_to_del)} checkpoint(s) removed."
                        ),
                        "cleared_count": len(_to_del),
                    }
                except Exception as _e_cl:
                    logger.warning("skill_clear_harness failed: %s", _e_cl)
                    return {"status": "error", "message": f"Clear failed: {_e_cl}"}

            if op == "install":
                # ── Harness path: target is a numeric candidate ID ──────────
                import re as _re_hn, datetime as _dt_inst
                _hn_target = str(
                    (delegation or {}).get("target")
                    or action.get("target")
                    or ""
                ).strip()
                _hn_match = _re_hn.match(r'^(\d+)$', _hn_target)
                if _hn_match:
                    _h_cid = int(_hn_match.group(1))
                    _h_cp = await self._skill_harness_load_checkpoint()
                    if not _h_cp:
                        return {
                            "status": "no_checkpoint",
                            "message": "No skill search in progress. Run a skill search first.",
                        }
                    _h_review = _h_cp.get("step_results", {}).get("review", {})
                    if not _h_review or _h_review.get("candidate_id") != _h_cid:
                        return {
                            "status": "review_required",
                            "message": (
                                f"Candidate {_h_cid} must be reviewed before installation. "
                                f"Run 'review candidate {_h_cid}' first."
                            ),
                        }
                    if _h_review.get("verdict") == "block":
                        return {
                            "status": "blocked",
                            "verdict": "block",
                            "message": (
                                f"Candidate {_h_cid} was blocked by security review "
                                "and cannot be installed."
                            ),
                        }
                    _h_cands = (
                        _h_cp.get("step_results", {}).get("search", {}).get("candidates", [])
                    )
                    _h_cand = next((c for c in _h_cands if c.get("id") == _h_cid), None)
                    if not _h_cand:
                        return {
                            "status": "not_found",
                            "message": f"Candidate {_h_cid} not found in checkpoint.",
                        }
                    _h_slug = _h_review.get("slug") or _h_cand.get("slug", "")
                    _h_md = _h_cand.get("skill_md", "")
                    _h_rv = {
                        "decision": _h_review.get("verdict", "review"),
                        "risk_level": _h_review.get("risk_level", "unknown"),
                        "escalation_reasons": _h_review.get("escalation_reasons", []),
                        "escalate_to_director": _h_review.get("escalate_to_director", False),
                    }
                    if not confirmed:
                        # Present to Director for HIGH-tier confirmation
                        _h_pending = {
                            "name": _h_slug,
                            "skill_md": _h_md,
                            "review_result": _h_rv,
                            "clawhub_slug": _h_slug,
                            "clawhub_certified": bool(_h_cand.get("certified", False)),
                            "_harness_candidate_id": _h_cid,
                            "raw_url": _h_cand.get("raw_url"),
                        }
                        _h_escalated = _h_review.get("escalate_to_director", False)
                        _h_dec = _h_review.get("verdict", "review")
                        _h_summary = (
                            f"Install skill '{_h_slug}' (candidate {_h_cid}).\n"
                            f"Security review: {_h_dec.upper()}"
                            + (" — ESCALATED" if _h_escalated else "") + ".\n"
                            + (
                                "Escalation reasons: "
                                + "; ".join(_h_review.get("escalation_reasons", []))
                                + ".\n"
                                if _h_escalated else ""
                            )
                            + "Reply yes to install or no to cancel."
                        )
                        _h_resp = {
                            "status": "awaiting_director_confirmation",
                            "requires_confirmation": True,
                            "tier": "HIGH",
                            "action": "skill_install",
                            "summary": _h_summary,
                            "candidate": {
                                "id": _h_cid,
                                "slug": _h_slug,
                                "summary": _h_cand.get("summary"),
                                "github_url": _h_cand.get("github_url"),
                            },
                            "review_result": _h_rv,
                            "pending_delegation": {
                                "delegate_to": "devops_agent",
                                "intent": "skill_install",
                                "_pending_load": _h_pending,
                            },
                        }
                        if _h_escalated:
                            _h_resp["escalation_notice"] = (
                                f"Escalation reasons: "
                                f"{'; '.join(_h_review.get('escalation_reasons', []))}."
                            )
                        return _h_resp
                    # confirmed=True — execute install
                    _h_dl = (delegation or {}).get("_pending_load") or {}
                    _h_install_result = await lifecycle.load(
                        name=_h_dl.get("name") or _h_slug,
                        skill_md_content=_h_dl.get("skill_md") or _h_md,
                        review_result=_h_dl.get("review_result") or _h_rv,
                        confirmed=True,
                        clawhub_slug=_h_dl.get("clawhub_slug") or _h_slug,
                        clawhub_certified=bool(_h_dl.get("clawhub_certified", False)),
                        proposed_by=(delegation or {}).get("delegate_to", "devops_agent"),
                        reason=f"Director confirmed harness install of candidate {_h_cid}.",
                        raw_url=_h_dl.get("raw_url"),
                    )
                    if _h_install_result.get("status") == "installed":
                        _h_ts = _dt_inst.datetime.now(_dt_inst.timezone.utc).isoformat()
                        _h_cp_up = dict(_h_cp)
                        _h_sr = dict(_h_cp.get("step_results", {}))
                        _h_sr["install"] = {
                            "candidate_id": _h_cid,
                            "skill_name": _h_dl.get("name") or _h_slug,
                            "path": _h_install_result.get("path", ""),
                            "ts": _h_ts,
                        }
                        _h_cp_up["step_results"] = _h_sr
                        _h_cp_up["current_step"] = "complete"
                        _h_cp_up["last_checkpoint_ts"] = _h_ts
                        await self._skill_harness_save_checkpoint(_h_cp_up)
                    return _h_install_result

                # ── Legacy composite 3-step flow: search → review → load ────
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
                        raw_url=_dl_pending.get("raw_url"),
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

                # Update harness checkpoint with fresh search results so that any
                # subsequent "install candidate N" uses the correct candidates from
                # THIS search, not a stale checkpoint from a prior session.
                _leg_all = search_result.get("candidates", [])
                _leg_valid = [c for c in _leg_all if c.get("skill_md")]
                if _leg_valid:
                    import uuid as _leg_uuid, datetime as _leg_dt
                    _leg_numbered = [{"id": i + 1, **c} for i, c in enumerate(_leg_valid)]
                    _leg_ts = _leg_dt.datetime.now(_leg_dt.timezone.utc).isoformat()
                    await self._skill_harness_save_checkpoint({
                        "skill_name": "skill-harness",
                        "session_id": str(_leg_uuid.uuid4()),
                        "current_step": "search",
                        "step_results": {
                            "search": {
                                "query": query,
                                "candidates": _leg_numbered,
                                "ts": _leg_ts,
                            },
                        },
                        "last_checkpoint_ts": _leg_ts,
                    })
                    search_result["candidates"] = _leg_numbered

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
                    "raw_url": top.get("raw_url"),
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
                        raw_url=pending.get("raw_url"),
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
                # Default to SEMANTIC for explicit remember_fact writes — facts and URLs
                # committed by the Director should be durable RAID entries, not ephemeral
                # working_memory.  Action dict may override (e.g. prospective for commitments).
                _raw_coll = action.get("collection", SEMANTIC)
                coll = _raw_coll if _raw_coll in SOVEREIGN_COLLECTIONS else SEMANTIC
                mem_type = action.get("type", SEMANTIC)
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
                # Confirm the MIP key that was generated so the Director can verify retrieval
                _mip_key = None
                try:
                    _pts = await self.qdrant.archive_client.retrieve(
                        collection_name=coll,
                        ids=[point_id],
                        with_payload=True,
                        with_vectors=False,
                    )
                    if _pts:
                        _mip_key = _pts[0].payload.get("_key") or "(key generation pending)"
                except Exception:
                    pass
                return {
                    "status": "ok",
                    "message": f"Stored: {fact}",
                    "point_id": point_id,
                    "mip_key": _mip_key,
                    "collection": coll,
                }

            if op == "recall":
                # Exact content substring match — answers "do you remember / know this?"
                # Uses MatchText (Qdrant full-text index) rather than vector search so the
                # result is deterministic: address found = yes, not found = no.
                query = (action.get("query")
                         or (delegation or {}).get("target")
                         or (specialist or {}).get("query")
                         or prompt or "").strip()
                if not query:
                    return {"status": "error", "message": "recall requires a query term"}
                from qdrant_client.models import Filter, FieldCondition, MatchText as _MatchText
                _recall_results: list = []
                try:
                    _scroll_pts, _ = await self.qdrant.archive_client.scroll(
                        collection_name="semantic",
                        scroll_filter=Filter(must=[
                            FieldCondition(key="content", match=_MatchText(text=query))
                        ]),
                        limit=5,
                        with_payload=True,
                        with_vectors=False,
                    )
                    _recall_results = [
                        {k: v for k, v in pt.payload.items()
                         if k not in ("_vector", "embedding")}
                        for pt in _scroll_pts
                    ]
                except Exception as _recall_err:
                    import logging as _rl
                    _rl.getLogger(__name__).warning("memory_recall error: %s", _recall_err)
                if _recall_results:
                    return {
                        "status": "ok",
                        "found": True,
                        "query": query,
                        "count": len(_recall_results),
                        "results": _recall_results,
                    }
                return {
                    "status": "ok",
                    "found": False,
                    "query": query,
                    "message": f"No memory entry found containing '{query}'.",
                }

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

        if domain == "memory_synthesise":
            from memory.synthesis import run_synthesis
            return await run_synthesis(self.qdrant, cog=self.cog)

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

        if domain == "wallet_watchlist":
            _sov_wallet = os.environ.get("SOV_WALLET_URL", "http://sov-wallet:3001")
            _wt_token   = os.environ.get("WALLET_INTERNAL_TOKEN", "")
            _wt_headers = {"Content-Type": "application/json", "X-Wallet-Token": _wt_token}

            if name == "wallet_list_addresses":
                async with __import__("httpx").AsyncClient(timeout=10.0) as _wc:
                    _wr = await _wc.get(f"{_sov_wallet}/watchlist")
                if _wr.status_code != 200:
                    return {"status": "error", "error": f"sov-wallet HTTP {_wr.status_code}"}
                _data = _wr.json()
                addrs = _data.get("addresses", [])
                return {
                    "status": "ok",
                    "count": len(addrs),
                    "addresses": [
                        {"label": a.get("label", "?"), "value": a.get("value", "?"),
                         "chain": a.get("metadata", {}).get("chain", "?"),
                         "harness": a.get("metadata", {}).get("harness", [])}
                        for a in addrs
                    ],
                }

            if name == "wallet_portfolio":
                async with __import__("httpx").AsyncClient(timeout=15.0) as _wc:
                    _wr = await _wc.get(f"{_sov_wallet}/portfolio")
                if _wr.status_code == 503:
                    return {"status": "pending", "message": "Portfolio snapshot not yet available — try again in a moment"}
                if _wr.status_code != 200:
                    return {"status": "error", "error": f"sov-wallet HTTP {_wr.status_code}"}
                snap = _wr.json().get("snapshot", {})
                return {
                    "status":    "ok",
                    "totals":    snap.get("totals", {}),
                    "timestamp": snap.get("timestamp", ""),
                    "prices":    snap.get("prices", {}),
                }

            if name == "wallet_check_address":
                _addr = (delegation or {}).get("target") or (specialist or {}).get("address") or ""
                if not _addr:
                    return {"status": "error", "error": "address required"}
                async with __import__("httpx").AsyncClient(timeout=15.0) as _wc:
                    _wr = await _wc.post(f"{_sov_wallet}/check",
                                         json={"address": _addr}, headers=_wt_headers)
                return _wr.json() if _wr.status_code == 200 else {"status": "error", "error": f"HTTP {_wr.status_code}"}

            if name == "wallet_add_address":
                _sp = specialist or {}
                _addr = (delegation or {}).get("target") or _sp.get("address") or _sp.get("value") or ""
                _label = _sp.get("label") or (delegation or {}).get("label", "")
                _chain = _sp.get("chain", "eth")
                _harness = _sp.get("harness", ["portfolio", "a2a"])
                if not _addr:
                    return {"status": "error", "error": "address required"}
                async with __import__("httpx").AsyncClient(timeout=10.0) as _wc:
                    _wr = await _wc.post(f"{_sov_wallet}/watchlist",
                                         json={"value": _addr, "label": _label, "chain": _chain,
                                               "harness": _harness},
                                         headers=_wt_headers)
                return _wr.json() if _wr.status_code == 200 else {"status": "error", "error": f"HTTP {_wr.status_code}"}

            if name == "wallet_remove_address":
                _addr = (delegation or {}).get("target") or (specialist or {}).get("address") or ""
                if not _addr:
                    return {"status": "error", "error": "address required"}
                async with __import__("httpx").AsyncClient(timeout=10.0) as _wc:
                    _wr = await _wc.delete(f"{_sov_wallet}/watchlist/{_addr}", headers=_wt_headers)
                return _wr.json() if _wr.status_code == 200 else {"status": "error", "error": f"HTTP {_wr.status_code}"}

            if name == "wallet_update_address":
                _sp = specialist or {}
                _addr = (delegation or {}).get("target") or _sp.get("address") or ""
                _label = _sp.get("label") or (delegation or {}).get("label", "")
                if not _addr:
                    return {"status": "error", "error": "address required"}
                if not _label:
                    return {"status": "error", "error": "new label required"}
                async with __import__("httpx").AsyncClient(timeout=10.0) as _wc:
                    _wr = await _wc.patch(f"{_sov_wallet}/watchlist/{_addr}",
                                          json={"label": _label}, headers=_wt_headers)
                if _wr.status_code == 200:
                    _res = _wr.json()
                    _res["address"] = _addr
                    return _res
                return {"status": "error", "error": f"HTTP {_wr.status_code}"}

            return {"error": f"Unknown wallet_watchlist action: {name}"}

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

            # AUTH_PROFILES hot-reload removed — BrowserAdapter deleted (Phase 1 adapter removal).
            # New profiles are applied on the next sovereign-core restart.

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

        # ── Self-improvement harness — monitoring domain ──────────────────────────
        # All operations are LOW tier, read/observe only. Proposals are stored in
        # prospective memory with pending_approval status — Director must approve
        # before any corrective action is routed to another harness.

        if domain == "monitoring":
            op = action.get("operation", "")
            from monitoring.self_improvement import (
                run_manual_observe, list_pending_proposals, get_baseline_report,
            )
            if op == "observe":
                _app_state = getattr(self, "app_state", None)
                return await run_manual_observe(self.qdrant, self.cog, self.ledger, _app_state)
            if op == "proposals":
                return await list_pending_proposals(self.qdrant)
            if op == "baseline":
                return await get_baseline_report(self.qdrant)
            return {"error": f"Unknown monitoring operation: {op}"}

        if domain == "dev_harness":
            op = action.get("operation", "")
            # ── Dev-Harness phases ─────────────────────────────────────────────
            # Phase 1 (analyse) auto-chains Phase 2→3 when gate != APPROVE.
            # Phase 4 (approve) generates CC runsheet; Director pastes to CC.
            # Sovereign never self-modifies — runsheet is the execution artefact.
            dh = self._get_dev_harness()
            _dh_target = (delegation or {}).get("target") or ""
            if op == "analyse":
                # trigger comes from the action dict — set by the scheduler step's
                # params field ({"trigger": "nightly"}) or defaulted to "explicit"
                # for Director-initiated runs. Never inferred from message content.
                _trigger = action.get("trigger", "explicit")
                # Pre-flight self-awareness snapshots — LOW tier, introspective.
                # Called via _dispatch_inner directly; never re-enters handle_chat.
                _skill_snap   = await self._dispatch_inner({"domain": "portal", "name": "skill_status"}, payload={})
                _harness_snap = await self._dispatch_inner({"domain": "portal", "name": "harness_status"}, payload={})
                _ledger = getattr(self, "ledger", None)
                if _ledger:
                    _ledger.append("portal_read", "introspective", {"tier": "LOW", "intent": "skill_status",   "source": "dev_harness"})
                    _ledger.append("portal_read", "introspective", {"tier": "LOW", "intent": "harness_status", "source": "dev_harness"})
                return await dh.run_phase1(trigger=_trigger, skill_snapshot=_skill_snap, harness_snapshot=_harness_snap)
            if op == "status":
                return await dh.run_status()
            if op == "approve":
                # ── Pre-flight validation before Phase 4 ──────────────────────
                # _quick_classify only extracts the 8-char hex session_id_short from
                # the message — it cannot do async Qdrant lookups. Validation happens
                # here (async context) before the runsheet is generated.
                # Two checks:
                #   1. WM checkpoint exists for this session_id_short (source of truth)
                #   2. Prospective plan entry exists with status=pending_director_approval
                #      (belt-and-suspenders — guards against stale/typo IDs)
                if not _dh_target:
                    return {
                        "success": False,
                        "error": "No session ID provided. Usage: 'approve dev fix {id}'",
                    }
                _cp_pre = await dh._load_checkpoint_by_session(_dh_target)
                if _cp_pre is None:
                    return {
                        "success": False,
                        "error": (
                            f"No active dev harness session found matching '{_dh_target}'. "
                            "Run 'dev analyse' to start a new session, "
                            "or 'dev status' to check current state."
                        ),
                    }
                # Prospective plan check — session must be at pending_director_approval
                _prospective_valid = False
                try:
                    from execution.adapters.qdrant import PROSPECTIVE
                    from qdrant_client.models import Filter, FieldCondition, MatchValue
                    _p_res, _ = await dh.qdrant.archive_client.scroll(
                        collection_name=PROSPECTIVE,
                        scroll_filter=Filter(must=[
                            FieldCondition(key="_dev_plan",        match=MatchValue(value=True)),
                            FieldCondition(key="session_id_short", match=MatchValue(value=_dh_target)),
                            FieldCondition(key="status",           match=MatchValue(value="pending_director_approval")),
                        ]),
                        limit=1,
                        with_payload=False,
                        with_vectors=False,
                    )
                    _prospective_valid = len(_p_res) > 0
                except Exception as _pf_err:
                    # Qdrant archive unavailable — log and fall through on WM checkpoint alone
                    logger.warning(
                        "dev_harness approve: prospective pre-flight query failed: %s — "
                        "proceeding on WM checkpoint only",
                        _pf_err,
                    )
                    _prospective_valid = True  # degrade gracefully; run_phase4 re-validates
                if not _prospective_valid:
                    return {
                        "success": False,
                        "error": (
                            f"Session '{_dh_target}' has no pending approval in prospective memory. "
                            "It may have already been approved or rejected, "
                            "or Phase 3 did not complete. Run 'dev status' to check."
                        ),
                    }
                return await dh.run_phase4(_dh_target)
            if op == "reject":
                return await dh.run_reject(_dh_target)
            if op == "verify":
                return await dh.run_verify(_dh_target)
            if op == "clear":
                _cleared = await dh.clear_checkpoint()
                return {
                    "success": True,
                    "deleted": _cleared,
                    "message": f"Dev harness session cleared ({_cleared} point(s) removed).",
                }
            return {"error": f"Unknown dev_harness operation: {op}"}

        if domain == "portal":
            op = action.get("name", action.get("operation", ""))
            if op == "skill_status":
                skill_summary = getattr(getattr(self, "app_state", None), "skill_summary", None) or {}
                if not skill_summary:
                    skill_summary = getattr(getattr(self, "_app_state", None), "skill_summary", None) or {}
                total_ops = 0
                result_skills = []
                for specialist, names in skill_summary.items():
                    for name in names:
                        result_skills.append({"name": name, "specialist": specialist})
                return {
                    "status": "ok",
                    "skill_count": sum(len(v) for v in skill_summary.values()),
                    "skills": result_skills,
                    "summary": skill_summary,
                }
            if op == "harness_status":
                from execution.adapters.qdrant import WORKING
                harness_flags = {
                    "developer_harness": "_developer_harness_checkpoint",
                    "self_improvement":  "_self_improvement_session",
                    "skill_harness":     "_skill_harness_checkpoint",
                }
                harness_states = {}
                try:
                    offset = None
                    while True:
                        result_pts, next_offset = await self.qdrant.client.scroll(
                            collection_name=WORKING, limit=100, offset=offset,
                            with_payload=True, with_vectors=False,
                        )
                        for r in result_pts:
                            p = r.payload or {}
                            for hkey, flag in harness_flags.items():
                                if p.get(flag) and hkey not in harness_states:
                                    harness_states[hkey] = {
                                        "active": True,
                                        "current_step": p.get("current_step"),
                                        "last_checkpoint_ts": p.get("last_checkpoint_ts"),
                                    }
                        if next_offset is None:
                            break
                        offset = next_offset
                except Exception as e:
                    logger.warning("portal harness_status scroll failed: %s", e)
                statuses = []
                for hkey, flag in harness_flags.items():
                    if hkey in harness_states:
                        statuses.append({"harness": hkey, **harness_states[hkey]})
                    else:
                        statuses.append({"harness": hkey, "active": False})
                return {"status": "ok", "harnesses": statuses}
            return {"error": f"Unknown portal operation: {op}"}

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

    # ── Outcome write-back (Beta-2 Task 6) ───────────────────────────────────

    async def _outcome_write_back(
        self,
        intent: str,
        action: dict,
        result: dict,
        routing_source: str,
        delegation: dict,
    ) -> None:
        """Write execution outcome to episodic memory and update semantic intent stats.

        Fires via asyncio.create_task() from _dispatch() — non-blocking, never raises.

        Always writes an episodic entry.
        Updates semantic:intent:{slug} success_count / failure_count /
        consecutive_failure_count via set_payload() — lightweight, no re-embedding.
        Resets consecutive_failure_count to 0 on success.
        Fires _write_failure_prospective() at exactly consecutive_failure_count == 3.
        """
        if not self.qdrant:
            return
        try:
            import uuid as _uuid
            ts = datetime.now(timezone.utc).isoformat()
            domain = action.get("domain", "")

            # Determine outcome from result
            is_success = (
                "error" not in result
                and result.get("status") not in ("error", "failed", "blocked")
                and result.get("success", True) is not False
                and not result.get("requires_security_confirmation")
                and not result.get("requires_confirmation")
            )
            outcome = "success" if is_success else "failure"

            # 1. Episodic entry — always
            await self.qdrant.store(
                content=(
                    f"OUTCOME [{ts[:10]}] intent={intent!r} domain={domain!r} "
                    f"outcome={outcome!r} routing_source={routing_source!r}"
                ),
                metadata={
                    "type":              "episodic",
                    "domain":            domain,
                    "_key":              f"episodic:outcome:{ts[:10]}:{intent}",
                    "intent":            intent,
                    "outcome":           outcome,
                    "routing_source":    routing_source,
                    "execution_confirmed": is_success,
                    "timestamp":         ts,
                },
                collection=EPISODIC,
                writer="sovereign-core",
            )

            # 2. Update semantic:intent:{slug} counters — no re-embedding
            slug    = intent.replace("_", "-")
            sem_key = f"semantic:intent:{slug}"
            sem     = await self.qdrant.retrieve_by_key(sem_key)
            if not sem:
                return

            point_id   = _uuid.UUID(sem["point_id"])
            collection = sem.get("collection", SEMANTIC)
            success_count = int(sem.get("success_count", 0))
            failure_count = int(sem.get("failure_count", 0))
            consec_fail   = int(sem.get("consecutive_failure_count", 0))

            new_payload: dict = {"last_outcome": outcome, "last_outcome_ts": ts}
            if is_success:
                success_count += 1
                consec_fail    = 0
                new_payload["success_count"]            = success_count
                new_payload["consecutive_failure_count"] = 0
            else:
                failure_count += 1
                consec_fail   += 1
                new_payload["failure_count"]            = failure_count
                new_payload["consecutive_failure_count"] = consec_fail
                new_payload["last_failure_ts"]          = ts

            await self.qdrant.archive_client.set_payload(
                collection_name=collection,
                payload=new_payload,
                points=[point_id],
            )

            # 3. Consecutive failure alert — fire prospective + Telegram at EXACTLY 3
            if not is_success and consec_fail == 3:
                import asyncio as _asyncio_ob
                _asyncio_ob.create_task(self._write_failure_prospective(
                    intent=intent,
                    slug=slug,
                    sem_key=sem_key,
                    ts=ts,
                    total_failures=failure_count,
                ))

        except Exception as exc:
            import logging as _lg
            _lg.getLogger(__name__).debug("_outcome_write_back: failed (non-fatal): %s", exc)

    async def _write_failure_prospective(
        self,
        intent: str,
        slug: str,
        sem_key: str,
        ts: str,
        total_failures: int,
    ) -> None:
        """Write prospective failure alert and send Telegram at consecutive_failure_count == 3.

        Dedup gate: checks for existing prospective:failure_alert:{slug} entry before writing.
        Non-blocking, never raises.
        """
        if not self.qdrant:
            return
        try:
            alert_key = f"prospective:failure_alert:{slug}"
            existing  = await self.qdrant.retrieve_by_key(alert_key)
            if existing:
                return   # active alert already in place for this intent

            content = (
                f"FAILURE ALERT: intent {intent!r} has failed 3 consecutive times. "
                f"Last failure: {ts[:10]}. Total failures recorded: {total_failures}. "
                f"Semantic entry: {sem_key}. Review and resolve before the alert clears."
            )
            await self.qdrant.store(
                content=content,
                metadata={
                    "type":                     "prospective",
                    "domain":                   "failure_alert",
                    "_key":                     alert_key,
                    "intent":                   intent,
                    "last_failure_ts":          ts,
                    "total_failures":           total_failures,
                    "consecutive_failure_count": 3,
                    "status":                   "pending_review",
                    "timestamp":                ts,
                },
                collection=PROSPECTIVE,
                writer="sovereign-core",
            )

            # Telegram notification — same pattern as task_scheduler._notify_telegram
            import httpx as _httpx
            _token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            _chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
            if _token and _chat_id:
                msg = (
                    f"⚠️ *Failure Alert*\n"
                    f"Intent `{intent}` has failed *3 consecutive times*.\n"
                    f"Last failure: {ts[:10]}\n"
                    f"Total failures: {total_failures}\n"
                    f"Prospective entry written at `{alert_key}`."
                )
                try:
                    async with _httpx.AsyncClient(timeout=10.0) as _hx:
                        await _hx.post(
                            f"https://api.telegram.org/bot{_token}/sendMessage",
                            json={"chat_id": _chat_id, "text": msg, "parse_mode": "Markdown"},
                        )
                except Exception:
                    pass   # Telegram failure must never propagate

        except Exception as exc:
            import logging as _lg
            _lg.getLogger(__name__).debug("_write_failure_prospective: failed (non-fatal): %s", exc)
