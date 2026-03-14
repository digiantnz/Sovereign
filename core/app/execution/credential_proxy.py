"""CredentialProxy — session-scoped one-time token credential delegation.

Phase 2 of nanobot-01 credential access model.

Flow:
  1. NanobotAdapter.run() calls proxy.issue(services) before _forward()
  2. Token + proxy URL injected into nanobot-01 request context
  3. nanobot-01 calls POST sovereign-core:8000/credential_proxy with token
  4. proxy.redeem() returns credentials and immediately invalidates the token
  5. nanobot-01 injects credentials as subprocess env vars for python3_exec

Security properties:
  - Tokens are single-use (invalidated on first redeem)
  - Tokens expire after TTL (default 60s) even if not redeemed
  - Credentials never stored in nanobot-01, never logged
  - Token has no value after redemption (replay attack window = zero)
  - Audit ledger records every issue + redeem event (no credential values)
"""
import logging
import os
import time
import uuid
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

# Credential service definitions — maps service name → env var names
# All values read from sovereign-core env at issue time (never at startup)
_SERVICE_MAP: dict[str, list[str]] = {
    "imap_business": [
        "BUSINESS_IMAP_HOST", "BUSINESS_IMAP_PORT",
        "BUSINESS_IMAP_USER", "BUSINESS_IMAP_PASS",
    ],
    "imap_personal": [
        "PERSONAL_IMAP_HOST", "PERSONAL_IMAP_PORT",
        "PERSONAL_IMAP_USER", "PERSONAL_IMAP_PASS",
    ],
    "smtp_business": [
        "BUSINESS_SMTP_HOST", "BUSINESS_SMTP_PORT",
        "BUSINESS_SMTP_USER", "BUSINESS_SMTP_PASS",
    ],
    "smtp_personal": [
        "PERSONAL_SMTP_HOST", "PERSONAL_SMTP_PORT",
        "PERSONAL_SMTP_USER", "PERSONAL_SMTP_PASS",
    ],
    "nextcloud": [
        "NEXTCLOUD_URL", "NEXTCLOUD_ADMIN_USER", "NEXTCLOUD_ADMIN_PASSWORD",
        "WEBDAV_BASE_URL", "CALDAV_BASE_URL",
    ],
}

# Known safe services — others are rejected at issue time
_KNOWN_SERVICES = frozenset(_SERVICE_MAP)


class CredentialProxy:
    """In-memory single-use token store for credential delegation to nanobot-01."""

    def __init__(self, default_ttl: int = 60, ledger=None):
        self._tokens: dict[str, dict] = {}
        self._lock = Lock()
        self._default_ttl = default_ttl
        self._ledger = ledger

    def issue(self, services: list[str], ttl: int | None = None) -> str | None:
        """Issue a one-time token granting access to listed credential services.

        Returns the token string, or None if no requested service is known.
        Unknown services are silently dropped (with a warning log).
        """
        valid_services = [s for s in services if s in _KNOWN_SERVICES]
        unknown = [s for s in services if s not in _KNOWN_SERVICES]
        if unknown:
            logger.warning("CredentialProxy.issue: unknown services dropped: %s", unknown)
        if not valid_services:
            return None

        credentials: dict[str, str] = {}
        for svc in valid_services:
            for var in _SERVICE_MAP[svc]:
                val = os.environ.get(var, "")
                if val:
                    credentials[var] = val

        token = str(uuid.uuid4())
        expires_at = time.monotonic() + (ttl or self._default_ttl)

        with self._lock:
            self._tokens[token] = {
                "services": valid_services,
                "credentials": credentials,
                "expires_at": expires_at,
            }

        logger.debug("CredentialProxy: issued token for services=%s ttl=%ss",
                     valid_services, ttl or self._default_ttl)
        self._audit("credential_token_issued", {"services": valid_services})
        return token

    def redeem(self, token: str) -> Optional[dict]:
        """Redeem a token — returns credential dict and immediately invalidates.

        Returns None if token is unknown, already used, or expired.
        """
        with self._lock:
            entry = self._tokens.pop(token, None)

        if not entry:
            logger.warning("CredentialProxy.redeem: token not found or already used")
            self._audit("credential_token_invalid", {"reason": "not_found_or_reused"})
            return None

        if time.monotonic() > entry["expires_at"]:
            logger.warning("CredentialProxy.redeem: token expired")
            self._audit("credential_token_invalid", {"reason": "expired", "services": entry["services"]})
            return None

        logger.debug("CredentialProxy: redeemed token for services=%s", entry["services"])
        self._audit("credential_token_redeemed", {"services": entry["services"]})
        return entry["credentials"]

    def cleanup_expired(self) -> int:
        """Remove expired tokens. Call from a periodic maintenance task."""
        now = time.monotonic()
        with self._lock:
            expired = [t for t, e in self._tokens.items() if now > e["expires_at"]]
            for t in expired:
                del self._tokens[t]
        if expired:
            logger.debug("CredentialProxy: cleaned up %d expired tokens", len(expired))
        return len(expired)

    def _audit(self, event_type: str, data: dict) -> None:
        if self._ledger:
            try:
                self._ledger.append(event_type=event_type, stage="credential_proxy", data=data)
            except Exception:
                pass
