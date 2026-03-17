"""
Security helpers:
- Shared secret authentication (X-API-Key header)
- UNTRUSTED_CONTENT wrapping before Ollama calls
- Sanitisation of LLM-enriched output before returning to Sovereign
"""
import re
import urllib.parse
from fastapi import HTTPException, Request
import config

# Patterns that suggest prompt injection in result snippets
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/INST\]", re.IGNORECASE),
    re.compile(r"###\s*(human|assistant|system)\s*:", re.IGNORECASE),
]


def verify_secret(request: Request):
    """FastAPI dependency — raises 401 if shared secret missing/wrong."""
    if not config.SHARED_SECRET:
        # Unconfigured — block all traffic
        raise HTTPException(status_code=503, detail="Service not configured")
    key = request.headers.get("X-API-Key", "")
    if key != config.SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def wrap_untrusted(raw_results: list[dict]) -> str:
    """Wrap raw search result data in UNTRUSTED_CONTENT tags for safe Ollama ingestion."""
    import json
    content = json.dumps(raw_results, ensure_ascii=False, indent=2)
    return (
        "UNTRUSTED_CONTENT_BEGIN\n"
        + content
        + "\nUNTRUSTED_CONTENT_END"
    )


def _sanitise_text(text: str, max_len: int) -> str:
    """Strip HTML tags, truncate, flag injection attempts."""
    if not isinstance(text, str):
        return ""
    # Strip HTML
    clean = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    # Truncate
    clean = clean[:max_len]
    # Flag injection patterns — replace with [REDACTED]
    for pat in _INJECTION_PATTERNS:
        clean = pat.sub("[REDACTED]", clean)
    return clean


def _sanitise_url(url: str) -> str:
    """Validate URL — must be http/https with a real hostname."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ""
        if not parsed.netloc or len(parsed.netloc) < 3:
            return ""
        return url[:512]
    except Exception:
        return ""


def sanitise_results(results: list[dict]) -> list[dict]:
    """Sanitise raw search results before they reach Sovereign."""
    clean = []
    for r in results:
        clean.append({
            "title": _sanitise_text(r.get("title", ""), config.MAX_TITLE_LEN),
            "url": _sanitise_url(r.get("url", r.get("href", ""))),
            "snippet": _sanitise_text(r.get("snippet", r.get("body", "")), config.MAX_SNIPPET_LEN),
            "source": str(r.get("source", ""))[:20],
            "rank": int(r.get("rank", 0)),
        })
    return [r for r in clean if r["url"]]


def sanitise_enrichment(enriched: dict) -> dict:
    """Sanitise LLM enrichment output — check all string fields."""
    def clean_str(v, max_len=500):
        if not isinstance(v, str):
            return ""
        return _sanitise_text(v, max_len)

    def clean_list(lst, max_len=200) -> list:
        if not isinstance(lst, list):
            return []
        return [clean_str(item, max_len) for item in lst if item][:10]

    def clean_float(v, default=0.5) -> float:
        try:
            f = float(v)
            return round(max(0.0, min(1.0, f)), 3)
        except Exception:
            return default

    def clean_sentinel(v, options, default) -> str:
        return v if isinstance(v, str) and v in options else default

    # query_intelligence
    qi = enriched.get("query_intelligence", {})
    enriched["query_intelligence"] = {
        "original": qi.get("original", ""),
        "interpreted": clean_str(qi.get("interpreted", ""), 300),
        "confidence": clean_float(qi.get("confidence", 0.7)),
        "type": clean_sentinel(qi.get("type"), ["factual", "research", "current_events", "navigational", "transactional"], "research"),
        "temporal_sensitivity": clean_sentinel(qi.get("temporal_sensitivity"), ["low", "medium", "high"], "medium"),
    }

    # sovereign_synthesis
    ss = enriched.get("sovereign_synthesis", {})
    enriched["sovereign_synthesis"] = {
        "summary": clean_str(ss.get("summary", ""), 1000),
        "confidence": clean_float(ss.get("confidence", 0.5)),
        "consensus": clean_str(ss.get("consensus", ""), 500),
        "contradiction": clean_str(ss.get("contradiction") or "", 300) or None,
    }

    # bias_analysis
    ba = enriched.get("bias_analysis", {})
    enriched["bias_analysis"] = {
        "bias_flags": clean_list(ba.get("bias_flags", [])),
        "narrative_warnings": clean_list(ba.get("narrative_warnings", [])),
        "sentiment": clean_sentinel(ba.get("sentiment"), ["neutral", "positive", "negative"], "neutral"),
    }

    # structured_entities
    se = enriched.get("structured_entities", {})
    enriched["structured_entities"] = {
        "prices": clean_list(se.get("prices", [])),
        "dates": clean_list(se.get("dates", [])),
        "organisations": clean_list(se.get("organisations", [])),
        "claims": clean_list(se.get("claims", [])),
    }

    # ai_navigation
    an = enriched.get("ai_navigation", {})
    enriched["ai_navigation"] = {
        "follow_up_queries": clean_list(an.get("follow_up_queries", []), 150),
        "related_queries": clean_list(an.get("related_queries", []), 150),
        "suggested_next_action": clean_str(an.get("suggested_next_action", "Review search results"), 200),
    }

    # quality_metrics
    qm = enriched.get("quality_metrics", {})
    enriched["quality_metrics"] = {
        "result_quality_score": clean_float(qm.get("result_quality_score", 0.5)),
        "evidence_strength": clean_sentinel(qm.get("evidence_strength"), ["weak", "moderate", "strong"], "moderate"),
        "data_completeness": clean_float(qm.get("data_completeness", 0.5)),
    }

    return enriched
