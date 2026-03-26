# Sovereign Core — Implementation Invariants

This file is loaded by Claude Code when working inside `core/app/`. It supplements the root `CLAUDE.md` with all adapter-level, cognition, and governance implementation rules.

---

## General Rules

- All adapters use `httpx.AsyncClient` (never blocking `requests` in async methods)
- Ollama API: always set `"stream": False` (default streams NDJSON, breaks `r.json()`)
- FastAPI uses lifespan context manager (not deprecated `@app.on_event`)
- Governance validates → raises `ValueError` on failure, returns rules dict on success
- Execution engine wraps `gov.validate()` in `try/except ValueError`
- `requires_confirmation` / `requires_double_confirmation` read from returned rules dict

---

## Cognitive Loop Invariants

### Pass routing
- PASS 1 (orchestrator classify), PASS 3b (specialist inbound), PASS 4 (orchestrator evaluate), PASS 5 (translator) → always local Ollama
- PASS 2 (specialist outbound) → only externally-routable pass via `_routing_decision()`
- `_routing_decision(prompt, user_input)` scores complexity on `user_input` (NOT full specialist prompt — persona length would inflate every score)
- DCL hard-block: tier in `{"PRIVATE","SECRET"}` → `force_local=True` regardless of explicit override
- Provider signals: `_CLAUDE_SIGNAL_RE` (architectural/plan/review/design/strategy) → claude; `_GROK_SIGNAL_RE` (current/latest/news/today/recent/market) → grok; default → grok
- Operational penalty: score≥0.50 AND `_OPERATIONAL_RE` (restart/container/service/deploy/port/compose/nginx) → -0.20
- `specialist_plan` always includes `_routing_reason`, `_complexity_score`, `_intended_provider` (even on local fallback)
- Claude/Grok API unavailable → graceful fallback to Ollama; no error raised

### Confirmed-continuation bypass
- When `confirmed=True` and `pending_delegation._pending_load is not None`: skip PASS 2 + PASS 3 (specialist + CEO evaluation)
- Reasoning already happened before confirmation prompt; re-running is pure overhead (~80s saved)
- Stash carry-forward state in `pending_delegation._pending_load` to get this bypass
- Remaining work: PASS 4 dispatch + translate only (~45s)
- **CRITICAL**: `confirmed` must be passed to `_dispatch_inner` via `payload={"confirmed": confirmed}` in the central `_dispatch()` call in `handle_chat`. Without this, `confirmed = payload.get("confirmed", False)` is always False inside `_dispatch_inner` and the short-circuit never fires (confirmed-continuation bypass is silently bypassed)

### Untrusted nanobot content
- All nanobot results stamped `_trust: "untrusted_external"` in `nanobot.py _forward()`
- Scanner runs on result content between EXEC and PASS 3b
- Flagged content: `_untrusted_flagged: True` + `_scan_categories` set; ledger entry logged
- `prompts.specialist_inbound()` surfaces trust warning when `_untrusted_flagged` or `_trust == "untrusted_external"`
- `result_for_translator` from PASS 4 is the fabrication firewall — translator receives ONLY this

### InternalMessage envelope (`cognition/message.py`)
- Director input hashed at PASS 1 (SHA-256); raw text never stored in envelope or passed between agents
- `append_pass()` hashes current payload (SHA-256 hex[:16]) — never stores raw content
- `nanobot_request_slice()` returns only `{request_id, skill, operation, payload, timeout_ms}`
- `translator_slice()` returns only `result.get("result_for_translator")`
- `validate()` pass_num check uses `pass_num > 0` guard (avoids false failures on PASS 1 construction)
- `set_security_clearance()` only accepts: `"cleared"` | `"conditional"` | `"blocked"`

