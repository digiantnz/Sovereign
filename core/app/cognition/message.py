"""InternalMessage — universal agent-to-agent message envelope.

Every agent-to-agent call in the cognitive loop uses this envelope.
No raw dicts are passed between agents.

Structure:
  envelope  — routing metadata (set at construction, updated per pass)
  context   — preserved context carried through all passes without modification
  payload   — pass-specific content written by each agent (previous payload → history)
  history   — append-only audit trail (output_hash only — no raw content)
  result    — null on outbound passes; populated by loop after nanobot returns

Boundary exceptions:
  Nanobot receives: {request_id, skill, operation, payload, timeout_ms}
  Nanobot returns:  {request_id, skill, operation, success, status_code, data, raw_error}
  Translator receives: result_for_translator object from orchestrator PASS 4 only.
  These slices are extracted from the envelope — the envelope itself never crosses the boundary.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── History entry ────────────────────────────────────────────────────────────

@dataclass
class PassRecord:
    pass_num:    int
    from_agent:  str
    timestamp:   str        # ISO 8601
    output_hash: str        # SHA-256 of payload at this pass (hex[:16])
    duration_ms: float
    success:     bool

    def to_dict(self) -> dict:
        return {
            "pass_num":    self.pass_num,
            "from_agent":  self.from_agent,
            "timestamp":   self.timestamp,
            "output_hash": self.output_hash,
            "duration_ms": round(self.duration_ms, 1),
            "success":     self.success,
        }


# ── Envelope ─────────────────────────────────────────────────────────────────

@dataclass
class Envelope:
    message_id:  str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id:  str = ""
    timestamp:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    pass_num:    int = 0
    mode:        str = "outbound"     # outbound | inbound
    from_agent:  str = "sovereign-core"
    to_agent:    str = ""
    tier:        str = "LOW"
    timeout_ms:  int = 30000

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "timestamp":  self.timestamp,
            "pass_num":   self.pass_num,
            "mode":       self.mode,
            "from":       self.from_agent,
            "to":         self.to_agent,
            "tier":       self.tier,
            "timeout_ms": self.timeout_ms,
        }


# ── Context ──────────────────────────────────────────────────────────────────

@dataclass
class MessageContext:
    """Preserved through all passes. Set at PASS 1, never overwritten."""
    original_intent:      str = ""    # set by orchestrator PASS 1; never changed
    director_input_hash:  str = ""    # SHA-256 of raw Director input; raw never travels past PASS 1
    routing_rationale:    str = ""    # set by orchestrator PASS 1
    security_clearance:   str = ""    # "cleared" | "conditional" | "blocked" — set by PASS 2
    skill:                str = ""    # null until specialist_outbound sets it
    operation:            str = ""    # null until specialist_outbound sets it

    def to_dict(self) -> dict:
        return {
            "original_intent":     self.original_intent,
            "director_input_hash": self.director_input_hash,
            "routing_rationale":   self.routing_rationale,
            "security_clearance":  self.security_clearance,
            "skill":               self.skill,
            "operation":           self.operation,
        }


# ── InternalMessage ──────────────────────────────────────────────────────────

@dataclass
class InternalMessage:
    """Universal agent-to-agent message envelope for the cognitive loop.

    Construction: InternalMessage.create(director_input, session_id, tier)
    Passing between agents: msg.for_pass(pass_num, from_agent, to_agent, mode)
    Appending history: msg.append_pass(pass_num, from_agent, duration_ms, success)
    Nanobot slice: msg.nanobot_request_slice()
    Translator slice: msg.translator_slice()
    """
    envelope: Envelope = field(default_factory=Envelope)
    context:  MessageContext = field(default_factory=MessageContext)
    payload:  dict[str, Any] = field(default_factory=dict)
    history:  list[PassRecord] = field(default_factory=list)
    result:   dict[str, Any] | None = None

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        director_input: str,
        session_id: str = "",
        tier: str = "LOW",
        request_id: str = "",
    ) -> "InternalMessage":
        """Create a new InternalMessage from a Director request.

        director_input is hashed — raw text never stored in the envelope.
        """
        req_id = request_id or str(uuid.uuid4())[:8]
        input_hash = hashlib.sha256(director_input.encode()).hexdigest()
        env = Envelope(
            request_id=req_id,
            session_id=session_id,
            tier=tier,
        )
        ctx = MessageContext(
            director_input_hash=input_hash,
        )
        return cls(envelope=env, context=ctx)

    # ── Pass boundary helpers ─────────────────────────────────────────────────

    def for_pass(
        self,
        pass_num: int,
        from_agent: str,
        to_agent: str,
        mode: str = "outbound",
    ) -> "InternalMessage":
        """Return a copy of this message updated for the next pass.

        payload is preserved; history is not modified here (call append_pass separately).
        """
        new_env = Envelope(
            message_id=self.envelope.message_id,
            request_id=self.envelope.request_id,
            session_id=self.envelope.session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pass_num=pass_num,
            mode=mode,
            from_agent=from_agent,
            to_agent=to_agent,
            tier=self.envelope.tier,
            timeout_ms=self.envelope.timeout_ms,
        )
        return InternalMessage(
            envelope=new_env,
            context=self.context,      # preserved unchanged
            payload=dict(self.payload),
            history=list(self.history),
            result=self.result,
        )

    def append_pass(
        self,
        pass_num: int,
        from_agent: str,
        duration_ms: float,
        success: bool,
    ) -> "InternalMessage":
        """Append a pass record to history. History is append-only.

        Hashes the current payload — never stores raw content.
        Returns self (mutates in place for convenience).
        """
        payload_json = json.dumps(self.payload, sort_keys=True, default=str)
        output_hash = hashlib.sha256(payload_json.encode()).hexdigest()[:16]
        record = PassRecord(
            pass_num=pass_num,
            from_agent=from_agent,
            timestamp=datetime.now(timezone.utc).isoformat(),
            output_hash=output_hash,
            duration_ms=duration_ms,
            success=success,
        )
        self.history.append(record)
        return self

    def set_payload(self, new_payload: dict) -> "InternalMessage":
        """Replace current payload. Old payload is captured in history via append_pass."""
        self.payload = dict(new_payload)
        return self

    def merge_result(self, result: dict) -> "InternalMessage":
        """Merge nanobot return into result field. Returns self."""
        self.result = dict(result)
        return self

    def set_security_clearance(self, clearance: str) -> "InternalMessage":
        """PASS 2 output — clearance must be 'cleared', 'conditional', or 'blocked'."""
        if clearance not in ("cleared", "conditional", "blocked"):
            raise ValueError(f"Invalid security_clearance: {clearance!r}")
        self.context = MessageContext(
            original_intent=self.context.original_intent,
            director_input_hash=self.context.director_input_hash,
            routing_rationale=self.context.routing_rationale,
            security_clearance=clearance,
            skill=self.context.skill,
            operation=self.context.operation,
        )
        return self

    def set_skill(self, skill: str, operation: str) -> "InternalMessage":
        """Called by specialist_outbound to record chosen skill + operation in context."""
        self.context = MessageContext(
            original_intent=self.context.original_intent,
            director_input_hash=self.context.director_input_hash,
            routing_rationale=self.context.routing_rationale,
            security_clearance=self.context.security_clearance,
            skill=skill,
            operation=operation,
        )
        return self

    # ── Boundary slices ──────────────────────────────────────────────────────

    def nanobot_request_slice(self) -> dict:
        """Slice sent to nanobot — request_id, skill, operation, payload, timeout_ms only.

        Raw Director input never crosses this boundary.
        Specialist's full payload is included — nanobot executes it verbatim.
        """
        return {
            "request_id": self.envelope.request_id,
            "skill":      self.context.skill,
            "operation":  self.context.operation,
            "payload":    self.payload,
            "timeout_ms": self.envelope.timeout_ms,
        }

    def translator_slice(self) -> dict | None:
        """Slice sent to translator — result_for_translator from orchestrator PASS 4 only.

        Full envelope never crosses this boundary (fabrication firewall).
        Returns None if result is not yet set.
        """
        if not self.result:
            return None
        return self.result.get("result_for_translator")

    # ── Validation ───────────────────────────────────────────────────────────

    def validate(self, pass_num: int, required_context_fields: list[str] | None = None) -> list[str]:
        """Validate envelope at a pass boundary. Returns list of error strings (empty = ok)."""
        errors: list[str] = []
        if not self.envelope.request_id:
            errors.append("envelope.request_id is missing")
        if self.envelope.pass_num != pass_num and pass_num > 0:
            errors.append(f"envelope.pass_num={self.envelope.pass_num!r} expected {pass_num!r}")
        if self.envelope.tier not in ("LOW", "MID", "HIGH"):
            errors.append(f"envelope.tier={self.envelope.tier!r} is not LOW|MID|HIGH")
        if required_context_fields:
            for f in required_context_fields:
                if not getattr(self.context, f, None):
                    errors.append(f"context.{f} is required but missing")
        return errors

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "envelope": self.envelope.to_dict(),
            "context":  self.context.to_dict(),
            "payload":  self.payload,
            "history":  [r.to_dict() for r in self.history],
            "result":   self.result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InternalMessage":
        env_d = d.get("envelope", {})
        env = Envelope(
            message_id=env_d.get("message_id", str(uuid.uuid4())),
            request_id=env_d.get("request_id", ""),
            session_id=env_d.get("session_id", ""),
            timestamp=env_d.get("timestamp", datetime.now(timezone.utc).isoformat()),
            pass_num=env_d.get("pass_num", 0),
            mode=env_d.get("mode", "outbound"),
            from_agent=env_d.get("from", "sovereign-core"),
            to_agent=env_d.get("to", ""),
            tier=env_d.get("tier", "LOW"),
            timeout_ms=env_d.get("timeout_ms", 30000),
        )
        ctx_d = d.get("context", {})
        ctx = MessageContext(
            original_intent=ctx_d.get("original_intent", ""),
            director_input_hash=ctx_d.get("director_input_hash", ""),
            routing_rationale=ctx_d.get("routing_rationale", ""),
            security_clearance=ctx_d.get("security_clearance", ""),
            skill=ctx_d.get("skill", ""),
            operation=ctx_d.get("operation", ""),
        )
        history = [
            PassRecord(
                pass_num=h.get("pass_num", 0),
                from_agent=h.get("from_agent", ""),
                timestamp=h.get("timestamp", ""),
                output_hash=h.get("output_hash", ""),
                duration_ms=h.get("duration_ms", 0.0),
                success=h.get("success", False),
            )
            for h in d.get("history", [])
        ]
        return cls(
            envelope=env,
            context=ctx,
            payload=d.get("payload", {}),
            history=history,
            result=d.get("result"),
        )
