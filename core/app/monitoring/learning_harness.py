"""Sovereign Learning Harness — autonomous document learning from /downloads/ and Notes

Trigger (three paths):
  1. Hourly poll — lists /downloads/, processes new files during synthesis window
     (UTC hours 15–17) to avoid Ollama contention.
  2. Immediate — Telegram attachment upload to /downloads/ calls
     check_downloads(app_state, immediate=True) as a background task; no time gate.
  3. Hourly poll (same window) — check_notes() queries Nextcloud Notes API and processes
     notes not yet seen. No immediate trigger; synthesis window gate always active.

Confidence loop (per file):
  Round-robin: semantic pass → relational pass over all chunks.
  Each pass proposes creates/updates to archive memory.
  Exits when a full round-robin cycle produces zero delta (plateau reached).
  Minimum one full cycle guaranteed. Safety cap: 10 cycles.

Collections written:
  semantic   — new concepts/facts extracted from document
  relational — structural links between concepts

Collections NOT written by this harness:
  associative — populated ONLY by run_synthesis() nightly cron (13:00 UTC = 01:00 NZST)
                (associative entries ARE read for context in doc_array)

Sentinel: episodic:learning:processed:{slug} (MIP format)
  Regular files: slug = sha256(file_path + "|" + str(size) + "|" + last_modified)[:16]
  .url files:    slug = sha256(url.strip())[:16] — keyed to URL, not file metadata
                 failed fetch → episodic:learning:failed:{slug} (prevents retry)
  Notes API:     slug = sha256("notes-api:{id}|{modified}")[:16] — keyed to note ID + timestamp
  Written on successful plateau AND on format-skip. Prevents re-processing.
  Hard failures do NOT write sentinel — retried next tick.
  All-timeout failures (GPU busy for every chunk, zero entries written) increment a
  timeout-count sentinel (episodic:learning:timeout-count:{slug}). After
  _MAX_TIMEOUT_RETRIES consecutive all-timeout runs the regular processed sentinel is
  written with outcome "loop_timed_out", blocking further retries. Delete that sentinel
  from qdrant-archive episodic to re-enable processing.

No file-size gate — documents of any size are accepted; the chunker handles splitting.

Last-run summary: _last_run_summary module dict, read by task_scheduler morning briefing.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import string
from datetime import datetime, timezone

import httpx
from qdrant_client.models import Filter, FieldCondition, MatchValue

logger = logging.getLogger(__name__)

from config import cfg as _cfg

# ── Constants ─────────────────────────────────────────────────────────────────

_PROCESSING_HOURS = frozenset(_cfg.learning_harness.processing_hours_utc)
_CHUNK_CHARS      = _cfg.learning_harness.chunk_chars
_CONTEXT_CHARS    = _cfg.learning_harness.context_chars
_MAX_DOC_ARRAY    = _cfg.learning_harness.max_doc_array
_SCROLL_BATCH     = _cfg.learning_harness.scroll_batch
_MAX_CYCLES       = _cfg.learning_harness.max_cycles
_MAX_FILE_BYTES   = _cfg.learning_harness.max_file_bytes
_NOTES_ENABLED    = getattr(_cfg.learning_harness, "notes_enabled", True)

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "up", "about", "into", "through", "during", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall", "can", "that",
    "this", "these", "those", "it", "its", "they", "them", "their", "what", "which",
    "who", "how", "when", "where", "why", "not", "also", "as", "if", "so", "then",
    "than", "just", "each", "all", "any", "both", "few", "more", "most", "other",
    "some", "such", "no", "nor", "only", "own", "same", "too", "very", "i", "we",
    "he", "she", "you", "me", "him", "her", "us",
})

# Extensions where fs_read returns parseable plain text
_SUPPORTED_TEXT_EXT = frozenset({
    ".txt", ".md", ".csv", ".json", ".py", ".rst", ".log",
    ".html", ".yaml", ".yml", ".toml", ".xml",
    ".pdf",
})

# URL shortcut files — fetched via browser, not read directly
_URL_EXT = frozenset({".url"})

# ── Module-level state ─────────────────────────────────────────────────────────

_run_in_progress  = False   # reentrance guard — False while idle, True during processing
_last_run_summary: dict = {}  # shared with task_scheduler for morning briefing injection


# ── Telegram notification ─────────────────────────────────────────────────────

async def _notify_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("LearningHarness: Telegram credentials missing — skipping notification")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("LearningHarness: Telegram notification failed: %s", e)


# ── Keyword extraction ────────────────────────────────────────────────────────

def _extract_keywords(text: str) -> set:
    """Deterministic stopword-filtered token set. Min 4 chars, no punctuation."""
    text = text.lower().translate(str.maketrans("", "", string.punctuation))
    return {w for w in text.split() if len(w) >= 4 and w not in _STOPWORDS}


# ── Text chunking ─────────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list:
    """Split text into ~_CHUNK_CHARS chunks, preserving sentence boundaries."""
    text = re.sub(r'\r\n|\r', '\n', text)
    # Split on sentence boundaries and blank lines
    parts = re.split(r'(?<=[.!?])\s+|\n\n+', text)

    chunks = []
    current: list = []
    current_len   = 0

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if current_len + len(part) > _CHUNK_CHARS and current:
            chunks.append(" ".join(current))
            current     = [part]
            current_len = len(part)
        else:
            current.append(part)
            current_len += len(part) + 1

    if current:
        chunks.append(" ".join(current))

    return chunks


# ── Sentinel helpers ──────────────────────────────────────────────────────────

_MAX_TIMEOUT_RETRIES = 3  # all-timeout failures before writing terminal sentinel


def _file_slug(file_path: str, size: int, last_modified: str) -> str:
    return hashlib.sha256(f"{file_path}|{size}|{last_modified}".encode()).hexdigest()[:16]


def _sentinel_key(file_path: str, size: int, last_modified: str) -> str:
    return f"episodic:learning:processed:{_file_slug(file_path, size, last_modified)}"


def _timeout_count_key(file_path: str, size: int, last_modified: str) -> str:
    return f"episodic:learning:timeout-count:{_file_slug(file_path, size, last_modified)}"


def _notes_api_sentinel_key(note_id: int, modified: int) -> str:
    """MIP-format sentinel key for Notes API notes: keyed to note ID + modified timestamp."""
    raw  = f"notes-api:{note_id}|{modified}"
    slug = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"episodic:learning:processed:{slug}"


async def _has_sentinel(qdrant, key: str) -> bool:
    try:
        entry = await qdrant.retrieve_by_key(key)
        return entry is not None
    except Exception as e:
        logger.warning("LearningHarness: sentinel check failed: %s", e)
        return False


async def _write_sentinel(qdrant, sentinel_key: str, metadata: dict) -> None:
    try:
        await qdrant.store(
            collection="episodic",
            content=(
                f"Learning harness: {metadata.get('outcome', 'processed')} "
                f"— {metadata.get('file_path', '')}"
            ),
            metadata={
                "type":       "episodic",
                "_key":       sentinel_key,
                "event_type": "learning_harness_processed",
                **metadata,
            },
        )
        logger.info("LearningHarness: sentinel written — %s", sentinel_key)
    except Exception as e:
        logger.warning("LearningHarness: sentinel write failed: %s", e)


# ── Last-run summary (shared with task_scheduler) ────────────────────────────

def _write_last_run(filename: str, cycles: int, created: int,
                    updated: int, gaps_flagged: int) -> None:
    global _last_run_summary
    _last_run_summary = {
        "filename":        filename,
        "cycles":          cycles,
        "entries_created": created,
        "entries_updated": updated,
        "gaps_flagged":    gaps_flagged,
        "completed_at":    datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "LearningHarness: %s — %d cycles, %d created, %d updated, %d gaps",
        filename, cycles, created, updated, gaps_flagged,
    )


# ── Prospective memory helpers ────────────────────────────────────────────────


async def _write_failure_prospective(qdrant, failed_step: str,
                                     reason: str, filename: str) -> None:
    body = (
        f"Learning harness failed while processing '{filename}'.\n"
        f"Failed step: {failed_step}\n"
        f"Reason: {reason}\n\n"
        "Review and re-trigger via Telegram once the issue is resolved."
    )
    try:
        await qdrant.store(
            collection="prospective",
            content=body,
            metadata={
                "type":           "prospective",
                "status":         "pending_approval",
                "title":          f"Learning harness failure — {filename}",
                "target_session": "morning_briefing",
                "failed_step":    failed_step,
                "source_file":    filename,
                "ts":             datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.warning("LearningHarness: failure prospective write failed: %s", e)


# ── Doc array construction ────────────────────────────────────────────────────

async def _build_doc_array(doc_keywords: set, qdrant) -> list:
    """Build a ranked array of semantic entries relevant to the document.

    Scrolls semantic collection in pages of _SCROLL_BATCH.
    Filters client-side by keyword overlap with document.
    Resolves relational + associative context for each matched entry.
    Sorted by confidence descending (absent confidence field → 1.0).
    Capped at _MAX_DOC_ARRAY entries.
    """
    filtered    = []
    next_offset = None

    try:
        while True:
            records, next_offset = await qdrant.archive_client.scroll(
                collection_name="semantic",
                with_payload=True,
                with_vectors=False,
                limit=_SCROLL_BATCH,
                offset=next_offset,
            )
            for rec in records:
                payload = rec.payload or {}
                content = payload.get("content", "")
                if not content:
                    continue
                entry_tokens = _extract_keywords(content)
                if not (doc_keywords & entry_tokens):
                    continue
                filtered.append({
                    "_point_id":   str(rec.id),
                    "_key":        payload.get("_key", ""),
                    "content":     content,
                    "confidence":  float(payload.get("confidence", 1.0)),
                    "type":        payload.get("type", "semantic"),
                    "title":       payload.get("title", ""),
                    "relational":  [],
                    "associative": [],
                })
            if next_offset is None or len(filtered) >= _MAX_DOC_ARRAY:
                break
    except Exception as e:
        logger.warning("LearningHarness: semantic scroll failed: %s", e)
        return []

    # Sort by confidence desc, apply cap
    filtered.sort(key=lambda e: e["confidence"], reverse=True)
    filtered = filtered[:_MAX_DOC_ARRAY]

    # Resolve relational + associative context (read-only for associative)
    for entry in filtered:
        entry_key = entry["_key"]
        if not entry_key:
            continue

        try:
            rel_recs, _ = await qdrant.archive_client.scroll(
                collection_name="relational",
                scroll_filter=Filter(
                    should=[
                        FieldCondition(key="concept_a", match=MatchValue(value=entry_key)),
                        FieldCondition(key="concept_b", match=MatchValue(value=entry_key)),
                    ]
                ),
                with_payload=True,
                with_vectors=False,
                limit=10,
            )
            entry["relational"] = [r.payload for r in rel_recs if r.payload]
        except Exception:
            pass  # relational lookup is best-effort

        try:
            assoc_recs, _ = await qdrant.archive_client.scroll(
                collection_name="associative",
                scroll_filter=Filter(
                    should=[
                        FieldCondition(key="source_key", match=MatchValue(value=entry_key)),
                        FieldCondition(key="target_key", match=MatchValue(value=entry_key)),
                    ]
                ),
                with_payload=True,
                with_vectors=False,
                limit=5,
            )
            entry["associative"] = [r.payload for r in assoc_recs if r.payload]
        except Exception:
            pass  # associative lookup is best-effort, read-only

    logger.info("LearningHarness: doc_array built — %d semantic entries matched", len(filtered))
    return filtered


# ── Chunk context builder ─────────────────────────────────────────────────────

def _build_chunk_context(chunk: str, doc_array: list) -> list:
    """Return doc_array entries relevant to chunk, capped at _CONTEXT_CHARS."""
    chunk_tokens = _extract_keywords(chunk)
    context      = []
    total_chars  = 0

    for entry in doc_array:  # already sorted by confidence desc
        content      = entry.get("content", "")
        entry_tokens = _extract_keywords(content)
        if not (chunk_tokens & entry_tokens):
            continue
        summary = {
            "key":              entry.get("_key", ""),
            "point_id":         entry.get("_point_id", ""),
            "content":          content[:300],
            "confidence":       entry.get("confidence", 1.0),
            "relational_count": len(entry.get("relational", [])),
        }
        est = len(json.dumps(summary)) + 20
        if total_chars + est > _CONTEXT_CHARS:
            break
        context.append(summary)
        total_chars += est

    return context


# ── LLM prompt builder ────────────────────────────────────────────────────────

_PASS_INSTRUCTIONS = {
    "semantic": (
        "Focus on extracting factual concepts, definitions, and entities from the chunk. "
        "Propose NEW semantic entries not already captured in the memory context. "
        "For updates, only increase confidence if the chunk provides clearer evidence."
    ),
    "relational": (
        "Focus on structural relationships between concepts. "
        "Propose relational entries that link concepts from the chunk "
        "with concepts already in memory context. "
        "Use MIP key format: relational:{concept_a}:{concept_b}"
    ),
}


def _build_llm_prompt(chunk: str, pass_type: str, context: list,
                      supplemental_ctx: dict) -> str:
    context_json = json.dumps(context, indent=2) if context else "[]"
    supp = ""
    if supplemental_ctx:
        entries = [
            f"Query: {q}\nResult: {r[:400]}"
            for q, r in list(supplemental_ctx.items())[:3]
        ]
        supp = "\n\nAdditional browser context:\n" + "\n---\n".join(entries)

    return f"""You are analyzing a document to extract knowledge for memory storage.
