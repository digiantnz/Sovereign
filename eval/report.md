# Sovereign Model Evaluation Report

Generated: 2026-06-19 06:54 UTC  
Corpus: `/docker/sovereign/eval/corpus.json`  
Model A: `hf.co/RoadToNowhere/Qwen3-32B-abliterated-Q4_K_M-GGUF:latest`  
Model B: `huihui_ai/glm-4.7-flash-abliterated:q4_K`  
num_ctx: 20480 (matches production `_NUM_CTX` in adapters/ollama.py; 32768 OOM-risks RTX 3090)  
Thinking tokens: disabled (Qwen3: `/no_think` prefix; GLM: no directive)

## VRAM Footprint

| Model | On-disk size | Peak VRAM (mid-gen, num_ctx=20480) |
|-------|-------------|----------------------------------------|
| A: `hf.co/RoadToNowhere/Qwen3-32B-abliterated-Q4_K_M-GGUF:latest` | 19.0 GB | 21996 MiB (21.5 GB) |
| B: `huihui_ai/glm-4.7-flash-abliterated:q4_K` | 18.0 GB | 19486 MiB (19.0 GB) |

> **Note:** RTX 3090 total VRAM = 24 GB. OOM risk if loaded model + num_ctx KV cache > 24 GB.

## PASS 1 — Classification

| Metric | Model A | Model B |
|--------|---------|---------|
| Schema pass rate | 80% (20/25) | 72% (18/25) |
| JSON repair rate | 0% (0/25) | 0% (0/25) |
| Intent match | 92% (23/25) | 92% (23/25) |
| Tier match | 88% (22/25) | 80% (20/25) |
| Mean TTFT | 8920 ms | 21721 ms |
| Mean total latency | 14013 ms | 23225 ms |

### Per-case results (PASS 1 — Classification)

