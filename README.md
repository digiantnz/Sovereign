# Sovereign AI

A self-hosted personal AI agent system with a Telegram interface, local GPU cognition, strong governance, and a community skill system.

---

## What it is

Sovereign is a personal AI control plane built to be:

- **Agentic** — a 5-pass cognitive loop that classifies intent, routes to specialists, executes actions, evaluates results, and translates everything back into plain English
- **Governed** — every action is validated against a tiered governance policy (LOW / MID / HIGH) before execution; destructive actions require explicit confirmation or double-confirmation
- **Local-first** — reasoning runs on a local GPU (Ollama / llama3.1:8b); no prompt data leaves the machine unless explicitly escalated to Claude or Grok
- **Extensible** — skills are installed at runtime from community SKILL.md definitions, reviewed by a security pipeline, and executed by a sidecar nanobot

The Director (Matt) interacts via Telegram. Sovereign handles the rest.

---

## Architecture

```
Telegram
   │
Gateway (Python)
   │
Sovereign-Core (FastAPI)
   │
   ├── PASS 1: Orchestrator classify      → intent, tier, delegate_to
   ├── PASS 2: Specialist outbound        → skill + payload planning
   ├── EXEC:   _dispatch_inner()          → deterministic adapter call
   ├── PASS 3: Specialist inbound         → interpret result
   ├── PASS 4: Orchestrator evaluate      → memory action + result_for_translator
   └── PASS 5: Translator                 → plain English director message
        │
        ├── Ollama (local GPU — llama3.1:8b)
        ├── Docker Broker (docker.sock boundary)
        ├── Nanobot-01 (skill execution sidecar)
        ├── a2a-browser (web search + fetch)
        └── Qdrant (vector memory)
```

### Cognitive loop

All local reasoning runs through Ollama. The specialist outbound pass is the only externally-routable pass — Claude or Grok can be used for high-complexity planning (complexity scored before routing). All other passes stay local.

A deterministic pre-classifier (`_quick_classify`) handles common intents via keyword matching before any LLM call, preventing small-model misrouting.

### Governance tiers

| Tier | Confirmation | Examples |
|------|-------------|---------|
| LOW  | None | docker ps/logs/stats, read email, read files, web search |
| MID  | `requires_confirmation` | send email, write files, calendar events, docker restart |
| HIGH | `requires_double_confirmation` | delete files, docker rebuild/prune |

Policy lives at `/home/sovereign/governance/governance.json` on RAID — mounted read-only into the container. Never baked into the image.

---

## Stack

| Component | Technology |
|-----------|-----------|
| Cognition | Ollama / llama3.1:8b-instruct-q4_K_M (local GPU) |
| API escalation | Anthropic Claude, Grok |
| Execution sidecar | Python FastAPI (nanobot-01) |
| Web search | SearXNG + DDG via a2a-browser |
| Vector memory | Qdrant |
| Message broker | Docker Broker (docker.sock isolation) |
| Telegram | python-telegram-bot |
| Protocol | A2A JSON-RPC 3.0 (sovereign_a2a package) |

---

## Skills

Skills are installed at runtime. Each skill is a `SKILL.md` file defining operations, parameters, and execution metadata. The skill lifecycle is:

1. **Search** — find candidates via GitHub search (SearXNG)
2. **Review** — security pipeline scans the SKILL.md; escalates to Director for flagged content
3. **Load** — MID-tier confirmation required; written to RAID; checksum registered

Installed skills: `imap-smtp-email`, `openclaw-nextcloud`, `rss-digest`, `deep-research`, `security-audit`, `session-wrap-up`, `memory-curate`, `sovereign-browser`, `weather`

---

## Memory

Qdrant-backed vector store with 8 collections:

`semantic` · `episodic` · `prospective` · `procedural` · `relational` · `associative` · `working_memory` · `meta`

Prospective memory drives the task scheduler — recurring tasks (daily briefing, etc.) are stored as `PROSPECTIVE` entries with a cron schedule and executed as multi-step procedures.

---

## Storage

- **RAID5 (`/home/sovereign/`)** — durable truth: governance, personas, skills, memory, audit logs, keys
- **NVMe (`/docker/sovereign/`)** — runtime; symlinked to RAID source

All config edits go to RAID first. Containers are bounced after.

---

## Security model

- `docker.sock` is held exclusively by the broker container — sovereign-core has no direct Docker access
- Credentials are issued as single-use tokens (60s TTL) via CredentialProxy and injected into nanobot subprocess env vars
- All nanobot results are tagged `_trust: "untrusted_external"` and scanned before the specialist inbound pass
- Ed25519 signing on wallet operations; `/verify` Telegram command for anti-spoofing checks
- Soul Guardian validates persona integrity on every startup

---

## Repository layout

```
core/          sovereign-core FastAPI app (cognition, execution, governance, memory, skills)
gateway/       Telegram bot gateway
nanobot-01/    skill execution sidecar
broker/        docker.sock isolation broker (Node.js)
wallet/        Safe multisig transaction service proxy
a2a-browser/   web search + fetch service (also deployed externally on node04)
nginx/         reverse proxy config
scripts/       operator scripts
docs/          architecture and design documents
```

---

*Personal project — not accepting contributions.*
