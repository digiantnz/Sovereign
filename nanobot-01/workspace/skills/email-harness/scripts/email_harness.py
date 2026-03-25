#!/usr/bin/env python3
"""email_harness.py — Session-manifest email processing harness for nanobot-01.

Architecture:
  Phase 1  build-manifest   — fetch all unread IMAP headers, validate schema, write
                              manifest to /tmp/email_manifest_{account}.json
  Phase 2  (per-email ops)  — read-email, classify-email, act-on-email operate
                              against the manifest; IMAP called only for body fetch
  Phase 3  reconcile        — surface unactioned items, return learnable patterns

Validation gates (every step must pass before next runs):
  list → manifest    : each entry must have uid, from, subject, date
  manifest → read    : uid must exist in manifest and not already actioned
  read → classify    : body must be non-empty
  classify → act     : action must be in ACTIONS_ENUM
  act → update       : IMAP must confirm before marking actioned

Only classify-email calls the LLM (Ollama). All other steps are deterministic.

Commands:
  build-manifest   --account {business|personal} [--limit N]
  list-manifest    --account {business|personal}
  search-manifest  --account {business|personal} [--keyword S] [--sender S]
                   [--status S] [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]
  read-email       --account {business|personal} --uid UID
  classify-email   --account {business|personal} --uid UID [--body TEXT]
  act-on-email     --account {business|personal} --uid UID --action ACTION
                   [--target-folder FOLDER]
  reconcile        --account {business|personal}
  clear-manifest   --account {business|personal}

Output: JSON to stdout.
Errors: {"success": false, "step": "...", "error": "..."} + exit 1.

Env vars (injected by CredentialProxy):
  BUSINESS_IMAP_HOST / PERSONAL_IMAP_HOST
  BUSINESS_IMAP_PORT / PERSONAL_IMAP_PORT  (default 993)
  BUSINESS_IMAP_USER / PERSONAL_IMAP_USER
  BUSINESS_IMAP_PASS / PERSONAL_IMAP_PASS
  OLLAMA_URL  (default http://ollama:11434)
"""

import argparse
import email as email_module
import email.header
import html
import imaplib
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ── Constants ──────────────────────────────────────────────────────────────────

MANIFEST_DIR = Path("/tmp")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
CLASSIFY_MODEL = "llama3.1:8b-instruct-q4_K_M"

ACTIONS_ENUM = frozenset({"archive", "delete", "read", "flag", "reply", "skip"})
MANIFEST_ENTRY_REQUIRED = {"uid", "from", "subject", "date"}

CLASSIFY_PROMPT = """\
You are an email classifier. Read the email and decide what to do with it.

Respond with ONLY a JSON object on one line — no markdown, no explanation:
{{"action": "<archive|delete|read|flag|reply|skip>", "reason": "<one sentence>"}}

Actions:
  archive  safe to archive, no action needed
  delete   spam, promotional, or irrelevant
  read     needs attention but no action; mark as read
  flag     important, follow up required
  reply    needs a response
  skip     uncertain; leave for manual review

From: {from_}
Subject: {subject}
Body:
{body}
"""

# ── Manifest I/O ───────────────────────────────────────────────────────────────

def _manifest_path(account: str) -> Path:
    return MANIFEST_DIR / f"email_manifest_{account}.json"


def _load_manifest(account: str) -> dict:
    p = _manifest_path(account)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"built_at": None, "account": account, "entries": {}, "audit": []}


def _save_manifest(manifest: dict, account: str):
    _manifest_path(account).write_text(json.dumps(manifest, indent=2))


def _audit(manifest: dict, step: str, detail: str):
    manifest["audit"].append({
        "ts": datetime.utcnow().isoformat(),
        "step": step,
        "detail": detail,
    })


# ── IMAP helpers ───────────────────────────────────────────────────────────────

def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email_module.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    text = " ".join(decoded).strip()
    return text.encode("utf-8", "replace").decode("utf-8")


def _strip_html(text: str) -> str:
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _text_body(msg) -> str:
    html_fallback = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                return decoded
            if ct == "text/html" and html_fallback is None:
                html_fallback = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(decoded)
            return decoded
    return _strip_html(html_fallback) if html_fallback else ""


