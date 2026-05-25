# Sovereign AI — Claude Code Context

## Design Documents
- Full architecture: `/home/sovereign/sovereign/docs/Sovereign-v2.md`
- Phase 3 detail: `/home/sovereign/sovereign/docs/Sovereign-Phase_3.md`
- Cognitive loop rework: `/home/sovereign/sovereign/docs/Sovereign-CognitiveLoopRework.md`
- Always consult before making architectural decisions.

---

## Standing Design Orders

These rules apply in every session. They are not negotiable and cannot be overridden by inline task instructions.

1. **Know the distinction: OpenClaw registry vs Skill Harness.** OpenClaw is the upstream community skill registry (clawhub.ai). Skill Harness is the local multi-step install orchestrator in sovereign-core. The rule: if OpenClaw has a certified skill covering a capability, install it via the Skill Harness and execute via nanobot-01. Never build bespoke when a certified community skill covers the need.

2. **Canonical location: single source of truth, no copies.** Bespoke capability = single canonical location; no duplicates anywhere in the codebase. Updates happen in-place at the canonical location only — no shadow copies, no versioned copies (e.g. `skill_v2.py`), no inline patches. The same capability surfacing in two invocation points is a bug, not a pattern.

3. **One implementation, one invocation point — no duplicates regardless of location.** The same capability must never exist in two places (e.g. inline in engine.py AND in a nanobot skill). If it exists in two places it is a bug to be fixed, not a pattern to follow.

4. **Engine.py is orchestration only.** Capability logic belongs in skill modules or dedicated harness modules. Engine.py may extract parameters, route, and normalise responses — it must not implement capability logic inline.

5. **Every skill (nanobot or bespoke) has exactly one semantic memory entry pointing to its canonical trigger.** Key format: `semantic:intent:{slug}`.

6. **New skill created or installed → semantic memory entry written at that time.** The Skill Harness install step writes the entry automatically. Bespoke modules must write their entry in their own creation task.

7. **New component created → sov_id assigned + semantic memory entry written at that time.** No component is fully created until its semantic entry exists.

8. **Deprecation → episodic entry written, semantic entry marked inactive, no deletion.** Inactive entries remain for historical integrity. Never delete semantic or episodic entries.

9. **Foundational entities use `entity_type`; system components use `component_type`. Never both on the same entry.** A foundational entity (`sovereign_entity` class) qualifies for a sequential sov_id only if it: (1) exists independently of Sovereign, (2) is bootstrap critical, (3) has a durable named relationship to the sovereign root, and (4) has been explicitly approved by the Director. Sequential sov_ids are assigned in `entity_registry.py` (append-only, never re-assigned). All other entities receive UUID5 sov_ids. The sovereign root entry is `semantic:entity:sovereign` (not `semantic:component:sovereign`).

10. **Harness session checkpoint flags — naming and deviations.** The three canonical harness checkpoint flags are: `_skill_harness_checkpoint` (Skill-Harness), `_self_improvement_session` (SI-Harness), `_developer_harness_checkpoint` (Dev-Harness). The SI harness uses `_self_improvement_session` rather than `_self_improvement_harness_checkpoint` — this deviation is intentional because the SI harness maintains rolling session state rather than a step-gated checkpoint. Do not rename without Director approval.

11. **`_DIAGNOSTIC_INTENTS` = structured result bypasses PASS 4 narrative and passes directly to the translator.** Only add an intent to this list when the raw execution result is itself the Director-facing output and no PASS 4 narrative is needed or safe (e.g. `memory_recall`, `memory_list_keys`, harness status intents). Never add intents that return raw internal state.

