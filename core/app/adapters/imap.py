"""IMAP adapter — mail read and management.

Reference: openclaw imap-smtp-email community skill (gzlicanyi)
All methods return explicit structured error dicts — no exceptions propagate to callers.
All IMAP operations use UID commands (mail.uid()) throughout for message stability
under concurrent access — sequence numbers are never used after the initial login.

Model B note: operations candidates for future DSL frontmatter:
  fetch_unread(account, max), fetch_message(account, uid),
  search(account, criteria), move_message(account, uid, destination),
  delete_message(account, uid), mark_read(account, uid),
  mark_unread(account, uid), list_folders(account)
"""

import imaplib
import email
import email.message
import os
import re
import asyncio
import logging
from email.header import decode_header

logger = logging.getLogger(__name__)

ACCOUNTS = {
    "personal": {
        "host":     os.environ.get("PERSONAL_IMAP_HOST", ""),
        "port":     int(os.environ.get("PERSONAL_IMAP_PORT", 993)),
        "user":     os.environ.get("PERSONAL_IMAP_USER", ""),
        "password": os.environ.get("PERSONAL_IMAP_PASS", ""),
    },
    "business": {
        "host":     os.environ.get("BUSINESS_IMAP_HOST", ""),
        "port":     int(os.environ.get("BUSINESS_IMAP_PORT", 993)),
        "user":     os.environ.get("BUSINESS_IMAP_USER", ""),
        "password": os.environ.get("BUSINESS_IMAP_PASS", ""),
    },
}


def _decode_header_value(raw) -> str:
    """Decode an email header value that may be RFC 2047 encoded."""
    if raw is None:
        return ""
    parts, result = decode_header(raw), []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain text body from a message, preferring text/plain."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        # Fallback: first text/* part
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
    return ""


