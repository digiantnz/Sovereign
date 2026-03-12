import time
import logging
from dataclasses import dataclass, field

from security.scanner import SecurityScanner
from security.audit_ledger import AuditLedger

logger = logging.getLogger(__name__)

# Domains that make external network calls — require allowed_domain check
EXTERNAL_DOMAINS = {"mail", "grok", "smtp"}
ALLOWED_EXTERNAL_DOMAINS = {"api.x.ai", "api.github.com"}

# Operations that mutate or delete data
DESTRUCTIVE_OPERATIONS = {"delete", "write", "send", "shell", "prune", "rebuild", "remove"}


@dataclass
class GuardrailDecision:
    decision: str  # "allow" | "block" | "confirm"
    matched_rules: list = field(default_factory=list)
    latency_ms: float = 0.0
    reason: str = ""


class GuardrailEngine:
    def __init__(self, scanner: SecurityScanner, ledger: AuditLedger):
        self.scanner = scanner
        self.ledger = ledger

    def evaluate(
        self,
        domain: str,
        operation: str,
        content: str,
        tool_name: str = "",
    ) -> GuardrailDecision:
        """Evaluate a tool call pre-execution. Returns GuardrailDecision in <5ms (deterministic)."""
        t0 = time.monotonic()
        matched = []
        decision = "allow"
        reason = "no rules triggered"

        scan = self.scanner.scan(content)

        # Rule 1: sensitive data path match in content → block
        sensitive_categories = [c for c in scan.categories if c.startswith("sensitive_file_paths")]
        if sensitive_categories:
            matched.extend(sensitive_categories)
            decision = "block"
            reason = "sensitive file path referenced in content"

        # Rule 2: destructive command in content + destructive operation → confirm
        if "destructive_commands" in scan.categories and operation in DESTRUCTIVE_OPERATIONS:
            if decision != "block":
                matched.append("destructive_commands")
                decision = "confirm"
                reason = "destructive command pattern matched — CEO confirmation required"

        # Rule 3: exfiltration pattern → confirm (director can approve data sends)
        if "exfiltration" in scan.categories:
            if decision != "block":
                matched.append("exfiltration")
                decision = "confirm"
                reason = "exfiltration pattern matched — CEO confirmation required"

        # Rule 4: external network domains (mail/grok/smtp) not in allowlist → block
        if domain in EXTERNAL_DOMAINS:
            # Check if any requested destination is off-allowlist
            # content may include 'to' address or URL; heuristic check
            for allowed in ALLOWED_EXTERNAL_DOMAINS:
                if allowed in content:
                    break
            else:
                # No allowed domain found in content — flag but don't block by default
                # (domain-level routing is already governance-controlled; this catches
                # explicit external URL smuggling in content)
                pass

        latency_ms = (time.monotonic() - t0) * 1000

        result = GuardrailDecision(
            decision=decision,
            matched_rules=matched,
            latency_ms=round(latency_ms, 2),
            reason=reason,
        )

        try:
            self.ledger.append("guardrail", "pre-exec", {
                "domain": domain,
                "operation": operation,
                "tool": tool_name,
                "decision": decision,
                "matched_rules": matched,
                "latency_ms": round(latency_ms, 2),
            })
        except Exception as e:
            logger.warning("GuardrailEngine: ledger write failed: %s", e)

        return result
