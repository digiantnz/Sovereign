"""SMTP adapter — mail send.

Reference: openclaw imap-smtp-email community skill (gzlicanyi)
All methods return explicit structured dicts — no exceptions propagate to callers.
Handles port 465 (SSL) vs port 587 (STARTTLS) automatically.
Returns actual SMTP response codes in the result dict.

Model B note: operations candidates for future DSL frontmatter:
  send(account, to, subject, body, cc, bcc, html)
"""

import smtplib
import os
import asyncio
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

ACCOUNTS = {
    "personal": {
        "host":     os.environ.get("PERSONAL_SMTP_HOST", ""),
        "port":     int(os.environ.get("PERSONAL_SMTP_PORT", 587)),
        "user":     os.environ.get("PERSONAL_SMTP_USER", ""),
        "password": os.environ.get("PERSONAL_SMTP_PASS", ""),
    },
    "business": {
        "host":     os.environ.get("BUSINESS_SMTP_HOST", ""),
        "port":     int(os.environ.get("BUSINESS_SMTP_PORT", 587)),
        "user":     os.environ.get("BUSINESS_SMTP_USER", ""),
        "password": os.environ.get("BUSINESS_SMTP_PASS", ""),
    },
}


class SMTPAdapter:
    def __init__(self, account: str = "personal"):
        cfg = ACCOUNTS.get(account)
        if not cfg:
            raise ValueError(f"Unknown mail account: {account}")
        self.account  = account
        self.host     = cfg["host"]
        self.port     = cfg["port"]
        self.user     = cfg["user"]
        self.password = cfg["password"]

    def _send_sync(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html: str = "",
    ) -> dict:
        """Send email. Returns structured result with SMTP response codes.
        Never raises — all errors are captured and returned as status="error".

        Port 465 → SMTP_SSL (implicit TLS).
        Port 587 (default) → SMTP + STARTTLS.
        """
        # Build recipients list
        recipients = [addr.strip() for addr in to.split(",") if addr.strip()]
        if cc:
            recipients += [addr.strip() for addr in cc.split(",") if addr.strip()]
        if bcc:
            recipients += [addr.strip() for addr in bcc.split(",") if addr.strip()]

        # Build message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self.user
        msg["To"]      = to
        if cc:
            msg["Cc"] = cc
        # BCC is in recipients list but not in headers (correct SMTP behaviour)

        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))

        ehlo_response: str = ""
        try:
            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port)
            else:
                server = smtplib.SMTP(self.host, self.port)

            with server:
                code, ehlo_data = server.ehlo()
                ehlo_response = f"{code} {ehlo_data.decode('utf-8', errors='replace') if isinstance(ehlo_data, bytes) else ehlo_data}"

                if self.port != 465:
                    tls_code, tls_msg = server.starttls()
                    if tls_code != 220:
                        return {
                            "status": "error",
                            "step": "starttls",
                            "account": self.account,
                            "smtp_code": tls_code,
                            "smtp_response": str(tls_msg),
                            "ehlo_response": ehlo_response,
                        }
                    # Re-EHLO after STARTTLS (required by RFC 3207)
                    server.ehlo()

                login_code, login_msg = server.login(self.user, self.password)
                if login_code not in (235, 334):
                    # 235 = auth success, 334 = auth challenge (some servers)
                    # smtplib.login() raises SMTPAuthenticationError on real failure,
                    # so this branch is defensive
                    return {
                        "status": "error",
                        "step": "login",
                        "account": self.account,
                        "smtp_code": login_code,
                        "smtp_response": str(login_msg),
                        "ehlo_response": ehlo_response,
                    }

                refused = server.sendmail(self.user, recipients, msg.as_string())
                # sendmail() returns a dict of refused recipients (empty = all delivered)
                if refused:
                    return {
                        "status": "partial",
                        "account": self.account,
                        "to": to,
                        "subject": subject,
                        "refused_recipients": refused,
                        "ehlo_response": ehlo_response,
                        "message": "Some recipients were refused by the server",
                    }

        except smtplib.SMTPAuthenticationError as e:
            return {
                "status": "error",
                "step": "authentication",
                "account": self.account,
                "smtp_code": e.smtp_code,
                "smtp_response": str(e.smtp_error),
                "ehlo_response": ehlo_response,
                "error": "SMTP authentication failed — check credentials or app password",
            }
        except smtplib.SMTPRecipientsRefused as e:
            return {
                "status": "error",
                "step": "send",
                "account": self.account,
                "refused_recipients": {str(k): str(v) for k, v in e.recipients.items()},
                "ehlo_response": ehlo_response,
                "error": "All recipients refused by server",
            }
        except smtplib.SMTPException as e:
            return {
                "status": "error",
                "step": "smtp",
                "account": self.account,
                "error": f"SMTP error: {type(e).__name__}: {e}",
                "ehlo_response": ehlo_response,
            }
        except OSError as e:
            return {
                "status": "error",
                "step": "connect",
                "account": self.account,
                "error": f"Connection error: {type(e).__name__}: {e}",
            }

        return {
            "status": "ok",
            "account": self.account,
            "to": to,
            "cc": cc,
            "subject": subject,
            "recipient_count": len(recipients),
            "ehlo_response": ehlo_response,
        }

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html: str = "",
    ) -> dict:
        """Send an email. Returns structured result — never raises.

        Args:
            to:      comma-separated recipient addresses
            subject: email subject line
            body:    plain text body
            cc:      comma-separated CC addresses (optional)
            bcc:     comma-separated BCC addresses (optional, not in headers)
            html:    HTML body part (optional; sent as alternative to plain)
        """
        if not self.host or not self.user:
            return {
                "status": "unconfigured",
                "account": self.account,
                "message": f"Credentials not set for {self.account} account",
            }
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._send_sync(to, subject, body, cc, bcc, html)
        )
