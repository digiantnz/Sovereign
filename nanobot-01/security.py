"""
Security helpers for nanobot-01:
- Shared secret authentication (X-API-Key header)
- UNTRUSTED_CONTENT wrapping before LLM calls
- Prompt injection scanning on inbound skill results
"""
import json
import os
import re

from fastapi import HTTPException, Request

# Shared secret — must match NANOBOT_SHARED_SECRET in sovereign-core's env
SHARED_SECRET: str = os.environ.get("NANOBOT_SHARED_SECRET", "")

# Patterns that suggest prompt injection in skill result payloads
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/INST\]", re.IGNORECASE),
    re.compile(r"###\s*(human|assistant|system)\s*:", re.IGNORECASE),
]


def verify_secret(request: Request) -> None:
    """FastAPI dependency — raises 401 if shared secret missing/wrong."""
    if not SHARED_SECRET:
        # Unconfigured — block all traffic until secret is set
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
    """Scan a skill result dict for prompt injection patterns.

    Walks all string values recursively. Returns a list of violation descriptions.
    Empty list = clean.
    """
    findings: list[str] = []
    _scan_value(result, findings, depth=0)
    return findings


def _scan_value(obj: object, findings: list[str], depth: int) -> None:
    if depth > 8:
        return
    if isinstance(obj, str):
        for pat in _INJECTION_PATTERNS:
            if pat.search(obj):
                findings.append(f"injection pattern '{pat.pattern[:40]}' matched in result string")
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan_value(v, findings, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _scan_value(item, findings, depth + 1)