def _connect(account: str):
    prefix = "BUSINESS" if account == "business" else "PERSONAL"
    host = os.environ.get(f"{prefix}_IMAP_HOST", "")
    port = int(os.environ.get(f"{prefix}_IMAP_PORT", "993"))
    user = os.environ.get(f"{prefix}_IMAP_USER", "")
    password = os.environ.get(f"{prefix}_IMAP_PASS", "")
    if not host or not user or not password:
        raise ValueError(
            f"Missing IMAP credentials for account={account!r} "
            f"(need {prefix}_IMAP_HOST / {prefix}_IMAP_USER / {prefix}_IMAP_PASS)"
        )
    use_ssl = (port == 993) or (os.environ.get(f"{prefix}_IMAP_SSL", "").lower() == "true")
    conn = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    conn.login(user, password)
    return conn


def _parse_fetch_response(raw_data):
    results = []
    for item in raw_data:
        if isinstance(item, tuple) and len(item) == 2:
            meta, data = item
            meta_str = meta.decode("latin-1", errors="replace") if isinstance(meta, bytes) else str(meta)
            m = re.search(r"\bUID\s+(\d+)\b", meta_str, re.IGNORECASE)
            uid = m.group(1) if m else None
            results.append((uid or "?", data if isinstance(data, bytes) else b""))
    return results


# ── Validation gates ───────────────────────────────────────────────────────────

def _validate_entry(entry: dict) -> tuple[bool, str]:
    missing = MANIFEST_ENTRY_REQUIRED - entry.keys()
    if missing:
        return False, f"missing fields: {sorted(missing)}"
    if not str(entry.get("uid", "")).strip():
        return False, "uid is empty"
    return True, ""


def _validate_action(action: str) -> tuple[bool, str]:
    if action not in ACTIONS_ENUM:
        return False, f"'{action}' not in {sorted(ACTIONS_ENUM)}"
    return True, ""


# ── LLM classification ─────────────────────────────────────────────────────────

def _classify_via_ollama(from_: str, subject: str, body: str) -> tuple[bool, dict, str]:
    if not _HAS_REQUESTS:
        return False, {}, "requests library not available — cannot call Ollama"
    prompt = CLASSIFY_PROMPT.format(from_=from_, subject=subject, body=body[:2500])
    try:
        resp = _requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": CLASSIFY_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Extract JSON object from response (may have surrounding text)
        m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if not m:
            return False, {}, f"LLM returned no JSON object — raw: {raw[:300]}"
        cls = json.loads(m.group(0))
        ok, err = _validate_action(cls.get("action", ""))
        if not ok:
            return False, {}, f"classification schema error: {err} — raw: {raw[:200]}"
        return True, cls, ""
    except Exception as exc:
        return False, {}, f"Ollama error: {exc}"


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_build_manifest(account: str, limit: int) -> dict:
    """Phase 1: fetch unread headers from IMAP, validate, write manifest."""
    try:
        conn = _connect(account)
    except ValueError as exc:
        return {"success": False, "step": "imap_connect", "error": str(exc)}

    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("SEARCH", None, b"UNSEEN")
        if typ != "OK":
            return {"success": False, "step": "imap_search", "error": f"SEARCH UNSEEN failed: {data}"}

        uid_list = data[0].decode("ascii", errors="replace").split() if data[0] else []
        # Most recent first (highest UIDs), up to limit
        uid_batch = uid_list[-limit:]
        entries = []

        if uid_batch:
            uid_str = ",".join(uid_batch)
            typ, msgs = conn.uid(
                "FETCH", uid_str,
                "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
            )
            if typ != "OK":
                return {"success": False, "step": "imap_fetch_headers", "error": f"FETCH failed: {msgs}"}

            for uid, raw_headers in _parse_fetch_response(msgs):
                msg = email_module.message_from_bytes(raw_headers)
                entries.append({
                    "uid": uid,
                    "from": _decode_header(msg.get("From")),
                    "subject": _decode_header(msg.get("Subject")) or "(no subject)",
                    "date": msg.get("Date", ""),
                    "message_id": msg.get("Message-ID", ""),
                    "status": "unread",
                    "action_taken": None,
                    "classification": None,
                })
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    # Gate: validate every entry has required fields
    errors = []
    for e in entries:
        ok, err = _validate_entry(e)
        if not ok:
            errors.append({"uid": e.get("uid"), "error": err})
    if errors:
        return {
            "success": False,
            "step": "list_to_manifest",
            "error": f"{len(errors)} entries failed schema validation",
            "validation_errors": errors,
        }

    entries.reverse()  # newest first
    manifest = {
        "built_at": datetime.utcnow().isoformat(),
        "account": account,
        "entries": {e["uid"]: e for e in entries},
        "audit": [],
    }
    _audit(manifest, "build_manifest", f"{len(entries)} unread emails fetched (limit={limit})")
    _save_manifest(manifest, account)

    return {
        "success": True,
        "count": len(entries),
        "entries": [
            {"uid": e["uid"], "from": e["from"], "subject": e["subject"], "date": e["date"]}
            for e in entries
        ],
    }


