# Sovereign Cognitive Loop Rework — Design & Implementation Plan

**Status**: ALL STEPS COMPLETE — envelope, nanobot contract, untrusted tagging, tests validated
**Date**: 2026-03-17
**Author**: Rex / Matt (Sovereign AI project)

---

## Motivation

The prior cognitive loop (OC-S6) used a single `specialist_reason()` call that conflated:
- Planning what to do (outbound: skill + payload selection)
- Interpreting what happened (inbound: result analysis)

This meant the specialist had no way to correct a bad payload after seeing a real error, the
translator received raw adapter internals, and the orchestrator was called twice (evaluate +
memory decision) adding latency for no benefit.

---

## New Pass Structure

```
Director message
      │
  PASS 1  ─── Orchestrator (local Ollama, always deterministic)
              Classify intent → delegate_to, intent, tier
      │
  [Short-circuit for ollama/memory/browser/scheduler domains]
      │                └─ result_for_translator → PASS 5
      │
  PASS 2  ─── Security Agent (CONDITIONAL: HIGH tier only, or pre-LLM scanner flag)
              Evaluate risk → block or continue
      │
  PASS 3a ─── Specialist OUTBOUND (externally routable: Grok/Claude for research)
              Select skill + build complete execution payload
      │
  EXECUTION ─ _dispatch_inner() (deterministic, adapter calls, governance already applied)
              Calls adapter → returns raw protocol result verbatim
      │
  PASS 3b ─── Specialist INBOUND (always local Ollama)
              Interpret execution result → success, outcome, anomaly, retry_with
      │
  [Optional retry — one attempt if specialist sets retry_with]
      │
  PASS 4  ─── Orchestrator (local Ollama, merged evaluate + memory decision)
              Approve plan → memory_action → result_for_translator
      │
  [Async memory write — asyncio.create_task(), never blocks return path]
      │
  PASS 5  ─── Translator (local Ollama, restricted input)
              Receives ONLY result_for_translator — no raw adapter internals
              Outputs plain English director_message
```

---

## Design Decisions

### (1) routing: specialist_outbound only
`_routing_decision()` applies to PASS 3a (outbound) only. PASS 1/3b/4 are always local.
Research agent may call external LLMs for complex outbound reasoning.

### (2) All short-circuit paths through translator
Every return path exits through `translator_pass()`. No raw adapter output reaches Director.
Short-circuit paths build a `result_for_translator` struct before calling translator.

### (3) Sovereign-core ↔ nanobot protocol contract

**Request (sovereign-core → nanobot):**
```json
{
  "skill":      "skill-name",
  "operation":  "operation-name",
  "payload":    {},
  "request_id": "uuid",
  "timeout_ms": 25000
}
```
Existing fields (`action`, `params`, `context`) accepted for backward compat.

**Response (nanobot → sovereign-core):**
```json
{
  "request_id":  "uuid",
  "skill":       "skill-name",
  "operation":   "operation-name",
  "success":     true,
  "status_code": "HTTP 201 | IMAP OK | 404 | BAD | ...",
  "data":        {},
  "raw_error":   null
}
```
Nanobot is a dumb executor: fires the skill, returns protocol result verbatim.
No retry logic, no interpretation, no fabrication inside nanobot.

### (4) Nanobot results are untrusted external content by definition

Every result returned by nanobot-01 is stamped `_trust: "untrusted_external"` in `nanobot.py _forward()` unconditionally — before any other code sees it. Before `specialist_inbound` (PASS 3b) runs, the security scanner evaluates the result content. If the scanner flags anything, `_untrusted_flagged: True` is set on the result and an audit ledger entry is written. `specialist_inbound` prompts surface an explicit warning whenever the flag is set.

