"""Sovereign Research Harness

Multi-step research: gather (browser + Yahoo Finance + Grok) → synthesise (Ollama qwen2.5:32b)
→ save (Nextcloud Notes, MID tier, Director confirmation).

Public entry points:
  run_research_gather(cog, nanobot, qdrant, topic, user_input) → gather + synthesise atomically
  run_research_save(cog, nanobot, qdrant)                      → persist to Nextcloud Notes
  run_research_clear(qdrant)                                   → wipe checkpoint

Checkpoint (_research_harness_checkpoint=True) lives in working_memory between gather and save.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

_SYNTHESIS_TIMEOUT = 180.0       # qwen2.5:32b takes 60-120s for full report
_BROWSER_CONTENT_MAX_CHARS = 24_000  # ~6k tokens @ 4 chars/token
_CHECKPOINT_FLAG = "_research_harness_checkpoint"
_CHECKPOINT_KEY  = "research:session"


@dataclass
class GatheredSources:
    """Raw texts from the three gather helpers — passed to synthesis agents separately."""
    news:    str = ""   # browser search results (web research)
    finance: str = ""   # Yahoo Finance / CoinGecko market data
    grok:    str = ""   # Grok sentiment commentary


async def _no_data() -> tuple[str, None]:
    """Placeholder coroutine for optional gather slots."""
    return "", None


# ── Domain scope classification ───────────────────────────────────────────────

_EXCHANGE_RE = re.compile(
    r'\b(NZX|ASX|NYSE|NASDAQ|LSE|TSX|SGX)\s*:\s*([A-Z]{1,6})\b',
    re.IGNORECASE,
)
_FINANCIAL_CONTEXT = frozenset({
    "stock", "share", "shares", "equity", "etf", "fund", "ipo", "earnings",
    "revenue", "dividend", "market cap", "p/e", "valuation", "analyst",
    "invest", "investing", "investment", "company", "corporation",
    "ltd", "limited", "plc", "inc", "nzx", "asx", "nyse", "nasdaq", "listed",
})
_COMMODITY_KW = frozenset({
    "gold", "silver", "oil", "crude", "wti", "brent", "natural gas", "natgas",
    "lng", "coal", "wheat", "corn", "soybeans", "copper", "platinum", "palladium",
    "xau", "xag", "futures", "commodity", "commodities",
})
_BARE_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')


def _classify_domain_scope(topic: str) -> tuple[str, str | None]:
    """Return (domain_scope, ticker_or_None).

    Priority:
    1. Exchange-prefixed ticker (NZX:PEB, ASX:WBC)  → securities + ticker
    2. Commodity keyword present                     → commodities + None
    3. CAPS 2-5 char code + financial context words  → securities + ticker
    4. Fallback                                      → general + None
    """
    t_lower = topic.lower()

    # 1. Exchange prefix — most reliable signal
    m = _EXCHANGE_RE.search(topic)
    if m:
        return "securities", m.group(2).upper()

    # 2. Commodity keywords
    if any(kw in t_lower for kw in _COMMODITY_KW):
        return "commodities", None

    # 3. Bare ticker + financial context
    if any(kw in t_lower for kw in _FINANCIAL_CONTEXT):
        ticker_m = _BARE_TICKER_RE.search(topic)
        if ticker_m:
            return "securities", ticker_m.group(1).upper()

    return "general", None


# ── NZX dual-listing map ──────────────────────────────────────────────────────
# Maps NZX ticker / company name slug → better Yahoo Finance ticker (US or ASX).
# Yahoo Finance data for NZX listings is sparse; the US/ASX listing usually has
# richer fundamental data. Rex grows this map via memory as new companies arise.
_NZX_DUAL_LISTING: dict[str, str] = {
    # RocketLab: NZX:RKL → NASDAQ:RKLB
    "rkl":          "RKLB",
    "rkl.nz":       "RKLB",
    "rocketlab":    "RKLB",
    "rocket lab":   "RKLB",
    # A2 Milk: NZX:ATM → ASX:A2M
    "atm":          "A2M",
    "atm.nz":       "A2M",
    "a2 milk":      "A2M",
    "a2milk":       "A2M",
    # Xero: NZX:XRO → ASX:XRO
    "xro":          "XRO",
    "xro.nz":       "XRO",
    "xero":         "XRO",
    # Fisher & Paykel Healthcare: NZX:FPH → ASX:FPH
    "fph":          "FPH",
    "fph.nz":       "FPH",
    "fisher paykel": "FPH",
    "fisher & paykel": "FPH",
    # Mainfreight: NZX only, Yahoo Finance ticker
    "mft.nz":       "MFT.NZ",
    # Serko: NZX:SKO → ASX:SKO
    "sko":          "SKO",
    "sko.nz":       "SKO",
    "serko":        "SKO",
    # Vista Group: NZX:VGL → ASX:VGL
    "vgl":          "VGL",
    "vgl.nz":       "VGL",
    "vista group":  "VGL",
}


def _nzx_ticker_base(ticker: str) -> str:
    """Strip .NZ suffix → base ticker for NZX URL."""
    return ticker.upper().removesuffix(".NZ")


def _build_nzx_url(ticker: str | None, topic: str) -> str | None:
    """NZX company page URL — only for NZ-listed securities.

    Returns the NZX instruments page which has price, market cap, P/E,
    EPS, 52-week range, and a link to recent company announcements.
    """
    if not ticker:
        return None
    if ticker.upper().endswith(".NZ"):
        base = _nzx_ticker_base(ticker)
        return f"https://www.nzx.com/instruments/{base}"
    # Also check topic for NZX signals
    t_lower = topic.lower()
    if "nzx" in t_lower or "nz stock" in t_lower or "nzx:" in t_lower:
        # Try to extract bare ticker from topic for NZX URL
        m = re.search(r'\b([A-Z]{2,6})\b', topic)
        if m:
            return f"https://www.nzx.com/instruments/{m.group(1)}"
    return None


def _resolve_yahoo_ticker(ticker: str | None, topic: str) -> str | None:
    """Return the best Yahoo Finance ticker — prefers US/ASX dual listing over NZX."""
    if not ticker:
        return None
    key = ticker.lower()
    if key in _NZX_DUAL_LISTING:
        return _NZX_DUAL_LISTING[key]
    # Also try company name slug from topic
    t_lower = topic.lower()
    for name, us_ticker in _NZX_DUAL_LISTING.items():
        if name in t_lower:
            return us_ticker
    return ticker


def _build_finance_url(domain_scope: str, ticker: str | None, topic: str) -> str | None:
    """Yahoo Finance public API URL for securities or commodity futures.

    For NZX-listed stocks, applies dual-listing map to prefer US/ASX ticker
    where available (richer data). NZX.com is fetched separately via _build_nzx_url.
    """
    if domain_scope == "securities" and ticker:
        yahoo_ticker = _resolve_yahoo_ticker(ticker, topic)
        return (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}"
            "?interval=1d&range=1mo"
        )
    if domain_scope == "commodities":
        t = topic.lower()
        code = None
        if any(k in t for k in ("gold", "xau")):      code = "GC%3DF"
        elif any(k in t for k in ("silver", "xag")): code = "SI%3DF"
        elif any(k in t for k in ("oil", "crude", "wti", "brent")): code = "CL%3DF"
        elif any(k in t for k in ("gas", "natgas", "natural gas", "lng")): code = "NG%3DF"
        if code:
            return (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
                "?interval=1d&range=1mo"
            )
    return None


# ── Working memory checkpoint ─────────────────────────────────────────────────

async def _write_checkpoint(qdrant, session_id: str, current_step: str,
                             step_results: dict) -> None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        await qdrant.store(
            collection="working_memory",
            content=f"Research harness checkpoint — step: {current_step}",
            metadata={
                _CHECKPOINT_FLAG: True,
                "session_id":          session_id,
                "current_step":        current_step,
                "step_results":        step_results,
                "last_checkpoint_ts":  now,
                "_key":                _CHECKPOINT_KEY,
            },
        )
    except Exception as exc:
        logger.warning("research_harness: checkpoint write failed: %s", exc)


async def _read_checkpoint(qdrant) -> dict | None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        points, _ = await qdrant.client.scroll(
            collection_name="working_memory",
            scroll_filter=Filter(
                must=[FieldCondition(key=_CHECKPOINT_FLAG, match=MatchValue(value=True))]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        return points[0].payload if points else None
    except Exception as exc:
        logger.warning("research_harness: checkpoint read failed: %s", exc)
        return None


async def _clear_checkpoint(qdrant) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        await qdrant.client.delete(
            collection_name="working_memory",
            points_selector=Filter(
                must=[FieldCondition(key=_CHECKPOINT_FLAG, match=MatchValue(value=True))]
            ),
        )
    except Exception as exc:
        logger.warning("research_harness: checkpoint clear failed: %s", exc)


# ── Gather helpers ────────────────────────────────────────────────────────────

async def _gather_browser(nanobot, query: str) -> tuple[str, str | None]:
    try:
        nb = await nanobot.run(
            "sovereign-browser", "search",
            {"query": query, "return_format": "full"},
        )
        result = nb.get("result") if nb.get("result") is not None else nb
        parts = []
        if isinstance(result, dict):
            synth = result.get("sovereign_synthesis", {})
            if synth.get("summary"):
                parts.append(synth["summary"])
            for r in result.get("results", [])[:8]:
                title   = r.get("title", "")
                content = r.get("content") or r.get("snippet") or ""
                if title:
                    parts.append(f"### {title}\n{content[:500]}")
        text = "\n\n".join(parts)
        return text[:_BROWSER_CONTENT_MAX_CHARS], None
    except Exception as exc:
        logger.warning("research_harness: browser search failed: %s", exc)
        return "", str(exc)


async def _gather_finance(nanobot, finance_url: str) -> tuple[str, str | None]:
    try:
        nb = await nanobot.run(
            "sovereign-browser", "fetch",
            {"url": finance_url, "extract": "text", "timeout": 30},
        )
        content = (nb.get("result") or nb).get("content", "")
        return content[:2000], None
    except Exception as exc:
        logger.warning("research_harness: finance fetch failed: %s", exc)
        return "", str(exc)


async def _gather_grok(cog, topic: str) -> tuple[str, str | None]:
    try:
        prompt = (
            f"Provide a concise market sentiment summary and recent analyst commentary for: {topic}. "
            "Include: recent price action narrative, key bull/bear factors, notable news this week. "
            "Factual and concise — 3–5 sentences."
        )
        result = await cog.ask_grok(prompt, agent="research_agent")
        if result.get("error"):
            return "", result["error"]
        return result.get("response", ""), None
    except Exception as exc:
        logger.warning("research_harness: Grok enrichment failed: %s", exc)
        return "", str(exc)


# ── Synthesis ─────────────────────────────────────────────────────────────────

async def _synthesise(cog, topic: str, domain_scope: str,
                       browser_content: str, finance_data: str,
                       grok_context: str) -> dict:
    today = date.today().isoformat()
    sections = []
    if browser_content:
        sections.append(f"## Web Research\n{browser_content}")
    if finance_data:
        sections.append(f"## Market Data\n{finance_data[:1500]}")
    if grok_context:
        sections.append(f"## Market Sentiment\n{grok_context}")
    gathered = "\n\n".join(sections) or "No sources returned results."

    financial_sections = (
        "3. **Bull Case** — 2–3 supporting factors for upside\n"
        "4. **Bear Case / Risks** — 2–3 risk factors or headwinds\n"
    ) if domain_scope in ("securities", "commodities") else ""

    prompt = f"""You are a research analyst preparing a report for a Director. Today is {today}.

