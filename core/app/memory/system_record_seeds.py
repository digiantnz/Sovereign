"""System Record Seeds — governance invariants, adapter rules, and as-built decisions.

Migrates the current-state system knowledge from CLAUDE.md and as-built.md into
Qdrant SEMANTIC collection so the routing pipeline can reason about architecture
constraints without reading files at runtime.

Builder functions:
    build_governance_seeds()         — 10 Standing Design Orders + invariants
    build_adapter_invariant_seeds()  — key rules from core/app/CLAUDE.md
    build_nanobot_invariant_seeds()  — key rules from nanobot-01/CLAUDE.md
    build_governance_domain_rule_seeds(gov_path) — LOW/MID/HIGH tier rules from governance.json
    build_as_built_seeds()           — last 3 as-built decisions (live) + older (historical)

Historical entries carry status="historical" and are excluded from routing searches
by the `must_not` filter added to search_all_sovereign() / search_all_weighted() (Beta-2 Task 7).
They remain for audit purposes and can be queried directly by key.

Per confirmed design decisions (Beta-2):
  - intent_tiers section skipped — already covered by Task 3 intent_seed entries
  - Current-state only for live entries; stale superseded limitations excluded
  - Historical entries in semantic:system:history:* namespace
"""

# ── Standing Design Orders (10) ───────────────────────────────────────────────

_STANDING_DESIGN_ORDERS = [
    {
        "slug": "sdo-01-openclaw-first",
        "title": "SDO-01: OpenClaw skill exists → Skill Harness install",
        "content": (
            "Standing Design Order 1: If an OpenClaw equivalent skill exists for a capability, "
            "install it via the Skill Harness and execute via nanobot-01. "
            "Never build bespoke when a certified community skill covers the need."
        ),
    },
    {
        "slug": "sdo-02-bespoke-single-location",
        "title": "SDO-02: No OpenClaw skill → bespoke, single canonical location",
        "content": (
            "Standing Design Order 2: If no OpenClaw equivalent exists, implement as a bespoke "
            "module at one location appropriate to its architecture. "
            "Do not install partial duplicates."
        ),
    },
    {
        "slug": "sdo-03-no-duplicates",
        "title": "SDO-03: One implementation, one invocation point — no duplicates",
        "content": (
            "Standing Design Order 3: The same capability must never exist in two places "
            "(e.g. inline in engine.py AND in a nanobot skill). "
            "If it exists in two places it is a bug to be fixed, not a pattern to follow."
        ),
    },
    {
        "slug": "sdo-04-canonical-location-only",
        "title": "SDO-04: Updates go to the canonical location only",
        "content": (
            "Standing Design Order 4: No shadow copies, no inline patches. "
            "Find the canonical file and edit it. Never update a copy."
        ),
    },
    {
        "slug": "sdo-05-engine-orchestration-only",
        "title": "SDO-05: Engine.py is orchestration only",
        "content": (
            "Standing Design Order 5: Capability logic belongs in skill modules or dedicated "
            "harness modules. Engine.py may extract parameters, route, and normalise responses "
            "— it must not implement capability logic inline."
        ),
    },
    {
        "slug": "sdo-06-one-semantic-entry-per-skill",
        "title": "SDO-06: Every skill has exactly one semantic:intent:{slug} entry",
        "content": (
            "Standing Design Order 6: Every skill (nanobot or bespoke) has exactly one "
            "semantic memory entry pointing to its canonical trigger. "
            "Key format: semantic:intent:{slug}."
        ),
    },
    {
        "slug": "sdo-07-semantic-entry-on-install",
        "title": "SDO-07: New skill created/installed → semantic memory entry written",
        "content": (
            "Standing Design Order 7: New skill created or installed → semantic memory entry "
            "written at that time. The Skill Harness install step writes the entry automatically. "
            "Bespoke modules must write their entry in their own creation task."
        ),
    },
    {
        "slug": "sdo-08-sov-id-on-new-component",
        "title": "SDO-08: New component created → sov_id assigned + semantic entry",
        "content": (
            "Standing Design Order 8: New component created → sov_id assigned + semantic memory "
            "entry written at that time. No component is fully created until its semantic entry "
            "exists. sov_id = uuid5(namespace=7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d, "
            "'component:{name}')."
        ),
    },
    {
        "slug": "sdo-09-deprecation-not-deletion",
        "title": "SDO-09: Deprecation → episodic entry, semantic marked inactive, no deletion",
        "content": (
            "Standing Design Order 9: Deprecation → episodic entry written, semantic entry "
            "marked inactive, no deletion. Inactive entries remain for historical integrity. "
            "Never delete semantic or episodic entries."
        ),
    },
    {
        "slug": "sdo-10-modify-in-place",
        "title": "SDO-10: Updates to existing skills → modify in place at canonical location only",
        "content": (
            "Standing Design Order 10: Updates to existing skills → modify in place at canonical "
            "location only. No new file, no versioned copy, no shadow. "
            "The canonical file is the source of truth."
        ),
    },
]


