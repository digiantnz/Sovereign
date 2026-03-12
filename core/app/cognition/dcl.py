"""Disclosure Control Layer (DCL)

Implements SENSITIVITY_MODEL.md + EXTERNAL_COGNITION.md.
All content destined for external providers (Grok, Claude) passes through here.
No agent bypasses the DCL. Classification is deterministic — no LLM involved.
"""

import re
from dataclasses import dataclass, field

# ── Sensitivity tiers (ordered lowest → highest) ────────────────────────────
PUBLIC             = "PUBLIC"
WORKSPACE_INTERNAL = "WORKSPACE_INTERNAL"
CONFIDENTIAL       = "CONFIDENTIAL"
PRIVATE            = "PRIVATE"
SECRET             = "SECRET"

_TIER_ORDER = [PUBLIC, WORKSPACE_INTERNAL, CONFIDENTIAL, PRIVATE, SECRET]

# ── Transformation names ─────────────────────────────────────────────────────
TRANSFORM_PASSTHROUGH = "pass_through"
TRANSFORM_COMPRESS    = "compress"
TRANSFORM_ABSTRACT    = "abstract"
TRANSFORM_MASK        = "mask"
TRANSFORM_BLOCK       = "block"

# Tier → transformation mapping
_TIER_TRANSFORM = {
    PUBLIC:             TRANSFORM_PASSTHROUGH,
    WORKSPACE_INTERNAL: TRANSFORM_COMPRESS,
    CONFIDENTIAL:       TRANSFORM_ABSTRACT,
    PRIVATE:            TRANSFORM_MASK,
    SECRET:             TRANSFORM_BLOCK,
}

# ── Token-cost rates (per 1M tokens, USD) ────────────────────────────────────
_COST_RATES = {
    "claude":      {"input": 3.00,  "output": 15.00},
    "claude-haiku":{"input": 0.80,  "output": 4.00},
    "grok":        {"input": 2.00,  "output": 10.00},
}
_COMPRESS_MAX_CHARS = 1500

# ── Detection patterns ────────────────────────────────────────────────────────

# SECRET — hard block
_SECRET_PATTERNS = [
    re.compile(r"\[SENS:SECRET\]", re.IGNORECASE),
    re.compile(r"-----BEGIN\s", re.IGNORECASE),
    re.compile(r"\bPRIVATE\s+KEY\b", re.IGNORECASE),
    re.compile(r"[A-Z_]*API[_-]?KEY\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"[A-Z_]*SECRET[_-]?KEY\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bTOKEN[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"AUTH[_-]?TOKEN\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bBEARER\s+\S{8,}", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE),
    re.compile(r"password\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"ssh-(?:rsa|ed25519|dss|ecdsa)\s+", re.IGNORECASE),
]

# PRIVATE — mask PII
_PRIVATE_PATTERNS = [
    re.compile(r"\[SENS:PRIVATE\]", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    # Phone: require at least one space/dash/paren separator — excludes IP addresses (dots only)
    re.compile(r"\+?[\d][\d\s\-\(\)]{8,}\d"),
]

# CONFIDENTIAL — abstract internal specifics
_CONFIDENTIAL_PATTERNS = [
    re.compile(r"\[SENS:CONFIDENTIAL\]", re.IGNORECASE),
    re.compile(r"\b172\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),          # RFC1918 172.x
    re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),               # RFC1918 192.168.x
    re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),            # RFC1918 10.x
    re.compile(r"/home/sovereign\b"),
    re.compile(r"/docker/sovereign\b"),
    re.compile(r"\bsovereign-core\b|\bdocker-broker\b|\ba2a-browser\b|"
               r"\bsearxng\b|\bqdrant\b|\bnextcloud-rp\b", re.IGNORECASE),
]

# Inline marker patterns for all tiers
_INLINE_MARKERS = {
    PUBLIC:             re.compile(r"\[SENS:PUBLIC\]", re.IGNORECASE),
    WORKSPACE_INTERNAL: re.compile(r"\[SENS:WORKSPACE_INTERNAL\]", re.IGNORECASE),
    CONFIDENTIAL:       re.compile(r"\[SENS:CONFIDENTIAL\]", re.IGNORECASE),
    PRIVATE:            re.compile(r"\[SENS:PRIVATE\]", re.IGNORECASE),
    SECRET:             re.compile(r"\[SENS:SECRET\]", re.IGNORECASE),
}

# ── Abstraction substitution map ──────────────────────────────────────────────
_ABSTRACT_SUBS = [
    (re.compile(r"\b172\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<INTERNAL_IP>"),
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),      "<INTERNAL_IP>"),
    (re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),   "<INTERNAL_IP>"),
    (re.compile(r"/home/sovereign(?:/[^\s,\"']+)?"),       "<INTERNAL_PATH>"),
    (re.compile(r"/docker/sovereign(?:/[^\s,\"']+)?"),     "<INTERNAL_PATH>"),
    (re.compile(r"\bsovereign-core\b", re.IGNORECASE),    "internal-service"),
    (re.compile(r"\bdocker-broker\b", re.IGNORECASE),     "internal-service"),
    (re.compile(r"\ba2a-browser\b",   re.IGNORECASE),     "internal-service"),
    (re.compile(r"\bnextcloud-rp\b",  re.IGNORECASE),     "internal-service"),
    (re.compile(r"\bsearxng\b",       re.IGNORECASE),     "internal-service"),
    (re.compile(r"\bqdrant\b",        re.IGNORECASE),     "internal-service"),
]

# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class DCLResult:
    tier: str
    transformation: str
    content: str          # transformed content; empty string if blocked
    blocked: bool
    agent: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd: float = 0.0


