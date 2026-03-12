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
