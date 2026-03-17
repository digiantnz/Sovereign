# Sovereign AI — Architectural Decision Records

Decisions that look arbitrary without context. Read before modifying the cognitive loop, adapter layer, or trust model.

**Cross-references**:
- Full cognitive loop design: `docs/Sovereign-CognitiveLoopRework.md`
- System architecture: `docs/Sovereign-v2.md`
- Signed history: `/home/sovereign/docs/as-built.md` (RAID)
- GitHub: `digiantnz/Sovereign` — commit history is the authoritative record of what changed and why

---

## ADR-001: Every nanobot result is untrusted external content

**Date**: 2026-03-17
**Status**: Active
**Applies to**: `core/app/adapters/nanobot.py`, `core/app/execution/engine.py`, `core/app/cognition/prompts.py`

### Decision

All data returned by nanobot-01 is stamped `_trust: "untrusted_external"` unconditionally in `_forward()`, before the result is used anywhere in sovereign-core. Before `specialist_inbound` (PASS 3b) sees the result, the security scanner runs on the result content. If the scanner flags anything, the result is annotated `_untrusted_flagged: True` and logged to the audit ledger. `specialist_inbound` prompts surface an explicit trust warning when the flag is set or when `_trust == "untrusted_external"`.

### Why

nanobot-01 executes skills against live external systems: IMAP servers, Nextcloud, RSS feeds, arbitrary scripts. The content those systems return is not under sovereign-core's control. A compromised IMAP server, a malicious feed, or a tampered Nextcloud file could inject adversarial content into result data. If that content reaches `specialist_inbound` (a local Ollama LLM) without any screening, it could manipulate the inbound interpretation — effectively a prompt injection attack delivered via a third-party data source rather than through the Director channel.

The trust boundary is structural, not probabilistic. nanobot-01 cannot be granted implicit trust simply because it is a sidecar on the same Docker network. It executes code and handles external data; sovereign-core does not control what those external systems return.

### Consequences

- Every nanobot result goes through the scanner before PASS 3b, adding a small latency cost
- `specialist_inbound` is explicitly warned when content was flagged — it can factor this into its interpretation
- The `result_for_translator` firewall (PASS 4 → PASS 5) provides a second layer: translator only receives the orchestrator's curated summary, never raw adapter output
- Adding a new data source that goes through nanobot-01 automatically inherits this trust model — no per-source opt-in required

### What would break this

- Stripping the `_trust` stamp in `_forward()` before returning
- Bypassing the scan block in `handle_chat()` when `_trust == "untrusted_external"`
- Passing raw nanobot result directly to translator (bypassing PASS 4 curation)
- Granting nanobot-01 a "trusted" designation in the adapter layer

---

## ADR-002: PASS 1, PASS 4, PASS 5 are always local — never externally routable

**Date**: 2026-03-17
**Status**: Active
**Applies to**: `core/app/cognition/engine.py` — `_routing_decision()`

### Decision

Orchestrator classify (PASS 1), orchestrator evaluate (PASS 4), and translator (PASS 5) are hardcoded to local Ollama. Only PASS 2 (specialist outbound) may route to an external LLM. `_routing_decision()` is called exclusively from specialist paths. There is no routing logic in the orchestrator or translator code paths.

### Why

PASS 1 classifies intent and derives tier. PASS 4 evaluates the result and decides whether to write memory. PASS 5 produces the Director-facing message. These three passes are the governance and output layer of the loop. Routing them externally would mean:

- Tier classification and memory decisions could be influenced by an external LLM provider with different alignment, different context, or different availability guarantees
- The Director-facing message could be composed by a third party — not sovereign-core's voice
- A network outage or API rate-limit at an external provider would break governance, not just skill execution

PASS 2 (specialist outbound) is externally routable because it handles research and skill planning — tasks where reasoning quality benefits from a larger model and where the output (a skill selection + payload) is validated by governance before execution. An external model cannot bypass governance by returning a dangerous payload; validation still happens in sovereign-core.

### Consequences

- External LLM routing improves reasoning quality on complex skill tasks without touching governance or output integrity
- If all external providers are unavailable, the loop degrades gracefully — specialist falls back to local Ollama, other passes are unaffected
- Complexity scoring in `_routing_decision()` operates on `user_input` (Director's message), not the full specialist prompt — persona length must not inflate every score

---

## ADR-003: Director raw input is hashed at PASS 1 and never stored or forwarded

**Date**: 2026-03-17
**Status**: Active
**Applies to**: `core/app/cognition/message.py` — `InternalMessage.create()`

### Decision

When `InternalMessage` is constructed at the start of `handle_chat()`, the Director's raw input string is hashed (SHA-256) immediately. Only `director_input_hash` is stored in `MessageContext`. The raw string is held in local scope for PASS 1 only and is not placed in the envelope, history, or any slice that crosses an agent boundary.

### Why

The Director's raw message may contain sensitive information: credentials pasted accidentally, personal names, private account details, contract terms. Storing it in the envelope would mean it travels across every agent boundary in the loop — including to specialist prompts that may be routed to external LLMs. The hash preserves auditability (two runs with the same input produce the same hash) without exposing the content.

This is consistent with the broader principle that sovereign-core intermediates between the Director and all execution environments — the Director's words should not reach nanobot, broker, Nextcloud, or external LLM APIs verbatim.

### Consequences

- `nanobot_request_slice()` contains `payload` (specialist-constructed) but never the Director's original message
- `translator_slice()` contains only `result_for_translator` (orchestrator-curated) — not the Director's message and not raw adapter output
- Audit trail integrity is maintained via hash without raw content storage
- `append_pass()` hashes the payload at each pass boundary for the same reason — history is a sequence of hashes, not a copy of intermediate LLM outputs

---

## ADR-004: Governance tier is derived deterministically — LLM cannot assert or override it

**Date**: 2026-03-17
**Status**: Active
**Applies to**: `core/app/governance/engine.py`, `core/app/execution/engine.py`

### Decision

Intent tier (LOW/MID/HIGH) is always derived from `INTENT_TIER_MAP` (static dict) or `governance.json intent_tiers` (policy file). No LLM call is made inside `GovernanceEngine`. The execution engine calls `gov.validate()` before every dispatch; this raises `ValueError` on failure and cannot be suppressed by specialist output. Specialists include tier context in their prompts for awareness, but their output cannot change the tier that governance enforces.

### Why

If tier could be asserted by an LLM, any adversarial input that manipulates the specialist could escalate a LOW action to HIGH, bypassing confirmation gates. Governance must be a hard mechanical boundary, not a soft LLM-mediated one. A deterministic policy file on RAID (mounted read-only) is authoritative; an LLM reasoning about what tier seems appropriate is not.

### Consequences

- Adding a new intent requires a corresponding entry in `INTENT_TIER_MAP` or `governance.json intent_tiers` — the system will not infer it
- `GovernanceEngine.get_intent_tier(intent)` is the single source of truth for skills-domain intents
- `governance.json` is on RAID, never baked into the image — changes require Director confirmation and trigger config change notification
