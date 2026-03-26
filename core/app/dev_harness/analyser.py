"""
Dev-Harness Phase 1 — Analyser.

Responsibilities:
  - Canonical Finding dataclass and AnalysisResult container.
  - Deterministic scoring and gate logic (no LLM calls, no side effects).
  - Parsers for pylint, semgrep, and boundary_scanner output.
  - AnalysisResult.run() orchestrates the full Phase 1 local path.
    The GitHub path is handled separately by github_client.py and merged
    into the result by harness.py.

LLM/deterministic boundary: this module contains no LLM calls.
The gate() function is the sole decision point — its output is final and
is never modified by Phase 2 LLM classification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity weights — deterministic, spec §5 Phase 1
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS: dict[str, int] = {
    "critical": 50,
    "high":     20,
    "medium":    5,
    "low":       1,
}

# Gate score thresholds (lower bound for each decision band)
_THRESHOLD_REVISE   =  1   # score > 0
_THRESHOLD_BLOCK    = 20
_THRESHOLD_ESCALATE = 50


# ---------------------------------------------------------------------------
# Finding — canonical schema
# Must stay in sync with boundary_scanner.py Finding fields.
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    source:   str  # "pylint" | "semgrep" | "boundary" | "github"
    type:     str  # "lint" | "security" | "boundary"
    file:     str  # relative path from scan root
    line:     int
    message:  str
    severity: str  # "low" | "medium" | "high" | "critical"
    rule_id:  str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(
            source   = str(d.get("source",   "unknown")),
            type     = str(d.get("type",     "unknown")),
            file     = str(d.get("file",     "")),
            line     = int(d.get("line",     0)),
            message  = str(d.get("message",  "")),
            severity = _normalise_severity(str(d.get("severity", "low"))),
            rule_id  = str(d.get("rule_id",  "")),
        )


def _normalise_severity(raw: str) -> str:
    """Map any unknown severity string to 'low' rather than crashing."""
    return raw if raw in SEVERITY_WEIGHTS else "low"


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------

class GateDecision(str, Enum):
    APPROVE  = "approve"
    REVISE   = "revise"
    BLOCK    = "block"
    ESCALATE = "escalate"


def score_findings(findings: list[Finding]) -> int:
    """Deterministic total score. Never influenced by LLM output."""
    return sum(SEVERITY_WEIGHTS.get(f.severity, 1) for f in findings)


def gate(score: int) -> GateDecision:
    """
    Deterministic gate decision from score.

    Thresholds (spec §5 Phase 1):
      score == 0          → approve   (skip Phase 2)
      0 < score < 20      → revise    (Phase 2 required)
      20 <= score < 50    → block     (Phase 2 required)
      score >= 50         → escalate  (Phase 2 required; Claude mandatory)

    This function is the sole gate decision point.
    Its return value is written to the checkpoint and is final.
    Phase 2 LLM output does NOT change it.
    """
    if score == 0:
        return GateDecision.APPROVE
    if score < _THRESHOLD_BLOCK:
        return GateDecision.REVISE
    if score < _THRESHOLD_ESCALATE:
        return GateDecision.BLOCK
    return GateDecision.ESCALATE


# ---------------------------------------------------------------------------
# AnalysisResult container
# ---------------------------------------------------------------------------

@dataclass
class AnalysisResult:
    session_id:    str
    trigger:       str                         # "explicit" | "nightly"
    scan_root:     str
    findings:      list[Finding]  = field(default_factory=list)
    total_score:   int            = 0
    gate_decision: GateDecision   = GateDecision.APPROVE
    local_count:   int            = 0          # findings from local tools
    github_count:  int            = 0          # findings from GitHub path
    tool_errors:   list[str]      = field(default_factory=list)

    def finalise(self) -> "AnalysisResult":
        """Compute score and gate from current findings list. Call before saving checkpoint."""
        self.total_score   = score_findings(self.findings)
        self.gate_decision = gate(self.total_score)
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gate_decision"] = self.gate_decision.value
        d["findings"]      = [f.to_dict() for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# Output parsers — each converts raw tool output to Finding objects
# ---------------------------------------------------------------------------

# ── pylint ───────────────────────────────────────────────────────────────────

_PYLINT_SEVERITY: dict[str, str] = {
    "convention": "low",
    "refactor":   "low",
    "warning":    "medium",
    "error":      "high",
    "fatal":      "critical",
}


def parse_pylint_output(raw: str, scan_root: str) -> list[Finding]:
    """Parse ``pylint --output-format=json`` output."""
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("pylint JSON parse error: %s", exc)
        return [Finding(
            source="pylint", type="lint", file="", line=0,
            message=f"pylint output could not be parsed: {exc}",
            severity="low", rule_id="PARSE_ERROR",
        )]

    findings = []
    for item in items:
        findings.append(Finding(
            source   = "pylint",
            type     = "lint",
            file     = _rel_path(item.get("path", ""), scan_root),
            line     = int(item.get("line", 0)),
            message  = item.get("message", ""),
            severity = _PYLINT_SEVERITY.get(item.get("type", ""), "low"),
            rule_id  = item.get("message-id", ""),
        ))
    return findings


# ── semgrep ──────────────────────────────────────────────────────────────────

_SEMGREP_SEVERITY: dict[str, str] = {
    "INFO":    "low",
    "WARNING": "medium",
    "ERROR":   "high",
}


def parse_semgrep_output(raw: str, scan_root: str) -> list[Finding]:
    """Parse ``semgrep --json`` output."""
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("semgrep JSON parse error: %s", exc)
        return [Finding(
            source="semgrep", type="security", file="", line=0,
            message=f"semgrep output could not be parsed: {exc}",
            severity="low", rule_id="PARSE_ERROR",
        )]

    findings = []
    for result in data.get("results", []):
        extra   = result.get("extra", {})
        sev_raw = extra.get("severity", "WARNING").upper()
        findings.append(Finding(
            source   = "semgrep",
            type     = "security",
            file     = _rel_path(result.get("path", ""), scan_root),
            line     = result.get("start", {}).get("line", 0),
            message  = extra.get("message", ""),
            severity = _SEMGREP_SEVERITY.get(sev_raw, "medium"),
            rule_id  = result.get("check_id", ""),
        ))
    return findings


# ── boundary scanner ─────────────────────────────────────────────────────────

def parse_boundary_output(raw: str) -> list[Finding]:
    """
    Parse newline-delimited JSON from boundary_scanner.py.
    Each line is one JSON object matching the Finding schema.
    Malformed lines become low-severity parse-error Findings.
    """
    findings = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            findings.append(Finding.from_dict(d))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("boundary scanner output line %d parse error: %s", lineno, exc)
            findings.append(Finding(
                source="boundary", type="boundary", file="", line=0,
                message=f"Boundary scanner output parse error on line {lineno}: {exc}",
                severity="low", rule_id="PARSE_ERROR",
            ))
    return findings


# ---------------------------------------------------------------------------
# Local tool runner
#
# Runs pylint, semgrep, and boundary_scanner against the scan root via
# direct subprocess. In production (Phase 1 full run) these are invoked via
# the broker command dispatch so they execute with host filesystem access.
# This runner is used for direct / test invocations only.
# ---------------------------------------------------------------------------

_BOUNDARY_SCANNER = Path(__file__).parent / "boundary_scanner.py"


def _run_subprocess(
    cmd: list[str],
    *,
    timeout_s: int = 120,
    label: str = "",
) -> tuple[str, str | None]:
    """
    Run a subprocess and return (stdout, error_message).
    error_message is None on success, a string on failure.
    Never raises.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return result.stdout, None
    except subprocess.TimeoutExpired:
        msg = f"{label or cmd[0]} timed out after {timeout_s}s"
        logger.warning(msg)
        return "", msg
    except FileNotFoundError:
        msg = f"{label or cmd[0]} not found — is it installed?"
        logger.warning(msg)
        return "", msg
    except Exception as exc:  # noqa: BLE001
        msg = f"{label or cmd[0]} unexpected error: {exc}"
        logger.warning(msg)
        return "", msg


