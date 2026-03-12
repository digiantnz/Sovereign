"""Sovereign Skill Loader

Loads SKILL.md files from /home/sovereign/skills/ for a given specialist, validates
their integrity, checks adapter dependency availability, and injects their content
into specialist system prompts.

Security model:
  - sovereign.checksum in frontmatter = SHA256 of the skill body (everything after
    the closing --- of the YAML frontmatter block).  Verified on every load.
  - /home/sovereign/security/skill-checksums.json = whole-file SHA256 reference hashes
    maintained by this loader (written to the security volume, which is rw-mounted).
    First boot = bootstrap: reference file is created and all valid skills are enrolled.
    Subsequent boots: any whole-file hash drift triggers a refusal + audit event.
  - Skills whose adapter_deps are unavailable are skipped (logged, not errors).
  - All load events (success, checksum mismatch, drift, missing deps) are logged to
    the AuditLedger.
"""

import hashlib
import json
import logging
import os
import re
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

SKILLS_DIR = "/home/sovereign/skills"
# Reference checksums live in the security volume (rw-mounted); separate from skills dir (ro-mounted)
CHECKSUMS_PATH = "/home/sovereign/security/skill-checksums.json"

# Adapters always present in the running Sovereign stack (no env check required)
_ALWAYS_AVAILABLE = {"broker", "ollama", "qdrant"}


def _available_adapters() -> set[str]:
    """Detect which adapters are live based on environment variables and filesystem."""
    avail = set(_ALWAYS_AVAILABLE)
    # a2a-browser — env var is always set in compose; check it's non-empty
    if os.environ.get("A2A_BROWSER_URL") or os.environ.get("A2A_SHARED_SECRET"):
        avail.add("browser")
    if os.environ.get("WEBDAV_BASE"):
        avail.add("webdav")
    if os.environ.get("CALDAV_BASE"):
        avail.add("caldav")
    if os.environ.get("PERSONAL_IMAP_HOST") or os.environ.get("BUSINESS_IMAP_HOST"):
        avail.add("imap")
    if os.environ.get("PERSONAL_SMTP_HOST") or os.environ.get("BUSINESS_SMTP_HOST"):
        avail.add("smtp")
    if os.environ.get("GITHUB_PAT"):
        avail.add("github")
    if os.path.exists("/home/sovereign/keys/sovereign.key"):
        avail.add("signing")
    if os.environ.get("WHISPER_URL"):
        avail.add("whisper")
    return avail


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_skill_md(path: str) -> tuple[dict, str]:
    """Parse a SKILL.md into (frontmatter_dict, body_text).

    The file must begin with a YAML frontmatter block delimited by --- lines.
    Returns ({}, "") on any parse error; caller treats this as a load failure.
    """
    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        logger.warning("SkillLoader: cannot read %s: %s", path, e)
        return {}, ""

    match = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
    if not match:
        logger.warning("SkillLoader: malformed SKILL.md (no frontmatter delimiter): %s", path)
        return {}, ""

    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        logger.warning("SkillLoader: YAML parse error in %s: %s", path, e)
        return {}, ""

    return fm, match.group(2)


