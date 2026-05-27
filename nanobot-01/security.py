"""
Security helpers for nanobot-01:
- Shared secret authentication (X-API-Key header)
- UNTRUSTED_CONTENT wrapping before LLM calls
- Prompt injection scanning on inbound skill results via nanobot_security
"""
import json
import logging
import os

import yaml
from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)

# Shared secret — must match NANOBOT_SHARED_SECRET in sovereign-core's env
SHARED_SECRET: str = os.environ.get("NANOBOT_SHARED_SECRET", "")

# ── Security module initialisation ──────────────────────────────────────────
# nanobot_security is available via PYTHONPATH=/nanobot-security (compose.yml mount).
# Loaded at import time; failures fall back to empty scanner (allow-all).

_security_module = None

try:
    from nanobot_security import NanobotSecurityModule
    _security_module = NanobotSecurityModule(
        nanobot_name="nanobot-01",
        ledger_path="/workspace/audit/nanobot-security-audit.jsonl",
    )
    _security_module.load()

    # Augment with live dynamic patterns from sovereign-core's clawsec_harness output
    _dynamic_path = "/home/sovereign/security/clawsec_dynamic.yaml"
    if os.path.exists(_dynamic_path):
        try:
            with open(_dynamic_path) as _f:
                _dyn = yaml.safe_load(_f) or {}
            _categories = _dyn.get("categories", {})
            if _categories:
                _security_module._scanner.load_dynamic_patterns(_categories)
                _log.info(
                    "nanobot-01 security: loaded clawsec_dynamic.yaml patterns (%d categories)",
                    len(_categories),
                )
        except Exception as _exc:
            _log.warning("nanobot-01 security: failed to load clawsec_dynamic.yaml: %s", _exc)

    _log.info(
        "nanobot-01 security: loaded — %d rules", _security_module._scanner.rule_count
    )

except ImportError:
    _log.warning(
        "nanobot-01 security: nanobot_security not available — "
        "PYTHONPATH=/nanobot-security not set or package missing. "
        "Falling back to allow-all scanner."
    )
except Exception as _exc:
    _log.warning("nanobot-01 security: module init failed: %s", _exc)


def verify_secret(request: Request) -> None:
    """FastAPI dependency — raises 401 if shared secret missing/wrong."""
    if not SHARED_SECRET:
        raise HTTPException(status_code=503, detail="Service not configured")
    key = request.headers.get("X-API-Key", "")
    if key != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def wrap_untrusted(data: object) -> str:
    """Wrap raw skill result data in UNTRUSTED_CONTENT tags for safe LLM ingestion."""
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return (
        "UNTRUSTED_CONTENT_BEGIN\n"
        + content
        + "\nUNTRUSTED_CONTENT_END"
    )


def scan_result(result: dict) -> list[str]:
    """Scan a skill result dict for injection patterns.

    Uses NanobotSecurityModule if available; returns empty list on allow.
    """
    if _security_module is None:
        return []
    try:
        sr = _security_module.scan_outbound(result)
        if sr.decision != "allow":
            return [sr.reason]
    except Exception as exc:
        _log.warning("nanobot-01 security: scan_result error: %s", exc)
    return []
