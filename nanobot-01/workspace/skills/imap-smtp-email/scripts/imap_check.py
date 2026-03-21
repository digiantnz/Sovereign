#!/usr/bin/env python3
"""IMAP email operations for nanobot-01 python3_exec dispatch.

Commands:
  check          -- List messages SEARCH ALL (newest first, limit N)
  fetch          -- Fetch full message by UID (headers + body)
  search         -- Search by text/from/subject
  mark-read      -- Add \\Seen flag to UID
  mark-unread    -- Remove \\Seen flag from UID
  list-mailboxes -- List all IMAP folders

Env vars (Phase 1 static mounts via compose.yml env_file):
  BUSINESS_IMAP_HOST / PERSONAL_IMAP_HOST
  BUSINESS_IMAP_PORT / PERSONAL_IMAP_PORT  (default 993)
  BUSINESS_IMAP_USER / PERSONAL_IMAP_USER
  BUSINESS_IMAP_PASS / PERSONAL_IMAP_PASS

Output: JSON to stdout. Errors: {"status":"error","error":"..."} + exit 1.
"""

import argparse
import email
import email.header
import html
import imaplib
import json
import os
import re
import sys


def _decode_header(value):
    """Decode a MIME-encoded header value to plain UTF-8 string.

    Strips surrogate characters that appear in some emoji-containing subjects
    and would cause JSON serialisation failures or raw unicode escape output.
    """
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    text = " ".join(decoded).strip()
    # Remove surrogate characters (lone surrogates from malformed UTF-16 email subjects)
    return text.encode("utf-8", "replace").decode("utf-8")


def _strip_html(text):
    """Strip HTML tags and decode entities for plain-text fallback."""
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _text_body(msg):
    """Extract plain-text body from an email.message.Message.

    Prefers text/plain. If absent, strips HTML from text/html part.
    """
    html_fallback = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
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

    if html_fallback:
        return _strip_html(html_fallback)
    return ""


def _connect(account):
    """Connect and authenticate to IMAP server for the given account."""
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

    # SSL for port 993, plain IMAP for others (e.g. ProtonMail Bridge on 1143)
    use_ssl = (port == 993) or (os.environ.get(f"{prefix}_IMAP_SSL", "").lower() == "true")
    if use_ssl:
        mail = imaplib.IMAP4_SSL(host, port)
    else:
        mail = imaplib.IMAP4(host, port)
    mail.login(user, password)
    return mail


def _extract_uid(meta):
    """Extract UID from a FETCH meta line (e.g. b'1 (UID 42 FLAGS ...')."""
    if isinstance(meta, bytes):
        meta = meta.decode("latin-1", errors="replace")
    m = re.search(r"\bUID\s+(\d+)\b", meta, re.IGNORECASE)
    return m.group(1) if m else None


def _parse_fetch_response(raw_data):
    """Parse imaplib FETCH response into list of (uid_str, data_bytes) pairs."""
    results = []
    for item in raw_data:
        if isinstance(item, tuple) and len(item) == 2:
            meta, data = item
            uid = _extract_uid(meta)
            results.append((uid or "?", data if isinstance(data, bytes) else b""))
    return results


def cmd_check(mail, mailbox, limit, unseen):
    """SEARCH ALL (or UNSEEN), return newest N messages with headers."""
    typ, _ = mail.select(mailbox, readonly=True)
    if typ != "OK":
        return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    criteria = b"UNSEEN" if unseen else b"ALL"
    typ, data = mail.uid("SEARCH", None, criteria)
    if typ != "OK":
        return {"status": "error", "error": f"SEARCH failed: {data}"}

    uid_list = data[0].decode("ascii", errors="replace").split() if data[0] else []
    if not uid_list:
        return {"status": "ok", "messages": [], "count": 0}

    # Take highest UIDs = newest messages
    uid_batch = uid_list[-limit:]
    uid_str = ",".join(uid_batch)

    typ, msgs = mail.uid(
        "FETCH", uid_str,
        "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
    )
    if typ != "OK":
        return {"status": "error", "error": f"FETCH headers failed: {msgs}"}

    messages = []
    for uid, raw_headers in _parse_fetch_response(msgs):
        msg = email.message_from_bytes(raw_headers)
        messages.append({
            "uid": uid,
            "from": _decode_header(msg.get("From")),
            "subject": _decode_header(msg.get("Subject")) or "(no subject)",
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
        })

    messages.reverse()  # newest first
    return {"status": "ok", "messages": messages, "count": len(messages)}