12. **Director-directed create/write operations write a semantic memory entry to qdrant-archive on confirmed success, non-blocking via `asyncio.create_task()`. Read operations never write semantic memory under any circumstance. Delete operations mark the corresponding semantic entry `status: historical` in qdrant-archive on confirmed success — no physical Qdrant point deletion, ever. Historical entries survive in qdrant-archive indefinitely and are excluded from PASS 0 working_memory consultation. The nightly associative synthesis pass may traverse historical entries — historical facts remain valid for relationship inference.**

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
- `business_net`: nextcloud, nc-redis, nc-db, nginx, sovereign-core (dual-homed)
- `browser_net`: (no local container; compose-managed for future use)
- sovereign-core dual-homed (ai_net + business_net); a2a-browser on node04 (172.16.201.4:8001, external)

### Security Boundaries (hard rules — do not violate)
- `docker.sock` → broker container only
- `sovereign-core` → no privileged mounts, no docker.sock
- Ollama API → ai_net only, no host exposure
- Sovereign API → `127.0.0.1:8000` loopback only
- Nextcloud → business_net only
- **Fabrication firewall:** `result_for_translator` from PASS 4 is the only content PASS 5 (translator) receives — never raw adapter output, never specialist output. PASS 5 is isolated to this field only.

### GPU (EVGA RTX 3090, 24GB VRAM — power-capped at 300W, SF750 PSU constraint)
- Ollama uses ~20 GB (qwen2.5:32b-instruct-q4_K_M); also has llama3.1:8b and mistral:7b installed
- 24GB VRAM means qwen2.5:32b fits fully on-GPU; ~3× faster than RTX 3060 Ti but model is 4× larger
- Whisper (node04:8003) runs remotely — no VRAM contention with local Ollama
- **ollama-embed runs CPU-only (OLLAMA_NUM_GPU=0) — no VRAM constraint**
- 300W power cap via `/etc/systemd/system/nvidia-power-limit.service` — remove when 850W+ PSU installed

### Sequential GPU constraint
- Whisper runs on node04 (a2a-whisper, 172.16.201.4:8003) — no local VRAM contention
- ollama-embed is CPU-only and has NO VRAM constraint — can run at any time
- qwen2.5:32b inference takes 30s+ per turn; /metrics latency probe uses 6s timeout, returns "busy" if GPU saturated

### Container memory limits (32GB RAM host — AMD Ryzen 9 9900X)
| Container | Limit | Notes |
|-----------|-------|-------|
| sovereign-core | 2g | Python process + all in-process adapters |
| ollama | 22g | qwen2.5:32b ~20GB VRAM + CPU overhead |
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
| nginx | 128m | |
**Total ~38.5GB** — VRAM is GPU-side; host RAM usage ~16GB actual for CPU-side processes.

### 64GB RAM upgrade path
- Upgrade target: 2× 32GB DDR5 (matching existing sticks)
- Enables: expand working_memory qdrant limit from 4g → 8g+; increase sovereign-core to 4g; add periodic background flush to reduce crash-loss risk for working_memory entries
- working_memory collection's `on_disk=False` config persists — only the container memory limit changes

### CPU pinning (AMD Ryzen 9 9900X — 12 physical cores, 24 logical CPUs)
- Core-to-CPU mapping: Core N → CPU N, CPU N+12 (e.g. Core 0 → CPU 0,12; Core 11 → CPU 11,23)
- **sovereign-core, qdrant, qdrant-archive**: cpuset `0-7,12-19` — first 8 physical cores (both threads each); memory queries are on the critical path of the cognitive loop
- **adapter services** (broker, gateway, nanobot-01, sov-wallet, ollama-embed, nc-db, nc-redis, nextcloud, nginx): cpuset `8-11,20-23` — last 4 physical cores
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

- All short-circuit paths (ollama/memory/browser/scheduler) build `result_for_translator` and call `translator_pass()` before returning — see Security Boundaries above for the fabrication firewall isolation invariant
- Per-pass timeout: `PASS_TIMEOUT_SECONDS` (default 30s); total: `TOTAL_TIMEOUT_SECONDS` (default 120s)
- Async memory dispatch: `asyncio.create_task()` — never blocks return path
- All nanobot results stamped `_trust: "untrusted_external"`; scanner runs before PASS 3b
- InternalMessage envelope (`cognition/message.py`): Director input hashed at PASS 1, never stored raw; append-only history (output_hash only)

