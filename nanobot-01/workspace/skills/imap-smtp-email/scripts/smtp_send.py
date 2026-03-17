#!/usr/bin/env python3
"""SMTP email send for nanobot-01 python3_exec dispatch.

Env vars (Phase 1 static mounts via compose.yml env_file):
  BUSINESS_SMTP_HOST / PERSONAL_SMTP_HOST
  BUSINESS_SMTP_PORT / PERSONAL_SMTP_PORT  (default 587)
  BUSINESS_SMTP_USER / PERSONAL_SMTP_USER
  BUSINESS_SMTP_PASS / PERSONAL_SMTP_PASS

Output: JSON to stdout. Errors: {"status":"error","error":"..."} + exit 1.
"""

import argparse
import json
import os
import smtplib
import sys
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate


def _connect(account):
    """Return a connected + authenticated SMTP client."""
    prefix = "BUSINESS" if account == "business" else "PERSONAL"
    host = os.environ.get(f"{prefix}_SMTP_HOST", "")
    port = int(os.environ.get(f"{prefix}_SMTP_PORT", "587"))
    user = os.environ.get(f"{prefix}_SMTP_USER", "")
    password = os.environ.get(f"{prefix}_SMTP_PASS", "")

    if not host or not user or not password:
        raise ValueError(
            f"Missing SMTP credentials for account={account!r} "
            f"(need {prefix}_SMTP_HOST / {prefix}_SMTP_USER / {prefix}_SMTP_PASS)"
        )

    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port)
    else:
        smtp = smtplib.SMTP(host, port)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()

    smtp.login(user, password)
    return smtp, user


def send_email(account, to_addr, subject, body, cc=None):
    """Send a plain-text email via SMTP."""
    smtp, from_addr = _connect(account)

    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = f"<{uuid.uuid4()}@{from_addr.split('@')[-1]}>"
    if cc:
        msg["Cc"] = cc

    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [to_addr]
    if cc:
        recipients += [addr.strip() for addr in cc.split(",")]

    try:
        smtp.sendmail(from_addr, recipients, msg.as_string())
        return {"status": "ok", "message_id": msg["Message-ID"],
                "from": from_addr, "to": to_addr}
    finally:
        smtp.quit()


def main():
    parser = argparse.ArgumentParser(description="SMTP email send")
    parser.add_argument("--account", default="business", choices=["business", "personal"])
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--cc", default="")
    args = parser.parse_args()

    try:
        result = send_email(
            account=args.account,
            to_addr=args.to,
            subject=args.subject,
            body=args.body,
            cc=args.cc or None,
        )
    except Exception as e:
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    print(json.dumps(result))
    sys.exit(1 if result.get("status") == "error" else 0)


if __name__ == "__main__":
    main()
