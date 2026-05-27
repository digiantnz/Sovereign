"""ClawSec pattern refresh pipeline.

Fetches the live ClawSec advisory feed, translates to dynamic patterns,
writes clawsec_dynamic.yaml, and atomically updates the soul-guardian checksum.

Atomicity guarantee: YAML write and checksum write are in the same synchronous
block with no await between them.
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx
import yaml

logger = logging.getLogger(__name__)

_FEED_URL           = "https://raw.githubusercontent.com/prompt-security/clawsec/main/advisories/feed.json"
_FEED_TIMEOUT       = 20.0
_DYNAMIC_YAML_PATH  = "/home/sovereign/security/clawsec_dynamic.yaml"
_CHECKSUM_PATH      = "/home/sovereign/security/.checksums.json"
_PENDING_DIR        = "/home/sovereign/security/pending"
_MAX_PENDING_DAYS   = 30

# CWE slug → internal scanner category
_TYPE_MAP = {
    "path_traversal":              "path_traversal",
    "directory_traversal":         "path_traversal",
    "local_file_inclusion":        "path_traversal",
    "server_side_request_forgery": "ssrf",
    "ssrf":                        "ssrf",
    "command_injection":           "command_injection",
    "os_command_injection":        "command_injection",
    "shell_injection":             "command_injection",
    "code_injection":              "command_injection",
    "incorrect_authorization":     "skill_injection",
    "authorization_bypass":        "skill_injection",
    "authentication_bypass":       "skill_injection",
    "improper_authentication":     "skill_injection",
    "privilege_escalation":        "skill_injection",
    "sandbox_escape":              "skill_injection",
    "information_disclosure":      "credential_leak",
    "credential_exposure":         "credential_leak",
}

_STOP_WORDS = {
    "openclaw", "before", "after", "version", "vulnerability", "via",
    "the", "and", "not", "for", "with", "from", "that", "this", "when",
    "are", "its", "was", "a", "an", "in", "of", "to", "is", "or",
}


def _slug_to_category(cwe_type: str, title: str) -> str:
    slug = cwe_type.lower().strip()
    if slug in _TYPE_MAP:
        return _TYPE_MAP[slug]
    title_l = title.lower()
    if any(w in title_l for w in ("prompt inject", "instruction override", "jailbreak")):
        return "prompt_injection"
    if any(w in title_l for w in ("path traversal", "directory traversal", "file inclusion")):
        return "path_traversal"
    if any(w in title_l for w in ("ssrf", "request forgery", "internal endpoint")):
        return "ssrf"
    if any(w in title_l for w in ("command inject", "shell inject", "code inject", "exec")):
        return "command_injection"
    if any(w in title_l for w in ("auth bypass", "authorization bypass", "sandbox escape", "privilege")):
        return "skill_injection"
    if any(w in title_l for w in ("token", "credential", "secret", "password", "key leak")):
        return "credential_leak"
    return "advisory_block"


def _title_to_keyword_pattern(title: str) -> str | None:
    words = re.findall(r"[a-z][a-z0-9_\-]{3,}", title.lower())
    keywords = [w for w in words if w not in _STOP_WORDS]
    if len(keywords) < 2:
        return None
    return "(?i)" + ".*".join(re.escape(w) for w in keywords[:3])


def _severity_to_action(severity: str, exploit_available: bool) -> str:
    s = severity.lower()
    if s == "critical":
        return "block"
    if s == "high" and exploit_available:
        return "block"
    return "warn"


def _translate_feed(feed: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for adv in feed.get("advisories", []):
        cve_id   = adv.get("id", "unknown")
        severity = adv.get("severity", "medium")
        cwe_type = adv.get("type", "")
        title    = adv.get("title", "")
        exploit  = adv.get("exploit_detection", {}).get("exploit_available", False)
        action   = _severity_to_action(severity, exploit)
        category = _slug_to_category(cwe_type, title)

        if category != "advisory_block":
            continue

        pattern = _title_to_keyword_pattern(title)
        if not pattern:
            continue

        result.setdefault("advisory_block", []).append({
            "pattern":   pattern,
            "action":    action,
            "source_id": cve_id,
            "severity":  severity,
        })
    return result


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_dynamic_yaml_and_checksum(patterns: dict, version: str) -> None:
    """Write clawsec_dynamic.yaml then immediately update .checksums.json — no await between."""
    ts = datetime.now(timezone.utc).isoformat()
    data = {"version": version, "updated": ts, "categories": patterns}

    tmp_yaml = _DYNAMIC_YAML_PATH + ".tmp"
    with open(tmp_yaml, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
    os.replace(tmp_yaml, _DYNAMIC_YAML_PATH)

    new_hash = _sha256_file(_DYNAMIC_YAML_PATH)
    try:
        with open(_CHECKSUM_PATH) as f:
            checksums = json.load(f)
    except Exception:
        checksums = {}
    checksums[_DYNAMIC_YAML_PATH] = new_hash
    tmp_cs = _CHECKSUM_PATH + ".tmp"
    with open(tmp_cs, "w") as f:
        json.dump(checksums, f, indent=2)
    os.replace(tmp_cs, _CHECKSUM_PATH)
    logger.info(
        "clawsec_harness: dynamic yaml written v%s, checksum %s",
        version, new_hash[:16],
    )


def _log_to_pending(patterns: dict, version: str) -> None:
    """Write a fetch log entry to security/pending/."""
    os.makedirs(_PENDING_DIR, exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(_PENDING_DIR, f"fetch-{ts_tag}.json")
    entry = {
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
        "feed_version":  version,
        "patterns_added": sum(len(v) for v in patterns.values()),
        "categories":    list(patterns.keys()),
    }
    with open(log_path, "w") as f:
        json.dump(entry, f, indent=2)


def _clear_old_pending_logs(max_age_days: int = _MAX_PENDING_DAYS) -> int:
    """Remove fetch-*.json log files older than max_age_days. Returns count removed."""
    if not os.path.exists(_PENDING_DIR):
        return 0
    now = datetime.now(timezone.utc)
    removed = 0
    for fname in os.listdir(_PENDING_DIR):
        if not (fname.startswith("fetch-") and fname.endswith(".json")):
            continue
        fpath = os.path.join(_PENDING_DIR, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            fetched_at = data.get("fetched_at", "")
            if fetched_at:
                age_days = (now - datetime.fromisoformat(fetched_at)).days
            else:
                age_days = (now.timestamp() - os.path.getmtime(fpath)) / 86400
            if age_days > max_age_days:
                os.remove(fpath)
                removed += 1
        except Exception as exc:
            logger.warning("clawsec_harness: error checking pending log %s: %s", fname, exc)
    if removed:
        logger.info("clawsec_harness: cleared %d old pending logs (>%d days)", removed, max_age_days)
    return removed


async def fetch_and_apply(qdrant, ledger) -> dict:
    """Full ClawSec pipeline: fetch → translate → write YAML+checksum → log.

    Returns summary dict: {status, feed_version, patterns_added, errors}.
    """
    result: dict = {
        "status":         "ok",
        "feed_version":   None,
        "patterns_added": 0,
        "errors":         [],
    }

    # 1. Fetch feed
    try:
        async with httpx.AsyncClient(timeout=_FEED_TIMEOUT) as client:
            resp = await client.get(_FEED_URL)
            resp.raise_for_status()
            feed = resp.json()
        version = feed.get("version", "unknown")
        result["feed_version"] = version
    except Exception as exc:
        msg = f"feed fetch failed: {exc}"
        logger.error("clawsec_harness: %s", msg)
        result["errors"].append(msg)
        result["status"] = "error"
        return result

    # 2. Translate
    try:
        patterns = _translate_feed(feed)
        result["patterns_added"] = sum(len(v) for v in patterns.values())
    except Exception as exc:
        msg = f"translate failed: {exc}"
        logger.error("clawsec_harness: %s", msg)
        result["errors"].append(msg)
        result["status"] = "error"
        return result

    # 3. Write YAML + checksum atomically (synchronous — no await between writes)
    try:
        _write_dynamic_yaml_and_checksum(patterns, version)
    except Exception as exc:
        msg = f"yaml write failed: {exc}"
        logger.error("clawsec_harness: %s", msg)
        result["errors"].append(msg)
        result["status"] = "partial"

    # 4. Clear old fetch logs + write new log
    try:
        _clear_old_pending_logs()
        _log_to_pending(patterns, version)
    except Exception as exc:
        logger.warning("clawsec_harness: pending log failed: %s", exc)

    # 5. Audit ledger entry
    if ledger:
        try:
            ledger.append("clawsec_update", "security", {
                "feed_version":   version,
                "patterns_added": result["patterns_added"],
                "status":         result["status"],
            })
        except Exception:
            pass

    return result