Research subject: {topic}
Domain: {domain_scope}

GATHERED DATA:
{gathered}

Produce a structured research report:
1. **Executive Summary** — 2–3 sentences answering the core question
2. **Key Findings** — 4–6 bullet points of substantive facts
{financial_sections}5. **Confidence** — HIGH (multiple corroborating sources), MEDIUM (single/mixed), LOW (sparse/conflicting)
6. **Sources** — list the sources used

After the report, provide this JSON block exactly:
```json
{{
  "telegram_summary": ["<bullet 1>", "<bullet 2>", "<bullet 3>"],
  "confidence": "HIGH|MEDIUM|LOW",
  "topic": "{topic}"
}}
```

Write the full report first, then the JSON. Be factual — never fabricate data not in the gathered content."""

    try:
        from adapters.inference_queue import InferenceQueue
        result = await cog.ask_local(
            prompt, priority=InferenceQueue.NORMAL, timeout=_SYNTHESIS_TIMEOUT
        )
        if result.get("status") == "llm_timeout":
            logger.error("research_harness: synthesis timed out after %.0fs", _SYNTHESIS_TIMEOUT)
            return {
                "full_report": f"# Research: {topic}\n\nSynthesis timed out.",
                "telegram_summary": [f"Research on {topic} timed out during synthesis."],
                "confidence": "LOW",
                "topic": topic,
            }
        raw = result.get("response", "") if isinstance(result, dict) else str(result)
    except Exception as _synth_exc:
        logger.error("research_harness: synthesis error: %s", _synth_exc)
        return {
            "full_report": f"# Research: {topic}\n\nSynthesis failed.",
            "telegram_summary": [f"Research on {topic} failed during synthesis."],
            "confidence": "LOW",
            "topic": topic,
        }

    # Extract trailing JSON block; remainder is the full_report
    telegram_summary = [f"Research on {topic} complete."]
    confidence = "MEDIUM"
    json_m = re.search(r'```json\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if json_m:
        try:
            meta = json.loads(json_m.group(1))
            telegram_summary = meta.get("telegram_summary", telegram_summary)
            confidence       = meta.get("confidence",        confidence)
            full_report      = raw[:json_m.start()].strip()
        except (json.JSONDecodeError, KeyError):
            full_report = raw
    else:
        full_report = raw

    return {
        "full_report":      full_report,
        "telegram_summary": telegram_summary if isinstance(telegram_summary, list) else [str(telegram_summary)],
        "confidence":       confidence,
        "topic":            topic,
    }


# ── Episodic write (non-blocking) ─────────────────────────────────────────────

async def _write_episodic(qdrant, topic: str, domain_scope: str,
                           confidence: str, sources_ok: list,
                           note_id: str | None) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        await qdrant.store(
            collection="episodic",
            content=(
                f"Research completed at {ts}: '{topic}' "
                f"(domain: {domain_scope}, confidence: {confidence}). "
                f"Sources: {sources_ok}. "
                f"Note saved: {'yes id=' + str(note_id) if note_id else 'no'}."
            ),
            metadata={
                "type":        "episodic",
                "event_type":  "research_complete",
                "topic":       topic,
                "domain_scope": domain_scope,
                "confidence":  confidence,
                "sources_ok":  sources_ok,
                "note_id":     note_id,
                "ts":          ts,
            },
        )
    except Exception as exc:
        logger.warning("research_harness: episodic write failed: %s", exc)


# ── Intent classifier ────────────────────────────────────────────────────────

async def _classify_research_intent(cog, query: str) -> dict:
    """Route research query to the appropriate synthesis path.

    Returns dict with: intent ("security"|"financial_topic"|"general"),
    security_name, ticker, slug, related.
    Falls back to "general" on any failure.
    """
    from adapters.inference_queue import InferenceQueue
    today = date.today().isoformat()
    prompt = f"""Classify this research request. Today is {today}.

