#!/usr/bin/env python3
"""sovereign-browser — python3_exec script for nanobot-01.

Routes web search and page fetch requests to the a2a-browser service
(node04, 172.16.201.4:8001) via REST. Auth via X-API-Key shared secret.

Commands:
  search  -- POST /search  -- SearXNG-backed web search with AI enrichment
  fetch   -- POST /fetch   -- Playwright page render and text extraction

Output format (flat — no nested "data" key):
  search success: {"status":"ok", "sovereign_synthesis":{...}, "results":[...], ...}
  fetch  success: {"status":"ok", "url":"...", "title":"...", "content":"...", ...}
  error:          {"status":"error", "error":"..."}  + exit 1

Env vars (injected from secrets/browser.env):
  A2A_BROWSER_URL     -- base URL of a2a-browser (default: http://172.16.201.4:8001)
  A2A_SHARED_SECRET   -- shared secret for X-API-Key auth

Known limitation: AUTH_PROFILES (e.g. GITHUB_PAT headers for api.github.com) are not
currently injected into fetch payloads. a2a-browser does not expose an auth field on
/search. When GITHUB_PAT is populated in future, this script should build per-host auth
headers from env and pass them in the fetch payload where a2a-browser supports it.
"""

import argparse
import json
import os
import sys

import requests

_BASE_URL = os.environ.get("A2A_BROWSER_URL", "http://172.16.201.4:8001").rstrip("/")
_SECRET   = os.environ.get("A2A_SHARED_SECRET", "")
_TIMEOUT  = 200


def _headers():
    return {"X-API-Key": _SECRET, "Content-Type": "application/json"}


def cmd_search(args):
    if not _SECRET:
        print(json.dumps({"status": "error", "error": "A2A_SHARED_SECRET not configured"}))
        sys.exit(1)

    body = {
        "query":         args.query,
        "locale":        args.locale,
        "return_format": args.return_format,
    }
    if str(args.test_mode).lower() in ("true", "1", "yes"):
        body["test_mode"] = True

    try:
        r = requests.post(f"{_BASE_URL}/search", json=body, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        # Output flat — a2a-browser SearchResponse fields at top level.
        # Explicitly exclude a2a-browser's own "status" field (may be "failed" for degraded
        # enrichment) so our wrapper status="ok" (HTTP 200) is not overridden.
        result = r.json()
        flat = {k: v for k, v in result.items() if k != "status"}
        print(json.dumps({"status": "ok", **flat}))
    except requests.HTTPError as e:
        print(json.dumps({"status": "error", "error": f"a2a-browser HTTP {e.response.status_code}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"a2a-browser unreachable: {e}"}))
        sys.exit(1)


def cmd_fetch(args):
    if not _SECRET:
        print(json.dumps({"status": "error", "error": "A2A_SHARED_SECRET not configured"}))
        sys.exit(1)

    body = {
        "url":     args.url,
        "extract": args.extract,
    }

    try:
        r = requests.post(f"{_BASE_URL}/fetch", json=body, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        # Output flat — a2a-browser FetchResponse fields at top level.
        # Exclude a2a-browser's own "status" field to avoid overriding our wrapper status.
        result = r.json()
        flat = {k: v for k, v in result.items() if k != "status"}
        print(json.dumps({"status": "ok", **flat}))
    except requests.HTTPError as e:
        print(json.dumps({"status": "error", "error": f"a2a-browser HTTP {e.response.status_code}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"a2a-browser unreachable: {e}"}))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="sovereign-browser nanobot script")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search")
    p_search.add_argument("--query",         required=True)
    p_search.add_argument("--locale",        default="en-NZ")
    p_search.add_argument("--return_format", default="full")
    p_search.add_argument("--test_mode",     default="false")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--url",     required=True)
    p_fetch.add_argument("--extract", default="text")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "fetch":
        cmd_fetch(args)


if __name__ == "__main__":
    main()
