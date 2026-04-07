"""Static definitions for intent and skill semantic memory seeds.

Written to the SEMANTIC Qdrant collection at startup (idempotent).
Each entry: _key = semantic:intent:{slug}, pointing to the canonical
trigger location for that intent, skill, or harness.

build_intent_seeds()  — one entry per INTENT_ACTION_MAP key
build_skill_seeds()   — one entry per installed RAID skill
build_harness_seeds() — one entry per whole harness system
"""

import os
import re
from datetime import datetime, timezone

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower().strip()).strip("-")[:48]


# Domain → (owner, trigger_point)
_DOMAIN_META: dict[str, tuple[str, str]] = {
    "docker":            ("engine",  "engine:broker_adapter"),
    "webdav":            ("nanobot", "nanobot:sovereign-nextcloud-fs"),
    "caldav":            ("nanobot", "nanobot:openclaw-nextcloud"),
    "notes":             ("nanobot", "nanobot:openclaw-nextcloud"),
    "ncfs":              ("nanobot", "nanobot:sovereign-nextcloud-fs"),
    "ncingest":          ("nanobot", "nanobot:sovereign-nextcloud-ingest"),
    "session":           ("engine",  "engine:cognitive_skill"),
    "memory_curate":     ("engine",  "engine:cognitive_skill"),
    "mail":              ("nanobot", "nanobot:nc-mail"),
    "ollama":            ("engine",  "engine:cognition_module"),
    "browser":           ("nanobot", "nanobot:sovereign-browser"),
    "feeds":             ("nanobot", "nanobot:rss-digest"),
    "security":          ("engine",  "engine:github_adapter"),
    "github":            ("engine",  "engine:github_adapter"),
    "skills":            ("harness", "harness:skill_lifecycle"),
    "memory":            ("engine",  "engine:qdrant_adapter"),
    "memory_index":      ("engine",  "engine:qdrant_adapter"),
    "memory_synthesise": ("bespoke", "bespoke:memory/synthesis.py"),
    "wallet":            ("engine",  "engine:wallet_adapter"),
    "wallet_watchlist":  ("engine",  "engine:sov-wallet_service"),
    "scheduler":         ("engine",  "engine:task_scheduler"),
    "nanobot":           ("engine",  "engine:nanobot_adapter"),
    "browser_config":    ("bespoke", "bespoke:engine:browser_config_handler"),
    "monitoring":        ("harness", "harness:self_improvement"),
    "dev_harness":       ("harness", "harness:dev_harness"),
    "portal":            ("engine",  "engine:portal_handler"),
}


def build_intent_seeds(intent_action_map: dict) -> list[dict]:
    """Generate semantic seed dicts for every entry in INTENT_ACTION_MAP."""
    seeds = []
    for intent, action in intent_action_map.items():
        domain    = action.get("domain", "")
        operation = action.get("operation", "")
        name      = action.get("name", "")
        owner, trigger_point = _DOMAIN_META.get(domain, ("engine", f"engine:{domain}"))
        slug    = _slug(intent.replace("_", "-"))
        key     = f"semantic:intent:{slug}"
        content = (
            f"Intent: {intent}. Domain: {domain}. Operation: {operation}. "
            f"Action name: {name or operation}. "
            f"Sovereign capability — dispatches via {trigger_point}."
        )
        seeds.append({
            "seed_id":   f"intent_seed_v1_{intent}",
            "key":       key,
            "title":     f"{intent} — {domain}:{operation}",
            "content":   content,
            "domain":    domain,
            "extra_meta": {
                "intent_signals": [intent.replace("_", " "), intent],
                "action":         f"{domain}:{operation}:{name or operation}",
                "trigger_point":  trigger_point,
                "owner":          owner,
                "success_count":  0,
                "failure_count":  0,
            },
        })
    return seeds


