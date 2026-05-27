import os
import re
import yaml
from dataclasses import dataclass, field

SECURITY_DIR = "/home/sovereign/security"


@dataclass
class ScanResult:
    flagged: bool
    categories: list = field(default_factory=list)
    matched_phrases: list = field(default_factory=list)


class SecurityScanner:
    def __init__(self, security_dir: str = SECURITY_DIR):
        self._dir = security_dir
        self._literal_patterns: dict[str, list[str]] = {}  # category -> phrases
        self._regex_patterns: list[tuple[str, re.Pattern]] = []  # (category, compiled)

    def load(self):
        """Load all YAML pattern files. Call at startup."""
        self._literal_patterns = {}
        self._regex_patterns = []

        injection = self._load_yaml("injection_patterns.yaml")
        if injection:
            for category, phrases in injection.items():
                if category == "version":
                    continue
                if category == "prompt_injection_regex":
                    for pattern in (phrases or []):
                        try:
                            self._regex_patterns.append(
                                (category, re.compile(pattern))
                            )
                        except re.error:
                            pass
                else:
                    self._literal_patterns[category] = [
                        p.lower() for p in (phrases or [])
                    ]

        sensitive = self._load_yaml("sensitive_data_patterns.yaml")
        if sensitive:
            for category, phrases in sensitive.items():
                if category == "version":
                    continue
                self._literal_patterns.setdefault(f"sensitive_{category}", []).extend(
                    [p.lower() for p in (phrases or [])]
                )

        for fname, category in [
            ("destructive_commands.yaml", "destructive_commands"),
            ("exfiltration_patterns.yaml", "exfiltration"),
        ]:
            data = self._load_yaml(fname)
            if data and "patterns" in data:
                for item in data["patterns"]:
                    pattern = item.get("pattern", "")
                    if pattern:
                        try:
                            self._regex_patterns.append(
                                (category, re.compile(pattern, re.IGNORECASE))
                            )
                        except re.error:
                            pass

        self._load_clawsec_dynamic()

    def _load_clawsec_dynamic(self) -> None:
        """Load dynamic patterns from clawsec_dynamic.yaml (written by clawsec_harness).

        File is optional — silently skipped if absent (before first clawsec_update run).
        Format: {version, updated, categories: {name: [{pattern, action, source_id, severity}]}}
        """
        path = os.path.join(self._dir, "clawsec_dynamic.yaml")
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            categories = data.get("categories", {})
            added = 0
            for cat_name, entries in categories.items():
                for entry in (entries or []):
                    pattern = entry.get("pattern", "")
                    if not pattern:
                        continue
                    try:
                        self._regex_patterns.append(
                            (cat_name, re.compile(pattern, re.IGNORECASE))
                        )
                        added += 1
                    except re.error:
                        pass
            if added:
                import logging as _log
                _log.getLogger(__name__).info(
                    "SecurityScanner: loaded %d dynamic patterns from clawsec_dynamic.yaml", added
                )
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "SecurityScanner: failed to load clawsec_dynamic.yaml: %s", exc
            )

    def _load_yaml(self, filename: str) -> dict:
        path = os.path.join(self._dir, filename)
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def scan(self, text: str) -> ScanResult:
        """Deterministic scan. No LLM calls. Returns ScanResult."""
        text_lower = text.lower()
        categories = []
        matched_phrases = []

        for category, phrases in self._literal_patterns.items():
            for phrase in phrases:
                if phrase in text_lower:
                    if category not in categories:
                        categories.append(category)
                    matched_phrases.append(phrase)

        for category, pattern in self._regex_patterns:
            match = pattern.search(text)
            if match:
                if category not in categories:
                    categories.append(category)
                matched_phrases.append(match.group(0)[:80])

        return ScanResult(
            flagged=bool(categories),
            categories=categories,
            matched_phrases=list(set(matched_phrases)),
        )
