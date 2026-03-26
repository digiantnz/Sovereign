# Sovereign AI — Claude Code Context

## Design Documents
- Full architecture: `/home/sovereign/sovereign/docs/Sovereign-v2.md`
- Phase 3 detail: `/home/sovereign/sovereign/docs/Sovereign-Phase_3.md`
- Cognitive loop rework: `/home/sovereign/sovereign/docs/Sovereign-CognitiveLoopRework.md`
- Always consult before making architectural decisions.

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
execution/adapters/           ← ollama, whisper, broker, webdav, caldav, imap, smtp, grok, nanobot
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

---

## Installed Skills

| Skill | Executor | Specialists | Operations |
|-------|----------|-------------|------------|
| `imap-smtp-email` | python3_exec → nanobot-01 | business_agent | |
| `openclaw-nextcloud` | python3_exec → nanobot-01 | business_agent | calendar_list/create/delete/update, tasks_list/create/complete/delete, files_list/search/read/write/delete/mkdir (14 ops) |
| `rss-digest` | python3_exec → nanobot-01 | research_agent, business_agent | |
| `deep-research` | browser+ollama | research_agent | |
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
| W1+W2 | **BUILT — pending first boot** | sov-wallet, BIP-39 keygen, HKDF+AES-256-GCM, EIP-712 SafeTx, get_btc_xpub |
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
| Notes-Index | **BUILT — PENDING UAT** | Session-scoped title→ID index on ExecutionEngine (5 min TTL). `_notes_get_or_build_index()` + `_notes_find_by_title()` (exact→substring→reverse-substring match). Index built on `list_notes`; auto-fetched cold on read/update/delete by title. Note-suffix classifier: "read/delete/update the [title] note" → deterministic regex extracts title → stored in `delegation["target"]`. `_resolve_note_id()` handles numeric vs title strings. Routing fixes: "list all the notes", "list the notes from nextcloud" → list_notes. `create/update/delete_note` added to `_DIAGNOSTIC_INTENTS` — IDs now shown in response. Delete fast-path now extracts numeric ID; `_original_request` regex fallback in delete dispatch. Matt live-tested 2026-03-24: list ✓ create ✓ update-by-ID ✓ delete-by-ID needs UAT. |
| NC-Index-Universal | **PROPOSAL** | Extend the title→ID index pattern to all Nextcloud item types (calendar events, tasks, mail). Each item type has its own scoped index (notes_index, calendar_index, mail_index) built on list operations. Disambiguation is NOT a problem — intent routing already separates "read the dentist note" (read_note) from "cancel the dentist appointment" (delete_event). Item type is determined by the verb+noun pattern before the title lookup. Calendar index is harder: events can have duplicate titles (multiple "dentist" entries on different dates) — index entry would need to include date + title. Mail already has sender/subject/date as natural keys; a title index is less useful. Priority: Notes-Index UAT first, then assess. |
| Skill-Harness | **COMPLETE** | Stateful multi-step skill lifecycle harness in engine.py + working_memory. search→list→review→install→clear. Pre-scan gate, WM checkpoint, HIGH-tier confirm gate. E2E tested 2026-03-23. |
| SI-Harness | **COMPLETE** | Self-improvement harness (monitoring/self_improvement.py). Daily observe loop, baseline+anomaly detection, proposal generation with Director approval gate. Primary autonomy boundary. 2026-03-23. |
| Dev-Harness | **COMPLETE** | 4-phase code quality harness (Analyse→Classify→Plan→Execute). pylint+semgrep+boundary_scanner+GitHub Actions. LLM advisory (Ollama+Claude escalation via DCL). CC runsheet HITL handoff. Memory integration: episodic/meta/semantic/prospective. Nightly cron 14:00 UTC. Self-scan: 0 findings. 2026-03-25. |
| Scheduler+SI-Fixes | **COMPLETE** | `_get_procedure`/`_find_point_id` limit=200 ceiling replaced with filtered Qdrant queries. `seed_nightly_dev_task` idempotency via PROCEDURAL step check (title-based unreliable — `qdrant.store()` overwrites title). SI proposal dedup gate (`_existing_pending_proposal`). Task data cleanup (5 cancelled). 2026-03-26. |
| Translator-Bleed-Fix | **COMPLETE** | Deterministic `_translator_sanitise()` strips meta-commentary sentences/bullets before Telegram delivery. Retry-once on >30% strip with EPISODIC violation log. Health brief re-routed through `translator_pass()`. B5 boundary rule detects leak phrases baked into string literals in cognition/gateway path. Validator Queue monitor updated: Entry+Exit extraction, 7-day alert threshold. 2026-03-26. |
| PM-Harness | **PROPOSAL — PENDING DIRECTOR APPROVAL** | Full SDLC project management harness against sovereign semantic memory. Proposal in prospective memory. Requires Dev-Harness first. |
| Retry-Logic | **PROPOSAL — PENDING DIRECTOR APPROVAL** | Exponential backoff (max 3) for all harnesses, surface failure to Director. Proposal in prospective memory. |

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
  "_harness_checkpoint": true
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

The session payload flag (`_harness_checkpoint`, `_si_session`, etc.) distinguishes the record type in working_memory scrolls. All session keys are ephemeral — they live in working_memory (NVMe in-process RAM) and are re-established on demand.

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

`{flag_key}` is the unique bool field used to identify this checkpoint type when scrolling working_memory (e.g. `_harness_checkpoint`, `_si_session`).

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
| Skill-Harness | `_harness_checkpoint` | search → list → review → install → clear | HIGH confirm on install |
| SI-Harness | `_si_session` | observe (daily auto) → propose (auto-triggered) | Director approves proposals; never self-modifies |

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
