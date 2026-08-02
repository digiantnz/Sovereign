"""WorldMonitor memory writes — shared by news_harness (risk-scores) and
portfolio_analysis_harness (macro-signals). Single canonical location for
both consumers per SDO #2/#3, rather than duplicating the write shape in
each harness.

EPISODIC: one entry per pull, timestamped, templated content (never a bare
proposition — risk scores and macro indicators are volatile time-series;
asserted as SEMANTIC facts they'd become confident staleness six months on).
Degradation/staleness fields are first-class metadata, not prose.

META: latest-snapshot upsert per domain, same pattern as
cognition/rextrics.py's write_last_report_cache() — deterministic UUID5 point
ID + zero vector via archive_client.upsert(), overwrite-only, no
read-modify-write.
"""

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_NAMESPACE = uuid.UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d")
_ZERO_VEC = [0.0] * 768
_META = "meta"


def _summarise(domain: str, result: dict) -> str:
    if domain == "risk-scores":
        n = len(result.get("cii_scores") or [])
        state = ("suppressed (stale-by-age)" if result.get("suppressed")
                 else "non-assertive/degraded" if not result.get("assertable")
                 else "assertable")
        return f"{n} region risk score(s), {state}"
    if domain == "macro-signals":
        n = len(result.get("signals") or [])
        state = ("suppressed (stale-by-age)" if result.get("suppressed")
                 else "non-assertive/degraded" if not result.get("assertable")
                 else "assertable")
        consensus = result.get("consensus_to_interrogate") or {}
        return (f"{n} macro signal(s), {state}; "
                f"third-party consensus verdict={consensus.get('verdict')!r} "
                f"({consensus.get('bullish_count')}/{consensus.get('total_count')} bullish, interrogate not adopt)")
    return "unrecognised domain"


def _common_fields(domain: str, result: dict) -> dict:
    fields = {
        "domain": domain,
        "degraded": bool(result.get("degraded", False)),
        "stale": bool(result.get("stale", False)),
        "unavailable": bool(result.get("unavailable", False)),
        "computed_at_ms": result.get("computed_at_ms"),
        "observation_age_hours": result.get("observation_age_hours"),
        "age_known": bool(result.get("age_known", False)),
        "suppressed": bool(result.get("suppressed", False)),
        "assertable": bool(result.get("assertable", False)),
        "gate_reason": result.get("gate_reason"),
    }
    if domain == "risk-scores":
        fields["advisory_provenance"] = [
            row.get("advisoryProvenance") for row in (result.get("cii_scores") or [])
            if isinstance(row, dict)
        ]
    return fields


async def write_episodic(qdrant, domain: str, result: dict) -> None:
    try:
        now = datetime.now(timezone.utc)
        content = f"as at {now.date().isoformat()}, WorldMonitor reported {_summarise(domain, result)}"
        metadata = {
            "type": "episodic",
            "event_type": "worldmonitor_pull",
            "ts": now.isoformat(),
            **_common_fields(domain, result),
        }
        await qdrant.store(collection="episodic", content=content, metadata=metadata)
    except Exception as exc:
        logger.warning("worldmonitor_memory: episodic write failed for domain=%r: %s", domain, exc)


async def write_meta_snapshot(qdrant, domain: str, result: dict) -> None:
    try:
        from qdrant_client.models import PointStruct
        key = f"meta:worldmonitor:{domain}"
        point_id = str(uuid.uuid5(_NAMESPACE, key))
        now = datetime.now(timezone.utc).isoformat()
        await qdrant.archive_client.upsert(
            collection_name=_META,
            points=[PointStruct(
                id=point_id,
                vector=_ZERO_VEC,
                payload={"_key": key, "last_pull_ts": now, **_common_fields(domain, result)},
            )],
        )
    except Exception as exc:
        logger.warning("worldmonitor_memory: meta snapshot write failed for domain=%r: %s", domain, exc)


async def record_pull(qdrant, domain: str, result: dict) -> None:
    """Convenience wrapper — both writes for one pull. Fire via
    asyncio.create_task() from the caller; never blocks the return path."""
    await write_episodic(qdrant, domain, result)
    await write_meta_snapshot(qdrant, domain, result)