Query: "{query}"

Classify as exactly one of:
- security: a named financial security (stock ticker, crypto asset, ETF, fund, company name)
- financial_topic: a financial subject without a single named security (interest rates, market sectors, macro conditions, asset class analysis)
- general: non-financial research (technology, science, news, regulatory, general knowledge)

If security, extract the name and ticker if identifiable. For crypto use the slug (e.g. "eth", "btc"). For NZ stocks include the .NZ suffix if known.

Respond with JSON only — no preamble:
{{"intent": "security|financial_topic|general", "security_name": "RocketLab", "ticker": "RKL.NZ", "slug": "rkl", "related": []}}

Use null for fields that do not apply."""

    try:
        result = await cog.ask_local(prompt, priority=InferenceQueue.HIGH, timeout=30.0)
        if result.get("status") == "llm_timeout":
            logger.warning("research_harness: classifier timed out — falling back to general")
            return {"intent": "general", "security_name": None, "ticker": None, "slug": None}
        raw = result.get("response", "")
        json_m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if json_m:
            data = json.loads(json_m.group(0))
            return {
                "intent":        data.get("intent", "general"),
                "security_name": data.get("security_name"),
                "ticker":        data.get("ticker"),
                "slug":          data.get("slug"),
                "related":       data.get("related") or [],
            }
    except Exception as exc:
        logger.warning("research_harness: classifier error: %s — falling back to general", exc)
    return {"intent": "general", "security_name": None, "ticker": None, "slug": None}


# ── Security analysis engine (6-agent adversarial pipeline) ──────────────────

async def security_analysis_engine(
    cog,
    security_name: str,
    ticker: str | None,
    gathered: GatheredSources,
    asset_spec=None,
) -> dict:
    """6-agent adversarial analysis pipeline for a named security.

    asset_spec: AssetSpec instance from portfolio harness (provides cost-basis context),
                or None for /research calls.
    All 6 agent calls are NORMAL priority; each has a 180s timeout.

    Returns: security_name, ticker, verdict, confidence, rationale, summary,
             bull_case, bear_case, tax_note, full_report, agent_outputs.
    """
    from adapters.inference_queue import InferenceQueue
    today      = date.today().isoformat()
    sec_label  = f"{security_name} ({ticker})" if ticker else security_name

    # ── Cost-basis block (injected into Agents 2 and 6 when position held) ───
    cost_basis_block      = ""
    position_context_block = "No existing position — evaluate as a potential new entry."
    if asset_spec is not None:
        balance    = asset_spec.balance
        cost_basis = asset_spec.cost_basis_nzd
        value_nzd  = asset_spec.value_nzd
        avg_buy    = cost_basis / balance if balance else 0.0
        pnl_nzd    = value_nzd - cost_basis
        pnl_pct    = (pnl_nzd / cost_basis * 100.0) if cost_basis else 0.0
        weight_pct = asset_spec.weight_pct

        target_pct  = asset_spec.extra.get("target_weight_pct")
        band_lower  = asset_spec.extra.get("rebalance_band_lower_pct")
        band_upper  = asset_spec.extra.get("rebalance_band_upper_pct")
        weight_line = f"Portfolio weight: {weight_pct:.1f}%"
        if target_pct is not None and band_lower is not None and band_upper is not None:
            weight_line += (
                f" (target: {target_pct:.0f}%, band: {band_lower:.0f}–{band_upper:.0f}%)"
            )

        cost_basis_block = (
            f"DIRECTOR POSITION CONTEXT:\n"
            f"Held: {balance:.4f} units | Cost basis: NZD {cost_basis:,.0f} "
            f"(avg: NZD {avg_buy:,.0f}/unit)\n"
            f"Current value: NZD {value_nzd:,.0f} | "
            f"Unrealised P&L: NZD {pnl_nzd:,.0f} ({pnl_pct:+.1f}%)\n"
            f"{weight_line}"
        )
        position_context_block = (
            f"Director holds {balance:.4f} units at avg cost NZD {avg_buy:,.0f}/unit. "
            f"Current value NZD {value_nzd:,.0f} ({pnl_pct:+.1f}% unrealised P&L). "
            f"{weight_line}."
        )

    # ── NZ tax note ───────────────────────────────────────────────────────────
    tax_note_default = (
        "NZ tax: crypto disposals are taxable income under the crypto asset rules. "
        "Selling triggers a taxable event — factor NZ income tax marginal rate "
        "into any SELL recommendation."
    ) if (asset_spec and getattr(asset_spec, "asset_type", "crypto") == "crypto") else (
        "NZ tax: disposals of securities are typically taxable in NZ. "
        "Consult a tax adviser on the specific treatment for this asset class."
    )
    tax_note_from_spec = (
        (asset_spec.extra.get("tax_note") or tax_note_default)
        if asset_spec else tax_note_default
    )

    verdict_options = "BUY | HOLD | SELL" if asset_spec else "BULLISH | NEUTRAL | BEARISH"

    # ── Shared helper: one queued LLM call ───────────────────────────────────
    async def _agent_call(prompt_text: str, agent_name: str) -> str:
        try:
            result = await cog.ask_local(
                prompt_text,
                priority=InferenceQueue.NORMAL,
                timeout=_SYNTHESIS_TIMEOUT,
            )
            if result.get("status") == "llm_timeout":
                logger.warning("security_analysis_engine: %s timed out", agent_name)
                return f"[{agent_name} timed out — GPU busy]"
            return result.get("response", "")
        except Exception as exc:
            logger.error("security_analysis_engine: %s error: %s", agent_name, exc)
            return f"[{agent_name} error: {exc}]"

    # ── Agent 1 — News Analyst ────────────────────────────────────────────────
    news_report = await _agent_call(f"""You are a News Analyst at a trading firm. Today is {today}.