def cmd_list_manifest(account: str) -> dict:
    manifest = _load_manifest(account)
    if not manifest["built_at"]:
        return {"success": False, "step": "list_manifest", "error": "no manifest — call build-manifest first"}
    entries = list(manifest["entries"].values())
    return {
        "success": True,
        "built_at": manifest["built_at"],
        "count": len(entries),
        "entries": [
            {
                "uid": e["uid"], "from": e["from"], "subject": e["subject"],
                "date": e["date"], "status": e.get("status", "unread"),
                "action_taken": e.get("action_taken"),
                "classification": e.get("classification"),
            }
            for e in entries
        ],
    }


def cmd_search_manifest(account: str, keyword: str, sender: str,
                        status: str, date_from: str, date_to: str) -> dict:
    """In-RAM search — no IMAP calls."""
    manifest = _load_manifest(account)
    if not manifest["built_at"]:
        return {"success": False, "step": "search_manifest", "error": "no manifest — call build-manifest first"}

    kw = keyword.lower()
    snd = sender.lower()

    df = None
    dt = None
    try:
        if date_from:
            df = datetime.strptime(date_from, "%Y-%m-%d").date()
        if date_to:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        return {"success": False, "step": "search_manifest", "error": f"date parse error: {exc}"}

    results = []
    for e in manifest["entries"].values():
        if kw and kw not in (e["subject"] + " " + e["from"]).lower():
            continue
        if snd and snd not in e["from"].lower():
            continue
        if status and e.get("status") != status:
            continue
        if df or dt:
            # Try to parse entry date for comparison (best-effort)
            try:
                import email.utils
                ts = email.utils.parsedate_to_datetime(e["date"]).date()
                if df and ts < df:
                    continue
                if dt and ts > dt:
                    continue
            except Exception:
                pass  # skip date filter if unparseable
        results.append(e)

    return {
        "success": True,
        "count": len(results),
        "entries": [
            {
                "uid": e["uid"], "from": e["from"], "subject": e["subject"],
                "date": e["date"], "status": e.get("status", "unread"),
                "action_taken": e.get("action_taken"),
            }
            for e in results
        ],
    }


def cmd_read_email(account: str, uid: str) -> dict:
    uid = uid.strip()
    manifest = _load_manifest(account)

    # Gate: uid must exist in manifest
    if uid not in manifest["entries"]:
        return {
            "success": False,
            "step": "manifest_to_read",
            "error": f"uid '{uid}' not in manifest — call build-manifest first",
        }

    entry = manifest["entries"][uid]

    # Gate: not already actioned
    if entry.get("status") == "actioned":
        return {
            "success": False,
            "step": "manifest_to_read",
            "error": f"uid '{uid}' already actioned ({entry.get('action_taken')}) — no re-read needed",
        }

    try:
        conn = _connect(account)
    except ValueError as exc:
        return {"success": False, "step": "imap_connect", "error": str(exc)}

    try:
        conn.select("INBOX", readonly=True)
        typ, msgs = conn.uid("FETCH", uid, "(FLAGS BODY[])")
        if typ != "OK":
            return {"success": False, "step": "imap_fetch_body", "error": f"FETCH body failed: {msgs}"}

        pairs = _parse_fetch_response(msgs)
        if not pairs:
            return {"success": False, "step": "imap_fetch_body", "error": f"uid '{uid}' not found in IMAP"}

        found_uid, raw = pairs[0]
        msg = email_module.message_from_bytes(raw)
        body = _text_body(msg)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    # Gate: body must be non-empty
    if not body.strip():
        return {
            "success": False,
            "step": "read_to_classify",
            "error": f"uid '{uid}' body is empty or unreadable",
        }

    entry["status"] = "read"
    entry["body_preview"] = body[:500]
    _audit(manifest, "read_email", f"uid={uid} subject={entry['subject'][:60]!r}")
    _save_manifest(manifest, account)

    return {
        "success": True,
        "uid": uid,
        "from": entry["from"],
        "subject": entry["subject"],
        "date": entry["date"],
        "body": body[:8000],
        "body_truncated": len(body) > 8000,
    }