def build_governance_seeds() -> list[dict]:
    """Build seeds for Standing Design Orders + key architectural invariants."""
    seeds = []

    # 10 Standing Design Orders
    for sdo in _STANDING_DESIGN_ORDERS:
        slug = sdo["slug"]
        seed_id = f"system_record_v1_sdo_{slug.replace('-', '_')}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:governance:standing-design-order:{slug}",
            "title":    sdo["title"],
            "content":  sdo["content"],
            "domain":   "governance",
            "extra_meta": {"source": "governance_seed", "record_type": "standing_design_order"},
        })

    # Key architectural invariants
    _INVARIANTS = [
        {
            "slug": "tmpfs-vs-raid",
            "title": "Invariant: tmpfs working_memory vs RAID sovereign collections",
            "content": (
                "working_memory (qdrant:6333) is tmpfs-backed, ephemeral, lost on crash — by design. "
                "All 7 sovereign collections (semantic, episodic, procedural, prospective, "
                "associative, relational, meta) are RAID-backed via qdrant-archive:6333. "
                "Crash without clean shutdown = un-promoted working_memory entries LOST."
            ),
        },
        {
            "slug": "broker-sole-docker-socket",
            "title": "Invariant: docker-broker is the sole holder of docker.sock",
            "content": (
                "docker.sock is mounted ONLY in the docker-broker container. "
                "sovereign-core never has direct Docker access. "
                "All system commands (docker ps/logs/restart/rebuild/prune) go through broker. "
                "nanobot-01 handles all application skills. Hard boundary — do not violate."
            ),
        },
        {
            "slug": "governance-no-llm",
            "title": "Invariant: GovernanceEngine must remain deterministic — no LLM calls",
            "content": (
                "GovernanceEngine.validate() raises ValueError on failure, returns rules dict on success. "
                "GovernanceEngine.get_intent_tier() reads intent_tiers section deterministically. "
                "Never add LLM calls inside GovernanceEngine. "
                "Tier is always derived deterministically — never trust the LLM for tier assignment."
            ),
        },
        {
            "slug": "ollama-embed-cpu-only",
            "title": "Invariant: ollama-embed is CPU-only for embeddings",
            "content": (
                "ollama-embed runs CPU-only (OLLAMA_NUM_GPU=0) at http://ollama-embed:11434. "
                "nomic-embed-text (768-dim) — no VRAM constraint, can run at any time. "
                "ollama (GPU) runs llama3.1:8b on RTX 3060 Ti. "
                "Whisper (node04:8003) and ollama MUST NOT run concurrently — GPU contention."
            ),
        },
        {
            "slug": "api-loopback-only",
            "title": "Invariant: sovereign-core API is loopback-only",
            "content": (
                "sovereign-core API binds to 127.0.0.1:8000 (loopback only). "
                "No host port binding. Gateway container (ai_net) reaches it via "
                "http://sovereign-core:8000. nextcloud-rp proxies portal routes only "
                "(not /chat — blocked 403)."
            ),
        },
    ]
    for inv in _INVARIANTS:
        slug = inv["slug"]
        seed_id = f"system_record_v1_invariant_{slug.replace('-', '_')}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:governance:invariant:{slug}",
            "title":    inv["title"],
            "content":  inv["content"],
            "domain":   "governance",
            "extra_meta": {"source": "governance_seed", "record_type": "invariant"},
        })

    return seeds


# ── Adapter invariants (from core/app/CLAUDE.md) ──────────────────────────────