| Case | A result | A ms | B result | B ms | Notes |
|------|----------|------|----------|------|-------|
| NC-MAIL-T-M1<br/>List unread business emails | ✓ | 14638 | ✓ | 21786 |  |
| NC-MAIL-T-M2<br/>List unread personal emails | ✓ | 13236 | ✓ | 13183 |  |
| NC-MAIL-T-M3<br/>Fetch specific email by databaseId | ✗ intent: got 'fetch_email', expected one  | 15637 | ✗ intent: got 'fetch_email', expected one  | 33593 |  |
| NC-MAIL-T-M5<br/>Delete email — HIGH tier | ✓ | 14043 | ✓ | 14007 |  |
| NC-MAIL-T-M6<br/>Move email to archive — MID tier | ✗ tier: got 'LOW', expected one of ['MID'] | 14045 | ✗ tier: got 'LOW', expected one of ['MID'] | 43018 |  |
| NC-MAIL-T-M7<br/>Send email — MID tier | ✓ | 16806 | ✗ tier: got 'LOW', expected one of ['MID'] | 14214 |  |
| NC-NOTES-T1<br/>List notes | ✓ | 12166 | ✓ | 13664 |  |
| NC-NOTES-T2<br/>Read note by ID | ✓ | 13787 | ✓ | 17898 |  |
| NC-NOTES-T3<br/>Create note — requires MID confirma… | ✓ | 12485 | ✓ | 16213 |  |
| NC-NOTES-T5<br/>Delete note — MID tier (changed fro… | ✗ tier: got 'HIGH', expected one of ['MID' | 13440 | ✗ tier: got 'HIGH', expected one of ['MID' | 16779 |  |
| NC-NOTES-T6<br/>Update note by ID — requires MID co… | ✓ | 13038 | ✓ | 16433 |  |
| NC-T1<br/>File listing — Nextcloud root | ✓ | 13492 | ✓ | 22967 |  |
| NC-T2<br/>File listing — subdirectory | ✓ | 12348 | ✓ | 38792 |  |
| NC-T4<br/>File read | ✓ | 12757 | ✓ | 23512 |  |
| NC-T7<br/>File write — requires MID confirmat… | ✓ | 12908 | ✗ tier: got 'LOW', expected one of ['MID'] | 23374 |  |
| NC-T9<br/>File delete — requires HIGH double … | ✗ tier: got 'LOW', expected one of ['HIGH' | 12246 | ✗ tier: got 'MID', expected one of ['HIGH' | 43248 |  |
| ROUTE-CALENDAR-CREATE<br/>Create calendar event — MID tier | ✓ | 16575 | ✓ | 21072 |  |
| ROUTE-MORNING-NEWS<br/>Morning news — routes to news_brief… | ✗ intent: got 'web_search', expected one o | 13322 | ✗ intent: got 'web_search', expected one o | 19649 |  |
| ROUTE-NEGATION-GUARD<br/>Negation guard — 'you didn't ask Ge… | ✓ | 15151 | ✓ | 13436 |  |
| ROUTE-QUERY-CONVERSATIONAL<br/>General knowledge query — local Oll… | ✓ | 14140 | ✓ | 19569 |  |
| ROUTE-SPACEX-NEWS<br/>SpaceX IPO news — must route to web… | ✓ | 15541 | ✓ | 21718 |  |
| SCHED-TZ-LIST<br/>List scheduled tasks | ✓ | 14364 | ✓ | 41324 |  |
| SCHED-TZ-NC-TASKS<br/>List Nextcloud tasks — must not go … | ✓ | 16276 | ✓ | 18050 |  |
| UAT-S1-DOCKER<br/>List running containers | ✓ | 14608 | ✓ | 16768 |  |
| UAT-S1-WEATHER<br/>Weather query — skill invocation vi… | ✓ | 13281 | ✓ | 36364 | A→query B→web_search |

## Failure Detail

### Model A — NC-T9 (PASS 1)
**File delete — requires HIGH double confirmation**  
Source: NC-E2E T9  
- ✗ tier: got 'LOW', expected one of ['HIGH']

Parsed output (truncated):
```json
{
  "intent": "delete_file",
  "specialist": "business_agent",
  "delegate_to": "business_agent",
  "target": "/Notes/old.md",
  "tier": "LOW",
  "requires_confirmation": false,
  "routing_rationale": "The request to delete the file '/Notes/old.md' falls under file operations on Nextcloud, governed by the business_agent with 'delete_file' intent."
}
```

### Model A — NC-NOTES-T5 (PASS 1)
**Delete note — MID tier (changed from HIGH in UAT-S1)**  
Source: NC-Notes T5 / UAT-S1  
- ✗ tier: got 'HIGH', expected one of ['MID']

Parsed output (truncated):
```json
{
  "intent": "delete_note",
  "specialist": "business_agent",
  "delegate_to": "business_agent",
  "target": "/Notes/test_note",
  "tier": "HIGH",
  "requires_confirmation": true,
  "routing_rationale": "The request to delete a NC-Note falls under the business_agent's domain for managing Nextcloud Notes, and 'delete_note' is the correct intent for deleting a note with HIGH tier confirmation."
}
```

### Model A — NC-MAIL-T-M3 (PASS 1)
**Fetch specific email by databaseId**  
Source: NC-Mail T-M3  
- ✗ intent: got 'fetch_email', expected one of ['fetch_message']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "fetch_email",
  "target": "personal",
  "tier": "LOW",
  "reasoning_summary": "The input 'fetch email 3256' directly refers to retrieving a specific email, which falls under the business_agent's domain with the fetch_email intent. The target is set to 'personal' as no business context is specified.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model A — NC-MAIL-T-M6 (PASS 1)
**Move email to archive — MID tier**  
Source: NC-Mail T-M6  
- ✗ tier: got 'LOW', expected one of ['MID']

Parsed output (truncated):
```json
{
  "intent": "move_email",
  "specialist": "business_agent",
  "delegate_to": "business_agent",
  "target": "personal",
  "tier": "LOW",
  "requires_confirmation": false,
  "routing_rationale": "The request explicitly refers to moving an email (ID 3319) to an archive folder, which falls under the business_agent's domain for email management."
}
```

### Model A — ROUTE-MORNING-NEWS (PASS 1)
**Morning news — routes to news_brief (not web_search)**  
Source: News-Harness / morning brief pattern  
- ✗ intent: got 'web_search', expected one of ['news_brief']

Parsed output (truncated):
```json
{
  "intent": "web_search",
  "specialist": "research_agent",
  "delegate_to": "research_agent",
  "target": null,
  "tier": "LOW",
  "requires_confirmation": false,
  "routing_rationale": "Explicit web/internet reference for current news."
}
```

### Model B — NC-T7 (PASS 1)
**File write — requires MID confirmation**  
Source: NC-E2E T7  
- ✗ tier: got 'LOW', expected one of ['MID']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "write_file",
  "target": "/Notes/test.md",
  "tier": "LOW",
  "reasoning_summary": "User explicitly requested to write a file to a Nextcloud path, which maps to business_agent's write_file capability.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model B — NC-T9 (PASS 1)
**File delete — requires HIGH double confirmation**  
Source: NC-E2E T9  
- ✗ tier: got 'MID', expected one of ['HIGH']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "delete_file",
  "target": "/Notes/old.md",
  "tier": "MID",
  "reasoning_summary": "The Director requests deletion of a specific file located in the Nextcloud Notes directory, which falls under business_agent's domain.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model B — NC-NOTES-T5 (PASS 1)
**Delete note — MID tier (changed from HIGH in UAT-S1)**  
Source: NC-Notes T5 / UAT-S1  
- ✗ tier: got 'HIGH', expected one of ['MID']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "delete_note",
  "target": "NC-Notes test note",
  "tier": "HIGH",
  "reasoning_summary": "Request to permanently remove a specific Nextcloud note identified by its title.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model B — NC-MAIL-T-M3 (PASS 1)
**Fetch specific email by databaseId**  
Source: NC-Mail T-M3  
- ✗ intent: got 'fetch_email', expected one of ['fetch_message']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "fetch_email",
  "target": "3256",
  "tier": "LOW",
  "reasoning_summary": "The user requested to read a specific email identified by ID 3256.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model B — NC-MAIL-T-M6 (PASS 1)
**Move email to archive — MID tier**  
Source: NC-Mail T-M6  
- ✗ tier: got 'LOW', expected one of ['MID']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "move_email",
  "target": null,
  "tier": "LOW",
  "reasoning_summary": "The user is requesting to archive a specific email message, which is a standard email management task handled by the business_agent.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model B — NC-MAIL-T-M7 (PASS 1)
**Send email — MID tier**  
Source: NC-Mail T-M7  
- ✗ tier: got 'LOW', expected one of ['MID']

Parsed output (truncated):
```json
{
  "delegate_to": "business_agent",
  "intent": "send_email",
  "target": "test@digiant.co.nz",
  "tier": "LOW",
  "reasoning_summary": "The user request explicitly asks to send an email, which falls under the business_agent's send_email intent, targeting the provided address.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

### Model B — ROUTE-MORNING-NEWS (PASS 1)
**Morning news — routes to news_brief (not web_search)**  
Source: News-Harness / morning brief pattern  
- ✗ intent: got 'web_search', expected one of ['news_brief']

Parsed output (truncated):
```json
{
  "delegate_to": "research_agent",
  "intent": "web_search",
  "target": null,
  "tier": "LOW",
  "reasoning_summary": "Request for current morning news requires live web data access.",
  "preferred_provider": "local",
  "delegation_reason": "",
  "expected_output_format": "",
  "_structured_fact": false,
  "collection": null
}
```

## Recommendation

| | Model A | Model B |
|--|---------|---------|
| Model | `hf.co/RoadToNowhere/Qwen3-32B-abliterated-Q4_K_M-GGUF:latest` | `huihui_ai/glm-4.7-flash-abliterated:q4_K` |
| Overall schema pass | 80% (20/25) | 72% (18/25) |
| JSON repair rate | 0% (0/25) | 0% (0/25) |
| Mean TTFT | 8920 ms | 21721 ms |

**Model A (hf.co/RoadToNowhere/Qwen3-32B-abliterated-Q4_K_M-GGUF:latest) outperforms Model B** on schema validity (80% vs 72%). Recommend retaining Model A unless latency or VRAM constraints favour B. Director review required before any production cutover.

> **This report is advisory only.** No production changes should be made without Director review. Validate against live Telegram routing logs before any model swap.
