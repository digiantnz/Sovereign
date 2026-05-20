#!/usr/bin/env python3
"""pypdf — python3_exec script for nanobot-01.

Extracts plain text from PDF files stored in Nextcloud via WebDAV.

Commands:
  extract_text  -- download PDF from Nextcloud, return extracted text

Output format:
  success: {"status":"ok", "path":"...", "pages":N, "text":"...", "chars":N}
  error:   {"status":"error", "error":"..."}  + exit 1

Env vars (from nanobot.env via CredentialProxy):
  NEXTCLOUD_URL          -- base URL (default: http://nextcloud)
  NEXTCLOUD_ADMIN_USER   -- WebDAV username
  NEXTCLOUD_ADMIN_PASSWORD -- WebDAV password
"""

import argparse
import io
import json
import os
import sys

import requests
import pypdf

_NC_URL  = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NC_USER = os.environ.get("NEXTCLOUD_ADMIN_USER", "")
_NC_PASS = os.environ.get("NEXTCLOUD_ADMIN_PASSWORD", "")


def _auth():
    return (_NC_USER, _NC_PASS)


def _dav_url(path: str) -> str:
    path = path.lstrip("/")
    return f"{_NC_URL}/remote.php/dav/files/{_NC_USER}/{path}"


def _out(data: dict):
    print(json.dumps(data))
    sys.exit(0)


def _err(msg: str, **kwargs):
    print(json.dumps({"status": "error", "error": msg, **kwargs}))
    sys.exit(1)


def cmd_extract_text(args):
    path = args.path
    url  = _dav_url(path)

    try:
        r = requests.get(url, auth=_auth(), timeout=60)
    except Exception as e:
        _err(f"WebDAV GET failed: {e}")

    if r.status_code != 200:
        _err(f"GET {path} returned {r.status_code}", http_status=r.status_code)

    try:
        reader = pypdf.PdfReader(io.BytesIO(r.content))
        pages  = len(reader.pages)
        parts  = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        text = "\n\n".join(parts)
    except Exception as e:
        _err(f"PDF parse failed: {e}")

    _out({
        "status": "ok",
        "path":   path,
        "pages":  pages,
        "text":   text,
        "chars":  len(text),
    })


def main():
    parser = argparse.ArgumentParser()
    sub    = parser.add_subparsers(dest="command")

    p_ext = sub.add_parser("extract_text")
    p_ext.add_argument("--path", required=True, help="Nextcloud file path e.g. /downloads/doc.pdf")

    args = parser.parse_args()
    if args.command == "extract_text":
        cmd_extract_text(args)
    else:
        _err(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
