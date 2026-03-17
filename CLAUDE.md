# Sovereign AI — Claude Code Context

## Design Document
Full architecture reference: `/docker/sovereign/docs/Sovereign-v2.md`
Phase 3 detail: `/docker/sovereign/docs/Sovereign-Phase_3.md`
Always consult these before making architectural decisions.

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
- `ai_net`: ollama, whisper, sovereign-core, docker-broker, qdrant, a2a-browser, (gateway — Phase 3)
- `business_net`: nextcloud, nc-redis, nc-db, nextcloud-rp, sovereign-core (dual-homed)
- `browser_net`: a2a-browser only (internet egress; compose-managed; no route to ai_net or business_net at network level)
- sovereign-core is dual-homed (ai_net + business_net); a2a-browser is dual-homed (ai_net + browser_net)

### Security Boundaries (hard rules — do not violate)
- `docker.sock` → broker container only
- `sovereign-core` → no privileged mounts, no docker.sock
- Ollama API → ai_net only, no host exposure
- Sovereign API → `127.0.0.1:8000` loopback only
- Nextcloud → business_net only

### GPU (RTX 3060 Ti, 8GB VRAM)
- Ollama uses ~4.4 GB (llama3.1:8b-instruct-q4_K_M) — also has mistral:7b-instruct-q4_K_M installed
- Whisper medium uses ~769 MB
- Both cannot run simultaneously — whisper adapter evicts Ollama via `keep_alive=0` before transcription
- Never load models that would exceed ~7.5 GB combined

---

## Storage Layout

### NVMe — `/docker/sovereign/`
```
compose.yml
CLAUDE.md
core/          ← sovereign-core FastAPI app (runtime code)
broker/        ← docker workflow scripts
gateway/       ← Telegram gateway (Phase 3)
a2a-browser/   ← AI-native web search service (MVP)
nginx/         ← nextcloud reverse proxy config
secrets/       ← .env files (never commit)
docs/          ← design documents
runtime/       ← ephemeral AI workspaces (session scratch)
tmp/
```

### RAID — `/home/sovereign/`
```
governance/
  governance.json   ← tier policy (LOW/MID/HIGH) — mounted :ro into container
memory/
  MEMORY.md         ← durable AI memory — mounted rw into container
audit/              ← action audit logs
backups/            ← container inspect snapshots pre-operation
skills/             ← Sovereign skill definitions — mounted :ro into container
  <skill-name>/
    SKILL.md        ← YAML frontmatter (sovereign: block) + skill body
security/
  skill-checksums.json  ← whole-file SHA256 reference hashes (written by SkillLoader on first boot)
personas/           ← Sovereign-soul.md + orchestrator + specialist persona files
  sovereign-soul.md   ← Cognitive constitution (sole identity document, checksummed, auto-restore)
  orchestrator.md     ← Orchestrator persona (classification / evaluation / memory-decision passes)
  translator.md       ← Translator persona (Director-facing translation pass only)
  devops_agent.md     ← Infrastructure specialist (replaces DOCKER_AGENT.md)
  research_agent.md   ← Web + intelligence specialist
  business_agent.md   ← Nextcloud/calendar/mail specialist
  security_agent.md   ← Risk evaluation specialist
  memory_agent.md     ← Cognitive store curation specialist
```

---

## Governance Tiers

| Tier | Confirmation | Examples |
|------|-------------|---------|
| LOW  | None | docker ps/logs/stats, file read, mail read, WebDAV read, Ollama query |
| MID  | `requires_confirmation: true` | docker restart/update, file write, calendar write, mail send |
| HIGH | `requires_double_confirmation: true` | docker rebuild/prune, file delete |

Policy file: `/home/sovereign/governance/governance.json` (RAID, mounted read-only)
- **Never bake governance.json into the image**
- **Never add LLM calls inside GovernanceEngine** — it must remain deterministic

---

## Application Structure (`core/app/`)

```
main.py                 ← FastAPI app, lifespan context manager
config.py
api/routes.py
governance/
  engine.py             ← deterministic tier/action validation
  schema.py
skills/
  loader.py             ← SkillLoader: discovers, validates, injects SKILL.md into specialist prompts
  lifecycle.py          ← SkillLifecycleManager: SEARCH/REVIEW/LOAD/AUDIT over external skills
config_policy/
  notifier.py           ← ConfigChangeNotifier: Telegram + as-built.md on any in-scope config write
cognition/
  engine.py             ← calls OllamaAdapter, never executes actions; parse_task_intent() for scheduler
  prompts.py
scheduling/
  task_scheduler.py     ← TaskScheduler: NL intent parser, capability check, Qdrant store, background executor
execution/
  engine.py             ← routes to adapters after governance check
  adapters/
    ollama.py           ← httpx async, stream:False, keep_alive control
    whisper.py          ← evicts Ollama VRAM before transcription
    broker.py           ← calls docker-broker HTTP API only
    webdav.py           ← Nextcloud file operations
    caldav.py           ← Nextcloud calendar operations
    imap.py             ← mail read (imaplib)
    smtp.py             ← mail send (smtplib)
    grok.py             ← external LLM (sanitize IPs/hostnames/secrets first)
memory/
  session.py
```