def cmd_fetch(mail, mailbox, uid, from_addr="", subject=""):
    """Fetch full message by UID — returns headers + text body.

    If uid is empty but from_addr or subject is provided, search for the most
    recent matching message first, then fetch its body (search-then-fetch).
    """
    uid = (uid or "").strip()

    # Search-then-fetch: resolve UID from from_addr / subject when uid absent
    if not uid:
        if not from_addr and not subject:
            return {"status": "error", "step": "uid_guard",
                    "error": "fetch: uid is required (or provide from_addr/subject to search)"}

        typ, _ = mail.select(mailbox, readonly=True)
        if typ != "OK":
            return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

        def _imap_quote(s):
            """Wrap value in double-quotes for IMAP SEARCH if it contains spaces."""
            s = s.strip()
            if " " in s and not (s.startswith('"') and s.endswith('"')):
                return f'"{s}"'
            return s

        def _search(crit):
            t, d = mail.uid("SEARCH", None, *crit)
            if t != "OK":
                return []
            return d[0].decode("ascii", errors="replace").split() if d[0] else []

        criteria = []
        if from_addr:
            criteria += ["FROM", _imap_quote(from_addr)]
        if subject:
            criteria += ["SUBJECT", _imap_quote(subject)]

        uid_list = _search(criteria)

        # Fallback 1: if combined criteria failed and subject was given but not from_addr,
        # try treating subject value as a FROM search (handles LLM misclassifying sender as subject)
        if not uid_list and subject and not from_addr:
            uid_list = _search(["FROM", _imap_quote(subject)])

        # Fallback 2: if from_addr + subject combined found nothing, try each independently
        if not uid_list and from_addr and subject:
            uid_list = _search(["FROM", _imap_quote(from_addr)])
            if not uid_list:
                uid_list = _search(["SUBJECT", _imap_quote(subject)])

        if not uid_list:
            return {"status": "error",
                    "error": "No message found matching the search criteria"}

        # Take the most recent (highest UID)
        uid = uid_list[-1]

    typ, _ = mail.select(mailbox, readonly=True)
    if typ != "OK":
        return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    typ, msgs = mail.uid("FETCH", uid, "(FLAGS BODY[])")
    if typ != "OK":
        return {"status": "error", "error": f"FETCH body failed: {msgs}"}

    pairs = _parse_fetch_response(msgs)
    if not pairs:
        return {"status": "error", "error": f"UID {uid} not found"}

    found_uid, raw_body = pairs[0]
    msg = email.message_from_bytes(raw_body)
    body_text = _text_body(msg)

    return {
        "status": "ok",
        "uid": found_uid,
        "from": _decode_header(msg.get("From")),
        "subject": _decode_header(msg.get("Subject")) or "(no subject)",
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        "body": body_text[:8000],
        "body_truncated": len(body_text) > 8000,
    }


def cmd_search(mail, mailbox, query, from_addr, subject, since, limit):
    """Search messages by text, from, subject, or date."""
    typ, _ = mail.select(mailbox, readonly=True)
    if typ != "OK":
        return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    def _q(s):
        s = s.strip()
        return f'"{s}"' if " " in s and not (s.startswith('"') and s.endswith('"')) else s

    criteria = []
    if query:
        criteria += ["TEXT", _q(query)]
    if from_addr:
        criteria += ["FROM", _q(from_addr)]
    if subject:
        criteria += ["SUBJECT", _q(subject)]
    if since:
        try:
            from datetime import datetime
            dt = datetime.strptime(since, "%Y-%m-%d")
            criteria += ["SINCE", dt.strftime("%d-%b-%Y")]
        except ValueError:
            criteria += ["SINCE", since]

    if not criteria:
        criteria = ["ALL"]

    typ, data = mail.uid("SEARCH", None, *criteria)
    if typ != "OK":
        return {"status": "error", "error": f"SEARCH failed: {data}"}

    uid_list = data[0].decode("ascii", errors="replace").split() if data[0] else []
    if not uid_list:
        return {"status": "ok", "messages": [], "count": 0}

    uid_batch = uid_list[-limit:]
    uid_str = ",".join(uid_batch)

    typ, msgs = mail.uid(
        "FETCH", uid_str,
        "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
    )
    if typ != "OK":
        return {"status": "error", "error": "FETCH headers failed"}

    messages = []
    for uid, raw_headers in _parse_fetch_response(msgs):
        msg = email.message_from_bytes(raw_headers)
        messages.append({
            "uid": uid,
            "from": _decode_header(msg.get("From")),
            "subject": _decode_header(msg.get("Subject")) or "(no subject)",
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
        })

    messages.reverse()
    return {"status": "ok", "messages": messages, "count": len(messages)}


