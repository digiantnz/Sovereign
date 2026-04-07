"""Component Registry — deterministic UUID5 sov_ids for all sovereign system components.

Every component (cognitive passes, short-circuit domains, adapters, harnesses,
skills, gateway, containers, modules) receives a stable sov_id derived from:

    uuid5(_SOV_NS, "component:{name}")

The registry is seeded to Qdrant at startup (idempotent via _backfill_seed_id):
  - Individual entries: semantic:component:{name}   → SEMANTIC collection (searchable)
  - Aggregate index:    meta:system:component-registry → META collection (lookup)

Every component entry uses component_type (not entity_type — never both on one entry).
Every component entry carries parent_sov_id — the hierarchy is:

    sovereign root entity (semantic:entity:sovereign, SOVEREIGN_ROOT_ID)
    ├── Sovereign Server (003) — hardware entity; parent of all local containers
    │   ├── container-sovereign-core
    │   │   ├── cognitive passes, short-circuit domains, adapters, harnesses, modules
    │   ├── container-nanobot-01
    │   │   └── skills
    │   ├── container-gateway
    │   │   └── gateway-telegram
    │   └── all other local containers (qdrant, ollama, nextcloud stack, etc.)
    ├── node04 (005) — hardware entity; parent of external service containers
    │   ├── container-a2a-browser
    │   └── container-a2a-whisper
    └── Start9 Server (006) — hardware entity; parent of BTC infrastructure containers

Hardware entity sov_ids are defined in entity_registry.py. The constants below
are duplicated here to avoid a circular import.

Standing Design Order 8: New component created → sov_id assigned + semantic
memory entry written at that time. This module is the canonical source for all
pre-defined system component sov_ids.
"""
import uuid
from datetime import datetime, timezone

# Same namespace used for Universal Item Index and all sov_id derivations.
_SOV_NS = uuid.UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d")

# Hardcoded sovereign root sov_id.
# Within the component registry, sovereign root is the ONLY component-class
# entry with parent_sov_id=None. Entity-class entries (entity_registry.py)
# may also carry parent_sov_id=None for external entities — that is permitted.
SOVEREIGN_ROOT_ID = "00000000-0000-0000-0000-000000000001"

# Hardware entity sov_ids — physical parents for container re-parenting.
# Canonical source: entity_registry.py ENTITY_SOV_IDS.
# Duplicated here to avoid circular import; values must stay in sync.
_HW_SOVEREIGN_SERVER = "00000000-0000-0000-0000-000000000003"  # Sovereign Server
_HW_NODE01           = "00000000-0000-0000-0000-000000000004"  # node01
_HW_NODE04           = "00000000-0000-0000-0000-000000000005"  # node04
_HW_START9           = "00000000-0000-0000-0000-000000000006"  # Start9 Server


def sov_id_for(name: str) -> str:
    """Return the deterministic UUID5 sov_id for a named component."""
    return str(uuid.uuid5(_SOV_NS, f"component:{name}"))


# ── Parent sov_id mapping ──────────────────────────────────────────────────────
# Computed at module load — sov_id_for() is deterministic so this is safe.
# Default by component_type; name-specific overrides in _PARENT_NAME_OVERRIDES.

_CORE_ID     = sov_id_for("container-sovereign-core")
_NB_ID       = sov_id_for("container-nanobot-01")
_GATEWAY_ID  = sov_id_for("container-gateway")

_PARENT_BY_TYPE: dict[str, str] = {
    "container":      _HW_SOVEREIGN_SERVER,  # default: Sovereign Server; see overrides below
    "cognitive_pass": _CORE_ID,
    "adapter":        _CORE_ID,
    "harness":        _CORE_ID,
    "module":         _CORE_ID,
    "skill":          _NB_ID,
    "gateway":        _GATEWAY_ID,
}

# Per-name overrides (name → parent_sov_id) for containers not on Sovereign Server.
_PARENT_NAME_OVERRIDES: dict[str, str] = {
    "container-a2a-browser": _HW_NODE04,   # runs on node04
    "container-a2a-whisper": _HW_NODE04,   # runs on node04
}


def parent_sov_id_for(name: str, component_type: str) -> str:
    """Return the parent_sov_id for a named component.

    Guard: within the component registry, all components must resolve a parent
    sov_id. Only sovereign_entity class entries (entity_registry.py) may have
    parent_sov_id=None, and only when their relationship to the system is
    associative rather than hierarchical.
    """
    if name in _PARENT_NAME_OVERRIDES:
        return _PARENT_NAME_OVERRIDES[name]
    parent = _PARENT_BY_TYPE.get(component_type)
    if parent is None:
        raise ValueError(
            f"component_registry: cannot determine parent_sov_id for {name!r} "
            f"(type={component_type!r}). All components must resolve a parent. "
            "Add an explicit _PARENT_NAME_OVERRIDES entry or map the component_type "
            "in _PARENT_BY_TYPE. Only sovereign_entity class entries may have "
            "parent_sov_id=None."
        )
    return parent


# ── Component definitions ─────────────────────────────────────────────────────
# Each entry: name (slug), component_type, title, content, location, extras.
# "name" becomes both the sov_id seed and the key suffix: semantic:component:{name}

