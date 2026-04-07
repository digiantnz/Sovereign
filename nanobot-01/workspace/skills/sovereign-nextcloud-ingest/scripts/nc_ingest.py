#!/usr/bin/env python3
"""Nextcloud knowledge ingestion pipeline for nanobot-01 python3_exec dispatch.

Commands:
  fetch_classify        -- fetch single file, inline scan, classify type
  fetch_classify_folder -- recursive folder fetch with per-file scan
  ingest_status         -- check OCS tags on a file

Env vars:
  NEXTCLOUD_ADMIN_USER
  NEXTCLOUD_ADMIN_PASSWORD
  NEXTCLOUD_URL (default: http://nextcloud)

Output: JSON to stdout. Errors: {"status":"error","error":"..."} + exit 1.

Private folder policy: any path containing "private" (case-insensitive)
sets _private:true in the response. sovereign-core enforces force_local on
all LLM calls using that content.
"""

import argparse
import base64
import json
import os
import re
import sys
from urllib.parse import unquote, quote

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_NC_URL  = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NC_USER = os.environ.get("NEXTCLOUD_ADMIN_USER", "digiant")
_NC_PASS = os.environ.get("NEXTCLOUD_ADMIN_PASSWORD", "")

_WEBDAV_BASE = f"{_NC_URL}/remote.php/dav/files/{_NC_USER}"
_OCS_TAGS    = f"{_NC_URL}/ocs/v2.php/apps/systemtags/api/v1"

_MAX_SINGLE     = 128 * 1024   # 128 KB per file
_MAX_FOLDER_TOT = 512 * 1024   # 512 KB total for folder ingest
_DEFAULT_MAX_FILES = 20

_BASE_PATH_STRIP = f"/remote.php/dav/files/{_NC_USER}"


def _auth():
    return HTTPBasicAuth(_NC_USER, _NC_PASS)


def _ocs_headers():
    return {"OCS-APIRequest": "true", "Accept": "application/json"}


def _dav_url(path):
    return f"{_WEBDAV_BASE}/{quote(path.lstrip('/'), safe='/')}"


def _out(obj):
    print(json.dumps(obj))


def _err(msg, **kwargs):
    _out({"status": "error", "error": msg, **kwargs})
    sys.exit(1)


