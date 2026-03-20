import json

class GovernanceEngine:
    def __init__(self, path):
        with open(path, 'r') as f:
            self.policy = json.load(f)
        self.meta = self.policy.get("meta", {})

    def get_intent_tier(self, intent: str) -> str | None:
        """Return the governance-mandated tier for an intent, if defined in intent_tiers.
        Returns None if the intent is not found (caller falls back to INTENT_TIER_MAP)."""
        return self.policy.get("intent_tiers", {}).get(intent)

    def validate(self, action, tier):
        """
        Validates if the action is allowed under the given tier.
        action: dict with keys 'domain', 'operation', and optionally 'name'
                (e.g., {'domain': 'docker', 'operation': 'read', 'name': 'docker_ps'})
        Returns the tier rules dict if allowed; raises ValueError otherwise.
        """
        if tier not in self.policy["tiers"]:
            raise ValueError(f"Invalid tier: {tier}")

        rules = self.policy["tiers"][tier]

        domain = action.get('domain')
        operation = action.get('operation')
        name = action.get('name')

        if not domain or not operation:
            raise ValueError("Action must include 'domain' and 'operation' keys")

        # If a specific action name is given, check the allowed_actions whitelist
        if name and name not in rules.get('allowed_actions', []):
            raise ValueError(f"Action '{name}' not in allowed_actions for tier {tier}")

        # Domain/operation checks
        if domain == 'docker':
            if operation in ['read', 'ps', 'logs', 'stats']:
                if rules.get('docker_read', False):
                    return rules
            elif operation in rules.get('docker_workflows', []):
                return rules
        elif domain == 'file':
            if operation == 'read' and rules.get('file_read', False):
                return rules
            elif operation == 'write' and rules.get('file_write', False):
                return rules
            elif operation == 'delete' and rules.get('file_delete', False):
                return rules
        elif domain == 'mail':
            if operation in ('read', 'search') and rules.get('mail_read', False):
                return rules
            elif operation == 'move' and rules.get('mail_move', False):
                return rules
            elif operation == 'delete' and rules.get('mail_delete', False):
                return rules
            elif operation == 'send' and rules.get('mail_send', False):
                return rules
        elif domain == 'webdav':
            if operation == 'read' and rules.get('webdav_read', False):
                return rules
            elif operation == 'write' and rules.get('webdav_write', False):
                return rules
            elif operation == 'delete' and rules.get('webdav_delete', False):
                return rules
            elif operation == 'mkdir' and rules.get('webdav_mkdir', False):
                return rules
        elif domain == 'caldav':
            if operation == 'read' and rules.get('caldav_read', False):
                return rules
            elif operation == 'write' and rules.get('calendar_write', False):
                return rules
            elif operation == 'delete' and rules.get('calendar_delete', False):
                return rules
        elif domain == 'ollama':
            if operation == 'query' and rules.get('ollama_query', False):
                return rules
        elif domain == 'memory':
            if operation in ('read', 'write', 'search', 'store') and rules.get('memory_write', False):
                return rules
            elif operation == 'promote' and rules.get('memory_promote', False):
                return rules
        elif domain == 'scheduler':
            # list + recall are read-only (memory_write suffices at LOW tier)
            if operation in ('list', 'recall') and rules.get('memory_write', False):
                return rules
            # schedule/update — LOW tier (internal memory write, no Director confirmation)
            if operation in ('schedule', 'update') and rules.get('memory_write', False):
                return rules
        elif domain == 'skills':
            if operation in ('search', 'review', 'audit') and rules.get('skill_read', False):
                return rules
            if operation in ('load', 'unload') and rules.get('skill_load', False):
                return rules
            # install = composite (search+review+load); search/review are LOW read ops.
            # The load step inside the composite flow has its own confirmed=True gate.
            # Allow at LOW (skill_read) or MID (skill_load) — composite handles confirmation.
            if operation == 'install' and (rules.get('skill_read', False) or rules.get('skill_load', False)):
                return rules
        elif domain == 'security':
            if operation in ('check_updates', 'read') and rules.get('security_read', False):
                return rules
        elif domain == 'browser':
            if operation == 'search' and rules.get('browser_search', False):
                return rules
            elif operation == 'fetch' and rules.get('browser_fetch', False):
                return rules
        elif domain == 'github':
            if operation == 'read' and rules.get('github_read', False):
                return rules
            elif operation == 'push_doc' and rules.get('github_push_doc', False):
                return rules
            elif operation in ('push_soul', 'push_sec') and rules.get('github_push_soul', False):
                return rules
        elif domain == 'feeds':
            # RSS/Atom feed reads via nanobot rss-digest — LOW tier, read-only
            if operation == 'read' and rules.get('memory_write', False):
                return rules
        elif domain == 'browser_config':
            # Browser auth profile configuration — writes to RAID YAML; MID tier required
            if operation == 'configure_auth' and rules.get('file_write', False):
                return rules
        elif domain == 'wallet':
            if operation == 'read' and rules.get('wallet_read', False):
                return rules
            elif operation == 'sign' and rules.get('wallet_sign', False):
                return rules
            elif operation == 'propose' and rules.get('wallet_propose', False):
                return rules

        raise ValueError(f"Action {action} not allowed under tier {tier}")