def cmd_mark(mail, mailbox, uid, flag, add):
    """Add or remove a flag on a message by UID."""
    if not uid or not uid.strip():
        return {"status": "error", "step": "uid_guard",
                "error": "mark: uid is required and must be non-empty"}

    uid = uid.strip()
    typ, _ = mail.select(mailbox, readonly=False)
    if typ != "OK":
        return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    op = "+FLAGS" if add else "-FLAGS"
    typ, data = mail.uid("STORE", uid, op, r"(\Seen)")
    if typ != "OK":
        return {"status": "error", "error": f"STORE {op} failed: {data}"}

    return {"status": "ok", "uid": uid, "flag": flag,
            "action": "added" if add else "removed"}


def _parse_from_addr(from_addr):
    """Parse 'Display Name <email@addr>' into (display_name, email_addr).

    Returns the original string as display_name if not in RFC2822 format.
    Also strips [uid:XXXX] tags that specialist may pass from context.
    """
    # Strip [uid:...] tags that specialist might accidentally include
    from_addr = re.sub(r'\[uid:\d+\]', '', from_addr).strip()
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_addr)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return from_addr, ""


def _resolve_uid(mail, mailbox, uid, from_addr, subject):
    """Resolve a UID from direct uid or search-then-resolve via from_addr/subject.

    Tries multiple IMAP search strategies when from_addr is in 'Name <email>' format:
    1. Display name only (FROM "Ladbrokes")
    2. Email address only (FROM "info@email.ladbrokes.com")
    3. Subject keyword fallback

    Returns (uid_str, error_dict_or_None).
    """
    uid = (uid or "").strip()
    if uid:
        return uid, None

    from_addr = (from_addr or "").strip()
    subject   = (subject or "").strip()

    if not from_addr and not subject:
        return None, {"status": "error", "error": "uid, from_addr, or subject required"}

    typ, _ = mail.select(mailbox, readonly=True)
    if typ != "OK":
        return None, {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    def _q(s):
        s = s.strip()
        return f'"{s}"' if " " in s and not (s.startswith('"') and s.endswith('"')) else s

    def _search(crit):
        t, d = mail.uid("SEARCH", None, *crit)
        if t != "OK":
            return []
        return d[0].decode("ascii", errors="replace").split() if d[0] else []

    display_name, email_addr = _parse_from_addr(from_addr) if from_addr else ("", "")

    uid_list = []

    # Strategy 1: display name + subject (most specific)
    if display_name and subject:
        uid_list = _search(["FROM", _q(display_name), "SUBJECT", _q(subject)])

    # Strategy 2: display name only
    if not uid_list and display_name:
        uid_list = _search(["FROM", _q(display_name)])

    # Strategy 3: email address only (when Name <addr> parsing succeeded)
    if not uid_list and email_addr:
        uid_list = _search(["FROM", _q(email_addr)])

    # Strategy 4: full original from_addr string (last resort before subject-only)
    if not uid_list and from_addr and from_addr not in (display_name, email_addr):
        uid_list = _search(["FROM", _q(from_addr)])

    # Strategy 5: subject only
    if not uid_list and subject:
        uid_list = _search(["SUBJECT", _q(subject)])

    if not uid_list:
        return None, {
            "status": "error",
            "error": (
                f"No message found matching from_addr={from_addr!r} subject={subject!r}. "
                "Verify the sender name matches the inbox listing."
            ),
        }

    return uid_list[-1], None


def cmd_delete(mail, mailbox, uid, from_addr, subject):
    """Delete a message by UID (or search-then-delete via from_addr/subject).

    Marks the message \\Deleted then expunges.
    """
    uid, err = _resolve_uid(mail, mailbox, uid, from_addr, subject)
    if err:
        return err

    typ, _ = mail.select(mailbox, readonly=False)
    if typ != "OK":
        return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    typ, data = mail.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    if typ != "OK":
        return {"status": "error", "error": f"STORE +FLAGS \\Deleted failed: {data}"}

    mail.expunge()
    return {"status": "ok", "uid": uid, "action": "deleted"}


def cmd_move(mail, mailbox, uid, from_addr, subject, target_folder):
    """Move a message to target_folder by UID (or search-then-move).

    Uses IMAP COPY + mark \\Deleted + EXPUNGE (no server-side MOVE extension needed).
    """
    if not target_folder or not target_folder.strip():
        return {"status": "error", "error": "move: target_folder is required"}

    target_folder = target_folder.strip()

    uid, err = _resolve_uid(mail, mailbox, uid, from_addr, subject)
    if err:
        return err

    typ, _ = mail.select(mailbox, readonly=False)
    if typ != "OK":
        return {"status": "error", "error": f"SELECT {mailbox!r} failed"}

    # Wrap folder name in quotes if it contains spaces
    folder_arg = f'"{target_folder}"' if " " in target_folder else target_folder
    typ, data = mail.uid("COPY", uid, folder_arg)
    if typ != "OK":
        return {"status": "error", "error": f"COPY to {target_folder!r} failed: {data}"}

    mail.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    mail.expunge()
    return {"status": "ok", "uid": uid, "action": "moved", "destination": target_folder}


def cmd_list_mailboxes(mail):
    """List all IMAP folders."""
    typ, data = mail.list()
    if typ != "OK":
        return {"status": "error", "error": f"LIST failed: {data}"}

    mailboxes = []
    for item in data:
        if isinstance(item, bytes):
            decoded = item.decode("utf-8", errors="replace")
            # Format: '(\HasNoChildren) "/" "INBOX"' or '(\HasNoChildren) "/" INBOX'
            m = re.search(r'"\s*$', decoded)
            if m:
                # quoted name at end
                m2 = re.search(r'"([^"]+)"\s*$', decoded)
                if m2:
                    mailboxes.append(m2.group(1))
            else:
                parts = decoded.rsplit(" ", 1)
                if len(parts) == 2:
                    mailboxes.append(parts[1].strip().strip('"'))

    return {"status": "ok", "mailboxes": sorted(mailboxes), "count": len(mailboxes)}


def main():
    parser = argparse.ArgumentParser(description="IMAP email operations")
    parser.add_argument("--command", required=True,
                        choices=["check", "fetch", "search", "mark-read", "mark-unread",
                                 "list-mailboxes", "delete", "move"])
    parser.add_argument("--account", default="business", choices=["business", "personal"])
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--uid", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--from_addr", default="")
    parser.add_argument("--subject", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--unseen", action="store_true")
    parser.add_argument("--target_folder", default="")
    args = parser.parse_args()

    try:
        mail = _connect(args.account)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"IMAP connect failed: {e}"}))
        sys.exit(1)

    try:
        if args.command == "check":
            result = cmd_check(mail, args.mailbox, args.limit, args.unseen)
        elif args.command == "fetch":
            result = cmd_fetch(mail, args.mailbox, args.uid,
                               from_addr=args.from_addr, subject=args.subject)
        elif args.command == "search":
            result = cmd_search(mail, args.mailbox, args.query, args.from_addr,
                                args.subject, args.since, args.limit)
        elif args.command == "mark-read":
            result = cmd_mark(mail, args.mailbox, args.uid, r"\Seen", add=True)
        elif args.command == "mark-unread":
            result = cmd_mark(mail, args.mailbox, args.uid, r"\Seen", add=False)
        elif args.command == "list-mailboxes":
            result = cmd_list_mailboxes(mail)
        elif args.command == "delete":
            result = cmd_delete(mail, args.mailbox, args.uid, args.from_addr, args.subject)
        elif args.command == "move":
            result = cmd_move(mail, args.mailbox, args.uid, args.from_addr, args.subject,
                              args.target_folder)
        else:
            result = {"status": "error", "error": f"Unknown command: {args.command}"}
    except Exception as e:
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    print(json.dumps(result))
    sys.exit(1 if result.get("status") == "error" else 0)


if __name__ == "__main__":
    main()
