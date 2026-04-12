# Sovereign AI — Claude Code Context

## Design Documents
- Full architecture: `/home/sovereign/sovereign/docs/Sovereign-v2.md`
- Phase 3 detail: `/home/sovereign/sovereign/docs/Sovereign-Phase_3.md`
- Cognitive loop rework: `/home/sovereign/sovereign/docs/Sovereign-CognitiveLoopRework.md`
- Always consult before making architectural decisions.

---

## Standing Design Orders

These rules apply in every session. They are not negotiable and cannot be overridden by inline task instructions.

1. **OpenClaw skill exists → Skill Harness install.** If an OpenClaw equivalent skill exists for a capability, install it via the Skill Harness and execute via nanobot-01. Never build bespoke when a certified community skill covers the need.

2. **No OpenClaw skill → bespoke, single canonical location.** If no OpenClaw equivalent exists, implement as a bespoke module at one location appropriate to its architecture. Do not install partial duplicates.

3. **One implementation, one invocation point — no duplicates regardless of location.** The same capability must never exist in two places (e.g. inline in engine.py AND in a nanobot skill). If it exists in two places it is a bug to be fixed, not a pattern to follow.

4. **Updates go to the canonical location only.** No shadow copies, no inline patches. Find the canonical file and edit it. Never update a copy.

5. **Engine.py is orchestration only.** Capability logic belongs in skill modules or dedicated harness modules. Engine.py may extract parameters, route, and normalise responses — it must not implement capability logic inline.

6. **Every skill (nanobot or bespoke) has exactly one semantic memory entry pointing to its canonical trigger.** Key format: `semantic:intent:{slug}`.

7. **New skill created or installed → semantic memory entry written at that time.** The Skill Harness install step writes the entry automatically. Bespoke modules must write their entry in their own creation task.

8. **New component created → sov_id assigned + semantic memory entry written at that time.** No component is fully created until its semantic entry exists.

9. **Deprecation → episodic entry written, semantic entry marked inactive, no deletion.** Inactive entries remain for historical integrity. Never delete semantic or episodic entries.

10. **Updates to existing skills → modify in place at canonical location only.** No new file, no versioned copy, no shadow. The canonical file is the source of truth.

11. **Foundational entities use `entity_type`; system components use `component_type`. Never both on the same entry.** A foundational entity (`sovereign_entity` class) qualifies for a sequential sov_id only if it: (1) exists independently of Sovereign, (2) is bootstrap critical, (3) has a durable named relationship to the sovereign root, and (4) has been explicitly approved by the Director. Sequential sov_ids are assigned in `entity_registry.py` (append-only, never re-assigned). All other entities receive UUID5 sov_ids. The sovereign root entry is `semantic:entity:sovereign` (not `semantic:component:sovereign`).

---

## Core Philosophy
- **tmpfs (`sovereign_runtime` Docker volume, 4GB)** — ephemeral RAM scratch; qdrant working_memory storage + sovereign-core session data; lost on restart by design
- **RAID5 (`/home/sovereign`)** — subconscious/durable truth; governance, memory, audit, backups; all 7 sovereign Qdrant collections
- **Broker** — sole holder of `docker.sock`; sovereign-core never has direct Docker access
- **Sovereign-core** — reasoning engine and orchestration brain; enforces governance before any action
- **Ollama** — local GPU-accelerated inference only; never executes actions
- **Ollama-embed** — CPU-only nomic-embed-text embedding service; no VRAM; `http://ollama-embed:11434`
- **Nextcloud** — business memory (WebDAV/CalDAV)

---

## Container Architecture

### Networks
- `ai_net`: ollama, ollama-embed, sovereign-core, docker-broker, qdrant, qdrant-archive, gateway, nanobot-01
- `business_net`: nextcloud, nc-redis, nc-db, nextcloud-rp, sovereign-core (dual-homed)
- `browser_net`: (no local container; compose-managed for future use)
- sovereign-core dual-homed (ai_net + business_net); a2a-browser on node04 (172.16.201.4:8001, external)

### Security Boundaries (hard rules — do not violate)
- `docker.sock` → broker container only
- `sovereign-core` → no privileged mounts, no docker.sock
- Ollama API → ai_net only, no host exposure
- Sovereign API → `127.0.0.1:8000` loopback only
- Nextcloud → business_net only

### GPU (RTX 3060 Ti, 8GB VRAM)
- Ollama uses ~4.4 GB (llama3.1:8b-instruct-q4_K_M); also has mistral:7b installed
- Whisper medium uses ~769 MB — cannot run simultaneously with Ollama (RTX 3060 Ti is shared)
- Whisper adapter evicts Ollama via `keep_alive=0` before transcription
- Never load models exceeding ~7.5 GB combined
- **ollama-embed runs CPU-only (OLLAMA_NUM_GPU=0) — no VRAM constraint**

### Sequential GPU constraint (RTX 3060 Ti)
- llama3.1:8b (ollama container) and Whisper (node04:8003, a2a-whisper) MUST NOT run concurrently
- ollama-embed is CPU-only and has NO VRAM constraint — can run at any time
- The whisper adapter evicts Ollama via `keep_alive=0` before each transcription request
- Future: when Whisper moves back on-host, enforce mutual exclusion at the adapter layer

### Container memory limits (32GB RAM host — AMD Ryzen 9 9900X)
| Container | Limit | Notes |
|-----------|-------|-------|
| sovereign-core | 2g | Python process + all in-process adapters |
| ollama | 6g | llama3.1:8b ~4.4GB VRAM + CPU overhead |
| ollama-embed | 2g | nomic-embed-text CPU inference |
| qdrant | 4g | working_memory in-RAM (on_disk=False) |
| qdrant-archive | 4g | RAID collections, mmap'd access |
| nanobot-01 | 512m | |
| docker-broker | 512m | |
| gateway | 256m | |
| sov-wallet | 256m | |
| nextcloud | 2g | |
| nc-db | 512m | |
| nc-redis | 256m | |
| nextcloud-rp | 128m | |
**Total ~22.5GB** — leaves ~9.5GB for OS (4GB target) + page cache.

### 64GB RAM upgrade path
- Upgrade target: 2× 32GB DDR5 (matching existing sticks)
- Enables: expand working_memory qdrant limit from 4g → 8g+; increase sovereign-core to 4g; add periodic background flush to reduce crash-loss risk for working_memory entries
- working_memory collection's `on_disk=False` config persists — only the container memory limit changes

### CPU pinning (AMD Ryzen 9 9900X — 12 physical cores, 24 logical CPUs)
- Core-to-CPU mapping: Core N → CPU N, CPU N+12 (e.g. Core 0 → CPU 0,12; Core 11 → CPU 11,23)
- **sovereign-core, qdrant, qdrant-archive**: cpuset `0-7,12-19` — first 8 physical cores (both threads each); memory queries are on the critical path of the cognitive loop
- **adapter services** (broker, gateway, nanobot-01, sov-wallet, ollama-embed, nc-db, nc-redis, nextcloud, nextcloud-rp): cpuset `8-11,20-23` — last 4 physical cores
- ollama: no cpuset (GPU-bound; CPU threads used for tokenisation and KV cache management)

