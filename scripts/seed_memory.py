#!/usr/bin/env python3
"""Seed sovereign semantic memory from key architecture documents.

Chunks source docs into ~200-word sections and POSTs each to the
semantic collection via the /query API. Run once to bootstrap confidence.

Usage (from inside the container):
    python3 /opt/sovereign/scripts/seed_memory.py [--dry-run]

Chunking: TokenChunker(WordTokenizer, chunk_size=200, chunk_overlap=30).
WordTokenizer counts on split(" ") — single-space only.  Single-newline
separators (markdown tables, code blocks) count as part of the preceding
token, so real word counts may run ~10% over TARGET_WORDS on dense
markdown sections.  Still well within nomic-embed-text's 8192-token limit.
30-word overlap (~15%) prevents facts/sentences from being severed at a
chunk boundary and losing meaning in both resulting embeddings.
"""
import json
import sys
import time
import urllib.request

from chonkie import TokenChunker, WordTokenizer

SOVEREIGN_URL = "http://localhost:8000"
TARGET_WORDS  = 200   # approx words per chunk (passed as chunk_size)
CHUNK_OVERLAP = 30    # ~15% overlap — stops facts being severed at a chunk boundary
PAUSE_S       = 0.5   # brief pause between embeds to avoid hammering Ollama

CHUNKER = TokenChunker(tokenizer=WordTokenizer(), chunk_size=TARGET_WORDS, chunk_overlap=CHUNK_OVERLAP)

SOURCES = [
    {
        "path": "/docker/sovereign/CLAUDE.md",
        "label": "Architecture/CLAUDE.md",
    },
    {
        "path": "/home/sovereign/docs/Sovereign-cognition.md",
        "label": "Architecture/Sovereign-cognition.md",
    },
    {
        "path": "/home/openclaw/workspaces/agents/CEO/DIARIES/as-built.md",
        "label": "History/as-built.md",
    },
    {
        "path": "/home/sovereign/personas/sovereign_security_architecture.md",
        "label": "Security/sovereign_security_architecture.md",
    },
    {
        "path": "/home/sovereign/personas/security_architecturev2.md",
        "label": "Security/security_architecturev2.md",
    },
]

DRY_RUN = "--dry-run" in sys.argv


def store_chunk(chunk: str) -> dict:
    body = json.dumps({
        "action": {
            "domain": "memory",
            "operation": "store",
            "collection": "semantic",
            "type": "semantic",
        },
        "tier": "LOW",
        "prompt": chunk,
    }).encode()
    req = urllib.request.Request(
        f"{SOVEREIGN_URL}/query",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    print(f"Sovereign memory seeder {'[DRY RUN] ' if DRY_RUN else ''}— target ~{TARGET_WORDS} words/chunk\n")

    # Health check
    try:
        with urllib.request.urlopen(f"{SOVEREIGN_URL}/health", timeout=5) as r:
            r.read()
    except Exception as e:
        print(f"ERROR: sovereign-core not reachable at {SOVEREIGN_URL}: {e}")
        sys.exit(1)

    total_chunks = 0
    total_stored = 0

    for source in SOURCES:
        path = source["path"]
        label = source["label"]

        try:
            with open(path) as f:
                text = f.read()
        except FileNotFoundError:
            print(f"  SKIP {label} — file not found: {path}")
            continue

        chunks = [f"[{label}]\n{c.text}" for c in CHUNKER.chunk(text)]
        print(f"  {label}: {len(chunks)} chunks from {len(text.split())} words")
        total_chunks += len(chunks)

        for i, chunk in enumerate(chunks, 1):
            preview = chunk.splitlines()[1][:60] if len(chunk.splitlines()) > 1 else chunk[:60]
            if DRY_RUN:
                print(f"    [{i}/{len(chunks)}] DRY: {preview}...")
                continue

            try:
                result = store_chunk(chunk)
                pid = result.get("point_id", "?")
                print(f"    [{i}/{len(chunks)}] stored {pid[:8]}… — {preview}...")
                total_stored += 1
            except Exception as e:
                print(f"    [{i}/{len(chunks)}] ERROR: {e} — {preview}...")

            time.sleep(PAUSE_S)

        print()

    print(f"Done. {total_stored}/{total_chunks} chunks stored to semantic collection.")
    if total_stored > 0:
        print("Restart sovereign-core to trigger startup_load, or query immediately — semantic is live.")


if __name__ == "__main__":
    main()
