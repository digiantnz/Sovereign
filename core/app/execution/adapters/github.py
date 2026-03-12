import base64
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx

# ── Pre-push secret scanner ───────────────────────────────────────────────────
# These patterns are checked against file content AND path before every push.
# A match blocks the push entirely — no exceptions, no bypass.
_SECRET_PATTERNS = [
    # Assignment patterns: negative lookahead excludes placeholder values (<REVOKED>, <REDACTED>, etc.)
    # so already-redacted documentation does not trigger a block.
    (re.compile(r'[A-Z_]*API[_\s]*KEY\s*=\s*(?!<)[^\s<]{8,}',  re.I), "API key assignment"),
    (re.compile(r'[A-Z_]*TOKEN\s*=\s*(?!<)[^\s<]{8,}',          re.I), "token assignment"),
    (re.compile(r'[A-Z_]*PASSWORD\s*=\s*(?!<)[^\s<]{4,}',       re.I), "password assignment"),
    (re.compile(r'[A-Z_]*SECRET\s*=\s*[a-f0-9]{16,}',           re.I), "secret hex value"),
    (re.compile(r'-----BEGIN .{1,30}PRIVATE KEY-----'),                  "private key block"),
    (re.compile(r'\bBearer\s+[A-Za-z0-9\-._~+/]{20,}'),                 "Bearer token"),
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}'),                              "sk- prefixed key (OpenAI-style)"),
    (re.compile(r'\bghp_[A-Za-z0-9]{30,}'),                             "GitHub PAT"),
    (re.compile(r'\bghs_[A-Za-z0-9]{30,}'),                             "GitHub App token"),
    (re.compile(r'\bBSA[A-Za-z0-9\-_]{10,}'),                           "Brave Search API key"),
    # RFC1918 IPs — only checked for non-documentation files.
    # as-built.md and design docs legitimately reference internal infrastructure.
    # The _DOC_EXTENSIONS set in _scan_for_secrets controls which paths skip this check.
    (re.compile(r'\b(?:192\.168|172\.1[6-9]\.|172\.2[0-9]\.|172\.3[01]\.|10\.)\d+\.\d+'),
                                                                         "RFC1918 internal IP"),
]

# File extensions where RFC1918 IP references are expected (infrastructure documentation).
_DOC_EXTENSIONS = {".md", ".txt", ".rst"}

# Paths that are blocked unconditionally regardless of content
_BLOCKED_PATH_PATTERNS = [
    re.compile(r'(^|/)secrets/',    re.I),
    re.compile(r'\.env(\.|$)',      re.I),
    re.compile(r'\.key$',           re.I),
    re.compile(r'\.pem$',           re.I),
    re.compile(r'\.p12$',           re.I),
]


def _scan_for_secrets(path: str, content: str) -> list[str]:
    """Scan path and content for secrets. Returns list of violation descriptions.
    Empty list = clean. Non-empty = block the push.

    RFC1918 IPs are skipped for documentation file extensions (.md, .txt, .rst)
    because as-built.md and design docs legitimately reference internal infrastructure.
    Assignment patterns (API_KEY=, TOKEN=, PASSWORD=) already exclude placeholder values
    via negative lookahead — redacted values like <REVOKED> are allowed in documentation.
    """
    violations = []
    for pattern in _BLOCKED_PATH_PATTERNS:
        if pattern.search(path):
            violations.append(f"Blocked path pattern matched: {path}")
            return violations  # path block is terminal — no need to scan content

    is_doc = any(path.lower().endswith(ext) for ext in _DOC_EXTENSIONS)

    for pattern, label in _SECRET_PATTERNS:
        if label == "RFC1918 internal IP" and is_doc:
            continue  # internal IPs are expected in infrastructure documentation
        match = pattern.search(content)
        if match:
            # Report match position but never log the actual secret value
            violations.append(f"{label} detected at char {match.start()}")
    return violations

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/prompt-security/clawsec/releases"
ADVISORY_URL = "https://clawsec.prompt.security/advisories/feed.json"
PENDING_DIR = "/home/sovereign/security/pending"
KNOWN_PATH = "/home/sovereign/security/.known_releases.json"

# Sovereign git identity — loaded from secrets/github.env at runtime.
# Token is DCL SECRET tier: never passed to external LLMs, never logged.
_GIT_NAME  = os.environ.get("GITHUB_GIT_NAME",  "Sovereign")
_GIT_EMAIL = os.environ.get("GITHUB_GIT_EMAIL", "rex@digiant.nz")
_REPO_OWNER = os.environ.get("GITHUB_REPO_OWNER", "digiantnz")
_REPO_NAME  = os.environ.get("GITHUB_REPO_NAME",  "Sovereign")