def build_skill_seeds(skills_dir: str = "/home/sovereign/skills") -> list[dict]:
    """Generate semantic seed dicts for all installed RAID skills."""
    seeds = []
    if not os.path.isdir(skills_dir):
        return seeds
    for skill_name in sorted(os.listdir(skills_dir)):
        skill_path = os.path.join(skills_dir, skill_name, "SKILL.md")
        if not os.path.isfile(skill_path):
            continue
        slug = _slug(skill_name)
        key  = f"semantic:intent:{slug}"
        seeds.append({
            "seed_id":   f"skill_seed_v1_{skill_name}",
            "key":       key,
            "title":     f"Skill: {skill_name}",
            "content":   (
                f"Nanobot skill: {skill_name}. "
                f"Installed at /home/sovereign/skills/{skill_name}/SKILL.md. "
                f"Executed by nanobot-01 via python3_exec or DSL operations."
            ),
            "domain":    "skills",
            "extra_meta": {
                "intent_signals": [skill_name, skill_name.replace("-", " ")],
                "action":         f"nanobot:skill:{skill_name}",
                "trigger_point":  f"nanobot:{skill_name}",
                "owner":          "nanobot",
                "success_count":  0,
                "failure_count":  0,
            },
        })
    return seeds


def build_harness_seeds() -> list[dict]:
    """Generate semantic seed dicts for the 3 primary harnesses as whole systems."""
    harnesses = [
        {
            "name": "skill-harness",
            "description": (
                "Multi-step skill lifecycle harness in execution/engine.py. "
                "Steps: search → list_candidates → review_candidate → install → clear. "
                "Manages skill discovery, pre-scan security review, "
                "and Director-confirmed installation with working_memory checkpointing."
            ),
            "trigger_point": "harness:skill_lifecycle",
        },
        {
            "name": "SI-harness",
            "description": (
                "Self-improvement harness in monitoring/self_improvement.py. "
                "Daily observe loop, baseline and anomaly detection, "
                "proposal generation with Director approval gate. "
                "Primary autonomy boundary — never self-modifies without Director confirm."
            ),
            "trigger_point": "harness:self_improvement",
        },
        {
            "name": "dev-harness",
            "description": (
                "4-phase code quality harness in dev_harness module. "
                "Phases: Analyse (pylint+semgrep+boundary_scanner) → Classify → Plan → Execute. "
                "LLM advisory via Ollama/Claude. CC runsheet HITL handoff. "
                "Never self-modifies. Nightly cron at 15:00 UTC (03:00 NZST)."
            ),
            "trigger_point": "harness:dev_harness",
        },
    ]
    seeds = []
    for h in harnesses:
        slug = _slug(h["name"])
        key  = f"semantic:intent:{slug}"
        seeds.append({
            "seed_id":   f"harness_seed_v1_{h['name']}",
            "key":       key,
            "title":     f"Harness: {h['name']}",
            "content":   h["description"],
            "domain":    "harness",
            "extra_meta": {
                "intent_signals": [h["name"], h["name"].replace("-", " ")],
                "action":         f"harness:{h['name']}",
                "trigger_point":  h["trigger_point"],
                "owner":          "harness",
                "success_count":  0,
                "failure_count":  0,
            },
        })
    return seeds


def make_skill_semantic_seed(skill_name: str, specialists: list, tier: str) -> dict:
    """Build a single skill seed dict for a newly installed skill.

    Called by the Skill Harness install step immediately after load() succeeds.
    """
    slug = _slug(skill_name)
    return {
        "seed_id":   f"skill_seed_v1_{skill_name}",
        "key":       f"semantic:intent:{slug}",
        "title":     f"Skill: {skill_name}",
        "content":   (
            f"Nanobot skill: {skill_name}. "
            f"Installed at /home/sovereign/skills/{skill_name}/SKILL.md. "
            f"Active for specialists: {', '.join(specialists)}. "
            f"Governance tier: {tier}. "
            f"Executed by nanobot-01 via python3_exec or DSL operations."
        ),
        "domain":    "skills",
        "extra_meta": {
            "intent_signals": [skill_name, skill_name.replace("-", " ")],
            "action":         f"nanobot:skill:{skill_name}",
            "trigger_point":  f"nanobot:{skill_name}",
            "owner":          "nanobot",
            "success_count":  0,
            "failure_count":  0,
        },
    }