def build_adapter_invariant_seeds() -> list[dict]:
    """Build seeds for key adapter-level invariants from core/app/CLAUDE.md."""
    seeds = []

    _ADAPTER_INVARIANTS = [
        {
            "adapter": "qdrant",
            "slug": "client-routing",
            "title": "QdrantAdapter: client vs archive_client routing",
            "content": (
                "self.client → qdrant container (working_memory, tmpfs, ephemeral). "
                "self.archive_client → qdrant-archive container (all 7 RAID collections). "
                "_client_for(collection) helper routes correctly. "
                "NO wm_client in-process — working_memory lives in qdrant container. "
                "setup() always recreates working_memory fresh on startup."
            ),
        },
        {
            "adapter": "qdrant",
            "slug": "mip-key-generation",
            "title": "QdrantAdapter: MIP key generation and fallback",
            "content": (
                "Every sovereign collection write calls _generate_key_and_title() → single Ollama call. "
                "Key format: {type}:{domain}:{slug} — prefix from known fields, only slug LLM-derived. "
                "LLM cannot override type or domain. "
                "Ollama timeout → _no_key: True + last_updated stored; never blocks promotion."
            ),
        },
        {
            "adapter": "nanobot",
            "slug": "dispatch-model",
            "title": "NanobotAdapter: dispatch model and response normalisation",
            "content": (
                "NanobotAdapter forwards to nanobot-01 via A2A JSON-RPC 3.0 (POST /run). "
                "All results stamped _trust: untrusted_external. "
                "python3_exec responses are flat (no nested 'result' key). "
                "Use nb.get('result') if nb.get('result') is not None else nb — not nb.get('result', nb). "
                "CredentialProxy token forwarded in request context."
            ),
        },
        {
            "adapter": "broker",
            "slug": "system-commands-whitelist",
            "title": "BrokerAdapter: system commands whitelist only",
            "content": (
                "docker-broker handles ONLY: docker_ps/logs/restart/stats/inspect/exec, "
                "uname/df/free/ps/nvidia_smi/systemctl_status/journalctl. "
                "All other application skills (IMAP/SMTP/feeds/WebDAV/CalDAV) → nanobot-01. "
                "Do NOT add new system commands to broker without architectural review."
            ),
        },
        {
            "adapter": "ollama",
            "slug": "pass-routing",
            "title": "CognitionEngine: which passes route where",
            "content": (
                "PASS 1/3b/4/5 always local Ollama. "
                "PASS 2 (specialist outbound) is the only externally-routable pass via _routing_decision(). "
                "DCL hard-block: tier in {PRIVATE, SECRET} → force_local=True. "
                "Claude API → DCL escalation for complex architectural tasks. "
                "Grok → grok-3 for current/news/market queries. "
                "Fallback chain: external unavailable → graceful fallback to Ollama."
            ),
        },
    ]

    for inv in _ADAPTER_INVARIANTS:
        adapter = inv["adapter"]
        slug    = inv["slug"]
        seed_id = f"system_record_v1_adapter_{adapter}_{slug.replace('-', '_')}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:adapter:{adapter}:invariant:{slug}",
            "title":    inv["title"],
            "content":  inv["content"],
            "domain":   f"adapter.{adapter}",
            "extra_meta": {"source": "governance_seed", "record_type": "adapter_invariant", "adapter": adapter},
        })

    return seeds


# ── Nanobot-01 invariants (from nanobot-01/CLAUDE.md) ────────────────────────

def build_nanobot_invariant_seeds() -> list[dict]:
    """Build seeds for nanobot-01 implementation invariants from nanobot-01/CLAUDE.md."""
    seeds = []

    _NANOBOT_INVARIANTS = [
        {
            "slug": "protocol-contract",
            "title": "nanobot-01: A2A JSON-RPC 3.0 protocol contract",
            "content": (
                "nanobot-01 accepts A2A 3.0 via POST /run. Method format: skill-name/operation-name. "
                "params.payload = operation params. Legacy flat format (no jsonrpc key) also accepted. "
                "_normalise_to_contract() wraps all responses as A2AMessage.success/error. "
                "nanobot-01 is a dumb executor: fires skill, returns verbatim. No retry, no fabrication."
            ),
        },
        {
            "slug": "python3-exec-flat-response",
            "title": "nanobot-01: python3_exec responses are flat (no nested result key)",
            "content": (
                "python3_exec script output is merged at top level — no nested 'result' key. "
                "_forward() in sovereign-core normalises: if body.get('result') is None, "
                "builds body_result from all non-wrapper body fields "
                "(wrapper fields: run_id, skill, action, path, elapsed_s). "
                "Never use nb.get('result', nb) — use nb.get('result') if nb.get('result') is not None else nb."
            ),
        },
        {
            "slug": "credential-proxy-flow",
            "title": "nanobot-01: CredentialProxy single-use token delegation",
            "content": (
                "CredentialProxy issues single-use tokens (60s TTL) for nanobot-01 credential access. "
                "Services: imap_business, imap_personal, smtp_business, smtp_personal, nextcloud. "
                "Token redeemed via POST sovereign-core:8000/credential_proxy. "
                "Injected as subprocess env vars, immediately invalidated after redemption."
            ),
        },
    ]

    for inv in _NANOBOT_INVARIANTS:
        slug    = inv["slug"]
        seed_id = f"system_record_v1_nanobot_{slug.replace('-', '_')}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:component:nanobot-01:invariant:{slug}",
            "title":    inv["title"],
            "content":  inv["content"],
            "domain":   "component.nanobot-01",
            "extra_meta": {"source": "governance_seed", "record_type": "nanobot_invariant"},
        })

    return seeds