# ── Core DCL class ────────────────────────────────────────────────────────────

class DisclosureControlLayer:
    """Classify and transform content before any external provider call."""

    # ── Classification ────────────────────────────────────────────────────────

    def classify(self, content: str) -> str:
        """Return the highest sensitivity tier detected in content.

        Priority:
          1. Explicit [SENS:X] inline markers (any tier, including PUBLIC)
          2. Pattern-based detection (SECRET → PRIVATE → CONFIDENTIAL)
          3. Default: WORKSPACE_INTERNAL

        PUBLIC can only be set via explicit marker — never inferred.
        Multi-level rule: highest tier governs.
        """
        # Step 1: collect all explicit inline markers (any tier)
        explicit_tier: str | None = None
        for t, pattern in _INLINE_MARKERS.items():
            if pattern.search(content):
                explicit_tier = self._max_tier(explicit_tier or PUBLIC, t)

        # Step 2: pattern-based detection
        if any(p.search(content) for p in _SECRET_PATTERNS):
            return SECRET  # short-circuit — can't get higher
        pattern_tier: str | None = None
        if any(p.search(content) for p in _PRIVATE_PATTERNS):
            pattern_tier = PRIVATE
        elif any(p.search(content) for p in _CONFIDENTIAL_PATTERNS):
            pattern_tier = CONFIDENTIAL

        # Step 3: resolve
        if explicit_tier is not None and pattern_tier is not None:
            return self._max_tier(explicit_tier, pattern_tier)
        if explicit_tier is not None:
            return explicit_tier
        if pattern_tier is not None:
            return pattern_tier
        return WORKSPACE_INTERNAL  # default

    @staticmethod
    def _max_tier(a: str, b: str) -> str:
        return b if _TIER_ORDER.index(b) > _TIER_ORDER.index(a) else a

    # ── Transformations ───────────────────────────────────────────────────────

    def _compress(self, content: str) -> str:
        """Remove blank lines and trim to COMPRESS_MAX_CHARS."""
        lines = [l for l in content.splitlines() if l.strip()]
        text = "\n".join(lines)
        if len(text) > _COMPRESS_MAX_CHARS:
            text = text[:_COMPRESS_MAX_CHARS] + "\n[...compressed]"
        return text

    def _abstract(self, content: str) -> str:
        """Replace internal identifiers with generics, then compress."""
        text = content
        for pattern, replacement in _ABSTRACT_SUBS:
            text = pattern.sub(replacement, text)
        # Strip inline markers
        text = re.sub(r"\[SENS:[A-Z_]+\]", "", text).strip()
        return self._compress(text)

    def _mask(self, content: str) -> str:
        """Replace PII with stable placeholders, then abstract."""
        text = content
        # Email → <EMAIL>
        text = re.sub(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
            "<EMAIL>", text,
        )
        # Phone → <PHONE> (10+ digit sequences with separators)
        text = re.sub(
            r"\+?[\d][\d\s\-\(\)\.]{8,}\d",
            "<PHONE>", text,
        )
        return self._abstract(text)

    def _apply_transform(self, tier: str, content: str) -> tuple[str, str]:
        """Return (transformation_name, transformed_content)."""
        if tier == SECRET:
            return TRANSFORM_BLOCK, ""
        if tier == PRIVATE:
            return TRANSFORM_MASK, self._mask(content)
        if tier == CONFIDENTIAL:
            return TRANSFORM_ABSTRACT, self._abstract(content)
        if tier == WORKSPACE_INTERNAL:
            return TRANSFORM_COMPRESS, self._compress(content)
        # PUBLIC
        return TRANSFORM_PASSTHROUGH, content

    # ── Cost estimation ───────────────────────────────────────────────────────

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    def estimate_cost(self, provider: str, input_tokens: int,
                      output_tokens: int) -> float:
        rates = _COST_RATES.get(provider, _COST_RATES["grok"])
        return (
            input_tokens  * rates["input"]  / 1_000_000 +
            output_tokens * rates["output"] / 1_000_000
        )

    # ── Main entry point ──────────────────────────────────────────────────────

    def prepare(self, content: str, agent: str, provider: str) -> DCLResult:
        """Classify and transform content for an external provider call.

        Returns a DCLResult. If blocked=True, caller must NOT transmit.
        """
        tier = self.classify(content)
        transformation, transformed = self._apply_transform(tier, content)
        input_tokens = self.estimate_tokens(transformed) if not (transformation == TRANSFORM_BLOCK) else 0
        return DCLResult(
            tier=tier,
            transformation=transformation,
            content=transformed,
            blocked=(transformation == TRANSFORM_BLOCK),
            agent=agent,
            provider=provider,
            input_tokens=input_tokens,
        )

    # ── Audit logging ─────────────────────────────────────────────────────────

    def log_call(
        self,
        result: DCLResult,
        ledger,
        output_tokens: int = 0,
        provider_error: str | None = None,
    ) -> None:
        """Append an external_cognition audit record to ledger."""
        result.output_tokens = output_tokens
        result.estimated_usd = self.estimate_cost(
            result.provider, result.input_tokens, output_tokens
        )
        data = {
            "agent":          result.agent,
            "provider":       result.provider,
            "sensitivity":    result.tier,
            "transformation": result.transformation,
            "blocked":        result.blocked,
            "input_tokens":   result.input_tokens,
            "output_tokens":  result.output_tokens,
            "estimated_usd":  round(result.estimated_usd, 6),
        }
        if provider_error:
            data["provider_error"] = provider_error
        ledger.append("external_cognition", "dcl_transmission", data)