**Why this is a principle, not a convenience flag**: nanobot-01 executes skills against live external systems whose content sovereign-core does not control. A compromised IMAP server, a malicious RSS feed, or a tampered Nextcloud file could embed adversarial content in result data. Without screening, that content reaches a local Ollama LLM in `specialist_inbound` — a prompt injection vector delivered via data, not via the Director channel. The trust boundary is structural: nanobot-01 cannot be implicitly trusted simply because it runs on the same Docker network. It handles external data; sovereign-core does not.

The `result_for_translator` firewall (PASS 4 → PASS 5) provides a second layer: the translator only receives the orchestrator's curated summary, never raw adapter output. A future nanobot data source automatically inherits this model — no per-source opt-in required.

Full ADR: `docs/CLAUDE-ARCH.md` ADR-001.

### (5) Retry logic in the loop
`specialist_inbound()` may set `retry_with` (corrected payload dict).
Loop calls dispatch once more with merged payload → runs `specialist_inbound` on second result.
No further retries. Nanobot receives whatever payload it is given.

### (5) Timing
Per-pass timeout: `PASS_TIMEOUT_SECONDS` env var (default 30s).
Total timeout: `TOTAL_TIMEOUT_SECONDS` env var (default 120s).
Async memory dispatch eliminates memory write from the critical path.

---

## Files Changed

| File | Change |
|------|--------|
| `/home/sovereign/personas/orchestrator.md` | Dual role PASS 1 + PASS 4 output contracts; routing memory instructions |
| `/home/sovereign/personas/translator.md` | Restricted to `result_for_translator` only; tone from soul §3 |
| `/home/sovereign/personas/devops_agent.md` | Explicit outbound mode (skill+payload) and inbound mode (interpret result) |
| `/home/sovereign/personas/business_agent.md` | Same, with caldav/webdav/imap skill examples |
| `/home/sovereign/personas/research_agent.md` | Notes external LLM routing available in outbound mode |
| `/home/sovereign/personas/security_agent.md` | Clarifies PASS 2 conditional role (HIGH tier or scanner flag) |
| `/home/sovereign/personas/memory_agent.md` | Async dispatch only; never blocks return path |
| `core/app/cognition/engine.py` | `orchestrator_classify`, `specialist_outbound`, `specialist_inbound`, `orchestrator_evaluate`, `translator_pass` |
| `core/app/cognition/prompts.py` | New prompt builders for all new functions |
| `core/app/execution/engine.py` | `handle_chat()` rewritten with 5-pass structure, timeouts, async memory |
| `nanobot-01/server.py` | `/run` response normalised to contract format |
| `core/app/adapters/nanobot.py` | `_forward()` updated to read `success`/`status_code`/`raw_error`; stamps `_trust: "untrusted_external"` on all nanobot results |
| `core/app/cognition/message.py` | NEW — `InternalMessage` universal envelope (Envelope + MessageContext + history + translator/nanobot slices) |

---

## Governance Invariants (unchanged)

- Tier always derived deterministically from `INTENT_TIER_MAP` — never from LLM
- `governance.validate()` runs before every dispatch — raises ValueError on failure
- `execution_confirmed` stamped from actual HTTP status — LLM cannot assert completion
- Prospective memory gate: mutating intents require HTTP 2xx before `execution_confirmed=True`
- DCL hard-block: PRIVATE/SECRET content → force_local regardless of explicit override

---

## Test Sequence (Step 5)

1. **LOW tier** — "what containers are running"
   - Expect: full pass sequence completes, translator produces plain English
2. **MID tier** — "move the Beatport email to archive"
   - Expect: confirmation gate fires; on confirm: correct IMAP payload, nanobot returns real status, inbound interprets, translator plain English
3. **Error path** — operation that will fail (invalid UID)
   - Expect: nanobot returns raw error, inbound catches it, translator reports failure — no fabrication
4. **Short-circuit** — "what is the weather in Christchurch"
   - Expect: search short-circuit with translator pass before Director
5. **Security pass** — HIGH tier action
   - Expect: PASS 2 security agent fires, result logged to audit ledger
