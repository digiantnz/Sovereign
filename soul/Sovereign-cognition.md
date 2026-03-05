# Sovereign Cognition Architecture — Typed Memory Collections

**Version**: 2.0
**Status**: Live (Phase 4)
**RAID path**: `/home/sovereign/docs/Sovereign-cognition.md`

---

## Overview

Sovereign uses 7 typed sovereign collections plus one ephemeral working memory and one legacy
collection, all backed by Qdrant vector store. Embeddings are produced by `nomic-embed-text`
via Ollama (768-dimensional cosine space). Phase 4 adds context-weighted retrieval, query type
classification, session-start prospective briefing, and automatic gap entry creation.

---

## Collections

### `working_memory` (ephemeral, NVMe)
- **Purpose**: Ephemeral session cache, in-progress reasoning
- **Persistence**: Wiped on every startup (`on_disk=False`)
- **Writers**: `sovereign-core`, `specialist`
- **Schema**: `content`, `timestamp`, `type` (for promotion routing), `input`
- **Promotion**: Eligible items auto-promoted on shutdown if `type ∈ SOVEREIGN_COLLECTIONS`

### `semantic` (RAID)
- **Purpose**: Durable facts, system knowledge, configuration truths
- **Writers**: `sovereign-core` only
- **Schema fields**: `content`, `timestamp`, `confidence`, `domain`, `source`

### `procedural` (RAID)
- **Purpose**: Repeatable workflows, multi-step processes
- **Writers**: `sovereign-core` only, **requires `human_confirmed=True`**
- **Schema fields**: `content`, `timestamp`, `triggers[]`, `frequency`, `preconditions[]`, `last_executed`

### `episodic` (RAID)
- **Purpose**: Timestamped experiences with outcomes and lessons
- **Writers**: `sovereign-core`, `specialist`
- **Schema fields**: `content`, `timestamp`, `outcome` (`positive`/`negative`/`neutral`), `learned`

### `prospective` (RAID)
- **Purpose**: Scheduled or conditional future tasks/intentions
- **Writers**: `sovereign-core`, `specialist`
- **Schema fields**: `content`, `timestamp`, `next_due`, `condition`, `status`

### `associative` (RAID)
- **Purpose**: Links/relationships between specific memory items
- **Writers**: `sovereign-core` only
- **Schema fields**: `content`, `timestamp`, `source_id`, `target_id`, `relationship_type`, `strength`

### `relational` (RAID)
- **Purpose**: Concept comparisons and contrasts
- **Writers**: `sovereign-core` only
- **Schema fields**: `content`, `timestamp`, `concept_a`, `concept_b`, `shared[]`, `diverges[]`, `insight`

### `meta` (RAID)
- **Purpose**: Knowledge-about-knowledge, domain maps, gap tracking
- **Writers**: `sovereign-core` only
- **Schema fields**: `content`, `timestamp`, `domain`, `confidence_level`, `gaps[]`, `source_quality`, `last_updated`
- **Special**: `gaps[]` arrays are extracted and surfaced in every API response; gap entries auto-created when confidence < 0.5

### `sovereign_memory` (RAID) — Legacy
- **Purpose**: Pre-Phase-4 general memory; retained for backward compatibility
- **Writers**: No new writes; existing items still retrieved via weighted search (weight=0.8 for action/session_start, 0.8 for knowledge)
- **Note**: Do not delete; do not write new items here

---

## Query Pipeline (Phase 4)

```
User input
    │
    ▼
[Session start check] — if no context_window and no pending_delegation
    │  get_due_prospective() → items with next_due <= today
    │  ceo_translate() → morning_briefing string (returned alongside main response)
    │
    ▼
classify_query_type(user_input)  ← deterministic keyword match
    │  returns: "action" | "knowledge" | "session_start"
    │
    ▼
CognitionEngine.load_memory_context(query, query_type)
    │  ┌── embed query once
    │  ├── parallel query_points × 8 collections (all except working_memory)
    │  ├── multiply each score by COLLECTION_WEIGHTS[query_type][collection]
    │  ├── merge + sort by weighted score descending
    │  ├── compute_confidence() → mean of top-3 weighted scores
    │  ├── if confidence < 0.5 → ensure_gap_entry(query) [auto-create meta gap]
    │  └── returns (context_str, confidence, gaps[])
    │
    ▼
confidence gate (ExecutionEngine.handle_chat)
    │  if 0 < confidence < 0.75 → requires_confidence_acknowledgement
    │  if confidence == 0.0    → proceed (no prior knowledge)
    │  if confidence >= 0.75   → proceed
    │
    ▼
CEO classification Pass 1 — with full 3-turn session history + pronoun resolution rule
    │
    ▼
[specialist + evaluation if action domain]
    │
    ▼
Execution Pass 4
    │
    ▼
Memory decision Pass 5
    │  CEO chooses collection + type + collection-specific metadata
    │  (outcome for episodic, next_due for prospective,
    │   concept_a/concept_b/shared/diverges/insight for relational,
    │   item_a_id/item_b_id/link_type for associative)
    │  extra_metadata extracted and passed to save_lesson()
    │
    ▼
Response includes: confidence, gaps[], morning_briefing (session start only)
```