def _is_private(path):
    return bool(re.search(r'\bprivate\b', path, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Inline security scan
# ---------------------------------------------------------------------------

_SCAN_PATTERNS = [
    (re.compile(r'(?:eval|exec|subprocess|os\.system|popen)\s*\(', re.I), "shell_exec"),
    (re.compile(r'ignore\s+(?:previous|prior|above)\s+instructions', re.I), "prompt_injection"),
    (re.compile(r'you\s+are\s+now\s+(?:a|an)\b', re.I), "prompt_injection"),
    (re.compile(r'(?:password|passwd|api_key|secret|token)\s*[=:]\s*\S{8,}', re.I), "credential_leak"),
    (re.compile(r'base64.*decode.*exec|eval.*base64', re.I | re.S), "base64_payload"),
]

_CRITICAL_PATTERNS = {"shell_exec", "base64_payload"}


def _inline_scan(content):
    found = []
    for pattern, label in _SCAN_PATTERNS:
        if pattern.search(content):
            found.append(label)
    critical = any(f in _CRITICAL_PATTERNS for f in found)
    if len(found) == 0:
        risk = "low"
    elif len(found) <= 2 and not critical:
        risk = "medium"
    else:
        risk = "high"
    return {"risk_level": risk, "patterns_found": found, "pattern_count": len(found)}


# ---------------------------------------------------------------------------
# Content type classification
# ---------------------------------------------------------------------------

_TEXT_TYPES = {
    "text/plain", "text/markdown", "text/html", "text/csv",
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/yaml",
}

_WORKFLOW_KW = re.compile(
    r'\b(?:step\s+\d|procedure|instructions|how\s+to|workflow|checklist|prerequisites)\b', re.I
)
_DATE_PATTERN = re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2},?\s+\d{4}', re.I)


def _suggest_memory_type(path, content, content_type):
    """Heuristic: semantic (reference), episodic (time-stamped), procedural (workflow)."""
    name_lower = path.lower()
    if _WORKFLOW_KW.search(content[:4096]):
        return "procedural"
    if _DATE_PATTERN.search(name_lower) or re.search(r'\d{4}[-_]\d{2}[-_]\d{2}', name_lower):
        return "episodic"
    if content_type in ("text/markdown", "text/html"):
        return "semantic"
    return "semantic"


def _is_text_content_type(ct):
    base = ct.split(";")[0].strip()
    return base in _TEXT_TYPES or base.startswith("text/")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_fetch_classify(args):
    path    = args.path
    private = _is_private(path)
    url     = _dav_url(path)

    r = requests.get(url, auth=_auth(), timeout=30)
    if r.status_code != 200:
        _err(f"GET {path} failed: {r.status_code}", http_status=r.status_code)

    ct = r.headers.get("content-type", "").split(";")[0].strip()
    size = len(r.content)

    if not _is_text_content_type(ct):
        _out({
            "status": "ok",
            "path": path,
            "content": None,
            "content_type": ct,
            "size": size,
            "binary": True,
            "encoding": None,
            "inline_scan": {"risk_level": "low", "patterns_found": [], "binary": True},
            "suggested_memory_type": "semantic",
            "_private": private,
        })
        return

    truncated = size > _MAX_SINGLE
    text = r.content[:_MAX_SINGLE].decode("utf-8", errors="replace")
    scan = _inline_scan(text)
    scan["binary"]    = False
    scan["truncated"] = truncated

    memory_type = _suggest_memory_type(path, text, ct)

    _out({
        "status": "ok",
        "path": path,
        "content": text,
        "content_type": ct,
        "size": size,
        "binary": False,
        "encoding": "utf-8",
        "inline_scan": scan,
        "suggested_memory_type": memory_type,
        "_private": private,
        "http_status": r.status_code,
    })


def cmd_fetch_classify_folder(args):
    path      = args.path
    max_files = int(args.max_files or _DEFAULT_MAX_FILES)

    # PROPFIND Depth:infinity to get all items
    propfind_xml = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:">'
        "<d:prop><d:displayname/><d:getcontentlength/><d:resourcetype/>"
        "<d:getcontenttype/></d:prop>"
        "</d:propfind>"
    )
    url = _dav_url(path)
    r = requests.request("PROPFIND", url, auth=_auth(),
                         headers={"Depth": "infinity", "Content-Type": "application/xml; charset=utf-8"},
                         data=propfind_xml.encode(), timeout=30)
    if r.status_code not in (207, 200):
        _err(f"PROPFIND {path} failed: {r.status_code}")

    # Parse items, filter to files only
    files = []
    prefix = (_BASE_PATH_STRIP + "/" + path.strip("/")).rstrip("/")
    for resp in re.findall(r"<d:response>(.*?)</d:response>", r.text, re.DOTALL):
        href_m = re.search(r"<d:href>(.*?)</d:href>", resp)
        if not href_m:
            continue
        href = href_m.group(1).rstrip("/")
        if href == prefix or href == prefix + "/":
            continue
        if "<d:collection" in resp:
            continue  # skip folders
        item_path = unquote(href[len(_BASE_PATH_STRIP):]) if href.startswith(_BASE_PATH_STRIP) else href
        ct_m = re.search(r"<d:getcontenttype>(.*?)</d:getcontenttype>", resp)
        ct   = (ct_m.group(1).split(";")[0].strip()) if ct_m else ""
        size_m = re.search(r"<d:getcontentlength>(.*?)</d:getcontentlength>", resp)
        size   = int(size_m.group(1)) if size_m else 0
        if _is_text_content_type(ct) and size <= _MAX_SINGLE:
            files.append({"path": item_path, "content_type": ct, "size": size})

    total_found = len(files)
    files = files[:max_files]
    skipped = total_found - len(files)

    results   = []
    total_bytes = 0
    for f in files:
        if total_bytes >= _MAX_FOLDER_TOT:
            skipped += 1
            continue
        furl = _dav_url(f["path"])
        fr   = requests.get(furl, auth=_auth(), timeout=20)
        if fr.status_code != 200:
            results.append({"path": f["path"], "status": "error",
                            "error": f"GET failed: {fr.status_code}", "_private": _is_private(f["path"])})
            continue
        text = fr.content[:_MAX_SINGLE].decode("utf-8", errors="replace")
        total_bytes += len(text)
        scan = _inline_scan(text)
        results.append({
            "path":                f["path"],
            "status":             "ok",
            "content_type":       f["content_type"],
            "size":               f["size"],
            "content":            text,
            "inline_scan":        scan,
            "suggested_memory_type": _suggest_memory_type(f["path"], text, f["content_type"]),
            "_private":           _is_private(f["path"]),
        })

    _out({
        "status":      "ok",
        "path":        path,
        "files":       results,
        "total_files": len(results),
        "total_bytes": total_bytes,
        "skipped":     skipped,
        "_private":    _is_private(path),
    })


def cmd_ingest_status(args):
    path = args.path

    # Get fileId via PROPFIND
    propfind = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:prop><oc:fileid/></d:prop>"
        "</d:propfind>"
    )
    url = _dav_url(path)
    r = requests.request("PROPFIND", url, auth=_auth(),
                         headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                         data=propfind.encode())
    if r.status_code not in (207, 200):
        _err(f"PROPFIND {path} failed: {r.status_code}")

    fid_m = re.search(r"<oc:fileid>(.*?)</oc:fileid>", r.text)
    file_id = fid_m.group(1).strip() if fid_m else None
    if not file_id:
        _err(f"Could not get fileId for {path}")

    # List tags on file
    tags_url = f"{_OCS_TAGS}/objects/files/{file_id}"
    tr = requests.get(tags_url, auth=_auth(), headers=_ocs_headers())
    tags = []
    sovereign_reviewed = False
    if tr.status_code == 200:
        try:
            tags_data = tr.json().get("ocs", {}).get("data", [])
            for t in tags_data:
                tags.append({"id": t.get("id"), "name": t.get("name")})
                if t.get("name") == "sovereign-reviewed":
                    sovereign_reviewed = True
        except Exception:
            pass

    _out({
        "status":             "ok",
        "path":               path,
        "file_id":            file_id,
        "tags":               tags,
        "sovereign_reviewed": sovereign_reviewed,
        "_private":           _is_private(path),
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMANDS = {
    "fetch_classify":        cmd_fetch_classify,
    "fetch_classify_folder": cmd_fetch_classify_folder,
    "ingest_status":         cmd_ingest_status,
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--command",   required=True, choices=list(_COMMANDS))
    p.add_argument("--path",      default="")
    p.add_argument("--max-files", dest="max_files", default=str(_DEFAULT_MAX_FILES))
    args = p.parse_args()

    fn = _COMMANDS[args.command]
    try:
        fn(args)
    except SystemExit:
        raise
    except Exception as e:
        _err(f"{args.command} raised: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