class GitHubAdapter:
    """Sovereign-owned GitHub adapter.

    Identity: name={_GIT_NAME}, email={_GIT_EMAIL}
    Repo:     https://github.com/{_REPO_OWNER}/{_REPO_NAME}
    Auth:     GITHUB_PAT from secrets/github.env — token owned by Sovereign, not CC.
    """

    def __init__(self):
        os.makedirs(PENDING_DIR, exist_ok=True)
        # Load PAT lazily at first use to avoid startup failures if env not set
        self._pat: str | None = None

    def _get_pat(self) -> str | None:
        if self._pat is None:
            self._pat = os.environ.get("GITHUB_PAT") or None
        return self._pat

    def _auth_headers(self) -> dict:
        pat = self._get_pat()
        if not pat:
            return {"Accept": "application/vnd.github+json"}
        return {
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── Sovereign repo operations (Contents API — no git binary required) ──

    async def push_file(self, path: str, content: str, message: str,
                        branch: str = "main") -> dict:
        """Create or update a file in Sovereign's repo via GitHub Contents API.

        path:    repo-relative path, e.g. "soul/Sovereign-soul.md"
        content: plain text file content
        message: commit message
        Returns {"status": "ok", "sha": "<commit_sha>", "path": path} or {"error": ...}
        """
        pat = self._get_pat()
        if not pat:
            return {"error": "GITHUB_PAT not configured — cannot push to repo"}

        url = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/contents/{path.lstrip('/')}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        # ── Secret scan — block before any bytes leave the system ────────
        violations = _scan_for_secrets(path, content)
        if violations:
            logger.error("GitHubAdapter: push_file BLOCKED — secret scan failed for %s: %s",
                         path, violations)
            return {
                "error": "SECRET_SCAN_BLOCKED",
                "path": path,
                "violations": violations,
                "message": (
                    f"Push to {path} blocked by pre-push secret scanner. "
                    f"Violations: {'; '.join(violations)}. "
                    "Remove secrets before pushing."
                ),
            }

        payload: dict = {
            "message": message,
            "content": encoded,
            "branch":  branch,
            "committer": {"name": _GIT_NAME, "email": _GIT_EMAIL},
            "author":    {"name": _GIT_NAME, "email": _GIT_EMAIL},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Check if file already exists (need its SHA to update)
            existing = await client.get(url, headers=self._auth_headers())
            if existing.status_code == 200:
                payload["sha"] = existing.json().get("sha", "")

            r = await client.put(url, headers=self._auth_headers(), json=payload)
            if r.status_code in (200, 201):
                data = r.json()
                commit_sha = data.get("commit", {}).get("sha", "")
                logger.info("GitHubAdapter: pushed %s (commit %s)", path, commit_sha[:8])
                return {"status": "ok", "sha": commit_sha, "path": path}
            else:
                logger.warning("GitHubAdapter: push_file failed %s: %s", r.status_code, r.text[:200])
                return {"error": f"GitHub API {r.status_code}", "detail": r.text[:200]}

    def _load_known(self) -> dict:
        if not os.path.exists(KNOWN_PATH):
            return {"releases": [], "advisories": []}
        try:
            with open(KNOWN_PATH) as f:
                return json.load(f)
        except Exception:
            return {"releases": [], "advisories": []}

    def _save_known(self, known: dict):
        with open(KNOWN_PATH, "w") as f:
            json.dump(known, f, indent=2)

    async def check_releases(self) -> list[dict]:
        """Fetch latest ClawSec releases. Return new ones since last check.
        Writes new release metadata to pending/ for Director review."""
        known = self._load_known()
        known_tags = set(known.get("releases", []))

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    RELEASES_URL,
                    headers=self._auth_headers(),
                )
                r.raise_for_status()
                releases = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Repo may not exist yet — return empty gracefully
                return []
            raise
        except Exception as e:
            logger.warning("GitHubAdapter: release fetch failed: %s", e)
            return []

        new_releases = []
        for rel in releases:
            tag = rel.get("tag_name", "")
            if not tag or tag in known_tags:
                continue
            metadata = {
                "tag": tag,
                "name": rel.get("name", ""),
                "published_at": rel.get("published_at", ""),
                "html_url": rel.get("html_url", ""),
                "body": rel.get("body", "")[:2000],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "github_release",
                "status": "pending_review",
            }
            pending_path = os.path.join(PENDING_DIR, f"release-{tag}.json")
            try:
                with open(pending_path, "w") as f:
                    json.dump(metadata, f, indent=2)
            except Exception as e:
                logger.warning("GitHubAdapter: failed to write pending file: %s", e)
            known_tags.add(tag)
            new_releases.append(metadata)

        known["releases"] = list(known_tags)
        self._save_known(known)
        return new_releases

    async def fetch_advisory_feed(self) -> list[dict]:
        """Fetch advisory JSON feed. Return advisories not yet in pending/."""
        known = self._load_known()
        known_ids = set(known.get("advisories", []))

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(ADVISORY_URL)
                r.raise_for_status()
                advisories = r.json()
                if not isinstance(advisories, list):
                    advisories = advisories.get("advisories", [])
        except Exception as e:
            logger.warning("GitHubAdapter: advisory feed fetch failed: %s", e)
            return []

        new_advisories = []
        for adv in advisories:
            adv_id = adv.get("id") or adv.get("cve_id", "")
            if not adv_id or adv_id in known_ids:
                continue
            metadata = {
                **adv,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "advisory_feed",
                "status": "pending_review",
            }
            pending_path = os.path.join(PENDING_DIR, f"advisory-{adv_id}.json")
            try:
                with open(pending_path, "w") as f:
                    json.dump(metadata, f, indent=2)
            except Exception as e:
                logger.warning("GitHubAdapter: failed to write advisory file: %s", e)
            known_ids.add(adv_id)
            new_advisories.append(metadata)

        known["advisories"] = list(known_ids)
        self._save_known(known)
        return new_advisories

    async def get_pending_updates(self) -> list[dict]:
        """List files in pending/ dir — items awaiting Director review."""
        items = []
        try:
            for fname in sorted(os.listdir(PENDING_DIR)):
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(PENDING_DIR, fname)
                try:
                    with open(path) as f:
                        items.append(json.load(f))
                except Exception:
                    items.append({"file": fname, "error": "parse failed"})
        except Exception as e:
            logger.warning("GitHubAdapter: pending list failed: %s", e)
        return items