### Qdrant memory architecture (RAID-only sovereign collections)
- `self.client` → qdrant container (`http://qdrant:6333`, tmpfs-backed `sovereign_runtime` volume) — working_memory ONLY; on_disk=False vectors; ephemeral by design
- `self.archive_client` → qdrant-archive container (`http://qdrant-archive:6333`, `/home/sovereign/vector` RAID) — all 7 sovereign collections; durable; on_disk=True
- `_client_for(collection)` helper — routes `WORKING` → `self.client`, everything else → `self.archive_client`
- NO `wm_client` in-process Python client — working_memory lives in the qdrant container (tmpfs)
- `setup()` always recreates working_memory fresh on startup; creates 7 RAID collections on archive_client if absent
- `startup_load()` — pre-warms working_memory from RAID (top-50/collection, score≥0.3, hard stop at 2GB); tagged `startup_load=True` so shutdown_promote() skips them
- `shutdown_promote()` — clean exit: promotes eligible working_memory entries → RAID collections; skips startup_load items, procedural, and items with no valid type
- `sync_from_archive()` / `sync_to_archive()` — both are no-ops; retained for API compat; log a warning
- Lifecycle: `setup()` → `startup_load()` (RAID→working_memory, 2GB limit) → session → `shutdown_promote()` (working_memory→RAID)
- Crash without clean shutdown: un-promoted working_memory entries are LOST — known acceptable risk; mitigated by 64GB RAM upgrade (enables periodic background flush)
- Graceful shutdown: `stop_grace_period: 30s` (compose.yml), uvicorn `--timeout-graceful-shutdown 25` (Dockerfile)
- Embeddings: `_embed()` uses `self._embed_url` (default `http://ollama-embed:11434`) — CPU-only service; never blocks GPU
- Key generation: `_generate_key_and_title()` uses `self._ollama_url` (`http://ollama:11434`) — GPU llama3.1:8b; separate from embedding

### Memory Index Protocol (MIP)
- `execution/adapters/qdrant.py` implements ContextKeep v1.2 two-step retrieve pattern
- Every sovereign collection write (not working_memory) calls `_generate_key_and_title()` → single Ollama call (10s timeout) → stores `_key`, `title`, `last_updated` in payload
- Key format: `{type}:{domain}:{slug}` — prefix assembled from known fields, only slug is LLM-derived; LLM cannot override type or domain
- Fallback: Ollama timeout/failure → `_no_key: True` + `last_updated` stored; Python WARNING logged to container logs; `key_generation_failed` entry written to `memory-promotions.jsonl` — never blocks promotion
- `startup_migration()`: called at boot after `startup_load()`; scrolls all 7 sovereign collections, patches `_no_key: True` on pre-MIP entries (no re-embedding); idempotent
- `seed_static_facts()`: idempotent high-value backfill; checks `_backfill_seed_id` before writing; if existing entry has wrong key (exact mismatch), deletes and reseeds
- `tag_high_value_entries(patterns)`: startup scan of semantic collection; matches entries by content substring; assigns `_key`+`title`+`last_updated` via `set_payload()` (no re-embedding); idempotent (already-keyed entries skipped)
- Soul checksum seed is dynamic — computed from `guardian.get_checksum()` at startup; seed_id `backfill_v1_soul_checksum` is recreated if the checksum changes
- `list_all_keys()`: scrolls all 7 collections, returns index fields only (`collection`, `point_id`, `key`, `type`, `title`, `last_updated`) — no content, no vector search
- `retrieve_by_key(key)`: Qdrant payload filter `_key == key` across all 7 collections in order; never touches vector index; returns full payload + collection/point_id, or None
- Two intents: `memory_list_keys` (LOW, memory_agent) and `memory_retrieve_key` (LOW, memory_agent) — both in `_DIAGNOSTIC_INTENTS`, both in `_system_signals`
- `_AGENT_DEFAULT_INTENT["memory_agent"] = "memory_list_keys"`
- Governance: `memory_index` domain added to `governance/engine.py`; gates on `memory_search` permission (already true at LOW)
- Session tracking: `self._mip_listed_this_session` (bool) on ExecutionEngine; set True on `list_keys` dispatch; checked on `retrieve_key` — violation logs Python WARNING + signed ledger entry (`mip_protocol_warning`), never blocks
- PASS 1 prompt: `memory_agent` block + `MEMORY RETRIEVAL PROTOCOL — MANDATORY` rule added to `cognition/prompts.py:classify()`
- MIP payload totals (first boot): 723 legacy entries → `_no_key=True`, 22 new entries → proper `_key`
- `"eth address"`, `"wallet address"`, `"safe address"`, `"tailscale"` added to both `_system_signals` (passes conversational guard) and `_mem_list_kw` (short-circuits to `memory_list_keys`); "what is my ETH address" now routes correctly
- `memory_index` domain added to short-circuit tuple at line 1333; full 5-pass loop caused PASS 4 rejection (same pattern as `browser_config`)
- Short-circuit `else:` branch passes `delegation` to `_dispatch` so `retrieve_key` key extraction works (target field carries the extracted key from `_quick_classify`)

### Memory dispatch
- Async memory write via `asyncio.create_task()` — never blocks return path
- Prospective memory confirmation gate: for mutating intents (`create_event`, `create_task`, `write_file`, `send_email`, `delete_file`, `delete_email`, `delete_task`, `restart_container`, `create_folder`) — `execution_confirmed` stamped from actual HTTP status code; never from LLM assertion
- Not 2xx → `execution_confirmed: False, outcome: "unconfirmed"` regardless of LLM memory decision

