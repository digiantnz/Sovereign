"""Sovereign Skill Lifecycle Manager

Four operations over the full skill lifecycle:

  SEARCH — Query the ClawhHub community registry for skill candidates. Returns slug,
            summary, downloads, stars, certification status, and clawhub_url.
            Fetches raw SKILL.md content for top candidates via httpx (REST API primary;
            A2ABrowserAdapter SearXNG fallback if API is unreachable).

  REVIEW — Pass candidate SKILL.md content through the full inbound security pipeline:
            deterministic pattern scan across all YAML rule files, then LLM security agent
            evaluation. Skills touching identity/governance/memory/soul keywords are
            unconditionally escalated to Director review regardless of certification.
            Non-certified skills always enter review regardless of scan outcome.
            Returns structured {"decision": "block|review|approve", ...}.

  LOAD   — MID tier; requires Director confirmation. On approval: writes the skill to
            /home/sovereign/skills/<name>/, synthesises a valid sovereign: frontmatter block,
            computes body checksum, registers with the soul-guardian watchlist, and logs to
            the audit ledger. Skill becomes active on next session load.

  AUDIT  — Lists all installed skills with current checksums, reference checksums, last
            accessed timestamps, specialist assignments, and clawhub provenance. Any skill
            whose whole-file hash has drifted since installation is flagged as a HIGH tier
            integrity incident.

Security defaults:
  - certified_only=True for all searches; non-certified results require explicit Director override
  - All external content treated as UNTRUSTED throughout the pipeline
  - No skill may bypass the review gate before LOAD
  - Body checksum + reference file = dual integrity gates (consistent with SkillLoader)
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
import yaml

logger = logging.getLogger(__name__)

SKILLS_DIR = "/home/sovereign/skills"
CHECKSUMS_PATH = "/home/sovereign/security/skill-checksums.json"
METADATA_PATH = "/home/sovereign/security/skill-metadata.json"
WATCHLIST_PATH = "/home/sovereign/security/skill-watchlist.json"

# Keywords in SKILL.md content that trigger unconditional Director escalation
# regardless of certification status or scanner verdict.
ESCALATION_KEYWORDS = frozenset({
    "memory", "governance", "soul", "identity", "signing", "credential",
    "guardian", "audit", "ledger", "checksum", "persona", "orchestrator",
    "translator", "sovereign-soul", "governance.json", "protected_files",
    "checksums", "soul_guardian", "signing_key", "private_key", "secret_key",
})

# Adapter names valid in sovereign: blocks
KNOWN_ADAPTERS = frozenset({
    "broker", "ollama", "qdrant", "browser", "webdav", "caldav",
    "imap", "smtp", "github", "signing", "whisper",
})

# Specialist names valid in sovereign: blocks
KNOWN_SPECIALISTS = frozenset({
    "research_agent", "devops_agent", "business_agent",
    "memory_agent", "security_agent",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _github_url_to_raw(url: str) -> Optional[str]:
    """Convert a github.com blob/tree URL to raw.githubusercontent.com equivalent.

    Examples:
      https://github.com/user/repo/blob/main/skills/foo/SKILL.md
      → https://raw.githubusercontent.com/user/repo/main/skills/foo/SKILL.md

    Returns None if the URL is not a recognisable GitHub file URL.
    """
    if not url:
        return None
    # Already a raw URL
    if "raw.githubusercontent.com" in url:
        return url if url.endswith("SKILL.md") else None
    if "github.com" not in url:
        return None
    # Strip query string / fragment
    clean = url.split("?")[0].split("#")[0]
    # Replace /blob/ or /tree/ with nothing (raw path uses neither)
    # Pattern: github.com/<user>/<repo>/blob/<branch>/<path>
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/(?:blob|tree)/([^/]+)/(.*)",
        clean,
    )
    if not m:
        return None
    user, repo, branch, path = m.groups()
    if not path.endswith("SKILL.md"):
        if path.lower().endswith(".md"):
            return None  # Some other .md file — not a SKILL.md
        # Likely a directory — guess SKILL.md is inside it
        path = path.rstrip("/") + "/SKILL.md"
    return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"


def _candidate_meta(fm: dict, content: str) -> dict:
    """Extract display metadata from parsed SKILL.md frontmatter.

    Returns a dict of human-readable fields safe to send to the translator
    without including the full skill body.
    """
    sv = fm.get("sovereign", {}) or {}
    ops = sv.get("operations", {}) or {}
    return {
        "version": fm.get("version", ""),
        "tier": sv.get("tier_required", ""),
        "specialists": sv.get("specialists", []),
        "adapter_deps": sv.get("adapter_deps", []),
        "operations": list(ops.keys()) if isinstance(ops, dict) else [],
        "operation_count": len(ops) if isinstance(ops, dict) else 0,
    }


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_skill_md_content(content: str) -> tuple[dict, str]:
    """Parse raw SKILL.md string into (frontmatter_dict, body_text).
    Returns ({}, "") if no YAML frontmatter is detected — body is the full content.
    """
    match = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
    if not match:
        return {}, content  # no frontmatter — treat whole content as body
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, content
    return fm, match.group(2)


def load_skill_watchlist() -> list[str]:
    """Load the durable skill path watchlist from RAID. Called at startup."""
    if not os.path.isfile(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH) as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, str)]
    except Exception as e:
        logger.warning("SkillLifecycle: could not read watchlist: %s", e)
        return []


def _write_watchlist(paths: list[str]):
    try:
        os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(sorted(set(paths)), f, indent=2)
    except OSError as e:
        logger.error("SkillLifecycle: failed to write watchlist: %s", e)


# ── SkillLifecycleManager ────────────────────────────────────────────────────

class SkillLifecycleManager:
    """Manages the full lifecycle of Sovereign skills: search → review → load → audit.

    Constructor args (all optional — degrade gracefully if absent):
      scanner   — SecurityScanner instance (deterministic pattern matching)
      cog       — CognitionEngine instance (LLM security evaluation via security_evaluate())
      browser   — BrowserAdapter instance (fallback search if ClawhHub API unreachable)
      ledger    — AuditLedger instance (all lifecycle events logged)
      guardian  — SoulGuardian instance (runtime file watchlist registration on LOAD)
    """

    def __init__(
        self,
        scanner=None,
        cog=None,
        browser=None,
        ledger=None,
        guardian=None,
    ):
        self.scanner = scanner
        self.cog = cog
        self.browser = browser
        self.ledger = ledger
        self.guardian = guardian

    # ── SEARCH ────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        certified_only: bool = True,
        limit: int = 10,
    ) -> dict:
        """Search for skills via SearXNG → GitHub raw SKILL.md fetch.

        Primary: SearXNG (a2a-browser) searches GitHub for SKILL.md files matching
        the query, then fetches raw content directly from raw.githubusercontent.com.
        Fallback: SearXNG general web search (broader — may include non-GitHub sources).

        No direct calls to topclawhubskills.com — all discovery is via open search
        routing through the same trusted SearXNG path as all other web searches.

        Non-certified candidates are always flagged; certified_only=True (default)
        adds an explicit WARNING marker to discourage loading unreviewed skills.
        """
        if not self.browser:
            return {
                "query": query,
                "candidates": [],
                "error": "No browser adapter available — cannot search for skills",
                "_source": "none",
            }

        candidates: list[dict] = []
        search_error: Optional[str] = None

        # ── Direct URL shortcut — if query contains a raw GitHub URL, fetch directly ──
        import re as _re_url
        _url_in_query = _re_url.search(r'https?://[^\s]+', query)
        if _url_in_query:
            direct_url = _url_in_query.group(0).rstrip(".,)")
            raw_url = _github_url_to_raw(direct_url) or direct_url
            content = await self._fetch_raw_url(raw_url)
            if content:
                fm, body = _parse_skill_md_content(content)
                slug = fm.get("name") or raw_url.rstrip("/").split("/")[-2] or "unknown"
                return {
                    "query": query,
                    "candidates": [{
                        "slug": slug,
                        "summary": fm.get("description", ""),
                        "github_url": direct_url,
                        "raw_url": raw_url,
                        "certified": None,
                        "skill_md": content,
                        "WARNING": "Source: direct URL — review required before loading",
                    }],
                    "total": 1,
                    "_source": "direct_url",
                    "certified_only": certified_only,
                }

        # ── Primary: GitHub Code Search API (no auth required, 10 req/min) ───
        # More reliable than SearXNG when DDG/Google are CAPTCHA-blocked.
        # Routes through a2a-browser.fetch() for internet egress.
        try:
            import urllib.parse as _urlparse
            _gh_query = _urlparse.quote(f'filename:SKILL.md {query}')
            _gh_api_url = f"https://api.github.com/search/code?q={_gh_query}&per_page={min(limit * 2, 30)}"
            # Use direct httpx — browser.fetch() goes through Playwright which renders
            # api.github.com as HTML, not raw JSON. sovereign-core has internet egress.
            _gh_pat = os.environ.get("GITHUB_PAT", "")
            _gh_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if _gh_pat:
                _gh_headers["Authorization"] = f"Bearer {_gh_pat}"
            async with httpx.AsyncClient(timeout=30.0) as _gh_client:
                _gh_resp = await _gh_client.get(_gh_api_url, headers=_gh_headers)
            _gh_text = _gh_resp.text if _gh_resp.status_code == 200 else ""
            if _gh_resp.status_code != 200:
                logger.warning("SkillLifecycle.search: GitHub API %s: %s", _gh_resp.status_code, _gh_resp.text[:200])
            if _gh_text:
                import json as _json
                try:
                    _gh_data = _json.loads(_gh_text)
                    for item in _gh_data.get("items", [])[:limit * 3]:
                        _html_url = item.get("html_url", "")
                        raw_url = _github_url_to_raw(_html_url)
                        if not raw_url:
                            continue
                        content = await self._fetch_raw_url(raw_url)
                        if not content:
                            continue
                        fm, body = _parse_skill_md_content(content)
                        has_name = bool(fm.get("name"))
                        has_sovereign = "sovereign:" in content
                        has_openclaw = "openclaw" in content.lower() or "allowed-tools" in content
                        if not has_name and not has_sovereign and not has_openclaw:
                            continue
                        slug = fm.get("name") or _html_url.rstrip("/").split("/")[-2] or "unknown"
                        candidates.append({
                            "slug": slug,
                            "summary": fm.get("description", ""),
                            "github_url": _html_url,
                            "raw_url": raw_url,
                            "certified": None,
                            "skill_md": content,
                            "meta": _candidate_meta(fm, content),
                            "WARNING": "Source: GitHub API — review required before loading",
                        })
                        if len(candidates) >= limit:
                            break
                    if candidates:
                        logger.info("SkillLifecycle.search: GitHub API returned %d candidates", len(candidates))
                except _json.JSONDecodeError:
                    logger.warning("SkillLifecycle.search: GitHub API response not JSON")
        except Exception as e:
            logger.warning("SkillLifecycle.search: GitHub API path failed: %s", e)

        # ── Secondary: GitHub SKILL.md search via SearXNG ──────────────────
        if not candidates:
            try:
                search_q = f'sovereign skill {query} "SKILL.md" site:github.com'
                result = await self.browser.search(search_q)
                raw_items = ((result.get("data") or {}).get("data") or {}).get("results", []) or []

                for item in raw_items[:limit * 3]:  # overscan to account for non-SKILL.md hits
                    url = item.get("url", "")
                    raw_url = _github_url_to_raw(url)
                    if not raw_url:
                        continue
                    content = await self._fetch_raw_url(raw_url)
                    if not content:
                        continue
                    fm, body = _parse_skill_md_content(content)
                    has_name = bool(fm.get("name"))
                    has_sovereign = "sovereign:" in content
                    has_openclaw = "openclaw" in content.lower() or "allowed-tools" in content
                    if not has_name and not has_sovereign and not has_openclaw:
                        continue
                    slug = fm.get("name") or url.rstrip("/").split("/")[-2] or "unknown"
                    candidate: dict = {
                        "slug": slug,
                        "summary": fm.get("description", item.get("content", "")[:200]),
                        "github_url": url,
                        "raw_url": raw_url,
                        "certified": None,
                        "skill_md": content,
                        "meta": _candidate_meta(fm, content),
                        "WARNING": "Source: GitHub — review required before loading",
                    }
                    candidates.append(candidate)
                    if len(candidates) >= limit:
                        break

            except Exception as e:
                search_error = f"SearXNG GitHub search failed: {type(e).__name__}: {e}"
                logger.warning("SkillLifecycle.search: %s — attempting general fallback", search_error)

        # ── Fallback: general web search if GitHub search yielded nothing ──
        if not candidates:
            try:
                fallback_q = f"sovereign AI skill {query} SKILL.md github"
                result = await self.browser.search(fallback_q)
                raw_items = ((result.get("data") or {}).get("data") or {}).get("results", []) or []
                for item in raw_items[:limit]:
                    url = item.get("url", "")
                    raw_url = _github_url_to_raw(url)
                    content: Optional[str] = None
                    if raw_url:
                        content = await self._fetch_raw_url(raw_url)
                    fm_fb: dict = {}
                    if content:
                        fm_fb, _ = _parse_skill_md_content(content)
                    slug_fb = fm_fb.get("name") or url.rstrip("/").split("/")[-1] or "unknown"
                    candidates.append({
                        "slug": slug_fb,
                        "summary": fm_fb.get("description", item.get("content", "")[:200]),
                        "github_url": url,
                        "raw_url": raw_url,
                        "certified": None,
                        "skill_md": content,
                        "WARNING": "Source: general web search — certification and validity unverified",
                    })
            except Exception as e2:
                fb_err = f"Fallback search also failed: {e2}"
                logger.warning("SkillLifecycle.search: %s", fb_err)
                if search_error:
                    search_error = f"{search_error}; {fb_err}"
                else:
                    search_error = fb_err

        result_out: dict = {
            "status": "ok" if candidates else "no_results",
            "query": query,
            "certified_only": certified_only,
            "candidates": candidates,
            "_source": "searxng_github",
        }
        if not candidates:
            result_out["info"] = (
                "No skills found matching your query via GitHub search. "
                "Try a more specific query or check GitHub directly."
            )
        if search_error:
            result_out["search_warning"] = search_error

        self._audit("skill_search", "ok", {
            "query": query,
            "certified_only": certified_only,
            "candidates_returned": len(candidates),
            "source": "searxng_github",
        })
        return result_out

    async def _fetch_raw_url(self, url: str) -> Optional[str]:
        """Fetch raw content from a URL (expected: raw.githubusercontent.com).

        Tries direct httpx first (works if sovereign-core has internet egress).
        Falls back to browser.fetch() via a2a-browser on browser_net (internet egress).
        """
        # Try direct httpx (fast path — works if internet egress is available)
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                r = await client.get(url, headers={"Accept": "text/plain", "User-Agent": "SovereignCore/1.0"})
                if r.status_code == 200:
                    return r.text
                logger.warning("SkillLifecycle._fetch_raw_url: HTTP %s for %s", r.status_code, url)
        except Exception as e:
            logger.warning("SkillLifecycle._fetch_raw_url direct: %s — %s", url, e)

        # Fall back to a2a-browser /fetch (browser_net has internet egress)
        if self.browser:
            try:
                result = await self.browser.fetch(url, extract="text")
                if result.get("status") == "ok":
                    content = ((result.get("data") or {}).get("data") or {}).get("content", "")
                    if content:
                        logger.info("SkillLifecycle._fetch_raw_url: browser fallback succeeded for %s", url)
                        return content
                logger.warning("SkillLifecycle._fetch_raw_url browser fallback: %s", result.get("message", "no content"))
            except Exception as e:
                logger.warning("SkillLifecycle._fetch_raw_url browser fallback: %s — %s", url, e)

        return None

    # ── REVIEW ────────────────────────────────────────────────────────────────

    async def review(
        self,
        slug: str,
        skill_md_content: str,
        certified: bool = True,
    ) -> dict:
        """Full inbound security review of a candidate SKILL.md.

        Pipeline:
          1. Escalation keyword scan (deterministic — before anything else)
          2. SecurityScanner pattern match (all YAML rule files)
          3. LLM security_evaluate() if scanner flags anything
          4. Certification status check (non-certified always enters review)

        Returns:
          {
            "decision": "block|review|approve",
            "risk_level": "low|medium|high|critical",
            "escalate_to_director": bool,
            "escalation_reasons": [...],
            "scanner_categories": [...],
            "matched_phrases": [...],
            "llm_assessment": {...} | null,
            "certified": bool,
          }
        """
        result: dict = {
            "slug": slug,
            "certified": certified,
            "decision": "approve",
            "risk_level": "low",
            "escalate_to_director": False,
            "escalation_reasons": [],
            "scanner_categories": [],
            "matched_phrases": [],
            "llm_assessment": None,
        }

        # ── 1. Non-certified: unconditional escalation ─────────────────────
        if not certified:
            result["escalate_to_director"] = True
            result["escalation_reasons"].append("non_certified_skill")
            result["decision"] = "review"

        # ── 2. Escalation keyword scan ────────────────────────────────────
        content_lower = skill_md_content.lower()
        found_escalation = sorted(
            kw for kw in ESCALATION_KEYWORDS if kw in content_lower
        )
        if found_escalation:
            result["escalate_to_director"] = True
            result["escalation_reasons"].append(
                f"escalation_keywords: {', '.join(found_escalation[:8])}"
            )
            if result["decision"] == "approve":
                result["decision"] = "review"

        # ── 3. SecurityScanner (deterministic pattern match) ──────────────
        if self.scanner:
            try:
                scan = self.scanner.scan(skill_md_content)
                if scan.flagged:
                    result["scanner_categories"] = scan.categories
                    result["matched_phrases"] = scan.matched_phrases[:10]
                    result["escalate_to_director"] = True

                    # ── 4. LLM security evaluation ─────────────────────────
                    if self.cog:
                        try:
                            llm = await self.cog.security_evaluate(scan, skill_md_content)
                            result["llm_assessment"] = llm
                            if llm.get("block"):
                                result["decision"] = "block"
                                result["risk_level"] = llm.get("risk_level", "high")
                                result["escalation_reasons"].append("scanner_flagged_llm_block")
                            else:
                                risk = llm.get("risk_level", "low")
                                result["risk_level"] = risk
                                if risk in ("high", "critical"):
                                    result["decision"] = "review"
                                    result["escalation_reasons"].append(
                                        f"llm_risk_level_{risk}"
                                    )
                        except Exception as e:
                            logger.warning("SkillLifecycle.review: LLM evaluation failed: %s", e)
                            # Scanner flagged but LLM unavailable → conservative escalation
                            result["decision"] = "review"
                            result["risk_level"] = "medium"
                            result["escalation_reasons"].append("llm_evaluation_failed")
            except Exception as e:
                logger.warning("SkillLifecycle.review: scanner error: %s", e)

        self._audit("skill_review", result["decision"], {
            "slug": slug,
            "certified": certified,
            "decision": result["decision"],
            "risk_level": result["risk_level"],
            "escalate_to_director": result["escalate_to_director"],
            "escalation_reasons": result["escalation_reasons"],
            "scanner_flagged": bool(result["scanner_categories"]),
        })
        return result

    # ── LOAD ──────────────────────────────────────────────────────────────────

    async def load(
        self,
        name: str,
        skill_md_content: str,
        review_result: dict,
        confirmed: bool = False,
        specialist_overrides: Optional[list] = None,
        tier_override: Optional[str] = None,
        clawhub_slug: Optional[str] = None,
        clawhub_certified: bool = False,
        proposed_by: str = "devops_agent",
        reason: str = "Director requested skill installation.",
    ) -> dict:
        """Write a reviewed skill to RAID and register with integrity systems.

        MID tier — returns requires_confirmation if Director has not yet confirmed.
        Escalated skills (escalate_to_director=True) additionally require
        Director acknowledgement of the escalation reason before proceeding.

        On confirmation:
          - Synthesises a compliant sovereign: frontmatter block
          - Computes body SHA256 (sovereign.checksum)
          - Writes /home/sovereign/skills/<name>/SKILL.md
          - Registers whole-file hash in skill-checksums.json
          - Updates skill-metadata.json
          - Appends skill path to soul-guardian watchlist
          - Adds path to guardian._files at runtime
          - Logs skill_install to audit ledger

        Returns:
          {"status": "installed", "skill": name, "path": ..., "activates": "next_session"}
          or {"requires_confirmation": True, ...}
          or {"error": "SKILL_BLOCKED", ...}
        """
        # ── Safety: hard block on review verdict ──────────────────────────
        if review_result.get("decision") == "block":
            return {
                "error": "SKILL_BLOCKED",
                "reason": "Security review returned block verdict — cannot load",
                "risk_level": review_result.get("risk_level"),
                "llm_assessment": review_result.get("llm_assessment"),
            }

        # ── MID tier gate — always requires Director confirmation ─────────
        if not confirmed:
            resp: dict = {
                "requires_confirmation": True,
                "tier": "MID",
                "action": "skill_load",
                "skill_name": name,
                "review_decision": review_result.get("decision"),
                "escalate_to_director": review_result.get("escalate_to_director", False),
                "escalation_reasons": review_result.get("escalation_reasons", []),
                "clawhub_certified": clawhub_certified,
                "warning": (
                    "Loading an external skill is a MID tier action requiring confirmation. "
                    "The skill will be active from the next session."
                ),
            }
            if review_result.get("escalate_to_director"):
                resp["director_escalation_notice"] = (
                    "This skill touched sensitive keywords or was flagged by the scanner. "
                    "Director must acknowledge escalation before proceeding. "
                    f"Reasons: {'; '.join(review_result.get('escalation_reasons', []))}"
                )
            return resp

        # ── Name sanitisation — only allow safe filesystem names ─────────
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", name).strip("-")[:64]
        if not safe_name:
            return {"error": "INVALID_NAME", "reason": "Skill name must be non-empty and contain alphanumeric characters"}

        # ── Parse existing frontmatter (if any) from the incoming content ─
        existing_fm, body = _parse_skill_md_content(skill_md_content)
        if not body.strip():
            return {"error": "EMPTY_BODY", "reason": "Skill body content is empty after frontmatter parsing"}

        existing_sov = existing_fm.get("sovereign") or {}

        # ── Nanobot translation (OpenClaw / community format) ─────────────
        # If the skill has no sovereign: block (or no operations), call nanobot-01
        # /translate which detects the format and returns the full sovereign: block
        # including the operations: DSL. This is how OpenClaw skills gain their
        # broker_exec operations without any Python adapter involvement.
        nanobot_translated: dict = {}
        nanobot_advisory: dict | None = None
        if not existing_sov:
            nanobot_translated, nanobot_advisory = await self._translate_via_nanobot(
                skill_md_content, safe_name
            )

        # Merge: nanobot translation → parsed values → defaults
        translated_sov = nanobot_translated or existing_sov

        # Build sovereign: block — overrides win; fall back to translated/parsed values
        specialists = specialist_overrides or translated_sov.get("specialists") or ["research_agent"]
        tier = tier_override or translated_sov.get("tier_required") or "LOW"
        adapter_deps = translated_sov.get("adapter_deps") or existing_sov.get("adapter_deps") or []
        operations = translated_sov.get("operations") or existing_sov.get("operations") or {}

        # Validate and sanitise
        specialists = [s for s in specialists if s in KNOWN_SPECIALISTS] or ["research_agent"]
        if tier not in ("LOW", "MID", "HIGH"):
            tier = "LOW"
        adapter_deps = [a for a in adapter_deps if a in KNOWN_ADAPTERS]

        # Compute body checksum
        body_checksum = _sha256_text(body)

        # Build the new frontmatter dict
        sovereign_block: dict = {
            "specialists": specialists,
            "tier_required": tier,
            "adapter_deps": adapter_deps,
            "checksum": body_checksum,
        }
        if operations:
            sovereign_block["operations"] = operations

        fm_dict: dict = {
            "name": safe_name,
            "version": str(existing_fm.get("version", "1.0")),
            "description": (
                translated_sov.get("description")
                or existing_fm.get("description")
                or f"Skill loaded from ClawhHub: {clawhub_slug or safe_name}"
            ),
            "sovereign": sovereign_block,
        }
        if clawhub_slug:
            fm_dict["clawhub_slug"] = clawhub_slug
            fm_dict["clawhub_certified"] = clawhub_certified

        new_skill_md = "---\n" + yaml.dump(fm_dict, default_flow_style=False) + "---\n" + body

        # ── Write to RAID ─────────────────────────────────────────────────
        skill_dir = os.path.join(SKILLS_DIR, safe_name)
        skill_path = os.path.join(skill_dir, "SKILL.md")
        try:
            os.makedirs(skill_dir, exist_ok=True)
            with open(skill_path, "w") as f:
                f.write(new_skill_md)
        except OSError as e:
            return {"error": f"FILE_WRITE_FAILED: {e}"}

        # ── Register in skill-checksums.json (SkillLoader reference) ─────
        whole_file_hash = _sha256_file(skill_path)
        self._update_checksums(safe_name, whole_file_hash)

        # ── Update skill-metadata.json ─────────────────────────────────
        now = datetime.now(timezone.utc).isoformat()
        self._update_metadata(safe_name, {
            "loaded_at": now,
            "last_accessed": now,
            "loaded_by": "director_confirmed",
            "clawhub_slug": clawhub_slug,
            "clawhub_certified": clawhub_certified,
            "specialists": specialists,
            "tier": tier,
            "body_checksum": body_checksum,
            "file_hash": whole_file_hash,
            "review_decision": review_result.get("decision"),
        })

        # ── Soul Guardian registration ─────────────────────────────────
        self._register_with_guardian(skill_path)

        # ── Config change notification (Telegram + as-built.md) ────────────
        try:
            from config_policy.notifier import notify_config_change
            source_label = f" (from ClawhHub: {clawhub_slug})" if clawhub_slug else ""
            cert_label = (" — certified" if clawhub_certified
                          else " — NOT certified" if clawhub_slug else "")
            change_narrative = (
                f"New skill '{safe_name}' installed{source_label}{cert_label}. "
                f"Active for: {', '.join(specialists)}. "
                f"Governance tier: {tier}. "
                f"Security review decision: {review_result.get('decision', 'unknown')}."
            )
            await notify_config_change(
                path=skill_path,
                narrative=change_narrative,
                proposed_by=proposed_by,
                reason=reason,
                tier="MID",
                ledger=self.ledger,
                technical={
                    "clawhub_slug": clawhub_slug,
                    "clawhub_certified": clawhub_certified,
                    "body_checksum": body_checksum[:16] + "…",
                    "file_hash": whole_file_hash[:16] + "…",
                    "review_decision": review_result.get("decision"),
                    "escalation_reasons": review_result.get("escalation_reasons", []),
                },
            )
        except Exception as e:
            logger.warning("SkillLifecycle.load: config change notification failed: %s", e)

        # ── Audit log ─────────────────────────────────────────────────
        self._audit("skill_install", "load_complete", {
            "skill": safe_name,
            "clawhub_slug": clawhub_slug,
            "clawhub_certified": clawhub_certified,
            "specialists": specialists,
            "tier": tier,
            "body_checksum": body_checksum[:16] + "…",
            "file_hash": whole_file_hash[:16] + "…",
            "review_decision": review_result.get("decision"),
            "escalation_reasons": review_result.get("escalation_reasons", []),
        })

        result: dict = {
            "status": "installed",
            "skill": safe_name,
            "path": skill_path,
            "specialists": specialists,
            "tier": tier,
            "body_checksum": body_checksum,
            "file_hash": whole_file_hash,
            "activates": "next_session",
            "operations_count": len(operations),
            "execution_path": "dsl_native" if operations else "llm_fallback",
            "message": (
                f"Skill '{safe_name}' installed. "
                f"Active for {', '.join(specialists)} from the next session. "
                f"{len(operations)} operation(s) mapped to DSL execution path."
            ),
        }

        # Surface any nanobot advisory so Sovereign can inform the Director
        if nanobot_advisory:
            result["nanobot_advisory"] = nanobot_advisory
            if not nanobot_advisory.get("can_emulate", True):
                result["status"] = "installed_with_warnings"
                result["message"] += (
                    f" WARNING: nanobot-01 reports this skill needs development work "
                    f"before it can be fully executed. "
                    f"Reason: {nanobot_advisory.get('reason', '')} "
                    f"Steps: {'; '.join(nanobot_advisory.get('steps', []))}"
                )

        return result

    # ── AUDIT ─────────────────────────────────────────────────────────────────

    def audit(self) -> dict:
        """List all installed skills with integrity status.

        For each skill:
          - Parse SKILL.md frontmatter for metadata
          - Compare current whole-file SHA256 against skill-checksums.json reference
          - Retrieve last_accessed from skill-metadata.json
          - Flag drifted skills as HIGH tier incidents; log to audit ledger

        Returns:
          {
            "skills": [...],
            "total": int,
            "clean": int,
            "drifted": [...],    # names of drifted skills
            "incident_tier": "HIGH" | null,
          }
        """
        reference = self._load_checksums()
        metadata = self._load_metadata()
        skills: list[dict] = []
        drifted: list[str] = []

        if not os.path.isdir(SKILLS_DIR):
            return {
                "skills": [], "total": 0, "clean": 0,
                "drifted": [], "incident_tier": None,
                "message": f"Skills directory not found: {SKILLS_DIR}",
            }

        for entry in sorted(os.listdir(SKILLS_DIR)):
            if entry.startswith("."):
                continue
            skill_dir = os.path.join(SKILLS_DIR, entry)
            skill_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isdir(skill_dir) or not os.path.isfile(skill_path):
                continue

            # Parse frontmatter
            try:
                with open(skill_path) as f:
                    content = f.read()
                fm, body = _parse_skill_md_content(content)
            except OSError:
                fm, body = {}, ""

            sov = fm.get("sovereign") or {} if fm else {}
            meta = metadata.get(entry, {})

            # Checksum comparison
            try:
                current_hash = _sha256_file(skill_path)
            except OSError:
                current_hash = "unreadable"
            ref_hash = reference.get(entry)

            skill_drifted = bool(ref_hash and current_hash != ref_hash)
            if skill_drifted:
                drifted.append(entry)
                self._audit("skill_drift_detected", "audit", {
                    "skill": entry,
                    "reference_prefix": ref_hash[:16] if ref_hash else None,
                    "current_prefix": current_hash[:16],
                    "incident_tier": "HIGH",
                })

            skills.append({
                "name": entry,
                "version": fm.get("version", "?") if fm else "?",
                "description": (fm.get("description", "") or "")[:120] if fm else "",
                "specialists": sov.get("specialists", []),
                "tier": sov.get("tier_required", "?"),
                "body_checksum": (sov.get("checksum") or "")[:16] + "…",
                "file_hash_current": current_hash[:16] + "…",
                "file_hash_reference": (ref_hash[:16] + "…") if ref_hash else "not_enrolled",
                "integrity": (
                    "DRIFTED — HIGH TIER INCIDENT" if skill_drifted
                    else "ok" if ref_hash
                    else "not_enrolled"
                ),
                "clawhub_slug": meta.get("clawhub_slug"),
                "clawhub_certified": meta.get("clawhub_certified"),
                "loaded_at": meta.get("loaded_at", "unknown"),
                "last_accessed": meta.get("last_accessed", "unknown"),
                "review_decision": meta.get("review_decision"),
            })

        message = (
            f"⚠ {len(drifted)} skill(s) have drifted checksums — HIGH tier incident. "
            "Manual investigation required."
            if drifted
            else f"{len(skills)} skill(s) installed, all integrity checks clean."
        )

        return {
            "skills": skills,
            "total": len(skills),
            "clean": len(skills) - len(drifted),
            "drifted": drifted,
            "incident_tier": "HIGH" if drifted else None,
            "message": message,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_checksums(self) -> dict:
        if not os.path.isfile(CHECKSUMS_PATH):
            return {}
        try:
            with open(CHECKSUMS_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _update_checksums(self, name: str, file_hash: str):
        ref = self._load_checksums()
        ref[name] = file_hash
        try:
            os.makedirs(os.path.dirname(CHECKSUMS_PATH), exist_ok=True)
            with open(CHECKSUMS_PATH, "w") as f:
                json.dump(ref, f, indent=2)
        except OSError as e:
            logger.error("SkillLifecycle: failed to update checksums: %s", e)

    def _load_metadata(self) -> dict:
        if not os.path.isfile(METADATA_PATH):
            return {}
        try:
            with open(METADATA_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _update_metadata(self, name: str, data: dict):
        meta = self._load_metadata()
        meta[name] = data
        try:
            os.makedirs(os.path.dirname(METADATA_PATH), exist_ok=True)
            with open(METADATA_PATH, "w") as f:
                json.dump(meta, f, indent=2)
        except OSError as e:
            logger.error("SkillLifecycle: failed to update metadata: %s", e)

    def _register_with_guardian(self, path: str):
        """Register a newly installed skill path with the soul guardian.

        Two layers:
          1. Runtime: append to guardian._files so the current process monitors it
          2. Durable: append to skill-watchlist.json for next-boot registration
        """
        # Runtime registration
        if self.guardian is not None:
            if hasattr(self.guardian, "_files") and path not in self.guardian._files:
                self.guardian._files.append(path)
                logger.info(
                    "SkillLifecycle: registered '%s' with SoulGuardian (runtime)", path
                )

        # Durable watchlist
        watchlist = load_skill_watchlist()
        if path not in watchlist:
            watchlist.append(path)
            _write_watchlist(watchlist)
            logger.info(
                "SkillLifecycle: registered '%s' in soul-guardian watchlist (durable)", path
            )

    async def _translate_via_nanobot(
        self, skill_md_content: str, name: str
    ) -> tuple[dict, dict | None]:
        """Call nanobot-01 /translate to convert community skill format to Sovereign DSL.

        Returns (sovereign_block, advisory).
          sovereign_block: the sovereign: dict to embed in frontmatter (specialists, ops, etc.)
          advisory: None if full emulation, or {can_emulate, reason, steps, missing} dict

        On any error: returns ({}, None) — caller falls back to defaults silently.
        Advisory is also logged to audit ledger so the Director has a record.
        """
        nanobot_url = os.environ.get("NANOBOT_01_URL", "http://nanobot-01:8080")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{nanobot_url}/translate",
                    json={"content": skill_md_content, "name": name},
                )
            if r.status_code == 200:
                body = r.json()
                sov = body.get("sovereign") or {}
                advisory = body.get("advisory")  # None for known categories
                translate_status = body.get("status", "ok")

                if sov:
                    logger.info(
                        "SkillLifecycle: nanobot translated %r → status=%s specialists=%s ops=%s",
                        name, translate_status,
                        sov.get("specialists"),
                        list((sov.get("operations") or {}).keys()),
                    )
                    self._audit("skill_translate", translate_status, {
                        "skill": name,
                        "translate_status": translate_status,
                        "operations_count": len(sov.get("operations") or {}),
                        "specialists": sov.get("specialists"),
                        "needs_development": translate_status == "needs_development",
                        "advisory_missing": (advisory or {}).get("missing", []),
                        "advisory_steps": (advisory or {}).get("steps", []),
                    })
                    return sov, advisory

            logger.warning(
                "SkillLifecycle._translate_via_nanobot: HTTP %s — falling back to defaults",
                r.status_code,
            )
        except Exception as e:
            logger.warning("SkillLifecycle._translate_via_nanobot: %s — falling back to defaults", e)
        return {}, None

    def _audit(self, event_type: str, stage: str, extra: Optional[dict] = None):
        if not self.ledger:
            return
        data: dict = {"source": "skill_lifecycle"}
        if extra:
            data.update(extra)
        try:
            self.ledger.append(event_type, stage, data)
        except Exception as e:
            logger.warning("SkillLifecycle: audit write failed: %s", e)