# ── Governance domain rules (from governance.json tiers — NOT intent_tiers) ──

def build_governance_domain_rule_seeds(gov_path: str = "/home/sovereign/governance/governance.json") -> list[dict]:
    """Build seeds from governance.json tiers dict (LOW/MID/HIGH domain rules).

    Reads the tiers section — NOT intent_tiers (those are already covered by
    Task 3 intent_seed entries, per confirmed design decisions).
    One entry per tier summarising the key permissions.
    """
    import json as _json
    seeds = []
    try:
        with open(gov_path) as f:
            gov = _json.load(f)
    except Exception:
        return seeds

    tiers = gov.get("tiers", {})
    tier_version = gov.get("meta", {}).get("version", "unknown")

    _TIER_DESCRIPTIONS = {
        "LOW": (
            "LOW tier: No confirmation required. "
            "Allows: docker read, file read, mail read, WebDAV/CalDAV read, Ollama query, "
            "memory write/search, security read, browser fetch/search, skill search/review/audit, "
            "scheduler read, wallet read config, notes read, ncfs/ncingest read. "
            "Director confirmation NOT required for LOW tier actions."
        ),
        "MID": (
            "MID tier: requires_confirmation: true. "
            "Allows everything LOW permits plus: docker restart/update, file write, mail send/move, "
            "WebDAV/CalDAV write, skill load, scheduler create/update, wallet get_address/sign, "
            "notes create/update, ingest file/folder, nanobot MID ops. "
            "Director must confirm before execution."
        ),
        "HIGH": (
            "HIGH tier: requires_double_confirmation: true. "
            "Allows everything MID permits plus: docker rebuild/prune/stop/remove, "
            "file delete, notes delete, skill unload, wallet propose_safe_tx. "
            "Director must confirm TWICE (double-confirmation IS the security gate). "
            "PASS 2 security skipped when confirmed=True — double-confirm is already the gate."
        ),
    }

    for tier in ("LOW", "MID", "HIGH"):
        if tier not in tiers:
            continue
        description = _TIER_DESCRIPTIONS.get(tier, f"{tier} tier rules from governance.json v{tier_version}.")
        slug    = tier.lower()
        seed_id = f"system_record_v1_domain_rule_{slug}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:governance:domain-rule:{slug}",
            "title":    f"Governance tier {tier}: domain-level permission rules",
            "content":  description + f" (governance.json v{tier_version})",
            "domain":   "governance",
            "extra_meta": {
                "source":       "governance_seed",
                "record_type":  "domain_rule",
                "tier":         tier,
                "gov_version":  tier_version,
            },
        })

    return seeds


# ── As-built decisions (live + historical) ────────────────────────────────────

