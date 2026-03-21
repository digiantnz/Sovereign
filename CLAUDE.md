# Sovereign AI — Claude Code Context

## Design Documents
- Full architecture: `/home/sovereign/sovereign/docs/Sovereign-v2.md`
- Phase 3 detail: `/home/sovereign/sovereign/docs/Sovereign-Phase_3.md`
- Cognitive loop rework: `/home/sovereign/sovereign/docs/Sovereign-CognitiveLoopRework.md`
- Always consult before making architectural decisions.

---

## Core Philosophy
- **NVMe (`/docker`)** — fast ephemeral AI runtime; nothing critical stored here long-term
- **RAID5 (`/home/sovereign`)** — durable truth; governance, memory, audit, backups
- **Broker** — sole holder of `docker.sock`; sovereign-core never has direct Docker access
- **Sovereign-core** — reasoning engine and orchestration brain; enforces governance before any action
- **Ollama** — local GPU-accelerated cognition only; never executes actions
- **Nextcloud** — business memory (WebDAV/CalDAV)

---

## Container Architecture

### Networks
- `ai_net`: ollama, sovereign-core, docker-broker, qdrant, gateway, nanobot-01
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
- Whisper medium uses ~769 MB — cannot run simultaneously with Ollama
- Whisper adapter evicts Ollama via `keep_alive=0` before transcription
- Never load models exceeding ~7.5 GB combined

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

Full phase history: `docs/CLAUDE-archive.md`

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
