"""
a2a-browser — AI-native pay-per-search browser service (MVP / internal use).
Endpoints: POST /search, POST /fetch, GET /health, GET /metrics
Auth: X-API-Key shared secret (see config.SHARED_SECRET)
"""
import asyncio
import hashlib
import json
import urllib.request
from contextlib import asynccontextmanager
from time import monotonic
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

import config
import metrics as met
import security
from enrichment import ollama as enrich_ollama
from schema import (
    AiNavigation, BiasAnalysis, EpistemicMetadata, EvidenceItem,
    FetchRequest, FetchResponse,
    QualityMetrics, QueryIntelligence, SearchRequest, SearchResponse,
    SearchResult, SovereignSynthesis, StructuredEntities, TestModeMetrics,
)
from search.router import SearchRouter


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Metrics
    store = met.init()
    app.state.metrics = store

    # Playwright browser (singleton — new context per request for isolation)
    app.state.playwright = None
    app.state.browser = None
    try:
        from playwright.async_api import async_playwright
        app.state._pw_ctx = async_playwright()
        pw = await app.state._pw_ctx.__aenter__()
        app.state.playwright = pw
        app.state.browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
    except Exception as e:
        # Playwright not critical — library-based DDG still works
        app.state.browser = None

    # Search router
    router = SearchRouter(browser=app.state.browser)
    app.state.search_router = router

    yield

    # Shutdown
    if app.state.browser:
        await app.state.browser.close()
    if app.state.playwright:
        await app.state._pw_ctx.__aexit__(None, None, None)