### Key Implementation Rules
- All adapters use `httpx.AsyncClient` (never blocking `requests` in async methods)
- Ollama API: always set `"stream": False` (default streams NDJSON, breaks `r.json()`)
- Governance validates → raises `ValueError` on failure, returns rules dict on success
- Execution engine wraps `gov.validate()` in `try/except ValueError`
- `requires_confirmation` / `requires_double_confirmation` read from returned rules dict
- FastAPI uses lifespan context manager (not deprecated `@app.on_event`)
- **CalDAV raw-response invariant**: `_discover_calendar()` always returns a dict `{url, propfind_http_status, propfind_response_body, calendars_found}` — never `None`. `create_event` / `delete_event` / `create_task` / `delete_task` always include `http_calls_made`, `http_status`, `response_body`, and `propfind_http_status` in their return dict. If a call was not made, the dict says so explicitly (`"PUT not attempted"`). No synthesised error strings — only raw status codes and bodies.
- **CalDAV PUT path**: All write operations PROPFIND to `/remote.php/dav/calendars/digiant/` (Depth:1) first to discover real slugs, then PUT/DELETE to `{discovered_url}/{uid}.ics`. Never assume the LLM label is a valid Nextcloud slug.
- **CalDAV VTODO support**: `create_task(calendar, uid, summary, due, start, description, status)` generates a valid VTODO ICS component and PUTs it via the same PROPFIND discovery flow. `delete_task` delegates to `delete_event`. Tasks calendar slug discovery uses the same partial-match logic (`_discover_calendar` with `"tasks"` as default calendar name).
- **HTTP status invariant**: All CalDAV methods check `r.status_code` directly — no `raise_for_status()`. Return `{"status": "error", "error": ..., "http_status": ...}` for non-2xx. `_safe_translate` never passes error results to `ceo_translate`; error path is deterministic.
- **Prospective memory confirmation gate** (engine.py PASS 5): for mutating intents (`create_event`, `create_task`, `write_file`, `send_email`, `delete_file`, `delete_email`, `delete_task`, `restart_container`, `create_folder`), any prospective memory entry receives `execution_confirmed: bool` stamped deterministically from `execution_result.http_status`. If not 2xx → `execution_confirmed: False, outcome: "unconfirmed"` regardless of LLM memory decision. Never allow the LLM to assert an action completed without a real HTTP 2xx.
- **IMAP archive discovery**: `_find_archive` candidates are `["archive", "archives", "inbox.archive", "saved messages"]` — no Gmail-specific entries. Accounts are on `digiant.co.nz`, `digiant.nz`, or `e.email`.
- **IMAP UID invariant**: all operations use `mail.uid()` throughout — sequence numbers are unstable. `list_inbox()` fetches all messages with real UIDs via `SEARCH + FETCH RFC822.HEADER`. `_move_sync()`, `_delete_sync()`, and `_mark_flag_sync()` all have UID guards: if uid is None/empty/whitespace → return `{status: error, step: uid_guard}` immediately. `import email.message` must be explicit (bare `import email` does not expose submodule).
- **IMAP archive COPY quoting**: `_move_sync()` checks for spaces in `archive_folder` and wraps in double-quotes before the IMAP UID COPY command (`imap_folder = f'"{archive_folder}"' if " " in archive_folder else archive_folder`). imaplib does NOT auto-quote mailbox names. Pre-COPY guard checks both `uid` and `archive_folder` are non-empty; error dict includes `imap_folder_arg` field showing the exact string sent.
- **CalDAV intent routing**: `_quick_classify` has a calendar fast-path block (before `_sched_early` and file-write checks) for `create_event`, `delete_event`, `update_event` — these always route to `business_agent` and never go through CEO LLM. `delete_event` and `update_event` are in `INTENT_ACTION_MAP` (domain=caldav) and `INTENT_TIER_MAP` (MID). `_dispatch_inner` has a `calendar_update` handler routing to `caldav.update_event()`.
- **CalDAV datetime normalisation**: `_normalise_dt(value)` in engine.py normalises freeform datetime strings to ISO 8601 `YYYY-MM-DDTHH:MM:SS`. Strips NZDT/NZST/NZT/UTC/GMT suffixes, ordinal suffixes (st/nd/rd/th), "at" separators, then tries fromisoformat → multiple strptime formats. Default year 2026 if year absent. `calendar_create` in `_dispatch_inner` tries 12+ field names for start/end (start, start_time, datetime, when, date_time, event_start, scheduled_at, begin; also combines date+date_part+time fields; scans content/draft_content/target as last resort).
- **Specialist schema hints**: `prompts.specialist()` injects intent-specific required-field reminders for `create_event`, `create_task`, `delete_event`, `update_event` — anchors the small LLM (llama3.1:8b) to output correct schema rather than defaulting to mail/delegation format. Includes today's date for relative date resolution.
- **WebDAV path extraction invariant** (engine.py): `path = action.get("path", "/")` always returns `"/"` because `INTENT_ACTION_MAP` static entries carry no runtime `path`. Runtime file path is always in the `specialist` output. Extraction block checks `specialist.get("path")` first, then falls back to `specialist.get("target")` as `source = sp_path or sp_target`. `target` is for container names only and is last resort for webdav.
- **Community skill routing invariant**: Mail domain in `execution/engine.py` calls `self.nanobot.run("imap-smtp-email", action, params)` — NOT IMAPAdapter/SMTPAdapter. CalDAV/WebDAV still use Python adapters. Account suffix `_suf = "" if account == "business" else "_personal"` selects personal vs business broker command. nanobot.run() DSL intercept routes broker_exec ops to `BrokerAdapter.exec_command()`. Move/delete ops return `{status: error}` — no broker command exists.
- **nanobot-01 = PRIMARY skill execution environment** (OC-S5 COMPLETE): nanobot-01 handles ALL OpenClaw/ClawhHub application-level skill execution. **Broker boundary (hard rule)**: broker handles ONLY system calls in SYSTEM_COMMANDS whitelist: docker_ps/logs/restart/stats/inspect/exec, uname/df/free/ps/nvidia_smi/systemctl_status/journalctl. All other broker_exec commands → nanobot-01 (+ deprecation warning). IMAP/SMTP/feeds/WebDAV/CalDAV are application logic; do NOT add new ones to broker.
- **nanobot.run() dispatch model** (post-OC-S6): `_NATIVE_TOOLS={"browser"}` stays in sovereign-core; `_REMOTE_TOOLS={"filesystem","exec","python3_exec","imap","smtp","webdav","caldav"}` forwarded to nanobot-01; broker_exec checked against SYSTEM_COMMANDS first. **Credential delegation**: `op_spec.credential_services` → `CredentialProxy.issue()` → UUID token → forwarded in context → nanobot-01 redeems via POST sovereign-core:8000/credential_proxy → injected as subprocess env vars → immediately invalidated (single-use, 60s TTL). **python3_exec result normalization**: `_forward()` in nanobot.py normalises result — if `body.get("result")` is None (DSL flat response), builds body_result from all non-wrapper body fields (wrapper = {run_id, skill, action, path, elapsed_s}). This ensures `nb.get("result", nb)` in engine.py always returns actual data. **Scripts deployed**: imap_check.py, smtp_send.py in imap-smtp-email/scripts/; nextcloud.py in openclaw-nextcloud/scripts/ — pre-deployed in nanobot-01/workspace/. Future: lifecycle.load() auto-deploys scripts/ at skill install time.
- **python3_exec tool type** (nanobot-01/server.py): `_dispatch_python3_exec(skill, op_spec, params, run_id, context)` — builds path `workspace/skills/<name>/scripts/<script_rel>`, path-traversal guard, redeems credential token, calls `_dispatch_exec(cmd, run_id, extra_env=credentials)`. `_dispatch_exec` merges extra_env into `os.environ.copy()` before `subprocess.run()`. Script must be pre-deployed; error returned if not found.
- **route_cognition / specialist_reason external routing invariants** (cognition/engine.py): `_routing_decision(prompt, user_input)` centralises routing logic. PASS 2 (specialist_reason) is the **only** externally-routable pass — PASS 1/3/4 always local (governance/classify must be deterministic). Complexity is scored on `user_input` (Director's message), NOT the full specialist prompt (persona length would inflate every score). DCL hard-block: tier in `{"PRIVATE","SECRET"}` → `force_local=True` regardless of explicit override. Provider selection: `_CLAUDE_SIGNAL_RE` (architectural/plan/review/design/strategy) → claude; `_GROK_SIGNAL_RE` (current/latest/news/today/recent/market) → grok; default → grok. Operational penalty: score≥0.50 AND `_OPERATIONAL_RE` (restart/container/service/deploy/port/compose/nginx) → -0.20. `specialist_plan` always includes `_routing_reason`, `_complexity_score`, `_intended_provider` for diagnostics, even when falling back to local. Claude/Grok API unavailable → graceful fallback to Ollama; no error raised.
- **Confirmed-continuation bypass** (engine.py handle_chat): when `confirmed=True` and `pending_delegation._pending_load is not None`, PASS 2 (specialist) and PASS 3 (CEO evaluation) are skipped. The reasoning already happened before the confirmation prompt was issued; re-running Ollama is pure overhead (~80s). Any new confirmed-continuation op should stash its carry-forward state in `pending_delegation._pending_load` to get this bypass. Remaining work is PASS 4 dispatch + translate only (~45s total).
- **skill_install flow invariants**: `_system_signals` must include `"skill", "clawhub", "openclaw"` or conversational guard fires first. `intent_tiers.skill_install` must be `LOW` in governance.json (composite manages its own confirmation). Governance engine allows `install` at LOW when `skill_read: true`. `handle_chat` promotes `requires_confirmation`, `pending_delegation`, `summary`, `escalation_notice` to top-level result_dict (gateway reads top-level). `pending_delegation` from skill_install response contains `{delegate_to, intent, _pending_load}` — engine short-circuits on `confirmed=True and _pending_load`. Translator receives simplified `_confirm_ctx` for confirmation responses (not raw review JSON). `lifecycle.search()` detects URLs in query and fetches directly (no SearXNG). `_quick_classify` URL check: if "install/load/add" + "skill" in message → route to `skill_install` not `fetch_url`. **URL preservation**: `_quick_classify` sets `target: user_input` (full text including URL); `op == "install"` in engine.py extracts URL from `delegation.get("target")` before falling back to `sp.get("search_query")` — specialist strips the URL; without this extraction the URL is lost and SearXNG returns the wrong skill. `_fetch_raw_url` in lifecycle.py tries direct httpx first, falls back to `self.browser.fetch()` (a2a-browser has browser_net internet egress; sovereign-core does not).
- **Broker CLI exec invariants** (`broker/index.js`, `broker/commands-policy.yaml`): `POST /exec/:commandName` route registered BEFORE the docker-policy catch-all — it bypasses docker-policy trust check and uses commands-policy tier instead. `SHELL_META` guard applied to all string params before any processing. `allowlist: []` (empty array) = deny all — check is `allowlist !== undefined && !includes(val)` not `length > 0`. `__container_exec__` → `execInContainer(cmd.container, cmd.fixed_args)`; `__script__` → path traversal check + existence check + `spawnRun`. All execution via `spawn(shell:false)`. `broker.py exec_command()` never raises — all errors returned as structured dicts. `broker_exec` DSL tool in nanobot.py resolves command name via `op_spec.get("action", action)` — op_spec's `action` field names the broker command; outer DSL op key is fallback. To enable systemctl/journalctl: (1) add `pid: host` to broker in compose.yml, (2) add `nsenter` to broker Dockerfile (`apk add util-linux`), (3) set `enabled: true` in commands-policy.yaml.
- **Task scheduler invariants**: `scheduling/task_scheduler.py` — TaskScheduler is data-driven (no task-specific code). Tasks stored as: PROSPECTIVE (when/status/next_due), PROCEDURAL (steps, human_confirmed=True), EPISODIC (run history). All three share `task_id`. Scheduler loop runs every 60s; uses `qdrant.client.set_payload()` to update next_due/status without re-embedding. `compute_next_due()` handles cron/interval/one_time. Scheduler keywords must be checked in `_quick_classify` BEFORE the conversational guard and BEFORE email keywords to avoid misrouting scheduling requests. `confirmed=True` must be passed via `payload={"confirmed": confirmed}` in the short-circuit `_dispatch` call so PROCEDURAL writes get `human_confirmed=True`.

---

## Phase Status

| Phase | Status | Description |
|-------|--------|-------------|
| 0 | **COMPLETE** | Read-only observer — docker/file/WebDAV reads, Ollama cognition, governance active |
| 1 | **COMPLETE** | Broker + MID tier docker workflows — docker_ps/logs/stats/restart live |
| 2 | **COMPLETE** | WebDAV r/w, CalDAV, IMAP (personal+business), SMTP, HIGH tier |
| Security | **COMPLETE** | Scanner, guardrail, soul guardian, audit ledger, GitHub adapter, Sovereign-soul.md protection |
| 3 | **COMPLETE** | Telegram gateway, multi-pass CEO cognitive loop, persona switching, SearXNG, agent layer |
| 4 | **COMPLETE** | Cognitive memory: weighted retrieval, query type classification, prospective briefing, gap auto-create |
| 4.5 | **COMPLETE** | Observability: /metrics endpoint, scheduled self-check, morning health brief, self-diagnostic routing, DCL, persona renames |
| 5 | **COMPLETE** | Sovereign Secure Signing: Ed25519 keypair, SigningAdapter, signed audit ledger (rex_sig), governance snapshot, browser ACK |
| 6 | **COMPLETE** | Sovereign Skill System: SkillLoader, SKILL.md format, dual-layer integrity (body checksum + reference file), specialist injection, 4 seed skills |
| 6.5 | **COMPLETE** | Skill Lifecycle Manager: SEARCH/REVIEW/LOAD/AUDIT, security review pipeline, soul-guardian registration, config change notification policy |
| 6.6 | **COMPLETE** | Skill system gap fixes: skills domain in governance.json (skill_read/skill_load flags, intent_tiers section), GovernanceEngine.get_intent_tier(), skills domain validator, SearXNG-only skill discovery (no direct ClawhHub HTTP), skill_install composite intent (search→review→load), procedural memory seed for 3-step sequence |
| W1 | **BUILT — pending first boot** | Sovereign Wallet Phase 1: sov-wallet container, BIP-39 keygen, HKDF+AES-256-GCM seed encryption, GPG Director backup, signed Telegram notification, /verify anti-spoofing |
| W2 | **BUILT** | Sovereign Wallet Phase 2: WalletControlAdapter (eth_account direct signing, no MetaMask/Playwright), sign_message (MID), propose_safe_transaction (HIGH, EIP-712 SafeTx), get_pending_proposals, get_btc_xpub (LOW), Safe API proxy via sov-wallet |
| 7 | **COMPLETE** | Generalised task scheduler: NL intent parser, Qdrant-backed task storage (PROSPECTIVE+PROCEDURAL+EPISODIC), 60s background executor, cron/interval/one_time schedules, capability checking, conditional notification, schedule_task/list_tasks/pause_task/cancel_task intents |
| OC-S1 | **COMPLETE** | OpenClaw March Stage 1: WebDAV/CalDAV/IMAP/SMTP adapters rewritten using community skills as reference; four Path 1 SKILL.md prompt wrappers deployed to RAID; new intents wired (search_files, list_events, complete_task, fetch_message, mark_read, mark_unread, list_folders) |
| OC-S2 | **COMPLETE** | OpenClaw March Stage 2: nanobot-01 sidecar live on ai_net (port 8080, HKUDS/nanobot v0.1.4); FastAPI bridge + NanobotAdapter (MID minimum, credential stripping, audit ledger); soul section 12 "Division of Sovereignty" recorded; governance.json v1.13 nanobots block |
| OC-S3 | **COMPLETE** | OpenClaw March Stage 3: Model B DSL — typed `operations:` frontmatter in SKILL.md, adapter/method/params schema; NanobotAdapter DSL intercept (imap/webdav/caldav native, filesystem/exec forwarded to nanobot-01); _load_skill_dsl() mtime cache; _validate_dsl_params() type coercion; 4 skills updated (25 total ops); `path: dsl_native|dsl_remote|llm` in all responses; zero Ollama calls on DSL path confirmed |
| OC-S3.1 | **COMPLETE** | Broker CLI exec: `commands-policy.yaml` typed allowlist (uname/df/free/ps/nvidia_smi enabled; systemctl_status/journalctl disabled—need pid:host+nsenter; script with empty allowlist); `POST /exec/:commandName` in broker/index.js — SHELL_META guard, typed param validation, `spawn(shell:false)`, `__container_exec__` (nvidia_smi via ollama), `__script__` path; `broker.py` exec_command(); `broker_exec` DSL tool type in nanobot.py; validated DSL path end-to-end |
| BugFix-2026-03-13 | **COMPLETE** | IMAP: archive_folder space quoting in UID COPY; uid guards added to _delete_sync + _mark_flag_sync. CalDAV: calendar fast-path in _quick_classify (create/delete/update_event); delete_event+update_event added to INTENT_ACTION_MAP; _normalise_dt() multi-format datetime parser; multi-field start/end extraction in calendar_create; specialist schema hints in prompts.py. list_events regex namespace fix (uppercase C: → any prefix match); time-range floating datetime (no Z suffix). Validated: HTTP 201 "Shave the cat" 2026-03-16T10:00:00 + list_events 10 events + date-filtered 1 event |
| OC-S4 | **COMPLETE** | Community skills installed: `imap-smtp-email` + `openclaw-nextcloud` (broker_exec DSL ops). Old Python adapter skills removed (sovereign-imap/smtp/caldav/webdav). Mail domain in execution engine rewired to `nanobot.run("imap-smtp-email", ...)` → DSL → broker_exec. Validated: fetch_email → imap_business_check → 10 real emails, path: dsl_native. NanobotAdapter ledger.log→append fix. skill_install flow: 7 bugs fixed (system_signals, regex, name extraction, governance pre-check, requires_confirmation promotion, pending_delegation stash, translator). Direct URL install path in lifecycle.search(). governance.json intent_tiers skill_install=LOW. |
| OC-S5 | **COMPLETE** | nanobot-01 as primary skill executor. Hard boundary: docker-broker=system calls only (SYSTEM_COMMANDS whitelist); nanobot-01=all application skills. Phase 1: python3 runtime packages (requirements.txt), static env mounts (imap/nextcloud creds). Phase 2: CredentialProxy single-use token delegation — issue() → UUID token → forwarded to nanobot-01 → redeem() via POST /credential_proxy → inject as subprocess env vars → immediately invalidated. _dispatch_python3_exec() added; _dispatch_exec(extra_env=) wired. Confirmed-continuation bypass: skip PASS 2+3 when confirmed=True + _pending_load present (~80s saved on skill confirmations). |
| OC-S6 | **COMPLETE** | broker_exec → python3_exec cutover. Wrote imap_check.py, smtp_send.py, nextcloud.py (CalDAV+WebDAV) as stdlib/requests Python scripts deployed to nanobot-01/workspace/skills/. Updated imap-smtp-email + openclaw-nextcloud SKILL.md: all ops → python3_exec, adapter_deps → [nanobot]. Fixed nanobot.py _forward: python3_exec responses are flat (no nested "result") — normalize by building body_result from non-wrapper fields. Added mail block fallthrough guard + execution_result None-safety guard. Added "nanobot" to SkillLoader _ALWAYS_AVAILABLE. Validated: /chat mail+nextcloud full cognitive loop, path: dsl_python3, director_message translated correctly. rss-digest skill: feeds.py + _translate_rss_feeds() + SKILL.md on RAID + skill-checksums.json. route_cognition wired into PASS 2 only: _routing_decision() helper, complexity scored on user_input, DCL hard-block for PRIVATE/SECRET, _routing_reason+_intended_provider in specialist_plan always set. |

### Phase 0 Validated Capabilities
- `docker ps`, `docker logs`, `docker stats` → observer status (read-only)
- WebDAV read → observer status
- LOW tier: no confirmation required ✓
- MID tier: `requires_confirmation: true` returned ✓
- HIGH tier: `requires_double_confirmation: true` returned ✓
- Illegal action at LOW tier (e.g. file delete) → rejected ✓
- Ollama inference via `/query` route → live GPU inference working ✓
- Whisper medium model → cached in `whisper_models` volume, GPU transcription working ✓

---

## Secrets Files (`secrets/`)
| File | Purpose |
|------|---------|
| `ollama.env` | Ollama runtime config (model, VRAM limits) |
| `whisper.env` | Whisper URL and model name |
| `redis.env` | Redis password |
| `nextcloud.env` | MariaDB creds, Redis password, Nextcloud admin |
| `grok.env` | Grok API key |
| `telegram.env` | Telegram bot token + authorized user ID (Phase 3) |
| `openclaw.env` | Legacy — review before Phase 2 |
| `imap-personal.env` | Personal IMAP/SMTP credentials |
| `imap-business.env` | Business IMAP/SMTP credentials |
| `browser.env` | a2a-browser shared secret + optional Brave/Bing API keys |

---

## Phase 3 Preview (Telegram + Multi-Pass Cognitive Loop)
- Separate `gateway/` container — **do not embed Telegram in sovereign-core**
- Gateway responsibilities: auth, session state, confirmation prompts, forward JSON to core
- Cognitive loop: Orchestrator classification → Specialist reasoning → Orchestrator evaluation → Execution → Memory decision → Translator translation
- Personas stored on RAID: `/home/sovereign/personas/` — orchestrator.md (classify/evaluate/memory-decision), translator.md (Director translation only), plus 5 specialist files
- Structured JSON enforcement: Ollama called with `"format": "json"`, reject non-JSON
- Safety rules: specialists cannot override tier, write memory, or escalate without Sovereign Core approval
- **ALL Director messages pass through translator pass** (`cog.ceo_translate()` → `director_message` field)
- orchestrator.md = classify/evaluate/memory-decision persona; translator.md = Director-facing translation only (distinct roles)
- **sovereign-soul.md = sole identity document** — orchestrator.md and translator.md are functional personas only
- Qdrant vector DB at `/home/sovereign/vector` — **COMPLETE** (Phase 3.5)
- Phase 4 cognitive memory: `search_all_weighted()`, `classify_query_type()`, `ensure_gap_entry()`, `get_due_prospective()`, session-start morning briefing, richer memory_decision schema — **COMPLETE**

## Agent Layer Architecture (implemented 2026-03-04)
- **Sovereign Core** = reasoning engine, orchestration, governance enforcement
- **Specialists** report to Sovereign Core only (devops_agent, research_agent, business_agent, security_agent, memory_agent)
- **Translator** (translator.md) = Director interface only — translates results to plain English, no other role
- **No specialist may communicate directly with Director** — enforced in persona definitions + code
- `cog.ceo_translate()` called at end of both handle_chat return paths; populates `director_message` field
- Gateway checks `director_message` first; falls back to `_format_result()` if translation returns empty

## Search Backend Status
- **SearXNG**: LIVE — self-hosted on browser_net, aggregates Google/Bing/DDG/Startpage; always-primary
- **DDG** (ddgs library + Playwright fallback): LIVE — always-on ordered fallback
- **Brave**: dead letter — free tier discontinued early 2026; key returns 401/402
- **Bing**: dead letter — Search API retired 2025-08-11; key blank
- Priority order: SearXNG → DDG → Brave (fails) → Bing (fails)
- SearXNG config: `searxng/settings.yml` (mounted :ro); secret in `secrets/searxng.env`
- `searxng.env` SEARXNG_SECRET must be kept in sync with settings.yml if settings are regenerated
- **a2a-browser**: runs on node04 (172.16.201.4:8001); enrichment model phi3:mini; timeouts: 180s enrichment / 200s sovereign-core adapter
- node04 GPU: 4GB (HP z420/z440 upgrade pending — 8GB card); BIND_ADDRESS must be 0.0.0.0 (Docker proxy fails on specific VLAN IPs)

---

## Sovereign Skill System (Phase 6)

### SKILL.md Format
Each skill lives at `/home/sovereign/skills/<skill-name>/SKILL.md`:
```yaml
---
name: <skill-name>
version: "1.0"
description: "<short description>"
sovereign:
  specialists:          # list of agent names that load this skill
    - research_agent
  tier_required: LOW    # LOW | MID | HIGH — minimum tier for skill actions
  adapter_deps:         # adapters that must be available; skill skipped if any are absent
    - browser
    - ollama
  checksum: <sha256>    # SHA256 of body content (everything after closing ---)
---
# Skill body content here
```

### Integrity Model
- `sovereign.checksum` = SHA256 of the body (text after frontmatter's closing `---`)
- `/home/sovereign/security/skill-checksums.json` = whole-file SHA256 reference (rw-mounted)
- SkillLoader validates both on every load; either mismatch → refuse + audit log
- First boot (no reference file) = bootstrap mode: reference is created from current files
- Drift in either hash triggers a `skill_drift` or `skill_checksum_mismatch` audit event

### Integration
- `CognitionEngine.specialist_reason()` creates `SkillLoader(agent_name)` per call
- `loader.inject_into_persona(persona)` appends `## ACTIVE SKILLS` section to specialist prompt
- `scan_all_skills()` runs at lifespan startup → logs summary, stored in `app.state.skill_summary`
- Skills whose `adapter_deps` are unavailable are silently skipped (logged, not errors)
- All load events logged to AuditLedger

### Seed Skills
| Skill | Specialists | Tier | Adapter deps |
|---|---|---|---|
| `deep-research` | research_agent | LOW | browser, ollama |
| `security-audit` | security_agent | LOW | ollama |
| `session-wrap-up` | all 5 specialists | MID | ollama |
| `memory-curate` | memory_agent | LOW | ollama, qdrant |

### Skill Lifecycle Manager
- `SkillLifecycleManager(scanner, cog, browser, ledger, guardian)` in `skills/lifecycle.py`
- **SEARCH**: SearXNG via a2a-browser (primary) — queries `"sovereign skill <query> SKILL.md site:github.com"`, fetches raw SKILL.md from `raw.githubusercontent.com`. No direct calls to `topclawhubskills.com`. Fallback: general web search. `_github_url_to_raw()` converts blob/tree GitHub URLs to raw. `_fetch_raw_url()` fetches content with httpx.
- **REVIEW**: escalation keyword scan → SecurityScanner → `cog.security_evaluate()` → structured verdict
  - Non-certified → always "review" decision regardless of scan; escalate_to_director=True
  - Escalation keywords: memory/governance/soul/identity/signing/credential/guardian/audit/ledger/checksum/persona/orchestrator/translator
- **LOAD**: MID tier; confirmed=True required; writes to RAID; updates skill-checksums.json + skill-metadata.json + skill-watchlist.json; soul-guardian registration; Telegram + as-built.md notification
- **AUDIT**: compares current whole-file hash vs skill-checksums.json reference; drift = HIGH tier incident logged to ledger
- Execution intents: `skill_search` (LOW), `skill_review` (LOW), `skill_load` (MID), `skill_audit` (LOW), `skill_install` (MID — composite)
- **`skill_install` composite**: search→review→load in one flow; returns `requires_confirmation` with full `review_result` for Director; on confirmed=True proceeds to `lifecycle.load()`. Quick-classify keywords: "install a skill", "load a skill", "get me a skill", etc.
- Tier policy for skills: governed by `governance.json intent_tiers` section (not hardcoded in INTENT_TIER_MAP). `GovernanceEngine.get_intent_tier(intent)` reads from `intent_tiers`.
- **Skills domain in governance.json**: `skill_read: true` on LOW tier; `skill_load: true` on MID tier. `governance/engine.py` validates `skills` domain operations. Allowed actions: LOW=[skill_search, skill_review, skill_audit], MID=[skill_load, skill_unload].
- **Procedural memory seed**: `qdrant.seed_skill_install_procedure()` called at startup — writes 3-step skill install sequence to PROCEDURAL collection (`human_confirmed=True`). Idempotent (checks for existing entry). devops_specialist retrieves it on skill install requests.
- Metadata: `/home/sovereign/security/skill-metadata.json` (loaded_at, last_accessed, clawhub provenance)
- Watchlist: `/home/sovereign/security/skill-watchlist.json` (read at startup; merged into SoulGuardian protected_files)

### Config Change Notification Policy
Module: `config_policy/notifier.py`
- Fires AFTER a confirmed write to any in-scope file (post-write notification)
- Sends Telegram message to Director with what changed, who proposed it, and why
- Appends CEO-readable narrative entry to `/home/sovereign/docs/as-built.md`
- Logs technical detail (checksums, hashes) to AuditLedger — NOT to as-built.md
- `config_write()` helper for future adapters writing RAID config files directly

**In-scope files and policy tiers:**

| File / Pattern | Policy Tier | Notes |
|---|---|---|
| `governance.json` | ANY | Policy document |
| `sovereign-soul.md` | HIGH | Identity — double confirmation |
| `/home/sovereign/security/*.yaml` | MID | Security pattern files |
| `/home/sovereign/personas/*` | MID | Specialist personas |
| `/home/sovereign/skills/*` | MID | Skill definitions (add or remove) |
| `skill-checksums.json` | HIGH | Tamper evidence |

- Confirmation enforcement is at governance layer (execution engine); notifier fires post-write
- `is_in_scope(path)` and `get_policy_tier(path)` available for any future adapter to check

### OpenClaw Skill Translation Rules
Skills from community formats (e.g. OpenClaw registry) can be translated by mapping:
- `exec/system.run` → BrokerAdapter at the declared `tier_required`
- `web_fetch` / `browser` → A2ABrowserAdapter (query string only — no direct URL fetch)
- memory writes → episodic or prospective collections only (not semantic/procedural without confirmation)
- All adapter calls route through normal governance before execution — no bypass
- No OpenClaw runtime dependency; no clawhub CLI; intelligence only

---

## Sovereign Wallet Architecture (Phases W1 + W2)

### Container: sov-wallet
- Base image: `node:18-alpine` (thin Express.js Safe Transaction Service proxy only)
- Networks: `ai_net` (wallet API port 3001) + `browser_net` (Safe Transaction Service API internet egress)
- Wallet API: `http://sov-wallet:3001` — `GET /safe/nonce`, `POST /safe/propose`, `GET /safe/pending`, `GET /health`
- No Chrome, no MetaMask, no CDP — removed in favour of direct eth_account signing
- Config: `secrets/wallet.env` — SAFE_ADDRESS, CHAIN_ID, SOV_WALLET_URL, ETH_RPC_*, BTC_RPC_*, SPECTER_*

### Key Material (all on RAID `/home/sovereign/keys/`)
- `sovereign.key` / `sovereign.pub` — Ed25519 signing keypair (also mounted as Docker secret in sovereign-core)
- `wallet-seed.enc` — BIP-39 mnemonic encrypted with HKDF(sovereign.key)+AES-256-GCM (12-byte nonce prefix)
- `wallet-seed.gpg` — GPG backup encrypted to Director's key (`matt@digiant.co.nz`)
- `wallet-state.json` — `{address, derivation_path, initialized_at}` (perms 600)
- `director.gpg.pub` — Director's OpenPGP public key for GPG backup

### Seed Encryption Invariant
`SigningAdapter.encrypt_seed(phrase)` and `decrypt_seed(blob)`:
- Derives 32-byte AES key via HKDF-SHA256 from raw Ed25519 private key bytes (info=`b"sovereign-wallet-seed-v1"`)
- AES-256-GCM with AAD `b"sovereign-wallet-v1"` — format: 12-byte nonce || ciphertext+GCM-tag
- Derived key is **never written to disk** — zeroed in memory after each encrypt/decrypt
- sovereign.key is mounted as Docker secret at `/run/secrets/sovereign_key`; `SOVEREIGN_KEY_PATH` env var points to it

### Wallet Config (`/home/sovereign/governance/wallet-config.json`)
- Contains: Safe address, 2-of-3 owner structure, ETH node URLs, BTC/Specter config
- Mounted `:rw` in sovereign-core so WalletAdapter can write Rex's address on first boot
- Signed as `wallet_config_snapshot` alongside `governance_snapshot` on every startup
- Read: LOW tier (`wallet_read_config` intent) — any agent. Write: MID tier, Director confirmation only.
- ETH nodes: `172.16.201.15:8545` (primary exec), `172.16.201.2:8545` (secondary), `.15:5052` (beacon)
- BTC: Bitcoin Knots at `172.16.201.5:8332`, Specter at `172.16.201.5:25441`
- **ETH/BTC node connections are NOT active** — config is stored for future use; no adapters actively query nodes yet

### Safe Multisig (Ethereum)
- Address: `0x50BF8f009ECC10DB65262c65d729152e989A9323` (Ethereum Mainnet)
- Threshold: 2-of-3 — Rex (#1 sovereign-core), Director Ledger (#2), Director Mobile (#3)
- `propose_safe_transaction(to, value, data, purpose)` — HIGH tier, double confirmation
  - Signs EIP-712 SafeTx typed data directly via `eth_account.sign_message` (no MetaMask/browser)
  - Decrypts seed from `wallet-seed.enc` at call time; private key zeroed immediately after
  - Submits off-chain proposal to Safe Transaction Service via sov-wallet proxy (browser_net)
  - Signs canonical `{safe, to, value_wei, purpose, proposed_at}` dict with sovereign.key
  - Sends Telegram notification with proposal summary + `/verify <sig_prefix>`
  - Full ETH signature stored in audit ledger ONLY — never in Director-facing messages

### BTC Specter Multisig
- Same BIP-39 mnemonic as ETH wallet — BTC key at `m/48'/0'/0'/2'` (P2WSH multisig)
- `get_btc_xpub()` — LOW tier, derives xpub at `m/48'/0'/0'/2'` using `embit`
  - One-time ceremony: decrypts seed, derives child key, serialises as standard `xpub` (BIP-32 `0x0488B21E`)
  - Outputs full Specter key descriptor: `[fingerprint/48'/0'/0'/2']xpub...`
  - Writes `rex_xpub`, `rex_fingerprint`, `rex_descriptor` to `wallet-config.json` (`btc.*`)
  - Sends Telegram notification with descriptor, logs `wallet_btc_xpub` to audit ledger
  - xpub is public key material — paste descriptor into Specter Desktop as 2-of-3 P2WSH signer
- 2-of-3 Specter wallet: Rex Zpub + 2× Director Ledger keys
- Lightning/PSBT signing (`sign_psbt()`) — HIGH tier — not yet built

### Governance
- `wallet_read_config` → LOW (any agent, no confirmation)
- `wallet_get_btc_xpub` → LOW (public key export, no signing authority)
- `wallet_get_address` → MID
- `wallet_sign_message` → MID
- `wallet_get_proposals` → MID
- `wallet_propose_safe_tx` → HIGH (double confirmation)

### Anti-Spoofing (/verify)
- Every wallet keygen and Safe proposal message includes `rex_sig:<8-char-prefix>...` + `/verify <prefix>`
- `/verify <prefix>` command in Telegram gateway → calls `/wallet/verify?prefix=` on sovereign-core
- sovereign-core scans audit JSONL for matching entry, reconstructs canonical payload, calls `signer.verify()`
- Responds: `✓ Verified` + proposal summary, or `✗ Not found/invalid`
- Verification requests logged to audit ledger

---

## Phase Close Checklist
At the end of every phase, before moving on:
1. Append a signed-off entry to `/home/sovereign/docs/as-built.md` (RAID) covering: containers changed, config changes made, volumes/mounts, models, validation results, and signed-off invariants.
2. Update phase status table in this file.
3. Update `/home/matt/.claude/projects/-docker-sovereign/memory/MEMORY.md`.

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

# Ollama inference
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"action":{"domain":"ollama","operation":"query"},"tier":"LOW","prompt":"What is 2+2?"}'
```