---

## Storage Layout

### NVMe — `/docker/sovereign/`
```
compose.yml  CLAUDE.md  core/  broker/  gateway/  a2a-browser/
nanobot-01/  nginx/  secrets/  docs/  runtime/  tmp/
```
Subsystem docs: `core/app/CLAUDE.md` · `nanobot-01/CLAUDE.md`

### RAID — `/home/sovereign/`
```
governance/governance.json   ← tier policy :ro mount
memory/MEMORY.md             ← durable AI memory :rw mount
audit/                       ← action audit logs
backups/                     ← container inspect snapshots
skills/<name>/SKILL.md       ← skill definitions :ro mount
security/skill-checksums.json
personas/                    ← sovereign-soul.md + 5 specialist + translator personas
keys/                        ← Ed25519 keypair + wallet-seed.enc
```

---

## Governance Tiers

| Tier | Confirmation | Examples |
|------|-------------|---------|
| LOW  | None | docker ps/logs/stats, file read, mail read, WebDAV read, Ollama query |
| MID  | `requires_confirmation: true` | docker restart/update, file write, calendar write, mail send |
| HIGH | `requires_double_confirmation: true` | docker rebuild/prune, file delete |

- Policy: `/home/sovereign/governance/governance.json` — RAID, mounted read-only; **never bake into image**
- **Never add LLM calls inside GovernanceEngine** — must remain deterministic
- `GovernanceEngine.validate()` raises ValueError on failure, returns rules dict on success
- `GovernanceEngine.get_intent_tier(intent)` reads `intent_tiers` section (used for skills domain)

---

## Application Structure (`core/app/`)

```
main.py  config.py  api/routes.py
governance/engine.py          ← deterministic tier/action validation
skills/loader.py              ← SkillLoader: validates + injects SKILL.md into specialist prompts
skills/lifecycle.py           ← SkillLifecycleManager: SEARCH/REVIEW/LOAD/AUDIT
config_policy/notifier.py     ← ConfigChangeNotifier: Telegram + as-built.md on config writes
cognition/engine.py           ← cognitive passes; parse_task_intent() for scheduler
cognition/prompts.py
cognition/message.py          ← InternalMessage universal envelope
scheduling/task_scheduler.py  ← NL intent parser, Qdrant-backed store, 60s executor
execution/engine.py           ← routes to adapters after governance check
execution/adapters/           ← ollama, whisper, broker, imap, smtp, grok, nanobot
memory/session.py
```

See `core/app/CLAUDE.md` for all adapter invariants, cognitive loop rules, and implementation gotchas.

---

## Cognitive Loop (5-Pass Structure)

```
PASS 1  Orchestrator classify   → intent, delegate_to, tier  (always local)
PASS 2  Security agent          → cleared/conditional/blocked (HIGH tier or scanner flag only)
PASS 3a Specialist outbound     → skill + payload             (externally routable)
EXEC    _dispatch_inner()       → deterministic adapter call
PASS 3b Specialist inbound      → interpret result            (always local)
PASS 4  Orchestrator evaluate   → memory_action + result_for_translator (always local)
PASS 5  Translator              → plain English director_message (always local, restricted input)
```

- All short-circuit paths (ollama/memory/browser/scheduler) build `result_for_translator` and call `translator_pass()` before returning — no raw adapter output reaches Director
- Per-pass timeout: `PASS_TIMEOUT_SECONDS` (default 30s); total: `TOTAL_TIMEOUT_SECONDS` (default 120s)
- Async memory dispatch: `asyncio.create_task()` — never blocks return path
- All nanobot results stamped `_trust: "untrusted_external"`; scanner runs before PASS 3b
- InternalMessage envelope (`cognition/message.py`): Director input hashed at PASS 1, never stored raw; append-only history (output_hash only)

### PASS 2 External Routing — Provider Selection

Two separate routing layers. Do not conflate them.

#### PASS 2/3a — LLM selection for planning (auto-routing applies)
`_routing_decision` in `cognition/engine.py` selects which LLM writes the specialist's action plan:
1. **Explicit override**: `use grok|ask grok|via grok|use claude|ask claude|via claude|external llm/model/ai` → forces external
2. **Provider signals** on raw user message: `current/latest/news/today/recent/market/trending` → Grok; `architectural/architecture/plan/review/design/strategy/strategic` → Claude; default → Grok
3. **Complexity ≥ 0.50** → external Grok. Operational penalty (`restart/container/service/deploy/compose/nginx/redis/mariadb/subnet`) subtracts 0.20.
4. **DCL gate** — PRIVATE/SECRET → force local

#### `domain: ollama` execution — explicit-only external routing
When `_dispatch_inner` reaches `domain: ollama`, Grok/Claude are only called on **explicit** triggers. Auto-signals are intentionally excluded to avoid firing Grok alongside RSS/browser paths PASS 1 may have already chosen.

- **Grok**: `use grok` · `ask grok` · `via grok` · `trending` · `current events`
- **Claude**: `use claude` · `ask claude` · `via claude`
- Everything else → Ollama (local, fast, no billing)

**Preferred external provider: Grok** — Claude API key is set but plan may not cover usage costs.

---

## Installed Skills

| Skill | Executor | Specialists | Operations |
|-------|----------|-------------|------------|
| `imap-smtp-email` | python3_exec → nanobot-01 | business_agent | |
| `openclaw-nextcloud` | python3_exec → nanobot-01 | business_agent | calendar_list/list_events/create/delete/update, tasks_list/create/complete/delete, files_list/search/read/write/delete/mkdir, notes_list/read/create/update/delete (22 ops) |
| `sovereign-browser` | python3_exec → nanobot-01 | research_agent | search, fetch (2 ops) |
| `sovereign-nextcloud-fs` | python3_exec → nanobot-01 | business_agent | telegram_upload, fs_list/list_recursive/read/move/copy/mkdir/delete/tag/untag/search (11 ops) |
| `sovereign-nextcloud-ingest` | python3_exec → nanobot-01 | memory_agent | fetch_classify, fetch_classify_folder, ingest_status (3 ops) |
| `rss-digest` | python3_exec → nanobot-01 | research_agent, business_agent | |
| `deep-research` | nanobot+ollama | research_agent | |
| `security-audit` | ollama | security_agent | |
| `session-wrap-up` | ollama | all 5 specialists | |
| `memory-curate` | ollama+qdrant | memory_agent | |

Skills live at `/home/sovereign/skills/<name>/SKILL.md` (RAID, mounted :ro). See `core/app/CLAUDE.md` for SKILL.md format and integrity model.

---

## Nanobot-01

- Sidecar on `ai_net` + `business_net` (port 8080); primary skill execution environment
- **Hard boundary**: broker = SYSTEM_COMMANDS whitelist only; nanobot-01 = all application skills
- Protocol contract + dispatch invariants: see `nanobot-01/CLAUDE.md`

---

## node04 — External Services Host (172.16.201.4, VLAN 172.16.201.0/24)

