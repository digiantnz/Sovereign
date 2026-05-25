"""Portfolio Analysis Harness

Per-category deep financial analysis: gather (parallel HTTP) → synthesise (sequential GPU)
→ write-back (Nextcloud ledger) → Telegram push.

Storage: ledger files at /portfolios/{slug}.md in Nextcloud root, accessed via
openclaw-nextcloud nanobot skill (files_read / files_write).

Public entry points:
  run_portfolio_analysis(cog, nanobot, qdrant, category, sov_wallet_url) → spawn background task + return ack
  run_portfolio_analysis_save(cog, nanobot, qdrant)                      → persist checkpoint to Nextcloud Notes
  run_portfolio_analysis_clear(qdrant)                                   → wipe checkpoint
"""

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from monitoring.research_harness import (
    _gather_browser,
    _gather_finance,
    _gather_grok,
    _classify_domain_scope,
    _build_finance_url,
    security_analysis_engine,
    GatheredSources,
)

logger = logging.getLogger(__name__)


async def _notify_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("PortfolioHarness: Telegram credentials missing — skipping notification")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning("PortfolioHarness: Telegram notification failed: %s", e)

_SYNTHESIS_TIMEOUT = 180.0
_CHECKPOINT_FLAG   = "_portfolio_analysis_checkpoint"
_CHECKPOINT_KEY    = "portfolio_analysis:session"
_RUNNING_FLAG      = "_portfolio_analysis_running"
_RUNNING_KEY       = "portfolio_analysis:running"

# Normalise short user-facing category names to file slugs
_CATEGORY_ALIAS: dict[str, str] = {
    "retirement":      "retirement-fund",
    "retirement fund": "retirement-fund",
    "kiwisaver":       "retirement-fund",
    "property":        "property",
    "properties":      "property",
    "crypto":          "crypto",
}

# Harness-maintained fields — only these are patched on write-back
_HARNESS_FIELDS = {
    "current_value_nzd",
    "unrealised_pnl_nzd",
    "unrealised_pnl_pct",
    "unrealised_equity_nzd",
    "gross_yield_pct",
    "net_yield_pct",
    "last_analysis",
    "last_verdict",
    "last_confidence",
    "last_verdict_rationale",
}


# ── AssetSpec ─────────────────────────────────────────────────────────────────

@dataclass
class AssetSpec:
    slug:             str
    display_name:     str
    asset_type:       str           # "crypto" | "fund" | "property"
    balance:          float
    value_nzd:        float
    cost_basis_nzd:   float
    weight_pct:       float
    purchase_history: list = field(default_factory=list)
    extra:            dict = field(default_factory=dict)


# ── Working memory checkpoint ─────────────────────────────────────────────────

async def _write_checkpoint(qdrant, session_id: str, category: str,
                             step_results: dict) -> None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        await qdrant.store(
            collection="working_memory",
            content=f"Portfolio analysis checkpoint — category: {category}",
            metadata={
                _CHECKPOINT_FLAG:     True,
                "session_id":         session_id,
                "category":           category,
                "current_step":       "analysed",
                "step_results":       step_results,
                "last_checkpoint_ts": now,
                "_key":               _CHECKPOINT_KEY,
            },
        )
    except Exception as exc:
        logger.warning("portfolio_harness: checkpoint write failed: %s", exc)


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
        logger.warning("portfolio_harness: checkpoint read failed: %s", exc)
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
        logger.warning("portfolio_harness: checkpoint clear failed: %s", exc)


# ── In-progress WM flag helpers ───────────────────────────────────────────────