---

## Nanobot Adapter (`adapters/nanobot.py`)

### Dispatch model
- `_NATIVE_TOOLS={"browser"}` stays in sovereign-core
- `_REMOTE_TOOLS={"filesystem","exec","python3_exec","imap","smtp","webdav","caldav"}` forwarded to nanobot-01
- `broker_exec` checked against SYSTEM_COMMANDS whitelist first; non-whitelisted → nanobot-01 + deprecation warning

### Credential delegation
- `op_spec.credential_services` → `CredentialProxy.issue()` → UUID token → forwarded in context
- nanobot-01 redeems via POST sovereign-core:8000/credential_proxy → injected as subprocess env vars → immediately invalidated
- Single-use token, 60s TTL

### Response normalisation
- `_forward()` reads contract fields: `success`, `raw_error`, `status_code`, `data`
- Derives legacy `status` field from `contract_success` for backward compat
- python3_exec responses are flat (no nested "result" key) — if `body.get("result")` is None, builds `body_result` from all non-wrapper body fields (wrapper = `{run_id, skill, action, path, elapsed_s}`)
- Use `nb.get("result") if nb.get("result") is not None else nb` — NOT `nb.get("result", nb)` (latter returns None when key exists)
- All results stamped `_trust: "untrusted_external"`

### Hard boundary (do not violate)
- Broker handles ONLY: `docker_ps/logs/restart/stats/inspect/exec, uname/df/free/ps/nvidia_smi/systemctl_status/journalctl`
- All other application skills (IMAP/SMTP/feeds/WebDAV/CalDAV) → nanobot-01
- Do NOT add new system commands to broker without architectural review

---

## CalDAV Adapter (`adapters/caldav.py`)

- `_discover_calendar()` always returns a dict `{url, propfind_http_status, propfind_response_body, calendars_found}` — never `None`
- `create_event` / `delete_event` / `create_task` / `delete_task` always include `http_calls_made`, `http_status`, `response_body`, `propfind_http_status`; if call not made → says so explicitly ("PUT not attempted")
- No synthesised error strings — only raw status codes and bodies
- All write ops PROPFIND to `/remote.php/dav/calendars/digiant/` (Depth:1) first, then PUT/DELETE to `{discovered_url}/{uid}.ics`. Never assume LLM label is valid Nextcloud slug
- All methods check `r.status_code` directly — no `raise_for_status()`. Return `{"status": "error", "error": ..., "http_status": ...}` for non-2xx
- `_safe_translate` never passes error results to translator; error path is deterministic
- `create_task(calendar, uid, summary, due, start, description, status)` generates valid VTODO ICS; `delete_task` delegates to `delete_event`
- Tasks calendar slug discovery: same `_discover_calendar` partial-match logic with `"tasks"` as default name

### CalDAV in execution/engine.py
- Calendar fast-path in `_quick_classify`: `create_event`, `delete_event`, `update_event` always route to `business_agent` — never through CEO LLM
- `delete_event` and `update_event` in `INTENT_ACTION_MAP` (domain=caldav) and `INTENT_TIER_MAP` (MID)
- `_dispatch_inner` has `calendar_update` handler routing to `caldav.update_event()`
- `_normalise_dt(value)`: strips NZDT/NZST/NZT/UTC/GMT suffixes, ordinal suffixes (st/nd/rd/th), "at" separators; tries `fromisoformat` → multiple `strptime` formats; default year 2026 if absent
- `calendar_create` in `_dispatch_inner` tries 12+ field names for start/end (start, start_time, datetime, when, date_time, event_start, scheduled_at, begin + date+date_part+time combinations; scans content/draft_content/target as last resort)
- `prompts.specialist()` injects intent-specific required-field reminders for create/delete/update_event, create_task — includes today's date for relative date resolution

---

## IMAP Adapter (`adapters/imap.py`)

- All operations use `mail.uid()` throughout — sequence numbers are unstable
- `list_inbox()` fetches all messages with real UIDs via `SEARCH + FETCH RFC822.HEADER`
- UID guards in `_move_sync()`, `_delete_sync()`, `_mark_flag_sync()`: if uid is None/empty/whitespace → return `{status: error, step: uid_guard}` immediately
- `import email.message` must be explicit (bare `import email` does not expose submodule)
- Archive folder candidates: `["archive", "archives", "inbox.archive", "saved messages"]` — no Gmail-specific entries
- Accounts on `digiant.co.nz`, `digiant.nz`, or `e.email`
- `_move_sync()`: checks for spaces in `archive_folder` → wraps in double-quotes before IMAP UID COPY command. imaplib does NOT auto-quote mailbox names. Error dict includes `imap_folder_arg` showing exact string sent