_COMPONENTS: list[dict] = [

    # ── Cognitive passes (6) ─────────────────────────────────────────────────
    {
        "name": "pass-1-orchestrator-classify",
        "component_type": "cognitive_pass",
        "title": "PASS 1 — Orchestrator classify",
        "content": (
            "PASS 1: intent classification by the orchestrator LLM. Determines intent, "
            "delegate_to specialist, and governance tier. Always runs locally. "
            "Pre-empted by _quick_classify for deterministic keyword paths. "
            "Loads memory context from Qdrant before LLM call."
        ),
        "location": "cognition/engine.py:orchestrator_classify",
    },
    {
        "name": "pass-2-security",
        "component_type": "cognitive_pass",
        "title": "PASS 2 — Security gate",
        "content": (
            "PASS 2: security agent evaluates the action before execution. "
            "Only invoked for HIGH tier actions or when the security scanner raises a flag. "
            "Can block execution unconditionally. Skipped when confirmed=True "
            "(double-confirmation IS the security gate for HIGH tier)."
        ),
        "location": "execution/engine.py:_dispatch_inner (PASS 2 block)",
    },
    {
        "name": "pass-3a-specialist-outbound",
        "component_type": "cognitive_pass",
        "title": "PASS 3a — Specialist outbound",
        "content": (
            "PASS 3a: specialist system prompt construction and payload extraction. "
            "Selects the correct specialist persona (research, business, security, etc.) "
            "and builds the structured payload for the executor. Externally routable — "
            "nanobot-01 skills run via this pass."
        ),
        "location": "cognition/engine.py:specialist_outbound",
    },
    {
        "name": "pass-3b-specialist-inbound",
        "component_type": "cognitive_pass",
        "title": "PASS 3b — Specialist inbound",
        "content": (
            "PASS 3b: interprets the raw executor result in context of the specialist's "
            "task. Always runs locally. All nanobot results are tagged _trust: untrusted_external "
            "and scanned before this pass. Normalises result_for_translator."
        ),
        "location": "cognition/engine.py:specialist_inbound",
    },
    {
        "name": "pass-4-orchestrator-evaluate",
        "component_type": "cognitive_pass",
        "title": "PASS 4 — Orchestrator evaluate",
        "content": (
            "PASS 4: evaluates the specialist result, determines memory_action "
            "(remember/forget/none), and packages result_for_translator. "
            "Always runs locally. Triggers async memory writes via asyncio.create_task."
        ),
        "location": "cognition/engine.py:orchestrator_evaluate",
    },
    {
        "name": "pass-5-translator",
        "component_type": "cognitive_pass",
        "title": "PASS 5 — Translator",
        "content": (
            "PASS 5: converts structured result_for_translator into plain English "
            "for the Director (Matt). Restricted input — only result_for_translator "
            "and conversation history are passed; no raw adapter output. "
            "_translator_sanitise() post-processes output to strip meta-commentary."
        ),
        "location": "cognition/engine.py:translator_pass",
    },

    # ── Short-circuit domains (10) ────────────────────────────────────────────
    {
        "name": "sc-ollama",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: ollama — conversational query",
        "content": (
            "ollama short-circuit domain: conversational queries routed directly to "
            "OllamaAdapter.ask_conversational() bypassing the full 5-pass loop. "
            "Result still passes through translator before reaching Director."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='ollama')",
        "short_circuit_domain": "ollama",
    },
    {
        "name": "sc-memory",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: memory — memory read/write",
        "content": (
            "memory short-circuit domain: memory store/retrieve operations bypass "
            "the full 5-pass loop. Handles remember_fact, memory_retrieve_key, "
            "memory_list_keys operations directly via QdrantAdapter."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='memory')",
        "short_circuit_domain": "memory",
    },
    {
        "name": "sc-browser",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: browser — web search and fetch",
        "content": (
            "browser short-circuit domain: web search and URL fetch routed directly "
            "to sovereign-browser nanobot skill bypassing full 5-pass loop. "
            "Routes to nanobot-01 via A2A 3.0 (python3_exec, browser.py script)."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='browser')",
        "short_circuit_domain": "browser",
    },
    {
        "name": "sc-scheduler",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: scheduler — task scheduling",
        "content": (
            "scheduler short-circuit domain: task scheduling operations (schedule_task, "
            "list_tasks, get_task, pause_task, cancel_task) bypass the full 5-pass loop. "
            "Routes to TaskScheduler directly."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='scheduler')",
        "short_circuit_domain": "scheduler",
    },
    {
        "name": "sc-browser-config",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: browser_config — auth profile writes",
        "content": (
            "browser_config short-circuit domain: configure_browser_auth writes "
            "authentication profile entries to browser-auth-profiles.yaml. "
            "Bypasses full 5-pass loop — deterministic YAML write."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='browser_config')",
        "short_circuit_domain": "browser_config",
    },
    {
        "name": "sc-feeds",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: feeds — RSS feed reads",
        "content": (
            "feeds short-circuit domain: RSS feed reads bypass the full 5-pass loop "
            "and route directly to nanobot-01 rss-digest skill. "
            "Note: RSS ingest (adding feeds) does NOT short-circuit — requires CEO LLM."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='feeds')",
        "short_circuit_domain": "feeds",
    },
    {
        "name": "sc-memory-index",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: memory_index — MIP key listing",
        "content": (
            "memory_index short-circuit domain: Memory Index Protocol key directory "
            "listing and two-step retrieval bypass the full 5-pass loop. "
            "Routes to QdrantAdapter.list_keys() and retrieve_by_key()."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='memory_index')",
        "short_circuit_domain": "memory_index",
    },
    {
        "name": "sc-wallet-watchlist",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: wallet_watchlist — watched address management",
        "content": (
            "wallet_watchlist short-circuit domain: ETH/BTC wallet address watchlist "
            "operations (list, add, remove, get, check) bypass the full 5-pass loop. "
            "Uses inline httpx calls to ETH/BTC RPC nodes."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='wallet_watchlist')",
        "short_circuit_domain": "wallet_watchlist",
    },
    {
        "name": "sc-wallet",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: wallet — wallet operations",
        "content": (
            "wallet short-circuit domain: sov-wallet operations (read config, get address, "
            "get ETH/BTC balance, get xpub) bypass the full 5-pass loop. "
            "Routes to WalletAdapter."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='wallet')",
        "short_circuit_domain": "wallet",
    },
    {
        "name": "sc-memory-synthesise",
        "component_type": "cognitive_pass",
        "subtype": "short_circuit",
        "title": "Short-circuit: memory_synthesise — episodic synthesis",
        "content": (
            "memory_synthesise short-circuit domain: nightly episodic memory synthesis "
            "runs bypass the full 5-pass loop. Calls memory.synthesis.run_synthesis() "
            "directly. Triggered by nightly cron (15:00 UTC = 03:00 NZST) or manual request."
        ),
        "location": "execution/engine.py:_dispatch_inner (domain=='memory_synthesise')",
        "short_circuit_domain": "memory_synthesise",
    },

    # ── Adapters (10) ─────────────────────────────────────────────────────────
    {
        "name": "adapter-broker",
        "component_type": "adapter",
        "title": "Adapter: DockerBrokerAdapter — system commands",
        "content": (
            "DockerBrokerAdapter: sole pathway to docker.sock via the docker-broker container. "
            "System commands whitelist only (ps, logs, stats, restart, inspect, rebuild, prune). "
            "All application I/O routes through nanobot-01, not the broker."
        ),
        "location": "execution/adapters/broker.py",
    },
    {
        "name": "adapter-nanobot",
        "component_type": "adapter",
        "title": "Adapter: NanobotAdapter — skill execution via A2A 3.0",
        "content": (
            "NanobotAdapter: A2A JSON-RPC 3.0 client to nanobot-01 (http://nanobot-01:8080). "
            "Primary skill execution pathway for all application skills. "
            "Handles CredentialProxy token forwarding in request context. "
            "All responses tagged _trust: untrusted_external before PASS 3b."
        ),
        "location": "execution/adapters/nanobot.py",
    },
    {
        "name": "adapter-ollama",
        "component_type": "adapter",
        "title": "Adapter: OllamaAdapter — local GPU LLM inference",
        "content": (
            "OllamaAdapter: HTTP client to ollama:11434 on ai_net. "
            "Model: llama3.1:8b-instruct-q4_K_M on RTX 3060 Ti (8GB VRAM). "
            "Used for all PASS 1/3/4/5 LLM calls and conversational queries. "
            "Separate ollama-embed service (CPU-only) handles embeddings."
        ),
        "location": "execution/adapters/ollama.py",
    },
    {
        "name": "adapter-grok",
        "component_type": "adapter",
        "title": "Adapter: GrokAdapter — Grok API fallback LLM",
        "content": (
            "GrokAdapter: Grok API (grok-3 model) used as LLM fallback when Ollama "
            "is unavailable or for tasks requiring external LLM capability. "
            "API key from secrets/grok.env. Governed by DCL escalation policy."
        ),
        "location": "execution/adapters/grok.py",
    },
    {
        "name": "adapter-claude",
        "component_type": "adapter",
        "title": "Adapter: ClaudeAdapter — Claude API for DCL escalation",
        "content": (
            "ClaudeAdapter: Anthropic Claude API used for Dev-Harness LLM advisory "
            "and DCL (Delegated Capability Layer) escalation. "
            "Highest capability tier — used only when Ollama and Grok are insufficient."
        ),
        "location": "execution/adapters/claude.py",
    },
    {
        "name": "adapter-github",
        "component_type": "adapter",
        "title": "Adapter: GitHubAdapter — GitHub API for skill search",
        "content": (
            "GitHubAdapter: GitHub Search API client used by the Skill Harness "
            "to search OpenClaw registry (github.com/openclaw/skills). "
            "PAT auth from AUTH_PROFILES. Primary skill discovery pathway."
        ),
        "location": "execution/adapters/github.py",
    },
    {
        "name": "adapter-qdrant",
        "component_type": "adapter",
        "title": "Adapter: QdrantAdapter — 7-collection sovereign memory",
        "content": (
            "QdrantAdapter: manages working_memory (qdrant:6333, tmpfs, ephemeral) and "
            "7 RAID collections in qdrant-archive:6333 (semantic, episodic, procedural, "
            "prospective, associative, relational, meta). Embeddings via ollama-embed. "
            "Two-tier memory with startup pre-warm and graceful shutdown promotion."
        ),
        "location": "execution/adapters/qdrant.py",
    },
    {
        "name": "adapter-signing",
        "component_type": "adapter",
        "title": "Adapter: SigningAdapter — Ed25519 ledger signing",
        "content": (
            "SigningAdapter: Ed25519 keypair holder for AuditLedger entry signing. "
            "Key loaded from /home/sovereign/keys/sovereign.key at startup. "
            "All ledger entries carry rex_sig from this adapter."
        ),
        "location": "execution/adapters/signing.py",
    },
    {
        "name": "adapter-wallet",
        "component_type": "adapter",
        "title": "Adapter: WalletAdapter — sov-wallet BIP-39 / Safe multisig",
        "content": (
            "WalletAdapter: interface to sov-wallet container (ai_net:3001). "
            "BIP-39 keygen, HKDF+AES-256-GCM seed encryption, EIP-712 SafeTx construction. "
            "Safe multisig: 0x50BF8f009ECC10DB65262c65d729152e989A9323 (2-of-3). "
            "ETH and BTC balance queries; xpub derivation. Initialized 2026-03-31."
        ),
        "location": "execution/adapters/wallet.py",
    },
    {
        "name": "adapter-whisper",
        "component_type": "adapter",
        "title": "Adapter: WhisperAdapter — transcription via a2a-whisper",
        "content": (
            "WhisperAdapter: client to a2a-whisper on node04 (172.16.201.4:8003). "
            "faster-whisper-server; evicts Ollama via keep_alive=0 before transcription "
            "to avoid GPU contention on RTX 3060 Ti."
        ),
        "location": "execution/adapters/whisper.py",
    },

    # ── Harnesses (3) ─────────────────────────────────────────────────────────
    {
        "name": "harness-skill",
        "component_type": "harness",
        "title": "Harness: Skill install (/install)",
        "content": (
            "Skill Harness: stateful multi-step skill lifecycle orchestrator. "
            "Steps: search → LLM select best → Director confirm gate → scanner → install. "
            "Backed by _skill_harness_checkpoint in working_memory. "
            "Triggered via /install slash command, bypassing NL routing."
        ),
        "location": "execution/engine.py (_skill_harness_* methods)",
    },
    {
        "name": "harness-si",
        "component_type": "harness",
        "title": "Harness: Self-improvement (SI)",
        "content": (
            "SI Harness: daily observe loop + proposal generation. "
            "Monitors system metrics, detects anomalies, writes proposals to prospective memory. "
            "Director must approve proposals before execution. "
            "Never self-modifies. Backed by _self_improvement_session in working_memory."
        ),
        "location": "monitoring/self_improvement.py",
    },
    {
        "name": "harness-dev",
        "component_type": "harness",
        "title": "Harness: Dev (nightly code quality)",
        "content": (
            "Dev Harness: 4-phase code quality pipeline (Analyse→Classify→Plan→Execute). "
            "Runs pylint, semgrep, boundary_scanner, GitHub Actions. "
            "LLM advisory (Ollama + Claude escalation). HITL: Director approves findings. "
            "Nightly cron 14:00 UTC. Backed by _developer_harness_checkpoint in working_memory."
        ),
        "location": "dev_harness/harness.py",
    },

    # ── Skills (18) ──────────────────────────────────────────────────────────
    {
        "name": "skill-deep-research",
        "component_type": "skill",
        "title": "Skill: deep-research",
        "content": (
            "deep-research skill: multi-source research combining browser fetch and Ollama "
            "synthesis. Executor: browser+ollama. Specialist: research_agent."
        ),
        "location": "/home/sovereign/skills/deep-research/SKILL.md",
    },
    {
        "name": "skill-email-harness",
        "component_type": "skill",
        "title": "Skill: email-harness",
        "content": (
            "email-harness skill: multi-step email composition and workflow harness. "
            "Specialist: business_agent."
        ),
        "location": "/home/sovereign/skills/email-harness/SKILL.md",
    },
    {
        "name": "skill-imap-smtp-email",
        "component_type": "skill",
        "title": "Skill: imap-smtp-email",
        "content": (
            "imap-smtp-email skill: IMAP fetch + SMTP send for personal and business accounts. "
            "Executor: python3_exec → nanobot-01 (imap_check.py, smtp_send.py). "
            "Specialist: business_agent. Credentials via CredentialProxy."
        ),
        "location": "/home/sovereign/skills/imap-smtp-email/SKILL.md",
    },
    {
        "name": "skill-memory-curate",
        "component_type": "skill",
        "title": "Skill: memory-curate",
        "content": (
            "memory-curate skill: interactive Qdrant memory curation. "
            "Executor: ollama+qdrant. Specialist: memory_agent. "
            "Allows Rex to review, tag, and promote memory entries."
        ),
        "location": "/home/sovereign/skills/memory-curate/SKILL.md",
    },
    {
        "name": "skill-nc-mail",
        "component_type": "skill",
        "title": "Skill: nc-mail",
        "content": (
            "nc-mail skill: 9 Nextcloud mail operations (list, read, send, delete, move, "
            "search, draft, mark_read, mark_unread). Executor: python3_exec → nanobot-01. "
            "Specialist: business_agent. Uses databaseId integers for stable message IDs."
        ),
        "location": "/home/sovereign/skills/nc-mail/SKILL.md",
    },
    {
        "name": "skill-openclaw-nextcloud",
        "component_type": "skill",
        "title": "Skill: openclaw-nextcloud",
        "content": (
            "openclaw-nextcloud skill (certified OpenClaw): 21 operations across calendar, "
            "tasks, files, notes (calendar_list/create/delete/update/list_events, "
            "tasks_list/create/complete/delete, files_list/search/read/write/delete/mkdir, "
            "notes_list/read/create/update/delete). "
            "Executor: python3_exec → nanobot-01 (nextcloud.py)."
        ),
        "location": "/home/sovereign/skills/openclaw-nextcloud/SKILL.md",
    },
    {
        "name": "skill-pdf",
        "component_type": "skill",
        "title": "Skill: pdf",
        "content": (
            "pdf skill: PDF document processing — extract text, summarise, query. "
            "Specialist: research_agent."
        ),
        "location": "/home/sovereign/skills/pdf/SKILL.md",
    },
    {
        "name": "skill-pytest-testing",
        "component_type": "skill",
        "title": "Skill: pytest-testing",
        "content": (
            "pytest-testing skill: run pytest test suites, report results. "
            "Used by Dev-Harness for automated test execution."
        ),
        "location": "/home/sovereign/skills/pytest-testing/SKILL.md",
    },
    {
        "name": "skill-rss-digest",
        "component_type": "skill",
        "title": "Skill: rss-digest",
        "content": (
            "rss-digest skill: RSS feed fetch and digest generation. "
            "Executor: python3_exec → nanobot-01 (feeds.py). "
            "Specialists: research_agent, business_agent. "
            "Used by morning briefing task scheduler steps."
        ),
        "location": "/home/sovereign/skills/rss-digest/SKILL.md",
    },
    {
        "name": "skill-security-audit",
        "component_type": "skill",
        "title": "Skill: security-audit",
        "content": (
            "security-audit skill: security posture review via Ollama LLM analysis. "
            "Executor: ollama. Specialist: security_agent."
        ),
        "location": "/home/sovereign/skills/security-audit/SKILL.md",
    },
    {
        "name": "skill-session-wrap-up",
        "component_type": "skill",
        "title": "Skill: session-wrap-up",
        "content": (
            "session-wrap-up skill: end-of-session summary generation across all 5 specialists. "
            "Executor: ollama. Specialists: research_agent, business_agent, security_agent, "
            "dev_agent, memory_agent."
        ),
        "location": "/home/sovereign/skills/session-wrap-up/SKILL.md",
    },
    {
        "name": "skill-skill-creator",
        "component_type": "skill",
        "title": "Skill: skill-creator",
        "content": (
            "skill-creator skill: guided skill scaffold generation for new sovereign skills. "
            "Produces SKILL.md + script template."
        ),
        "location": "/home/sovereign/skills/skill-creator/SKILL.md",
    },
    {
        "name": "skill-sovereign-browser",
        "component_type": "skill",
        "title": "Skill: sovereign-browser",
        "content": (
            "sovereign-browser skill: web search and URL fetch via a2a-browser on node04. "
            "Executor: python3_exec → nanobot-01 (browser.py). "
            "Commands: search (POST /search) and fetch (POST /fetch) against a2a-browser. "
            "All external internet egress from sovereign-core routes through this skill."
        ),
        "location": "/home/sovereign/skills/sovereign-browser/SKILL.md",
    },
    {
        "name": "skill-sovereign-nextcloud-fs",
        "component_type": "skill",
        "title": "Skill: sovereign-nextcloud-fs",
        "content": (
            "sovereign-nextcloud-fs skill: 11 Nextcloud filesystem operations "
            "(fs_list, fs_list_recursive, fs_read, fs_move, fs_copy, fs_mkdir, fs_delete, "
            "fs_tag, fs_untag, fs_search, telegram_upload). "
            "Executor: python3_exec → nanobot-01 (nc_fs.py). Specialist: business_agent."
        ),
        "location": "/home/sovereign/skills/sovereign-nextcloud-fs/SKILL.md",
    },
    {
        "name": "skill-sovereign-nextcloud-ingest",
        "component_type": "skill",
        "title": "Skill: sovereign-nextcloud-ingest",
        "content": (
            "sovereign-nextcloud-ingest skill: AI-powered Nextcloud document classification. "
            "3 operations: fetch_classify (single file), fetch_classify_folder, ingest_status. "
            "Executor: python3_exec → nanobot-01 (nc_ingest.py). "
            "Private folder policy enforced (_is_private() check)."
        ),
        "location": "/home/sovereign/skills/sovereign-nextcloud-ingest/SKILL.md",
    },
    {
        "name": "skill-uv-tdd",
        "component_type": "skill",
        "title": "Skill: uv-tdd",
        "content": (
            "uv-tdd skill: test-driven development workflow using uv package manager. "
            "Automates venv creation, dependency install, and test execution cycles."
        ),
        "location": "/home/sovereign/skills/uv-tdd/SKILL.md",
    },
    {
        "name": "skill-wdk",
        "component_type": "skill",
        "title": "Skill: wdk (Tether Wallet Development Kit)",
        "content": (
            "wdk skill (certified OpenClaw): Tether Wallet Development Kit. "
            "Wallet research and development tooling. Specialist: research_agent. "
            "Installed from OpenClaw registry 2026-03-28."
        ),
        "location": "/home/sovereign/skills/wdk/SKILL.md",
    },
    {
        "name": "skill-weather",
        "component_type": "skill",
        "title": "Skill: weather",
        "content": (
            "weather skill: current conditions and forecast retrieval. "
            "Routes via browser fetch. Specialist: research_agent."
        ),
        "location": "/home/sovereign/skills/weather/SKILL.md",
    },

    # ── Gateway (1) ───────────────────────────────────────────────────────────
    {
        "name": "gateway-telegram",
        "component_type": "gateway",
        "title": "Gateway: Telegram bot interface",
        "content": (
            "Telegram gateway: Director-facing interface. Handles text messages, "
            "attachment uploads, and /command dispatch. FIFO session store (6-turn history). "
            "CommandHandler entries: /install. Routes to sovereign-core:8000/chat. "
            "Container: gateway on ai_net."
        ),
        "location": "gateway/main.py",
    },

    # ── Containers (13) ───────────────────────────────────────────────────────
    {
        "name": "container-docker-broker",
        "component_type": "container",
        "title": "Container: docker-broker",
        "content": (
            "docker-broker container: sole holder of docker.sock. "
            "System commands whitelist only. cpuset: 8-11,20-23. mem_limit: 512m. "
            "Networks: ai_net."
        ),
        "location": "compose.yml:docker-broker",
    },
    {
        "name": "container-qdrant",
        "component_type": "container",
        "title": "Container: qdrant (working_memory)",
        "content": (
            "qdrant container: working_memory collection only. "
            "tmpfs-backed via sovereign_runtime Docker volume (4GB). "
            "on_disk=False — ephemeral by design, lost on crash. "
            "cpuset: 0-7,12-19 (perf cores — on cognitive loop critical path). mem_limit: 4g."
        ),
        "location": "compose.yml:qdrant",
    },
    {
        "name": "container-qdrant-archive",
        "component_type": "container",
        "title": "Container: qdrant-archive (RAID collections)",
        "content": (
            "qdrant-archive container: 7 durable RAID collections "
            "(semantic, episodic, procedural, prospective, associative, relational, meta). "
            "RAID storage: /home/sovereign/vector. on_disk=True. "
            "cpuset: 0-7,12-19. mem_limit: 4g."
        ),
        "location": "compose.yml:qdrant-archive",
    },
    {
        "name": "container-sovereign-core",
        "component_type": "container",
        "title": "Container: sovereign-core (FastAPI orchestration)",
        "content": (
            "sovereign-core container: FastAPI reasoning engine and orchestration brain. "
            "5-pass cognitive loop, governance enforcement, memory management. "
            "Networks: ai_net + business_net (dual-homed). "
            "API: 127.0.0.1:8000 loopback only. cpuset: 0-7,12-19. mem_limit: 2g."
        ),
        "location": "compose.yml:sovereign-core",
    },
    {
        "name": "container-ollama",
        "component_type": "container",
        "title": "Container: ollama (GPU LLM inference)",
        "content": (
            "ollama container: local GPU-accelerated LLM inference. "
            "Model: llama3.1:8b-instruct-q4_K_M (~4.4GB VRAM). "
            "Also has mistral:7b installed. RTX 3060 Ti passthrough. "
            "mem_limit: 6g. Networks: ai_net."
        ),
        "location": "compose.yml:ollama",
    },
    {
        "name": "container-ollama-embed",
        "component_type": "container",
        "title": "Container: ollama-embed (CPU embeddings)",
        "content": (
            "ollama-embed container: nomic-embed-text embedding service. "
            "OLLAMA_NUM_GPU=0 — CPU-only, no VRAM constraint. "
            "768-dimensional embeddings for all Qdrant writes. "
            "cpuset: 8-11,20-23. mem_limit: 2g."
        ),
        "location": "compose.yml:ollama-embed",
    },
    {
        "name": "container-nc-db",
        "component_type": "container",
        "title": "Container: nc-db (MariaDB for Nextcloud)",
        "content": (
            "nc-db container: MariaDB database backend for Nextcloud. "
            "Networks: business_net. cpuset: 8-11,20-23. mem_limit: 512m."
        ),
        "location": "compose.yml:db",
    },
    {
        "name": "container-nc-redis",
        "component_type": "container",
        "title": "Container: nc-redis (Redis for Nextcloud)",
        "content": (
            "nc-redis container: Redis session cache for Nextcloud. "
            "Networks: business_net. cpuset: 8-11,20-23. mem_limit: 256m."
        ),
        "location": "compose.yml:redis",
    },
    {
        "name": "container-nextcloud",
        "component_type": "container",
        "title": "Container: nextcloud (business memory)",
        "content": (
            "nextcloud container: Nextcloud instance — business memory (WebDAV/CalDAV). "
            "LAN direct: http://172.16.201.25 (port 80). "
            "Tailscale: https://sovereign.tail887d2b.ts.net. "
            "Networks: business_net. mem_limit: 2g."
        ),
        "location": "compose.yml:nextcloud",
    },
    {
        "name": "container-sov-wallet",
        "component_type": "container",
        "title": "Container: sov-wallet (Safe Transaction Service proxy)",
        "content": (
            "sov-wallet container: node:18-alpine Safe Transaction Service proxy. "
            "BIP-39 keygen, HKDF+AES-256-GCM encryption, EIP-712 SafeTx. "
            "Networks: ai_net + browser_net. Port 3001. mem_limit: 256m."
        ),
        "location": "compose.yml:sov-wallet",
    },
    {
        "name": "container-gateway",
        "component_type": "container",
        "title": "Container: gateway (Telegram bot)",
        "content": (
            "gateway container: Telegram bot interface for Director (Matt). "
            "Authorised chat ID: 5401323149. "
            "Networks: ai_net. cpuset: 8-11,20-23. mem_limit: 256m."
        ),
        "location": "compose.yml:gateway",
    },
    {
        "name": "container-nanobot-01",
        "component_type": "container",
        "title": "Container: nanobot-01 (skill execution sidecar)",
        "content": (
            "nanobot-01 container: primary skill execution environment. "
            "A2A JSON-RPC 3.0 server on port 8080. "
            "Networks: ai_net + business_net. "
            "cpuset: 8-11,20-23. mem_limit: 512m."
        ),
        "location": "compose.yml:nanobot-01",
    },
    {
        "name": "container-nextcloud-rp",
        "component_type": "container",
        "title": "Container: nextcloud-rp (nginx reverse proxy)",
        "content": (
            "nextcloud-rp container: nginx reverse proxy for Nextcloud Tailscale access. "
            "Also proxies sovereign-core management portal routes (port 8000). "
            "Blocks /chat (403). cpuset: 8-11,20-23. mem_limit: 128m."
        ),
        "location": "compose.yml:nextcloud-rp",
    },
    {
        "name": "container-a2a-browser",
        "component_type": "container",
        "title": "Container: a2a-browser (node04 web egress)",
        "content": (
            "a2a-browser container: web search and URL fetch service on node04 "
            "(172.16.201.4:8001). All internet egress from sovereign-core routes through "
            "this service. Supports POST /search (SearXNG) and POST /fetch. "
            "AUTH_PROFILES attach credentials per host. Auth: X-API-Key shared secret."
        ),
        "location": "node04:a2a-browser (172.16.201.4:8001)",
    },
    {
        "name": "container-a2a-whisper",
        "component_type": "container",
        "title": "Container: a2a-whisper (node04 transcription)",
        "content": (
            "a2a-whisper container: faster-whisper-server on node04 (172.16.201.4:8003). "
            "Replaces local whisper container (removed 2026-03-20). "
            "Evicts Ollama via keep_alive=0 before transcription to avoid GPU contention. "
            "WHISPER_URL=http://172.16.201.4:8003 in secrets/whisper.env."
        ),
        "location": "node04:a2a-whisper (172.16.201.4:8003)",
    },

    # ── Modules (8) ───────────────────────────────────────────────────────────
    {
        "name": "module-synthesis",
        "component_type": "module",
        "title": "Module: memory/synthesis.py — episodic synthesis",
        "content": (
            "synthesis.py: 3-pass episodic memory synthesis module. "
            "Pass 1: same-intent variant grouping → associative:intent:{slug}:variants. "
            "Pass 2: mixed-outcome relational → relational:intent:{a}:{b}. "
            "Pass 3: co-occurrence links → associative:intent:{a}:{b}. "
            "Runs nightly (15:00 UTC) and on demand via memory_synthesise intent."
        ),
        "location": "memory/synthesis.py",
    },
    {
        "name": "module-semantic-seeds",
        "component_type": "module",
        "title": "Module: memory/semantic_seeds.py — seed builders",
        "content": (
            "semantic_seeds.py: builds semantic memory seed lists for all intents, "
            "skills, and harnesses. Functions: build_intent_seeds(), build_skill_seeds(), "
            "build_harness_seeds(), make_skill_semantic_seed(). "
            "137 seeds (116 intents + 18 skills + 3 harnesses) written at startup."
        ),
        "location": "memory/semantic_seeds.py",
    },
    {
        "name": "module-component-registry",
        "component_type": "module",
        "title": "Module: memory/component_registry.py — sov_id registry",
        "content": (
            "component_registry.py: deterministic UUID5 sov_ids for all system components. "
            "69 components across 8 types (cognitive_pass, adapter, harness, skill, "
            "gateway, container, module + short_circuit subtype). "
            "Writes semantic:component:{name} entries + meta:system:component-registry index."
        ),
        "location": "memory/component_registry.py",
    },
    {
        "name": "module-task-scheduler",
        "component_type": "module",
        "title": "Module: scheduling/task_scheduler.py — Qdrant-backed task executor",
        "content": (
            "task_scheduler.py: NL task parser → TaskDefinition → Qdrant-backed store. "
            "PROSPECTIVE + PROCEDURAL + EPISODIC triple per task_id. "
            "60-second executor loop. Seeded tasks: Dev-Harness (14:00 UTC), "
            "Memory Synthesis (15:00 UTC), Weekday Briefing (20:30 UTC Mon-Fri)."
        ),
        "location": "scheduling/task_scheduler.py",
    },
    {
        "name": "module-credential-proxy",
        "component_type": "module",
        "title": "Module: execution/credential_proxy.py — single-use token delegation",
        "content": (
            "credential_proxy.py: CredentialProxy issues single-use tokens (60s TTL) "
            "for nanobot-01 credential access. Services: imap_business, imap_personal, "
            "smtp_business, smtp_personal, nextcloud. "
            "Token redeemed via POST sovereign-core:8000/credential_proxy."
        ),
        "location": "execution/credential_proxy.py",
    },
    {
        "name": "module-governance-engine",
        "component_type": "module",
        "title": "Module: governance/engine.py — deterministic tier validation",
        "content": (
            "GovernanceEngine: deterministic tier/action validation from governance.json. "
            "validate() raises ValueError on failure. get_intent_tier() for skills domain. "
            "Never contains LLM calls — must remain deterministic. "
            "Policy: /home/sovereign/governance/governance.json (RAID, mounted :ro)."
        ),
        "location": "governance/engine.py",
    },
    {
        "name": "module-skill-loader",
        "component_type": "module",
        "title": "Module: skills/loader.py — SkillLoader SKILL.md validation",
        "content": (
            "SkillLoader: validates and loads SKILL.md definitions from "
            "/home/sovereign/skills/<name>/SKILL.md. "
            "Enforces dual-layer integrity (JSON schema + Ed25519 checksum). "
            "Injects SKILL.md into specialist prompts at load time."
        ),
        "location": "skills/loader.py",
    },
    {
        "name": "module-lifecycle-manager",
        "component_type": "module",
        "title": "Module: skills/lifecycle.py — SkillLifecycleManager",
        "content": (
            "SkillLifecycleManager: SEARCH/REVIEW/LOAD/AUDIT lifecycle for skills. "
            "Backed by Skill Harness checkpoint in working_memory. "
            "load() auto-writes semantic memory entry on successful install. "
            "Receives qdrant instance from ExecutionEngine._get_lifecycle()."
        ),
        "location": "skills/lifecycle.py",
    },
    {
        "name": "module-entity-registry",
        "component_type": "module",
        "title": "Module: memory/entity_registry.py — foundational entity registry",
        "content": (
            "entity_registry.py: sequential sov_ids (002–013) for foundational sovereign "
            "entities. Director-approved, manually curated. entity_type field (not "
            "component_type) used for all entries. "
            "Functions: build_entity_seeds(), build_entity_index(). "
            "Writes semantic:entity:{slug} entries + semantic:governance:entity-registry "
            "to SEMANTIC collection at startup."
        ),
        "location": "memory/entity_registry.py",
    },
]