---

## Query Type Weights (Phase 4)

Score multipliers applied per collection per query type before ranking:

| Collection       | `action` | `knowledge` | `session_start` |
|------------------|:--------:|:-----------:|:---------------:|
| `episodic`       | 1.4      | 0.9         | 0.9             |
| `procedural`     | 1.3      | 0.8         | 0.8             |
| `semantic`       | 1.0      | 1.4         | 1.1             |
| `meta`           | 0.9      | 1.3         | 1.1             |
| `associative`    | 0.8      | 0.8         | 0.7             |
| `prospective`    | 0.8      | 0.6         | 1.5             |
| `relational`     | 0.7      | 1.1         | 0.7             |
| `sovereign_memory`| 0.8     | 0.8         | 0.7             |

`classify_query_type()` keyword triggers:
- **`session_start`**: "good morning", "morning brief", "what's on today", "what do I have on", "briefing"
- **`action`**: "restart", "delete", "send", "write", "create", "move", "update", "rebuild", "deploy", "copy", "remove"
- **`knowledge`**: all other queries (default)

---

## Write Permission Matrix

| Collection     | sovereign-core | specialist | human_confirmed required |
|----------------|:--------------:|:----------:|:------------------------:|
| `semantic`     | ✓              |            |                          |
| `procedural`   | ✓              |            | ✓ (always)               |
| `episodic`     | ✓              | ✓          |                          |
| `prospective`  | ✓              | ✓          |                          |
| `associative`  | ✓              |            |                          |
| `relational`   | ✓              |            |                          |
| `meta`         | ✓              |            |                          |
| `working_memory` | ✓            | ✓          |                          |

---

## Confidence Handling

| Score range        | Behaviour |
|--------------------|-----------|
| `== 0.0`           | No prior knowledge — proceed normally |
| `0 < score < 0.75` | Return `requires_confidence_acknowledgement: true` gate |
| `>= 0.75`          | High confidence — proceed normally |

**Re-submit pattern** (client):
```json
{
  "input": "original question",
  "confidence_acknowledged": true,
  "pending_delegation": { "<...stashed delegation...>" }
}
```

CEO override is logged to audit JSONL and stored as an episodic memory.

---

## Startup Sequence

1. `setup()`: wipe + recreate `working_memory`; create absent sovereign collections
2. `startup_load()`: embed sentinel query → parallel query all 7 collections (limit=2,
   score_threshold=0.3) → copy vectors directly into `working_memory` tagged
   `startup_load=True`, `source_collection=<coll>`

---

## Shutdown Sequence

`shutdown_promote()` scrolls all `working_memory` items and promotes eligible ones:
- **Skip** if `startup_load=True` (already came from sovereign)
- **Skip** if `type` not in `SOVEREIGN_COLLECTIONS`
- **Skip** if `type == procedural` (requires human confirmation)
- All others: upsert to `payload["type"]` collection using existing vector, then audit log

---

## Audit Format

All sovereign writes, promotions, and CEO overrides are appended to:
`/home/sovereign/audit/memory-promotions.jsonl`

Each line is a JSON object:
```json
{
  "timestamp": "2026-03-03T12:00:00+00:00",
  "event_type": "store|promote|shutdown_promote|ceo_confidence_override",
  "collection": "semantic",
  "point_id": "uuid",
  "writer": "sovereign-core",
  "content_preview": "first 120 chars of content"
}
```

---

## Gap Surfacing & Auto-Creation (Phase 4)

`meta` collection items contain a `gaps[]` array. These are extracted from every
weighted search call and returned in **every** API response under the `gaps` key.
Gaps are never silently inferred — they are always explicit in the response payload.

**Auto-creation** (`ensure_gap_entry`): When `confidence < 0.5` after weighted search,
the system searches meta at `score_threshold=0.65`. If no similar gap entry exists,
a new gap entry is stored automatically (`type=gap`, content = original query).
Threshold configurable in `governance.json` at `cognition.gap_auto_create_threshold`.

---

## Session-Start Prospective Check (Phase 4)

Triggered at the top of `handle_chat()` when there is no existing `context_window`
(first message of a new session) and no `pending_delegation`.

1. `get_due_prospective()` scrolls the prospective collection, filters `next_due <= today`
2. If any items are due, `ceo_translate()` converts them to plain-English morning briefing
3. Briefing returned in `morning_briefing` field alongside the main response
4. Gateway sends briefing as a separate `[Morning briefing]` message before the director reply

Configurable via `governance.json` at `cognition.prospective_check_on_session_start`.

---

## Safety Invariants

- GovernanceEngine remains deterministic — no LLM calls, no collection awareness
- Specialists cannot write to `semantic`, `associative`, `relational`, `meta`
- Procedural writes always require `human_confirmed=True`
- Confidence gate blocks low-confidence execution without explicit acknowledgement
- All sovereign writes audited to JSONL (audit failure never crashes adapter)
- Old `sovereign_memory` collection left intact — not deleted, not used by code