### Community skill routing (execution/engine.py)
- Mail domain calls `self.nanobot.run("imap-smtp-email", action, params)` — NOT IMAPAdapter/SMTPAdapter
- Account suffix: `_suf = "" if account == "business" else "_personal"` selects personal vs business command
- Account resolution order: `sp.get("account")` → `delegation.get("target")` → `action.get("account")` → `"personal"` (never default to business)
- CalDAV/WebDAV still use Python adapters
- Email list pre-formatter in `execution/engine.py`: runs after EXEC on `fetch_email/search_email/list_inbox` intents; produces numbered `sender — subject (date)` lines; builds `uid_index` dict for subsequent delete/move
- Loop variable MUST be `_em` (not `_msg`) — `_msg` is the InternalMessage envelope; shadowing it causes a 500 on the next pass
- `delete_message` / `move_message` operations: defined in SKILL.md frontmatter (not server.py `_translate_imap_smtp`); `imap_check.py` `cmd_delete` + `cmd_move` use `_resolve_uid()` helper
- SKILL.md body checksum: use SkillLoader's exact regex `^---\n(.*?)\n---\n(.*)` group(2), NOT `split('---', 2)[2]` — they differ when body starts with newline
- `specialist_outbound` receives `context_window` (last 4 turns) so delete/move can infer account and UIDs from prior list results without repeating them
- Schema hints for `delete_email`/`move_email` in `prompts.specialist_outbound()` — `from_addr` is display name, NOT a guessed email address

---

## WebDAV Adapter (`adapters/webdav.py`)

- `path = action.get("path", "/")` always returns `"/"` (static `INTENT_ACTION_MAP` entries carry no runtime path)
- Runtime file path always comes from specialist output: check `specialist.get("path")` first, then `specialist.get("target")`
- `target` field is for container names; is last resort for webdav paths

---

## Broker Adapter (`adapters/broker.py`)

- `broker.py exec_command()` never raises — all errors returned as structured dicts
- `broker_exec` DSL tool resolves command name via `op_spec.get("action", action)` — op_spec's `action` names the broker command; outer DSL op key is fallback

### Broker `index.js` invariants
- `POST /exec/:commandName` route registered BEFORE docker-policy catch-all — bypasses docker-policy trust check; uses commands-policy tier
- `SHELL_META` guard applied to all string params before any processing
- `allowlist: []` (empty array) = deny all — check is `allowlist !== undefined && !includes(val)` not `length > 0`
- `__container_exec__` → `execInContainer(cmd.container, cmd.fixed_args)`; `__script__` → path traversal + existence check + `spawnRun`
- All execution via `spawn(shell:false)`
- To enable systemctl/journalctl: (1) `pid: host` in broker compose.yml, (2) `nsenter` in broker Dockerfile (`apk add util-linux`), (3) `enabled: true` in commands-policy.yaml

---

## Skill System

### SKILL.md format
```yaml
---
name: <skill-name>
version: "1.0"
description: "<short description>"
sovereign:
  specialists: [research_agent]
  tier_required: LOW
  adapter_deps: [browser, ollama]
  checksum: <sha256-of-body>
---
# Skill body
```

