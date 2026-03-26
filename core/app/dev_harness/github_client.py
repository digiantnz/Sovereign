"""
Dev-Harness Phase 1 — GitHub client.

Stateless adapter. Fetches annotations from the latest completed Actions run
on the digiantnz/Sovereign repository and maps them to Finding objects.

Design constraints (ref: dev-harness-assessment.md §9):
  - Stateless: no working memory, no session state, no persistent data.
  - Not a specialist agent: no LLM calls, no Qdrant writes, no governance checks.
  - Follows lifecycle.py precedent for direct httpx usage from sovereign-core.
  - On ANY failure (network, auth, schema): log, return [], never raise.
    Phase 1 continues with local results only.

GitHub API path:
  GET /repos/{owner}/{repo}/actions/runs?per_page=1  → latest run_id
  GET /repos/{owner}/{repo}/actions/runs/{id}/jobs   → job list (check_run ids)
  GET /repos/{owner}/{repo}/check-runs/{id}/annotations → annotation list
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx as _httpx

from dev_harness.analyser import Finding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO             = "digiantnz/Sovereign"
_GITHUB_API       = "https://api.github.com"
_TIMEOUT_S        = 10      # per-request timeout; GitHub is fast or it's down
_MAX_JOBS         = 50      # cap to avoid pagination on large CI runs
_MAX_ANNOTATIONS  = 100     # per job

_ANNOTATION_SEVERITY: dict[str, str] = {
    "notice":  "low",
    "warning": "medium",
    "failure": "high",
}

_GITHUB_HEADERS = {
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def get_latest_run_annotations(token: str) -> list[Finding]:
    """
    Return Finding objects from the most recent completed Actions run.

    Returns [] if:
    - token is empty or None
    - GitHub API is unreachable or returns an error
    - The repository has no completed runs
    - Any unexpected exception occurs

    The caller (harness.py) is responsible for deciding whether an empty
    list represents a transient failure or a genuine absence of annotations.
    """
    try:
        import httpx  # noqa: PLC0415 — optional dependency check
    except ImportError:
        logger.warning("github_client: httpx not available — skipping GitHub path")
        return []

    if not token:
        logger.info("github_client: no GitHub token configured — skipping GitHub path")
        return []

    auth_headers = {**_GITHUB_HEADERS, "Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            run_id = await _get_latest_run_id(client, auth_headers)
            if run_id is None:
                return []
            return await _collect_annotations(client, auth_headers, run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("github_client: unexpected error fetching annotations: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Internal helpers — all return [] / None on error, never raise
# ---------------------------------------------------------------------------

async def _get_latest_run_id(client: "_httpx.AsyncClient", headers: dict) -> int | None:
    """Return the run_id of the most recently triggered workflow run, or None."""
    url = f"{_GITHUB_API}/repos/{_REPO}/actions/runs"
    try:
        resp = await client.get(url, headers=headers, params={"per_page": 1})
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
    except Exception as exc:
        logger.warning("github_client: failed to list runs: %s", exc)
        return None

    if not runs:
        logger.info("github_client: no Actions runs found on %s", _REPO)
        return None

    run_id = runs[0].get("id")
    status = runs[0].get("status", "unknown")
    logger.info("github_client: latest run_id=%s status=%s", run_id, status)
    return run_id


async def _collect_annotations(
    client: "_httpx.AsyncClient",
    headers: dict,
    run_id: int,
) -> list[Finding]:
    """Collect all annotations across all jobs in the given run."""
    jobs = await _get_jobs(client, headers, run_id)
    if not jobs:
        return []

    findings: list[Finding] = []
    for job in jobs[:_MAX_JOBS]:
        job_id   = job.get("id")
        job_name = job.get("name", str(job_id))
        if not job_id:
            continue
        annotations = await _get_job_annotations(client, headers, job_id, job_name)
        findings.extend(annotations)

    logger.info(
        "github_client: run %s — %d jobs, %d annotation findings",
        run_id, len(jobs), len(findings),
    )
    return findings


async def _get_jobs(
    client: "_httpx.AsyncClient",
    headers: dict,
    run_id: int,
) -> list[dict]:
    url = f"{_GITHUB_API}/repos/{_REPO}/actions/runs/{run_id}/jobs"
    try:
        resp = await client.get(url, headers=headers, params={"per_page": _MAX_JOBS})
        resp.raise_for_status()
        return resp.json().get("jobs", [])
    except Exception as exc:
        logger.warning("github_client: failed to fetch jobs for run %s: %s", run_id, exc)
        return []


async def _get_job_annotations(
    client: "_httpx.AsyncClient",
    headers: dict,
    job_id: int,
    job_name: str,
) -> list[Finding]:
    """
    Fetch annotations for a single check-run (job).
    job.id in the Actions API is the check_run_id for the annotations endpoint.
    """
    url = f"{_GITHUB_API}/repos/{_REPO}/check-runs/{job_id}/annotations"
    try:
        resp = await client.get(url, headers=headers, params={"per_page": _MAX_ANNOTATIONS})
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.warning(
            "github_client: failed to fetch annotations for job %s (%s): %s",
            job_id, job_name, exc,
        )
        return []

    if not isinstance(raw, list):
        logger.warning(
            "github_client: unexpected annotation response type for job %s: %s",
            job_id, type(raw).__name__,
        )
        return []

    findings: list[Finding] = []
    for ann in raw:
        f = _annotation_to_finding(ann, job_name)
        if f is not None:
            findings.append(f)
    return findings


def _annotation_to_finding(ann: dict, job_name: str) -> Finding | None:
    """
    Map one GitHub annotation dict to a Finding.
    Returns None if the annotation lacks a path (can't locate the issue).
    """
    path = ann.get("path", "").strip()
    if not path:
        return None

    level   = ann.get("annotation_level", "warning").lower()
    severity = _ANNOTATION_SEVERITY.get(level, "medium")

    # Prefer start_line; fall back to end_line; default 0
    line = ann.get("start_line") or ann.get("end_line") or 0

    title   = (ann.get("title") or "").strip()
    message = (ann.get("message") or "").strip()
    full_msg = f"[{job_name}] {title}: {message}".strip(": ") if title else f"[{job_name}] {message}"

    # raw_details can be verbose — truncate to 80 chars for rule_id
    raw_details = (ann.get("raw_details") or "")[:80].strip()

    return Finding(
        source   = "github",
        type     = "lint",          # GitHub annotations are linter/test outputs
        file     = path,
        line     = int(line),
        message  = full_msg,
        severity = severity,
        rule_id  = raw_details or level,
    )
