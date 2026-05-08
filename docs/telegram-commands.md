# Telegram Commands & Interaction Reference

**Status:** Live  
**Gateway module:** `gateway/main.py`  
**as-built entry:** n/a — operational reference doc

---

## Overview

The Telegram gateway is the Director's primary interface to Rex. All messages are restricted to the single authorized user (`OPENCLAW_TELEGRAM_ADMIN_CHAT_ID`). Unauthorized senders are silently ignored.

Two interaction modes:
- **Slash commands** — deterministic harness entry points; bypass NL routing entirely
- **Natural language** — everything else; routes through the 5-pass cognitive loop

---

## Slash Commands

### `/install <goal>`

Autonomous skill acquisition harness. Rex searches for a community skill matching the goal, LLM-selects the best candidate, presents a single confirm gate, then scans and installs.

```
/install crypto portfolio tracker
/install weather forecast
/install tether wallet development kit
```

No step-by-step micromanagement. One confirm gate before scan+install.

---

### `/skills <query>`

Browse available community skills without installing. Returns candidates from the OpenClaw registry.

```
/skills rss reader
/skills calendar sync
```

---

### `/selfimprove`

Manually triggers one Self-Improvement Harness observe cycle. Rex aggregates all monitoring sources, runs anomaly detection against baselines, and surfaces any pending proposals for Director review.

Equivalent to saying: `"run observe"` or `"self improve observe"` in natural language.

---

### `/devcheck`

Triggers the Developer Harness full analysis cycle (Phase 1 only). Rex runs pylint + semgrep + boundary_scanner against the codebase, scores findings, and sends results. BLOCK or ESCALATE gates require Director approval before Phase 4 (runsheet generation).

Also runs nightly at 14:00 UTC automatically. REVISE gate suppressed on nightly runs.

---

### `/portfolio`

Triggers a portfolio snapshot — current wallet balances and NZD/USD value.

*Note: gateway handler is wired but the portfolio harness backend is not yet built. Currently returns a stub response.*

---

### `/pm`

Project Management harness entry point.

*Note: PM harness is PLANNED (pending Director approval of the proposal). Returns stub message.*

---

### `/do_tax [year]`

NZ tax report harness. 3-turn human-in-the-loop flow.

```
/do_tax          — current NZ financial year
/do_tax 2026     — FY2026 (01 Apr 2025 – 31 Mar 2026)
```

**Turn flow:**
1. `/do_tax [year]` — Rex queries stored tax events for the FY, classifies crypto transactions, reports counts, asks for supplementary expense CSV filenames
2. Director replies with CSV names or `"none"` — Rex fetches from Nextcloud, merges expenses, reports counts, asks for confirmation
3. Director confirms — Rex generates `income{year}.csv` + `expenses{year}.csv` in `/Digiant/Tax/FY{year}/` on Nextcloud

**Prerequisites:** `semantic:tax:taxable_wallets` and `semantic:tax:staking_contracts` must be populated. `/do_tax` must be registered with BotFather manually (the gateway handles it; BotFather just needs the entry).

---

### `/remember <fact>` · `/memorise <fact>` · `/memorize <fact>`

Store a fact to memory. Forwards as `"remember that <fact>"` to the cognitive loop, which classifies it as `remember_fact` intent and writes to the appropriate sovereign collection.

```
/remember my ETH address is 0x623061...
/remember the Rocket Pool minipool address is 0x...
```

---

### `/verify <sig_prefix>`

Anti-spoofing check. Verifies that a message originated from Sovereign by looking up the Ed25519 signature prefix in the audit ledger.

Rex appends `rex_sig:<8-char-prefix>` to wallet/signing messages. Copy the prefix to verify:

```
/verify a7f3b2
```

Returns: event type, detail, and signed timestamp if found. Error if not found or invalid.

---

## Attachment Handling

Sending a file directly to the chat (no slash command needed) uploads it to Nextcloud `/downloads/` and triggers the Learning Harness in the background.

**Supported types:**