def cmd_classify_email(account: str, uid: str, body: str) -> dict:
    uid = uid.strip()
    manifest = _load_manifest(account)

    # Gate: uid must exist in manifest
    if uid not in manifest["entries"]:
        return {
            "success": False,
            "step": "manifest_to_classify",
            "error": f"uid '{uid}' not in manifest",
        }

    entry = manifest["entries"][uid]

    # Gate: body required (use passed body, fall back to stored preview)
    body = body.strip()
    if not body:
        body = entry.get("body_preview", "").strip()
    if not body:
        return {
            "success": False,
            "step": "read_to_classify",
            "error": f"uid '{uid}' has no body — call read-email first",
        }

    ok, cls, err = _classify_via_ollama(entry["from"], entry["subject"], body)
    if not ok:
        _audit(manifest, "classify_email", f"FAIL uid={uid} err={err[:120]}")
        _save_manifest(manifest, account)
        return {"success": False, "step": "classify_to_act", "error": err}

    entry["classification"] = cls
    _audit(manifest, "classify_email", f"uid={uid} action={cls['action']} reason={cls.get('reason','')[:60]!r}")
    _save_manifest(manifest, account)

    return {
        "success": True,
        "uid": uid,
        "from": entry["from"],
        "subject": entry["subject"],
        "classification": cls,
    }


def cmd_act_on_email(account: str, uid: str, action: str, target_folder: str) -> dict:
    uid = uid.strip()
    action = action.strip().lower()

    # Gate: action must be in enum
    ok, err = _validate_action(action)
    if not ok:
        return {"success": False, "step": "classify_to_act", "error": err}

    manifest = _load_manifest(account)

    # Gate: uid must exist in manifest
    if uid not in manifest["entries"]:
        return {
            "success": False,
            "step": "manifest_to_act",
            "error": f"uid '{uid}' not in manifest",
        }

    entry = manifest["entries"][uid]

    # No-ops: skip and reply don't touch IMAP
    if action == "skip":
        entry["status"] = "actioned"
        entry["action_taken"] = "skip"
        _audit(manifest, "act_on_email", f"uid={uid} action=skip")
        _save_manifest(manifest, account)
        return {"success": True, "uid": uid, "action": "skip", "confirmation": "skipped (no IMAP change)"}

    if action == "reply":
        # Reply payload goes back to sovereign-core for SMTP — mark for reply, not fully actioned
        entry["status"] = "actioned"
        entry["action_taken"] = "reply_pending"
        _audit(manifest, "act_on_email", f"uid={uid} action=reply_pending")
        _save_manifest(manifest, account)
        return {
            "success": True,
            "uid": uid,
            "action": "reply_pending",
            "from": entry["from"],
            "subject": entry["subject"],
            "confirmation": "flagged for reply — use send_email to respond",
        }

    # All other actions need IMAP
    try:
        conn = _connect(account)
    except ValueError as exc:
        return {"success": False, "step": "imap_connect", "error": str(exc)}

    try:
        confirmation = _execute_imap_action(conn, uid, action, target_folder)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    if not confirmation["success"]:
        _audit(manifest, "act_on_email", f"FAIL uid={uid} action={action} err={confirmation['error'][:80]}")
        _save_manifest(manifest, account)
        return {
            "success": False,
            "step": "act_to_manifest_update",
            "error": confirmation["error"],
        }

    # Gate: IMAP confirmed before marking actioned
    entry["status"] = "actioned"
    entry["action_taken"] = action
    _audit(manifest, "act_on_email", f"uid={uid} action={action} confirmation={confirmation['detail']}")
    _save_manifest(manifest, account)

    return {
        "success": True,
        "uid": uid,
        "action": action,
        "confirmation": confirmation["detail"],
    }


def _execute_imap_action(conn, uid: str, action: str, target_folder: str) -> dict:
    conn.select("INBOX", readonly=False)

    if action == "read":
        typ, data = conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        if typ != "OK":
            return {"success": False, "error": f"STORE +FLAGS \\Seen failed: {data}"}
        return {"success": True, "detail": "marked as read"}

    if action == "flag":
        typ, data = conn.uid("STORE", uid, "+FLAGS", r"(\Flagged)")
        if typ != "OK":
            return {"success": False, "error": f"STORE +FLAGS \\Flagged failed: {data}"}
        return {"success": True, "detail": "flagged"}

    if action == "delete":
        typ, data = conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        if typ != "OK":
            return {"success": False, "error": f"STORE +FLAGS \\Deleted failed: {data}"}
        conn.expunge()
        return {"success": True, "detail": "deleted and expunged"}

    if action == "archive":
        # Try standard archive folder names
        archive_candidates = target_folder.split(",") if target_folder else []
        archive_candidates += ["INBOX.Archive", "Archive", "INBOX.Archived", "Archived"]
        for folder in archive_candidates:
            folder = folder.strip()
            folder_arg = f'"{folder}"' if " " in folder else folder
            typ, data = conn.uid("COPY", uid, folder_arg)
            if typ == "OK":
                conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                conn.expunge()
                return {"success": True, "detail": f"archived to {folder}"}
        return {"success": False, "error": f"no archive folder found (tried: {archive_candidates[:4]})"}

    return {"success": False, "error": f"unhandled action: {action}"}