### PASS 2 External Routing — Provider Selection

Two separate routing layers. Do not conflate them.

**External LLM providers for PASS 2:** Grok, Gemini, Groq Inference, Ollama Cloud, and OpenRouter are all approved for PASS 2 routing (Director-approved 2026-05-21). The guard is the DCL gate (PRIVATE/SECRET → force local) plus `eligible_classifications` in `provider_registry`. Claude API is not wired for autonomous use and must not be wired. Provider selection is registry-driven via `_routing_decision()` — see `cognition/engine.py` for the full priority chain.

#### PASS 2/3a — LLM selection for planning (registry-aware routing)
`_routing_decision` in `cognition/engine.py` selects which provider writes the specialist's action plan:
1. **DCL gate** — PRIVATE/SECRET → force local (checked first, always); CONFIDENTIAL → force local unless `confidential_external_approved` session flag is set (treated as WORKSPACE_INTERNAL for eligibility checks only)
2. **Explicit override**: `use grok/gemini/groq/openrouter|ask grok/...` → that provider if eligible in registry
3. **task_type preference**: `web_aware_query`/`news_gather` → Grok first (real-time web access). Checked before alpha_vantage so news queries go to grok not the financial data API.
4. **task_type specialist**: financial task_types (securities_price/fundamentals/technicals/commodities/economic_indicators) → alpha_vantage tag (use_external=False; actual call via research harness)
5. **Complexity ≥ 0.50** → free-first order: groq_inference → gemini → openrouter → ollama_cloud → grok (paid last). Operational penalty subtracts 0.20.
6. **Rate-limit check** — provider marked rate-limited (429) within TTL 3600s → skipped; next eligible provider tried
7. **Default** → local Ollama
Provider eligibility: `enabled=True AND task_type in task_types AND DCL_tier in eligible_classifications` (governance.json `provider_registry`). Director-approved 2026-05-21.
Intent → task_type inference: `"research"/"web search"` → `web_aware_query`; `"news"` → `news_gather`; financial keywords → financial task_types.
Dev-Harness exemption: Dev-Harness security analysis (`cog.security_evaluate()`) always runs locally — never routes through `_routing_decision()`. Code review must not leave the local trust boundary.

#### `domain: ollama` execution — explicit-only external routing
When `_dispatch_inner` reaches `domain: ollama`, Grok is only called on **explicit** triggers. Auto-signals are intentionally excluded to avoid firing Grok alongside RSS/browser paths PASS 1 may have already chosen.

- **Grok**: `use grok` · `ask grok` · `via grok` · `trending` · `current events`
- Everything else → Ollama (local, fast)

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

Two distinct search contexts — do not conflate:

**Skill search** (`lifecycle.py` → `browser.fetch()`):
- **GitHub Search API**: primary — `browser.fetch("https://api.github.com/search/code?...")` with PAT headers from AUTH_PROFILES

**General web search** (sovereign-browser skill → `browser.py` → `POST /search` on a2a-browser):
- Backend priority order: SearXNG → DDG library → DDG Playwright → fail
- **SearXNG**: PRIMARY — aggregates google, bing, duckduckgo, startpage, wikipedia; per-engine timeout 10-15s (SearXNG settings.yml); a2a-browser `searxng.py` adapter timeout 20s
- **DDG library** (`duckduckgo_search`): fallback if SearXNG empty — `asyncio.wait_for` with `_DDG_TIMEOUT = 15.0` in deployed `ddg.py` (node04 code; stale local repo does NOT have this fix)
- **DDG Playwright**: fallback if library empty — `goto(timeout=15000)` in Playwright context
- **Brave**: dead — service discontinued early 2026 (returns 401/402); key in browser.env but ignored
- **Bing**: dead — Search API retired 2025-08-11; `BING_API_KEY` blank
- **Ollama enrichment**: runs after search returns results — mistral:7b on Quadro P4000 (node04), ~19-22 tok/s; `num_predict=1500`, `_TIMEOUT=90s`; no retry — invalid JSON returns partial result with `enrichment_status=invalid_json` immediately rather than burning another call
- `AUTH_PROFILES` in `execution/adapters/browser.py`: host-keyed header sets, auto-attached in `fetch()` — loaded from `secrets/browser.env` + `/home/sovereign/governance/browser-auth-profiles.yaml` at startup