node04 hosts all external-facing AI services that sovereign-core cannot run locally (internet egress, GPU-optional, independent scaling). sovereign-core has **no direct internet egress** — all external calls route through node04 services.

### Port convention (design decision — follow for all future node04 services)

| Port | Service | Status |
|------|---------|--------|
| 8001 | a2a-browser | Running |
| 8003 | a2a-whisper | Planned — pending CC deployment |

**Deployment rules for node04 services:**
- `BIND_ADDRESS` = loopback-only by default during setup
- Promote to VLAN (`172.16.201.x`) explicitly on go-live
- Auth via `X-API-Key` shared secret pattern (same as a2a-browser)
- Port mapping and healthcheck pattern: follow a2a-browser `docker-compose.yml` as template
- Assign next sequential port — never reuse or skip

### Services

- **a2a-browser** (8001): browser fetch + SearXNG search; internet egress; `AUTH_PROFILES` in `browser.py` attach credentials per host; all nanobot browsing also routes through here
- **a2a-whisper** (8003): faster-whisper-server; replaces local whisper container (removed 2026-03-20); `WHISPER_URL=http://172.16.201.4:8003` in `secrets/whisper.env`

### Search backend

- **GitHub Search API**: primary for skill search — `browser.fetch("https://api.github.com/search/code?...")` with PAT headers from AUTH_PROFILES
- **SearXNG** (via a2a-browser): secondary — DDG CAPTCHA-blocked, Google 403-blocked as of 2026-03-19; fallback only
- **Brave / Bing**: dead letters — both retired 2025/2026
- `AUTH_PROFILES` in `execution/adapters/browser.py`: host-keyed header sets, auto-attached in `fetch()` — loaded from `secrets/browser.env` + `/home/sovereign/governance/browser-auth-profiles.yaml` at startup

### Nextcloud access
- LAN direct: `http://172.16.201.25` (port 80, no reverse proxy) — `nextcloud` service, `business_net`
- Tailscale: `https://sovereign.tail887d2b.ts.net` (via `nextcloud-rp` nginx, ports 80/8443 on 100.111.130.60)
- `nextcloud` Docker hostname is a trusted domain (added 2026-03-21) — required for nanobot-01 WebDAV access
- `nextcloud.env` is NOT in sovereign-core env_file — CredentialProxy reads `NEXTCLOUD_ADMIN_USER`/`NEXTCLOUD_ADMIN_PASSWORD` from nanobot-01's static env; `NEXTCLOUD_URL` defaults to `http://nextcloud` in the script

### `_quick_classify` invariants (do not regress)
- File path guard: `_has_file_path` regex prevents year strings inside paths/content from tripping `_time_signals` → web_search
- `_infer_prior_domain`: uses word-boundary regex — substring `"mail"` in `"gmail"` must NOT set prior_domain="email"
- `_is_pronoun_ref`: uses word-boundary regex — substring `"it"` in `"with"` must NOT trigger pronoun resolution
- File delete fast-path (`_file_delete_kw` + slash-path regex) must remain BEFORE safety-net at line ~935 — without it, delete falls through to `intent: query` and Ollama hallucinates success
- PASS 2 security skipped when `confirmed=True` — double-confirmation IS the security gate for HIGH tier
- `_please_prefix` (`u.startswith("please ")`) bypasses the conversational guard — but does NOT route to `remember_fact` on its own; the `_memory_re` check runs first (requires `remember that/this`), then a secondary `please + remember (not to)` check catches bare `please remember X` without losing prospective-task routing for `please remember to X`
- `0x[0-9a-fA-F]{40}` address in a question context → `memory_recall` (exact MatchText search in semantic); extracted address passed as `target` — never sent to Ollama
- `memory_recall` intent: `domain: memory, operation: recall` — synchronous MatchText scroll on `qdrant-archive` semantic collection; returns `{found, query, count, results}` or `{found: false}`; in `_DIAGNOSTIC_INTENTS` so result passes directly to translator
- `remember_fact` is NOT in `_DIAGNOSTIC_INTENTS` — raw execution result (`point_id`, `mip_key`, `collection`) must not reach translator; PASS 4 → translator produces plain-English confirmation instead

### Semantic memory seeding — principle
- **Seed only for critical blind spots**: only seed domain knowledge when Rex has a complete vocabulary gap that prevents basic function (e.g. address format recognition, zero knowledge of a domain the harness depends on)
- **Never seed conceptual relationships**: relationships between concepts (ETH ↔ staking_reward ↔ income) should emerge organically via the associative synthesis pass (`run_synthesis()` Passes 1–3, nightly 15:00 UTC), not hand-crafted entries
- **Before adding a seed**: check semantic memory first — Rex may already have the knowledge from prior conversations; hand-stitching what he already knows is expensive and bypasses the learning process
- **Minimal footprint**: seed vocabulary and format recognition; leave reasoning and relationship-building to synthesis

### Memory task scheduler invariants
- `find_active_by_title()` uses ALL-word matching (≥5 chars) — not ANY — to avoid false dedup positives where unrelated tasks share a single common word (e.g. "nightly")
- `seed_nightly_synthesis_task()` idempotency: checks PROCEDURAL for `intent=memory_synthesise` step; falls through to `store_task` if not found; `store_task` ALL-match dedup is the second gate
- Synthesis cron: `0 15 * * *` (15:00 UTC = 03:00 NZST) — runs Passes 1–3 of `run_synthesis()` (episodic scan → associative/relational); structural Pass 4 fires inline on every semantic write (no schedule needed)
- Associative memory (`associative` collection) is populated ONLY by `run_synthesis()` Passes 1–3 — not by structural Pass 4, not by curate, not by seeding

---

## Sovereign Wallet (Built — Pending First Boot)

- `sov-wallet` container: node:18-alpine Safe Transaction Service proxy; ai_net:3001 + browser_net
- Key material RAID: `sovereign.key/pub`, `wallet-seed.enc` (HKDF+AES-256-GCM), `wallet-seed.gpg` (GPG backup)
- Safe multisig: `0x50BF8f009ECC10DB65262c65d729152e989A9323` — 2-of-3, threshold 2
- Full wallet spec: `/docker/sovereign/docs/Sovereign-v2.md` Wallet section

---

## Secrets Files (`secrets/`)

| File | Purpose |
|------|---------|
| `ollama.env` | Ollama runtime config |
| `whisper.env` | Whisper URL and model |
| `redis.env` | Redis password |
| `nextcloud.env` | MariaDB creds, Redis, Nextcloud admin |
| `grok.env` | Grok API key |
| `telegram.env` | Telegram bot token + authorized user ID |
| `imap-personal.env` | Personal IMAP/SMTP credentials |
| `imap-business.env` | Business IMAP/SMTP credentials |
| `browser.env` | a2a-browser shared secret |
| `nanobot.env` | nanobot-01 shared secret (X-API-Key) |
| `wallet.env` | SAFE_ADDRESS, CHAIN_ID, ETH_RPC_*, BTC_RPC_* |

---

## Phase Status