Security: {sec_label}

GATHERED NEWS AND RECENT DEVELOPMENTS:
{gathered.news or "No news data available."}

Write a brief news analysis using these markdown headings. Use plain prose and bullet points — do NOT output JSON or code blocks.

### Key Recent Events (last 30 days)
List each notable event as a bullet. Label each: [BULLISH], [BEARISH], or [NEUTRAL]. If nothing notable, say so.

### Regulatory & Macro Factors
Bullet list of regulatory developments or macro conditions affecting this security.

### Sentiment Trend
One sentence: improving, deteriorating, or stable — and why.

### Expected Catalysts (next 30–90 days)
Bullet list of upcoming events or announcements that could move the price. If none identified, say so.

No verdict. No recommendation.""", "news_analyst")

    # ── Agent 2 — Fundamentals Analyst ───────────────────────────────────────
    position_block = f"\n{cost_basis_block}\n" if cost_basis_block else ""
    fundamentals_report = await _agent_call(f"""You are a Fundamentals Analyst at a trading firm. Today is {today}.
Security: {sec_label}
{position_block}
GATHERED FUNDAMENTAL DATA:
{gathered.finance or "No fundamental data available."}

Write a brief fundamentals analysis using these markdown headings. Use plain prose and bullet points — do NOT output JSON or code blocks.

