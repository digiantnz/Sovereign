"""QdrantAdapter — 7 typed sovereign collections + ephemeral working_memory.

Collections:
  working_memory  — ephemeral RAM, session cache (NVMe, on_disk=False)
  semantic        — durable facts/knowledge (RAID, on_disk=True)
  procedural      — repeatable workflows (RAID, on_disk=True, human_confirmed required)
  episodic        — timestamped experiences with outcomes (RAID, on_disk=True)
  prospective     — scheduled/conditional tasks (RAID, on_disk=True)
  associative     — links between memory items (RAID, on_disk=True)
  relational      — concept comparisons/contrasts (RAID, on_disk=True)
  meta            — domain knowledge maps with gap tracking (RAID, on_disk=True)

Embeddings via Ollama nomic-embed-text (768-dim).
Write permissions enforced per collection. All sovereign writes audited to JSONL.
"""
import asyncio
import json
import logging
import os
import uuid
import httpx
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, HnswConfigDiff,
    Filter, FieldCondition, MatchValue,
)

# Collection names
WORKING      = "working_memory"
SEMANTIC     = "semantic"
PROCEDURAL   = "procedural"
EPISODIC     = "episodic"
PROSPECTIVE  = "prospective"
ASSOCIATIVE  = "associative"
RELATIONAL   = "relational"
META         = "meta"

SOVEREIGN_COLLECTIONS = [SEMANTIC, PROCEDURAL, EPISODIC, PROSPECTIVE, ASSOCIATIVE, RELATIONAL, META]

VECTOR_DIM          = 768
EMBED_MODEL         = "nomic-embed-text"
AUDIT_PATH          = "/home/sovereign/audit/memory-promotions.jsonl"
CONFIDENCE_THRESHOLD = 0.75

# ── Query type classification ─────────────────────────────────────────────
_ACTION_KW = frozenset([
    "restart", "delete", "send", "write", "create", "move", "run", "deploy",
    "update", "fix", "execute", "remove", "stop", "start", "rebuild", "prune",
    "add", "edit", "save", "push", "pull", "archive", "forward", "reply",
])
_SESSION_KW = (
    "good morning", "morning brief", "good evening", "hey sovereign",
    "start of day", "what's on today", "what do i have today", "briefing",
    "wake up", "morning", "what's due", "what's on",
)

def classify_query_type(user_input: str) -> str:
    """Classify query as action | knowledge | session_start for collection weighting."""
    u = user_input.lower()
    if any(w in u for w in _SESSION_KW):
        return "session_start"
    if set(u.split()) & _ACTION_KW:
        return "action"
    return "knowledge"

# Score multipliers per collection per query type
COLLECTION_WEIGHTS = {
    "action": {
        EPISODIC: 1.4, PROCEDURAL: 1.3, SEMANTIC: 1.0,
        META: 0.9, ASSOCIATIVE: 0.8, PROSPECTIVE: 0.8, RELATIONAL: 0.7,
    },
    "knowledge": {
        SEMANTIC: 1.4, META: 1.3, RELATIONAL: 1.1,
        EPISODIC: 0.9, ASSOCIATIVE: 0.8, PROCEDURAL: 0.8, PROSPECTIVE: 0.6,
    },
    "session_start": {
        PROSPECTIVE: 1.5, SEMANTIC: 1.1, META: 1.1,
        EPISODIC: 0.9, PROCEDURAL: 0.8, ASSOCIATIVE: 0.7, RELATIONAL: 0.7,
    },
}

# Write permissions per collection
WRITE_PERMISSIONS = {
    SEMANTIC:    {"sovereign-core"},
    PROCEDURAL:  {"sovereign-core"},
    EPISODIC:    {"sovereign-core", "specialist"},
    PROSPECTIVE: {"sovereign-core", "specialist"},
    ASSOCIATIVE: {"sovereign-core"},
    RELATIONAL:  {"sovereign-core"},
    META:        {"sovereign-core"},
    WORKING:     {"sovereign-core", "specialist"},
}