| Phase | Status | Description |
|-------|--------|-------------|
| 0–5 | **COMPLETE** | Observer, broker, WebDAV/CalDAV/IMAP/SMTP, HIGH tier, Ed25519 signing, Telegram, cognitive loop, agent layer, Qdrant, DCL, metrics |
| 6 | **COMPLETE** | Sovereign Skill System: SkillLoader, SKILL.md, dual-layer integrity, 4 seed skills |
| 6.5 | **COMPLETE** | Skill Lifecycle Manager: SEARCH/REVIEW/LOAD/AUDIT, security review pipeline, config change notification |
| 6.6 | **COMPLETE** | Skills domain in governance.json, GovernanceEngine.get_intent_tier(), skill_install composite, procedural memory seed |
| 7 | **COMPLETE** | Generalised task scheduler: NL→TaskDefinition, Qdrant PROSPECTIVE+PROCEDURAL+EPISODIC, 60s executor |
| W1+W2 | **COMPLETE** | sov-wallet, BIP-39 keygen, HKDF+AES-256-GCM, EIP-712 SafeTx, get_btc_xpub. First boot confirmed 2026-03-31. |
| OC-S1–S3.1 | **COMPLETE** | Python adapters, nanobot-01 sidecar, Model B DSL, broker CLI exec |
| OC-S4 | **COMPLETE** | Community skills (imap-smtp-email, openclaw-nextcloud), skill_install flow, confirmed-continuation bypass |
| OC-S5 | **COMPLETE** | nanobot-01 as primary executor, CredentialProxy single-use token delegation |
| OC-S6 | **COMPLETE** | python3_exec cutover, imap/smtp/nextcloud/feeds.py scripts, rss-digest skill, route_cognition PASS 2 wiring |
| CL-Rework | **COMPLETE** | 5-pass cognitive loop, InternalMessage envelope, nanobot protocol contract, untrusted tagging |
| E2E-S1 | **COMPLETE** | End-to-end Telegram routing hardening: RSS feeds, skill search, clawhub routing, translator leakage, morning briefing serialisation, enrichment truncation fix |
| OC-S7 | **COMPLETE** | openclaw-nextcloud +6 ops (files_read/write/delete/mkdir, calendar_update, tasks_delete); SKILL.md + checksums updated |
| MIP-S1+S2 | **COMPLETE** | Memory Index Protocol (ContextKeep v1.2): deterministic key/title generation, two-step retrieve protocol, session audit trail |
| MIP-Fix+S4 | **COMPLETE** | MIP routing hardening (memory_index short-circuit, delegation passthrough, _system_signals); canonical key backfill: 13 keys across wallet/networking/infrastructure/governance domains; tag_high_value_entries() for no-re-embed patching |
| NC-E2E | **COMPLETE** | Nextcloud end-to-end testing T1→T12 all passing: LAN port binding, trusted domain fix, Grok model update, routing hardening, delete fast-path, PASS 2 skip on confirmed, recursive list/read (T12) |
| Skill-Search | **COMPLETE** | Full skill search stack fixed: direct httpx for GitHub API, A2A double-nesting, query extraction, translator isolation (skill_md stripped), prior-domain install routing |
| Skill-Install-Fix | **COMPLETE** | confirmed-continuation bypass fixed: `confirmed=True` now passed via `payload={"confirmed": confirmed}` to `_dispatch_inner`; short-circuit to `lifecycle.load()` works correctly |
| Email-E2E | **COMPLETE** | delete + move operations (SKILL.md + imap_check.py); email list format (numbered, `sender — subject (date)`); context_window passed to specialist_outbound; account defaulting fixed; `_msg` envelope shadowing fixed |
| Qdrant-2tier | **COMPLETE** | Two-tier memory: NVMe conscious hot layer + RAID subconscious archive (`qdrant-archive`); truly ephemeral `working_memory` via `AsyncQdrantClient(location=":memory:")`; startup/shutdown sync + hourly `sync_to_archive`; graceful shutdown guarantee (`stop_grace_period: 30s`, uvicorn `--timeout-graceful-shutdown 25`) |
| Email-Fix | **COMPLETE** | Delete + move ops; numbered email list pre-formatter (`N. Sender — Subject (Date) [uid:XXXX]`); `_original_request` preservation for confirmed-continuation; account carry-forward from context_window; `fetch_message` fast-path; file listing post-formatter |
| NC-Mail | **COMPLETE** | `nc-mail` python3_exec skill (9 ops). Stable `databaseId` integers. Personal inbox graceful timeout (57s cap, `status:"ok"` with note). Client-side filter for search. Deterministic ID extraction in `_quick_classify`. `_DIAGNOSTIC_INTENTS` for delete/move/send with outcome stamps. `_unwrap_nb` preserves status/success for `execution_confirmed`. T-M1–T-M7 all passing 2026-03-25. |
| NC-Notes | **COMPLETE** | 5 ops (notes_list/read/create/update/delete) added to nextcloud.py + SKILL.md + governance.json (v1.16) + engine.py. All 6 tests T1–T6 passing 2026-03-24. |
| Notes-Index | **COMPLETE** | Session-scoped title→ID index on ExecutionEngine (5 min TTL). `_notes_get_or_build_index()` + `_notes_find_by_title()` (exact→substring→reverse-substring match). Index built on `list_notes`; auto-fetched cold on read/update/delete by title. Note-suffix classifier: "read/delete/update the [title] note" → deterministic regex extracts title → stored in `delegation["target"]`. `_resolve_note_id()` handles numeric vs title strings. UAT complete 2026-03-31: list ✓ create ✓ update-by-ID ✓ delete-by-title ✓. |
| NC-Index-Universal | **COMPLETE** | Universal Item Index in working_memory. All items Rex processes with a stable ID (notes, events, emails, files) are indexed as zero-vector Qdrant entries (`_item_index: True`). Point IDs are deterministic UUID5(`namespace`, `"{item_type}:{item_id}"`) — idempotent re-indexing. `_index_items()`, `_lookup_item()`, `_clear_item_index()` on ExecutionEngine. Content blobs (web search, RSS, file reads) stored as episodic entries with real embed vectors (promotable to RAID). 2026-03-31. |
| Skill-Harness | **COMPLETE** | Stateful multi-step skill lifecycle harness in engine.py + working_memory. search→list→review→install→clear. Pre-scan gate, WM checkpoint, HIGH-tier confirm gate. E2E tested 2026-03-23. |
| SI-Harness | **COMPLETE** | Self-improvement harness (monitoring/self_improvement.py). Daily observe loop, baseline+anomaly detection, proposal generation with Director approval gate. Primary autonomy boundary. 2026-03-23. |
| Dev-Harness | **COMPLETE** | 4-phase code quality harness (Analyse→Classify→Plan→Execute). pylint+semgrep+boundary_scanner+GitHub Actions. LLM advisory (Ollama+Claude escalation via DCL). CC runsheet HITL handoff. Memory integration: episodic/meta/semantic/prospective. Nightly cron 14:00 UTC. Self-scan: 0 findings. 2026-03-25. |
| Scheduler+SI-Fixes | **COMPLETE** | `_get_procedure`/`_find_point_id` limit=200 ceiling replaced with filtered Qdrant queries. `seed_nightly_dev_task` idempotency via PROCEDURAL step check (title-based unreliable — `qdrant.store()` overwrites title). SI proposal dedup gate (`_existing_pending_proposal`). Task data cleanup (5 cancelled). 2026-03-26. |
| Translator-Bleed-Fix | **COMPLETE** | Deterministic `_translator_sanitise()` strips meta-commentary sentences/bullets before Telegram delivery. Retry-once on >30% strip with EPISODIC violation log. Health brief re-routed through `translator_pass()`. B5 boundary rule detects leak phrases baked into string literals in cognition/gateway path. Validator Queue monitor updated: Entry+Exit extraction, 7-day alert threshold. 2026-03-26. |
| Skill-Install-Hardening | **COMPLETE** | Scanner false-positive fixes: `identity_override` + `prompt_injection_regex` removed from `_INJECT_HARD_BLOCK` (legitimately appear in security docs). LLM review removed from Phase B — Director confirmation is the security gate. OpenClaw certification: `certified=True` for `github.com/openclaw/skills`. `_install_select_best` deterministic for single certified candidate. Confirm gate shows clawhub.ai scan (llmAnalysis + vtAnalysis from SSR HTML). Hard-block on malicious/suspicious clawhub verdict. NL routing simplified. 2026-03-28. |
| WDK-Skill | **COMPLETE** | Tether Wallet Development Kit skill installed from OpenClaw registry (certified). Active for: research_agent. 2026-03-28. |
| Wallet-A2A-Rail | **COMPLETE** | `wallet/credit` push notification in `/wallet_event` endpoint (`main.py`). Full sequence: dedup (Qdrant `domain: wallet.a2a_credit`, UUID5 point ID) → mark seen IMMEDIATELY before normalisation → CoinGecko USD price (failure blocks, never estimate) → A2A 3.0 `wallet/credit` payload with Ed25519 sig → POST `{A2A_BROWSER_URL}/run` with `X-API-Key` → retry once (background asyncio.create_task, 30s delay) → alert Director on permanent failure. `duplicate: true` from a2a-browser is not an error. MIP seeds added: `semantic:network:endpoints:a2a-browser` + `semantic:wallet:pricefeed:coingecko`. 2026-03-28. |
| UAT-S1 | **COMPLETE** | Full UAT session 2026-03-31: weather ✓, browser ✓, nextcloud-ingest ✓, notes-index-delete ✓, security-audit ✓, memory-curate ✓, session-wrap-up (routing fixed). Attachment upload chain fixed (3 bugs: `_ledger` attr, argparse hyphens→underscores, WebDAV URL encoding for spaces). `delete_note` tier HIGH→MID. `ingest_status` routing moved before `ingest` keyword block. |
| Scheduler-TZ-Fix | **COMPLETE** | Task scheduler NZ timezone fix. `task_intent_parser` prompt: mandatory UTC conversion rules (NZST=UTC+12, NZDT=UTC+13), corrected cron examples, `read_feed`+`list_events` added to available intents. `_execute_task`: dynamic `"today"` substitution in step params. `store_task`: dedup guard via `find_active_by_title()` — refuses duplicate active tasks. Gateway context FIFO (`write_gateway_context`/`get_gateway_context`) removed from qdrant.py + engine.py (redundant with context_window). Weekday Morning Briefing task created: cron `30 20 * * 0-4` UTC = Mon-Fri 8:30 AM NZST; steps: personal email → business email → news → today's calendar events. 7 stale briefing tasks cancelled. 2026-03-31. |
| PM-Harness | **PROPOSAL — PENDING DIRECTOR APPROVAL** | Full SDLC project management harness against sovereign semantic memory. Proposal in prospective memory. Requires Dev-Harness first. |
| Retry-Logic | **PROPOSAL — PENDING DIRECTOR APPROVAL** | Exponential backoff (max 3) for all harnesses, surface failure to Director. Proposal in prospective memory. |
| Adapter-Removal | **COMPLETE** | Removed BrowserAdapter, WebDAVAdapter, CalDAVAdapter from sovereign-core. All application I/O through nanobot-01 only. Phase 1 Browser 2026-04-03, Phase 2 WebDAV 2026-04-03, Phase 3 CalDAV 2026-04-03. |
| Ops-Fixes-Apr8 | **COMPLETE** | Runaway scheduler task cancelled; ANTHROPIC_API_KEY startup warning; translator_pass failure detail improved; gateway ConnectError/HTTPStatusError handlers; soul guardian 7 files rebaselined; Grok explicit-only routing in domain:ollama; semantic entries for claude-api/grok-api. 2026-04-08. |
| News-Harness | **COMPLETE** | `monitoring/news_harness.py`. Parallel RSS+Grok+browser fetch; 60% word-set dedup; single synthesis pass. `news_brief` intent in INTENT_ACTION_MAP. `_news_kw` block in `_quick_classify` (14 patterns). Reads `semantic:preferences:news`. Returns `{"brief": "..."}`. 2026-04-08. |
| Morning-Brief-Refactor | **PENDING DIRECTOR ORDER** | Replace `read_feed` step with `news_brief` harness; add `list_nc_tasks` step. 3 changes: engine.py (INTENT_ACTION_MAP + dispatch), task_scheduler.py (`_format_step_content` brief key), qdrant-archive PROCEDURAL point `c6ee061f` steps array. See memory/project_sovereign.md for full spec. |
| Skill-Seed-v2 | **COMPLETE** | `semantic_seeds.py` enriched: `build_skill_seeds()` now parses SKILL.md frontmatter (description, operations with inputs/outputs, specialists, tier) into rich content. `seed_id` bumped to `v2_` with `_prev_seed_id` for v1 cleanup on next restart. `seed_intent_semantic_entries()` in qdrant.py handles `_prev_seed_id` delete-then-create upgrade. `lifecycle.load()` passes description + operations to `make_skill_semantic_seed()`. `build_tax_address_seeds()` writes `semantic:tax:taxable_wallets`, `semantic:tax:staking_contracts`, `semantic:tax:internal_addresses` placeholder entries. 2026-04-10. |
| Tax-Ingest-Harness | **COMPLETE** | Hourly NZ tax event ingestion: Nextcloud /Digiant/Tax/ CSV/PDF files + on-chain wallet events. Two tags: tax:crypto, tax:expense. CoinGecko NZD pricing. Scheduled cron `0 * * * *` — pending_approval until Director activates. |
| Tax-Report-Harness | **COMPLETE** | /do_tax [year] command. 3-turn human-in-the-loop: query semantic (date range) → Director provides expense CSVs → confirm → generates income{year}.csv + expenses{year}.csv in /Digiant/Tax/FY{year}/. Classifier labels crypto events. FIFO stub (Phase 3). |