app = FastAPI(title="a2a-browser", version="1.0.0", lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _query_type(query: str) -> str:
    q = query.lower()
    if any(q.startswith(p) for p in ("how to", "how do", "what is", "what are", "why", "explain")):
        return "factual"
    if any(w in q for w in ("news", "latest", "today", "2024", "2025", "2026", "recent", "update")):
        return "current_events"
    if any(w in q for w in ("buy", "price", "cost", "cheap", "discount", "shop")):
        return "transactional"
    if any(w in q for w in ("vs", "versus", "compare", "difference", "better", "best")):
        return "research"
    return "research"


def _temporal_sensitivity(query: str) -> str:
    q = query.lower()
    if any(w in q for w in ("today", "now", "live", "real-time", "current", "latest", "breaking")):
        return "high"
    if any(w in q for w in ("this week", "this month", "recent", "new", "2026", "2025")):
        return "medium"
    return "low"


def _diversity_score(results: list[dict]) -> float:
    domains = set()
    for r in results:
        url = r.get("url", "")
        try:
            import urllib.parse
            domains.add(urllib.parse.urlparse(url).netloc)
        except Exception:
            pass
    if not results:
        return 0.0
    return round(min(1.0, len(domains) / len(results)), 3)


def _result_sha256(results: list[dict]) -> str:
    payload = json.dumps(results, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _get_outbound_ip() -> Optional[str]:
    try:
        loop = asyncio.get_event_loop()
        def _fetch():
            with urllib.request.urlopen("https://api.ipify.org", timeout=4) as r:
                return r.read().decode().strip()
        return await loop.run_in_executor(None, _fetch)
    except Exception:
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "backends": config.enabled_backends(),
        "playwright": app.state.browser is not None,
    }


@app.get("/metrics")
async def get_metrics(_: None = Depends(security.verify_secret)):
    return await app.state.metrics.snapshot()


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, _: None = Depends(security.verify_secret)):
    t_start = monotonic()
    backend_used = "unknown"
    success = False

    try:
        # ── Stage 1: Search ───────────────────────────────────────────────
        t_search = monotonic()
        router: SearchRouter = app.state.search_router
        raw_results, backend_used = await router.search(req.query, req.locale)
        search_ms = (monotonic() - t_search) * 1000

        # Sanitise raw results before anything touches them
        clean_results = security.sanitise_results(
            [{"title": r.get("title", ""), "url": r.get("url", r.get("href", "")),
              "snippet": r.get("snippet", r.get("body", "")), "source": backend_used, "rank": i + 1}
             for i, r in enumerate(raw_results)]
        )

        # ── Stage 2: Enrich via Ollama ────────────────────────────────────
        t_enrich = monotonic()
        untrusted = security.wrap_untrusted(
            [{"title": r["title"], "url": r["url"], "snippet": r["snippet"]} for r in clean_results]
        )
        enriched_raw = await enrich_ollama.enrich(req.query, clean_results, untrusted)
        enrich_ms = (monotonic() - t_enrich) * 1000

        # ── Stage 3: Sanitise enrichment output ───────────────────────────
        t_san = monotonic()
        enriched = security.sanitise_enrichment(enriched_raw)
        san_ms = (monotonic() - t_san) * 1000

        # ── Assemble deterministic fields ─────────────────────────────────
        total_ms = (monotonic() - t_start) * 1000

        qi_base = _query_type(req.query)
        ts_base = _temporal_sensitivity(req.query)
        qi_enrich = enriched.get("query_intelligence", {})

        query_intelligence = QueryIntelligence(
            original=req.query,
            interpreted=qi_enrich.get("interpreted") or req.query,
            confidence=float(qi_enrich.get("confidence", 0.7)),
            type=qi_enrich.get("type") or qi_base,
            temporal_sensitivity=qi_enrich.get("temporal_sensitivity") or ts_base,
        )

        ss = enriched.get("sovereign_synthesis", {})
        sovereign_synthesis = SovereignSynthesis(
            summary=ss.get("summary", ""),
            confidence=float(ss.get("confidence", 0.5)),
            consensus=ss.get("consensus", ""),
            contradiction=ss.get("contradiction") or None,
        )

        epistemic_metadata = EpistemicMetadata(
            freshness="unknown",
            source_count=len(clean_results),
            diversity_score=_diversity_score(clean_results),
            cross_verification=len(clean_results) > 3,
        )

        ba = enriched.get("bias_analysis", {})
        bias_analysis = BiasAnalysis(
            bias_flags=ba.get("bias_flags", []),
            narrative_warnings=ba.get("narrative_warnings", []),
            sentiment=ba.get("sentiment", "neutral"),
        )

        se = enriched.get("structured_entities", {})
        structured_entities = StructuredEntities(
            prices=se.get("prices", []),
            dates=se.get("dates", []),
            organisations=se.get("organisations", []),
            claims=se.get("claims", []),
        )

        an = enriched.get("ai_navigation", {})
        ai_navigation = AiNavigation(
            follow_up_queries=an.get("follow_up_queries", []),
            related_queries=an.get("related_queries", []),
            suggested_next_action=an.get("suggested_next_action", "Review results"),
        )

        qm = enriched.get("quality_metrics", {})
        quality_metrics = QualityMetrics(
            result_quality_score=min(1.0, len(clean_results) / config.MAX_RESULTS),
            evidence_strength=qm.get("evidence_strength", "moderate"),
            data_completeness=float(qm.get("data_completeness", 0.5)),
        )

        results = [SearchResult(**r) for r in clean_results]

        evidence = []
        for ev in enriched.get("evidence", [])[:5]:
            if isinstance(ev, dict) and ev.get("claim") and ev.get("source_url"):
                evidence.append(EvidenceItem(
                    claim=str(ev["claim"])[:300],
                    source_url=security._sanitise_url(str(ev.get("source_url", ""))),
                    confidence=float(ev.get("confidence", 0.5)),
                ))

        sha = _result_sha256([r.model_dump() for r in results])

        # ── Test mode metrics ─────────────────────────────────────────────
        test_metrics = None
        if req.test_mode:
            outbound_ip = await _get_outbound_ip()
            test_metrics = TestModeMetrics(
                outbound_ip=outbound_ip,
                backend_used=backend_used,
                stage_latencies_ms={
                    "search_ms": round(search_ms, 1),
                    "enrich_ms": round(enrich_ms, 1),
                    "sanitize_ms": round(san_ms, 1),
                    "total_ms": round(total_ms, 1),
                },
            )

        success = True
        return SearchResponse(
            query_intelligence=query_intelligence,
            sovereign_synthesis=sovereign_synthesis,
            epistemic_metadata=epistemic_metadata,
            bias_analysis=bias_analysis,
            structured_entities=structured_entities,
            ai_navigation=ai_navigation,
            quality_metrics=quality_metrics,
            results=results,
            evidence=evidence,
            result_sha256=sha,
            backend_used=backend_used,
            test_mode_metrics=test_metrics,
        )

    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")
    finally:
        latency_ms = (monotonic() - t_start) * 1000
        await app.state.metrics.record(backend_used, success, latency_ms)


_FETCH_CONTENT_CAP = 50_000


@app.post("/fetch", response_model=FetchResponse)
async def fetch(req: FetchRequest, _: None = Depends(security.verify_secret)):
    if not app.state.browser:
        raise HTTPException(status_code=503, detail="Playwright unavailable")

    url = security._sanitise_url(req.url)
    if not url:
        raise HTTPException(status_code=400, detail="Invalid or disallowed URL")

    extract = req.extract if req.extract in ("text", "html") else "text"

    context = await app.state.browser.new_context()
    try:
        page = await context.new_page()
        await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        title = await page.title()
        if extract == "html":
            content = await page.content()
        else:
            content = await page.inner_text("body")
        content = content[:_FETCH_CONTENT_CAP]
    finally:
        await context.close()

    sha = hashlib.sha256(content.encode()).hexdigest()
    return FetchResponse(
        url=url,
        title=title[:200],
        content=content,
        content_length=len(content),
        fetch_sha256=sha,
    )
