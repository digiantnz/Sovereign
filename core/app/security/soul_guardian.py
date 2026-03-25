import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SOUL_MD_PATH = "/home/sovereign/personas/sovereign-soul.md"
SOUL_BACKUP_PATH = "/home/sovereign/governance/soul-backup/Sovereign-soul.md"

GOVERNANCE_PATH = "/app/governance/governance.json"  # container path (mounted from RAID)
GOVERNANCE_BACKUP_PATH = "/home/sovereign/governance/soul-backup/governance.json"

PROTECTED_FILES = [
    SOUL_MD_PATH,
    GOVERNANCE_PATH,
    "/home/sovereign/personas/orchestrator.md",
    "/home/sovereign/governance/SENSITIVITY_MODEL.md",
    "/home/sovereign/governance/EXTERNAL_COGNITION.md",
    "/home/sovereign/security/injection_patterns.yaml",
    "/home/sovereign/security/sensitive_data_patterns.yaml",
    "/home/sovereign/security/policy_rules.yaml",
    "/home/sovereign/security/destructive_commands.yaml",
    "/home/sovereign/security/exfiltration_patterns.yaml",
    "/home/sovereign/keys/sovereign.key",   # CRITICAL — Ed25519 signing key
]

# Files that escalate to CRITICAL alert (not just warning) on any modification
CRITICAL_FILES = {"/home/sovereign/keys/sovereign.key"}
CHECKSUM_PATH = "/home/sovereign/security/.checksums.json"

# Files that support auto-restore from a known backup.
# Backup is written in record_baseline() — guaranteed to exist after first startup.
RESTORABLE: dict[str, str] = {
    SOUL_MD_PATH:     SOUL_BACKUP_PATH,
    GOVERNANCE_PATH:  GOVERNANCE_BACKUP_PATH,
}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_soul_md() -> str:
    """Load Sovereign-soul.md. Called as first action on startup before any other init."""
    if not os.path.exists(SOUL_MD_PATH):
        raise RuntimeError(
            f"CRITICAL: Sovereign-soul.md not found at {SOUL_MD_PATH}. "
            "Cannot start without identity document."
        )
    with open(SOUL_MD_PATH) as f:
        return f.read()