Full phase history: `docs/CLAUDE-archive.md`

---

## Architectural Principles — Multi-Step Skill Execution

These principles apply to ALL multi-step skill execution (harnesses, lifecycle, scheduler tasks). Mandatory from 2026-03-23.

### 1. Mandatory Validation Gates

Every skill involving more than one sequential action MUST validate success before proceeding to the next step. No step may proceed on an unvalidated prior result.

**Per-step validation must confirm:**
- Expected response schema present (required fields non-null)
- Non-empty result (no empty list/dict where data is expected)
- No `error` flag or `success: false`
- Any domain-specific invariants (e.g. manifest has ≥1 candidate, file hash matches)

**On gate failure:**
- Halt immediately — do not proceed to next step
- Log failing step + reason to audit ledger AND episodic memory
- Return structured failure: `{"success": false, "failed_step": "...", "reason": "...", "last_checkpoint": "..."}`
- Never silently skip or retry without logging

### 2. Working Memory Checkpointing

Multi-step skills MUST write a checkpoint to `working_memory` at each validation gate.

**Checkpoint schema:**
```json
{
  "skill_name": "skill-harness",
  "session_id": "<uuid>",
  "current_step": "review",
  "step_results": {
    "search": {"candidates": [...], "ts": "..."},
    "fetch": {"candidate_id": 2, "slug": "...", "ts": "..."},
    "review": {"verdict": "approve", "risk_level": "low", "ts": "..."}
  },
  "last_checkpoint_ts": "...",
  "_skill_harness_checkpoint": true
}
```

