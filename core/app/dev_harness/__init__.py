"""
dev_harness — Developer Harness subsystem for Sovereign.

Four-phase code quality and boundary-compliance pipeline:
  Phase 1 — Analyse   (deterministic: pylint, semgrep, boundary scanner, GitHub)
  Phase 2 — Classify  (LLM advisory: Ollama → Claude escalation)
  Phase 3 — Plan      (surfaces to Director: prospective memory + Telegram)
  Phase 4 — Execute   (Director approval only: CC runsheet)

LLM/deterministic boundary invariant:
  - analyser.py, boundary_scanner.py, github_client.py: no LLM calls.
  - classifier.py: LLM calls only; no gating decisions.
  - Gate decisions are made by analyser.gate() only — never by LLM output.

Ref: /home/sovereign/docs/dev-harness-assessment.md
"""