class IMAPAdapter:
    def __init__(self, account: str = "personal"):
        cfg = ACCOUNTS.get(account)
        if not cfg:
            raise ValueError(f"Unknown mail account: {account}")
        self.account = account
        self.host     = cfg["host"]
        self.port     = cfg["port"]
        self.user     = cfg["user"]
        self.password = cfg["password"]

    def _connect(self) -> tuple[imaplib.IMAP4_SSL, dict | None]:
        """Open SSL connection and login. Returns (mail, None) on success or (None, error_dict)."""
        try:
            mail = imaplib.IMAP4_SSL(self.host, self.port)
        except Exception as e:
            return None, {
                "status": "error", "step": "connect",
                "account": self.account,
                "error": f"Connection failed: {type(e).__name__}: {e}",
            }
        login_typ, login_data = mail.login(self.user, self.password)
        if login_typ != "OK":
            try:
                mail.logout()
            except Exception:
                pass
            return None, {
                "status": "error", "step": "login",
                "account": self.account,
                "imap_response": login_typ,
                "response_data": str(login_data),
            }
        return mail, None

    @staticmethod
    def _parse_list_folders(data: list) -> list[str]:
        """Extract folder names from IMAP LIST response lines."""
        folders = []
        for line in data:
            if line is None:
                continue
            decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
            m = re.search(r'\) ".*?" (.+)$', decoded)
            if not m:
                m = re.search(r'\) NIL (.+)$', decoded, re.IGNORECASE)
            if m:
                name = m.group(1).strip().strip('"')
                if name:
                    folders.append(name)
        return folders

    @staticmethod
    def _find_archive(folders: list) -> str | None:
        """Return the Archive folder name from a folder list, or None if absent."""
        candidates = ["archive", "archives", "inbox.archive", "saved messages"]
        lower_map = {f.lower(): f for f in folders}
        for candidate in candidates:
            if candidate in lower_map:
                return lower_map[candidate]
        return None

    # ── Sync implementations (run in executor) ────────────────────────────────

    def _list_inbox_sync(self, max_messages: int = 50) -> dict:
        """List inbox messages with UIDs using ALL search + header-only fetch.

        Uses mail.uid('SEARCH', None, 'ALL') so every returned ID is a stable
        UID, then mail.uid('FETCH', uid, '(RFC822.HEADER)') for lightweight
        header retrieval. Returns {uid, subject, from, date} for each message.

        This is the correct method to call before move_message — the specialist
        must have a real UID before calling move; this provides it.
        """
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "account": self.account,
                    "imap_response": select_typ,
                    "response_data": str(select_data),
                }

            search_typ, search_data = mail.uid("SEARCH", None, "ALL")
            if search_typ != "OK":
                return {
                    "status": "error", "step": "uid_search_all",
                    "account": self.account,
                    "imap_response": search_typ,
                    "response_data": str(search_data),
                }

            uid_list = search_data[0].split() if search_data and search_data[0] else []
            uid_list = uid_list[-max_messages:]  # most recent N

            messages = []
            for uid_bytes in uid_list:
                uid_str = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
                fetch_typ, msg_data = mail.uid("FETCH", uid_str, "(RFC822.HEADER)")
                if fetch_typ != "OK" or not msg_data or msg_data[0] is None:
                    messages.append({"uid": uid_str, "error": f"FETCH returned {fetch_typ}"})
                    continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if not isinstance(raw, bytes):
                    messages.append({"uid": uid_str, "error": "unexpected fetch payload type"})
                    continue
                msg = email.message_from_bytes(raw)
                messages.append({
                    "uid":     uid_str,
                    "from":    msg.get("From", ""),
                    "subject": _decode_header_value(msg.get("Subject", "")),
                    "date":    msg.get("Date", ""),
                })

            return {
                "status": "ok",
                "account": self.account,
                "count": len(messages),
                "messages": messages,
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _fetch_unread_sync(self, max_messages: int = 10) -> dict:
        """Fetch unread messages using UID commands throughout."""
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "account": self.account,
                    "imap_response": select_typ,
                    "response_data": str(select_data),
                }

            # Use UID SEARCH so returned IDs are stable UIDs, not sequence numbers
            search_typ, search_data = mail.uid("SEARCH", None, "UNSEEN")
            if search_typ != "OK":
                return {
                    "status": "error", "step": "uid_search_unseen",
                    "account": self.account,
                    "imap_response": search_typ,
                    "response_data": str(search_data),
                }

            uid_list = search_data[0].split() if search_data and search_data[0] else []
            uid_list = uid_list[-max_messages:]  # most recent N

            messages = []
            for uid_bytes in uid_list:
                uid_str = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
                fetch_typ, msg_data = mail.uid("FETCH", uid_str, "(RFC822)")
                if fetch_typ != "OK" or not msg_data or msg_data[0] is None:
                    messages.append({
                        "uid": uid_str, "error": f"FETCH returned {fetch_typ}",
                    })
                    continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if not isinstance(raw, bytes):
                    messages.append({"uid": uid_str, "error": "unexpected fetch payload type"})
                    continue
                msg = email.message_from_bytes(raw)
                messages.append({
                    "uid":     uid_str,
                    "from":    msg.get("From", ""),
                    "subject": _decode_header_value(msg.get("Subject", "")),
                    "date":    msg.get("Date", ""),
                    "body":    _extract_body(msg)[:1000],
                })

            return {
                "status": "ok",
                "account": self.account,
                "count": len(messages),
                "messages": messages,
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _fetch_message_sync(self, uid: str) -> dict:
        """Fetch a single message by UID with full body."""
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "imap_response": select_typ, "response_data": str(select_data),
                }
            fetch_typ, msg_data = mail.uid("FETCH", uid, "(RFC822)")
            if fetch_typ != "OK" or not msg_data or msg_data[0] is None:
                return {
                    "status": "error", "step": "uid_fetch",
                    "uid": uid,
                    "imap_response": fetch_typ,
                }
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return {"status": "error", "uid": uid, "error": "unexpected payload type"}
            msg = email.message_from_bytes(raw)
            return {
                "status": "ok",
                "account": self.account,
                "uid": uid,
                "from":    msg.get("From", ""),
                "to":      msg.get("To", ""),
                "subject": _decode_header_value(msg.get("Subject", "")),
                "date":    msg.get("Date", ""),
                "body":    _extract_body(msg),
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _search_sync(self, criteria: dict) -> dict:
        """Live IMAP UID SEARCH against INBOX — never touches Qdrant.

        Supported criteria keys:
          subject   — SUBJECT search term
          from_addr — FROM address fragment
          since     — ISO 8601 date string; converted to DD-Mon-YYYY for IMAP
          body      — BODY keyword

        Returns actual IMAP response codes and matched messages (headers only, up to 20).
        """
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "imap_response": select_typ, "response_data": str(select_data),
                }

            parts = []
            if criteria.get("subject"):
                parts.append(f'SUBJECT "{criteria["subject"]}"')
            if criteria.get("from_addr"):
                parts.append(f'FROM "{criteria["from_addr"]}"')
            if criteria.get("since"):
                from datetime import datetime as _dt
                try:
                    d = _dt.fromisoformat(str(criteria["since"]))
                    parts.append(f'SINCE {d.strftime("%d-%b-%Y")}')
                except ValueError:
                    pass
            if criteria.get("body"):
                parts.append(f'BODY "{criteria["body"]}"')
            search_str = " ".join(parts) if parts else "ALL"

            search_typ, search_data = mail.uid("SEARCH", None, search_str)
            if search_typ != "OK":
                return {
                    "status": "error", "step": "uid_search",
                    "account": self.account,
                    "imap_response": search_typ,
                    "response_data": str(search_data),
                    "criteria_used": search_str,
                }

            uid_list = search_data[0].split() if search_data and search_data[0] else []
            messages = []
            for uid_bytes in uid_list[-20:]:
                uid_str = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
                fetch_typ, msg_data = mail.uid("FETCH", uid_str, "(RFC822.HEADER)")
                if fetch_typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if not isinstance(raw, bytes):
                    continue
                msg = email.message_from_bytes(raw)
                messages.append({
                    "uid":     uid_str,
                    "from":    msg.get("From", ""),
                    "subject": _decode_header_value(msg.get("Subject", "")),
                    "date":    msg.get("Date", ""),
                })

            return {
                "status": "ok",
                "account": self.account,
                "imap_search_response": search_typ,
                "criteria_used": search_str,
                "count": len(messages),
                "messages": messages,
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _move_sync(self, uid: str, destination: str = "Archive") -> dict:
        """Move: LIST to discover Archive → UID COPY → UID STORE \\Deleted → EXPUNGE."""
        if not uid or not str(uid).strip():
            return {
                "status": "error",
                "step": "uid_guard",
                "error": "No UID provided for move operation — specialist must fetch inbox first to obtain a real UID",
            }
        uid = str(uid).strip()
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "imap_response": select_typ, "response_data": str(select_data),
                }

            list_typ, list_data = mail.list('""', '"*"')
            if list_typ != "OK":
                return {
                    "status": "error", "step": "list_folders",
                    "imap_response": list_typ, "response_data": str(list_data),
                }
            available_folders = self._parse_list_folders(list_data)

            archive_folder = self._find_archive(available_folders)
            if archive_folder is None:
                create_typ, create_data = mail.create(destination)
                if create_typ != "OK":
                    return {
                        "status": "error", "step": "create_archive",
                        "imap_response": create_typ, "response_data": str(create_data),
                        "available_folders": available_folders,
                    }
                archive_folder = destination
                folder_action = "created"
            else:
                folder_action = "discovered"

            copy_typ, copy_data = mail.uid("COPY", uid, archive_folder)
            if copy_typ != "OK":
                return {
                    "status": "error", "step": "uid_copy",
                    "imap_response": copy_typ, "response_data": str(copy_data),
                    "archive_folder": archive_folder, "folder_action": folder_action,
                }

            store_typ, store_data = mail.uid("STORE", uid, "+FLAGS", "\\Deleted")
            expunge_typ, expunge_data = mail.expunge()

            overall = "ok" if store_typ == "OK" and expunge_typ == "OK" else "partial"
            return {
                "status": overall,
                "uid": uid,
                "archive_folder": archive_folder,
                "folder_action": folder_action,
                "available_folders": available_folders,
                "copy_response": copy_typ,
                "store_response": store_typ,
                "store_data": str(store_data),
                "expunge_response": expunge_typ,
                "expunge_data": str(expunge_data),
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _delete_sync(self, uid: str) -> dict:
        """Hard delete: UID STORE \\Deleted then EXPUNGE."""
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "imap_response": select_typ, "response_data": str(select_data),
                }

            store_typ, store_data = mail.uid("STORE", uid, "+FLAGS", "\\Deleted")
            expunge_typ, expunge_data = mail.expunge()

            overall = "ok" if store_typ == "OK" and expunge_typ == "OK" else "error"
            return {
                "status": overall,
                "uid": uid,
                "store_response": store_typ,
                "store_data": str(store_data),
                "expunge_response": expunge_typ,
                "expunge_data": str(expunge_data),
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _mark_flag_sync(self, uid: str, flag: str, set_flag: bool) -> dict:
        """Set or clear a flag (e.g. \\Seen) using UID STORE."""
        mail, err = self._connect()
        if err:
            return err
        try:
            select_typ, select_data = mail.select("INBOX")
            if select_typ != "OK":
                return {
                    "status": "error", "step": "select_inbox",
                    "imap_response": select_typ, "response_data": str(select_data),
                }
            action = "+FLAGS" if set_flag else "-FLAGS"
            store_typ, store_data = mail.uid("STORE", uid, action, flag)
            overall = "ok" if store_typ == "OK" else "error"
            return {
                "status": overall,
                "uid": uid,
                "flag": flag,
                "action": action,
                "imap_response": store_typ,
                "response_data": str(store_data),
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _list_folders_sync(self) -> dict:
        """LIST all IMAP folders."""
        mail, err = self._connect()
        if err:
            return err
        try:
            list_typ, list_data = mail.list('""', '"*"')
            if list_typ != "OK":
                return {
                    "status": "error", "step": "list_folders",
                    "account": self.account,
                    "imap_response": list_typ, "response_data": str(list_data),
                }
            folders = self._parse_list_folders(list_data)
            return {"status": "ok", "account": self.account, "folders": folders, "count": len(folders)}
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    # ── Async public API ──────────────────────────────────────────────────────

    def _unconfigured(self, method: str) -> dict:
        return {
            "status": "unconfigured",
            "account": self.account,
            "method": method,
            "message": f"Credentials not set for {self.account} account",
        }

    async def list_inbox(self, max_messages: int = 50) -> dict:
        """List inbox with UIDs — ALL messages, headers only. Use this before move_message."""
        if not self.host or not self.user:
            return self._unconfigured("list_inbox")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._list_inbox_sync(max_messages))

    async def fetch_unread(self, max_messages: int = 10) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("fetch_unread")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._fetch_unread_sync(max_messages))

    async def fetch_message(self, uid: str) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("fetch_message")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._fetch_message_sync(uid))

    async def search(self, criteria: dict) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("search")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._search_sync(criteria))

    async def move_message(self, uid: str, destination: str = "Archive") -> dict:
        if not self.host or not self.user:
            return self._unconfigured("move_message")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._move_sync(uid, destination))

    async def delete_message(self, uid: str) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("delete_message")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._delete_sync(uid))

    async def mark_read(self, uid: str) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("mark_read")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._mark_flag_sync(uid, "\\Seen", True))

    async def mark_unread(self, uid: str) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("mark_unread")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._mark_flag_sync(uid, "\\Seen", False))

    async def list_folders(self) -> dict:
        if not self.host or not self.user:
            return self._unconfigured("list_folders")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_folders_sync)