def build_as_built_seeds() -> list[dict]:
    """Build seeds for key as-built architecture decisions.

    Live entries (last 3 signed-off decisions as of Beta-2 implementation):
        semantic:architecture:decision:*  — status not set → included in routing searches

    Historical entries (older decisions, superseded or context-only):
        semantic:system:history:*  — status: historical → excluded from routing searches
        by the must_not filter in search_all_sovereign / search_all_weighted.
    """
    seeds = []

    # ── Live entries — last 3 signed-off decisions ────────────────────────
    _LIVE = [
        {
            "slug": "beta2-component-registry",
            "title": "Decision: Beta-2 Component Registry — UUID5 sov_ids for 69 components",
            "content": (
                "Beta-2 Task 4 (2026-04-04): Built component_registry.py with deterministic UUID5 sov_ids "
                "for all 69 system components (cognitive passes, short-circuits, adapters, harnesses, "
                "skills, gateway, containers, modules). "
                "seed_component_entries() writes semantic:component:{name} entries + "
                "meta:system:component-registry aggregate index. "
                "sov_id formula: uuid5('7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d', 'component:{name}')."
            ),
        },
        {
            "slug": "beta2-memory-routing-shadow",
            "title": "Decision: Beta-2 Memory Routing Shadow Mode — PASS 1 calibration",
            "content": (
                "Beta-2 Task 5 (2026-04-04): Memory-first routing shadow mode added to cognition/engine.py. "
                "MEMORY_ROUTING_SHADOW_MODE=True, MEMORY_ROUTING_THRESHOLD=0.85. "
                "_memory_route_shadow() searches semantic:intent:* entries against user_input. "
                "Logs LLM vs memory routing agreement to episodic:shadow_routing:{date}:{intent}. "
                "Routing unchanged until Reasoning Sunday validates thresholds."
            ),
        },
        {
            "slug": "beta2-outcome-write-back",
            "title": "Decision: Beta-2 Outcome Write-Back — success/failure tracking on semantic entries",
            "content": (
                "Beta-2 Task 6 (2026-04-04): _outcome_write_back() fires after every _dispatch_inner() call. "
                "Always writes episodic:outcome:{date}:{intent} entry. "
                "Updates semantic:intent:{slug} success_count/failure_count/consecutive_failure_count "
                "via set_payload() (no re-embedding). "
                "At exactly consecutive_failure_count==3: writes prospective:failure_alert:{slug} "
                "and sends Telegram alert. Dedup gate prevents duplicate alerts."
            ),
        },
    ]

    for entry in _LIVE:
        slug    = entry["slug"]
        seed_id = f"system_record_v1_asbuilt_{slug.replace('-', '_')}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:architecture:decision:{slug}",
            "title":    entry["title"],
            "content":  entry["content"],
            "domain":   "architecture",
            "extra_meta": {"source": "governance_seed", "record_type": "architecture_decision"},
        })

    # ── Historical entries — older decisions (excluded from routing searches) ─
    _HISTORICAL = [
        {
            "slug": "adapter-removal-browser-webdav-caldav",
            "title": "Decision (historical): Adapter-Removal — Browser/WebDAV/CalDAV removed",
            "content": (
                "Adapter-Removal 2026-04-03: BrowserAdapter, WebDAVAdapter, CalDAVAdapter removed from "
                "sovereign-core. All application I/O now routes through nanobot-01. "
                "sovereign-browser (python3_exec, browser.py) added to nanobot-01. "
                "domain==webdav routes to sovereign-nextcloud-fs (fs_*) and openclaw-nextcloud (files_write). "
                "domain==caldav routes to openclaw-nextcloud (calendar_*/tasks_*). "
                "RAID path exception: /home/sovereign/ and /docker/sovereign/ still use broker."
            ),
        },
        {
            "slug": "beta1-memory-synthesis",
            "title": "Decision (historical): Beta-1 Memory Synthesis Module",
            "content": (
                "Beta-1 Task 2 2026-04-03: memory/synthesis.py built. 3-pass episodic synthesis: "
                "same-intent variant grouping → associative:intent:{slug}:variants; "
                "mixed-outcome relational → relational:intent:{a}:{b}; "
                "co-occurrence → associative:intent:{a}:{b}. "
                "Nightly cron 15:00 UTC (03:00 NZST). "
                "Wired into engine.py INTENT_ACTION_MAP, governance.json (v1.23), task_scheduler.py."
            ),
        },
        {
            "slug": "beta1-semantic-seeds",
            "title": "Decision (historical): Beta-1 Semantic Memory Seeds — 137 entries",
            "content": (
                "Beta-1 Task 3 2026-04-03: semantic_seeds.py built. "
                "137 seeds at startup: 116 INTENT_ACTION_MAP intents + 18 skills + 3 harnesses. "
                "seed_intent_semantic_entries() idempotent via _backfill_seed_id. "
                "lifecycle.load() auto-writes semantic entry on skill install. "
                "Key format: semantic:intent:{slug}."
            ),
        },
    ]

    for entry in _HISTORICAL:
        slug    = entry["slug"]
        seed_id = f"system_record_v1_history_{slug.replace('-', '_')}"
        seeds.append({
            "seed_id":  seed_id,
            "key":      f"semantic:system:history:{slug}",
            "title":    entry["title"],
            "content":  entry["content"],
            "domain":   "architecture",
            "extra_meta": {
                "source":       "governance_seed",
                "record_type":  "architecture_decision",
                "status":       "historical",   # excluded from routing searches
            },
        })

    return seeds