class SkillLoader:
    """Loads, validates, and injects sovereign skills for a given specialist.

    Usage:
        loader = SkillLoader("research_agent", ledger=ledger)
        augmented_persona = loader.inject_into_persona(base_persona)

    Skills that fail any validation gate are refused without crashing the caller.
    A specialist with zero loaded skills receives their base persona unchanged.
    """

    def __init__(self, specialist: str, ledger=None):
        self.specialist = specialist
        self.ledger = ledger
        self._available = _available_adapters()
        # Each entry: {"name": str, "body": str, "tier": str, "version": str, "description": str}
        self.skills: list[dict] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def inject_into_persona(self, persona: str) -> str:
        """Append active skill blocks to a specialist system prompt.

        Skills are inserted under a clearly delimited ACTIVE SKILLS section so
        the LLM can distinguish base persona from injected skill protocols.
        """
        if not self.skills:
            return persona
        blocks = [
            "\n\n---\n## ACTIVE SKILLS\n",
            "The following skills are loaded and active for this session. "
            "Apply their protocols when the delegation matches their activation criteria.\n",
        ]
        for skill in self.skills:
            blocks.append(
                f"\n### Skill: {skill['name']} "
                f"(tier={skill['tier']}, v{skill['version']})\n"
            )
            blocks.append(skill["body"])
        return persona + "".join(blocks)

    def get_skill_names(self) -> list[str]:
        return [s["name"] for s in self.skills]

    # ── Load pipeline ─────────────────────────────────────────────────────

    def _load(self):
        """Discover and validate all eligible skills for this specialist."""
        if not os.path.isdir(SKILLS_DIR):
            logger.debug("SkillLoader: %s not found — no skills loaded", SKILLS_DIR)
            return

        reference = self._load_reference()
        new_reference = dict(reference)
        bootstrap = not reference  # first boot: no reference file yet

        if bootstrap:
            logger.info("SkillLoader: bootstrap mode — creating skill reference checksums")

        for entry in sorted(os.listdir(SKILLS_DIR)):
            if entry.startswith("."):
                continue
            skill_dir = os.path.join(SKILLS_DIR, entry)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isdir(skill_dir) or not os.path.isfile(skill_md):
                continue
            self._load_one(skill_md, entry, new_reference, bootstrap)

        # Persist reference (created on first boot; updated when new skills are added)
        if new_reference != reference:
            self._write_reference(new_reference)

    def _load_one(self, path: str, name: str, ref: dict, bootstrap: bool):
        """Validate and conditionally load a single SKILL.md. Mutates ref."""
        fm, body = _parse_skill_md(path)
        if not fm or not body:
            self._audit("skill_load_error", name, "parse_failed")
            return

        sov = fm.get("sovereign") or {}

        # ── 1. Specialist filter ──────────────────────────────────────────
        specialists = sov.get("specialists") or []
        if self.specialist not in specialists:
            return  # silently skip — not targeted at this specialist

        # ── 2. Body checksum validation (declared vs computed) ────────────
        declared = sov.get("checksum", "")
        computed = _sha256_text(body)
        if declared and declared != computed:
            logger.warning(
                "SkillLoader: body checksum MISMATCH for '%s' — refusing load "
                "(declared=%.12s… computed=%.12s…)",
                name, declared, computed,
            )
            self._audit("skill_checksum_mismatch", name, "body_hash_mismatch", {
                "declared_prefix": declared[:16],
                "computed_prefix": computed[:16],
            })
            return

        # ── 3. Whole-file drift detection (against reference) ─────────────
        current_file_hash = _sha256_file(path)
        if not bootstrap and name in ref:
            if ref[name] != current_file_hash:
                logger.warning(
                    "SkillLoader: DRIFT detected for skill '%s' — "
                    "file modified since last verified load "
                    "(ref=%.12s… current=%.12s…)",
                    name, ref[name], current_file_hash,
                )
                self._audit("skill_drift", name, "file_hash_drift", {
                    "reference_prefix": ref[name][:16],
                    "current_prefix": current_file_hash[:16],
                })
                return

        # Enrol / update reference hash
        ref[name] = current_file_hash

        # ── 4. Adapter dependency check ───────────────────────────────────
        deps = sov.get("adapter_deps") or []
        missing = [d for d in deps if d not in self._available]
        if missing:
            logger.info(
                "SkillLoader: skill '%s' skipped — adapter deps unavailable: %s",
                name, missing,
            )
            self._audit("skill_deps_missing", name, "adapter_unavailable", {"missing": missing})
            return

        # ── 5. Tier validation ─────────────────────────────────────────────
        tier = sov.get("tier_required", "LOW")
        if tier not in ("LOW", "MID", "HIGH"):
            logger.warning(
                "SkillLoader: skill '%s' has invalid tier_required '%s' — defaulting LOW",
                name, tier,
            )
            tier = "LOW"

        # ── Load success ──────────────────────────────────────────────────
        skill = {
            "name": name,
            "body": body.strip(),
            "tier": tier,
            "version": str(fm.get("version", "1.0")),
            "description": fm.get("description", ""),
        }
        self.skills.append(skill)
        logger.info(
            "SkillLoader: loaded '%s' v%s for %s (tier=%s)",
            name, skill["version"], self.specialist, tier,
        )
        self._audit("skill_loaded", name, "ok", {
            "version": skill["version"],
            "tier": tier,
            "specialist": self.specialist,
        })

    # ── Reference file I/O ────────────────────────────────────────────────

    def _load_reference(self) -> dict:
        if not os.path.isfile(CHECKSUMS_PATH):
            return {}
        try:
            with open(CHECKSUMS_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.warning("SkillLoader: could not read reference checksums: %s", e)
            return {}

    def _write_reference(self, ref: dict):
        try:
            os.makedirs(os.path.dirname(CHECKSUMS_PATH), exist_ok=True)
            with open(CHECKSUMS_PATH, "w") as f:
                json.dump(ref, f, indent=2)
            logger.info("SkillLoader: reference checksums updated (%d skills)", len(ref))
        except OSError as e:
            logger.error("SkillLoader: failed to write reference checksums: %s", e)

    # ── Audit logging ─────────────────────────────────────────────────────

    def _audit(self, event_type: str, skill_name: str, stage: str, extra: Optional[dict] = None):
        if not self.ledger:
            return
        data: dict = {"skill": skill_name, "specialist": self.specialist}
        if extra:
            data.update(extra)
        try:
            self.ledger.append(event_type, stage, data)
        except Exception as e:
            logger.warning("SkillLoader: audit write failed: %s", e)


def scan_all_skills(ledger=None) -> dict:
    """Startup scan: attempt to load all skills for all known specialists.

    Returns a summary dict for inclusion in the startup log.
    Runs once at lifespan startup — does not inject into any persona.
    """
    specialists = [
        "research_agent", "devops_agent", "business_agent",
        "memory_agent", "security_agent",
    ]
    summary: dict[str, list[str]] = {}
    for spec in specialists:
        loader = SkillLoader(spec, ledger=ledger)
        names = loader.get_skill_names()
        if names:
            summary[spec] = names
    loaded_total = sum(len(v) for v in summary.values())
    logger.info(
        "SkillLoader startup scan: %d skill(s) loaded across %d specialist(s): %s",
        loaded_total, len(summary), summary,
    )
    return summary
