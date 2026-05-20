# Sovereign Research Capability

> **This document describes the pre-2026-05-13 inline implementation.**
> The research capability has been rebuilt as a proper harness.
> **Current reference:** `/home/sovereign/docs/research-harness.md`

---

## Summary of Change (2026-05-13)

The old inline `deep-research` approach was replaced by `monitoring/research_harness.py`. Key differences:

| Old (inline engine.py) | New (research_harness.py) |
|------------------------|--------------------------|
| `_deep_research_kw` flag → `_bypass_browser_sc` | `_research_gather_kw` → short-circuit `research` domain |
| Research goes through full cognitive loop (PASS 2/3a/3b) | Harness handles fetching + synthesis directly |
| `full_report` extraction block in PASS 4 | Checkpoint in working_memory after gather |
| `_confirmed_research_save` continuation handler | `research_save` intent → `run_research_save()` |
| `_build_finance_url()` static method on ExecutionEngine | `_build_finance_url()` inside research_harness.py |
| `research_report` intent → `notes_create` | `research_save` intent → `run_research_save()` |

---

## Current Architecture

See `/home/sovereign/docs/research-harness.md` for the full as-built reference.

**Intents:**
- `research_gather` (LOW) — fetch + synthesise; returns `requires_confirmation: True`
- `research_save` (MID) — write Nextcloud Note from checkpoint; Director confirms
- `research_clear` (LOW) — clear WM checkpoint

**Entry point:** `monitoring/research_harness.py::run_research_gather(cog, nanobot, qdrant, topic, user_input)`

---

## Historical Context (pre-2026-05-13)

The original research implementation routed through the full 5-pass cognitive loop:

```
Director message
  → _quick_classify / PASS 1 → intent: web_search, delegate_to: research_agent
  → PASS 2/3a: research_agent specialist (deep-research SKILL.md injected)
      → emits domain_scope: general|securities|commodities
      → emits search_query, topic, research_plan
  → EXEC: _dispatch_inner domain="browser"
      → nanobot.run("sovereign-browser", "search", {query, locale})
      → a2a-browser /search → SearXNG results + sovereign_synthesis
      → [securities/commodities only] _build_finance_url() → browser fetch Yahoo Finance
  → PASS 3b: research_agent synthesises across all sources
  → PASS 4: engine extracts full_report → proposes Nextcloud Notes save
  → PASS 5: translator → telegram_summary bullets to Director
```

This approach had two fundamental problems:
1. Browser domain was in the short-circuit list → PASS 3a specialist never ran → no clean synthesis
2. qwen2.5:32b (CEO LLM) did not reliably classify natural research requests as `research` intent

Both problems are resolved by the harness architecture.