**Invariants:**
- Checkpoint is written AFTER validation passes, BEFORE responding to Director
- Checkpoint is read at the start of each step to verify prerequisites
- If checkpoint is missing when a non-first step is invoked → fail with "run X first"
- Checkpoint persists for the session (working_memory lifetime); wiped on `clear` command

### 3. Prospective Task Approval Flow

Any new prospective memory entry representing a **recurring scheduled task** must be surfaced to the Director for explicit approval before becoming `active`.

**Flow:**
1. Rex proposes task → writes `status: "pending_approval"` to prospective memory
2. Rex presents proposal to Director: title, schedule, steps, stop_condition
3. Director confirms → Rex sets `status: "active"` → scheduler picks it up
4. Director rejects → Rex sets `status: "cancelled"`

**Implementation:** `task_scheduler.create_task()` must default to `status: "pending_approval"` for all new recurring tasks. One-time reminders may activate immediately. Director approval changes status to `"active"`.

**Note:** Tasks created directly (e.g. programmatically by Claude Code) are exempt — this applies to tasks created by Rex in response to Director natural language requests.

---

## Harness Pattern — Standard Structure for All Multi-Step Capabilities

All future multi-step capabilities in sovereign-core follow this pattern. A harness is a **stateful step orchestrator** implemented in `execution/engine.py` (or a dedicated module imported by it), backed by `working_memory` session keys.

### Session key naming convention

```
{capability}:session      # e.g. skill_harness:session, self_improvement:session
```

The session payload flag (`_skill_harness_checkpoint`, `_self_improvement_session`, etc.) distinguishes the record type in working_memory scrolls. All session keys are ephemeral — they live in working_memory (NVMe in-process RAM) and are re-established on demand.

### Standard checkpoint format

```json
{
  "{flag_key}": true,
  "session_id": "<uuid>",
  "current_step": "<step_name>",
  "step_results": {
    "<step_a>": {"<result_fields>": "...", "ts": "<iso>"},
    "<step_b>": {"<result_fields>": "...", "ts": "<iso>"}
  },
  "last_checkpoint_ts": "<iso>"
}
```

`{flag_key}` is the unique bool field used to identify this checkpoint type when scrolling working_memory (e.g. `_skill_harness_checkpoint`, `_self_improvement_session`).

### Step sequence rules

1. **First step** — writes initial checkpoint to working_memory after validation gate passes
2. **Each subsequent step** — reads checkpoint at entry, verifies prerequisites, executes, updates checkpoint
3. **If checkpoint missing on non-first step** → return `{"status": "no_checkpoint", "message": "Run <first_step> first"}`
4. **Clear step** — deletes all working_memory points with the flag key set; no side effects

### Validation gate requirements

Every gate must confirm before writing checkpoint and proceeding:
- Required fields present and non-null
- No `error` field set; no `success: false`
- Domain-specific invariants (e.g. ≥1 candidate found, verdict is not `block`, file hash matches)

On gate failure: halt, log to audit + episodic, return `{success: false, failed_step, reason, last_checkpoint}`.

### Intent registration

Each harness step maps to an intent in `INTENT_ACTION_MAP` and `INTENT_TIER_MAP`:
- All read/observe steps: LOW tier
- Destructive/commit steps: MID or HIGH tier (requires Director confirmation)
- Clear step: LOW tier

All harness intents are added to the translator bypass list so structured output reaches Director directly.

### Implemented harnesses

| Harness | Session key flag | Steps | Notes |
|---------|-----------------|-------|-------|
| Skill-Harness (`/install`) | `_skill_harness_checkpoint` | search → LLM select → confirm → scan → install | Single Director gate: confirm before scan+install |
| SI-Harness | `_self_improvement_session` | observe (daily auto) → propose (auto-triggered) | Director approves proposals; never self-modifies |
| Dev-Harness | `_developer_harness_checkpoint` | analyse → status → approve/reject → verify → clear | Nightly cron; Director approves findings |
| Tax-Ingest-Harness | `_tax_ingest_harness_checkpoint` | check → ingest → enrich → store → notify → clear | Hourly cron; continuous tax event ingestion |
| Tax-Report-Harness (`/do_tax`) | `_tax_report_harness_checkpoint` | query → ingest → create → notify → clear | 3-turn human-in-the-loop; generates income + expenses CSVs |

### /command harness architecture

**Design principle**: What is deterministic should be deterministic; what needs reasoning is the LLM.
- Harness step sequencing → deterministic (checkpoint state drives next action)
- Candidate/option selection → LLM
- Security pattern scanning → deterministic (scanner.scan)
- Security verdict → LLM (interprets scanner + SKILL.md intent)
- Install/write/delete → deterministic (adapter call after confirmed)

**Implemented /commands** (registered in gateway.py + BotFather):

| Command | Harness | Behaviour |
|---------|---------|-----------|
| `/install <goal>` | Skill-Harness | Autonomous: search → LLM picks best → confirm gate → scan → install |

**Implemented /commands:**

| Command | Harness | Notes |
|---------|---------|-------|
| `/install <goal>` | Skill-Harness | Autonomous: search → LLM picks best → confirm gate → scan → install |
| `/selfimprove` | SI-Harness | Run observe+propose cycle; surface pending proposals to Director |
| `/devcheck` | Dev-Harness | Run full analysis cycle; surface findings requiring approval |
| `/portfolio` | Portfolio-Harness | Trigger snapshot; return current balances + NZD/USD value |
| `/do_tax [year]` | Tax-Report-Harness | Generate NZ tax report for FY; 3-turn human-in-the-loop; produces income+expenses CSVs |

**Pending /commands** — build when harnesses are ready:

| Command | Harness | Notes |
|---------|---------|-------|
| `/pm <action>` | PM-Harness | Project management harness (PROPOSAL — pending Director approval) |

All /commands:
- Bypass NL routing entirely (no `_quick_classify`)
- Are detected in `gateway.py` as `CommandHandler` entries
- Pass `_harness_cmd` field to `/chat` endpoint
- Are recovered from `pending_delegation._harness_cmd` on confirmed continuation
- Have a single Director confirmation gate at the action point, not at every step

---

## Tax Ingest Harness — Canonical Reference (Phase 1 refactored 2026-04-12)

**Files**: `core/app/tax_harness/` — `__init__.py`, `models.py`, `pricing.py`, `ingest.py`, `wallet_events.py`, `harness.py`

**Design principle**: Ingestion is dumb and fast — records what happened faithfully.
All tax treatment (income / disposal / internal transfer) is determined at report time by `/do_tax`,
which has full access to the known address lists in semantic memory.

### Event tags (exactly two)

**`tax:crypto`** — any on-chain or exchange transaction involving a known address.
Stores: timestamp, from_address, to_address, asset, amount, tx_hash, nzd_value (None if unavailable), tax_year, source, reference.

**`tax:expense`** — a receipt, invoice PDF, or fiat card spend row from CSV.
Stores: timestamp, vendor, amount_nzd, source (filename), reference (row external_id or filename), tax_year.

No other event tags exist at ingest time.

### Canonical TaxEvent schema
```
id:           UUID5(NAMESPACE_URL, "tax:{reference}") — deterministic dedup
event_tag:    "tax:crypto" | "tax:expense"
timestamp:    ISO8601 UTC
tax_year:     NZ FY e.g. "2026" (Apr 1 YYYY → Mar 31 YYYY+1 → tax_year = YYYY+1)
source:       filename (CSV/PDF) or chain identifier (on-chain)
reference:    tx_hash (on-chain) or "{source}:{external_id}" (CSV)
nzd_value:    "$X.XX NZD" or None — None → tag pricing_unresolved (crypto only)

# tax:crypto fields
from_address: str — wallet address or "wirex:account" / "swyftx:account" for exchange trades
to_address:   str — wallet address or exchange account identifier
asset:        ETH | BTC | etc.
amount:       "0.001000 ETH" formatted string
tx_hash:      str — on-chain hash or exchange order ID

# tax:expense fields
vendor:       merchant name or PDF source label
amount_nzd:   "$X.XX NZD" formatted string
```

### Ingestion rules

**wallet_events.py** (on-chain):
- Every event pushed by the wallet watcher → write tax:crypto event unconditionally
- No address filtering — the wallet watcher is the filter (only pushes watched-address txs)
- No classification, no address list lookup

**ingest.py** (CSV/PDF):
- Wirex CSV — crypto currency rows → tax:crypto (from/to = "wirex:account"); NZD rows → tax:expense
- Swyftx CSV — all rows → tax:crypto (from/to = "swyftx:account")
- PDF receipts → tax:expense

### Semantic address lists
- `semantic:tax:taxable_wallets` — Director-populated; used by the **wallet watcher** to decide what to push; also used by `/do_tax` at report time to identify which transaction side belongs to the Director
- `semantic:tax:staking_contracts` — Director-populated; used by `/do_tax` at report time only
- `semantic:tax:internal_addresses` — NOT seeded; internal/external classification is report-time only

### Director data required (before harness is useful)
- `semantic:tax:taxable_wallets`: Rex ETH `0x623061184E86914C07985c847773Ee8e7ac6d508`; mining address `0x2c228a2d04d65E54dE6b24885C1D3626098C776e`
- `semantic:tax:staking_contracts`: Rocket Pool deposit pool address

### Harness steps
| Step | Tier | Notes |
|------|------|-------|
| check | LOW | skill calls returned without error (empty = valid) |
| ingest | LOW | files + pending wallet events from working_memory |
| enrich | LOW | CoinGecko NZD pricing for tax:crypto; expense nzd_value = amount_nzd |
| store | MID | UUID5 upsert to SEMANTIC; partial failure → log + continue |
| notify | LOW | reports tax:crypto count, tax:expense count, nzd_value null count |
| clear | LOW | deletes _tax_ingest_harness_checkpoint entries from working_memory |

Session flag: `_tax_ingest_harness_checkpoint`
Session key: `tax_ingest:session`
Scheduled task: cron `0 * * * *` (hourly UTC), status `pending_approval` until Director activates

### Dedup
`uuid.uuid5(uuid.NAMESPACE_URL, f"tax:{reference}")` — deterministic.
Same event arriving twice → same UUID → Qdrant silently overwrites, no duplicate.

---

## Tax Report Harness — Canonical Reference (Phase 2, 2026-04-12)

**Trigger**: `/do_tax [year]` Telegram command — bypasses NL routing entirely.
**Module**: `core/app/tax_harness/report_harness.py`
**Session flag**: `_tax_report_harness_checkpoint`

### NZ tax year date range
Tax year YYYY = `{YYYY-1}-04-01T00:00:00Z` → `{YYYY}-03-31T23:59:59Z`
e.g. `/do_tax 2026` → 01 Apr 2025 to 31 Mar 2026

### Human-in-the-loop flow (3 turns)

| Turn | Director action | Rex action |
|------|----------------|------------|
| 1 | `/do_tax [year]` | Query semantic by date range; classify crypto; report counts; ask for expense CSV names |
| 2 | CSV filenames or "none" | Fetch+parse CSVs from Nextcloud (not stored); merge expense array; report counts; ask confirm |
| 3 | Confirm | Generate + save income{year}.csv and expenses{year}.csv; notify; clear |

### Classifier labels (income.csv Classification column)
`staking_reward` · `exchange_acquisition` · `exchange_disposal` · `internal_transfer` · `unknown_inbound` · `unknown_outbound` · `unknown`

### Output files (saved to /Digiant/Tax/FY{year}/)
- `income{year}.csv` — Date, Classification, From Address, To Address, Asset, Amount, NZD Value, Source, Reference
- `expenses{year}.csv` — Date, Vendor, Amount NZD, Source, Reference

### Expense data sources
1. Semantic memory `tax:expense` events in date range (Phase 1 ingested from /Digiant/Tax/)
2. Director-specified CSVs parsed at report time — merged into array, NOT stored to memory, NOT tagged

### New modules
- `classifier.py` — `classify_events(events, qdrant) -> ClassifiedEventSet`; pure Python, no LLM
- `fifo.py` — stub; `run_fifo()` returns empty `FifoResult`; FIFO calculations deferred to Phase 3
- `report_harness.py` — `TaxReportHarness`; 5 steps (query → ingest → create → notify → clear)

### Step intents
| Step | Intent | Tier |
|------|--------|------|
| query | `tax_report_query` | LOW |
| ingest | `tax_report_ingest` | LOW |
| create | `tax_report_create` | LOW |
| notify | `tax_report_notify` | LOW |
| clear | `tax_report_clear` | LOW |
| pre-flight | `tax_ingest_status` | LOW |

