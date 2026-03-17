# Sovereign AI — Phase Archive

Completed phases moved here to keep the root CLAUDE.md lean. Content is historical reference only — do not edit.

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