**Pipeline budget (post-fix):** ~20s search + ~90s Ollama max = ~110s worst case; sovereign engine.py subprocess cap 220s — comfortable headroom. Worst-case degraded path: Ollama truncates JSON at ~50s → `enrichment_status=invalid_json` quality gate → partial result lands at sovereign in ~70s total. No hangs past 90s.

**Fixes applied 2026-05-14 (node04 — `app/enrichment/ollama.py`):**
- `num_predict` 3000 → 1500 (root cause: 3000 tokens at ~20 tok/s = ~150s, hit 180s ceiling mid-JSON)
- `_TIMEOUT` 180s → 90s (1500 tokens at ~20 tok/s = ~75s max, 90s gives buffer)
- Retry removed — invalid JSON now returns partial result immediately

**Fix applied 2026-05-14 (sovereign — `engine.py`):** browser search/fetch `"timeout"` param 60 → 220 (was hardcoded in engine.py, overriding SKILL.md default). Rebuild done.

**Note:** stale local `a2a-browser-deploy/app/enrichment/ollama.py` has no retry logic and `num_predict=3000` — deployed node04 version differs significantly. Do NOT overwrite deployed code from local copy.

### Nextcloud access
- LAN direct: `http://172.16.201.25` (port 80, no reverse proxy) — `nextcloud` service, `business_net`
- Tailscale: `https://sovereign.tail887d2b.ts.net` (via `nginx`, ports 80/8443 on 100.111.130.60)
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
- **Never seed conceptual relationships**: relationships between concepts (ETH ↔ staking_reward ↔ income) should emerge organically via the associative synthesis pass (`run_synthesis()` Passes 1–3, nightly 13:00 UTC = 01:00 NZST), not hand-crafted entries
- **Before adding a seed**: check semantic memory first — Rex may already have the knowledge from prior conversations; hand-stitching what he already knows is expensive and bypasses the learning process
- **Minimal footprint**: seed vocabulary and format recognition; leave reasoning and relationship-building to synthesis

### Memory task scheduler invariants
- `find_active_by_title()` uses ALL-word matching (≥5 chars) — not ANY — to avoid false dedup positives where unrelated tasks share a single common word (e.g. "nightly")
- If no words in the title meet the ≥5-char threshold, the match returns no results — do not fall back to ANY-word matching. The caller must handle an empty result as 'not found'.
- `seed_nightly_synthesis_task()` idempotency: checks PROCEDURAL for `intent=memory_synthesise` step; falls through to `store_task` if not found; `store_task` ALL-match dedup is the second gate
- Synthesis cron: `0 13 * * *` (13:00 UTC = 01:00 NZST) — runs Passes 1–3 of `run_synthesis()` (episodic scan → associative/relational) only; Pass 4 structural synthesis is a **separate continuous background task** (see below)
- Associative memory (`associative` collection) is populated ONLY by `run_synthesis()` Passes 1–3 — not by structural synthesis, not by curate, not by seeding
- **Structural synthesis background task** (`run_structural_loop()` in `memory/synthesis.py`): started as a named asyncio task (`structural_synthesis_loop`) in `main.py` lifespan, cancelled on shutdown. Processes 20 un-stamped semantic entries per chunk → saves cursor to `meta:memory-synthesis:structural-cursor` in META (RAID-durable, survives reboots) → sleeps 30s → repeats. Skips entries already stamped `_structural_synthesised_ts`. Idles at 3600s when all entries are stamped. Similarity threshold 0.65 (was 0.5), top-K neighbours 5 (was 8). Scoped synthesis (`synthesise_structural(key=<new_key>)`) still fires on every new semantic write via `asyncio.create_task()` and also stamps processed entries — the background loop only attacks un-stamped (unvisited) entries.

