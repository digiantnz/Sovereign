"""Memory synthesis — discovers associative, relational, and structural patterns in memory.

Bespoke module (no OpenClaw equivalent) with deep Qdrant + Ollama dependencies.
Runs nightly at 03:00 NZST (15:00 UTC) via the task scheduler.
Also triggerable manually via intent: memory_synthesise (LOW tier, memory_agent).

Episodic scan logic (run_synthesis — Passes 1–3):
  1. Same intent, different phrasing → associative entry (relationship: same_intent_variant)
  2. Similar intents with different outcomes → relational entry (shared/diverges/insight)
  3. Co-occurring intent chains (same session_id) → associative entry (relationship: co_occurs_with)

Semantic structural pass (synthesise_structural — Pass 4):
  4. Vector-similarity neighbours in SEMANTIC → relational entry with LLM-inferred typed
     relationship. Relationship vocabulary: is_a, part_of, depends_on, owns, same_domain.
     Two modes:
       scoped (key provided) — triggered by QdrantAdapter.store() on every semantic write
       full scan (key=None)  — called by run_synthesis() nightly after Passes 1–3

Dedup: checks for existing _key before writing — never writes duplicate entries.
       Structural entries update on subsequent runs when insight or relationship_type changes.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

logger = logging.getLogger(__name__)

_LLM_MODEL = "llama3.1:8b-instruct-q4_K_M"

# Structural relationship vocabulary — typed edges in Rex's semantic knowledge graph
REL_IS_A        = "is_a"         # A is a type/subclass of B
REL_PART_OF     = "part_of"      # A is a component or sub-part of B
REL_DEPENDS_ON  = "depends_on"   # A requires or uses B to function
REL_OWNS        = "owns"         # A contains or manages B
REL_SAME_DOMAIN = "same_domain"  # A and B operate in the same context/category

REL_CO_OCCURS = "co_occurs_with"    # A and B appear together in sessions
REL_VARIANT   = "same_intent_variant"  # A is an alternate phrasing of intent B

_STRUCTURAL_REL_TYPES = frozenset({
    REL_IS_A, REL_PART_OF, REL_DEPENDS_ON, REL_OWNS, REL_SAME_DOMAIN,
    REL_CO_OCCURS, REL_VARIANT,
})

# Relationship types that also warrant an associative entry (high-confidence structural links)
_STRONG_STRUCTURAL = frozenset({REL_IS_A, REL_PART_OF, REL_DEPENDS_ON, REL_OWNS})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower().strip()).strip("-")[:48]


def _assoc_key(slug_a: str, slug_b: str) -> str:
    """Canonical key — alphabetically smaller slug first to prevent mirror duplicates."""
    a, b = sorted([slug_a, slug_b])
    return f"associative:intent:{a}:{b}"


def _rel_key(slug_a: str, slug_b: str) -> str:
    a, b = sorted([slug_a, slug_b])
    return f"relational:intent:{a}:{b}"


def _structural_rel_key(key_a: str, key_b: str) -> str:
    """Canonical structural relational key from two semantic entry _key values.
    Slugifies and sorts both keys to prevent mirror duplicates.
    """
    a, b = sorted([_slug(key_a), _slug(key_b)])
    return f"relational:structural:{a}:{b}"


async def _key_exists(qdrant, key: str) -> bool:
    """Return True if a Qdrant entry with _key == key already exists in archive."""
    existing = await qdrant.retrieve_by_key(key)
    return existing is not None


async def _infer_relationship(cog, payload_a: dict, payload_b: dict) -> dict | None:
    """Use Ollama to infer the relationship between two semantic entries.

    Shared helper used by Pass 2 (mixed-outcome intent pairs) and Pass 4 (structural).
    Uses cog.call_llm_json() which enforces JSON with one retry.

    Returns a dict {related, relationship_type, shared, diverges, insight} or None on
    failure. relationship_type is validated against _STRUCTURAL_REL_TYPES and defaults
    to same_domain if the model returns an unrecognised value.
    """
    content_a = (payload_a.get("content") or payload_a.get("_key") or "").strip()[:300]
    content_b = (payload_b.get("content") or payload_b.get("_key") or "").strip()[:300]
    if not content_a or not content_b:
        return None

    prompt = (
        "You are a knowledge graph reasoner for a sovereign AI system.\n"
        "Determine if these two knowledge items are meaningfully related.\n\n"
        f"Entry A: {content_a}\n\n"
        f"Entry B: {content_b}\n\n"
        "Respond with ONLY valid JSON.\n"
        "If related: "
        '{"related": true, "relationship_type": "<type>", '
        '"shared": ["<trait>"], "diverges": ["<difference>"], "insight": "<one sentence>"}\n'
        "If not related: "
        '{"related": false}\n\n'
        f"Valid relationship_type values:\n"
        f"  {REL_IS_A}        — A is a type or subclass of B\n"
        f"  {REL_PART_OF}     — A is a component or sub-part of B\n"
        f"  {REL_DEPENDS_ON}  — A requires or uses B to function\n"
        f"  {REL_OWNS}        — A contains or manages B\n"
        f"  {REL_SAME_DOMAIN} — A and B operate in the same context or category"
    )
    try:
        result = await cog.call_llm_json(prompt)
        if not isinstance(result, dict):
            return None
        if result.get("relationship_type") not in _STRUCTURAL_REL_TYPES:
            result["relationship_type"] = REL_SAME_DOMAIN
        return result
    except Exception as e:
        logger.warning("_infer_relationship: LLM call failed: %s", e)
        return None


async def _load_semantic_neighbours(qdrant, entry: dict, top_k: int = 8) -> list[dict]:
    """Vector-search SEMANTIC for entries similar to entry.

    Uses the entry's stored vector directly (fetch via scroll with_vectors=True on _key
    filter) to avoid a redundant embed call. Falls back to content re-embedding if the
    stored vector is unavailable.

    Score threshold 0.5 (higher than default 0.4) to surface only meaningful neighbours.
    Excludes the entry itself from results via _key comparison.
    """
    from execution.adapters.qdrant import SEMANTIC
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    entry_key = entry.get("_key", "")

    # Try to fetch stored vector for this entry
    query_vector: list[float] | None = None
    try:
        pts, _ = await qdrant.archive_client.scroll(
            collection_name=SEMANTIC,
            scroll_filter=Filter(
                must=[FieldCondition(key="_key", match=MatchValue(value=entry_key))]
            ),
            limit=1,
            with_payload=False,
            with_vectors=True,
        )
        if pts and isinstance(pts[0].vector, list):
            query_vector = pts[0].vector
    except Exception:
        pass  # fall through to re-embed

    if query_vector is None:
        # Fallback: re-embed content (slightly more expensive but always works)
        content = (entry.get("content") or entry_key).strip()
        if not content:
            return []
        try:
            query_vector = await qdrant._embed(content)
        except Exception:
            return []

    try:
        response = await qdrant.archive_client.query_points(
            collection_name=SEMANTIC,
            query=query_vector,
            limit=top_k + 1,   # +1 in case self appears in results
            score_threshold=0.5,
            with_payload=True,
        )
        return [
            {"score": r.score, **r.payload}
            for r in response.points
            if r.payload and r.payload.get("_key") != entry_key
        ][:top_k]
    except Exception as e:
        logger.warning("_load_semantic_neighbours: query failed for %r: %s", entry_key, e)
        return []


async def _upsert_raw(qdrant, collection: str, key: str, payload: dict) -> str:
    """Write a raw payload to archive_client bypassing embedding (associative/relational
    entries are looked up by _key filter, never by vector similarity).
    Uses zero-vector — the same pattern as the Universal Item Index."""
    import uuid as _uuid
    from qdrant_client.models import PointStruct

    _now = datetime.now(timezone.utc).isoformat()
    point_id = str(_uuid.uuid5(_uuid.UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d"), key))
    zero_vector = [0.0] * 768

    await qdrant.archive_client.upsert(
        collection_name=collection,
        points=[PointStruct(
            id=point_id,
            vector=zero_vector,
            payload={
                **payload,
                "_key": key,
                "last_updated": _now,
            },
        )],
    )
    return point_id


async def synthesise_structural(key: str = None, qdrant=None, cog=None) -> dict:
    """Pass 4 — structural synthesis over semantic memory.

    Discovers typed relationships between semantic knowledge entries via vector
    similarity search (first use of vectors in synthesis.py) and LLM inference.
    Writes relational entries with LLM-derived insight — never boilerplate.

    Two modes:
        key provided — scoped: process only the named semantic entry.
                       Called by QdrantAdapter.store() via asyncio.create_task()
                       on every semantic write. Non-blocking invariant: store()
                       has already succeeded before this task is scheduled.
        key=None     — full scan: process entire SEMANTIC collection.
                       Called by run_synthesis() as Pass 4 after Passes 1–3.

    Failures are logged at WARNING level and to episodic memory (scan-level failures
    only). Per-entry failures log and continue — never surface to Director.

    Args:
        key:    _key of a semantic entry to process (None for full scan)
        qdrant: QdrantAdapter instance (required)
        cog:    CognitionEngine instance (required for LLM inference;
                None → skips all inference, no relational entries written)
    """
    from execution.adapters.qdrant import SEMANTIC, RELATIONAL, EPISODIC

    stats: dict = {
        "semantic_processed": 0,
        "relational_created": 0,
        "relational_updated": 0,
        "associative_created": 0,
        "skipped_unrelated": 0,
        "skipped_no_cog": 0,
        "errors": 0,
    }

    if qdrant is None:
        logger.error("synthesise_structural: qdrant required — aborting")
        return {"status": "error", "error": "qdrant required", **stats}

    # ── Load entries to process ───────────────────────────────────────────────
    entries: list[dict] = []

    if key:
        # Scoped mode — single entry by key
        entry = await qdrant.retrieve_by_key(key)
        if entry is None or entry.get("collection") != SEMANTIC:
            logger.debug("synthesise_structural: key %r not found in SEMANTIC — skipping", key)
            return {"status": "ok", **stats}
        entries.append(entry)
    else:
        # Full scan mode — entire SEMANTIC collection (keyed entries only)
        try:
            offset = None
            while True:
                result, next_offset = await qdrant.archive_client.scroll(
                    collection_name=SEMANTIC,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for r in result:
                    p = dict(r.payload or {})
                    if p.get("_key") and not p.get("_no_key"):
                        entries.append({"point_id": str(r.id), **p})
                if next_offset is None:
                    break
                offset = next_offset
        except Exception as e:
            logger.error("synthesise_structural: SEMANTIC scroll failed: %s", e)
            # Write scan-level failure to episodic so Rex can recall it
            try:
                import uuid as _uuid
                from qdrant_client.models import PointStruct as _PS
                _now = datetime.now(timezone.utc).isoformat()
                await qdrant.archive_client.upsert(
                    collection_name=EPISODIC,
                    points=[_PS(
                        id=str(_uuid.uuid4()),
                        vector=[0.0] * 768,
                        payload={
                            "type": "episodic",
                            "domain": "memory.synthesis",
                            "event": "structural_scan_failed",
                            "error": str(e)[:500],
                            "timestamp": _now,
                            "_no_key": True,
                            "last_updated": _now,
                        },
                    )],
                )
            except Exception:
                pass  # episodic write failure is not surfaced
            return {"status": "error", "error": str(e), **stats}

    stats["semantic_processed"] = len(entries)
    if entries:
        logger.info(
            "synthesise_structural: processing %d semantic entries (scoped=%s)",
            len(entries), bool(key),
        )

    # ── Per-entry: find neighbours → infer relationship → write/update ────────
    for entry in entries:
        entry_key = entry.get("_key", "")
        if not entry_key:
            continue

        # ── cog=None fallback: derive structural links from payload fields ──
        # Writes part_of / depends_on relationships directly from parent_sov_id and
        # any depends_on arrays — no LLM required. These are high-confidence edges
        # that exist regardless of whether the model is available.
        if cog is None:
            parent_key = entry.get("parent_sov_id")
            if parent_key:
                # parent_sov_id may be a sov_id UUID or a semantic _key
                _pk = str(parent_key)
                # Construct a deterministic _key from parent reference
                _fallback_rel = _structural_rel_key(entry_key, _pk)
                if not await _key_exists(qdrant, _fallback_rel):
                    _fp = {
                        "type": "relational",
                        "_key": _fallback_rel,
                        "concept_a": entry_key,
                        "concept_b": _pk,
                        "relationship_type": REL_PART_OF,
                        "shared": ["parent component relationship"],
                        "diverges": [],
                        "insight": f"{entry_key} is a structural part of {_pk}.",
                        "synthesis_source": "structural",
                        "source": "structural_synthesis_payload",
                    }
                    try:
                        await _upsert_raw(qdrant, RELATIONAL, _fallback_rel, _fp)
                        stats["relational_created"] += 1
                    except Exception as e:
                        logger.warning(
                            "synthesise_structural: fallback parent write failed %s: %s",
                            _fallback_rel, e,
                        )
            stats["skipped_no_cog"] += 1
            continue

        try:
            neighbours = await _load_semantic_neighbours(qdrant, entry, top_k=8)
        except Exception as e:
            logger.warning(
                "synthesise_structural: neighbour search failed for %r: %s", entry_key, e
            )
            stats["errors"] += 1
            continue

        for nb in neighbours:
            nb_key = nb.get("_key", "")
            if not nb_key or nb_key == entry_key:
                continue

            rel_key = _structural_rel_key(entry_key, nb_key)

            # LLM-infer typed relationship — not boilerplate
            inference = await _infer_relationship(cog, entry, nb)
            if inference is None:
                stats["errors"] += 1
                continue
            if not inference.get("related", False):
                stats["skipped_unrelated"] += 1
                continue

            rel_type = inference.get("relationship_type", REL_SAME_DOMAIN)

            rel_payload = {
                "type": "relational",
                "_key": rel_key,
                "concept_a": entry_key,
                "concept_b": nb_key,
                "relationship_type": rel_type,
                "shared": inference.get("shared", []),
                "diverges": inference.get("diverges", []),
                "insight": inference.get("insight", ""),
                "similarity_score": round(float(nb.get("score", 0.0)), 4),
                "synthesis_source": "structural",
                "source": "structural_synthesis",
            }

            existing = await _key_exists(qdrant, rel_key)
            if existing:
                # Update — refresh relationship_type, insight, and score on re-synthesis
                try:
                    import uuid as _uuid2
                    point_id = str(_uuid2.uuid5(
                        _uuid2.UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d"), rel_key
                    ))
                    await qdrant.archive_client.set_payload(
                        collection_name=RELATIONAL,
                        payload={
                            "relationship_type": rel_type,
                            "shared":            rel_payload["shared"],
                            "diverges":          rel_payload["diverges"],
                            "insight":           rel_payload["insight"],
                            "similarity_score":  rel_payload["similarity_score"],
                            "synthesis_source":  "structural",
                            "last_updated":      datetime.now(timezone.utc).isoformat(),
                        },
                        points=[point_id],
                    )
                    stats["relational_updated"] += 1
                    logger.debug("synthesise_structural: updated %s", rel_key)
                except Exception as e:
                    logger.warning(
                        "synthesise_structural: update failed %s: %s", rel_key, e
                    )
                    stats["errors"] += 1
            else:
                try:
                    await _upsert_raw(qdrant, RELATIONAL, rel_key, rel_payload)
                    stats["relational_created"] += 1
                    logger.debug(
                        "synthesise_structural: created %s (%s)", rel_key, rel_type,
                    )
                except Exception as e:
                    logger.warning(
                        "synthesise_structural: write failed %s: %s", rel_key, e
                    )
                    stats["errors"] += 1
                    continue

            # ── Strong structural types → also write associative entry (strength=0.9) ──
            # is_a / part_of / depends_on / owns are high-confidence — bidirectional
            # associative link allows traversal in both directions from either node.
            if rel_type in _STRONG_STRUCTURAL:
                assoc_key = f"associative:structural:{_structural_rel_key(entry_key, nb_key).split('relational:structural:', 1)[-1]}"
                if not await _key_exists(qdrant, assoc_key):
                    _slug_a = _slug(entry_key)
                    _slug_b = _slug(nb_key)
                    assoc_payload = {
                        "type": "associative",
                        "_key": assoc_key,
                        "source_key": entry_key,
                        "target_key": nb_key,
                        "relationship": rel_type,
                        "strength": 0.9,
                        "observation_count": 1,
                        "synthesis_source": "structural",
                        "source": "structural_synthesis",
                    }
                    try:
                        await _upsert_raw(qdrant, ASSOCIATIVE, assoc_key, assoc_payload)
                        stats["associative_created"] += 1
                        logger.debug(
                            "synthesise_structural: assoc %s → %s (%s)",
                            entry_key, nb_key, rel_type,
                        )
                    except Exception as e:
                        logger.warning(
                            "synthesise_structural: assoc write failed %s: %s", assoc_key, e,
                        )

    logger.info(
        "synthesise_structural: complete — processed=%d rel_created=%d rel_updated=%d "
        "assoc_created=%d unrelated=%d no_cog=%d errors=%d",
        stats["semantic_processed"], stats["relational_created"], stats["relational_updated"],
        stats["associative_created"], stats["skipped_unrelated"],
        stats["skipped_no_cog"], stats["errors"],
    )
    return {"status": "ok", **stats}


async def run_synthesis(qdrant, cog=None) -> dict:
    """Full synthesis pass over episodic archive.

    Returns:
        {
            "status": "ok",
            "associative_created": N,
            "associative_updated": N,
            "relational_created": N,
            "skipped_existing": N,
            "episodic_scanned": N,
        }
    """
    from execution.adapters.qdrant import EPISODIC, ASSOCIATIVE, RELATIONAL

    stats = {
        "episodic_scanned": 0,
        "associative_created": 0,
        "associative_updated": 0,
        "relational_created": 0,
        "relational_updated": 0,
        "skipped_existing": 0,
    }

    # ── Step 1: Scroll all episodic entries from archive ─────────────────────
    episodic_entries: list[dict] = []
    try:
        offset = None
        while True:
            result, next_offset = await qdrant.archive_client.scroll(
                collection_name=EPISODIC,
                limit=200,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in result:
                p = dict(r.payload or {})
                # Only entries with a valid intent field are useful for synthesis
                if p.get("intent") and isinstance(p["intent"], str):
                    episodic_entries.append(p)
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.error("synthesis: episodic scroll failed: %s", e)
        return {"status": "error", "error": str(e)}

    stats["episodic_scanned"] = len(episodic_entries)
    logger.info("synthesis: scanned %d episodic entries", len(episodic_entries))

    if not episodic_entries:
        return {"status": "ok", **stats}

    # ── Step 2: Group by intent → collect phrasing + outcomes ────────────────
    # intent_stats[intent] = {"success": N, "failure": N, "total": N}
    intent_stats: dict[str, dict] = defaultdict(lambda: {"success": 0, "failure": 0, "total": 0})
    # session_intents[session_id] = [intent, ...]  — for co-occurrence detection
    session_intents: dict[str, list[str]] = defaultdict(list)

    for entry in episodic_entries:
        intent = entry.get("intent", "")
        if not intent:
            continue
        outcome = str(entry.get("outcome", "")).lower()
        is_success = any(w in outcome for w in ("success", "ok", "completed", "done", "created", "sent"))
        is_failure = any(w in outcome for w in ("fail", "error", "block", "reject", "timeout"))
        intent_stats[intent]["total"] += 1
        if is_success:
            intent_stats[intent]["success"] += 1
        elif is_failure:
            intent_stats[intent]["failure"] += 1

        session_id = entry.get("session_id") or entry.get("sov_id", "")[:8]
        if session_id:
            session_intents[session_id].append(intent)

    # ── Step 3: Same-intent-variant detection ─────────────────────────────────
    # Entries with the same intent but different user_input phrasing → associative (same_intent_variant)
    intent_inputs: dict[str, set] = defaultdict(set)
    for entry in episodic_entries:
        intent = entry.get("intent", "")
        user_input = entry.get("user_input", entry.get("content", ""))[:200]
        if intent and user_input:
            intent_inputs[intent].add(user_input)

    for intent, inputs in intent_inputs.items():
        if len(inputs) < 2:
            continue  # Only one phrasing seen — no variant to record
        slug_a = _slug(intent)
        # Strength = success rate of this intent
        st = intent_stats[intent]
        strength = round(st["success"] / st["total"], 3) if st["total"] > 0 else 0.0
        # Self-associative key (variant of same intent)
        key = f"associative:intent:{slug_a}:variants"
        if await _key_exists(qdrant, key):
            # Update observation_count and strength
            try:
                point_id = str(__import__("uuid").uuid5(
                    __import__("uuid").UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d"), key
                ))
                await qdrant.archive_client.set_payload(
                    collection_name=ASSOCIATIVE,
                    payload={
                        "observation_count": st["total"],
                        "strength": strength,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    },
                    points=[point_id],
                )
                stats["associative_updated"] += 1
            except Exception as e:
                logger.warning("synthesis: failed to update variant entry %s: %s", key, e)
            continue

        payload = {
            "type": "associative",
            "_key": key,
            "source_key": f"semantic:intent:{slug_a}",
            "target_key": f"semantic:intent:{slug_a}",
            "relationship": "same_intent_variant",
            "strength": strength,
            "observation_count": st["total"],
            "phrasing_variants": sorted(inputs)[:10],  # cap at 10 samples
        }
        try:
            await _upsert_raw(qdrant, ASSOCIATIVE, key, payload)
            stats["associative_created"] += 1
            logger.debug("synthesis: created same_intent_variant for %s (strength=%.3f)", intent, strength)
        except Exception as e:
            logger.warning("synthesis: failed to write variant entry %s: %s", key, e)

    # ── Step 4: Different outcome detection ───────────────────────────────────
    # Two intents that share semantic similarity but diverge in outcome → relational entry
    # Identify intent pairs where both have ≥1 success AND ≥1 failure
    mixed_intents = [
        intent for intent, st in intent_stats.items()
        if st["success"] >= 1 and st["failure"] >= 1 and st["total"] >= 3
    ]
    # For each pair of mixed intents: write or update a relational entry
    for intent_a, intent_b in combinations(mixed_intents, 2):
        slug_a, slug_b = _slug(intent_a), _slug(intent_b)
        key = _rel_key(slug_a, slug_b)

        st_a = intent_stats[intent_a]
        st_b = intent_stats[intent_b]
        shared = ["both have mixed success/failure outcomes"]
        diverges = [
            f"{intent_a} success_rate={st_a['success']}/{st_a['total']}",
            f"{intent_b} success_rate={st_b['success']}/{st_b['total']}",
        ]

        # LLM-infer insight when cog is available — look up actual semantic entries for
        # richer context than raw stats. Fall back to boilerplate if lookup fails or
        # cog is None (backward compat — nightly scheduler always passes cog).
        _boilerplate = (
            f"Both intents show mixed reliability. "
            f"Investigate failure patterns for {intent_a} and {intent_b} "
            f"to improve routing or payload construction."
        )
        if cog is not None:
            _ea = await qdrant.retrieve_by_key(f"semantic:intent:{slug_a}")
            _eb = await qdrant.retrieve_by_key(f"semantic:intent:{slug_b}")
            if _ea and _eb:
                _inf = await _infer_relationship(cog, _ea, _eb)
                insight = (
                    (_inf.get("insight") or _boilerplate)
                    if (_inf and _inf.get("related"))
                    else _boilerplate
                )
            else:
                insight = _boilerplate
        else:
            insight = _boilerplate

        if await _key_exists(qdrant, key):
            # Update path — refresh diverges (stats change) and insight on each nightly run
            try:
                _point_id = str(__import__("uuid").uuid5(
                    __import__("uuid").UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d"), key
                ))
                await qdrant.archive_client.set_payload(
                    collection_name=RELATIONAL,
                    payload={
                        "shared":       shared,
                        "diverges":     diverges,
                        "insight":      insight,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    },
                    points=[_point_id],
                )
                stats["relational_updated"] += 1
                logger.debug("synthesis: updated relational %s ↔ %s", intent_a, intent_b)
            except Exception as e:
                logger.warning("synthesis: pass2 update failed %s: %s", key, e)
                stats["skipped_existing"] += 1
            continue

        payload = {
            "type": "relational",
            "_key": key,
            "concept_a": f"semantic:intent:{slug_a}",
            "concept_b": f"semantic:intent:{slug_b}",
            "shared": shared,
            "diverges": diverges,
            "insight": insight,
        }
        try:
            await _upsert_raw(qdrant, RELATIONAL, key, payload)
            stats["relational_created"] += 1
            logger.debug("synthesis: created relational %s ↔ %s", intent_a, intent_b)
        except Exception as e:
            logger.warning("synthesis: failed to write relational entry %s: %s", key, e)

    # ── Step 5: Co-occurrence detection ────────────────────────────────────────
    # Intents that appear in the same session → associative co_occurs_with
    for session_id, intents in session_intents.items():
        # Deduplicate within session
        unique_intents = list(dict.fromkeys(intents))
        if len(unique_intents) < 2:
            continue

        # Only record co-occurrence for pairs — limit to first 6 unique to avoid combinatorial explosion
        for intent_a, intent_b in combinations(unique_intents[:6], 2):
            slug_a, slug_b = _slug(intent_a), _slug(intent_b)
            key = _assoc_key(slug_a, slug_b)
            if await _key_exists(qdrant, key):
                # Update strength (observation_count++)
                try:
                    point_id = str(__import__("uuid").uuid5(
                        __import__("uuid").UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d"), key
                    ))
                    # Fetch current observation_count
                    existing_entry = await qdrant.retrieve_by_key(key)
                    cur_count = (existing_entry or {}).get("observation_count", 0) if existing_entry else 0
                    new_count = cur_count + 1
                    # Strength grows with observation_count, caps at 1.0
                    new_strength = min(round(new_count / 10.0, 3), 1.0)
                    await qdrant.archive_client.set_payload(
                        collection_name=ASSOCIATIVE,
                        payload={
                            "observation_count": new_count,
                            "strength": new_strength,
                            "last_updated": datetime.now(timezone.utc).isoformat(),
                        },
                        points=[point_id],
                    )
                    stats["associative_updated"] += 1
                except Exception as e:
                    logger.warning("synthesis: co-occur update failed %s: %s", key, e)
                continue

            # Initial strength: 0.1 — low until multiple co-occurrences observed
            payload = {
                "type": "associative",
                "_key": key,
                "source_key": f"semantic:intent:{slug_a}",
                "target_key": f"semantic:intent:{slug_b}",
                "relationship": "co_occurs_with",
                "strength": 0.1,
                "observation_count": 1,
            }
            try:
                await _upsert_raw(qdrant, ASSOCIATIVE, key, payload)
                stats["associative_created"] += 1
                logger.debug("synthesis: co_occurs_with %s + %s", intent_a, intent_b)
            except Exception as e:
                logger.warning("synthesis: co-occur write failed %s: %s", key, e)

    # ── Pass 4: Structural synthesis over semantic collection ─────────────────
    # Full scan — cog required for inference; if None, structural stats are all zero.
    _structural = await synthesise_structural(key=None, qdrant=qdrant, cog=cog)
    if _structural.get("status") == "ok":
        stats["structural_processed"]   = _structural.get("semantic_processed", 0)
        stats["structural_created"]     = _structural.get("relational_created", 0)
        stats["structural_updated"]     = _structural.get("relational_updated", 0)
        stats["structural_assoc"]       = _structural.get("associative_created", 0)

    logger.info(
        "synthesis: complete — assoc_created=%d assoc_updated=%d "
        "rel_created=%d rel_updated=%d skipped=%d "
        "structural_created=%d structural_updated=%d",
        stats["associative_created"], stats["associative_updated"],
        stats["relational_created"], stats["relational_updated"],
        stats["skipped_existing"],
        stats.get("structural_created", 0), stats.get("structural_updated", 0),
    )
    return {"status": "ok", **stats}