async def _set_running(qdrant, slug: str) -> None:
    await qdrant.store(
        collection="working_memory",
        content=f"Portfolio analysis running: {slug}",
        metadata={
            "type": "working",
            _RUNNING_FLAG: True,
            "category": slug,
            "started": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _clear_running(qdrant) -> None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        f = Filter(must=[FieldCondition(key=_RUNNING_FLAG, match=MatchValue(value=True))])
        await qdrant.client.delete(collection_name="working_memory", points_selector=f)
    except Exception as e:
        logger.warning("portfolio_harness: clear_running failed: %s", e)


async def _get_running(qdrant) -> dict | None:
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        f = Filter(must=[FieldCondition(key=_RUNNING_FLAG, match=MatchValue(value=True))])
        hits, _ = await qdrant.client.scroll(
            collection_name="working_memory", scroll_filter=f,
            limit=1, with_payload=True, with_vectors=False,
        )
        return hits[0].payload if hits else None
    except Exception:
        return None


# ── Ledger access (Nextcloud) ─────────────────────────────────────────────────

def _resolve_slug(category: str) -> str:
    """Map user-typed category (e.g. 'retirement') to file slug ('retirement-fund')."""
    return _CATEGORY_ALIAS.get(category.lower().strip(), category.lower().strip())


async def _fetch_ledger(nanobot, category_slug: str) -> str | None:
    """Fetch ledger content from Nextcloud. Returns content string or None."""
    result = await nanobot.run("openclaw-nextcloud", "files_read", {
        "path": f"/portfolios/{category_slug}.md"
    })
    nb = result.get("result") if result.get("result") is not None else result
    if nb.get("status") == "error" or not nb.get("content"):
        return None
    return nb["content"]


def _parse_ledger(content: str) -> list[AssetSpec]:
    """Parse YAML blocks from ledger content string into AssetSpec list."""
    import yaml

    specs: list[AssetSpec] = []
    seen_slugs: set[str] = set()
    # Extract all ```yaml ... ``` fenced blocks
    for m in re.finditer(r'```yaml\s*\n(.*?)\n```', content, re.DOTALL):
        raw_block = m.group(1)
        try:
            block = yaml.safe_load(raw_block)
        except Exception as exc:
            logger.warning("portfolio_harness: YAML parse error (skipping block): %s", exc)
            continue
        if not isinstance(block, dict):
            continue
        slug     = block.get("slug")
        atype    = block.get("asset_type")
        if not slug or not atype:
            logger.warning("portfolio_harness: block missing slug/asset_type (skipping)")
            continue
        if slug in seen_slugs:
            logger.warning("portfolio_harness: duplicate slug '%s' — first wins", slug)
            continue
        seen_slugs.add(slug)

        if (block.get("extra") or {}).get("disposition") == "disposal_candidate":
            logger.info("portfolio_harness: skipping disposal candidate '%s'", slug)
            continue

        display_name = block.get("display_name", slug)
        purchases    = block.get("purchases", []) or []
        contribs     = block.get("contributions", []) or []
        purchase_history = purchases + contribs

        # Cost basis
        if atype == "fund":
            cost_basis = float(block.get("total_contributed_nzd", 0) or 0)
        else:
            cost_basis = float(block.get("total_cost_basis_nzd", 0) or 0)

        # Balance / quantity
        if atype == "crypto":
            balance = float(block.get("total_amount", 0) or 0)
        elif atype == "property":
            balance = 1.0
        else:
            balance = float(block.get("total_amount", 0) or 0)

        # Current value (may be overwritten by live data for crypto)
        value_nzd = float(block.get("current_value_nzd", 0) or 0)

        extra = {k: v for k, v in block.items()
                 if k not in ("slug", "display_name", "asset_type",
                              "purchases", "contributions", "total_amount",
                              "total_cost_basis_nzd", "total_contributed_nzd",
                              "current_value_nzd") and k not in _HARNESS_FIELDS}
        specs.append(AssetSpec(
            slug=slug, display_name=display_name, asset_type=atype,
            balance=balance, value_nzd=value_nzd, cost_basis_nzd=cost_basis,
            weight_pct=0.0, purchase_history=purchase_history, extra=extra,
        ))
    return specs


# ── Category resolution ────────────────────────────────────────────────────────

async def resolve_category(nanobot, slug: str, qdrant, sov_wallet_url: str) -> dict:
    """Resolve a category slug → list[AssetSpec] with live prices injected.

    Returns:
      {"status": "ok", "specs": [...], "total_value_nzd": x, "total_cost_basis_nzd": y}
      {"status": "not_configured", "message": "..."}
      {"status": "empty", "message": "..."}
      {"status": "pending", "message": "..."}
    """
    content = await _fetch_ledger(nanobot, slug)
    if content is None:
        return {
            "status": "not_configured",
            "message": (
                f"No ledger found for '{slug}'.\n"
                f"Create /portfolios/{slug}.md in Nextcloud with YAML blocks to enable analysis.\n"
                "See portfolio-harness-asset-ledger-spec.md for the format."
            ),
        }

    specs = _parse_ledger(content)
    if not specs:
        return {
            "status": "empty",
            "message": f"No assets found in {slug} ledger. Add YAML blocks to /portfolios/{slug}.md.",
        }

    if slug == "crypto":
        # Stash ledger values before live inject (used as fallback below)
        for s in specs:
            s.extra["_ledger_value_nzd"] = s.value_nzd
        # Overlay live balances and prices from sov-wallet
        try:
            async with httpx.AsyncClient(timeout=15.0) as wc:
                resp = await wc.get(f"{sov_wallet_url}/portfolio")
            if resp.status_code == 503:
                return {"status": "pending", "message": "sov-wallet snapshot not ready yet — try again in a moment."}
            if resp.status_code != 200:
                logger.warning("portfolio_harness: sov-wallet HTTP %s", resp.status_code)
            else:
                wallet_data = resp.json()
                _inject_crypto_live(specs, wallet_data)
        except Exception as exc:
            logger.warning("portfolio_harness: sov-wallet fetch failed: %s", exc)
        # Fallback for assets with no live value (non-ETH/BTC or fetch failure)
        for s in specs:
            if s.value_nzd == 0:
                ledger_val = float(s.extra.get("_ledger_value_nzd", 0) or 0)
                if ledger_val > 0:
                    s.value_nzd = ledger_val
                    s.extra["balance_source"] = "ledger_last_known"
                elif s.cost_basis_nzd > 0:
                    s.value_nzd = s.cost_basis_nzd
                    s.extra["balance_source"] = "cost_basis_proxy"

    # Calculate weights (assets with exclude_from_weight_calc are excluded from denominator)
    total_value  = sum(s.value_nzd for s in specs)
    total_cost   = sum(s.cost_basis_nzd for s in specs)
    weight_denom = sum(s.value_nzd for s in specs if not s.extra.get("exclude_from_weight_calc"))
    for s in specs:
        if s.extra.get("exclude_from_weight_calc"):
            s.weight_pct = -1.0  # sentinel: excluded; displayed as "excl."
        else:
            s.weight_pct = (s.value_nzd / weight_denom * 100.0) if weight_denom else 0.0

    return {
        "status": "ok",
        "specs": specs,
        "total_value_nzd": total_value,
        "total_cost_basis_nzd": total_cost,
    }


def _inject_crypto_live(specs: list[AssetSpec], wallet_data: dict) -> None:
    """Overwrite balance and value_nzd on crypto specs from sov-wallet snapshot."""
    snap = wallet_data.get("snapshot", wallet_data)
    balances = snap.get("balances", {})
    prices   = snap.get("prices", {})

    eth_price_nzd = float((prices.get("ethereum") or {}).get("nzd", 0) or 0)
    btc_price_nzd = float((prices.get("bitcoin")  or {}).get("nzd", 0) or 0)

    # Sum ETH across all chains
    eth_total = 0.0
    for _chain_balances in (balances.get("eth", {}), balances.get("arb", {}), balances.get("op", {})):
        for _addr_data in (_chain_balances or {}).values():
            eth_total += float((_addr_data or {}).get("eth", 0) or 0)

    # Sum BTC
    btc_total = 0.0
    for _addr_data in (balances.get("btc", {}) or {}).values():
        btc_total += float((_addr_data or {}).get("btc", 0) or 0)

    for spec in specs:
        ticker = str(spec.extra.get("ticker", "")).upper()
        if ticker == "ETH" and eth_price_nzd:
            spec.balance   = eth_total
            spec.value_nzd = eth_total * eth_price_nzd
            spec.extra["current_price_nzd"] = eth_price_nzd
        elif ticker == "BTC" and btc_price_nzd:
            spec.balance   = btc_total
            spec.value_nzd = btc_total * btc_price_nzd
            spec.extra["current_price_nzd"] = btc_price_nzd


# ── Weight display helper ─────────────────────────────────────────────────────

def _weight_str(weight_pct: float) -> str:
    """Return weight as string; -1.0 sentinel = excluded from weight calculation."""
    return "excl." if weight_pct < 0 else f"{weight_pct:.1f}%"


# ── Gather phase ──────────────────────────────────────────────────────────────

async def _null_gather() -> tuple[str, None]:
    """Placeholder coroutine for gather slots that are not applicable."""
    return ("", None)


def _merge_browser_results(results: list) -> dict:
    """Combine text from multiple browser result tuples into a single dict."""
    texts = []
    errors = []
    for res in results:
        if isinstance(res, Exception):
            errors.append(str(res))
        elif isinstance(res, tuple):
            if res[0]:
                texts.append(res[0])
            if res[1]:
                errors.append(str(res[1]))
        elif isinstance(res, dict):
            t = res.get("text") or res.get("content") or ""
            if t:
                texts.append(t)
    combined = "\n\n".join(texts) if texts else ""
    return (combined, errors[0] if errors and not combined else None)


async def _gather_one(nanobot, cog, spec: AssetSpec) -> dict:
    """Run browser + finance + Grok concurrently for one security."""
    logger.debug("portfolio_harness: _gather_one starting for %s (%s)", spec.slug, spec.asset_type)

    # Stablecoin short-circuit — no research needed, just report dry powder
    if spec.extra.get("asset_group") == "stablecoin":
        nzd_val = spec.value_nzd or spec.cost_basis_nzd
        return {
            "slug":           spec.slug,
            "gathered":       f"STABLECOIN: {spec.display_name} — NZD {nzd_val:,.0f} held as dry powder.",
            "sources_ok":     [],
            "sources_failed": [],
            "no_data":        False,
            "stablecoin":     True,
            "dry_powder_nzd": nzd_val,
        }

    atype = spec.asset_type

    if atype == "fund":
        provider = spec.extra.get("provider", "")
        browser_query = f"{provider} {spec.display_name} performance {date.today().year}"
        grok_query = f"Market context and outlook for {spec.display_name} ({spec.extra.get('asset_group', 'managed fund')}) for a NZ investor"
        finance_url = None  # no ticker for managed funds

        browser_coro = _gather_browser(nanobot, browser_query)
        grok_coro    = _gather_grok(cog, grok_query)
        browser_res, grok_res = await asyncio.gather(browser_coro, grok_coro, return_exceptions=True)
        finance_res = ("", None)

    elif atype == "property":
        suburb = spec.extra.get("suburb", "")
        city   = spec.extra.get("city", "New Zealand")
        browser_queries = [
            f"{suburb} {city} property market {date.today().year}",
            f"{suburb} {city} comparable sales {date.today().year}",
        ]
        grok_query = f"NZ property market conditions and outlook for {city} in {date.today().year}"

        browser_coros   = [_gather_browser(nanobot, q) for q in browser_queries]
        grok_coro       = _gather_grok(cog, grok_query)
        browser_results, grok_res = await asyncio.gather(
            asyncio.gather(*browser_coros, return_exceptions=True),
            grok_coro,
            return_exceptions=True,
        )
        browser_res = _merge_browser_results(browser_results if not isinstance(browser_results, Exception) else [])
        finance_res = ("", None)

    else:  # crypto
        ticker = spec.extra.get("ticker", "")
        browser_query = f"{spec.display_name} ({ticker or spec.slug}) analysis {date.today().year}"
        domain_scope, _ticker = _classify_domain_scope(f"{ticker} {spec.display_name}")
        if not _ticker and ticker:
            _ticker = ticker.upper()
        finance_url = _build_finance_url(domain_scope, _ticker, spec.display_name) if domain_scope == "securities" else None

        browser_coro  = _gather_browser(nanobot, browser_query)
        finance_coro  = _gather_finance(nanobot, finance_url) if finance_url else _null_gather()
        grok_coro     = _gather_grok(cog, spec.display_name)
        browser_res, finance_res, grok_res = await asyncio.gather(
            browser_coro, finance_coro, grok_coro, return_exceptions=True
        )

    browser_text = browser_res[0] if not isinstance(browser_res, Exception) and isinstance(browser_res, tuple) else ""
    browser_err  = browser_res[1] if not isinstance(browser_res, Exception) and isinstance(browser_res, tuple) else (str(browser_res) if isinstance(browser_res, Exception) else "")
    finance_text = finance_res[0] if not isinstance(finance_res, Exception) and isinstance(finance_res, tuple) else ""
    grok_text    = grok_res[0]    if not isinstance(grok_res,    Exception) and isinstance(grok_res,    tuple) else ""

    sources_ok, sources_failed = [], []
    if browser_text: sources_ok.append("browser")
    elif browser_err: sources_failed.append(f"browser: {browser_err}")
    if finance_text: sources_ok.append("yahoo_finance")
    if grok_text:    sources_ok.append("grok")

    sections = []
    if browser_text:  sections.append(f"## Web Research\n{browser_text}")
    if finance_text:  sections.append(f"## Market Data\n{finance_text[:1500]}")
    if grok_text:     sections.append(f"## Market Context\n{grok_text}")
    gathered_text = "\n\n".join(sections) or "No sources returned results."

    logger.debug("portfolio_harness: gathered content length for %s: %d chars", spec.slug, len(gathered_text))
    return {
        "slug":           spec.slug,
        "gathered":       gathered_text,
        # Raw parts — used by security_analysis_engine() to feed individual agents
        "browser_text":   browser_text,
        "finance_text":   finance_text,
        "grok_text":      grok_text,
        "sources_ok":     sources_ok,
        "sources_failed": sources_failed,
        "no_data":        not sources_ok,
    }


async def _gather_all(nanobot, cog, specs: list[AssetSpec]) -> list[dict]:
    """Parallel gather across all securities — pure HTTP, no GPU."""
    results = await asyncio.gather(*[_gather_one(nanobot, cog, s) for s in specs])
    return list(results)


# ── Synthesis prompts ─────────────────────────────────────────────────────────

def _build_synthesis_prompt(spec: AssetSpec, gathered: str) -> str:
    """Build asset-type-specific synthesis prompt. Expects clean output (think stripped upstream)."""
    today = date.today().isoformat()

    if spec.asset_type == "crypto":
        avg_buy  = spec.cost_basis_nzd / spec.balance if spec.balance else 0.0
        cur_price = spec.extra.get("current_price_nzd", spec.value_nzd / spec.balance if spec.balance else 0.0)
        pnl_nzd  = spec.value_nzd - spec.cost_basis_nzd
        pnl_pct  = (pnl_nzd / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0

        # Estimate holding period from earliest purchase date
        holding_period = "unknown"
        if spec.purchase_history:
            try:
                earliest = min(
                    p.get("date", "") for p in spec.purchase_history
                    if isinstance(p, dict) and p.get("date")
                )
                if earliest:
                    from datetime import date as _date
                    bought = _date.fromisoformat(str(earliest))
                    days   = (_date.today() - bought).days
                    months = days // 30
                    holding_period = (
                        f"{days}d" if days < 60
                        else f"{months}m" if months < 24
                        else f"{months // 12}y {months % 12}m"
                    )
            except Exception:
                pass

        cost_display = f"NZD {spec.cost_basis_nzd:,.0f}" if spec.cost_basis_nzd else "N/A"
        avg_display  = f"NZD {avg_buy:,.0f}/unit" if avg_buy else "N/A"
        pnl_display  = (f"NZD {pnl_nzd:,.0f} ({pnl_pct:+.1f}%)" if spec.cost_basis_nzd else "N/A (no cost basis recorded)")

        return f"""You are a portfolio analyst preparing a point-in-time analysis for the Director. Today is {today}.

POSITION:
Asset: {spec.display_name}
Held: {spec.balance:.4f} units
Cost basis: {cost_display} (avg buy: {avg_display})
Current value: NZD {spec.value_nzd:,.0f} (NZD {cur_price:,.0f}/unit)
Unrealised P&L: {pnl_display}
Portfolio weight: {_weight_str(spec.weight_pct)} of crypto portfolio
Holding period: {holding_period}

RESEARCH:
{gathered}

Produce:
1. Outlook (2 sentences — current market context)
2. Bull case (2 specific factors supporting upside)
3. Bear case (2 specific risk factors)
4. Verdict: BUY | HOLD | SELL with one-line rationale referencing the Director's actual position
5. Confidence: HIGH | MEDIUM | LOW

End with this JSON block on its own line:
{{"verdict": "HOLD", "confidence": "HIGH", "rationale": "...", "summary": ["...", "...", "..."]}}"""

    elif spec.asset_type == "fund":
        pnl_nzd = spec.value_nzd - spec.cost_basis_nzd
        pnl_pct = (pnl_nzd / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0
        provider = spec.extra.get("provider", "")
        fund_type = spec.extra.get("fund_type", "")

        total_contributed = f"NZD {spec.cost_basis_nzd:,.0f}" if spec.cost_basis_nzd else "N/A"
        return_display = f"{pnl_pct:+.1f}%" if spec.cost_basis_nzd else "N/A"

        return f"""You are a portfolio analyst preparing a point-in-time analysis for the Director. Today is {today}.

POSITION:
Fund: {spec.display_name}
Provider: {provider}{f" ({fund_type})" if fund_type else ""}
Total contributed: {total_contributed}
Current value: NZD {spec.value_nzd:,.0f}
Return since inception: {return_display}
Portfolio weight: {_weight_str(spec.weight_pct)} of retirement portfolio

RESEARCH:
{gathered}

Produce:
1. Fund outlook (2 sentences — current market context for this fund type)
2. Bull case (2 factors supporting continued performance)
3. Bear case (2 risks or headwinds)
4. Verdict: INCREASE_CONTRIBUTIONS | HOLD | SWITCH_FUND with one-line rationale
5. Confidence: HIGH | MEDIUM | LOW

End with this JSON block on its own line:
{{"verdict": "HOLD", "confidence": "MEDIUM", "rationale": "...", "summary": ["...", "...", "..."]}}"""

    else:  # property
        acquisition   = spec.extra.get("acquisition", {}) or {}
        mortgage      = spec.extra.get("mortgage", {}) or {}
        rental        = spec.extra.get("rental", {}) or {}
        costs         = spec.extra.get("costs", {}) or {}

        purchase_price = float(acquisition.get("total_cost_basis_nzd", acquisition.get("purchase_price_nzd", 0)) or 0)
        est_value      = spec.value_nzd or float(spec.extra.get("estimated_value_nzd", 0) or 0)
        remaining_mort = float(mortgage.get("remaining_nzd", 0) or 0)
        equity         = est_value - remaining_mort
        mort_rate      = float(mortgage.get("rate_pct", 0) or 0)

        gross_annual   = float(rental.get("annual_gross_nzd", 0) or 0)
        gross_yield    = (gross_annual / est_value * 100.0) if est_value and gross_annual else 0.0

        mgmt_fee_pct   = float(rental.get("management_fee_pct", 0) or 0)
        mgmt_cost      = gross_annual * mgmt_fee_pct / 100.0
        annual_costs   = sum([
            float(costs.get("annual_rates_nzd", 0) or 0),
            float(costs.get("annual_insurance_nzd", 0) or 0),
            float(costs.get("annual_maintenance_estimate_nzd", 0) or 0),
            float(costs.get("body_corporate_annual_nzd", 0) or 0),
            mgmt_cost,
        ])
        net_income     = gross_annual - annual_costs
        net_yield      = (net_income / est_value * 100.0) if est_value and gross_annual else 0.0
        spread         = net_yield - mort_rate
        carry          = "positive" if spread >= 0 else "negative"

        address = spec.extra.get("address", spec.display_name)

        return f"""You are a portfolio analyst preparing a point-in-time analysis for the Director. Today is {today}.

POSITION:
Property: {spec.display_name}
Address: {address}
Purchase price: NZD {purchase_price:,.0f}
Estimated current value: NZD {est_value:,.0f}
Unrealised equity: NZD {equity:,.0f} (estimated value minus remaining mortgage)
Mortgage rate: {mort_rate:.2f}% | Gross yield: {gross_yield:.1f}% | Net yield: {net_yield:.1f}%
Mortgage vs net yield spread: {spread:+.1f}% ({carry} carry)

RESEARCH:
{gathered}

Produce:
1. Market outlook for this suburb/city (2 sentences based on research)
2. Estimated current market value range based on comparable sales (provide a range)
3. Bull case (2 factors supporting hold/appreciation)
4. Bear case (2 risks — include cashflow if negative carry)
5. Verdict: HOLD | SELL | REFINANCE_CONSIDER with one-line rationale
6. Confidence: HIGH | MEDIUM | LOW

End with this JSON block on its own line:
{{"verdict": "HOLD", "confidence": "MEDIUM", "rationale": "...", "summary": ["...", "...", "..."], "estimated_value_range_nzd": [low, high]}}"""


# ── Synthesis execution ────────────────────────────────────────────────────────

def _extract_json_verdict(raw: str) -> dict:
    """Extract trailing JSON verdict block from synthesis output."""
    m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


async def _synthesise_security(cog, spec: AssetSpec, gather_result: dict) -> dict:
    """Per-security synthesis. Crypto assets use the 6-agent security_analysis_engine;
    fund and property assets use the existing single-call path.
    """
    from adapters.inference_queue import InferenceQueue

    # ── Stablecoin short-circuit ──────────────────────────────────────────────
    if gather_result.get("stablecoin"):
        nzd_val = gather_result.get("dry_powder_nzd", spec.value_nzd)
        return {
            "slug":         spec.slug,
            "display_name": spec.display_name,
            "status":       "ok",
            "verdict":      "DRY_POWDER",
            "confidence":   "HIGH",
            "rationale":    f"NZD {nzd_val:,.0f} held as liquid dry powder.",
            "summary":      [f"NZD {nzd_val:,.0f} available as dry powder.", "No market risk — stablecoin/fiat."],
            "full_report":  f"## {spec.display_name}\n\nStablecoin — NZD {nzd_val:,.0f} held as dry powder. No analysis required.",
        }

    # ── No-data short-circuit ────────────────────────────────────────────────
    if gather_result.get("no_data"):
        return {
            "slug":         spec.slug,
            "display_name": spec.display_name,
            "status":       "no_data",
            "verdict":      "N/A",
            "confidence":   "LOW",
            "rationale":    "No research data available for this security.",
            "summary":      [f"No data sources returned results for {spec.display_name}."],
            "full_report":  f"## {spec.display_name}\n\nNo research data available.",
        }

    # ── Crypto: 6-agent security analysis engine ──────────────────────────────
    if spec.asset_type == "crypto":
        gathered = GatheredSources(
            news=gather_result.get("browser_text", gather_result.get("gathered", "")),
            finance=gather_result.get("finance_text", ""),
            grok=gather_result.get("grok_text", ""),
        )
        try:
            eng = await security_analysis_engine(
                cog=cog,
                security_name=spec.display_name,
                ticker=spec.extra.get("ticker"),
                gathered=gathered,
                asset_spec=spec,
            )
        except Exception as exc:
            logger.error("portfolio_harness: security_analysis_engine failed for %s: %s", spec.slug, exc)
            return {
                "slug":         spec.slug,
                "display_name": spec.display_name,
                "status":       "error",
                "verdict":      "N/A",
                "confidence":   "LOW",
                "rationale":    f"Analysis engine failed: {exc}",
                "summary":      [f"Analysis failed for {spec.display_name}."],
                "full_report":  f"## {spec.display_name}\n\nAnalysis engine failed.",
            }
        return {
            "slug":         spec.slug,
            "display_name": spec.display_name,
            "status":       "ok",
            "verdict":      eng.get("verdict", "N/A"),
            "confidence":   eng.get("confidence", "LOW"),
            "rationale":    eng.get("rationale", ""),
            "summary":      eng.get("summary") or [],
            "full_report":  eng.get("full_report", ""),
            "sources_ok":   gather_result.get("sources_ok", []),
            "bull_case":    eng.get("bull_case", ""),
            "bear_case":    eng.get("bear_case", ""),
        }

    # ── Phase 2 — fund and property: migrate to engine when analyst prompts
    #    are designed for non-securities. Until then, existing single-call path.
    prompt = _build_synthesis_prompt(spec, gather_result.get("gathered", ""))
    try:
        _decision = cog._routing_decision(
            prompt, user_input=spec.display_name, task_type="llm_generate",
            delegation_reason="Portfolio asset synthesis — non-sensitive after DCL gate",
        )
        if _decision["use_external"]:
            _dispatch_map = {
                "grok":           cog.ask_grok,
                "gemini":         cog.ask_gemini,
                "groq_inference": cog.ask_groq_inf,
                "openrouter":     cog.ask_openrouter,
                "ollama_cloud":   cog.ask_ollama_cloud,
            }
            _fn = _dispatch_map.get(_decision["provider"], cog.ask_grok)
            result = await _fn(prompt, agent="research_agent", routing_decision=_decision)
        else:
            result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=_SYNTHESIS_TIMEOUT)
    except Exception as exc:
        logger.error("portfolio_harness: synthesis error for %s: %s", spec.slug, exc)
        return {
            "slug":         spec.slug,
            "display_name": spec.display_name,
            "status":       "error",
            "verdict":      "N/A",
            "confidence":   "LOW",
            "rationale":    f"Synthesis failed: {exc}",
            "summary":      [f"Synthesis failed for {spec.display_name}."],
            "full_report":  f"## {spec.display_name}\n\nSynthesis failed.",
        }

    if result.get("status") == "llm_timeout":
        logger.warning("portfolio_harness: synthesis timed out for %s", spec.slug)
        return {
            "slug":         spec.slug,
            "display_name": spec.display_name,
            "status":       "timeout",
            "verdict":      "N/A",
            "confidence":   "LOW",
            "rationale":    "Synthesis timed out — GPU may be busy.",
            "summary":      [f"Synthesis timed out for {spec.display_name}."],
            "full_report":  f"## {spec.display_name}\n\nSynthesis timed out.",
        }

    raw  = result.get("response", "")
    meta = _extract_json_verdict(raw)
    json_m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', raw, re.DOTALL)
    full_report = raw[:json_m.start()].strip() if json_m else raw

    return {
        "slug":         spec.slug,
        "display_name": spec.display_name,
        "status":       "ok",
        "verdict":      meta.get("verdict", "N/A"),
        "confidence":   meta.get("confidence", "LOW"),
        "rationale":    meta.get("rationale", ""),
        "summary":      meta.get("summary", []) or [],
        "full_report":  full_report,
        "sources_ok":   gather_result.get("sources_ok", []),
    }


async def _synthesise_overall(cog, category: str, specs: list[AssetSpec],
                               per_security: dict, total_value: float,
                               total_cost: float) -> dict:
    """One final cross-security synthesis call after all per-security work completes."""
    from adapters.inference_queue import InferenceQueue

    today = date.today().isoformat()
    pnl_total = total_value - total_cost
    pnl_pct   = (pnl_total / total_cost * 100.0) if total_cost else 0.0

    sec_lines = []
    for spec in specs:
        res = per_security.get(spec.slug, {})
        verdict = res.get("verdict", "N/A")
        rationale = res.get("rationale", "")
        pnl_nzd = spec.value_nzd - spec.cost_basis_nzd
        pnl_p   = (pnl_nzd / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0
        sec_lines.append(
            f"- {spec.display_name}: NZD {spec.value_nzd:,.0f} ({_weight_str(spec.weight_pct)} weight) "
            f"P&L {pnl_p:+.1f}% — Verdict: {verdict} ({rationale})"
        )

    prompt = f"""You are a portfolio analyst. Today is {today}.

PORTFOLIO: {category.upper()}
Total value: NZD {total_value:,.0f} vs cost basis NZD {total_cost:,.0f} ({pnl_pct:+.1f}%)

SECURITIES:
{chr(10).join(sec_lines)}

Produce:
1. Portfolio health score: 1-10 with brief rationale
2. Executive summary (2-3 sentences on overall portfolio state)
3. Top 3 ranked actions in priority order
4. Diversification comment (weight distribution, concentration risk)

End with this JSON block on its own line:
{{"health_score": 7, "executive_summary": "...", "ranked_actions": ["...", "...", "..."]}}"""

    try:
        _decision = cog._routing_decision(prompt, user_input=category, task_type="llm_generate")
        if _decision["use_external"]:
            _dispatch_map = {
                "grok":           cog.ask_grok,
                "gemini":         cog.ask_gemini,
                "groq_inference": cog.ask_groq_inf,
                "openrouter":     cog.ask_openrouter,
                "ollama_cloud":   cog.ask_ollama_cloud,
            }
            _fn = _dispatch_map.get(_decision["provider"], cog.ask_grok)
            result = await _fn(prompt, agent="research_agent", routing_decision=_decision)
        else:
            result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=_SYNTHESIS_TIMEOUT)
    except Exception as exc:
        logger.error("portfolio_harness: overall synthesis error: %s", exc)
        return {"health_score": 0, "executive_summary": "Overall synthesis failed.", "ranked_actions": []}

    if result.get("status") == "llm_timeout":
        return {"health_score": 0, "executive_summary": "Overall synthesis timed out.", "ranked_actions": []}

    raw  = result.get("response", "")
    meta = {}
    m    = re.search(r'\{[^{}]*"health_score"[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            meta = json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    json_m = re.search(r'\{[^{}]*"health_score"[^{}]*\}', raw, re.DOTALL)
    full_report = raw[:json_m.start()].strip() if json_m else raw

    return {
        "health_score":      meta.get("health_score", 0),
        "executive_summary": meta.get("executive_summary", ""),
        "ranked_actions":    meta.get("ranked_actions", []) or [],
        "full_report":       full_report,
    }


# ── Write-back (async, Nextcloud) ──────────────────────────────────────────────

async def _write_back(nanobot, category_slug: str, specs: list[AssetSpec], per_security: dict) -> bool:
    """Patch harness-maintained fields in the Nextcloud ledger file. Returns True on success."""
    content = await _fetch_ledger(nanobot, category_slug)
    if content is None:
        logger.warning("portfolio_harness: write-back skipped — ledger not found")
        return False
    today = date.today().isoformat()
    for spec in specs:
        res = per_security.get(spec.slug)
        if not res or res.get("status") not in ("ok",):
            continue

        updates: dict[str, object] = {
            "current_value_nzd":    round(spec.value_nzd, 2),
            "last_analysis":        f'"{today}"',
            "last_verdict":         res.get("verdict", "N/A"),
            "last_confidence":      res.get("confidence", "LOW"),
            "last_verdict_rationale": f'"{res.get("rationale", "")}"',
        }

        pnl_nzd = spec.value_nzd - spec.cost_basis_nzd
        pnl_pct = (pnl_nzd / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0
        updates["unrealised_pnl_nzd"] = round(pnl_nzd, 2)
        updates["unrealised_pnl_pct"]  = round(pnl_pct, 1)

        # Property-specific
        if spec.asset_type == "property":
            mortgage     = spec.extra.get("mortgage", {}) or {}
            rental       = spec.extra.get("rental", {}) or {}
            costs_block  = spec.extra.get("costs", {}) or {}
            remaining    = float(mortgage.get("remaining_nzd", 0) or 0)
            updates["unrealised_equity_nzd"] = round(spec.value_nzd - remaining, 2)
            gross_ann = float(rental.get("annual_gross_nzd", 0) or 0)
            est_val   = spec.value_nzd
            if est_val and gross_ann:
                gross_yield = gross_ann / est_val * 100.0
                mgmt_fee    = float(rental.get("management_fee_pct", 0) or 0)
                annual_costs = sum([
                    float(costs_block.get("annual_rates_nzd", 0) or 0),
                    float(costs_block.get("annual_insurance_nzd", 0) or 0),
                    float(costs_block.get("annual_maintenance_estimate_nzd", 0) or 0),
                    float(costs_block.get("body_corporate_annual_nzd", 0) or 0),
                    gross_ann * mgmt_fee / 100.0,
                ])
                net_yield = (gross_ann - annual_costs) / est_val * 100.0
                updates["gross_yield_pct"] = round(gross_yield, 1)
                updates["net_yield_pct"]   = round(net_yield, 1)

        content = _patch_yaml_block(content, spec.slug, updates)

    result = await nanobot.run("openclaw-nextcloud", "files_write", {
        "path": f"/portfolios/{category_slug}.md",
        "content": content,
    })
    nb = result.get("result") if result.get("result") is not None else result
    if nb.get("status") == "error":
        logger.warning("portfolio_harness: write-back files_write failed: %s", nb.get("error"))
        return False
    return True


def _patch_yaml_block(content: str, slug: str, updates: dict) -> str:
    """Patch harness-maintained fields inside the YAML block for the given slug."""
    # Find the YAML block containing this slug
    yaml_block_re = re.compile(r'(```yaml\s*\n)(.*?)(```)', re.DOTALL)

    def _patch_block(m: re.Match) -> str:
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        # Check if this block contains our slug
        slug_m = re.search(r'^\s*slug\s*:\s*' + re.escape(slug) + r'\s*$', body, re.MULTILINE)
        if not slug_m:
            return m.group(0)
        # Patch each harness-maintained field
        new_body = body
        for field_name, value in updates.items():
            # Format the value for YAML
            if isinstance(value, str) and not value.startswith('"'):
                yaml_value = value
            elif isinstance(value, str):
                # Already quoted string
                yaml_value = value
            elif isinstance(value, float):
                yaml_value = f"{value}"
            else:
                yaml_value = str(value)

            field_re = re.compile(
                r'^(\s*' + re.escape(field_name) + r'\s*:\s*)(.*)$',
                re.MULTILINE,
            )
            if field_re.search(new_body):
                new_body = field_re.sub(r'\g<1>' + yaml_value, new_body)
            else:
                # Field not present — append before closing backticks
                new_body = new_body.rstrip('\n') + f"\n{field_name}: {yaml_value}\n"
        return prefix + new_body + suffix

    return yaml_block_re.sub(_patch_block, content)


# ── Confirmation gate message ──────────────────────────────────────────────────

def _build_confirmation_message(category: str, specs: list[AssetSpec],
                                  per_security: dict, overall: dict,
                                  total_value: float, total_cost: float,
                                  word_count: int,
                                  note_title: str | None = None,
                                  note_id=None) -> str:
    """Build the Telegram summary / confirmation gate message."""
    pnl_nzd = total_value - total_cost
    pnl_pct = (pnl_nzd / total_cost * 100.0) if total_cost else 0.0

    if total_cost:
        header = (
            f"<b>{category.upper()} PORTFOLIO</b> — NZD {total_value:,.0f} "
            f"({pnl_pct:+.1f}% vs cost basis NZD {total_cost:,.0f})"
        )
    else:
        header = f"<b>{category.upper()} PORTFOLIO</b> — NZD {total_value:,.0f}"

    lines = [header, ""]
    for spec in specs:
        res = per_security.get(spec.slug, {})
        verdict    = res.get("verdict", "N/A")
        confidence = res.get("confidence", "")
        summary    = res.get("summary", [])
        spec_pnl   = spec.value_nzd - spec.cost_basis_nzd
        spec_pnl_p = (spec_pnl / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0

        if spec.asset_type == "property":
            mortgage  = spec.extra.get("mortgage", {}) or {}
            remaining = float(mortgage.get("remaining_nzd", 0) or 0)
            equity    = spec.value_nzd - remaining
            sec_header = (
                f"<b>{spec.display_name}</b> (NZD {spec.value_nzd:,.0f} | {_weight_str(spec.weight_pct)} | "
                f"equity NZD {equity:,.0f}) — {verdict}"
            )
        elif spec.cost_basis_nzd:
            sec_header = (
                f"<b>{spec.display_name}</b> (NZD {spec.value_nzd:,.0f} | {_weight_str(spec.weight_pct)} | "
                f"cost NZD {spec.cost_basis_nzd:,.0f} | {spec_pnl_p:+.1f}%) — {verdict}"
            )
        else:
            sec_header = f"<b>{spec.display_name}</b> (NZD {spec.value_nzd:,.0f} | {_weight_str(spec.weight_pct)}) — {verdict}"

        lines.append(sec_header)
        for bullet in summary[:2]:
            lines.append(f"• {bullet}")
        if confidence:
            lines.append(f"Confidence: {confidence}")
        lines.append("")

    health     = overall.get("health_score", 0)
    exec_sum   = overall.get("executive_summary", "")
    ranked     = overall.get("ranked_actions", [])

    if health:
        lines.append(f"Portfolio health: {health}/10")
    if exec_sum:
        lines.append(exec_sum)
    if ranked:
        lines.append(f"Key action: {ranked[0]}")
    lines.append("")
    lines.append(f"Full report: ~{word_count} words")
    if note_title:
        id_str = f" (ID: {note_id})" if note_id else ""
        lines.append(f"Saved to Notes: <i>{note_title}</i>{id_str}")
    else:
        lines.append("(Note save failed — say <b>save portfolio</b> to retry.)")

    return "\n".join(lines)


# ── Nextcloud note content ─────────────────────────────────────────────────────

def _build_note_content(category: str, specs: list[AssetSpec],
                          per_security: dict, overall: dict,
                          total_value: float, total_cost: float) -> str:
    """Build the full markdown report for the Nextcloud Note."""
    today = date.today().isoformat()
    pnl_nzd = total_value - total_cost
    pnl_pct = (pnl_nzd / total_cost * 100.0) if total_cost else 0.0

    lines = [
        f"# Portfolio Analysis: {category.title()}",
        f"**Date:** {today}  |  **Total value:** NZD {total_value:,.0f}"
        + (f"  |  **vs cost basis:** {pnl_pct:+.1f}%" if total_cost else ""),
        "",
        "---",
        "",
        "## Portfolio Summary",
        "",
    ]

    health   = overall.get("health_score", 0)
    exec_sum = overall.get("executive_summary", "")
    ranked   = overall.get("ranked_actions", [])
    if health:
        lines.append(f"**Health score: {health}/10**")
        lines.append("")
    if exec_sum:
        lines.append(exec_sum)
        lines.append("")

    # Summary table
    lines.append("| Asset | Value | Weight | Cost Basis | P&L | Verdict |")
    lines.append("|-------|-------|--------|------------|-----|---------|")
    for spec in specs:
        res    = per_security.get(spec.slug, {})
        verdict = res.get("verdict", "N/A")
        pnl_p  = ((spec.value_nzd - spec.cost_basis_nzd) / spec.cost_basis_nzd * 100.0
                  if spec.cost_basis_nzd else 0.0)
        cost_str = f"NZD {spec.cost_basis_nzd:,.0f}" if spec.cost_basis_nzd else "N/A"
        pnl_str  = f"{pnl_p:+.1f}%" if spec.cost_basis_nzd else "N/A"
        lines.append(
            f"| {spec.display_name} | NZD {spec.value_nzd:,.0f} | {_weight_str(spec.weight_pct)} "
            f"| {cost_str} | {pnl_str} | {verdict} |"
        )
    lines.append("")

    if ranked:
        lines.append("**Ranked actions:**")
        for i, action in enumerate(ranked[:3], 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    # Per-security sections
    lines.append("---")
    lines.append("")
    for spec in specs:
        res     = per_security.get(spec.slug, {})
        verdict = res.get("verdict", "N/A")
        conf    = res.get("confidence", "")
        report  = res.get("full_report", "")
        heading = f"## {spec.display_name} — {verdict}"
        if conf:
            heading += f" (Confidence: {conf})"
        lines.append(heading)
        lines.append("")
        if report:
            lines.append(report)
        lines.append("")
        lines.append("---")
        lines.append("")

    overall_report = overall.get("full_report", "")
    if overall_report:
        lines.append("## Overall Portfolio Assessment")
        lines.append("")
        lines.append(overall_report)
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("*Generated by Portfolio Analysis Harness. Sources: SearXNG, Grok, Yahoo Finance.*")
    return "\n".join(lines)


# ── Episodic write ─────────────────────────────────────────────────────────────

async def _write_episodic(qdrant, category: str, specs: list[AssetSpec],
                           per_security: dict, note_id: str | None) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        verdicts = {s.slug: per_security.get(s.slug, {}).get("verdict", "N/A") for s in specs}
        await qdrant.store(
            collection="episodic",
            content=(
                f"Portfolio analysis completed at {ts}: category='{category}', "
                f"securities={[s.slug for s in specs]}, verdicts={verdicts}. "
                f"Note saved: {'yes id=' + str(note_id) if note_id else 'no'}."
            ),
            metadata={
                "type":       "episodic",
                "event_type": "portfolio_analysis_complete",
                "category":   category,
                "securities": [s.slug for s in specs],
                "verdicts":   verdicts,
                "note_id":    note_id,
                "ts":         ts,
            },
        )
    except Exception as exc:
        logger.warning("portfolio_harness: episodic write failed: %s", exc)


# ── Background analysis task ───────────────────────────────────────────────────

async def _run_analysis_task(cog, nanobot, qdrant, category: str, slug: str, sov_wallet_url: str) -> None:
    """Background task: resolve → gather → synthesise → write-back → checkpoint → notify."""
    await _set_running(qdrant, slug)
    try:
        logger.info("portfolio_harness: [task] starting analysis category=%r slug=%r", category, slug)

        # Resolve category → specs + live prices
        resolve_result = await resolve_category(nanobot, slug, qdrant, sov_wallet_url)
        if resolve_result["status"] != "ok":
            await _notify_telegram(
                f"<b>Portfolio analysis: {category}</b>\n"
                f"{resolve_result.get('message', 'Category not configured.')}"
            )
            return

        specs      = resolve_result["specs"]
        total_val  = resolve_result["total_value_nzd"]
        total_cost = resolve_result["total_cost_basis_nzd"]
        session_id = str(uuid.uuid4())

        logger.info("portfolio_harness: [task] gathering %d securities in parallel", len(specs))
        gather_results = await _gather_all(nanobot, cog, specs)
        gather_by_slug = {r["slug"]: r for r in gather_results}

        logger.info("portfolio_harness: [task] synthesising %d securities sequentially", len(specs))
        per_security: dict[str, dict] = {}
        for spec in specs:
            synth = await _synthesise_security(cog, spec, gather_by_slug.get(spec.slug, {}))
            per_security[spec.slug] = synth

        logger.info("portfolio_harness: [task] overall synthesis")
        overall = await _synthesise_overall(cog, slug, specs, per_security, total_val, total_cost)

        # Write back to Nextcloud ledger
        try:
            wb_ok = await _write_back(nanobot, slug, specs, per_security)
            if not wb_ok:
                logger.warning("portfolio_harness: [task] write-back returned False")
        except Exception as exc:
            logger.warning("portfolio_harness: [task] write-back exception: %s", exc)

        # Build note content
        note_content = _build_note_content(slug, specs, per_security, overall, total_val, total_cost)
        word_count   = len(note_content.split())

        # Write WM checkpoint (retained as save-retry fallback)
        spec_dicts = [
            {
                "slug": s.slug, "display_name": s.display_name, "asset_type": s.asset_type,
                "balance": s.balance, "value_nzd": s.value_nzd, "cost_basis_nzd": s.cost_basis_nzd,
                "weight_pct": s.weight_pct,
            }
            for s in specs
        ]
        await _write_checkpoint(qdrant, session_id, slug, {
            "resolve": {
                "category": slug, "total_value_nzd": total_val,
                "total_cost_basis_nzd": total_cost, "securities": spec_dicts,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            "analyse": {
                _sec_slug: {
                    "verdict":     per_security[_sec_slug].get("verdict"),
                    "confidence":  per_security[_sec_slug].get("confidence"),
                    "rationale":   per_security[_sec_slug].get("rationale"),
                    "full_report": per_security[_sec_slug].get("full_report"),
                    "summary":     per_security[_sec_slug].get("summary"),
                    "ts":          datetime.now(timezone.utc).isoformat(),
                }
                for _sec_slug in per_security
            },
            "overall": {
                "health_score":      overall.get("health_score"),
                "executive_summary": overall.get("executive_summary"),
                "full_report":       overall.get("full_report"),
                "ranked_actions":    overall.get("ranked_actions"),
                "ts":                datetime.now(timezone.utc).isoformat(),
            },
            "note_content": note_content,
            "category_slug": slug,
            "category_display": category,
        })

        # Auto-save to Nextcloud Notes
        today      = date.today().isoformat()
        note_title = f"Portfolio Analysis: {category.title()} ({today})"
        note_id    = None
        try:
            nb = await nanobot.run("openclaw-nextcloud", "notes_create", {
                "title":    note_title,
                "content":  note_content,
                "category": "Portfolio",
            })
            nb_result = nb.get("result") if nb.get("result") is not None else nb
            if isinstance(nb_result, dict):
                note_id = nb_result.get("id") or nb_result.get("note_id")
            if note_id:
                logger.info("portfolio_harness: [task] note auto-saved id=%s", note_id)
            else:
                logger.warning("portfolio_harness: [task] note auto-save returned no id: %s", nb_result)
        except Exception as exc:
            note_title = None
            logger.warning("portfolio_harness: [task] note auto-save failed: %s", exc)

        # Write episodic audit trail
        asyncio.create_task(_write_episodic(
            qdrant, slug, specs, per_security,
            str(note_id) if note_id else None,
        ))

        # Build confirmation message and notify via Telegram
        confirm_msg = _build_confirmation_message(
            slug, specs, per_security, overall,
            total_val, total_cost, word_count,
            note_title=note_title, note_id=note_id,
        )
        await _notify_telegram(confirm_msg)

    except Exception as exc:
        logger.error("portfolio_harness: [task] analysis failed: %s", exc, exc_info=True)
        await _notify_telegram(
            f"<b>Portfolio analysis failed</b>\n"
            f"Category: {category}\n"
            f"Error: {exc}"
        )
    finally:
        await _clear_running(qdrant)


# ── Public entry points ────────────────────────────────────────────────────────

async def run_portfolio_analysis(cog, nanobot, qdrant,
                                  category: str, sov_wallet_url: str) -> dict:
    """Spawn background analysis task. Returns immediately with acknowledgement.

    Checks in-progress flag and stale checkpoint before spawning.
    Analysis runs in background; notifies Director via Telegram when done.
    """
    slug = _resolve_slug(category)

    # Check in-progress
    running = await _get_running(qdrant)
    if running:
        running_cat = running.get("category", "unknown")
        running_ts  = running.get("started", "")[:16]
        return {
            "status": "already_running",
            "requires_confirmation": False,
            "director_message": (
                f"Analysis already in progress ({running_cat} started {running_ts} UTC). "
                "It will notify you when done."
            ),
        }

    # Check stale checkpoint (pending save)
    stale = await _read_checkpoint(qdrant)
    if stale:
        stale_cat = stale.get("category", "unknown")
        stale_ts  = stale.get("last_checkpoint_ts", "")[:16]
        if stale_cat == slug:
            return {
                "status": "stale_checkpoint",
                "requires_confirmation": False,
                "director_message": (
                    f"Previous {stale_cat} analysis from {stale_ts} is pending save.\n"
                    "Say <b>save portfolio</b> to save it to Nextcloud Notes, "
                    "or <b>clear portfolio</b> to discard and run fresh."
                ),
            }
        # Different category — clear the old checkpoint
        await _clear_checkpoint(qdrant)

    # Spawn background task
    asyncio.create_task(_run_analysis_task(cog, nanobot, qdrant, category, slug, sov_wallet_url))

    return {
        "status": "running",
        "requires_confirmation": False,
    }


async def run_portfolio_analysis_save(cog, nanobot, qdrant) -> dict:
    """Read checkpoint → create Nextcloud Note → clear checkpoint + write episodic."""
    cp = await _read_checkpoint(qdrant)
    if not cp:
        return {"status": "error", "director_message": "No portfolio analysis checkpoint — run /portfolio <category> first."}

    step_results   = cp.get("step_results", {})
    note_content   = step_results.get("note_content", "")
    category_slug  = step_results.get("category_slug", cp.get("category", "portfolio"))
    category_disp  = step_results.get("category_display", category_slug)

    if not note_content:
        return {"status": "error", "director_message": "Checkpoint has no report — run /portfolio <category> first."}

    today      = date.today().isoformat()
    note_title = f"Portfolio Analysis: {category_disp.title()} ({today})"

    nb = await nanobot.run("openclaw-nextcloud", "notes_create", {
        "title":    note_title,
        "content":  note_content,
        "category": "Portfolio",
    })

    note_id = None
    result  = nb.get("result") if nb.get("result") is not None else nb
    if isinstance(result, dict):
        note_id = result.get("id") or result.get("note_id")

    resolve_info = step_results.get("resolve", {})
    specs_data   = resolve_info.get("securities", [])

    asyncio.create_task(_write_episodic(
        qdrant, category_slug,
        [AssetSpec(slug=s["slug"], display_name=s["display_name"],
                   asset_type=s["asset_type"], balance=s["balance"],
                   value_nzd=s["value_nzd"], cost_basis_nzd=s["cost_basis_nzd"],
                   weight_pct=s["weight_pct"]) for s in specs_data],
        step_results.get("analyse", {}),
        str(note_id) if note_id else None,
    ))

    await _clear_checkpoint(qdrant)

    id_line = f"\nNote ID: {note_id}" if note_id else ""
    return {
        "status":           "ok",
        "note_id":          note_id,
        "note_title":       note_title,
        "director_message": f"Portfolio analysis saved to Nextcloud Notes.\nTitle: {note_title}{id_line}",
    }


async def run_portfolio_analysis_clear(qdrant) -> dict:
    """Clear portfolio analysis checkpoint from working_memory."""
    await _clear_checkpoint(qdrant)
    return {"status": "ok", "director_message": "Portfolio analysis harness cleared."}