def build_component_seeds() -> list[dict]:
    """Return all component registry seed dicts for SEMANTIC collection.

    Each dict is compatible with QdrantAdapter.seed_intent_semantic_entries().
    The sov_id, component_type, and parent_sov_id are carried in extra_meta.
    """
    seeds = []
    for comp in _COMPONENTS:
        name = comp["name"]
        comp_sov_id = sov_id_for(name)
        safe_name = name.replace("-", "_")
        seed_id = f"component_registry_v1_{safe_name}"
        key = f"semantic:component:{name}"

        extra_meta: dict = {
            "sov_id":             comp_sov_id,
            "component_type":     comp["component_type"],
            "parent_sov_id":      parent_sov_id_for(name, comp["component_type"]),
            "canonical_location": comp.get("location", ""),
            "status":             "active",
            "source":             "component_registry",
        }
        # Carry optional extra fields (subtype, short_circuit_domain, etc.)
        for field in ("subtype", "short_circuit_domain"):
            if field in comp:
                extra_meta[field] = comp[field]

        seeds.append({
            "seed_id":  seed_id,
            "key":      key,
            "title":    comp["title"],
            "content":  comp["content"],
            "domain":   "system.component",
            "extra_meta": extra_meta,
        })
    return seeds


def build_component_index(seeds: list[dict]) -> dict:
    """Build the meta:system:component-registry payload.

    Returns a dict suitable for storage as a zero-vector META entry.
    Maps component name → {sov_id, parent_sov_id, component_type} for hierarchy traversal
    without scrolling SEMANTIC. Also carries sovereign_root_id for reference.
    """
    components: dict[str, dict] = {}
    for seed in seeds:
        name = seed["key"].replace("semantic:component:", "")
        em = seed["extra_meta"]
        components[name] = {
            "sov_id":         em["sov_id"],
            "parent_sov_id":  em.get("parent_sov_id"),
            "component_type": em.get("component_type", ""),
        }

    return {
        "_key":               "meta:system:component-registry",
        "type":               "meta",
        "domain":             "system",
        "title":              "Sovereign component registry — sov_id + hierarchy index",
        "total":              len(components),
        "sovereign_root_id":  SOVEREIGN_ROOT_ID,
        "components":         components,
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "_backfill_seed_id":  "component_registry_v1_meta_index",
    }
