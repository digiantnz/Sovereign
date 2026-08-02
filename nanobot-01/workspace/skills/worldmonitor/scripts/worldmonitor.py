#!/usr/bin/env python3
"""worldmonitor — python3_exec script for nanobot-01.

Calls a2a-browser's /worldmonitor route (node04 loopback to WorldMonitor).
Owns: staleness suppression (age-gate), the verdict/bullishCount
consensus-to-interrogate relabel, and advisoryProvenance passthrough.
a2a-browser stays dumb (fetch + pass-through only); this script is the one
policy/structural layer, per the anti-consensus obligation in sovereign-soul.md.

Commands:
  get-risk-scores   -- geopolitical/conflict risk scores (ciiScores, strategicRisks)
  get-macro-signals -- economic/macro indicators (signals) + consensus_to_interrogate

Output format (flat — no nested "data" key):
  success: {"status":"ok", "domain":"...", ...}
  error:   {"status":"error", "error":"..."}  + exit 1

Env vars (injected from secrets/browser.env — same as sovereign-browser, zero new
credentials):
  A2A_BROWSER_URL     -- base URL of a2a-browser (default: http://172.16.201.4:8001)
  A2A_SHARED_SECRET   -- shared secret for X-API-Key auth
"""

import argparse
import json
import os
import sys
import time

import requests

_BASE_URL = os.environ.get("A2A_BROWSER_URL", "http://172.16.201.4:8001").rstrip("/")
_SECRET   = os.environ.get("A2A_SHARED_SECRET", "")
_TIMEOUT  = 20

# Monitored guess (not derived) — adjust from logged observation_age_hours over time.
# Age is the sole suppress/caveat gate; degraded/stale/unavailable are qualifiers
# layered on top only once a pull has already passed the age gate. This resolves
# a real gap: the first live get-risk-scores pull returned stale:false on a payload
# that was pure static fallback (zero dynamic scoring) — the vendor's own flag
# under-reported staleness, so age-from-timestamp is authoritative, not the flag.
_WM_STALE_MAX_AGE_HOURS = 72


def _headers():
    return {"X-API-Key": _SECRET, "Content-Type": "application/json"}


def _call(domain: str, params: dict) -> dict:
    r = requests.post(
        f"{_BASE_URL}/worldmonitor", json={"domain": domain, "params": params},
        headers=_headers(), timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _staleness_gate(age_hours, age_known, degraded, stale, unavailable):
    """Two-stage: age decides suppress vs surface; flags decide assertible vs
    caveat-only, only once surfaced. Unknown age fails safe (never reads as fresh)."""
    if not age_known:
        return {"suppressed": False, "assertable": False, "reason": "age_unknown_failsafe"}
    if age_hours > _WM_STALE_MAX_AGE_HOURS:
        return {
            "suppressed": True, "assertable": False,
            "reason": f"age {age_hours:.1f}h > {_WM_STALE_MAX_AGE_HOURS}h",
        }
    if degraded or stale or unavailable:
        return {"suppressed": False, "assertable": False, "reason": "degraded_or_stale_flag"}
    return {"suppressed": False, "assertable": True, "reason": None}


def cmd_get_risk_scores(args):
    if not _SECRET:
        print(json.dumps({"status": "error", "error": "A2A_SHARED_SECRET not configured"}))
        sys.exit(1)

    try:
        body = _call("risk-scores", {})
    except requests.HTTPError as e:
        print(json.dumps({"status": "error", "error": f"a2a-browser HTTP {e.response.status_code}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"worldmonitor unreachable: {e}"}))
        sys.exit(1)

    cii_scores = body.get("ciiScores") or []
    degraded, stale = bool(body.get("degraded")), bool(body.get("stale"))

    # computedAt (epoch-ms) is per-row; observed identical across rows in a pull,
    # but not guaranteed by schema — take the oldest (min) row for the more
    # conservative (larger) age if rows ever diverge.
    computed_at_ms, age_known, age_hours = None, False, None
    candidates = [row.get("computedAt") for row in cii_scores
                  if isinstance(row, dict) and row.get("computedAt")]
    if candidates:
        computed_at_ms = min(candidates)
        age_known = True
        age_hours = (time.time() * 1000 - computed_at_ms) / 3_600_000

    gate = _staleness_gate(age_hours, age_known, degraded, stale, False)

    print(json.dumps({
        "status": "ok",
        "domain": "risk-scores",
        # advisoryProvenance stays in-row, unconsumed here — cheap to store now,
        # impossible to backfill later if a future version wants to weight by it.
        "cii_scores":      [] if gate["suppressed"] else cii_scores,
        "strategic_risks": [] if gate["suppressed"] else (body.get("strategicRisks") or []),
        "degraded": degraded,
        "stale": stale,
        "computed_at_ms": computed_at_ms,
        "observation_age_hours": age_hours,
        "age_known": age_known,
        "suppressed": gate["suppressed"],
        "assertable": gate["assertable"],
        "gate_reason": gate["reason"],
    }))


def cmd_get_macro_signals(args):
    if not _SECRET:
        print(json.dumps({"status": "error", "error": "A2A_SHARED_SECRET not configured"}))
        sys.exit(1)

    try:
        body = _call("macro-signals", {})
    except requests.HTTPError as e:
        print(json.dumps({"status": "error", "error": f"a2a-browser HTTP {e.response.status_code}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"worldmonitor unreachable: {e}"}))
        sys.exit(1)

    signals = body.get("signals") or []
    degraded, unavailable = bool(body.get("degraded")), bool(body.get("unavailable"))

    # get-macro-signals' timestamp field is unconfirmed (no live payload reviewed
    # yet — open item). Try known candidate field names inside `meta`; if none
    # match, age_known stays False so the fail-safe gate applies rather than
    # guessing a schema that hasn't been checked.
    meta = body.get("meta") or {}
    computed_at_ms, age_known, age_hours = None, False, None
    for _field in ("computedAt", "asOf", "timestamp", "updatedAt"):
        if isinstance(meta.get(_field), (int, float)):
            computed_at_ms = meta[_field]
            age_known = True
            age_hours = (time.time() * 1000 - computed_at_ms) / 3_600_000
            break

    gate = _staleness_gate(age_hours, age_known, degraded, False, unavailable)

    # verdict/bullishCount are a third-party conclusion, not data — deterministic
    # relabel, structurally separate from `signals` (grounded input). Never merged
    # into the same downstream prompt section.
    consensus = {
        "label": "THIRD-PARTY CONSENSUS — INTERROGATE, DO NOT ADOPT",
        "verdict": body.get("verdict"),
        "bullish_count": body.get("bullishCount"),
        "total_count": body.get("totalCount"),
    }

    print(json.dumps({
        "status": "ok",
        "domain": "macro-signals",
        "signals": [] if gate["suppressed"] else signals,
        "consensus_to_interrogate": consensus,
        "degraded": degraded,
        "unavailable": unavailable,
        "computed_at_ms": computed_at_ms,
        "observation_age_hours": age_hours,
        "age_known": age_known,
        "suppressed": gate["suppressed"],
        "assertable": gate["assertable"],
        "gate_reason": gate["reason"],
    }))


def main():
    parser = argparse.ArgumentParser(description="worldmonitor nanobot script")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get-risk-scores")
    sub.add_parser("get-macro-signals")

    args = parser.parse_args()
    dispatch = {
        "get-risk-scores":   cmd_get_risk_scores,
        "get-macro-signals": cmd_get_macro_signals,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