| Telegram type | Saved as |
|---------------|----------|
| Document (any) | Original filename |
| Photo | `photo_{timestamp}.jpg` (highest resolution) |
| Voice message | `voice_{timestamp}.ogg` |
| Video note (circle) | `videonote_{timestamp}.mp4` |
| Video | Original filename or `video_{timestamp}.mp4` |

Rex replies with confirmation: `"Uploaded {filename} ({size} KB)"`.

The Learning Harness runs immediately in the background (bypasses the UTC 15-17 synthesis window gate for Director-uploaded files).

---

## Confirmation Flows

Many actions require Director confirmation before executing. Rex pauses and waits for a reply.

### MID tier — single confirmation

Rex sends: `"[Confirmation required] <summary> — Reply yes to proceed or no to cancel."`

Reply `yes` / `y` / `confirm` → proceeds.  
Reply `no` / `n` / `cancel` → cancels.  
Any other message → cancels the pending action and processes the new message fresh.

### HIGH tier — double confirmation

Rex sends: `"[DOUBLE CONFIRMATION required] <summary>"`

Same reply flow, but the action itself requires `requires_double_confirmation` in governance — typically destructive operations (delete file, rebuild container, modify soul/governance files).

### Security guardrail

If the security scanner flags a planned action: `"⚠️ Security guardrail: confirmation required — Reason: <reason> — Proceed? Reply yes or no."`

### Low memory confidence

If Rex's confidence in the planned action is below threshold: `"⚠️ Low memory confidence ({score}) — Planned action: <intent> → <target> — Proceed anyway? Reply yes or no."`

---

## Natural Language — Reliable Patterns

These phrases have deterministic fast-paths in `_quick_classify` and bypass the full 5-pass LLM planning loop. They're consistently routed.

### News & Feeds

| Phrase | Routes to |
|--------|-----------|
| `"what's in the news"` · `"news brief"` · `"news update"` · `"news summary"` | News harness (RSS + Grok + browser → synthesised brief) |
| `"today's news"` · `"morning news"` · `"latest headlines"` · `"news today"` | News harness |
| `"what's happening"` · `"current events"` | News harness |
| `"my feeds"` · `"rss feed"` · `"news feeds"` · `"what's in my feeds"` | RSS-only (`rss-digest` skill) |
| `"latest from my feeds"` · `"from the news feeds"` | RSS-only |

### External Providers

| Phrase | Routes to |
|--------|-----------|
| `"ask grok about X"` · `"use grok to X"` · `"via grok X"` | Grok API directly |
| `"ask claude about X"` · `"use claude to X"` · `"via claude X"` | Claude API directly |

Provider directive is stripped from the prompt before sending (so Grok doesn't see "use grok" and get defensive about its identity).

### Memory

| Phrase | Routes to |
|--------|-----------|
| `"list my memories"` · `"show memory keys"` · `"memory index"` | MIP key index (all 7 collections) |
| `"remember that X"` · `"please remember X"` | Store fact to memory |
| `"retrieve memory <key>"` | Fetch specific memory by key |

### System

| Phrase | Routes to |
|--------|-----------|
| `"docker ps"` · `"container status"` · `"what's running"` | Docker container list |
| `"show logs for X"` · `"logs for X"` | Container log tail |
| `"system metrics"` · `"gpu usage"` · `"ram usage"` | Metrics collection |

---

## Chunk Reassembly

Telegram splits messages longer than 4096 characters into sequential parts. The gateway buffers incoming chunks and waits 1.5 seconds after the last chunk before forwarding the assembled text to sovereign-core. A `⚠️ [Input arrived in N parts]` notice is prepended to the response when this happens.

This is relevant when pasting large CC runsheets or documents directly into the chat.

---

## BotFather Registration Status

Commands registered with BotFather (visible in the Telegram command menu):

| Command | Registered | Notes |
|---------|------------|-------|
| `/install` | ✓ | |
| `/skills` | ✓ | |
| `/selfimprove` | ✓ | |
| `/devcheck` | ✓ | |
| `/portfolio` | ✓ | Stub — harness not yet built |
| `/pm` | ✓ | Stub — harness PLANNED |
| `/do_tax` | Manual required | Director must register with BotFather |
| `/remember` | ✓ | Also: `/memorise`, `/memorize` |
| `/verify` | ✓ | |