Pass type: {pass_type}
{_PASS_INSTRUCTIONS.get(pass_type, '')}

Document chunk:
\"\"\"
{chunk[:_CHUNK_CHARS]}
\"\"\"

Existing memory context (sorted by confidence, highest first):
{context_json}
{supp}

Propose memory writes as a JSON array. Each object must have:
- "operation": "create" or "update"
- "collection": "semantic" or "relational"
- "key": MIP format key (e.g. semantic:finance:ethereum-staking-yield)
- "content": concise factual statement, 1-3 sentences
- "confidence": float 0.0-1.0
- "point_id": existing point_id from context (updates only)
- "gap": true (optional) if you cannot resolve without external context
- "gap_description": what context is missing (if gap: true)
- "requires_context": true (optional) if a browser search would help
- "context_query": search query string (if requires_context: true)

Rules:
- Only propose entries with genuine new knowledge not already in context
- Never duplicate existing context entries (check key and content)
- Relational entries must describe the link between two named concepts
- Do NOT propose writes to the associative collection
- If nothing new to add, return empty array []
- Return ONLY a valid JSON array, no other text

Proposed writes:"""


# ── Proposal parser ───────────────────────────────────────────────────────────

def _parse_proposals(raw: str) -> list:
    try:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return []
        proposals = json.loads(match.group(0))
        if not isinstance(proposals, list):
            return []
        valid = []
        for p in proposals:
            if not isinstance(p, dict):
                continue
            if not p.get("operation") or not p.get("collection") or not p.get("key"):
                continue
            if p.get("collection") not in ("semantic", "relational"):
                continue
            if p.get("operation") not in ("create", "update"):
                continue
            valid.append(p)
        return valid
    except Exception as e:
        logger.debug("LearningHarness: proposal parse failed: %s", e)
        return []


# ── Memory write dispatcher ───────────────────────────────────────────────────

async def _write_proposed(proposal: dict, qdrant, doc_array: list,
                          extra_metadata: dict = None) -> int:
    """Execute a single proposed memory write. Returns delta: 0 or 1."""
    op         = proposal.get("operation")
    collection = proposal.get("collection")
    key        = proposal.get("key", "").strip()
    content    = proposal.get("content", "").strip()
    confidence = min(1.0, max(0.0, float(proposal.get("confidence", 0.5))))

    if not content or not key or collection not in ("semantic", "relational"):
        return 0

    try:
        if op == "create":
            await qdrant.store(
                collection=collection,
                content=content,
                metadata={
                    "type":       collection,
                    "_key":       key,
                    "confidence": confidence,
                    "source":     "learning_harness",
                    "ts":         datetime.now(timezone.utc).isoformat(),
                    **(extra_metadata or {}),
                },
            )
            logger.debug("LearningHarness: created %s:%s (conf=%.2f)", collection, key, confidence)
            return 1

        if op == "update":
            point_id = proposal.get("point_id", "")
            if not point_id:
                return 0
            existing = next(
                (e for e in doc_array if e.get("_point_id") == point_id), None
            )
            existing_conf = float(existing.get("confidence", 1.0)) if existing else 1.0
            if confidence <= existing_conf:
                return 0  # no confidence improvement — skip
            await qdrant.archive_client.set_payload(
                collection_name=collection,
                payload={
                    "content":      content,
                    "confidence":   confidence,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "source":       "learning_harness",
                },
                points=[point_id],
            )
            logger.debug("LearningHarness: updated %s:%s conf %.2f→%.2f",
                         collection, key, existing_conf, confidence)
            return 1

    except Exception as e:
        logger.warning("LearningHarness: write failed [%s %s:%s]: %s", op, collection, key, e)

    return 0


async def _log_learning_timeout(cog, timeout_result: dict, cycle: int, pass_type: str) -> None:
    """Write a GPU timeout event to episodic memory (async, non-blocking)."""
    if not cog.qdrant:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        await cog.qdrant.store(
            collection="episodic",
            content=f"Learning harness GPU timeout — cycle {cycle} pass {pass_type}",
            metadata={
                "type": "episodic",
                "event_type": "learning_timeout",
                "cycle": cycle,
                "pass_type": pass_type,
                "priority": timeout_result.get("priority", "LOW"),
                "timeout_seconds": timeout_result.get("timeout_seconds", 90),
                "ts": ts,
            },
        )
    except Exception:
        pass


# ── Confidence loop ───────────────────────────────────────────────────────────

async def _run_confidence_loop(chunks: list, doc_array: list,
                               cog, qdrant, nanobot, source_url: str = "",
                               source_note_id: int | None = None) -> dict:
    """Run semantic→relational round-robin until plateau or safety cap.

    Returns: {cycles, created, updated, gaps, timeout_skips}
    GPU timeouts skip the affected chunk rather than aborting the loop.
    """
    total_created    = 0
    total_updated    = 0
    all_gaps: list   = []
    timeout_skips    = 0
    supplemental_ctx: dict = {}
    if source_note_id is not None:
        _extra_meta = {"source_note_id": source_note_id}
    elif source_url:
        _extra_meta = {"source_url": source_url}
    else:
        _extra_meta = None

    for cycle in range(1, _MAX_CYCLES + 1):
        cycle_delta = 0

        for pass_type in ("semantic", "relational"):
            for chunk in chunks:
                context = _build_chunk_context(chunk, doc_array)
                prompt  = _build_llm_prompt(chunk, pass_type, context, supplemental_ctx)

                try:
                    from adapters.inference_queue import InferenceQueue
                    import asyncio as _aq
                    result = await cog.ask_local(
                        prompt, priority=InferenceQueue.NORMAL, timeout=90.0
                    )
                    if result.get("status") == "llm_timeout":
                        logger.warning(
                            "LearningHarness: GPU timeout on cycle %d %s — retrying once",
                            cycle, pass_type,
                        )
                        _aq.create_task(_log_learning_timeout(cog, result, cycle, pass_type))
                        result = await cog.ask_local(
                            prompt, priority=InferenceQueue.NORMAL, timeout=90.0
                        )
                        if result.get("status") == "llm_timeout":
                            timeout_skips += 1
                            logger.warning(
                                "LearningHarness: GPU still busy on cycle %d (%s) — "
                                "skipping chunk (%d skipped total)",
                                cycle, pass_type, timeout_skips,
                            )
                            continue
                    raw = result.get("response", "") if isinstance(result, dict) else str(result)
                except Exception as e:
                    logger.error("LearningHarness: Ollama call failed (cycle %d %s): %s",
                                 cycle, pass_type, e)
                    raise  # bubble to _process_file for Telegram + prospective

                proposals = _parse_proposals(raw)

                # Resolve browser context for requires_context proposals
                for p in proposals:
                    if p.get("requires_context") and p.get("context_query"):
                        query = p["context_query"]
                        if query not in supplemental_ctx:
                            try:
                                nb_res   = await nanobot.run(
                                    "sovereign-browser", "search", {"query": query}
                                )
                                res_data = nb_res.get("result") or nb_res
                                snippet  = ""
                                if isinstance(res_data, list) and res_data:
                                    snippet = (res_data[0].get("content")
                                               or res_data[0].get("title", ""))
                                elif isinstance(res_data, dict):
                                    snippet = (res_data.get("content")
                                               or res_data.get("response", ""))
                                if snippet:
                                    supplemental_ctx[query] = str(snippet)[:500]
                            except Exception as be:
                                logger.warning("LearningHarness: browser context failed: %s", be)

                # Process proposals
                for p in proposals:
                    if p.get("gap"):
                        all_gaps.append({
                            "key":             p.get("key", ""),
                            "gap_description": p.get("gap_description", ""),
                        })
                        continue
                    delta = await _write_proposed(p, qdrant, doc_array, _extra_meta)
                    if delta > 0:
                        cycle_delta += 1
                        if p.get("operation") == "create":
                            total_created += 1
                        else:
                            total_updated += 1

        logger.info("LearningHarness: cycle %d — delta=%d", cycle, cycle_delta)

        # Exit on plateau (after minimum one full cycle)
        if cycle >= 2 and cycle_delta == 0:
            break

    return {
        "cycles":        cycle,
        "created":       total_created,
        "updated":       total_updated,
        "gaps":          all_gaps,
        "timeout_skips": timeout_skips,
    }


# ── Content extractor ─────────────────────────────────────────────────────────

def _extract_text(content_raw, filename: str) -> str | None:  # noqa: F821
    """Extract plain text string from nanobot fs_read result."""
    if isinstance(content_raw, str):
        return content_raw
    if isinstance(content_raw, dict):
        for d in (content_raw, content_raw.get("result") or {}):
            if isinstance(d, dict):
                for f in ("content", "text", "data", "body"):
                    if isinstance(v := d.get(f), str) and v.strip():
                        return v
    return None


# ── Single file processor ─────────────────────────────────────────────────────

async def _process_file(app_state, file_info: dict,
                        content: str | None = None,
                        sentinel_override: str | None = None,
                        source_note_id: int | None = None) -> None:
    """Full learning pipeline for one file.

    content: if provided, skip fs_read and use directly (Notes API path).
    sentinel_override: if provided, use instead of computing from file_info.
    source_note_id: if provided, inject into semantic entry and sentinel metadata.
    """
    qdrant  = app_state.qdrant
    cog     = app_state.cog
    nanobot = app_state.exec.nanobot

    file_path     = file_info.get("path") or file_info.get("file_path", "")
    file_size     = int(file_info.get("size", 0))
    last_modified = str(file_info.get("last_modified") or file_info.get("modified", ""))
    filename      = file_path.split("/")[-1] if "/" in file_path else file_path

    sentinel     = sentinel_override if sentinel_override else _sentinel_key(file_path, file_size, last_modified)
    _source_meta = {"source": "notes_api", "note_id": source_note_id} if source_note_id is not None else {}

    # ── Format gate ───────────────────────────────────────────────────────
    # Skipped when content is injected directly (Notes API) — already plain text.
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if content is None and ext and ext not in _SUPPORTED_TEXT_EXT and ext not in _URL_EXT:
        logger.info("LearningHarness: unsupported format %s — %s", ext, filename)
        await _notify_telegram(
            f"⚠️ *Learning Harness*: `{filename}` is a `{ext}` file — "
            f"no text extractor installed. "
            f"Install a conversion skill via `/install` to enable this format."
        )
        await _write_sentinel(qdrant, sentinel, {
            "outcome":   "skipped_no_extractor",
            "file_path": file_path,
            "ext":       ext,
        })
        return

    _source_url = ""  # set in .url branch; carries source URL through pipeline

    # ── Step 1: Read file ─────────────────────────────────────────────────
    try:
        if content is not None:
            document_text = content
        elif ext == ".pdf":
            nb_read = await nanobot.run(
                "pypdf", "extract_text", {"path": file_path}
            )
            raw_result = nb_read.get("result") if nb_read.get("result") is not None else nb_read
            document_text = raw_result.get("text", "") if isinstance(raw_result, dict) else ""

        elif ext == ".url":
            # Read the shortcut file to extract the target URL
            nb_read = await nanobot.run(
                "sovereign-nextcloud-fs", "fs_read", {"path": file_path}
            )
            raw_result    = nb_read.get("result") if nb_read.get("result") is not None else nb_read
            shortcut_text = _extract_text(raw_result, filename) or ""
            # fs_read may return base64-encoded content for non-text MIME types
            if isinstance(raw_result, dict) and raw_result.get("binary") and shortcut_text:
                try:
                    import base64 as _b64
                    shortcut_text = _b64.b64decode(shortcut_text).decode("utf-8", errors="replace")
                except Exception:
                    shortcut_text = ""

            # Parse URL — supports Windows .url format (URL=https://...) and bare URLs
            target_url = None
            for line in shortcut_text.splitlines():
                line = line.strip()
                if line.lower().startswith("url="):
                    target_url = line[4:].strip()
                    break
                if line.startswith("http://") or line.startswith("https://"):
                    target_url = line
                    break

            if not target_url:
                logger.warning("LearningHarness: no URL found in %s", filename)
                await _write_sentinel(qdrant, sentinel, {
                    "outcome":   "skipped_no_url",
                    "file_path": file_path,
                })
                return

            if not target_url.startswith(("http://", "https://")):
                logger.warning("LearningHarness: invalid URL in %s — %s", filename, target_url)
                await _write_sentinel(qdrant, sentinel, {
                    "outcome":    "skipped_invalid_url",
                    "file_path":  file_path,
                    "target_url": target_url,
                })
                return

            # Sentinel slug keyed to URL content, not file metadata
            url_slug     = hashlib.sha256(target_url.strip().encode()).hexdigest()[:16]
            url_sentinel = f"episodic:learning:processed:{url_slug}"
            url_failed   = f"episodic:learning:failed:{url_slug}"

            if await _has_sentinel(qdrant, url_sentinel) or await _has_sentinel(qdrant, url_failed):
                logger.info("LearningHarness: URL already processed/failed — %s", target_url)
                return

            # Override sentinel to URL-based key; set source_url for metadata
            sentinel    = url_sentinel
            _source_url = target_url

            logger.info("LearningHarness: fetching URL %s from %s", target_url, filename)
            nb_fetch = await nanobot.run(
                "sovereign-browser", "fetch",
                {"url": target_url, "extract": "text", "timeout": 60},
            )
            flat          = nb_fetch.get("result") if nb_fetch.get("result") is not None else nb_fetch
            document_text = flat.get("content", "") if isinstance(flat, dict) else ""
            page_title    = flat.get("title",   "") if isinstance(flat, dict) else ""

            if not document_text or not document_text.strip():
                logger.warning("LearningHarness: URL fetch returned empty — %s", target_url)
                await _write_sentinel(qdrant, url_failed, {
                    "outcome":    "fetch_empty",
                    "target_url": target_url,
                })
                return

            if page_title:
                filename = f"{page_title} [{filename}]"

        else:
            nb_read = await nanobot.run(
                "sovereign-nextcloud-fs", "fs_read", {"path": file_path}
            )
            raw_result    = nb_read.get("result") if nb_read.get("result") is not None else nb_read
            document_text = _extract_text(raw_result, filename)
    except Exception as e:
        if ext == ".pdf":
            op = "pdf_extract"
        elif ext == ".url":
            op = "browser_fetch"
        else:
            op = "fs_read"
        reason = f"{op} failed: {e}"
        logger.error("LearningHarness: %s — %s", filename, reason)
        await _notify_telegram(
            f"⚠️ *Learning Harness* failed [read]: {reason} — `{filename}`"
        )
        await _write_failure_prospective(qdrant, "read", reason, filename)
        return

    if not document_text or not document_text.strip():
        logger.warning("LearningHarness: empty content after read — %s", filename)
        await _write_sentinel(qdrant, sentinel, {
            "outcome":   "skipped_empty",
            "file_path": file_path,
            **_source_meta,
        })
        return

    logger.info("LearningHarness: processing %s (%d chars)", filename, len(document_text))

    # ── Step 2: Keyword extraction ────────────────────────────────────────
    doc_keywords = _extract_keywords(document_text)
    if not doc_keywords:
        logger.warning("LearningHarness: no keywords extracted — %s", filename)
        await _write_sentinel(qdrant, sentinel, {
            "outcome":   "skipped_no_keywords",
            "file_path": file_path,
            **_source_meta,
        })
        return

    # ── Step 3: Build doc_array ───────────────────────────────────────────
    try:
        doc_array = await _build_doc_array(doc_keywords, qdrant)
    except Exception as e:
        reason = f"doc_array build failed: {e}"
        logger.error("LearningHarness: %s — %s", filename, reason)
        await _notify_telegram(
            f"⚠️ *Learning Harness* failed [doc_array]: {reason} — `{filename}`"
        )
        await _write_failure_prospective(qdrant, "doc_array", reason, filename)
        return

    # ── Step 4: Chunk document ────────────────────────────────────────────
    chunks = _chunk_text(document_text)
    if not chunks:
        logger.warning("LearningHarness: no chunks produced — %s", filename)
        await _write_sentinel(qdrant, sentinel, {
            "outcome":   "skipped_no_chunks",
            "file_path": file_path,
            **_source_meta,
        })
        return

    logger.info("LearningHarness: %s — %d chunks, %d context entries",
                filename, len(chunks), len(doc_array))

    # ── Step 5: Confidence loop ───────────────────────────────────────────
    try:
        loop_result = await _run_confidence_loop(
            chunks, doc_array, cog, qdrant, nanobot,
            source_url=_source_url, source_note_id=source_note_id,
        )
    except Exception as e:
        reason = f"confidence loop error: {e}"
        logger.error("LearningHarness: %s — %s", filename, reason)
        await _notify_telegram(
            f"⚠️ *Learning Harness* failed [loop]: {reason} — `{filename}`"
        )
        await _write_failure_prospective(qdrant, "loop", reason, filename)
        await _write_sentinel(qdrant, sentinel, {
            "outcome":   "loop_failed",
            "file_path": file_path,
            "reason":    reason,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        })
        return

    cycles          = loop_result["cycles"]
    entries_created = loop_result["created"]
    entries_updated = loop_result["updated"]
    gaps            = loop_result["gaps"]
    timeout_skips   = loop_result.get("timeout_skips", 0)

    # If the GPU was busy for every chunk and nothing was learned, increment the
    # timeout counter. After _MAX_TIMEOUT_RETRIES consecutive all-timeout runs,
    # write a terminal sentinel to stop the retry loop. Delete the processed
    # sentinel from qdrant-archive episodic to re-enable the file.
    if timeout_skips > 0 and entries_created == 0 and entries_updated == 0:
        tc_key = _timeout_count_key(file_path, file_size, last_modified)
        try:
            _tc_entry = await qdrant.retrieve_by_key(tc_key)
            count = int(_tc_entry.get("count", 0)) if _tc_entry else 0
        except Exception:
            count = 0
        count += 1
        if count >= _MAX_TIMEOUT_RETRIES:
            logger.warning(
                "LearningHarness: GPU busy on %d/%d attempts — writing terminal sentinel "
                "for %s; delete key '%s' from episodic to retry",
                count, _MAX_TIMEOUT_RETRIES, filename, sentinel,
            )
            await _write_sentinel(qdrant, sentinel, {
                "outcome":   "loop_timed_out",
                "file_path": file_path,
                "attempts":  count,
            })
        else:
            try:
                await qdrant.store(
                    collection="episodic",
                    content=f"Learning harness timeout retry count — {filename}",
                    metadata={
                        "type":         "episodic",
                        "_key":         tc_key,
                        "event_type":   "learning_harness_timeout_count",
                        "filename":     filename,
                        "count":        count,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as e:
                logger.warning("LearningHarness: timeout count write failed: %s", e)
            logger.warning(
                "LearningHarness: GPU busy throughout — no sentinel written for %s; "
                "will retry (attempt %d/%d, %d chunk(s) skipped)",
                filename, count, _MAX_TIMEOUT_RETRIES, timeout_skips,
            )
        return

    # ── Step 6: Write sentinel ────────────────────────────────────────────
    file_hash = hashlib.sha256(document_text.encode()).hexdigest()[:16]
    outcome   = "partial" if timeout_skips > 0 else "positive"
    await _write_sentinel(qdrant, sentinel, {
        "outcome":          outcome,
        "file_path":        file_path,
        "file_hash":        file_hash,
        "cycles_completed": cycles,
        "entries_created":  entries_created,
        "entries_updated":  entries_updated,
        "gaps_flagged":     len(gaps),
        "timeout_skips":    timeout_skips,
        "completed_at":     datetime.now(timezone.utc).isoformat(),
        **_source_meta,
    })

    # ── Step 7: Update last-run summary ──────────────────────────────────
    _write_last_run(filename, cycles, entries_created, entries_updated, len(gaps))

    # ── Step 8: Gap notification ──────────────────────────────────────────
    if gaps:
        _gap_lines = "\n".join(
            f"  - {g.get('key','?')}: {g.get('gap_description','(no description)')}"
            for g in gaps
        )
        _gap_body = (
            f"Document '{filename}' was processed but {len(gaps)} knowledge gap(s) "
            f"were flagged requiring external context:\n{_gap_lines}\n\n"
            "Review and provide context via Telegram, or approve skipping these gaps."
        )
        try:
            await qdrant.store(
                collection="prospective",
                content=_gap_body,
                metadata={
                    "type":           "prospective",
                    "status":         "pending_approval",
                    "title":          f"Review learning gaps — {filename}",
                    "target_session": "reasoning_and_cognition",
                    "gap_count":      len(gaps),
                    "source_file":    filename,
                    "ts":             datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            logger.warning("LearningHarness: gap prospective write failed: %s", e)

    logger.info(
        "LearningHarness: completed %s — %d cycles, %d created, %d updated, %d gaps",
        filename, cycles, entries_created, entries_updated, len(gaps),
    )


# ── Download checker (exported entry point) ───────────────────────────────────

async def check_downloads(app_state, immediate: bool = False) -> None:
    """Check /downloads/ for unprocessed files and run the learning pipeline.

    immediate=True  — Telegram attachment hook; bypasses time window gate.
    immediate=False — hourly poll; processes only during _PROCESSING_HOURS (UTC).
    """
    global _run_in_progress

    if _run_in_progress:
        logger.debug("LearningHarness: run already in progress — skipping")
        return

    if not immediate:
        current_hour = datetime.now(timezone.utc).hour
        if current_hour not in _PROCESSING_HOURS:
            logger.debug(
                "LearningHarness: outside processing window (UTC %02d:xx) — deferring",
                current_hour,
            )
            return

    qdrant  = app_state.qdrant
    nanobot = app_state.exec.nanobot

    # ── List /downloads/ ─────────────────────────────────────────────────
    try:
        nb_list = await nanobot.run(
            "sovereign-nextcloud-fs", "fs_list", {"path": "/downloads/"}
        )
        result  = nb_list.get("result") if nb_list.get("result") is not None else nb_list
        if isinstance(result, dict):
            files = (result.get("files") or result.get("items")
                     or result.get("entries") or [])
        elif isinstance(result, list):
            files = result
        else:
            files = []
    except Exception as e:
        logger.warning("LearningHarness: /downloads/ list failed: %s", e)
        return

    if not files:
        logger.debug("LearningHarness: /downloads/ is empty")
        return

    # ── Find unprocessed files ────────────────────────────────────────────
    pending = []
    for f in files:
        if not isinstance(f, dict):
            continue
        path = f.get("path") or f.get("file_path") or f.get("href", "")
        if not path:
            continue
        if f.get("type") == "directory" or f.get("is_dir"):
            continue
        size          = int(f.get("size", 0))
        last_modified = str(f.get("last_modified") or f.get("modified", ""))
        sentinel      = _sentinel_key(path, size, last_modified)
        if await _has_sentinel(qdrant, sentinel):
            logger.debug("LearningHarness: already processed — %s", path.split("/")[-1])
            continue
        pending.append({"path": path, "size": size, "last_modified": last_modified})

    if not pending:
        logger.debug("LearningHarness: all /downloads/ files already processed")
        return

    logger.info("LearningHarness: %d pending file(s) found", len(pending))

    _run_in_progress = True
    try:
        for file_info in pending:
            await _process_file(app_state, file_info)
    finally:
        _run_in_progress = False


# ── Notes checker ─────────────────────────────────────────────────────────────

async def check_notes(app_state) -> None:
    """Check Nextcloud Notes API for unprocessed notes; runs in synthesis window only."""
    global _run_in_progress

    if not _NOTES_ENABLED:
        logger.debug("LearningHarness: notes ingestion disabled — skipping")
        return

    current_hour = datetime.now(timezone.utc).hour
    if current_hour not in _PROCESSING_HOURS:
        logger.debug(
            "LearningHarness: outside processing window (UTC %02d:xx) — deferring notes",
            current_hour,
        )
        return

    if _run_in_progress:
        logger.debug("LearningHarness: run already in progress — skipping check_notes")
        return

    qdrant  = app_state.qdrant
    nanobot = app_state.exec.nanobot

    # ── List all notes ────────────────────────────────────────────────────
    try:
        nb_list    = await nanobot.run("openclaw-nextcloud", "notes_list", {})
        raw        = nb_list.get("result") if nb_list.get("result") is not None else nb_list
        notes_raw  = (raw.get("notes") if isinstance(raw, dict) else None) or []
    except Exception as e:
        logger.warning("LearningHarness: notes_list failed: %s", e)
        return  # no sentinel — retry next window

    if not notes_raw:
        logger.debug("LearningHarness: no notes found")
        return

    # ── Find unprocessed notes ────────────────────────────────────────────
    _SKIP_CATEGORIES = {"Research"}

    pending = []
    for n in notes_raw:
        if not isinstance(n, dict):
            continue
        note_id  = n.get("id")
        modified = n.get("modified")
        title    = n.get("title") or f"note-{note_id}"
        category = n.get("category") or ""
        if note_id is None or modified is None:
            continue
        if category in _SKIP_CATEGORIES:
            logger.debug("LearningHarness: skipping note '%s' (category=%r)", title, category)
            continue
        sentinel = _notes_api_sentinel_key(note_id, modified)
        if await _has_sentinel(qdrant, sentinel):
            logger.debug("LearningHarness: note already processed — %s", title)
            continue
        pending.append({"id": note_id, "modified": modified, "title": title, "sentinel": sentinel})

    if not pending:
        logger.debug("LearningHarness: all notes already processed")
        return

    logger.info("LearningHarness: %d pending note(s) found", len(pending))

    _run_in_progress = True
    try:
        for note_meta in pending:
            note_id  = note_meta["id"]
            modified = note_meta["modified"]
            title    = note_meta["title"]
            sentinel = note_meta["sentinel"]

            # Read note content
            try:
                nb_read     = await nanobot.run("openclaw-nextcloud", "notes_read", {"note-id": str(note_id)})
                read_raw    = nb_read.get("result") if nb_read.get("result") is not None else nb_read
                note_content = (read_raw.get("content") if isinstance(read_raw, dict) else None) or ""
            except Exception as e:
                logger.warning(
                    "LearningHarness: notes_read failed (note %s '%s'): %s",
                    note_id, title, e,
                )
                continue  # no sentinel — retry next window

            if not note_content or not note_content.strip():
                logger.info("LearningHarness: empty note — %s", title)
                await _write_sentinel(qdrant, sentinel, {
                    "outcome":   "skipped_empty",
                    "file_path": f"/Notes/{title}",
                    "source":    "notes_api",
                    "note_id":   note_id,
                })
                continue

            if len(note_content.strip()) < 80:
                logger.info(
                    "LearningHarness: note too short (%d chars) — skipping '%s'",
                    len(note_content.strip()), title,
                )
                await _write_sentinel(qdrant, sentinel, {
                    "outcome":   "skipped_too_short",
                    "file_path": f"/Notes/{title}",
                    "source":    "notes_api",
                    "note_id":   note_id,
                })
                continue

            file_info = {
                "path":          f"/Notes/{title}",
                "size":          len(note_content),
                "last_modified": str(modified),
            }
            await _process_file(
                app_state, file_info,
                content=note_content,
                sentinel_override=sentinel,
                source_note_id=note_id,
            )
    finally:
        _run_in_progress = False


# ── Status query (learning_harness_status intent) ────────────────────────────

def get_last_run_status() -> dict:
    """Return last-run summary for the learning_harness_status intent dispatch."""
    if not _last_run_summary:
        return {
            "status":  "no_runs",
            "message": "Learning harness has not completed a run since last restart.",
            "processing_hours_utc": sorted(_PROCESSING_HOURS),
            "run_in_progress": _run_in_progress,
        }
    fn    = _last_run_summary.get("filename", "?")
    ts    = _last_run_summary.get("completed_at", "?")
    c     = _last_run_summary.get("cycles", 0)
    nc    = _last_run_summary.get("entries_created", 0)
    nu    = _last_run_summary.get("entries_updated", 0)
    gaps  = _last_run_summary.get("gaps_flagged", 0)
    return {
        "status":        "ok",
        "last_file":     fn,
        "completed_at":  ts,
        "cycles":        c,
        "created":       nc,
        "updated":       nu,
        "gaps_flagged":  gaps,
        "run_in_progress": _run_in_progress,
        "result_for_translator": (
            f"Last learned: '{fn}' at {ts[:19]} UTC — "
            f"{c} cycle(s), {nc} new, {nu} updated, {gaps} gap(s) flagged."
        ),
    }


# ── On-demand learning (/learn command) ──────────────────────────────────────

async def learn_on_demand(text: str, source_label: str, qdrant, cog, nanobot,
                          source_url: str = "") -> dict:
    """Run the learning pipeline on demand and return a Director-facing result dict.

    No sentinel written — /learn is always re-runnable.
    Capped at 6 chunks to stay within interactive latency bounds.
    """
    doc_keywords = _extract_keywords(text)
    if not doc_keywords:
        return {"status": "error", "result_for_translator": "No content keywords found — nothing to learn."}

    chunks = _chunk_text(text)[:6]
    if not chunks:
        return {"status": "error", "result_for_translator": "No text chunks produced."}

    doc_array = await _build_doc_array(doc_keywords, qdrant)

    loop_result = await _run_confidence_loop(
        chunks, doc_array, cog, qdrant, nanobot, source_url=source_url
    )

    created       = loop_result["created"]
    updated       = loop_result["updated"]
    cycles        = loop_result["cycles"]
    gaps          = loop_result["gaps"]
    timeout_skips = loop_result.get("timeout_skips", 0)

    from cognition.subjects import find_relevant_subjects, score_and_fold_subjects, derive_priority
    triage_hits = await find_relevant_subjects(qdrant, text)
    priority_label, priority_score = derive_priority(triage_hits)
    subject_folds = await score_and_fold_subjects(qdrant, cog, text, source_label, subjects=triage_hits)

    already_known = [e.get("title") or e.get("_key") or "?" for e in doc_array[:5]]

    lines = [f"Learnt from: {source_label}"]
    if already_known:
        n = len(doc_array)
        lines.append(f"\nAlready in memory ({n} related entr{'y' if n == 1 else 'ies'}):")
        lines.extend(f"• {t}" for t in already_known)
        if n > 5:
            lines.append(f"  … and {n - 5} more")
    else:
        lines.append("\nNo related entries in memory — all knowledge is new.")
    lines.append(
        f"\nWritten: {created} new, {updated} updated"
        f" | Cycles: {cycles} | Gaps: {len(gaps)} | Timeouts: {timeout_skips}"
    )
    if gaps:
        gap_text = "; ".join(g.get("gap_description") or g.get("key", "?") for g in gaps[:3])
        lines.append(f"Knowledge gaps: {gap_text}")
    if subject_folds:
        lines.append("\nSubjects updated:")
        for f in subject_folds:
            lines.append(f"• {f['subject_id']}: " + "; ".join(f["added"]))
    lines.append(f"\nPriority: {priority_label} ({len(triage_hits)} subject(s) matched)")

    return {
        "status":                "ok",
        "created":               created,
        "updated":               updated,
        "cycles":                cycles,
        "already_known_count":   len(doc_array),
        "priority":              priority_label,
        "priority_score":        priority_score,
        "subject_folds":         subject_folds,
        "result_for_translator": "\n".join(lines),
    }


# ── Hourly background loop ────────────────────────────────────────────────────

async def learning_loop(app_state) -> None:
    """Hourly asyncio loop: poll /downloads/ and Nextcloud Notes for new content."""
    await asyncio.sleep(60)   # settle delay after startup
    while True:
        try:
            await check_downloads(app_state, immediate=False)
        except Exception as e:
            logger.error("LearningHarness: unhandled error in downloads poll: %s", e)
        try:
            await check_notes(app_state)
        except Exception as e:
            logger.error("LearningHarness: unhandled error in notes poll: %s", e)
        await asyncio.sleep(3600)


def start_learning_loop(app_state) -> asyncio.Task:
    """Start the learning harness hourly poll as a background asyncio task."""
    task = asyncio.create_task(learning_loop(app_state))
    logger.info(
        "LearningHarness: hourly poll loop started — processing window UTC %s",
        sorted(_PROCESSING_HOURS),
    )
    return task
