#!/usr/bin/env python3
"""Nextcloud Mail REST API operations for nanobot-01 python3_exec dispatch.

Commands:
  list_unread      -- GET /api/messages?mailboxId={id}&limit={n}  (filters to unread)
  fetch_message    -- GET /api/messages/{id}/body, or search by from_addr/subject
  delete_message   -- DELETE /api/thread/{id}
  move_message     -- POST /api/messages/{id}/move {destFolderId}
  mark_read        -- PUT /api/messages/{id}/flags {"flags":{"seen":true}}
  mark_unread      -- PUT /api/messages/{id}/flags {"flags":{"seen":false}}
  send             -- POST /api/outbox
  list_mailboxes   -- GET /api/mailboxes?accountId={id}
  list_accounts    -- GET /api/accounts

Env vars (from nextcloud.env via CredentialProxy):
  NEXTCLOUD_URL            (default: http://nextcloud)
  NEXTCLOUD_ADMIN_USER     (e.g. digiant)
  NEXTCLOUD_ADMIN_PASSWORD

Account IDs (confirmed from /api/accounts):
  1 = business  (matt@digiant.co.nz)
  2 = personal  (matt.hoare@e.email)

Output: JSON to stdout.  Errors: {"status":"error","error":"..."} + exit 1.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_NC_URL  = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NC_USER = os.environ.get("NEXTCLOUD_ADMIN_USER", "digiant")
_NC_PASS = os.environ.get("NEXTCLOUD_ADMIN_PASSWORD", "")

_MAIL_API = f"{_NC_URL}/apps/mail/api"

# Account name → NC Mail account ID
_ACCOUNT_IDS = {"business": 1, "personal": 2}

# Known INBOX databaseIds (confirmed 2026-03-21 via /api/mailboxes)
# Falls back to API discovery if not in cache.
_INBOX_CACHE = {1: 1, 2: 19}


def _auth():
    return HTTPBasicAuth(_NC_USER, _NC_PASS)


def _hdr(extra=None):
    h = {
        "OCS-APIREQUEST": "true",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _ok(data):
    print(json.dumps({"status": "ok", **data}))


def _err(msg):
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account_id(account):
    """Resolve 'business'/'personal' string or integer string to NC account ID."""
    try:
        return int(account)
    except (TypeError, ValueError):
        pass
    return _ACCOUNT_IDS.get(str(account or "").lower().strip(), 1)


def _from_str(from_field):
    """Format NC Mail from list [{label, email}] → 'Label <email>'."""
    if not from_field:
        return "unknown"
    f = from_field[0] if isinstance(from_field, list) else from_field
    if isinstance(f, dict):
        label = f.get("label", "").strip()
        email = f.get("email", "").strip()
        return f"{label} <{email}>" if label else email
    return str(f)


def _get_account_mailboxes(account_id):
    """Return a dict mapping lowercase role/name → databaseId for the given account."""
    try:
        r = requests.get(
            f"{_MAIL_API}/mailboxes",
            params={"accountId": account_id},
            auth=_auth(), headers=_hdr(), timeout=20,
        )
    except Exception:
        return {}
    if r.status_code != 200:
        return {}
    data = r.json()
    mailboxes = data.get("mailboxes", []) if isinstance(data, dict) else []
    mb_map = {}
    for mb in mailboxes:
        db_id = mb.get("databaseId")
        if not db_id:
            continue
        role = (mb.get("specialRole") or "").lower().strip()
        if role:
            mb_map[role] = db_id
        name = mb.get("displayName") or mb.get("name") or ""
        mb_map[name.lower()] = db_id
    return mb_map


def _get_inbox_id(account_id):
    """Return the INBOX databaseId for the given account.
    Uses cache for known accounts; falls back to API discovery.
    """
    if account_id in _INBOX_CACHE:
        return _INBOX_CACHE[account_id]
    mb_map = _get_account_mailboxes(account_id)
    return mb_map.get("inbox")


def _fmt_date(ts):
    """Convert Unix timestamp to 'D Mon YYYY' string for display."""
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return f"{dt.day} {dt.strftime('%b %Y')}"
    except (TypeError, ValueError, OSError):
        return str(ts)


def _strip_html(html):
    """Strip HTML tags; collapse whitespace for readable plain text."""
    if not html or "<" not in html:
        return html or ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list_unread(args):
    account_id = _account_id(args.account)
    inbox_id = _get_inbox_id(account_id)
    if not inbox_id:
        _err(f"Could not find INBOX for account {args.account!r}")

    # Nextcloud Mail API is slow (~10-40s per request due to IMAP sync).
    # When filtering, fetch more to increase match probability. Cap personal at 10 to avoid timeout.
    if args.filter:
        fetch_limit = min(args.limit * 3, 30) if account_id == 1 else min(args.limit, 10)
    else:
        fetch_limit = min(args.limit, 10)
    params = {"mailboxId": inbox_id, "limit": fetch_limit}
    if args.filter:
        params["filter"] = args.filter

    try:
        r = requests.get(
            f"{_MAIL_API}/messages",
            params=params, auth=_auth(), headers=_hdr(), timeout=57,
        )
    except requests.exceptions.Timeout:
        print(json.dumps({"status": "ok", "messages": [], "count": 0,
                          "note": f"{args.account} inbox is slow to sync — please try again in a moment"}))
        sys.exit(0)
    if r.status_code != 200:
        _err(f"list_unread HTTP {r.status_code}: {r.text[:200]}")

    msgs_raw = r.json()
    if not isinstance(msgs_raw, list):
        msgs_raw = []

    filter_lower = args.filter.lower() if args.filter else ""
    messages = []
    for msg in msgs_raw:
        if args.unread_only and msg.get("flags", {}).get("seen"):
            continue
        from_str = _from_str(msg.get("from", []))
        subject  = msg.get("subject", "")
        # Client-side filter: NC Mail API ignores filter param for search
        if filter_lower and filter_lower not in from_str.lower() and filter_lower not in subject.lower():
            continue
        messages.append({
            "databaseId": msg.get("databaseId"),
            "from":       from_str,
            "subject":    subject,
            "date":       _fmt_date(msg.get("dateInt")),
            "seen":       bool(msg.get("flags", {}).get("seen")),
        })
        if len(messages) >= args.limit:
            break

    _ok({"messages": messages, "count": len(messages), "account_id": account_id})


def cmd_fetch_message(args):
    account_id = _account_id(args.account)
    database_id = args.database_id

    if not database_id:
        # Search by from_addr or subject
        if not args.from_addr and not args.subject:
            _err("fetch_message requires --database_id, --from_addr, or --subject")
        inbox_id = _get_inbox_id(account_id)
        if not inbox_id:
            _err(f"Could not find INBOX for account {args.account!r}")

        flt = args.subject or args.from_addr
        r = requests.get(
            f"{_MAIL_API}/messages",
            params={"mailboxId": inbox_id, "limit": 20, "filter": flt},
            auth=_auth(), headers=_hdr(), timeout=20,
        )
        if r.status_code != 200:
            _err(f"Message search HTTP {r.status_code}: {r.text[:200]}")

        msgs_raw = r.json()
        if not isinstance(msgs_raw, list):
            msgs_raw = []

        found = None
        for msg in msgs_raw:
            from_list  = msg.get("from", [])
            from_email = from_list[0].get("email", "") if from_list else ""
            from_label = from_list[0].get("label", "") if from_list else ""
            subj = msg.get("subject", "")
            if args.from_addr and (
                args.from_addr.lower() in from_email.lower()
                or args.from_addr.lower() in from_label.lower()
            ):
                found = msg
                break
            if args.subject and args.subject.lower() in subj.lower():
                found = msg
                break

        if not found:
            _err("Message not found matching search criteria")
        database_id = found["databaseId"]

    # Fetch full body via /body sub-endpoint
    r = requests.get(
        f"{_MAIL_API}/messages/{database_id}/body",
        auth=_auth(), headers=_hdr(), timeout=20,
    )
    if r.status_code != 200:
        _err(f"fetch_message body HTTP {r.status_code}: {r.text[:200]}")

    msg = r.json()
    body_raw = msg.get("body", "")
    body_text = _strip_html(body_raw)[:4000]  # cap at 4000 chars

    _ok({
        "databaseId": database_id,
        "from":    _from_str(msg.get("from", [])),
        "subject": msg.get("subject", ""),
        "date":    _fmt_date(msg.get("dateInt")),
        "body":    body_text,
        "seen":    bool((msg.get("flags") or {}).get("seen")),
    })


def cmd_delete_message(args):
    database_id = args.database_id

    if not database_id:
        # Resolve by from_addr/subject search
        if not args.from_addr and not args.subject:
            _err("delete_message requires --database_id, --from_addr, or --subject")
        account_id = _account_id(args.account)
        inbox_id   = _get_inbox_id(account_id)
        if not inbox_id:
            _err(f"Could not find INBOX for account {args.account!r}")

        flt = args.subject or args.from_addr
        r = requests.get(
            f"{_MAIL_API}/messages",
            params={"mailboxId": inbox_id, "limit": 10, "filter": flt},
            auth=_auth(), headers=_hdr(), timeout=15,
        )
        if r.status_code != 200:
            _err(f"Delete search HTTP {r.status_code}: {r.text[:200]}")

        msgs_raw = r.json()
        if not isinstance(msgs_raw, list):
            msgs_raw = []

        for msg in msgs_raw:
            from_list  = msg.get("from", [])
            from_email = from_list[0].get("email", "") if from_list else ""
            subj = msg.get("subject", "")
            if args.from_addr and args.from_addr.lower() in from_email.lower():
                database_id = msg["databaseId"]
                break
            if args.subject and args.subject.lower() in subj.lower():
                database_id = msg["databaseId"]
                break

        if not database_id:
            _err("No message found matching delete criteria")

    r = requests.delete(
        f"{_MAIL_API}/thread/{database_id}",
        auth=_auth(), headers=_hdr(), timeout=15,
    )
    if r.status_code not in (200, 202, 204):
        _err(f"delete_message HTTP {r.status_code}: {r.text[:200]}")

    _ok({"databaseId": database_id, "action": "deleted"})


def cmd_move_message(args):
    if not args.database_id:
        _err("move_message requires --database_id")

    account_id = _account_id(args.account)
    mb_map = _get_account_mailboxes(account_id)

    # Resolve dest_folder name → mailbox databaseId
    dest = (args.dest_folder or "archive").lower().strip()
    dest_id = (
        mb_map.get(dest)                          # exact specialRole match ("archive","trash")
        or mb_map.get(f"inbox.{dest}")            # INBOX.Archive style
        or mb_map.get(f"inbox.{dest}e")           # common INBOX.Archiv typo
    )
    if not dest_id:
        # Fuzzy: any mailbox name containing the target string
        for k, v in mb_map.items():
            if dest in k:
                dest_id = v
                break

    if not dest_id:
        _err(
            f"Could not resolve mailbox {args.dest_folder!r} for account {args.account!r}. "
            f"Available keys: {sorted(mb_map.keys())}"
        )

    r = requests.post(
        f"{_MAIL_API}/messages/{args.database_id}/move",
        json={"destFolderId": dest_id},
        auth=_auth(), headers=_hdr(), timeout=15,
    )
    if r.status_code not in (200, 202, 204):
        _err(f"move_message HTTP {r.status_code}: {r.text[:200]}")

    _ok({"databaseId": args.database_id, "dest_mailbox_id": dest_id, "action": "moved"})


def cmd_mark_read(args):
    if not args.database_id:
        _err("mark_read requires --database_id")
    r = requests.put(
        f"{_MAIL_API}/messages/{args.database_id}/flags",
        json={"flags": {"seen": True}},
        auth=_auth(), headers=_hdr(), timeout=10,
    )
    if r.status_code not in (200, 202, 204):
        _err(f"mark_read HTTP {r.status_code}: {r.text[:200]}")
    _ok({"databaseId": args.database_id, "seen": True})


def cmd_mark_unread(args):
    if not args.database_id:
        _err("mark_unread requires --database_id")
    r = requests.put(
        f"{_MAIL_API}/messages/{args.database_id}/flags",
        json={"flags": {"seen": False}},
        auth=_auth(), headers=_hdr(), timeout=10,
    )
    if r.status_code not in (200, 202, 204):
        _err(f"mark_unread HTTP {r.status_code}: {r.text[:200]}")
    _ok({"databaseId": args.database_id, "seen": False})


def cmd_send(args):
    account_id = _account_id(args.account)

    # Parse to field: 'Name <email@x.com>', 'Name,email@x.com', or bare email
    to_raw = (args.to or "").strip()
    m_angle = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>$', to_raw)
    if m_angle:
        to_label, to_email = m_angle.group(1).strip(), m_angle.group(2).strip()
    elif "," in to_raw and "@" in to_raw.split(",", 1)[1]:
        parts = to_raw.split(",", 1)
        to_label, to_email = parts[0].strip(), parts[1].strip()
    else:
        to_label, to_email = to_raw, to_raw

    if not to_email or "@" not in to_email:
        _err(f"send requires a valid --to address; got {to_raw!r}")

    payload = {
        "accountId":    account_id,
        "subject":      args.subject or "",
        "bodyPlain":    args.body or "",
        "bodyHtml":     None,
        "editorBody":   None,
        "isHtml":       False,
        "smimeSign":    False,
        "smimeEncrypt": False,
        "to":           [{"label": to_label or to_email, "email": to_email}],
        "cc":           [],
        "bcc":          [],
        "attachments":  [],
        "sendAt":       int(time.time()),   # send immediately
    }

    r = requests.post(
        f"{_MAIL_API}/outbox",
        json=payload, auth=_auth(), headers=_hdr(), timeout=20,
    )
    if r.status_code not in (200, 201):
        _err(f"send HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    msg_id = (data.get("data") or {}).get("id") or data.get("id")
    _ok({"outbox_id": msg_id, "to": to_email, "subject": args.subject, "queued": True})


def cmd_list_mailboxes(args):
    account_id = _account_id(args.account)
    r = requests.get(
        f"{_MAIL_API}/mailboxes",
        params={"accountId": account_id},
        auth=_auth(), headers=_hdr(), timeout=15,
    )
    if r.status_code != 200:
        _err(f"list_mailboxes HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    mailboxes = data.get("mailboxes", []) if isinstance(data, dict) else []
    mbs = [
        {
            "databaseId":  mb["databaseId"],
            "name":        mb.get("displayName") or mb.get("name", ""),
            "specialRole": mb.get("specialRole") or "",
            "unread":      mb.get("unread", 0),
        }
        for mb in mailboxes
        if mb.get("databaseId")
    ]
    _ok({"mailboxes": mbs, "count": len(mbs), "account_id": account_id})


def cmd_list_accounts(_args):
    r = requests.get(
        f"{_MAIL_API}/accounts",
        auth=_auth(), headers=_hdr(), timeout=10,
    )
    if r.status_code != 200:
        _err(f"list_accounts HTTP {r.status_code}: {r.text[:200]}")
    accounts = r.json()
    if not isinstance(accounts, list):
        _err(f"Unexpected accounts response: {str(accounts)[:200]}")
    _ok({
        "accounts": [
            {"id": a["id"], "email": a.get("emailAddress", "")}
            for a in accounts
        ]
    })


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_COMMANDS = {
    "list_unread":     cmd_list_unread,
    "fetch_message":   cmd_fetch_message,
    "delete_message":  cmd_delete_message,
    "move_message":    cmd_move_message,
    "mark_read":       cmd_mark_read,
    "mark_unread":     cmd_mark_unread,
    "send":            cmd_send,
    "list_mailboxes":  cmd_list_mailboxes,
    "list_accounts":   cmd_list_accounts,
}


def main():
    p = argparse.ArgumentParser(description="Nextcloud Mail REST API client for nanobot-01")
    p.add_argument("--command",     required=True, choices=list(_COMMANDS))
    p.add_argument("--account",     default="business", help="business or personal")
    p.add_argument("--database_id", default="",         help="NC Mail message databaseId")
    p.add_argument("--from_addr",   default="",         help="Sender address for search/fetch/delete")
    p.add_argument("--subject",     default="",         help="Subject for search/fetch/delete/send")
    p.add_argument("--limit",       type=int, default=10, help="Max messages to return")
    p.add_argument("--filter",      default="",         help="Text search filter for list_unread")
    p.add_argument("--unread_only", default="false",    help="true/false — show only unread messages")
    p.add_argument("--dest_folder", default="Archive",  help="Destination folder name for move_message")
    p.add_argument("--to",          default="",         help="Recipient for send: 'Name <email>' or plain email")
    p.add_argument("--body",        default="",         help="Plain-text email body for send")
    p.add_argument("--cc",          default="",         help="CC address for send (currently unused)")
    args = p.parse_args()

    # Normalise boolean string → bool
    args.unread_only = args.unread_only.strip().lower() in ("true", "1", "yes")

    # Normalise database_id → int or None
    try:
        args.database_id = int(args.database_id) if args.database_id else None
    except ValueError:
        args.database_id = None

    _COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