### Prerequisites
- `semantic:tax:taxable_wallets` — must be populated for accurate classifier output
- `semantic:tax:staking_contracts` — must be populated for staking_reward classification
- `/do_tax` must be registered with BotFather manually (CC cannot do this)

---

## Phase Close Checklist
1. Append signed-off entry to `/home/sovereign/docs/as-built.md` (RAID)
2. Update phase status table above
3. Update `/home/matt/.claude/projects/-docker-sovereign/memory/MEMORY.md`

---

## Rebuild Workflow
```bash
cd /docker/sovereign
docker compose build sovereign-core
docker compose up -d sovereign-core
docker compose ps
curl http://localhost:8000/health
```

## Test Governance
```bash
# LOW — no confirmation
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"action":{"domain":"docker","operation":"read"},"tier":"LOW"}'

# MID — requires_confirmation
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"action":{"domain":"docker","operation":"workflow","name":"restart"},"tier":"MID"}'
```

---

## Plan: Adapter Removal — Browser → WebDAV → CalDAV

**Principle**: No duplicate of what nanobot does. All application I/O (browser, Nextcloud files, calendar/tasks) routes through nanobot-01. Direct adapters in sovereign-core are legacy and must be removed.

**Context**: BrowserAdapter was calling `POST /run` (non-existent on a2a-browser) — fixed to use `POST /search` / `POST /fetch` in session 2026-04-01. Adapters exist at `core/app/execution/adapters/browser.py`, `core/app/adapters/webdav.py`, `core/app/adapters/caldav.py`.

---

### Phase 1: Browser (do first)

**What needs adding to nanobot** — `sovereign-browser` SKILL.md exists but has no python3_exec script; currently routes through BrowserAdapter.

1. Add `secrets/browser.env` to nanobot-01 `env_file` in `compose.yml` — injects `A2A_BROWSER_URL` + `A2A_SHARED_SECRET`
2. Create `nanobot-01/workspace/skills/sovereign-browser/scripts/browser.py` — stdlib+requests, commands: `search` (POST `/search`) and `fetch` (POST `/fetch`) against a2a-browser; read `A2A_BROWSER_URL` + `A2A_SHARED_SECRET` from env; output flat JSON matching a2a-browser response schema
3. Update `/home/sovereign/skills/sovereign-browser/SKILL.md` — change `tool: browser` → `tool: python3_exec`, add `script: scripts/browser.py`, update args for search/fetch commands
4. Update `engine.py` `domain == "browser"` dispatch — replace `self.browser.search()`/`self.browser.fetch()` with `self.nanobot.run("sovereign-browser", "search"|"fetch", params)`; result data is in `nb["result"]` not `nb["data"]`; keep existing ledger logging and response formatting
5. Remove `from execution.adapters.browser import BrowserAdapter` and `self.browser = BrowserAdapter()` from engine.py
6. Delete `core/app/execution/adapters/browser.py`
7. Rebuild nanobot-01 + sovereign-core; test: ask Rex to search the web

**AUTH_PROFILES note**: `_build_fetch_payload` auth injection was dead code (a2a-browser `FetchRequest` has no `auth` field). Skip for now — add when a2a-browser supports it.

---

### Phase 2: WebDAV

**Nanobot already covers all operations** via `sovereign-nextcloud-fs` (nc_fs.py) and `openclaw-nextcloud` (nextcloud.py).

| Engine intent | Old call | New nanobot call |
|---|---|---|
| `file_navigate` | `webdav.navigate(path)` | `nanobot.run("sovereign-nextcloud-fs", "fs_list", {"path": path})` |
| `list_files` | `webdav.list(path)` | `nanobot.run("sovereign-nextcloud-fs", "fs_list", {"path": path})` |
| `read_file` | `webdav.read(path)` | `nanobot.run("sovereign-nextcloud-fs", "fs_read", {"path": path})` |
| `write_file` | `webdav.write(path, content)` | `nanobot.run("openclaw-nextcloud", "files_write", {"path": path, "content": content})` |
| `delete_file` | `webdav.delete(path)` | `nanobot.run("sovereign-nextcloud-fs", "fs_delete", {"path": path})` |
| `create_folder` | `webdav.mkdir(path)` | `nanobot.run("sovereign-nextcloud-fs", "fs_mkdir", {"path": path})` |
| `search_files` | `webdav.search(query, path)` | `nanobot.run("sovereign-nextcloud-fs", "fs_search", {"query": query, "path": path})` |
| `list_files_recursive` | already nanobot ✓ | unchanged |
| `read_files_recursive` | already nanobot ✓ | unchanged |

**RAID path exception** — keep as-is: `list_files`/`read_file` for `/home/sovereign/` and `/docker/sovereign/` paths → `broker.read_host_file()`. No WebDAVAdapter involved.

Steps:
1. Rewire engine.py `domain == "webdav"` block — replace each `self.webdav.*` with nanobot calls per table above; preserve RAID path exception for broker
2. Remove `from adapters.webdav import WebDAVAdapter` and `self.webdav = WebDAVAdapter()` from engine.py
3. Delete `core/app/adapters/webdav.py`
4. Rebuild sovereign-core; test: list/read/write/delete Nextcloud file

---

### Phase 3: CalDAV

**Nanobot already covers all operations** via `openclaw-nextcloud` (nextcloud.py).

| Engine intent | Old call | New nanobot call |
|---|---|---|
| `list_calendars` | `caldav.list_calendars()` | `nanobot.run("openclaw-nextcloud", "calendar_list", {})` |
| `list_events` | `caldav.list_events(cal, from, to)` | `nanobot.run("openclaw-nextcloud", "calendar_list", {"calendar": cal, "from_date": from, "to_date": to})` |
| `create_event` | `caldav.create_event(...)` | `nanobot.run("openclaw-nextcloud", "calendar_create", {...})` |
| `update_event` | `caldav.update_event(...)` | `nanobot.run("openclaw-nextcloud", "calendar_update", {...})` |
| `delete_event` | `caldav.delete_event(...)` | `nanobot.run("openclaw-nextcloud", "calendar_delete", {"uid": uid})` |
| `create_task` | `caldav.create_task(...)` | `nanobot.run("openclaw-nextcloud", "tasks_create", {...})` |
| `complete_task` | `caldav.complete_task(cal, uid)` | `nanobot.run("openclaw-nextcloud", "tasks_complete", {"uid": uid})` |
| `delete_task` | `caldav.delete_task(cal, uid)` | `nanobot.run("openclaw-nextcloud", "tasks_delete", {"uid": uid})` |

**Note**: All event field extraction (summary, start, end, UID, calendar name) from specialist output stays in engine.py — only the final adapter call changes.

Steps:
1. Rewire engine.py `domain == "caldav"` block — replace each `self.caldav.*` with nanobot calls; pass pre-processed fields as nanobot payload
2. Remove `from adapters.caldav import CalDAVAdapter` and `self.caldav = CalDAVAdapter()` from engine.py
3. Delete `core/app/adapters/caldav.py`
4. Rebuild sovereign-core; test: list events, create event, complete task