async def _notify_telegram(message: str):
    """Send a Telegram notification directly via Bot API (sovereign-core has token in env)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID")
    if not token or not chat_id:
        logger.warning("SoulGuardian: Telegram credentials not available — skipping notification")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("SoulGuardian: Telegram notification failed: %s", e)


class SoulGuardian:
    def __init__(
        self,
        protected_files: list[str] = PROTECTED_FILES,
        checksum_path: str = CHECKSUM_PATH,
    ):
        self._files = protected_files
        self._checksum_path = checksum_path

    def _load_stored(self) -> dict:
        if not os.path.exists(self._checksum_path):
            return {}
        try:
            with open(self._checksum_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_checksums(self, checksums: dict):
        os.makedirs(os.path.dirname(self._checksum_path), exist_ok=True)
        with open(self._checksum_path, "w") as f:
            json.dump(checksums, f, indent=2)

    def get_checksum(self, path: str) -> Optional[str]:
        """Return the current SHA256 checksum for a file."""
        if not os.path.exists(path):
            return None
        return _sha256(path)

    def record_baseline(self):
        """Write current SHA256s as the baseline.

        Also writes backups for all RESTORABLE files so that restore is always
        possible after a drift event.  Backup is written atomically via a temp
        file and rename — safe even if the container restarts mid-write.
        """
        checksums = {}
        for path in self._files:
            if os.path.exists(path):
                checksums[path] = _sha256(path)
                # Write backup for any restorable file — best-effort
                backup_path = RESTORABLE.get(path)
                if backup_path:
                    try:
                        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                        tmp = backup_path + ".tmp"
                        shutil.copy2(path, tmp)
                        os.replace(tmp, backup_path)
                        logger.info("SoulGuardian: backup written %s → %s", path, backup_path)
                    except Exception as e:
                        logger.warning("SoulGuardian: backup write failed for %s: %s", path, e)
            else:
                logger.warning(
                    "SoulGuardian: protected file missing at baseline: %s", path
                )
        self._write_checksums(checksums)
        logger.info("SoulGuardian: baseline recorded for %d files", len(checksums))

    def verify(self) -> list[str]:
        """Compare current SHA256s against stored baseline synchronously.
        Returns list of drifted file paths (empty = all good).
        Records baseline on first run. Auto-restores Sovereign-soul.md if drifted."""
        stored = self._load_stored()

        if not stored:
            logger.info("SoulGuardian: no baseline found — recording initial checksums")
            self.record_baseline()
            return []

        drifted = []
        for path in self._files:
            if not os.path.exists(path):
                logger.warning("SoulGuardian: protected file missing: %s", path)
                drifted.append(path)
                self._attempt_restore(path, auto_reason="file_missing")
                continue
            current = _sha256(path)
            expected = stored.get(path)
            if expected is None:
                # New file — add to baseline
                stored[path] = current
                self._write_checksums(stored)
            elif current != expected:
                logger.warning("SoulGuardian: drift detected: %s", path)
                drifted.append(path)
                self._attempt_restore(path, auto_reason="hash_mismatch")

        return drifted

    def _attempt_restore(self, path: str, auto_reason: str):
        """Restore from backup if a restore source is available."""
        backup = RESTORABLE.get(path)
        if not backup or not os.path.exists(backup):
            logger.warning(
                "SoulGuardian: no restorable backup for %s — alert only", path
            )
            return
        try:
            shutil.copy2(backup, path)
            logger.warning(
                "SoulGuardian: AUTO-RESTORED %s from %s (reason: %s)",
                path, backup, auto_reason,
            )
        except Exception as e:
            logger.error("SoulGuardian: restore failed for %s: %s", path, e)

    async def verify_and_notify(self, ledger=None) -> list[str]:
        """Async wrapper: verify checksums, auto-restore soul, send Telegram notification."""
        drifted = self.verify()
        if not drifted:
            return []

        soul_drifted       = SOUL_MD_PATH in drifted
        governance_drifted = GOVERNANCE_PATH in drifted
        key_drifted        = "/home/sovereign/keys/sovereign.key" in drifted
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        severity = "🔴 *CRITICAL SOUL GUARDIAN ALERT*" if key_drifted else "⚠️ *SOUL GUARDIAN ALERT*"
        msg_lines = [f"{severity} — {ts}", ""]
        for path in drifted:
            short = path.split("/")[-1]
            is_critical = path in CRITICAL_FILES
            restored = path in RESTORABLE and os.path.exists(RESTORABLE[path])
            status = "✅ AUTO-RESTORED" if restored else "❌ NO BACKUP — manual check required"
            prefix = "🔴 CRITICAL" if is_critical else "•"
            msg_lines.append(f"{prefix} `{short}`: {status}")
        if soul_drifted:
            msg_lines += [
                "",
                "🛡️ *Sovereign-soul.md was modified.* Identity document has been restored from backup.",
                "Any intended modifications require Director acknowledgement + double confirmation.",
            ]
        if governance_drifted:
            gov_restored = GOVERNANCE_PATH in RESTORABLE and os.path.exists(RESTORABLE[GOVERNANCE_PATH])
            if gov_restored:
                msg_lines += [
                    "",
                    "⚠️ *governance.json was modified outside governance channels.* "
                    "Tier policy has been restored from backup.",
                    "If this was an intended change, re-apply it through the governed write path.",
                ]
            else:
                msg_lines += [
                    "",
                    "⚠️ *governance.json was modified but no backup exists yet.* "
                    "Manual review required. A backup will be created on next restart.",
                ]
        if key_drifted:
            msg_lines += [
                "",
                "🔴 *sovereign.key was modified or replaced.* This is the Ed25519 signing key.",
                "All signed audit entries and payment ACKs after this point may use a different key.",
                "Immediate Director investigation required.",
            ]
        msg_lines.append("\nAll security events logged to audit ledger.")
        await _notify_telegram("\n".join(msg_lines))

        if ledger:
            for path in drifted:
                short = path.split("/")[-1]
                restored = path in RESTORABLE
                ledger.append("soul_guardian_drift", "startup", {
                    "file": path,
                    "short_name": short,
                    "auto_restored": restored,
                    "reason": "hash_mismatch",
                })

        return drifted