class QdrantAdapter:
    def __init__(self, qdrant_url="http://qdrant:6333",
                 qdrant_archive_url="http://qdrant-archive:6333",
                 ollama_url="http://ollama:11434"):
        self.client = AsyncQdrantClient(url=qdrant_url)                       # NVMe — conscious hot layer
        self.archive_client = AsyncQdrantClient(url=qdrant_archive_url)       # RAID — subconscious durable store
        self.wm_client = AsyncQdrantClient(location=":memory:")               # RAM — truly ephemeral, never touches disk
        self._ollama_url = ollama_url

    def _client_for(self, collection: str) -> "AsyncQdrantClient":
        """Route working_memory to the in-process RAM client; everything else to NVMe Qdrant."""
        return self.wm_client if collection == WORKING else self.client

    async def _embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.post(f"{self._ollama_url}/api/embeddings",
                                json={"model": EMBED_MODEL, "prompt": text})
            r.raise_for_status()
            return r.json()["embedding"]

    async def _generate_key_and_title(
        self, content: str, mem_type: str, domain: str
    ) -> tuple[str | None, str | None]:
        """Generate a deterministic _key and title for a memory entry via a single Ollama call.

        Key format: {type}:{domain}:{slug} — the type and domain prefix is assembled here from
        known fields so Ollama cannot deviate from it. Only the slug is LLM-generated.
        Returns (key, title) on success, (None, None) on any failure or timeout.
        Never raises — promotion must never block on key generation failure.
        """
        # Build safe prefix from known fields — strip anything non-alphanumeric/hyphen
        _type = "".join(c if c.isalnum() or c == "-" else "-" for c in mem_type.lower())[:20].strip("-") or "memory"
        _dom  = "".join(c if c.isalnum() or c == "-" else "-" for c in domain.lower())[:20].strip("-") or "general"
        _prefix = f"{_type}:{_dom}:"

        _prompt = (
            f"Given the memory item below, respond with ONLY valid JSON containing two fields:\n"
            f"- \"slug\": 2-5 lowercase hyphen-separated words that uniquely and specifically "
            f"identify this memory item's content (not the category — the specific subject). "
            f"Only use a-z, 0-9, and hyphens.\n"
            f"- \"title\": one sentence (max 15 words) summarising the content.\n\n"
            f"Content: {content[:400]}"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                r = await http.post(
                    f"{self._ollama_url}/api/generate",
                    json={
                        "model": "llama3.1:8b-instruct-q4_K_M",
                        "prompt": _prompt,
                        "stream": False,
                        "format": "json",
                    },
                )
                r.raise_for_status()
                raw = r.json().get("response", "{}")
                parsed = json.loads(raw)
                # Sanitise slug: lowercase, only a-z 0-9 hyphens, collapse multiples
                _raw_slug = str(parsed.get("slug", "")).lower().strip()
                slug = "".join(c if c.isalnum() or c == "-" else "-" for c in _raw_slug)
                # Collapse consecutive hyphens and trim
                while "--" in slug:
                    slug = slug.replace("--", "-")
                slug = slug.strip("-")
                title = str(parsed.get("title", "")).strip()
                if slug and 3 <= len(slug) <= 60:
                    return f"{_prefix}{slug}", title or content[:80]
                _log.warning(
                    "key_generation_invalid: Ollama returned unusable slug %r "
                    "(prefix=%r content_preview=%r)",
                    slug, _prefix, content[:60],
                )
                self._log_audit(
                    "key_generation_invalid", _prefix.rstrip(":"), "—",
                    "sovereign-core", content[:120],
                )
                return None, None
        except Exception as _exc:
            _log.warning(
                "key_generation_failed: %s — entry will be stored with _no_key=True "
                "(prefix=%r content_preview=%r)",
                type(_exc).__name__, _prefix, content[:60],
            )
            self._log_audit(
                "key_generation_failed", _prefix.rstrip(":"), "—",
                "sovereign-core", content[:120],
            )
            return None, None

    async def setup(self):
        """Called at startup. Recreates working_memory (ephemeral RAM).
        Creates each sovereign collection only if absent (preserves RAID data).
        Does NOT touch old sovereign_memory collection.
        """
        # working_memory: in-process RAM only — create on wm_client, never on the server
        await self.wm_client.create_collection(
            collection_name=WORKING,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE, on_disk=False),
            hnsw_config=HnswConfigDiff(on_disk=False),
        )

        # Remove any stale working_memory from the NVMe Qdrant server (now lives in wm_client only)
        existing = {c.name for c in (await self.client.get_collections()).collections}
        if WORKING in existing:
            await self.client.delete_collection(WORKING)

        # Remove stale working_memory from RAID archive — it should never persist there
        try:
            archive_existing = {c.name for c in (await self.archive_client.get_collections()).collections}
            if WORKING in archive_existing:
                await self.archive_client.delete_collection(WORKING)
                _log.info("setup: removed stale working_memory from RAID archive")
        except Exception as _e:
            _log.warning("setup: could not clean working_memory from archive: %s", _e)

        # 7 sovereign collections: create if absent (NVMe hot layer; RAID archive in sync_from/to_archive)
        for coll in SOVEREIGN_COLLECTIONS:
            if coll not in existing:
                await self.client.create_collection(
                    collection_name=coll,
                    vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE, on_disk=True),
                    hnsw_config=HnswConfigDiff(on_disk=True),
                )

    async def startup_load(self):
        """Warm working_memory from all 7 sovereign collections.
        Embeds a sentinel query once, then parallel-queries all collections.
        Copies vectors directly (no re-embed). Items tagged startup_load=True.
        """
        try:
            vector = await self._embed("current system state knowledge overview")
        except Exception:
            return

        async def _load_one(coll: str):
            try:
                response = await self.client.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=2,
                    score_threshold=0.3,
                    with_payload=True,
                    with_vectors=True,
                )
                for r in response.points:
                    raw_vec = r.vector if isinstance(r.vector, list) else None
                    if raw_vec is None:
                        continue
                    payload = dict(r.payload or {})
                    payload["startup_load"] = True
                    payload["source_collection"] = coll
                    await self.wm_client.upsert(                # RAM only — subconscious → conscious seed
                        collection_name=WORKING,
                        points=[PointStruct(
                            id=str(uuid.uuid4()),
                            vector=raw_vec,
                            payload=payload,
                        )],
                    )
            except Exception:
                pass  # swallow per-collection errors

        await asyncio.gather(*[_load_one(c) for c in SOVEREIGN_COLLECTIONS])

    async def seed_skill_install_procedure(self) -> bool:
        """Seed the PROCEDURAL collection with the skill installation sequence.

        This entry encodes the mandatory 3-step flow so the devops specialist
        retrieves it when the Director asks to find or install a skill:
          Step 1 — skill_search  : find candidates, present to Director
          Step 2 — skill_review  : 4-layer security pipeline, present result
          Step 3 — skill_load    : only with review_result + Director confirmation

        Idempotent — checks if an entry with tag 'skill_install_sequence' already
        exists before writing. Returns True if written, False if already seeded.
        """
        _SEED_TAG = "skill_install_sequence"
        _SEED_CONTENT = (
            "SKILL INSTALLATION PROCEDURE — devops_agent MUST follow this sequence exactly "
            "when any Director request involves finding, searching, reviewing, or installing a skill:\n\n"
            "Step 1 — skill_search: Search for candidate skills using SearXNG via the browser adapter. "
            "Present the candidates (slug, summary, github_url) to the Director and ask them to select one.\n\n"
            "Step 2 — skill_review: Call skill_review with the selected candidate's SKILL.md content. "
            "Run the full 4-layer security pipeline (escalation keywords → scanner → LLM evaluation → certification). "
            "Present the complete review_result to the Director: decision (approve/review/block), "
            "risk_level, escalation_reasons, scanner_categories, and llm_assessment. "
            "If decision is 'block', stop immediately — do NOT proceed to load. "
            "If escalate_to_director is True, explicitly state the escalation reasons and require "
            "the Director to say 'yes, install it' or equivalent clear confirmation.\n\n"
            "Step 3 — skill_load: Call skill_load ONLY after the Director has explicitly confirmed "
            "after seeing the review_result from Step 2. Pass review_result in the action payload. "
            "Never call skill_load without a completed review_result. Never skip Step 2.\n\n"
            "INVARIANT: skill_load without review_result is an error. If asked to 'just install it', "
            "always run the review first and present results before loading."
        )

        try:
            # Check if already seeded
            try:
                vec = await self._embed("install skill sequence search review load")
                existing = await self.client.query_points(
                    collection_name=PROCEDURAL,
                    query=vec,
                    limit=5,
                    score_threshold=0.85,
                    with_payload=True,
                )
                for pt in existing.points:
                    if (pt.payload or {}).get("tag") == _SEED_TAG:
                        return False  # already seeded
            except Exception:
                pass

            await self.store(
                content=_SEED_CONTENT,
                metadata={
                    "type": "procedural",
                    "tag": _SEED_TAG,
                    "domain": "skills",
                    "source": "system_seed",
                    "human_confirmed": True,
                },
                collection=PROCEDURAL,
                writer="sovereign-core",
                human_confirmed=True,
            )
            return True
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).warning("seed_skill_install_procedure failed: %s", e)
            return False

    async def store(self, content: str, metadata: dict,
                    collection: str = WORKING,
                    writer: str = "sovereign-core",
                    human_confirmed: bool = False) -> str:
        """Embed and store. Returns point ID.

        Raises PermissionError if writer lacks access or procedural written without human_confirmed.
        """
        if not self._can_write(writer, collection):
            raise PermissionError(
                f"Writer '{writer}' is not permitted to write to collection '{collection}'"
            )
        if collection == PROCEDURAL and not human_confirmed:
            raise PermissionError(
                "Collection 'procedural' requires human_confirmed=True to write"
            )

        vector = await self._embed(content)
        point_id = str(uuid.uuid4())
        _now = datetime.now(timezone.utc).isoformat()

        # Key + title generation — sovereign collections only (working_memory is ephemeral)
        _key_fields: dict = {}
        if collection in SOVEREIGN_COLLECTIONS:
            if metadata.get("_key"):
                # Canonical key explicitly provided — skip LLM, just stamp timestamps
                _key_fields = {"last_updated": _now}
            else:
                _key, _title = await self._generate_key_and_title(
                    content,
                    metadata.get("type", collection),
                    metadata.get("domain", "general"),
                )
                if _key:
                    _key_fields = {"_key": _key, "title": _title, "last_updated": _now}
                else:
                    _key_fields = {"_no_key": True, "last_updated": _now}

        await self._client_for(collection).upsert(
            collection_name=collection,
            points=[PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "content": content,
                    "timestamp": _now,
                    **metadata,
                    **_key_fields,
                },
            )],
        )

        if collection in SOVEREIGN_COLLECTIONS:
            self._log_audit("store", collection, point_id, writer, content[:120])

        return point_id

    async def search(self, query: str, collection: str = WORKING,
                     top_k: int = 5, score_threshold: float = 0.4) -> list[dict]:
        """Single-collection semantic search. Returns list of payload dicts with score."""
        vector = await self._embed(query)
        response = await self._client_for(collection).query_points(
            collection_name=collection,
            query=vector,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return [{"score": r.score, **r.payload} for r in response.points]

    async def search_all_sovereign(self, query: str,
                                   top_k: int = 3,
                                   score_threshold: float = 0.35) -> list[dict]:
        """Embed once, parallel-search all 7 sovereign collections.
        Returns merged results sorted descending by score, each tagged _collection.
        """
        vector = await self._embed(query)

        async def _search_one(coll: str):
            try:
                resp = await self.client.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=top_k,
                    score_threshold=score_threshold,
                    with_payload=True,
                )
                return [{"score": r.score, "_collection": coll, **r.payload}
                        for r in resp.points]
            except Exception:
                return []

        results_nested = await asyncio.gather(
            *[_search_one(c) for c in SOVEREIGN_COLLECTIONS],
            return_exceptions=True,
        )

        merged = []
        for item in results_nested:
            if isinstance(item, list):
                merged.extend(item)

        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged

    def compute_confidence(self, results: list[dict]) -> float:
        """Returns max score across results, or 0.0 if empty."""
        return max((r["score"] for r in results), default=0.0)

    def get_gaps(self, results: list[dict]) -> list[str]:
        """Extracts gaps[] arrays from meta collection results."""
        gaps = []
        for r in results:
            if r.get("_collection") == META:
                for g in r.get("gaps", []):
                    if g not in gaps:
                        gaps.append(g)
        return gaps

    async def promote(self, point_id: str,
                      target_collection: str = None,
                      writer: str = "sovereign-core",
                      human_confirmed: bool = False) -> bool:
        """Move a point from working_memory to a sovereign collection.

        target_collection inferred from payload.type if not provided; defaults to episodic.
        """
        points = await self.wm_client.retrieve(
            collection_name=WORKING,
            ids=[point_id],
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            return False
        p = points[0]
        payload = dict(p.payload or {})

        # Infer target from payload type if not explicit
        if target_collection is None:
            inferred = payload.get("type", "")
            target_collection = inferred if inferred in SOVEREIGN_COLLECTIONS else EPISODIC

        if not self._can_write(writer, target_collection):
            raise PermissionError(
                f"Writer '{writer}' cannot promote to collection '{target_collection}'"
            )
        if target_collection == PROCEDURAL and not human_confirmed:
            raise PermissionError(
                "Promoting to 'procedural' requires human_confirmed=True"
            )

        vec = p.vector if isinstance(p.vector, list) else None
        if vec is None:
            return False

        new_id = str(uuid.uuid4())
        _now = datetime.now(timezone.utc).isoformat()
        _key, _title = await self._generate_key_and_title(
            payload.get("content", ""),
            payload.get("type", target_collection),
            payload.get("domain", "general"),
        )
        if _key:
            payload["_key"] = _key
            payload["title"] = _title
            payload["last_updated"] = _now
        else:
            payload["_no_key"] = True
            payload["last_updated"] = _now

        await self.client.upsert(                   # sovereign collection → NVMe hot layer
            collection_name=target_collection,
            points=[PointStruct(id=new_id, vector=vec, payload=payload)],
        )
        await self.wm_client.delete(collection_name=WORKING, points_selector=[point_id])
        self._log_audit("promote", target_collection, new_id, writer,
                        payload.get("content", "")[:120])
        return True

    async def shutdown_promote(self) -> int:
        """On shutdown: promote eligible working_memory items to sovereign collections.

        Skips: items with startup_load=True, type not in SOVEREIGN_COLLECTIONS, procedural.
        Returns count of promoted items.
        """
        items = await self._get_all_working_memory()
        promoted = 0
        for item in items:
            payload = item.get("payload", {})
            mem_type = payload.get("type", "")

            # Skip startup-loaded items (already came from sovereign)
            if payload.get("startup_load"):
                continue
            # Skip items with no valid sovereign type
            if mem_type not in SOVEREIGN_COLLECTIONS:
                continue
            # Skip procedural — requires human confirmation
            if mem_type == PROCEDURAL:
                continue

            vec = item.get("vector")
            if not vec:
                continue

            new_id = str(uuid.uuid4())
            _now = datetime.now(timezone.utc).isoformat()
            _key, _title = await self._generate_key_and_title(
                payload.get("content", ""),
                mem_type,
                payload.get("domain", "general"),
            )
            if _key:
                payload["_key"] = _key
                payload["title"] = _title
                payload["last_updated"] = _now
            else:
                payload["_no_key"] = True
                payload["last_updated"] = _now
            try:
                await self.client.upsert(
                    collection_name=mem_type,
                    points=[PointStruct(id=new_id, vector=vec, payload=payload)],
                )
                self._log_audit(
                    "shutdown_promote", mem_type, new_id,
                    "sovereign-core", payload.get("content", "")[:120],
                )
                promoted += 1
            except Exception:
                pass

        return promoted

    async def sync_from_archive(self) -> int:
        """Startup: pull any sovereign collection points from RAID Qdrant that are absent on NVMe.

        Scrolls each of the 7 sovereign collections on both instances; upserts RAID points
        whose UUID is not already present on NVMe. working_memory is always ephemeral — skipped.
        Returns total count of points copied RAID → NVMe.
        """
        copied = 0
        for coll in SOVEREIGN_COLLECTIONS:
            try:
                # Ensure collection exists on NVMe (setup() should have created it, but be safe)
                existing_colls = {c.name for c in (await self.client.get_collections()).collections}
                if coll not in existing_colls:
                    await self.client.create_collection(
                        collection_name=coll,
                        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE, on_disk=True),
                        hnsw_config=HnswConfigDiff(on_disk=True),
                    )

                # Collect all existing NVMe point IDs for this collection
                nvme_ids: set = set()
                offset = None
                while True:
                    result, next_offset = await self.client.scroll(
                        collection_name=coll, limit=200, offset=offset,
                        with_payload=False, with_vectors=False,
                    )
                    for r in result:
                        nvme_ids.add(str(r.id))
                    if next_offset is None:
                        break
                    offset = next_offset

                # Scroll RAID archive and upsert missing points to NVMe
                offset = None
                while True:
                    try:
                        result, next_offset = await self.archive_client.scroll(
                            collection_name=coll, limit=100, offset=offset,
                            with_payload=True, with_vectors=True,
                        )
                    except Exception:
                        break  # collection may not exist on archive yet
                    for r in result:
                        if str(r.id) not in nvme_ids:
                            vec = r.vector if isinstance(r.vector, list) else None
                            if vec is None:
                                continue
                            await self.client.upsert(
                                collection_name=coll,
                                points=[PointStruct(id=r.id, vector=vec, payload=dict(r.payload or {}))],
                            )
                            copied += 1
                    if next_offset is None:
                        break
                    offset = next_offset
            except Exception as exc:
                _log.warning("sync_from_archive: error on collection %s: %s", coll, exc)
        return copied

    async def sync_to_archive(self) -> int:
        """Shutdown: push any sovereign collection points from NVMe Qdrant absent on RAID archive.

        Ensures RAID is always a superset of NVMe — the durable long-term store.
        Returns total count of points copied NVMe → RAID.
        """
        pushed = 0
        for coll in SOVEREIGN_COLLECTIONS:
            try:
                # Ensure collection exists on archive
                try:
                    archive_colls = {c.name for c in (await self.archive_client.get_collections()).collections}
                except Exception:
                    archive_colls = set()
                if coll not in archive_colls:
                    await self.archive_client.create_collection(
                        collection_name=coll,
                        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE, on_disk=True),
                        hnsw_config=HnswConfigDiff(on_disk=True),
                    )

                # Collect all existing RAID point IDs for this collection
                raid_ids: set = set()
                offset = None
                while True:
                    try:
                        result, next_offset = await self.archive_client.scroll(
                            collection_name=coll, limit=200, offset=offset,
                            with_payload=False, with_vectors=False,
                        )
                        for r in result:
                            raid_ids.add(str(r.id))
                        if next_offset is None:
                            break
                        offset = next_offset
                    except Exception:
                        break

                # Scroll NVMe and upsert missing points to RAID
                offset = None
                while True:
                    result, next_offset = await self.client.scroll(
                        collection_name=coll, limit=100, offset=offset,
                        with_payload=True, with_vectors=True,
                    )
                    for r in result:
                        if str(r.id) not in raid_ids:
                            vec = r.vector if isinstance(r.vector, list) else None
                            if vec is None:
                                continue
                            await self.archive_client.upsert(
                                collection_name=coll,
                                points=[PointStruct(id=r.id, vector=vec, payload=dict(r.payload or {}))],
                            )
                            pushed += 1
                    if next_offset is None:
                        break
                    offset = next_offset
            except Exception as exc:
                _log.warning("sync_to_archive: error on collection %s: %s", coll, exc)
        return pushed

    async def _get_all_working_memory(self) -> list[dict]:
        """Scroll all working_memory items (with vectors). Uses in-memory wm_client."""
        items = []
        offset = None
        while True:
            result, next_offset = await self.wm_client.scroll(
                collection_name=WORKING,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for r in result:
                items.append({
                    "id": r.id,
                    "payload": dict(r.payload or {}),
                    "vector": r.vector if isinstance(r.vector, list) else None,
                })
            if next_offset is None:
                break
            offset = next_offset
        return items

    async def search_all_weighted(self, query: str, query_type: str = "knowledge",
                                   top_k: int = 3,
                                   score_threshold: float = 0.35) -> list[dict]:
        """Embed once, parallel-search all 7 sovereign collections with context-aware
        score weighting. Episodic/procedural boosted for action queries; semantic/meta
        boosted for knowledge queries; prospective boosted on session start.
        """
        weights = COLLECTION_WEIGHTS.get(query_type, COLLECTION_WEIGHTS["knowledge"])
        vector = await self._embed(query)

        async def _search_one(coll: str):
            try:
                resp = await self.client.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=top_k,
                    score_threshold=score_threshold,
                    with_payload=True,
                )
                w = weights.get(coll, 1.0)
                return [
                    {"score": r.score * w, "_raw_score": r.score,
                     "_collection": coll, "_weight": w, **r.payload}
                    for r in resp.points
                ]
            except Exception:
                return []

        results_nested = await asyncio.gather(
            *[_search_one(c) for c in SOVEREIGN_COLLECTIONS],
            return_exceptions=True,
        )
        merged = []
        for item in results_nested:
            if isinstance(item, list):
                merged.extend(item)
        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged

    async def get_due_prospective(self) -> list[dict]:
        """Return prospective items where next_due <= today (ISO YYYY-MM-DD).
        Prospective is small so full scroll is cheap.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            scroll_result = await self.client.scroll(
                collection_name=PROSPECTIVE,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
            items = scroll_result[0]
        except Exception:
            return []
        due = []
        for item in items:
            payload = dict(item.payload or {})
            next_due = payload.get("next_due", "")
            if next_due and next_due <= today:
                due.append(payload)
        return sorted(due, key=lambda x: x.get("next_due", ""))

    async def ensure_gap_entry(self, query: str) -> bool:
        """Check meta for an existing gap entry covering this query.
        If none found and confidence was very low, create a gap entry.
        Returns True if a new gap entry was created.
        Only creates entries for genuinely opaque queries (score_threshold=0.65).
        """
        try:
            existing = await self.search(
                f"knowledge gap {query}", collection=META, top_k=3, score_threshold=0.65
            )
            for r in existing:
                if r.get("type") == "gap":
                    return False  # Already documented
            # Derive a domain label from the query
            words = [w for w in query.lower().split() if len(w) > 3][:5]
            domain_label = " ".join(words) if words else query[:40]
            await self.store(
                content=f"Knowledge gap: {query}",
                metadata={
                    "type": "gap",
                    "domain": domain_label,
                    "query": query[:200],
                    "gap_confirmed": True,
                    "source": "auto-gap-detection",
                },
                collection=META,
                writer="sovereign-core",
            )
            return True
        except Exception:
            return False

    # ── Memory Index Protocol (MIP) ───────────────────────────────────────

    async def startup_migration(self) -> int:
        """One-time migration: stamp _no_key=True on any sovereign entry missing a _key field.

        Scrolls all 7 sovereign collections, patches payload only — no re-embedding.
        Idempotent: entries already carrying _key or _no_key are skipped.
        Returns count of entries patched.
        """
        patched = 0
        for coll in SOVEREIGN_COLLECTIONS:
            try:
                offset = None
                while True:
                    result, next_offset = await self.client.scroll(
                        collection_name=coll,
                        limit=100,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    for r in result:
                        payload = dict(r.payload or {})
                        if "_key" not in payload and not payload.get("_no_key"):
                            await self.client.set_payload(
                                collection_name=coll,
                                payload={"_no_key": True},
                                points=[r.id],
                            )
                            patched += 1
                    if next_offset is None:
                        break
                    offset = next_offset
            except Exception:
                pass  # swallow per-collection errors; never crash startup
        return patched

    async def list_all_keys(self) -> list[dict]:
        """Return a structured directory of all sovereign memory entries.

        Scrolls all 7 collections. Returns index fields only — no full content.
        Use retrieve_by_key() for the complete payload.
        """
        directory: list[dict] = []
        for coll in SOVEREIGN_COLLECTIONS:
            try:
                offset = None
                while True:
                    result, next_offset = await self.client.scroll(
                        collection_name=coll,
                        limit=100,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    for r in result:
                        payload = dict(r.payload or {})
                        _raw_key = payload.get("_key")
                        if payload.get("_no_key"):
                            key_display = "NO_KEY"
                        elif _raw_key:
                            key_display = _raw_key
                        else:
                            key_display = None
                        directory.append({
                            "collection":   coll,
                            "point_id":     str(r.id),
                            "key":          key_display,
                            "type":         payload.get("type"),
                            "title":        payload.get("title") or payload.get("content", "")[:120],
                            "last_updated": payload.get("last_updated") or payload.get("timestamp"),
                        })
                    if next_offset is None:
                        break
                    offset = next_offset
            except Exception:
                pass
        return directory

    async def retrieve_by_key(self, key: str) -> dict | None:
        """Exact-key lookup across all 7 sovereign collections.

        Never uses vector search — pure Qdrant payload filter on _key == key.
        Returns full payload + collection and point_id, or None if not found.
        """
        for coll in SOVEREIGN_COLLECTIONS:
            try:
                result, _ = await self.client.scroll(
                    collection_name=coll,
                    scroll_filter=Filter(
                        must=[FieldCondition(key="_key", match=MatchValue(value=key))]
                    ),
                    limit=1,
                    with_payload=True,
                    with_vectors=False,
                )
                if result:
                    payload = dict(result[0].payload or {})
                    return {"collection": coll, "point_id": str(result[0].id), **payload}
            except Exception:
                continue
        return None

    async def seed_static_facts(self, facts: list[dict]) -> int:
        """Seed high-value static facts into semantic memory with MIP keys.

        Each fact dict: {seed_id, content, domain, key, title, extra_meta (optional)}.
        - key/title: canonical hardcoded values — passed in metadata so store() skips LLM.
        - Idempotent via _backfill_seed_id field.
        - If an existing entry's _key doesn't match the canonical key exactly, deletes and reseeds.
        Returns count of new or replaced entries written.
        """
        written = 0
        for fact in facts:
            seed_id = fact.get("seed_id", "")
            content = fact.get("content", "")
            domain = fact.get("domain", "general")
            canonical_key = fact.get("key", "")
            canonical_title = fact.get("title", content[:80])
            if not content or not seed_id:
                continue
            try:
                existing, _ = await self.client.scroll(
                    collection_name=SEMANTIC,
                    scroll_filter=Filter(
                        must=[FieldCondition(
                            key="_backfill_seed_id", match=MatchValue(value=seed_id)
                        )]
                    ),
                    limit=1,
                    with_payload=True,
                    with_vectors=False,
                )
                if existing:
                    pay = dict(existing[0].payload or {})
                    stored_key = pay.get("_key", "")
                    if stored_key == canonical_key:
                        continue  # Already seeded with correct canonical key — skip
                    # Wrong key (old generator or prefix change) — delete and re-seed
                    await self.client.delete(
                        collection_name=SEMANTIC,
                        points_selector=[existing[0].id],
                    )

                metadata = {
                    "type": "semantic",
                    "domain": domain,
                    "source": "static_backfill",
                    "_backfill_seed_id": seed_id,
                    "_key": canonical_key,
                    "title": canonical_title,
                    **(fact.get("extra_meta") or {}),
                }
                await self.store(
                    content=content,
                    metadata=metadata,
                    collection=SEMANTIC,
                    writer="sovereign-core",
                )
                written += 1
            except Exception:
                pass  # never block startup on backfill failure
        return written

    async def tag_high_value_entries(self, patterns: list[dict]) -> int:
        """Assign canonical MIP keys to existing semantic entries via set_payload().

        No re-embedding — existing vectors are preserved. Idempotent: entries that
        already have a _key are skipped. Entries are matched by content substring.

        Each pattern dict: {match, key, title}
        - match: substring that must appear in entry content (case-sensitive)
        - key:   canonical key to assign (e.g. 'semantic:governance:confirmation_tiers')
        - title: short title to assign
        Returns count of entries tagged.
        """
        _now = datetime.now(timezone.utc).isoformat()
        tagged = 0
        offset = None
        while True:
            try:
                pts, nxt = await self.client.scroll(
                    collection_name=SEMANTIC,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                _log.warning("tag_high_value_entries scroll failed: %s", exc)
                break
            for p in pts:
                pay = dict(p.payload or {})
                if pay.get("_key"):
                    continue  # already has a valid canonical key — skip
                content = pay.get("content", "")
                for pattern in patterns:
                    if pattern["match"] in content:
                        try:
                            await self.client.set_payload(
                                collection_name=SEMANTIC,
                                payload={
                                    "_key": pattern["key"],
                                    "title": pattern["title"],
                                    "last_updated": _now,
                                    "_no_key": None,  # clear migration tombstone if present
                                },
                                points=[p.id],
                            )
                            self._log_audit(
                                "tag_high_value", SEMANTIC, str(p.id),
                                "sovereign-core", f"→ {pattern['key']}"
                            )
                            tagged += 1
                        except Exception as exc:
                            _log.warning("tag_high_value set_payload failed: %s", exc)
                        break  # only match first pattern per entry
            offset = nxt
            if not nxt:
                break
        return tagged

    def _can_write(self, writer: str, collection: str) -> bool:
        return writer in WRITE_PERMISSIONS.get(collection, {"sovereign-core"})

    def _log_audit(self, event_type: str, collection: str,
                   point_id: str, writer: str, content_preview: str):
        try:
            os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
            entry = json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "collection": collection,
                "point_id": point_id,
                "writer": writer,
                "content_preview": content_preview,
            })
            with open(AUDIT_PATH, "a") as f:
                f.write(entry + "\n")
        except Exception:
            pass  # audit failure must never crash the adapter