### Valuation
Current valuation vs historical range. If no data is available, state that clearly.

### Key Metrics
Bullet list of the most important metrics (for crypto: on-chain health, TVL, staking yield; for stocks: P/E, revenue growth, margins, cash position). If no data, say so.

### Fundamental Trend
One sentence: strengthening, weakening, or stable — and why.

### Structural Risks & Opportunities
Bullet list of any structural factors that could affect performance over 12+ months.

No verdict. No recommendation.""", "fundamentals_analyst")

    # ── Agent 3 — Sentiment Analyst ───────────────────────────────────────────
    sentiment_report = await _agent_call(f"""You are a Sentiment Analyst at a trading firm. Today is {today}.
Security: {sec_label}

GATHERED SENTIMENT DATA:
{gathered.grok or "No sentiment data available."}

Write a brief sentiment analysis using these markdown headings. Use plain prose and bullet points — do NOT output JSON or code blocks.

### Overall Sentiment
One sentence rating: strongly bullish / bullish / neutral / bearish / strongly bearish.

### Key Sentiment Drivers
Bullet list of what is driving current sentiment.

### Extremes Check
Are there signs of euphoria or capitulation that historically precede reversals? Be specific.

### Sentiment vs Fundamentals
Does current sentiment align with the fundamental picture, or is there a notable divergence?

No verdict. No recommendation.""", "sentiment_analyst")

    # ── Agent 4 — Bull Researcher ─────────────────────────────────────────────
    bull_thesis = await _agent_call(f"""You are a Bull Researcher at a trading firm. Today is {today}.
Security: {sec_label}

ANALYST REPORTS:
News Analyst: {news_report}
Fundamentals Analyst: {fundamentals_report}
Sentiment Analyst: {sentiment_report}

Construct the strongest possible BULLISH case for this security.
- Cite specific evidence from the analyst reports
- Identify the 3 most compelling reasons to BUY or hold
- Acknowledge the key bear risks but explain why they are outweighed
- Be specific and evidence-based — not generic

Output a structured bull thesis. Do not hedge excessively.""", "bull_researcher")

    # ── Agent 5 — Bear Researcher ─────────────────────────────────────────────
    bear_thesis = await _agent_call(f"""You are a Bear Researcher at a trading firm. Today is {today}.
Security: {sec_label}

ANALYST REPORTS:
News Analyst: {news_report}
Fundamentals Analyst: {fundamentals_report}
Sentiment Analyst: {sentiment_report}

Construct the strongest possible BEARISH case for this security.
- Cite specific evidence from the analyst reports
- Identify the 3 most compelling reasons to SELL or reduce exposure
- Acknowledge the bull case but explain why the risks dominate
- Be specific and evidence-based — not generic

Output a structured bear thesis. Do not hedge excessively.""", "bear_researcher")

    # ── Agent 6 — Risk Manager (verdict) ─────────────────────────────────────
    risk_report = await _agent_call(f"""You are a Risk Manager at a trading firm. Today is {today}.
Security: {sec_label}

BULL CASE:
{bull_thesis}

BEAR CASE:
{bear_thesis}

{position_context_block}

NZ TAX CONTEXT: {tax_note_from_spec}

Your role: resolve the debate between the Bull and Bear researchers.

