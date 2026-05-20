# Sovereign AI — Phase Archive

Completed phases moved here to keep the root CLAUDE.md lean. Content is historical reference only — do not edit.

---

## Archived: Adapter Removal Plan — Browser → WebDAV → CalDAV (COMPLETE 2026-04-03)

Archived from root CLAUDE.md 2026-05-15. All three phases completed. See Phase Status table and as-built.md for the completed record.

**Principle**: No duplicate of what nanobot does. All application I/O (browser, Nextcloud files, calendar/tasks) routes through nanobot-01. Direct adapters in sovereign-core are legacy and must be removed.

**Context**: BrowserAdapter was calling `POST /run` (non-existent on a2a-browser) — fixed to use `POST /search` / `POST /fetch` in session 2026-04-01. Adapters exist at `core/app/execution/adapters/browser.py`, `core/app/adapters/webdav.py`, `core/app/adapters/caldav.py`.

---

### Phase 1: Browser (COMPLETE)

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

### Phase 2: WebDAV (COMPLETE)

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

### Phase 3: CalDAV (COMPLETE)

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

---

## Phase 0 Validated Capabilities

- `docker ps`, `docker logs`, `docker stats` → observer status (read-only) ✓
- WebDAV read → observer status ✓
- LOW tier: no confirmation required ✓
- MID tier: `requires_confirmation: true` returned ✓
- HIGH tier: `requires_double_confirmation: true` returned ✓
- Illegal action at LOW tier (e.g. file delete) → rejected ✓
- Ollama inference via `/query` route → live GPU inference working ✓
- Whisper medium model → cached in `whisper_models` volume, GPU transcription working ✓

---

## Phase 3 Design Notes (Telegram + Multi-Pass Cognitive Loop) — COMPLETE

- Separate `gateway/` container — Telegram NOT embedded in sovereign-core
- Gateway responsibilities: auth, session state, confirmation prompts, forward JSON to core
- Cognitive loop: Orchestrator classification → Specialist reasoning → Orchestrator evaluation → Execution → Memory decision → Translator translation
- Personas stored on RAID: `/home/sovereign/personas/` — orchestrator.md, translator.md, 5 specialist files
- Structured JSON enforcement: Ollama called with `"format": "json"`, reject non-JSON
- Safety rules: specialists cannot override tier, write memory, or escalate without Sovereign Core approval
- **ALL Director messages pass through translator pass** → `director_message` field
- `orchestrator.md` = classify/evaluate/memory-decision persona; `translator.md` = Director-facing translation only (distinct roles)
- **`sovereign-soul.md` = sole identity document** — orchestrator.md and translator.md are functional personas only
- Qdrant vector DB at `/home/sovereign/vector` — live (Phase 3.5)
- Phase 4 cognitive memory: `search_all_weighted()`, `classify_query_type()`, `ensure_gap_entry()`, `get_due_prospective()`, session-start morning briefing, richer memory_decision schema — complete

---

## Agent Layer Architecture (2026-03-04) — COMPLETE

- **Sovereign Core** = reasoning engine, orchestration, governance enforcement
- **Specialists** report to Sovereign Core only: devops_agent, research_agent, business_agent, security_agent, memory_agent
- **Translator** (`translator.md`) = Director interface only — translates results to plain English, no other role
- **No specialist may communicate directly with Director** — enforced in persona definitions + code
- `cog.ceo_translate()` called at end of both handle_chat return paths; populates `director_message` field
- Gateway checks `director_message` first; falls back to `_format_result()` if translation returns empty

---

## OpenClaw Skill Translation Rules

Skills from community formats (e.g. OpenClaw registry) can be translated by mapping:
- `exec/system.run` → BrokerAdapter at the declared `tier_required`
- `web_fetch` / `browser` → A2ABrowserAdapter (query string only — no direct URL fetch)
- memory writes → episodic or prospective collections only (not semantic/procedural without confirmation)
- All adapter calls route through normal governance before execution — no bypass
- No OpenClaw runtime dependency; no clawhub CLI; intelligence only

---

## Full Phase History