## Pending Director Decisions

The following items require Director input before CC can implement them. Do not resolve unilaterally.

1. **`confirmed=True` / PASS 2 bypass threat model** — The current text states PASS 2 is skipped when `confirmed=True` and that "double-confirmation IS the security gate for HIGH tier." This needs a one-paragraph explanation of the full confirmation lifecycle: when PASS 2 first fires, what the Director sees, and what `confirmed=True` represents as a re-entry. Without this, the bypass looks like a security hole. Director to draft the explanation; CC to insert it once approved.

2. **DCL "soul-protected config files" — undefined term** — The DCL hard-block rule references "soul-protected config files" without definition or cross-reference. Director to confirm the correct cross-reference (likely `sovereign-soul.md` and the GovernanceEngine SHA256 verification). CC to add the cross-reference once confirmed.

3. **SDO-07 split** — sov_id assignment and semantic entry creation are currently one SDO. These are independent concerns. Pending Director decision on whether to split into two separate SDOs.

---

## Sovereign Wallet (Built — First Boot 2026-03-31)

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
| MRFL-S1 | **COMPLETE** | Memory Relevance Feedback Loop Phase 1. PASS 0 scoring: `final_score = (norm_overlap×0.6) + (norm_slot×0.3) + (norm_weight×0.1)`. Weight range 1.0–5.0, +0.05 per successful EXEC. `pass0_hits` IDs stored in `InternalMessage.context.pass0_hits`. `_async_mrfl_increment()` writes to working_memory + archive_client directly (startup_load=True entries skip shutdown_promote). Episodic audit trail: `episodic:mrfl:weight-increment:{date}:{key}`, UUID5 dedup. Slot 8 in `startup_load()` pre-warms all `semantic:intent:*` entries (`_bootstrap_slot=5`). Phase 2 decay deferred. 2026-05-14. |
| Think-Tag-Strip | **COMPLETE** | Universal `<think>` tag stripping in `adapters/ollama.py`. `_strip_think()` strips tags, logs think content at DEBUG level (`llm_thinking:`). Applied to all `generate()` and `chat()` calls. `/no_think` directive sent to qwen3 for extraction-only prompts; never sent to Grok (Grok treats it as user content). 2026-05-14. |
| Email-List-Floor | **COMPLETE** | `nc_mail_list_default` (10) applied as minimum floor in email list dispatch: `count = max(count, _default_count)`. Prevents specialist LLM from returning fewer emails than the configured default. 2026-05-14. |
| News-Grok-Fix | **COMPLETE** | News synthesis: `_synthesise()` now uses clean `body` prompt for both local and Grok paths. `/no_think\n` prepended only for local Ollama path. Grok no longer receives the Ollama-specific directive. Removed redundant `_THINK_RE` from news_harness.py (adapter handles stripping). 2026-05-14. |
| Skill-Seed-v2 | **COMPLETE** | `semantic_seeds.py` enriched: `build_skill_seeds()` now parses SKILL.md frontmatter (description, operations with inputs/outputs, specialists, tier) into rich content. `seed_id` bumped to `v2_` with `_prev_seed_id` for v1 cleanup on next restart. `seed_intent_semantic_entries()` in qdrant.py handles `_prev_seed_id` delete-then-create upgrade. `lifecycle.load()` passes description + operations to `make_skill_semantic_seed()`. `build_tax_address_seeds()` writes `semantic:tax:taxable_wallets`, `semantic:tax:staking_contracts`, `semantic:tax:internal_addresses` placeholder entries. 2026-04-10. |
| Tax-Ingest-Harness | **COMPLETE** | Hourly NZ tax event ingestion: Nextcloud /Digiant/Tax/ CSV/PDF files + on-chain wallet events. Two tags: tax:crypto, tax:expense. CoinGecko NZD pricing. Scheduled cron `0 * * * *` — pending_approval until Director activates. |
| Tax-Report-Harness | **COMPLETE** | /do_tax [year] command. 3-turn human-in-the-loop: query semantic (date range) → Director provides expense CSVs → confirm → generates income{year}.csv + expenses{year}.csv in /Digiant/Tax/FY{year}/. Classifier labels crypto events. FIFO stub (Phase 3). |
| Learning-Harness | **COMPLETE** | `monitoring/learning_harness.py`. Autonomous document learning from /downloads/. Two triggers: (1) Telegram attachment upload → immediate background run; (2) hourly poll → synthesis window gate (UTC 15–17). Confidence loop: semantic→relational round-robin until plateau. Writes semantic + relational only (associative left to nightly synthesis). Sentinel: `episodic:learning:processed:{slug}`. Last-run summary injected into morning briefing news_brief step. Supported formats: text/md/csv/json/py/etc + `.pdf` (pypdf bespoke skill) + `.url` (browser fetch). No file-size gate. |
| pypdf-Skill | **COMPLETE** | Bespoke `pypdf` skill (nanobot-01). `extract_text` op: downloads PDF from Nextcloud via WebDAV, returns extracted text. Active for research_agent + memory_agent. Semantic entry: `semantic:intent:pypdf`. |
| learn_url-Intent | **COMPLETE** | `learn_url` intent (`domain: learning, operation: queue_url`). Director says "learn from https://..." → writes `.url` shortcut to `/downloads/{slug}.url` → fires `check_downloads(immediate=True)`. URL-based sentinel slug: `sha256(url)[:16]`. Failed fetches → `episodic:learning:failed:{slug}` (no retry). `source_url` in all new semantic entries from URL processing. |
| Smart-Bootstrap | **COMPLETE** | `startup_load()` in `qdrant.py` upgraded to two-phase boot. Phase 1: targeted slots — Slot 2 (all canonical MIP keys: semantic:wallet/network/networking/infrastructure/governance prefixes, two-pass payload scan + batch vector retrieve), Slot 3 (PROSPECTIVE due today, status=active, next_due<=today), Slot 6 (open SI proposals, pending_director_review), Slot 7 (active PROCEDURAL, human_confirmed=true, last_updated desc). Phase 2: vector similarity fill (remaining capacity). Dedup via `_loaded_ids` set. Returns stats dict. Telegram notification on completion. `bootstrap_working_memory()` removed. Step 2h in main.py removed. Typical result: ~15 targeted + ~295 similarity = 310-380 total entries ~1.1-1.3 MB. 2026-04-17. |
| Memory-Consultation-Pass | **COMPLETE** | PASS 0 in `execution/engine.py` `handle_chat()`. `_memory_consultation_pass()`: deterministic working_memory scroll (no LLM), keyword relevance scoring, top-15 ranked (slot priority + term overlap), formatted as COGNITIVE CONTEXT block injected into PASS 1 prompt via `prompts.classify(cognitive_context=...)`. Timing: 7-14ms typical (target <100ms, WARNING >200ms). Wired: `handle_chat` → `orchestrator_classify(cognitive_context=)` → `ceo_classify(cognitive_context=)` → `prompts.classify(cognitive_context=)`. 2026-04-17. |
| Director-Fact-Capture | **COMPLETE** | Auto-detection of Director-provided structured facts in `_quick_classify`. Five detection conditions: (1) 2+ airline codes, (2) 1 flight + hotel keyword, (3) hotel block >200 chars + multiline, (4) 1 flight + city-to-city route OR HH:MM time range + multiline, (5) Air NZ app share header "Here are my flight details" + multiline, (6) Booking.com share "I just booked...PIN code" pattern. Flight regex allows single-digit codes (`\d{1,4}` — covers NZ1, NZ6, QF1 etc). Topic extraction: `_booking_share` extracts hotel name + `bn=` ref from URL; else city extraction from curated list; fallback in `_dispatch_inner` derives topic from `Hotel:` line or city names in fact content. `_dispatch_inner` forces `human_confirmed=True` + `extra_metadata={"source": "director_provided"}`. Confirmation: "Stored your Doubletree By Hilton New York Times Square West." 2026-05-14. |
| Travel-Fact-Fixes | **COMPLETE** | (1) `governance/engine.py`: added `elif domain == 'memory_synthesise':` handler — "run nightly memory synthesis" was governance-blocked. (2) `monitoring/learning_harness.py`: `asyncio.TimeoutError` now caught explicitly with message "Ollama timed out on cycle N (pass_type) — GPU busy" instead of empty string. Notes quality gate: skip notes < 80 chars (writes `skipped_too_short` sentinel). (3) `main.py`: `/chat` endpoint wrapped in try/except catching `httpx.ReadTimeout`, `httpcore.ReadTimeout`, generic Exception — returns graceful JSON instead of HTTP 500. (4) `_quick_classify`: skill install false-positive fix — `"skill" in u` guard prevents "add a note to NextCloud" misrouting to skill install when `prior_domain=="skills"`. 2026-05-14. |
| Provider-Routing | **COMPLETE** | Full 8-task provider routing overhaul. 5 external LLM providers active (Groq Inference, Gemini, OpenRouter, Ollama Cloud, Grok — free-first, paid last). `_routing_decision()` rewritten with registry-aware queue, task_type preference (Grok for news_gather/web_aware_query), CONFIDENTIAL session-flag gate (`confidential_external_approved`), rate-limit TTL (3600s). `session_flag_set` intent (LOW tier, Director-activatable). All 3 harnesses (news, research, portfolio) converted from direct `ask_grok()` to routed dispatch. 8 MIP provider seeds added. CLAUDE.md routing invariants updated. `EXTERNAL_COGNITION.md` added to soul guardian RESTORABLE. 2026-05-22/23. |
| Memory-Synthesis-Redesign | **COMPLETE** | Extracted Pass 4 structural synthesis from the nightly `run_synthesis()` job into `run_structural_loop()` — a continuous asyncio background task started at boot, never scheduled. Processes 20 un-stamped SEMANTIC entries per chunk; saves cursor to `meta:memory-synthesis:structural-cursor` (META collection, RAID-durable) after each chunk so progress survives reboots. Stamps each processed entry `_structural_synthesised_ts` to avoid re-doing. Sleeps 30s between active chunks, 3600s when idle (all entries stamped). Similarity threshold raised 0.5→0.65, top-K neighbours reduced 8→5 (~74% fewer LLM pairs per entry). Scoped synthesis on every `qdrant.store()` also stamps entries, so new writes are never re-processed by the background loop. Nightly job now runs Passes 1–3 only. Eliminates the prior pattern where a full 7,000+ entry scan blocked the cognitive loop for 80+ hours per cycle. 2026-05-25. |

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

> **Crash note — working_memory is ephemeral (tmpfs).** A container crash or unclean shutdown clears all checkpoints. Harness implementations must not assume checkpoint survival across restarts. If a checkpoint is missing on a non-first step after a restart, the caller must restart the harness from step 1 — there is no recovery path.

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
| Learning-Harness | `_run_in_progress` (module bool, not WM) | poll → read → keywords → doc_array → chunk → confidence loop → sentinel | Formats: text/pdf/url. PDF via pypdf skill. URL via browser fetch + URL-based sentinel. No size gate. Writes semantic+relational; associative via synthesis cron; morning briefing injection |

### /command harness architecture

**Design principle**: What is deterministic should be deterministic; what needs reasoning is the LLM.
- Harness step sequencing → deterministic (checkpoint state drives next action)
- Candidate/option selection → LLM
- Security pattern scanning → deterministic (scanner.scan)
- Security verdict → LLM (interprets scanner + SKILL.md intent)
- Install/write/delete → deterministic (adapter call after confirmed)

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