### Integrity model
- `sovereign.checksum` = SHA256 of body (text after frontmatter's closing `---`)
- `/home/sovereign/security/skill-checksums.json` = whole-file SHA256 reference (rw-mounted)
- SkillLoader validates both on every load; either mismatch → refuse + audit log
- First boot (no reference file) = bootstrap mode: reference created from current files
- Body checksum method: SkillLoader regex `group(2)` — NOT `content.split('---', 2)[2]`
- SkillLoader `_ALWAYS_AVAILABLE` must include `"nanobot"` — otherwise python3_exec skills are skipped

### Skill Lifecycle Manager (`skills/lifecycle.py`)
- **SEARCH**: SearXNG via a2a-browser — query `"sovereign skill <query> SKILL.md site:github.com"`. `_github_url_to_raw()` converts blob/tree URLs to raw. `_fetch_raw_url()` tries direct httpx first, falls back to `self.browser.fetch()` (a2a-browser has internet egress; sovereign-core does not)
- No direct calls to clawhub/OpenClaw registry URLs
- **REVIEW**: escalation keyword scan → SecurityScanner → `cog.security_evaluate()` → structured verdict. Non-certified → always "review" decision. Escalation keywords: memory/governance/soul/identity/signing/credential/guardian/audit/ledger/checksum/persona/orchestrator/translator
- **LOAD**: MID tier; `confirmed=True` required; writes to RAID; updates skill-checksums.json + skill-metadata.json + skill-watchlist.json; soul-guardian registration; Telegram + as-built.md notification
- **AUDIT**: compare current whole-file hash vs reference; drift = HIGH tier incident

### skill_install composite flow
- `_system_signals` must include `"skill", "clawhub", "openclaw"` or conversational guard fires first
- `intent_tiers.skill_install` must be `LOW` in governance.json (composite manages its own confirmation)
- `handle_chat` promotes `requires_confirmation`, `pending_delegation`, `summary`, `escalation_notice` to top-level result_dict
- `_quick_classify` sets `target: user_input` (full text including URL) — URL preservation is critical
- `op == "install"` in engine.py extracts URL from `delegation.get("target")` BEFORE falling back to `sp.get("search_query")` — specialist strips the URL; without this the URL is lost and SearXNG returns wrong skill
- Confirmed-continuation: `confirmed=True + _pending_load` → short-circuit to `lifecycle.load()` only

### Config change notification (`config_policy/notifier.py`)
- Fires AFTER confirmed write to any in-scope file (post-write)
- In-scope: `governance.json` (ANY), `sovereign-soul.md` (HIGH), `/home/sovereign/security/*.yaml` (MID), `/home/sovereign/personas/*` (MID), `/home/sovereign/skills/*` (MID), `skill-checksums.json` (HIGH)
- Sends Telegram + appends narrative to `/home/sovereign/docs/as-built.md`
- Technical detail (checksums, hashes) → AuditLedger ONLY, not as-built.md

---

## Task Scheduler (`scheduling/task_scheduler.py`)

- Data-driven — no task-specific code
- Task types: PROSPECTIVE (when/status/next_due), PROCEDURAL (steps, `human_confirmed=True`), EPISODIC (run history) — all share `task_id`
- Scheduler loop: every 60s; uses `qdrant.archive_client.set_payload()` to update next_due/status without re-embedding
- `compute_next_due()` handles cron/interval/one_time
- Scheduler keywords in `_quick_classify`: must be checked BEFORE conversational guard and BEFORE email keywords to avoid misrouting
- `confirmed=True` must be passed via `payload={"confirmed": confirmed}` in short-circuit `_dispatch` call so PROCEDURAL writes get `human_confirmed=True`
- `_get_procedure()` + `_find_point_id()`: use **filtered** Qdrant scroll (`FieldCondition` on `task_id` + `type`), NOT unfiltered `limit=200`. PROCEDURAL can grow past 200 entries (skill harness procedural memory bloat). Full-table scan causes `no procedure found` once collection > limit.
- `seed_nightly_dev_task()` idempotency: checks PROCEDURAL for `type=task_procedure` + step `intent=dev_analyse + trigger=nightly`, then verifies PROSPECTIVE `status=active`. Title-based check unreliable — `qdrant.store()` overwrites `metadata["title"]` with LLM-generated `_key_fields["title"]` (last in payload merge order).
- `qdrant.store()` title overwrite invariant: if you need a stable title in a stored entry, pass `_key` in `metadata` so LLM generation is skipped and only `last_updated` is added to `_key_fields`.
- SI harness `_write_proposal()` + `propose()`: dedup gate via `_existing_pending_proposal()` — checks PROSPECTIVE for existing `pending_approval` proposal with same `trigger` + dedup field (`task_id`/`intent`/`event_type`/`metric`). Dedup fields stored in proposal payload so filter works on next cycle.

---

## Wallet Implementation

- `SigningAdapter.encrypt_seed(phrase)` / `decrypt_seed(blob)`: HKDF-SHA256 from Ed25519 private key bytes (info=`b"sovereign-wallet-seed-v1"`) → AES-256-GCM (AAD=`b"sovereign-wallet-v1"`); format: 12-byte nonce || ciphertext+GCM-tag
- Derived key **never written to disk** — zeroed after each encrypt/decrypt
- `sovereign.key` mounted as Docker secret at `/run/secrets/sovereign_key`; `SOVEREIGN_KEY_PATH` env var points to it
- `wallet-config.json` at `/home/sovereign/governance/wallet-config.json` — mounted `:rw`
- ETH nodes NOT active — config stored for future use only
- Every wallet op includes `rex_sig:<8-char-prefix>` for `/verify` anti-spoofing
- Wallet governance: `wallet_read_config/get_btc_xpub` → LOW; `get_address/sign_message/get_proposals` → MID; `propose_safe_tx` → HIGH