Write your assessment using these markdown headings — plain prose and bullets, do NOT output JSON yet:

### Verdict
State: {verdict_options} | Confidence: HIGH / MEDIUM / LOW
One sentence explaining the decisive factor.

### Key Factors
Three bullet points citing specific evidence that drove your verdict.

### Bull Case (summary)
One sentence distilling the strongest bull argument.

### Bear Case (summary)
One sentence distilling the strongest bear argument.

### NZ Tax Consideration
One sentence on the NZ tax implication of your verdict.

After the prose above, end with ONLY this JSON block — do not include any other JSON:
```json
{{"verdict": "{verdict_options.split(' | ')[1]}", "confidence": "HIGH", "rationale": "one sentence", "summary": ["bullet 1", "bullet 2", "bullet 3"], "bull_case": "one sentence", "bear_case": "one sentence", "tax_note": "one sentence"}}
```""", "risk_manager")

    # ── Extract JSON from Risk Manager output ─────────────────────────────────
    verdict    = "NEUTRAL" if not asset_spec else "HOLD"
    confidence = "LOW"
    rationale  = ""
    summary: list[str] = []
    bull_case  = ""
    bear_case  = ""
    tax_note   = tax_note_from_spec

    json_m = re.search(r'```json\s*(\{.*?\})\s*```', risk_report, re.DOTALL)
    if not json_m:
        json_m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', risk_report, re.DOTALL)
    if json_m:
        try:
            block_str = json_m.group(1) if '```' in json_m.group(0) else json_m.group(0)
            meta       = json.loads(block_str)
            verdict    = meta.get("verdict",    verdict)
            confidence = meta.get("confidence", confidence)
            rationale  = meta.get("rationale",  "")
            summary    = meta.get("summary",    []) or []
            bull_case  = meta.get("bull_case",  "")
            bear_case  = meta.get("bear_case",  "")
            tax_note   = meta.get("tax_note")   or tax_note_from_spec
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("security_analysis_engine: risk_manager JSON parse failed: %s", exc)

    risk_body = risk_report[:json_m.start()].strip() if json_m else risk_report
    # Fallback: if model output only JSON with no prose before it, reconstruct from extracted fields
    if not risk_body:
        risk_body = (
            f"### Verdict\n{verdict} ({confidence} confidence)\n{rationale}\n\n"
            + "\n".join(f"- {b}" for b in summary)
            + (f"\n\n**Bull:** {bull_case}" if bull_case else "")
            + (f"\n**Bear:** {bear_case}" if bear_case else "")
            + (f"\n\n*{tax_note}*" if tax_note else "")
        )

    full_report = "\n\n".join([
        f"## News Analysis\n{news_report}",
        f"## Fundamental Analysis\n{fundamentals_report}",
        f"## Sentiment Analysis\n{sentiment_report}",
        f"## Bull Case\n{bull_thesis}",
        f"## Bear Case\n{bear_thesis}",
        f"## Risk Manager Verdict\n{risk_body}",
    ])

    return {
        "security_name": security_name,
        "ticker":        ticker,
        "verdict":       verdict,
        "confidence":    confidence,
        "rationale":     rationale,
        "summary":       summary if isinstance(summary, list) else [str(summary)],
        "bull_case":     bull_case,
        "bear_case":     bear_case,
        "tax_note":      tax_note,
        "full_report":   full_report,
        "agent_outputs": {
            "news":         news_report,
            "fundamentals": fundamentals_report,
            "sentiment":    sentiment_report,
            "bull":         bull_thesis,
            "bear":         bear_thesis,
            "risk":         risk_body,
        },
    }


# ── Topic synthesis (financial_topic intent) ─────────────────────────────────

async def _synthesise_topic(cog, topic: str, gathered: GatheredSources) -> dict:
    """Single-call synthesis for financial_topic queries (interest rates, sectors, etc.)."""
    from adapters.inference_queue import InferenceQueue
    today = date.today().isoformat()
    sections = []
    if gathered.news:
        sections.append(f"## Web Research\n{gathered.news}")
    if gathered.finance:
        sections.append(f"## Market Data\n{gathered.finance[:1500]}")
    if gathered.grok:
        sections.append(f"## Market Sentiment\n{gathered.grok}")
    gathered_text = "\n\n".join(sections) or "No sources returned results."

    prompt = f"""You are a financial analyst. Today is {today}.
Topic: {topic}

RESEARCH:
{gathered_text}

Provide:
1. Current state (2 sentences)
2. Key drivers (3 bullet points)
3. NZ-specific implications
4. Outlook: improving / stable / deteriorating
5. Relevance to a crypto/property/equity investor in NZ

