"""Config Change Notification Policy

Enforces the rule that any write to sensitive Sovereign configuration files must:

  1. Have already received Director confirmation before the write occurs.
     (Confirmation enforcement is at the governance layer — execution engine returns
     requires_confirmation / requires_double_confirmation before the action runs.
     This module fires AFTER the write succeeds to close the notification loop.)

  2. Send a Telegram notification to the Director summarising what changed and why.

  3. Append a CEO-readable narrative entry to as-built.md with timestamp, what changed,
     which agent proposed it, and the reason given.

  The audit ledger receives the technical detail (checksums, paths, tiers).
  as-built.md receives the narrative only — no raw diffs, no technical internals.

Files in scope and their policy tiers:

  | File / Pattern                         | Tier | Notes                              |
  |----------------------------------------|------|------------------------------------|
  | governance.json                        | ANY  | Policy document                    |
  | sovereign-soul.md                      | HIGH | Identity document — double confirm |
  | /home/sovereign/security/*.yaml        | MID  | Security pattern files             |
  | /home/sovereign/personas/*             | MID  | Specialist persona files           |
  | /home/sovereign/skills/*               | MID  | Skill definitions (add or remove)  |
  | skill-checksums.json                   | HIGH | Tamper evidence — treat as CRITICAL|

Usage (async — call after a successful confirmed write):

    from config_policy.notifier import notify_config_change

    await notify_config_change(
        path="/home/sovereign/skills/my-skill/SKILL.md",
        narrative="Installed new skill 'my-skill' for the research and memory specialists. "
                  "It adds a structured multi-source research protocol.",
        proposed_by="devops_agent",
        reason="Director requested skill installation from ClawhHub registry.",
        tier="MID",
        ledger=ledger,
    )

For future adapters that write governance.json, personas, or security pattern files,
use the async config_write() helper which gates the write, runs the write, and notifies:

    result = await config_write(
        path="/home/sovereign/personas/research_agent.md",
        content=new_content,
        proposed_by=agent,
        reason=reason,
        tier="MID",
        ledger=ledger,
        confirmed=True,
    )
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

AS_BUILT_PATH = "/home/sovereign/docs/as-built.md"

# ── Scope definition ──────────────────────────────────────────────────────────
# Each entry: (label, policy_tier, match_function)
# Checked in order; first match wins. skill-checksums.json before generic skill paths.
_SCOPE: list[tuple[str, str, object]] = [
    (
        "Skill integrity reference (skill-checksums.json)",
        "HIGH",
        lambda p: "skill-checksums.json" in p,
    ),
    (
        "Sovereign identity (sovereign-soul.md)",
        "HIGH",
        lambda p: "sovereign-soul.md" in p,
    ),
    (
        "Governance policy (governance.json)",
        "ANY",
        lambda p: "governance.json" in p,
    ),
    (
        "Security pattern file",
        "MID",
        lambda p: p.startswith("/home/sovereign/security") and p.endswith(".yaml"),
    ),
    (
        "Specialist persona",
        "MID",
        lambda p: p.startswith("/home/sovereign/personas/"),
    ),
    (
        "Skill definition",
        "MID",
        lambda p: p.startswith("/home/sovereign/skills/"),
    ),
]


def is_in_scope(path: str) -> bool:
    """Return True if path falls under the config change notification policy."""
    return any(fn(path) for _, _, fn in _SCOPE)


def get_policy_tier(path: str) -> Optional[str]:
    """Return the policy tier for a path, or None if not in scope."""
    for _, tier, fn in _SCOPE:
        if fn(path):
            return tier
    return None


def _get_scope_entry(path: str) -> Optional[tuple[str, str]]:
    """Return (label, policy_tier) for the first matching scope entry, or None."""
    for label, tier, fn in _SCOPE:
        if fn(path):
            return label, tier
    return None


# ── Primary API ───────────────────────────────────────────────────────────────

async def notify_config_change(
    path: str,
    narrative: str,
    proposed_by: str,
    reason: str,
    tier: str,
    ledger=None,
    technical: Optional[dict] = None,
) -> None:
    """Send Director Telegram notification and append as-built.md entry.

    Must be called AFTER the write succeeds and Director confirmation has been
    received. Do not call speculatively or before write.

    Args:
        path:        Absolute path to the modified file.
        narrative:   Plain English description of what changed (CEO-readable).
                     Goes into as-built.md and the Telegram message.
                     Must not contain raw file content, diffs, or checksums.
        proposed_by: Agent or component that proposed the change (e.g. "devops_agent").
        reason:      Reason given for the change (Director-supplied or agent-derived).
        tier:        Governance tier of the originating action ("LOW"/"MID"/"HIGH"/"ANY").
        ledger:      AuditLedger instance — technical detail logged here, not as-built.md.
        technical:   Optional dict of technical fields for the audit entry only
                     (e.g. file_hash, checksum, clawhub_slug).
    """
    entry = _get_scope_entry(path)
    if not entry:
        logger.debug("notify_config_change: path not in scope: %s", path)
        return

    label, policy_tier = entry
    ts = datetime.now(timezone.utc)

    # Telegram notification
    await _send_telegram(path, label, narrative, proposed_by, reason, tier, ts)

    # as-built.md narrative entry
    _append_as_built(path, label, narrative, proposed_by, reason, tier, ts)

    # Audit ledger — technical detail only
    if ledger:
        data: dict = {
            "path": path,
            "label": label,
            "proposed_by": proposed_by,
            "reason": reason[:500],
            "narrative_preview": narrative[:300],
            "tier": tier,
            "policy_tier": policy_tier,
        }
        if technical:
            data.update({k: v for k, v in technical.items() if k not in data})
        try:
            ledger.append("config_change_notification", "post_write", data)
        except Exception as e:
            logger.warning("ConfigChangeNotifier: audit write failed: %s", e)


# ── Future-proof config write helper ─────────────────────────────────────────

async def config_write(
    path: str,
    content: str,
    proposed_by: str,
    reason: str,
    tier: str,
    ledger=None,
    confirmed: bool = False,
    narrative: Optional[str] = None,
    technical: Optional[dict] = None,
) -> dict:
    """Gate, write, and notify for in-scope config file writes.

    Intended for future adapters that need to write governance.json, persona files,
    or security pattern files. The current write adapters (WebDAV) write to Nextcloud,
    not to RAID paths — this helper is for direct RAID file writes only.

    Args:
        path:        Absolute RAID path to write.
        content:     File content to write.
        proposed_by: Agent proposing the change.
        reason:      Reason for the change.
        tier:        Governance tier (determines confirmation requirement).
        ledger:      AuditLedger instance.
        confirmed:   True only when Director has explicitly confirmed (gateway sets this).
        narrative:   Plain English summary; auto-generated from path + reason if not provided.
        technical:   Optional technical metadata for audit log.

    Returns:
        {"status": "ok", "path": path}
        or {"requires_confirmation": True, "tier": tier, "path": path}
        or {"error": "..."}
    """
    entry = _get_scope_entry(path)
    if not entry:
        # Not in scope — write without notification
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return {"status": "ok", "path": path}
        except OSError as e:
            return {"error": str(e)}

    label, policy_tier = entry

    # Confirmation gate — HIGH tier (soul, checksums) requires double confirmation.
    # The execution engine enforces this via governance before _dispatch is called,
    # but config_write enforces it independently for direct callers.
    if not confirmed:
        double = policy_tier == "HIGH" or tier == "HIGH"
        resp: dict = {
            "path": path,
            "label": label,
            "tier": tier,
            "policy_tier": policy_tier,
            "warning": (
                f"Modifying '{label}' requires Director confirmation before writing. "
                f"Resubmit with confirmed=True."
            ),
        }
        if double:
            resp["requires_double_confirmation"] = True
        else:
            resp["requires_confirmation"] = True
        return resp

    # Write the file
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    except OSError as e:
        return {"error": f"Write failed: {e}"}

    # Build narrative if not provided
    if not narrative:
        filename = path.split("/")[-1]
        narrative = f"{label} file '{filename}' was updated by {proposed_by}. Reason: {reason}"

    # Post-write notification
    await notify_config_change(
        path=path,
        narrative=narrative,
        proposed_by=proposed_by,
        reason=reason,
        tier=tier,
        ledger=ledger,
        technical=technical,
    )

    return {"status": "ok", "path": path}


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _send_telegram(
    path: str,
    label: str,
    narrative: str,
    proposed_by: str,
    reason: str,
    tier: str,
    ts: datetime,
) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID")
    if not token or not chat_id:
        logger.info(
            "ConfigChangeNotifier: Telegram credentials absent — notification skipped for %s",
            path.split("/")[-1],
        )
        return

    tier_icon = {"LOW": "ℹ️", "MID": "⚡", "HIGH": "🔴", "ANY": "⚙️"}.get(tier, "⚙️")
    filename = path.split("/")[-1]
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{tier_icon} *Config Change — {label}*",
        f"_{ts_str}_",
        "",
        f"*What changed:* {narrative}",
        f"*Proposed by:* `{proposed_by}`",
        f"*Reason:* {reason}",
        f"*Tier:* {tier} — Director confirmation received before write.",
        f"*File:* `{filename}`",
        "",
        "_Technical checksums and audit trail are in the security ledger._",
    ]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "\n".join(lines),
                    "parse_mode": "Markdown",
                },
            )
        logger.info(
            "ConfigChangeNotifier: Telegram notification sent for %s", filename
        )
    except Exception as e:
        logger.warning(
            "ConfigChangeNotifier: Telegram notification failed for %s: %s", filename, e
        )


# ── as-built.md ───────────────────────────────────────────────────────────────

def _append_as_built(
    path: str,
    label: str,
    narrative: str,
    proposed_by: str,
    reason: str,
    tier: str,
    ts: datetime,
) -> None:
    """Append a CEO-readable narrative config-change entry to as-built.md.

    Format is intentionally plain English. Technical detail (exact checksums,
    file hashes, byte counts) belongs in the audit ledger, not here.
    """
    filename = path.split("/")[-1]
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")
    dir_label = "/".join(path.split("/")[:-1]).replace("/home/sovereign/", "")

    lines = [
        "",
        "",
        f"## Config Change — {label} ({ts_str})",
        "",
        f"**What changed:** {narrative}",
        "",
        f"- **File:** `{filename}` (in `{dir_label}/`)",
        f"- **Proposed by:** {proposed_by}",
        f"- **Reason:** {reason}",
        f"- **Tier:** {tier} — Director confirmation was received before the write.",
        "",
        "_Full technical detail (checksums, audit hash) is in the security ledger._",
    ]

    try:
        os.makedirs(os.path.dirname(AS_BUILT_PATH), exist_ok=True)
        with open(AS_BUILT_PATH, "a") as f:
            f.write("\n".join(lines))
        logger.info(
            "ConfigChangeNotifier: as-built.md entry appended for %s", filename
        )
    except OSError as e:
        logger.error(
            "ConfigChangeNotifier: failed to append as-built.md for %s: %s", filename, e
        )