| Phase | Description |
|-------|-------------|
| 0 | Read-only observer — docker/file/WebDAV reads, Ollama cognition, governance active |
| 1 | Broker + MID tier docker workflows — docker_ps/logs/stats/restart live |
| 2 | WebDAV r/w, CalDAV, IMAP (personal+business), SMTP, HIGH tier |
| Security | Scanner, guardrail, soul guardian, audit ledger, GitHub adapter, Sovereign-soul.md protection |
| 3 | Telegram gateway, multi-pass CEO cognitive loop, persona switching, SearXNG, agent layer |
| 4 | Cognitive memory: weighted retrieval, query type classification, prospective briefing, gap auto-create |
| 4.5 | Observability: /metrics endpoint, scheduled self-check, morning health brief, self-diagnostic routing, DCL, persona renames |
| 5 | Sovereign Secure Signing: Ed25519 keypair, SigningAdapter, signed audit ledger (rex_sig), governance snapshot, browser ACK |
| 6 | Sovereign Skill System: SkillLoader, SKILL.md format, dual-layer integrity (body checksum + reference file), specialist injection, 4 seed skills |
| 6.5 | Skill Lifecycle Manager: SEARCH/REVIEW/LOAD/AUDIT, security review pipeline, soul-guardian registration, config change notification policy |
| 6.6 | Skill system gap fixes: skills domain in governance.json, GovernanceEngine.get_intent_tier(), SearXNG-only skill discovery, skill_install composite, procedural memory seed |
| W1 | Sovereign Wallet Phase 1: sov-wallet container, BIP-39 keygen, HKDF+AES-256-GCM seed encryption, GPG Director backup, signed Telegram notification, /verify anti-spoofing |
| W2 | Sovereign Wallet Phase 2: WalletControlAdapter (eth_account direct signing), sign_message (MID), propose_safe_transaction (HIGH, EIP-712 SafeTx), get_pending_proposals, get_btc_xpub (LOW) |
| 7 | Generalised task scheduler: NL intent parser, Qdrant-backed task storage, 60s background executor, cron/interval/one_time schedules |
| OC-S1 | OpenClaw March Stage 1: WebDAV/CalDAV/IMAP/SMTP adapters rewritten; four Path 1 SKILL.md prompt wrappers deployed; new intents wired |
| OC-S2 | OpenClaw March Stage 2: nanobot-01 sidecar live on ai_net; FastAPI bridge + NanobotAdapter; soul section 12 "Division of Sovereignty" |
| OC-S3 | OpenClaw March Stage 3: Model B DSL — typed operations: frontmatter; NanobotAdapter DSL intercept; _load_skill_dsl() mtime cache; 4 skills updated (25 total ops) |
| OC-S3.1 | Broker CLI exec: commands-policy.yaml typed allowlist; POST /exec/:commandName; SHELL_META guard; broker_exec DSL tool type |
| BugFix-2026-03-13 | IMAP: archive_folder space quoting, uid guards. CalDAV: calendar fast-path, _normalise_dt(), multi-field extraction, specialist schema hints. list_events regex fix |
| OC-S4 | Community skills installed: imap-smtp-email + openclaw-nextcloud. Mail domain rewired to nanobot. skill_install flow: 7 bugs fixed. Direct URL install path |
| OC-S5 | nanobot-01 as primary skill executor. CredentialProxy single-use token delegation. Confirmed-continuation bypass |
| OC-S6 | python3_exec cutover. imap_check.py, smtp_send.py, nextcloud.py, feeds.py deployed. rss-digest skill. route_cognition PASS 2 wiring |
| CL-Rework | 5-pass cognitive loop, InternalMessage envelope, nanobot protocol contract, untrusted tagging, translator firewall |
| Skill-Harness | Stateful multi-step skill lifecycle harness (search→list→review→install→clear). Working_memory checkpointing. HIGH confirm on install. E2E tested 2026-03-23 |
| NC-Mail | `nc-mail` python3_exec skill (9 ops). Stable databaseId. `_unwrap_nb`, `_DIAGNOSTIC_INTENTS` for write ops, outcome stamps. T-M1–T-M7 passing 2026-03-25 |
| NC-Notes | 5 ops (notes_list/read/create/update/delete) in openclaw-nextcloud. Notes-Index title→ID lookup (5 min TTL). T1–T6 passing 2026-03-24 |
