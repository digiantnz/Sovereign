"""Pydantic output schemas for Sovereign cognitive passes.

These are documentation + validation contracts, not call-path enforcement.
All schemas use extra='allow' so unknown LLM-emitted fields pass through
without raising — the goal is surfacing drift on *known* fields, not
rejecting valid responses that happen to include extra keys.

_parse_llm_output() validates against these after a successful parse and
logs any ValidationError to the audit ledger, but always returns the raw
dict.  The never-raises contract of _parse_llm_output() is preserved.

Field names match what the prompts and _dispatch_inner() actually expect:
  - Pass3a uses 'skill' (not 'skill_name') and flat payload fields (not nested 'payload')
  - Pass3b uses 'outcome' (not 'interpreted_result')
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Pass1Output(BaseModel):
    """PASS 1 — Orchestrator classification.

    Always local Ollama.  Result stamped with _routing_source='llm_pass1'
    by orchestrator_classify() after this schema is filled.
    """
    model_config = ConfigDict(extra="allow")

    intent: str
    delegate_to: str
    tier: Literal["LOW", "MID", "HIGH"]
    preferred_provider: str = "local"
    delegation_reason: str = ""
    expected_output_format: str = ""


class Pass2Output(BaseModel):
    """PASS 2 — Security agent evaluation.

    Called via call_llm_json() in security_evaluate(), not _parse_llm_output().
    Validated inline in security_evaluate() after the JSON parse.
    """
    model_config = ConfigDict(extra="allow")

    block: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    risk_categories: list[str]
    reasoning_summary: str
    required_mitigation: str = ""


class Pass3aOutput(BaseModel):
    """PASS 3a — Specialist outbound: execution plan.

    The LLM returns a flat dict.  'skill' and 'operation' are required;
    all other payload fields (query, target, path, account, uid, etc.) are
    dynamic and vary by skill — captured via extra='allow'.

    _dispatch_inner() reads these fields from the top level of the dict,
    not from a nested 'payload' key.
    """
    model_config = ConfigDict(extra="allow")

    skill: str
    operation: str
    mode: str = "outbound"


class Pass3bOutput(BaseModel):
    """PASS 3b — Specialist inbound: interpreted execution result.

    Always local Ollama.  Field is 'outcome', not 'interpreted_result'.
    """
    model_config = ConfigDict(extra="allow")

    success: bool
    outcome: str
    anomaly: Optional[str] = None
    retry_with: Optional[str] = None


class ResultForTranslator(BaseModel):
    """Nested structure inside Pass4Output.result_for_translator.

    Validated via Pydantic v2 coercion when Pass4Output.model_validate()
    is called on the raw dict — dict→model coercion is automatic.
    """
    model_config = ConfigDict(extra="allow")

    success: bool = True
    outcome: str = ""
    detail: dict = Field(default_factory=dict)
    error: Optional[str] = None
    next_action: Optional[str] = None


class Pass4Output(BaseModel):
    """PASS 4 — Orchestrator evaluation + memory decision.

    Always local Ollama.  result_for_translator is the only field passed
    to PASS 5 (translator); all other fields are for memory/governance only.
    """
    model_config = ConfigDict(extra="allow")

    result_for_translator: ResultForTranslator
    approved: bool = True
    memory_action: str = "none"
    memory_payload: Optional[dict] = None
    feedback: Optional[str] = None