def run_local_analysis(scan_root: str, semgrep_config: str = "broker/semgrep-rules.yaml") -> AnalysisResult:
    """
    Run all three local analysis tools against scan_root.
    Returns an AnalysisResult with findings and finalised gate decision.

    semgrep_config: path to the semgrep ruleset, relative to the repo root
    or absolute.  Defaults to the local custom ruleset (no internet required).

    TEST USE ONLY — production invocations must go via broker command dispatch.
    This function must never be called from engine.py domain handlers.
    Direct subprocess execution bypasses commands-policy.yaml validation entirely.
    """
    assert os.getenv("SOVEREIGN_ENV") != "production", (
        "run_local_analysis() called in production context — "
        "use broker command dispatch (harness.py Phase 1 path)"
    )
    session_id = str(uuid.uuid4())
    result     = AnalysisResult(
        session_id = session_id,
        trigger    = "direct",
        scan_root  = scan_root,
    )

    # ── pylint ────────────────────────────────────────────────────────────
    pylint_out, pylint_err = _run_subprocess(
        ["python3", "-m", "pylint", "--output-format=json", scan_root],
        timeout_s=120, label="pylint",
    )
    if pylint_err:
        result.tool_errors.append(f"pylint: {pylint_err}")
    else:
        pylint_findings = parse_pylint_output(pylint_out, scan_root)
        result.findings.extend(pylint_findings)
        result.local_count += len(pylint_findings)

    # ── semgrep ───────────────────────────────────────────────────────────
    semgrep_out, semgrep_err = _run_subprocess(
        ["semgrep", "--config", semgrep_config, "--json", scan_root],
        timeout_s=120, label="semgrep",
    )
    if semgrep_err:
        result.tool_errors.append(f"semgrep: {semgrep_err}")
    else:
        semgrep_findings = parse_semgrep_output(semgrep_out, scan_root)
        result.findings.extend(semgrep_findings)
        result.local_count += len(semgrep_findings)

    # ── boundary scanner ──────────────────────────────────────────────────
    boundary_out, boundary_err = _run_subprocess(
        ["python3", str(_BOUNDARY_SCANNER), scan_root],
        timeout_s=60, label="boundary_scanner",
    )
    if boundary_err:
        result.tool_errors.append(f"boundary_scanner: {boundary_err}")
    else:
        boundary_findings = parse_boundary_output(boundary_out)
        result.findings.extend(boundary_findings)
        result.local_count += len(boundary_findings)

    result.finalise()
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rel_path(path: str, root: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path
