"""
Sovereign runtime configuration loader.

Reads /home/sovereign/governance/sovereign-config.yaml at startup and exposes a
`cfg` singleton.  Never raises — every missing or malformed key falls back to the
hardcoded default that was previously baked into the source file.  A Python WARNING
is emitted for missing keys; startup continues regardless.
"""

import logging
import os

_log = logging.getLogger(__name__)

_CONFIG_PATH = "/home/sovereign/governance/sovereign-config.yaml"

# ── Hardcoded fallbacks — must match current source defaults exactly ────────────
_DEFAULTS: dict = {
    "models": {
        "classifier_model": "llama3.1:8b-instruct-q4_K_M",
        "primary_inference_model": "llama3.1:8b-instruct-q4_K_M",
        "embed_model": "nomic-embed-text",
    },
    "cognitive_loop": {
        "memory_routing_shadow_mode": True,
        "nanobot_script_pass_multiplier": 3,
        "skill_search_pass_multiplier": 6,
        "skills_domain_min_total_timeout_s": 240.0,
    },
    "memory": {
        "startup_preload_bytes": 2 * 1024 * 1024 * 1024,   # 2 GB
        "startup_preload_per_collection": 50,
        "sov_uuid_namespace": "7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d",
    },
    "thresholds": {
        "confidence": 0.75,
        "memory_routing": 0.85,
        "complexity_routing": 0.50,
        "operational_routing_penalty": 0.20,
        "dev_harness_escalation_score": 50,
        "vram_used_mb_warning": 7500,
        "qdrant_total_points_warning": 1000000,
        "gap_auto_create": 0.50,
    },
    "timeouts": {
        "pass_s": 30.0,
        "total_pipeline_s": 120.0,
        "embed_generation_s": 30.0,
        "mip_key_title_gen_s": 10.0,
        "nc_mail_s": 59,
        "gateway_dispatch_s": 180.0,
        "gateway_chunk_debounce_s": 1.5,
        "health_probe_s": 5.0,
        "ollama_inference_probe_s": 30.0,
        "dev_harness_analysis_s": 300.0,
        "dev_harness_classifier_inference_s": 60.0,
        "nanobot_task_s": 25,
        "nanobot_dsl_exec_s": 20,
        "nanobot_shell_script_s": 30,
        "nanobot_subprocess_launch_s": 120,
        "nanobot_config_load_s": 120,
        "task_scheduler_telegram_s": 10.0,
    },
    "limits": {
        "payload_dispatch_max_bytes": 25 * 1024 * 1024,    # 25 MB
        "rss_headlines_per_briefing": 10,
        "wm_scroll_max": 500,
        "skill_search_candidates": 10,
        "memory_recall_max": 100,
        "file_list_recursive_max": 2000,
        "nc_mail_list_default": 10,
        "nc_mail_unread_max": 5,
        "dev_harness_findings_query": 100,
        "dev_harness_findings_in_prompt": 20,
        "prospective_scroll_max": 200,
        "telegram_message_max_chars": 4000,
        "nanobot_imap_list_default": 10,
        "nanobot_fs_list_default": 20,
        "nanobot_rss_list_default": 20,
    },
    "intervals": {
        "health_check_s": 6 * 3600,    # 21600
        "archive_sync_s": 3600,
        "task_scheduler_loop_s": 60,
        "task_scheduler_startup_delay_s": 30,
    },
    "paths": {
        "skills_dir": "/home/sovereign/skills",
        "personas_dir": "/home/sovereign/personas",
        "skill_checksums": "/home/sovereign/security/skill-checksums.json",
        "audit_promotions_log": "/home/sovereign/audit/memory-promotions.jsonl",
        "portal_html": "/home/sovereign/portal/sovereign-portal.html",
        "governance_json_container": "/app/governance/governance.json",
    },
    "learning_harness": {
        "processing_hours_utc": [15, 16, 17],
        "chunk_chars": 6000,
        "context_chars": 2000,
        "max_doc_array": 500,
        "scroll_batch": 200,
        "max_cycles": 10,
        "max_file_bytes": 200_000,
    },
    "portal": {
        "log_containers": ["sovereign-core", "gateway", "nanobot-01"],
        "log_tail_each_container": 34,
        "log_heartbeat_s": 25.0,
        "memory_preview_max": 100,
        "api_limit_default": 20,
        "api_limit_max": 50,
        "memory_collection_preview_max": 50,
    },
    "gateway": {
        "history_max_turns": 10,
        "chunk_debounce_s": 1.5,
    },
    "nanobot": {
        "task_timeout_ms_default": 25000,
        "imap_list_default": 10,
        "fs_list_default": 20,
        "rss_list_default": 20,
        "credential_proxy_url": "http://sovereign-core:8000/credential_proxy",
    },
}


class _Section:
    """Attribute-access wrapper for one config section.

    Returns the YAML value when present; warns and returns the hardcoded
    default when a key is missing.  Never raises AttributeError for any key
    that has a default entry.
    """

    def __init__(self, section_name: str, data: dict, defaults: dict) -> None:
        self._section = section_name
        self._data = data
        self._defaults = defaults

    def __getattr__(self, key: str):
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._data:
            return self._data[key]
        if key in self._defaults:
            _log.warning(
                "sovereign-config: missing key '%s.%s' — using default %r",
                self._section,
                key,
                self._defaults[key],
            )
            return self._defaults[key]
        raise AttributeError(
            f"sovereign-config: no key '{self._section}.{key}' and no default defined"
        )

    def get(self, key: str, fallback=None):
        try:
            return getattr(self, key)
        except AttributeError:
            return fallback


class SovereignConfig:
    """Top-level config object.  Attribute access returns a `_Section` wrapper."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, section: str):
        if section.startswith("_"):
            raise AttributeError(section)
        section_data = self._data.get(section, {})
        section_defaults = _DEFAULTS.get(section, {})
        return _Section(section, section_data, section_defaults)


def _load() -> SovereignConfig:
    """Load sovereign-config.yaml; never raises; returns SovereignConfig with defaults on failure."""
    try:
        import yaml  # noqa: PLC0415 — intentional late import; avoids hard dep at module load
    except ImportError:
        _log.error(
            "PyYAML not installed — sovereign-config not loaded; using all defaults"
        )
        return SovereignConfig({})

    if not os.path.exists(_CONFIG_PATH):
        _log.warning(
            "sovereign-config: %s not found — using all defaults", _CONFIG_PATH
        )
        return SovereignConfig({})

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            _log.error(
                "sovereign-config: %s parsed to %s (expected dict) — using all defaults",
                _CONFIG_PATH,
                type(raw).__name__,
            )
            return SovereignConfig({})
        _log.info("sovereign-config: loaded from %s", _CONFIG_PATH)
        return SovereignConfig(raw)
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "sovereign-config: failed to load %s — %s; using all defaults",
            _CONFIG_PATH,
            exc,
        )
        return SovereignConfig({})


# Module-level singleton — runs exactly once at first import
cfg: SovereignConfig = _load()