Output a structured research report."""

    try:
        result = await cog.ask_local(
            prompt, priority=InferenceQueue.NORMAL, timeout=_SYNTHESIS_TIMEOUT,
        )
        if result.get("status") == "llm_timeout":
            return {
                "full_report":      f"# {topic}\n\nSynthesis timed out.",
                "telegram_summary": [f"Research on {topic} timed out during synthesis."],
                "confidence":       "LOW",
                "topic":            topic,
            }
        raw = result.get("response", "") if isinstance(result, dict) else str(result)
    except Exception as exc:
        logger.error("research_harness: topic synthesis error: %s", exc)
        return {
            "full_report":      f"# {topic}\n\nSynthesis failed.",
            "telegram_summary": [f"Research on {topic} failed during synthesis."],
            "confidence":       "LOW",
            "topic":            topic,
        }

    return {
        "full_report":      raw,
        "telegram_summary": [f"Research on {topic} complete."],
        "confidence":       "MEDIUM",
        "topic":            topic,
    }


# ── Public entry points ───────────────────────────────────────────────────────

async def run_research_gather(cog, nanobot, qdrant,
                               topic: str, user_input: str = "") -> dict:
    """Gather + synthesise research on topic. Stores checkpoint.

    Routing:
      security       → 6-agent security_analysis_engine() (~8 min)
      financial_topic → _synthesise_topic() (single call)
      general        → existing _synthesise() path (unchanged)

    Returns requires_confirmation=True so the engine can present the summary
    and ask the Director whether to save the full report to Nextcloud Notes.
    """
    import uuid

    # ── Stale checkpoint guard ────────────────────────────────────────────────
    existing_cp = await _read_checkpoint(qdrant)
    if existing_cp:
        synth      = (existing_cp.get("step_results") or {}).get("synthesise", {})
        cp_topic   = synth.get("topic", "previous research")
        cp_words   = synth.get("word_count", 0)
        return {
            "status":                "stale_checkpoint",
            "requires_confirmation": False,
            "director_message": (
                f"There's a pending research result: <b>{cp_topic}</b> (~{cp_words} words).\n"
                "Say <b>save research</b> to save it to Nextcloud Notes, "
                "or <b>clear research</b> to discard and run a new gather."
            ),
        }

    session_id = str(uuid.uuid4())

    # ── Intent classification ─────────────────────────────────────────────────
    classify_result = await _classify_research_intent(cog, topic)
    intent        = classify_result.get("intent", "general")
    security_name = classify_result.get("security_name") or topic
    llm_ticker    = classify_result.get("ticker")

    logger.info("research_harness: gather topic=%r intent=%s security=%r ticker=%s",
                topic, intent, security_name, llm_ticker)

    # Deterministic scope classifier — supplements LLM ticker if missing
    domain_scope, det_ticker = _classify_domain_scope(topic)
    ticker = llm_ticker or det_ticker

    # ── Gather phase ──────────────────────────────────────────────────────────
    sources_ok: list[str]     = []
    sources_failed: list[str] = []
    year = date.today().year

    if intent == "security":
        query_a     = f"{security_name}{' ' + ticker if ticker else ''} analysis outlook {year}"
        query_b     = f"{security_name} news {year}"
        finance_url = _build_finance_url(domain_scope, ticker, topic)
        nzx_url     = _build_nzx_url(ticker, topic)
        grok_query  = f"{security_name} market sentiment and recent analyst commentary"

        # Parallel: two browser queries + Yahoo Finance + Grok + optional NZX.com
        finance_coro = _gather_finance(nanobot, finance_url) if finance_url else _no_data()
        nzx_coro     = _gather_browser(nanobot, nzx_url) if nzx_url else _no_data()
        res_a, res_b, res_f, res_g, res_nzx = await asyncio.gather(
            _gather_browser(nanobot, query_a),
            _gather_browser(nanobot, query_b),
            finance_coro,
            _gather_grok(cog, grok_query),
            nzx_coro,
            return_exceptions=True,
        )
        def _safe(r, label: str) -> tuple[str, str | None]:
            if isinstance(r, Exception):
                return "", str(r)
            return r  # already (text, err)

        res_a, res_b, res_f, res_g, res_nzx = (
            _safe(res_a, "browser_a"), _safe(res_b, "browser_b"),
            _safe(res_f, "yahoo_finance"), _safe(res_g, "grok"),
            _safe(res_nzx, "nzx"),
        )
        browser_content = "\n\n".join(t for t in [res_a[0], res_b[0]] if t)
        browser_err     = res_a[1] or res_b[1]
        # Merge Yahoo Finance + NZX.com data into a single finance block
        finance_parts   = [p for p in [res_f[0], res_nzx[0]] if p]
        finance_data    = "\n\n---\n\n".join(finance_parts)
        finance_err     = res_f[1] if not res_f[0] else None
        grok_context    = res_g[0]
        if res_nzx[0]:
            sources_ok.append("nzx")

    else:
        # financial_topic or general — single browser query
        search_query = (
            f"{topic} stock analysis recent performance"
            if domain_scope == "securities"
            else f"{topic} {year}"
        )
        browser_content, browser_err = await _gather_browser(nanobot, search_query)

        finance_data, finance_err = "", None
        finance_url = _build_finance_url(domain_scope, det_ticker, topic)
        if finance_url:
            finance_data, finance_err = await _gather_finance(nanobot, finance_url)

        grok_context = ""
        if domain_scope in ("securities", "commodities"):
            grok_context, _ = await _gather_grok(cog, topic)

    # Source accounting
    if browser_content:
        sources_ok.append("browser")
    else:
        sources_failed.append(f"browser: {browser_err or 'empty'}")
    if finance_data:
        sources_ok.append("yahoo_finance")
    elif finance_err:
        sources_failed.append(f"yahoo_finance: {finance_err}")
    if grok_context:
        sources_ok.append("grok")

    gathered = GatheredSources(news=browser_content, finance=finance_data, grok=grok_context)

    # ── Synthesis — routes by intent ──────────────────────────────────────────
    if intent == "security":
        eng = await security_analysis_engine(cog, security_name, ticker, gathered)
        synthesis_data = {
            "full_report":      eng["full_report"],
            "telegram_summary": eng["summary"] or [f"Analysis of {security_name} complete."],
            "confidence":       eng["confidence"],
            "topic":            topic,
            "verdict":          eng["verdict"],
            "rationale":        eng["rationale"],
            "bull_case":        eng["bull_case"],
            "bear_case":        eng["bear_case"],
            "security_name":    eng["security_name"],
            "ticker":           eng["ticker"],
        }
    elif intent == "financial_topic":
        t_result = await _synthesise_topic(cog, topic, gathered)
        synthesis_data = {
            "full_report":      t_result["full_report"],
            "telegram_summary": t_result["telegram_summary"],
            "confidence":       t_result["confidence"],
            "topic":            topic,
        }
    else:
        # general — existing _synthesise() path, unchanged
        synthesis_data = await _synthesise(
            cog, topic, domain_scope, browser_content, finance_data, grok_context,
        )

    # ── Checkpoint ────────────────────────────────────────────────────────────
    today      = date.today().isoformat()
    word_count = len((synthesis_data.get("full_report") or "").split())

    await _write_checkpoint(qdrant, session_id, "synthesised", {
        "gather": {
            "topic":          topic,
            "intent":         intent,
            "security_name":  security_name if intent == "security" else None,
            "ticker":         ticker,
            "domain_scope":   domain_scope,
            "sources_ok":     sources_ok,
            "sources_failed": sources_failed,
            "ts":             datetime.now(timezone.utc).isoformat(),
        },
        "synthesise": {
            **synthesis_data,
            "word_count": word_count,
            "date":       today,
            "ts":         datetime.now(timezone.utc).isoformat(),
        },
    })

    status = "ok" if sources_ok else "error"
    if sources_ok and sources_failed:
        status = "partial"

    sum_lines = "\n".join(f"• {b}" for b in (synthesis_data.get("telegram_summary") or []))
    if synthesis_data.get("confidence"):
        sum_lines += f"\nConfidence: {synthesis_data['confidence']}"
    if intent == "security" and synthesis_data.get("verdict"):
        sum_lines = (
            f"Verdict: <b>{synthesis_data['verdict']}</b> — {synthesis_data.get('rationale', '')}\n"
            + sum_lines
        )

    conf_ask = (
        f"Research complete. Save full report to Nextcloud Notes? "
        f"({synthesis_data.get('topic', topic)}, ~{word_count} words)"
    )

    return {
        "status":                status,
        "requires_confirmation": True,
        "telegram_summary":      synthesis_data.get("telegram_summary") or [],
        "confidence":            synthesis_data.get("confidence", ""),
        "topic":                 synthesis_data.get("topic", topic),
        "intent":                intent,
        "word_count":            word_count,
        "sources_ok":            sources_ok,
        "sources_failed":        sources_failed,
        "director_message":      f"{sum_lines}\n\n{conf_ask}",
    }


async def run_research_save(cog, nanobot, qdrant) -> dict:
    """Read synthesised report from checkpoint and save to Nextcloud Notes."""
    cp = await _read_checkpoint(qdrant)
    if not cp:
        return {"status": "error", "error": "No research checkpoint — run research first"}

    synth = cp.get("step_results", {}).get("synthesise", {})
    if not synth.get("full_report"):
        return {"status": "error", "error": "Checkpoint has no report — run research first"}

    topic       = synth.get("topic", "Research")
    report_date = synth.get("date", date.today().isoformat())
    note_title  = f"Research: {topic} ({report_date})"

    nb = await nanobot.run("openclaw-nextcloud", "notes_create", {
        "title":    note_title,
        "content":  synth["full_report"],
        "category": "Research",
    })

    note_id = None
    result  = nb.get("result") if nb.get("result") is not None else nb
    if isinstance(result, dict):
        note_id = result.get("id") or result.get("note_id")

    gather_info = cp.get("step_results", {}).get("gather", {})
    asyncio.create_task(_write_episodic(
        qdrant, topic,
        gather_info.get("domain_scope", "general"),
        synth.get("confidence", ""),
        gather_info.get("sources_ok", []),
        str(note_id) if note_id else None,
    ))
    await _clear_checkpoint(qdrant)

    id_line = f"\nNote ID: {note_id}" if note_id else ""
    return {
        "status":           "ok",
        "note_id":          note_id,
        "note_title":       note_title,
        "telegram_summary": synth.get("telegram_summary", []),
        "confidence":       synth.get("confidence", ""),
        "director_message": f"Research saved to Nextcloud Notes.\nTitle: {note_title}{id_line}",
    }


async def run_research_clear(qdrant) -> dict:
    """Clear active research checkpoint from working_memory."""
    await _clear_checkpoint(qdrant)
    return {"status": "ok", "message": "Research harness cleared."}