def cmd_reconcile(account: str) -> dict:
    """Phase 3: surface unactioned items, return learnable patterns."""
    manifest = _load_manifest(account)
    if not manifest["built_at"]:
        return {"success": False, "step": "reconcile", "error": "no manifest for this session"}

    entries = list(manifest["entries"].values())
    actioned = [e for e in entries if e.get("status") == "actioned"]
    unactioned = [e for e in entries if e.get("status") != "actioned"]

    # Action count summary
    action_counts: dict[str, int] = {}
    for e in actioned:
        act = e.get("action_taken") or "unknown"
        action_counts[act] = action_counts.get(act, 0) + 1

    # Learnable patterns: sender domain → action where count ≥ 2
    sender_action: dict[tuple, int] = {}
    for e in actioned:
        sender = e.get("from", "")
        m = re.search(r'@([\w.\-]+)', sender)
        domain = m.group(1) if m else sender
        act = e.get("action_taken") or "unknown"
        key = (domain, act)
        sender_action[key] = sender_action.get(key, 0) + 1

    patterns = [
        {"sender_domain": k[0], "action": k[1], "count": v}
        for k, v in sorted(sender_action.items(), key=lambda x: -x[1])
        if v >= 2
    ]

    return {
        "success": True,
        "session_built_at": manifest["built_at"],
        "total": len(entries),
        "actioned_count": len(actioned),
        "unactioned_count": len(unactioned),
        "action_counts": action_counts,
        "unactioned": [
            {"uid": e["uid"], "from": e["from"], "subject": e["subject"], "date": e["date"]}
            for e in unactioned
        ],
        "learnable_patterns": patterns,
        "audit_entries": len(manifest.get("audit", [])),
    }


def cmd_clear_manifest(account: str) -> dict:
    p = _manifest_path(account)
    if p.exists():
        p.unlink()
        return {"success": True, "message": f"manifest cleared for account={account}"}
    return {"success": True, "message": "no manifest to clear"}


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Email session harness")
    parser.add_argument("--command", required=True, choices=[
        "build-manifest", "list-manifest", "search-manifest",
        "read-email", "classify-email", "act-on-email",
        "reconcile", "clear-manifest",
    ])
    parser.add_argument("--account", default="business", choices=["business", "personal"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--uid", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--action", default="")
    parser.add_argument("--target-folder", default="")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--sender", default="")
    parser.add_argument("--status", default="", choices=["", "unread", "read", "actioned"])
    parser.add_argument("--date-from", default="")
    parser.add_argument("--date-to", default="")
    args = parser.parse_args()

    try:
        if args.command == "build-manifest":
            result = cmd_build_manifest(args.account, args.limit)
        elif args.command == "list-manifest":
            result = cmd_list_manifest(args.account)
        elif args.command == "search-manifest":
            result = cmd_search_manifest(
                args.account, args.keyword, args.sender,
                args.status, args.date_from, args.date_to,
            )
        elif args.command == "read-email":
            if not args.uid.strip():
                result = {"success": False, "step": "pre_flight", "error": "--uid is required for read-email"}
            else:
                result = cmd_read_email(args.account, args.uid)
        elif args.command == "classify-email":
            if not args.uid.strip():
                result = {"success": False, "step": "pre_flight", "error": "--uid is required for classify-email"}
            else:
                result = cmd_classify_email(args.account, args.uid, args.body)
        elif args.command == "act-on-email":
            if not args.uid.strip():
                result = {"success": False, "step": "pre_flight", "error": "--uid is required for act-on-email"}
            elif not args.action.strip():
                result = {"success": False, "step": "pre_flight", "error": "--action is required for act-on-email"}
            else:
                result = cmd_act_on_email(args.account, args.uid, args.action, args.target_folder)
        elif args.command == "reconcile":
            result = cmd_reconcile(args.account)
        elif args.command == "clear-manifest":
            result = cmd_clear_manifest(args.account)
        else:
            result = {"success": False, "step": "dispatch", "error": f"unknown command: {args.command}"}
    except Exception as exc:
        import traceback
        result = {
            "success": False,
            "step": "unhandled_exception",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1000:],
        }

    print(json.dumps(result))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
