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
    _gather_technicals,
    _no_td,
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

# Maps asset slug → portfolio_targets group name.
# Any slug absent from this map falls to "everything_else".
_GROUP_MEMBERSHIP: dict[str, str] = {
    "eth":  "eth",   "weth": "eth",   "pseth": "eth",
    "btc":  "btc",
    "usdt": "stablecoins", "dai":  "stablecoins",
    "musd": "stablecoins", "ucap": "stablecoins",
    "sol":  "productive_alts", "link": "productive_alts",
    "yfi":  "productive_alts", "aave": "productive_alts",
    "dot":  "productive_alts",
    "enj":  "everything_else", "matic": "everything_else",
    "mkr":  "everything_else", "wxt":  "everything_else",
}

# CoinGecko simple/price IDs for crypto slugs.
# Used as fallback when sov-wallet has no live price (node down, unsupported asset).
# None = no CoinGecko ID available; falls back to ledger/cost-basis.
_COINGECKO_IDS: dict[str, str | None] = {
    "eth":   "ethereum",
    "weth":  "ethereum",      # wrapped ETH tracks ETH price
    "pseth": "ethereum",      # pStake ETH ≈ ETH price
    "btc":   "bitcoin",
    "usdt":  "tether",
    "dai":   "dai",
    "link":  "chainlink",
    "sol":   "solana",
    "yfi":   "yearn-finance",
    "uni":   "uniswap",
    "aave":  "aave",
    "dot":   "polkadot",
    "wxt":   "wirex-token",
    "enj":   "enjincoin",
    "matic": "matic-network",
    "mkr":   "maker",
}

# Human-readable labels for balance_source values stored in spec.extra.
# Raw internal keys must never appear in Director-facing output.
_BALANCE_SOURCE_LABELS: dict[str, str] = {
    "ledger_total_amount": "ledger",
    "coingecko":           "CoinGecko",
    "ledger_last_known":   "last known",
    "cost_basis_proxy":    "cost basis",
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

        _top_disp  = str(block.get("disposition", "") or "")
        _top_grp   = str(block.get("asset_group", "") or "")
        _nested    = block.get("extra") or {}
        _eff_disp  = str(_nested.get("disposition", "") or "") or _top_disp
        _eff_grp   = str(_nested.get("asset_group", "") or "") or _top_grp
        if _eff_disp in ("disposal_candidate", "sold", "planned") or _eff_grp == "closed":
            logger.info(
                "portfolio_harness: skipping '%s' (disposition=%r, asset_group=%r)",
                slug, _eff_disp, _eff_grp,
            )
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
        # Flatten nested `extra:` dict so asset_group, tax_note etc. are top-level
        if isinstance(extra.get("extra"), dict):
            for _ek, _ev in extra.pop("extra").items():
                extra.setdefault(_ek, _ev)
        specs.append(AssetSpec(
            slug=slug, display_name=display_name, asset_type=atype,
            balance=balance, value_nzd=value_nzd, cost_basis_nzd=cost_basis,
            weight_pct=0.0, purchase_history=purchase_history, extra=extra,
        ))
    return specs


# ── Concentration flag helpers ────────────────────────────────────────────────

def _parse_portfolio_targets(content: str) -> dict:
    """Extract the portfolio_targets YAML block from ledger content.

    Returns the inner dict (group → {target_weight_pct, band_lower, band_upper, rationale})
    or {} if absent or unparseable.
    """
    import yaml
    for m in re.finditer(r'```yaml\s*\n(.*?)\n```', content, re.DOTALL):
        try:
            block = yaml.safe_load(m.group(1))
        except Exception:
            continue
        if isinstance(block, dict) and "portfolio_targets" in block:
            raw = block["portfolio_targets"]
            return raw if isinstance(raw, dict) else {}
    return {}


def _parse_watchlist(content: str) -> list[dict]:
    """Extract the watchlist YAML block from ledger content.

    Returns list of {slug, reason} dicts, or [] if absent or unparseable.
    """
    import yaml
    for m in re.finditer(r'```yaml\s*\n(.*?)\n```', content, re.DOTALL):
        try:
            block = yaml.safe_load(m.group(1))
        except Exception:
            continue
        if isinstance(block, dict) and "watchlist" in block:
            raw = block["watchlist"]
            if isinstance(raw, list):
                return [
                    {"slug": str(e.get("slug", "")), "reason": str(e.get("reason", ""))}
                    for e in raw if isinstance(e, dict) and e.get("slug")
                ]
    return []


def _get_asset_group(spec, portfolio_targets: dict) -> str:
    """Hybrid group lookup for concentration flag calculations.

    Tries spec.extra['asset_group'] first — works for retirement, property, and any
    category where YAML asset_group values match portfolio_targets keys directly.
    Falls back to _GROUP_MEMBERSHIP for crypto where asset_group values (core,
    productive_alt, stablecoin, eth_derivative) don't match target keys.
    """
    ag = (spec.extra or {}).get("asset_group", "")
    if ag and ag in portfolio_targets:
        return ag
    return _GROUP_MEMBERSHIP.get(spec.slug, "everything_else")


def _calculate_concentration_flags(specs: list, portfolio_targets: dict) -> list[dict]:
    """Pure deterministic check: group weights vs portfolio_targets bands.

    Returns a list of flag dicts for groups outside their bands.
    Returns [] if all groups are within bands or portfolio_targets is empty.

    Each flag: {group, slugs_in_group, current_pct, target_pct,
                band_lower, band_upper, direction, rationale}
    """
    if not portfolio_targets:
        return []

    # Aggregate weight_pct per group (exclude assets with weight_pct sentinel -1.0)
    group_weights: dict[str, float]       = {}
    group_slugs:   dict[str, list[str]]   = {}
    total_weight = 0.0
    for spec in specs:
        if spec.weight_pct < 0:
            continue   # excluded from weight calc
        group = _get_asset_group(spec, portfolio_targets)
        group_weights[group] = group_weights.get(group, 0.0) + spec.weight_pct
        group_slugs.setdefault(group, []).append(spec.slug)
        total_weight += spec.weight_pct

    flags: list[dict] = []
    for group, targets in portfolio_targets.items():
        if not isinstance(targets, dict):
            continue
        target_pct  = float(targets.get("target_weight_pct",          0) or 0)
        band_lower  = float(targets.get("rebalance_band_lower_pct",   0) or 0)
        band_upper  = float(targets.get("rebalance_band_upper_pct", 100) or 100)
        current_pct = round(group_weights.get(group, 0.0), 1)
        slugs       = group_slugs.get(group, [])

        if current_pct > band_upper:
            direction = "overweight"
            rationale = (
                f"{group} group at {current_pct:.1f}% exceeds upper band {band_upper:.0f}% "
                f"(target: {target_pct:.0f}%)"
            )
        elif current_pct < band_lower:
            direction = "underweight"
            rationale = (
                f"{group} group at {current_pct:.1f}% is below lower band {band_lower:.0f}% "
                f"(target: {target_pct:.0f}%)"
            )
        else:
            continue   # within band — no flag

        flags.append({
            "group":          group,
            "slugs_in_group": slugs,
            "current_pct":    current_pct,
            "target_pct":     target_pct,
            "band_lower":     band_lower,
            "band_upper":     band_upper,
            "direction":      direction,
            "rationale":      rationale,
        })

    return flags


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

    portfolio_targets = _parse_portfolio_targets(content)
    watchlist         = _parse_watchlist(content)
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
        # CoinGecko prices for any asset still at zero value (BTC node down, non-ETH/BTC, etc.)
        _zero_specs = [s for s in specs if s.value_nzd == 0 and s.balance > 0]
        if _zero_specs:
            _cg_prices = await _fetch_coingecko_prices([s.slug for s in _zero_specs])
            for s in _zero_specs:
                cg_price = _cg_prices.get(s.slug, 0)
                if cg_price > 0:
                    s.value_nzd = s.balance * cg_price
                    s.extra["current_price_nzd"] = cg_price
                    s.extra["balance_source"] = "coingecko"
                else:
                    # CoinGecko also failed — stale ledger or cost basis
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
        "status":               "ok",
        "specs":                specs,
        "total_value_nzd":      total_value,
        "total_cost_basis_nzd": total_cost,
        "portfolio_targets":    portfolio_targets,
        "watchlist":            watchlist,
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
            spec.extra["current_price_nzd"] = btc_price_nzd
            if btc_total > 0:
                spec.balance = btc_total
            # if btc_total == 0, keep spec.balance from ledger (node may be down)
            spec.value_nzd = spec.balance * btc_price_nzd
            if btc_total == 0 and spec.balance > 0:
                spec.extra["balance_source"] = "ledger_total_amount"


async def _fetch_coingecko_prices(slugs: list[str]) -> dict[str, float]:
    """Batch-fetch NZD spot prices from CoinGecko for the given slugs.

    Returns {slug: price_nzd}. Slugs with no CoinGecko ID or failed fetches
    are absent from the result. Multiple slugs mapping to the same CoinGecko ID
    (e.g. eth/weth/pseth → ethereum) all receive the same price.
    """
    id_map = {s: _COINGECKO_IDS[s] for s in slugs
              if _COINGECKO_IDS.get(s)}   # slug → coingecko_id (str)
    if not id_map:
        return {}
    unique_ids = list(set(id_map.values()))
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={','.join(unique_ids)}&vs_currencies=nzd")
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        id_prices: dict[str, float] = {
            k: float(v.get("nzd", 0) or 0) for k, v in r.json().items()
        }
        result: dict[str, float] = {}
        for slug, cg_id in id_map.items():
            price = id_prices.get(cg_id, 0)
            if price > 0:
                result[slug] = price
        logger.info("portfolio_harness: CoinGecko prices fetched for %d slugs", len(result))
        return result
    except Exception as exc:
        logger.warning("portfolio_harness: CoinGecko batch fetch failed: %s", exc)
        return {}


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
    td    = _no_td(spec.slug)   # default; overwritten for crypto below

    # Zero-value short-circuit — skip all gather sources, return NO_DATA immediately
    if spec.value_nzd == 0.0 and not spec.extra.get("disposition"):
        logger.info("portfolio_harness: zero-value '%s' — skipping gather", spec.slug)
        return {
            "slug":           spec.slug,
            "gathered":       f"NO_DATA: {spec.display_name} — current_value_nzd is 0.",
            "sources_ok":     [],
            "sources_failed": ["zero_value"],
            "no_data":        True,
            "zero_value":     True,
            "td":             td,
        }

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
        browser_res, finance_res, grok_res, td = await asyncio.gather(
            browser_coro, finance_coro, grok_coro, _gather_technicals(spec.slug),
            return_exceptions=True,
        )
        if isinstance(td, Exception):
            td = _no_td(spec.slug)

    browser_text = browser_res[0] if not isinstance(browser_res, Exception) and isinstance(browser_res, tuple) else ""
    browser_err  = browser_res[1] if not isinstance(browser_res, Exception) and isinstance(browser_res, tuple) else (str(browser_res) if isinstance(browser_res, Exception) else "")
    finance_text = finance_res[0] if not isinstance(finance_res, Exception) and isinstance(finance_res, tuple) else ""
    grok_text    = grok_res[0]    if not isinstance(grok_res,    Exception) and isinstance(grok_res,    tuple) else ""

    logger.info(
        "portfolio_harness: gathered chars for %s — browser=%d finance=%d grok=%d technicals=%s",
        spec.slug, len(browser_text), len(finance_text), len(grok_text),
        "ok" if td.data_available else "unavailable",
    )

    sources_ok, sources_failed = [], []
    if browser_text: sources_ok.append("browser")
    elif browser_err: sources_failed.append(f"browser: {browser_err}")
    if finance_text: sources_ok.append("yahoo_finance")
    if grok_text:    sources_ok.append("grok")
    if td.data_available: sources_ok.append("technicals")

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
        "td":             td,
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


async def _synthesise_security(cog, spec: AssetSpec, gather_result: dict,
                               concentration_flags: list | None = None) -> dict:
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

    # ── Zero-value short-circuit ─────────────────────────────────────────────
    if gather_result.get("zero_value"):
        return {
            "slug":         spec.slug,
            "display_name": spec.display_name,
            "status":       "no_data",
            "verdict":      "NO_DATA",
            "confidence":   "LOW",
            "rationale":    "current_value_nzd is 0 — update from Sharesies app before analysis",
            "summary":      ["Update current_value_nzd from Sharesies app before next analysis."],
            "full_report":  f"## {spec.display_name}\n\ncurrent_value_nzd is 0 — update from Sharesies before analysis.",
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
                td=gather_result.get("td"),
                concentration_flags=concentration_flags or [],
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


def _build_buy_signals_block(specs: list, technicals: dict) -> str:
    """Deterministic scan of held assets for RSI/MACD/MA buy signals.

    Returns a formatted string for injection into the overall synthesis prompt.
    Excludes stablecoins, disposal candidates, utility tokens, and eth derivatives.
    """
    _EXCLUDED_GROUPS = {"stablecoin", "disposal", "utility", "closed", "eth_derivative"}
    signals = []

    for spec in specs:
        if spec.extra.get("asset_group") in _EXCLUDED_GROUPS:
            continue
        td = technicals.get(spec.slug)
        if not td or not td.data_available:
            continue

        asset_signals: list[str] = []

        if td.monthly_rsi is not None and td.monthly_rsi < 30:
            asset_signals.append(
                f"STRONG BUY: monthly RSI {td.monthly_rsi:.1f} (below 30 — historically rare entry)"
            )
        elif td.weekly_rsi is not None and td.weekly_rsi < 35:
            asset_signals.append(f"OVERSOLD: weekly RSI {td.weekly_rsi:.1f}")

        if td.macd_signal_type == "bullish_crossover":
            asset_signals.append("bullish MACD crossover on weekly bars")

        if td.price_vs_50w_ma_pct is not None and td.price_vs_50w_ma_pct < -30:
            asset_signals.append(
                f"deep undervaluation: {td.price_vs_50w_ma_pct:.1f}% below 50-week MA"
            )

        if asset_signals:
            target = spec.extra.get("target_weight_pct")
            target_str = f" (target {float(target):.0f}%)" if target is not None else ""
            signals.append(
                f"  {spec.display_name} ({spec.slug.upper()}): "
                + " | ".join(asset_signals)
                + f" | current weight {spec.weight_pct:.1f}%{target_str}"
            )

    if not signals:
        return "No oversold or buy signals detected in current holdings."
    return "HELD ASSETS WITH BUY/OVERSOLD SIGNALS:\n" + "\n".join(signals)


async def _correlation_analyst(
    cog,
    per_asset_results: dict,
    specs: list,
    concentration_flags: list,
    category: str,
) -> dict:
    """Run correlation analysis for crypto portfolios. Returns empty strings for other categories.

    Returns {"correlation_analysis": str, "correlation_summary": str} where
    correlation_summary is a single sentence for the condensed note (empty if not materially new).
    """
    from adapters.inference_queue import InferenceQueue

    if category != "crypto":
        return {"correlation_analysis": "", "correlation_summary": ""}

    today = date.today().isoformat()

    _SKIP_GROUPS = {"stablecoin", "disposal", "utility", "eth_derivative", "closed"}
    verdicts_block = "\n".join([
        f"{spec.display_name} ({spec.slug.upper()}): "
        f"{per_asset_results.get(spec.slug, {}).get('verdict', 'N/A')} "
        f"({per_asset_results.get(spec.slug, {}).get('confidence', '')}) | "
        f"weight {spec.weight_pct:.1f}% | "
        f"bull: {(per_asset_results.get(spec.slug, {}).get('bull_case', '') or '')[:80]} | "
        f"bear: {(per_asset_results.get(spec.slug, {}).get('bear_case', '') or '')[:80]}"
        for spec in specs
        if spec.extra.get("asset_group") not in _SKIP_GROUPS and spec.weight_pct >= 0
    ])

    flags_text = (
        "\n".join(f["rationale"] for f in concentration_flags)
        if concentration_flags
        else "None — all positions within target bands"
    )

    prompt = f"""You are a Correlation Analyst at a trading firm. Today is {today}.

PER-ASSET VERDICTS AND WEIGHTS:
{verdicts_block}

CONCENTRATION FLAGS:
{flags_text}

Assess the portfolio as a whole:

1. Genuine diversification:
   - Which positions are highly correlated and move together? (BTC/ETH/alts typically are)
   - Which positions provide genuine independent exposure?
   - Does the satellite allocation (SOL, LINK, YFI, DOT) provide real diversification or just leveraged beta to ETH?

2. Aggregate risk:
   - What is the dominant risk thesis across the portfolio?
   - If that thesis is wrong, what is the total downside scenario?

3. Single point of failure:
   - What one event or development would most damage this portfolio?

4. Diversification opportunity:
   - What addition would most improve the portfolio's risk distribution?
   - Consider: asset class, geography, correlation profile

Output structured assessment only. No overall verdict. This feeds into portfolio synthesis.
Be specific about correlation assumptions — do not assume correlation without stating your reasoning.

End your response with exactly this line (no extra text after it):
PORTFOLIO_CORRELATION_SUMMARY: <one sentence capturing the most important portfolio-level insight not already stated in the per-asset verdicts, or NONE if all findings restate per-asset verdicts>"""

    try:
        result = await cog.ask_local(prompt, priority=InferenceQueue.NORMAL, timeout=180.0)
    except Exception as exc:
        logger.warning("portfolio_harness: _correlation_analyst LLM failed: %s", exc)
        return {"correlation_analysis": "", "correlation_summary": ""}

    if result.get("status") == "llm_timeout":
        logger.warning("portfolio_harness: _correlation_analyst timed out")
        return {"correlation_analysis": "", "correlation_summary": ""}

    raw = result.get("response", "")

    # Extract and strip the summary marker line
    summary = ""
    analysis = raw
    summary_m = re.search(r'PORTFOLIO_CORRELATION_SUMMARY:\s*(.+?)$', raw, re.MULTILINE)
    if summary_m:
        extracted = summary_m.group(1).strip()
        if extracted.upper() != "NONE":
            summary = extracted
        analysis = raw[:summary_m.start()].strip()

    logger.info("portfolio_harness: _correlation_analyst complete — summary=%r", bool(summary))
    return {"correlation_analysis": analysis, "correlation_summary": summary}


# ── Stress tests ──────────────────────────────────────────────────────────────

def _stress_test_inputs(specs: list, per_asset_results: dict, portfolio_targets: dict) -> dict:
    """Pre-calculate all reference anchors for stress test prompts. Pure, synchronous, no LLM."""

    def _find(slug: str):
        return next((s for s in specs if s.slug == slug), None)

    eth = _find("eth")
    btc = _find("btc")

    stablecoin_total = sum(
        s.value_nzd for s in specs if s.extra.get("asset_group") == "stablecoin"
    )
    total_value = sum(s.value_nzd for s in specs)

    eth_value      = eth.value_nzd        if eth else 0.0
    eth_cost       = eth.cost_basis_nzd   if eth else 0.0
    eth_balance    = eth.balance          if eth else 0.0
    eth_weight     = eth.weight_pct       if eth else 0.0
    eth_price      = float((eth.extra or {}).get("current_price_nzd",
                            eth_value / eth_balance if eth_balance else 0)) if eth else 0.0
    eth_break_even = eth_cost / eth_balance if eth_balance else 0.0
    eth_pnl_nzd    = eth_value - eth_cost
    eth_pnl_pct    = (eth_pnl_nzd / eth_cost * 100.0) if eth_cost else 0.0

    btc_value  = btc.value_nzd      if btc else 0.0
    btc_cost   = btc.cost_basis_nzd if btc else 0.0
    btc_balance = btc.balance       if btc else 0.0
    btc_weight = btc.weight_pct     if btc else 0.0
    btc_price  = float((btc.extra or {}).get("current_price_nzd",
                        btc_value / btc_balance if btc_balance else 0)) if btc else 0.0

    # S1: -65% on risky holdings; stablecoins unaffected
    risky_holdings = total_value - stablecoin_total
    indicative_trough = risky_holdings * 0.35 + stablecoin_total

    # S2: ETH drops 50%, BTC flat
    portfolio_impact_s2 = eth_value * 0.50
    eth_at_50_drop      = eth_price * 0.50

    # S3: Correction price anchors + target weight gaps
    stablecoin_pct = (stablecoin_total / total_value * 100.0) if total_value else 0.0
    eth_at_35 = eth_price * 0.65
    eth_at_40 = eth_price * 0.60
    btc_at_35 = btc_price * 0.65
    btc_at_40 = btc_price * 0.60

    eth_targets    = (portfolio_targets.get("eth") or {})
    btc_targets    = (portfolio_targets.get("btc") or {})
    eth_target_pct = float(eth_targets.get("target_weight_pct", 0) or 0)
    btc_target_pct = float(btc_targets.get("target_weight_pct", 0) or 0)
    eth_gap        = eth_target_pct - eth_weight   # positive = underweight
    btc_gap        = btc_target_pct - btc_weight

    n_active = sum(
        1 for s in specs
        if s.extra.get("asset_group") not in
        ("stablecoin", "disposal", "utility", "closed", "eth_derivative")
        and s.weight_pct >= 0
    )

    return {
        "total_value": total_value, "stablecoin_total": stablecoin_total,
        "stablecoin_pct": stablecoin_pct, "n_active": n_active,
        "eth_value": eth_value, "eth_cost": eth_cost, "eth_balance": eth_balance,
        "eth_weight": eth_weight, "eth_price": eth_price, "eth_break_even": eth_break_even,
        "eth_pnl_nzd": eth_pnl_nzd, "eth_pnl_pct": eth_pnl_pct,
        "btc_value": btc_value, "btc_cost": btc_cost, "btc_weight": btc_weight,
        "btc_price": btc_price,
        "indicative_trough": indicative_trough,
        "portfolio_impact_s2": portfolio_impact_s2, "eth_at_50_drop": eth_at_50_drop,
        "eth_at_35": eth_at_35, "eth_at_40": eth_at_40,
        "btc_at_35": btc_at_35, "btc_at_40": btc_at_40,
        "eth_target_pct": eth_target_pct, "btc_target_pct": btc_target_pct,
        "eth_gap": eth_gap, "btc_gap": btc_gap,
    }


async def _run_stress_tests(
    cog,
    nanobot,
    specs: list,
    per_asset_results: dict,
    portfolio_targets: dict,
    note_id,
    health_score: int,
) -> None:
    """Run 3 stress test scenarios at LOW priority, append results to Nextcloud note.

    Spawned as asyncio.create_task() after main Telegram notify — non-blocking to Director.
    """
    from adapters.inference_queue import InferenceQueue

    today = date.today().isoformat()
    inp = _stress_test_inputs(specs, per_asset_results, portfolio_targets)

    total_value    = inp["total_value"]
    stable_total   = inp["stablecoin_total"]
    stable_pct     = inp["stablecoin_pct"]
    n_active       = inp["n_active"]
    eth_value      = inp["eth_value"]
    eth_cost       = inp["eth_cost"]
    eth_balance    = inp["eth_balance"]
    eth_weight     = inp["eth_weight"]
    eth_price      = inp["eth_price"]
    eth_break_even = inp["eth_break_even"]
    eth_pnl_nzd    = inp["eth_pnl_nzd"]
    eth_pnl_pct    = inp["eth_pnl_pct"]
    btc_value      = inp["btc_value"]
    btc_cost       = inp["btc_cost"]
    btc_weight     = inp["btc_weight"]
    btc_price      = inp["btc_price"]
    indicative_trough   = inp["indicative_trough"]
    portfolio_impact_s2 = inp["portfolio_impact_s2"]
    eth_at_50_drop = inp["eth_at_50_drop"]
    eth_at_35      = inp["eth_at_35"]
    eth_at_40      = inp["eth_at_40"]
    btc_at_35      = inp["btc_at_35"]
    btc_at_40      = inp["btc_at_40"]
    eth_target_pct = inp["eth_target_pct"]
    btc_target_pct = inp["btc_target_pct"]
    eth_gap        = inp["eth_gap"]
    btc_gap        = inp["btc_gap"]

    async def _run_scenario(prompt: str, label: str) -> tuple[str, dict]:
        try:
            result = await cog.ask_local(prompt, priority=InferenceQueue.LOW, timeout=180.0)
        except Exception as exc:
            logger.warning("portfolio_harness: [stress] %s LLM failed: %s", label, exc)
            return ("", {})
        if result.get("status") == "llm_timeout":
            logger.warning("portfolio_harness: [stress] %s timed out", label)
            return ("", {})
        raw = result.get("response", "")
        extracted: dict = {}
        fence_m = re.search(r'```json\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if fence_m:
            try:
                extracted = json.loads(fence_m.group(1))
            except json.JSONDecodeError:
                pass
        if not extracted:
            m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
            if m:
                try:
                    extracted = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        return (raw, extracted)

    s1_prompt = f"""You are a Risk Analyst stress-testing a crypto portfolio. Today is {today}.

CURRENT PORTFOLIO: NZD {total_value:,.0f} across {n_active} active positions
ETH:  NZD {eth_value:,.0f} | cost basis NZD {eth_cost:,.0f} | {eth_weight:.1f}% | {eth_balance:.4f} ETH
BTC:  NZD {btc_value:,.0f} | cost basis NZD {btc_cost:,.0f} | {btc_weight:.1f}%
DRY POWDER: NZD {stable_total:,.0f} ({stable_pct:.1f}%)
INDICATIVE TROUGH (−65% applied to non-stablecoin holdings): NZD {indicative_trough:,.0f}

Stress scenario: broad crypto market drawdown of −65% from current levels.
(Consistent with 2018 peak-to-trough and 2022 cycle. Scenario only — not a forecast.)

Assess:
1. Portfolio value at trough — refine the indicative figure above with any asset-specific assumptions
2. Which positions breach their cost basis, and by how much in NZD terms?
3. NZ tax-efficient drawdown response — specifically what NOT to do (avoid crystallising losses) and what to watch for (DCA windows, disposal candidate tax-loss harvesting)
4. At what price levels does systematic re-entry become compelling for ETH and BTC?

Be specific. Use actual NZD figures. Do not hedge with "it depends" — give a directional view.

End your response with exactly this JSON block:
```json
{{"portfolio_at_trough_nzd": <integer>, "assets_breaching_cost_basis": ["eth", "..."], "key_insight": "<one sentence>"}}
```"""

    s2_prompt = f"""You are a Risk Analyst stress-testing a portfolio. Today is {today}.

ETH POSITION: {eth_balance:.4f} ETH | NZD {eth_value:,.0f} | {eth_weight:.1f}% of portfolio
ETH COST BASIS: NZD {eth_cost:,.0f} total | NZD {eth_break_even:,.0f} per ETH (break-even price)
ETH CURRENT PRICE: NZD {eth_price:,.0f} per ETH
ETH UNREALISED P&L: {eth_pnl_pct:+.1f}% (NZD {eth_pnl_nzd:+,.0f})
ETH PRICE AT −50%: NZD {eth_at_50_drop:,.0f} per ETH
PORTFOLIO IMPACT AT −50% ETH: −NZD {portfolio_impact_s2:,.0f}

BTC POSITION: NZD {btc_value:,.0f} | {btc_weight:.1f}% of portfolio (held flat in this scenario)
REST OF PORTFOLIO: NZD {total_value - eth_value - btc_value:,.0f}

Stress scenario: ETH drops −50% while BTC is flat (BTC dominance surge, as seen 2022–2023).

Assess:
1. Total portfolio impact in NZD — show the calculation
2. Does ETH drop below cost basis in this scenario? Reference the break-even price of NZD {eth_break_even:,.0f} per ETH
3. Does this scenario change the portfolio thesis or represent a buying opportunity? State your reasoning
4. Does the {btc_weight:.1f}% BTC allocation provide meaningful partial hedge — quantify its offset in NZD terms

End your response with exactly this JSON block:
```json
{{"portfolio_impact_nzd": <integer>, "eth_breaks_cost_basis": <true|false>, "eth_break_even_price_nzd": <integer>, "key_insight": "<one sentence>"}}
```"""

    s3_prompt = f"""You are a Portfolio Analyst modelling dry powder deployment. Today is {today}.

DRY POWDER AVAILABLE: NZD {stable_total:,.0f} ({stable_pct:.1f}% of current portfolio)
CURRENT PRICES: ETH NZD {eth_price:,.0f} | BTC NZD {btc_price:,.0f}
PRICES AT −35%: ETH NZD {eth_at_35:,.0f} | BTC NZD {btc_at_35:,.0f}
PRICES AT −40%: ETH NZD {eth_at_40:,.0f} | BTC NZD {btc_at_40:,.0f}

CURRENT WEIGHTS: ETH {eth_weight:.1f}% | BTC {btc_weight:.1f}%
TARGET WEIGHTS:  ETH {eth_target_pct:.0f}% | BTC {btc_target_pct:.0f}%
WEIGHT GAP:      ETH {eth_gap:+.1f}pp | BTC {btc_gap:+.1f}pp

Scenario: broad correction of −35–40% from current levels. Dry powder sits in stablecoins.

Assess:
1. At what ETH and BTC prices would deploying NZD {stable_total:,.0f} bring both to their target weights? Show the maths
2. Is NZD {stable_total:,.0f} sufficient to close the weight gap to targets at these price levels?
3. Deployment priority — ETH or BTC first, and why (reference weight gaps and correlation profile)
4. Lump sum vs staged DCA across the correction — given NZ tax treatment (cost basis averaging) which approach is preferable?

End your response with exactly this JSON block:
```json
{{"eth_target_deploy_price_nzd": <integer>, "btc_target_deploy_price_nzd": <integer>, "deployment_strategy": "lump_sum|staged_dca", "key_insight": "<one sentence>"}}
```"""

    logger.info("portfolio_harness: [stress] starting 3 scenarios at LOW priority")
    s1_raw, s1_json = await _run_scenario(s1_prompt, "S1-crypto-winter")
    s2_raw, s2_json = await _run_scenario(s2_prompt, "S2-eth-shock")
    s3_raw, s3_json = await _run_scenario(s3_prompt, "S3-dry-powder")

    # ── Telegram stress summary ──────────────────────────────────────────────
    winter_value  = int(s1_json.get("portfolio_at_trough_nzd", indicative_trough))
    breach_assets = s1_json.get("assets_breaching_cost_basis", [])
    breach_count  = len(breach_assets)
    shock_impact  = int(s2_json.get("portfolio_impact_nzd", portfolio_impact_s2))
    eth_target_p  = int(s3_json.get("eth_target_deploy_price_nzd", 0))

    if any([s1_raw, s2_raw, s3_raw]):
        tg_lines = [
            f"📊 STRESS TESTS (main score: {health_score}/10):",
            f"❄️  Crypto winter (−65%): Portfolio → NZD {winter_value:,.0f} | {breach_count} position(s) breach cost basis",
            f"⚡ ETH shock (−50%): Impact −NZD {shock_impact:,.0f} | BTC partial hedge",
            (f"💧 Dry powder (−35–40%): NZD {stable_total:,.0f} available | ETH target ~NZD {eth_target_p:,.0f}"
             if eth_target_p else
             f"💧 Dry powder (−35–40%): NZD {stable_total:,.0f} available"),
            "Full stress analysis in Notes.",
        ]
        await _notify_telegram("\n".join(tg_lines))
    else:
        await _notify_telegram("Stress tests complete — full analysis in Notes.")

    # ── Update Nextcloud note ────────────────────────────────────────────────
    if note_id is None:
        logger.warning("portfolio_harness: [stress] no note_id — skipping note update")
        return

    stress_parts = []
    if s1_raw:
        stress_parts.append(f"## Stress Test: Crypto Winter (−65%)\n\n{s1_raw}")
    if s2_raw:
        stress_parts.append(f"## Stress Test: ETH Shock (−50%, BTC Flat)\n\n{s2_raw}")
    if s3_raw:
        stress_parts.append(f"## Stress Test: Dry Powder Deployment (−35–40%)\n\n{s3_raw}")

    if not stress_parts:
        logger.warning("portfolio_harness: [stress] no scenario outputs — skipping note update")
        return

    stress_md = "\n\n---\n\n".join(stress_parts)

    try:
        nb_read = await nanobot.run("openclaw-nextcloud", "notes_read", {"note_id": str(note_id)})
        read_result = nb_read.get("result") if nb_read.get("result") is not None else nb_read
        existing = read_result.get("content", "") if isinstance(read_result, dict) else ""
        updated  = existing.rstrip() + "\n\n---\n\n## Stress Tests\n\n" + stress_md
        nb_upd = await nanobot.run("openclaw-nextcloud", "notes_update", {
            "note_id": str(note_id),
            "content": updated,
        })
        upd_result = nb_upd.get("result") if nb_upd.get("result") is not None else nb_upd
        if isinstance(upd_result, dict) and upd_result.get("status") == "ok":
            logger.info("portfolio_harness: [stress] note %s updated with stress sections", note_id)
        else:
            logger.warning("portfolio_harness: [stress] note update failed: %s", upd_result)
    except Exception as exc:
        logger.warning("portfolio_harness: [stress] note update exception: %s", exc)


async def _synthesise_overall(
    cog, category: str, specs: list[AssetSpec],
    per_security: dict, total_value: float, total_cost: float,
    concentration_flags: list | None = None,
    technicals: dict | None = None,
    watchlist: list | None = None,
    portfolio_targets: dict | None = None,
    correlation_analysis: str | None = None,
) -> dict:
    """Overall cross-security synthesis with rebalancing intelligence."""
    from adapters.inference_queue import InferenceQueue

    today   = date.today().isoformat()
    pnl_pct = (((total_value - total_cost) / total_cost) * 100.0) if total_cost else 0.0
    targets = portfolio_targets or {}
    tech    = technicals or {}

    # ── Group weights ─────────────────────────────────────────────────────────
    def _grp_weight(group: str) -> float:
        return sum(
            s.weight_pct for s in specs
            if _get_asset_group(s, targets) == group and s.weight_pct >= 0
        )

    stable_total = sum(s.value_nzd for s in specs if s.extra.get("asset_group") == "stablecoin")
    stable_pct   = (stable_total / total_value * 100.0) if total_value else 0.0

    def _band_status(current: float, group: str) -> str:
        t = targets.get(group) or {}
        lo = float(t.get("rebalance_band_lower_pct", 0) or 0)
        hi = float(t.get("rebalance_band_upper_pct", 100) or 100)
        if current > hi:   return "overweight"
        if current < lo:   return "underweight"
        return "on target"

    # Dynamic allocation block — works for any category (crypto, retirement, property, etc.)
    alloc_lines: list[str] = []
    for _grp, _tgt_data in sorted(targets.items()):
        if not isinstance(_tgt_data, dict):
            continue
        _cur     = _grp_weight(_grp)
        _tgt_pct = float(_tgt_data.get("target_weight_pct", 0) or 0)
        _status  = _band_status(_cur, _grp)
        alloc_lines.append(
            f"{_grp.replace('_', ' ').title()}: {_tgt_pct:.0f}% target "
            f"(current: {_cur:.1f}%) — {_status}"
        )
    target_alloc_block = "\n".join(alloc_lines) if alloc_lines else "No target allocations configured."

    # ── Per-asset verdict lines; stablecoins → single dry-powder summary ──────
    _STABLE_GROUPS = {"stablecoin"}
    _SKIP_GROUPS   = {"disposal", "utility", "eth_derivative"}
    sec_lines: list[str] = []
    stable_parts: list[str] = []

    for spec in specs:
        ag = spec.extra.get("asset_group", "")
        if ag in _SKIP_GROUPS:
            continue
        if ag in _STABLE_GROUPS:
            stable_parts.append(f"{spec.display_name} NZD {spec.value_nzd:,.0f}")
            continue
        res       = per_security.get(spec.slug, {})
        verdict   = res.get("verdict", "N/A")
        conf      = res.get("confidence", "")
        rationale = res.get("rationale", "")
        bull      = res.get("bull_case", "")
        bear      = res.get("bear_case", "")
        pnl_nzd   = spec.value_nzd - spec.cost_basis_nzd
        pnl_p     = (pnl_nzd / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0
        spec_grp  = _get_asset_group(spec, targets)
        target_w  = spec.extra.get("target_weight_pct")
        wt_str    = (
            f"{spec.weight_pct:.1f}% (target {float(target_w):.0f}%, {_band_status(spec.weight_pct, spec_grp)})"
            if target_w is not None else f"{spec.weight_pct:.1f}%"
        )
        line = (
            f"{spec.display_name} ({spec.slug.upper()}): {verdict}"
            + (f" [{conf}]" if conf else "")
            + f" | P&L {pnl_p:+.1f}% | weight {wt_str}"
        )
        if rationale: line += f" | {rationale[:120]}"
        if bull:      line += f" | Bull: {bull[:80]}"
        if bear:      line += f" | Bear: {bear[:80]}"
        sec_lines.append(line)

    if stable_parts:
        sec_lines.append(
            f"DRY POWDER: NZD {stable_total:,.0f} ({' + '.join(stable_parts)}) — {stable_pct:.1f}% of portfolio"
        )

    # ── Concentration flags ───────────────────────────────────────────────────
    flags = concentration_flags or []
    flag_lines = (
        "\n".join(f"  [{f['direction'].upper()}] {f['rationale']}" for f in flags)
        if flags else "  All positions within target bands."
    )

    # ── Buy signals + watchlist ───────────────────────────────────────────────
    buy_signals_block = _build_buy_signals_block(specs, tech)
    wl = watchlist or []
    watchlist_block = (
        "\n".join(f"  {w['slug'].upper()}: {w['reason']}" for w in wl)
        if wl else "  No watchlist entries configured."
    )

    # ── Correlation context ───────────────────────────────────────────────────
    corr = (correlation_analysis or "").strip()
    corr_block = (
        f"\nCORRELATION ANALYSIS:\n{corr}\n"
        if corr
        else ""
    )

    # ── Prompt ────────────────────────────────────────────────────────────────
    prompt = f"""You are a Portfolio Manager conducting a periodic review. Today is {today}.
Review cadence: quarterly rebalancing, monthly purchases considered only on high-signal events.

PER-ASSET VERDICTS:
{chr(10).join(sec_lines)}

CONCENTRATION FLAGS:
{flag_lines}
{corr_block}
{buy_signals_block}

PORTFOLIO WATCHLIST (assets to consider on weakness):
{watchlist_block}

TOTAL PORTFOLIO: NZD {total_value:,.0f}
vs cost basis: NZD {total_cost:,.0f} ({pnl_pct:+.1f}%)
STABLECOIN DRY POWDER: NZD {stable_total:,.0f} ({stable_pct:.1f}% of portfolio)

NZ TAX CONTEXT:
Disposal events are taxable as ordinary income — no capital gains discount.
CARF reporting active from April 2026 — IRD has full visibility.
Prefer new capital allocation and yield redirection over disposals.
At 33% marginal rate: disposing NZD 10,000 of gains = ~NZD 3,300 tax.

TARGET ALLOCATIONS:
{target_alloc_block}

Produce:

1. Portfolio health score: 1–10 with one-sentence justification

2. Executive summary: 2–3 sentences on overall portfolio state,
   primary risk, and primary opportunity

3. Ranked rebalancing actions (maximum 5, ordered by priority):
   - What: specific — name the asset and the action
   - Why: reference the specific signal, concentration flag, or target deviation
   - How: new_capital | yield_redirection | partial_disposal
     (if partial_disposal, state estimated NZ tax cost at 33% marginal rate)
   - Timeline: immediate | next_purchase_event | quarterly_review | opportunistic_on_weakness

   Actions must address concentration flags directly.
   Prefer yield redirection and new capital over disposals.
   At least one action must address the largest concentration breach.
   If buy signals are present in held assets, include at least one accumulation action.

4. Accumulation opportunities (held assets only):
   List any held assets with active buy or oversold signals and briefly state the entry thesis.
   These are candidates for dry powder deployment or yield redirection.
   Omit this section entirely if no signals are active.

5. Key risk: the single biggest portfolio risk right now (one sentence)

6. Key opportunity: the single biggest opportunity right now (one sentence)

End with ONLY this JSON block — no text after it:
```json
{{
  "health_score": 7,
  "executive_summary": "...",
  "ranked_actions": [
    {{
      "action": "...",
      "reason": "...",
      "method": "yield_redirection",
      "tax_cost_nzd": null,
      "timeline": "next_purchase_event"
    }}
  ],
  "accumulation_opportunities": ["YFI — monthly RSI near 30, bullish MACD crossover"],
  "key_risk": "...",
  "key_opportunity": "..."
}}
```"""

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
        return {
            "health_score": 0, "executive_summary": "Overall synthesis failed.",
            "ranked_actions": [], "accumulation_opportunities": [],
            "key_risk": "", "key_opportunity": "", "full_report": "",
        }

    if result.get("status") == "llm_timeout":
        return {
            "health_score": 0, "executive_summary": "Overall synthesis timed out.",
            "ranked_actions": [], "accumulation_opportunities": [],
            "key_risk": "", "key_opportunity": "", "full_report": "",
        }

    raw = result.get("response", "")

    # ── Depth-aware JSON extraction with key validation ───────────────────────
    meta: dict = {}
    json_str: str | None = None

    fence_m = re.search(r'```json\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if fence_m:
        json_str = fence_m.group(1)
    else:
        hs_pos = raw.rfind('"health_score"')
        if hs_pos != -1:
            start = raw.rfind('{', 0, hs_pos)
            if start != -1:
                depth, in_str, escape = 0, False, False
                for i, ch in enumerate(raw[start:], start=start):
                    if escape:           escape = False;  continue
                    if ch == '\\' and in_str: escape = True; continue
                    if ch == '"':        in_str = not in_str; continue
                    if in_str:           continue
                    if ch == '{':        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            json_str = raw[start: i + 1]
                            break

    if json_str:
        try:
            candidate = json.loads(json_str)
            _EXPECTED = {"health_score", "ranked_actions", "accumulation_opportunities",
                         "key_risk", "key_opportunity"}
            missing = _EXPECTED - candidate.keys()
            if missing:
                logger.warning(
                    "portfolio_harness: overall synthesis JSON missing keys %s — using partial result",
                    sorted(missing),
                )
            meta = candidate
        except json.JSONDecodeError as exc:
            logger.warning("portfolio_harness: overall synthesis JSON parse failed: %s", exc)
    else:
        logger.warning("portfolio_harness: overall synthesis produced no JSON block")

    json_start = raw.rfind(json_str) if json_str else -1
    full_report = raw[:json_start].strip() if json_start > 0 else raw

    # Normalise ranked_actions: accept list[dict] or legacy list[str]
    raw_actions = meta.get("ranked_actions", []) or []
    ranked_actions: list[dict] = []
    for a in raw_actions:
        if isinstance(a, dict):
            ranked_actions.append(a)
        elif isinstance(a, str) and a:
            ranked_actions.append({
                "action": a, "reason": "", "method": "",
                "tax_cost_nzd": None, "timeline": "",
            })

    return {
        "health_score":               meta.get("health_score", 0),
        "executive_summary":          meta.get("executive_summary", ""),
        "ranked_actions":             ranked_actions,
        "accumulation_opportunities": meta.get("accumulation_opportunities", []) or [],
        "key_risk":                   meta.get("key_risk", ""),
        "key_opportunity":            meta.get("key_opportunity", ""),
        "full_report":                full_report,
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
                                  full_word_count: int = 0,
                                  note_title: str | None = None,
                                  note_id=None,
                                  full_report_path: str | None = None) -> str:
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
        _first = ranked[0]
        _action_text = _first.get("action", str(_first)) if isinstance(_first, dict) else str(_first)
        lines.append(f"Key action: {_action_text}")
    lines.append("")
    lines.append(f"Summary: ~{word_count} words | Full debate: ~{full_word_count} words")
    if note_title:
        id_str = f" (ID: {note_id})" if note_id else ""
        lines.append(f"Summary saved to Notes: <i>{note_title}</i>{id_str}")
    else:
        lines.append("(Note save failed — say <b>save portfolio</b> to retry.)")
    if full_report_path:
        lines.append(f"Full report: <code>{full_report_path}</code>")

    return "\n".join(lines)


# ── Nextcloud note content ─────────────────────────────────────────────────────

def _build_condensed_note(category: str, specs: list[AssetSpec],
                           per_security: dict, overall: dict,
                           total_value: float, total_cost: float,
                           full_report_path: str = "",
                           concentration_flags: list | None = None,
                           correlation_summary: str | None = None) -> str:
    """Build a condensed (~2-3K word) report for Nextcloud Notes.

    Contains verdicts, bullet rationale, overall synthesis, and ranked actions.
    Omits per-agent debate prose (that lives in the full report file).
    """
    today   = date.today().isoformat()
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
    if ranked:
        lines.append("**Ranked actions:**")
        for i, action in enumerate(ranked[:5], 1):
            if isinstance(action, dict):
                text     = action.get("action", "")
                reason   = action.get("reason", "")
                method   = action.get("method", "")
                timeline = action.get("timeline", "")
                tax      = action.get("tax_cost_nzd")
                line = f"{i}. **{text}**"
                if reason:   line += f" — {reason}"
                parts = []
                if method:   parts.append(method.replace("_", " "))
                if timeline: parts.append(timeline.replace("_", " "))
                if tax is not None: parts.append(f"est. tax NZD {tax:,.0f}")
                if parts:    line += f" *({', '.join(parts)})*"
                lines.append(line)
            else:
                lines.append(f"{i}. {action}")
        lines.append("")

    key_risk = overall.get("key_risk", "")
    key_opp  = overall.get("key_opportunity", "")
    if key_risk or key_opp:
        if key_risk: lines.append(f"**Key risk:** {key_risk}")
        if key_opp:  lines.append(f"**Key opportunity:** {key_opp}")
        lines.append("")

    # Correlation summary — single sentence, only if materially new
    _corr_summary = (correlation_summary or "").strip()
    if _corr_summary:
        lines.append(f"**Correlation:** {_corr_summary}")
        lines.append("")

    cf = concentration_flags or []
    if cf:
        lines.append("## Concentration Flags")
        lines.append("")
        for f in cf:
            direction = f["direction"].upper()
            lines.append(f"- **[{direction}]** {f['rationale']}")
        lines.append("")

    # Summary table
    lines.append("| Asset | Value | Weight | Cost Basis | P&L | Verdict |")
    lines.append("|-------|-------|--------|------------|-----|---------|")
    for spec in specs:
        res     = per_security.get(spec.slug, {})
        verdict = res.get("verdict", "N/A")
        pnl_p   = ((spec.value_nzd - spec.cost_basis_nzd) / spec.cost_basis_nzd * 100.0
                   if spec.cost_basis_nzd else 0.0)
        cost_str = f"NZD {spec.cost_basis_nzd:,.0f}" if spec.cost_basis_nzd else "N/A"
        pnl_str  = f"{pnl_p:+.1f}%" if spec.cost_basis_nzd else "N/A"
        src      = spec.extra.get("balance_source", "")
        src_label = _BALANCE_SOURCE_LABELS.get(src, src)
        src_tag  = f" _{src_label}_" if src and src not in ("", "live") else ""
        lines.append(
            f"| {spec.display_name} | NZD {spec.value_nzd:,.0f}{src_tag} | {_weight_str(spec.weight_pct)} "
            f"| {cost_str} | {pnl_str} | {verdict} |"
        )
    lines.append("")

    # Per-security: verdict + confidence + brief summary bullets + rationale
    lines.append("---")
    lines.append("")
    for spec in specs:
        res        = per_security.get(spec.slug, {})
        verdict    = res.get("verdict", "N/A")
        conf       = res.get("confidence", "")
        rationale  = res.get("rationale", "")
        summary    = res.get("summary", []) or []
        bull_case  = res.get("bull_case", "")
        bear_case  = res.get("bear_case", "")

        pnl_nzd_s  = spec.value_nzd - spec.cost_basis_nzd
        pnl_pct_s  = (pnl_nzd_s / spec.cost_basis_nzd * 100.0) if spec.cost_basis_nzd else 0.0
        val_line   = f"NZD {spec.value_nzd:,.0f} | {_weight_str(spec.weight_pct)}"
        if spec.cost_basis_nzd:
            val_line += f" | cost NZD {spec.cost_basis_nzd:,.0f} | {pnl_pct_s:+.1f}%"

        heading = f"### {spec.display_name} ({val_line}) — {verdict}"
        if conf:
            heading += f" [{conf}]"
        lines.append(heading)
        lines.append("")

        if rationale:
            lines.append(f"**Rationale:** {rationale}")
            lines.append("")
        for bullet in summary[:3]:
            lines.append(f"- {bullet}")
        if bull_case:
            lines.append(f"- **Bull:** {bull_case[:200]}")
        if bear_case:
            lines.append(f"- **Bear:** {bear_case[:200]}")
        if not rationale and not summary and not bull_case and not bear_case:
            lines.append("*No rationale extracted — see full report.*")
        lines.append("")

    accum_opps = overall.get("accumulation_opportunities", []) or []
    if accum_opps:
        lines.append("## Accumulation Opportunities")
        lines.append("")
        for opp in accum_opps:
            lines.append(f"- {opp}")
        lines.append("")

    if full_report_path:
        lines.append("---")
        lines.append("")
        lines.append(f"*Full agent debate: `{full_report_path}`*")
        lines.append("")
    lines.append("*Generated by Portfolio Analysis Harness.*")
    return "\n".join(lines)


def _build_full_report(category: str, specs: list[AssetSpec],
                          per_security: dict, overall: dict,
                          total_value: float, total_cost: float,
                          correlation_text: str | None = None) -> str:
    """Build the complete markdown report including all agent debate prose."""
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
        for i, action in enumerate(ranked[:5], 1):
            if isinstance(action, dict):
                text     = action.get("action", "")
                reason   = action.get("reason", "")
                method   = action.get("method", "")
                timeline = action.get("timeline", "")
                tax      = action.get("tax_cost_nzd")
                line = f"{i}. **{text}**"
                if reason:   line += f" — {reason}"
                parts = []
                if method:   parts.append(method.replace("_", " "))
                if timeline: parts.append(timeline.replace("_", " "))
                if tax is not None: parts.append(f"est. tax NZD {tax:,.0f}")
                if parts:    line += f" *({', '.join(parts)})*"
                lines.append(line)
            else:
                lines.append(f"{i}. {action}")
        lines.append("")

    key_risk   = overall.get("key_risk", "")
    key_opp    = overall.get("key_opportunity", "")
    accum_opps = overall.get("accumulation_opportunities", []) or []
    if key_risk: lines.append(f"**Key risk:** {key_risk}")
    if key_opp:  lines.append(f"**Key opportunity:** {key_opp}")
    if key_risk or key_opp: lines.append("")

    if accum_opps:
        lines.append("## Accumulation Opportunities")
        lines.append("")
        for opp in accum_opps:
            lines.append(f"- {opp}")
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

    # Correlation analysis — always present for crypto runs
    _corr_text = (correlation_text or "").strip()
    if _corr_text:
        lines.append("## Correlation Analysis")
        lines.append("")
        lines.append(_corr_text)
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

        specs             = resolve_result["specs"]
        total_val         = resolve_result["total_value_nzd"]
        total_cost        = resolve_result["total_cost_basis_nzd"]
        portfolio_targets = resolve_result.get("portfolio_targets", {})
        watchlist         = resolve_result.get("watchlist", [])
        session_id        = str(uuid.uuid4())

        # Concentration flags — deterministic, no LLM, computed once for all assets
        concentration_flags = _calculate_concentration_flags(specs, portfolio_targets)
        if concentration_flags:
            logger.info(
                "portfolio_harness: [task] concentration flags: %s",
                [f["rationale"] for f in concentration_flags],
            )

        logger.info("portfolio_harness: [task] gathering %d securities in parallel", len(specs))
        gather_results = await _gather_all(nanobot, cog, specs)
        gather_by_slug = {r["slug"]: r for r in gather_results}
        technicals     = {r["slug"]: r.get("td", _no_td(r["slug"])) for r in gather_results}

        logger.info("portfolio_harness: [task] synthesising %d securities sequentially", len(specs))
        per_security: dict[str, dict] = {}
        for spec in specs:
            synth = await _synthesise_security(
                cog, spec, gather_by_slug.get(spec.slug, {}),
                concentration_flags=concentration_flags,
            )
            per_security[spec.slug] = synth

        # Correlation analyst — crypto only; runs after per-asset, before overall synthesis
        logger.info("portfolio_harness: [task] correlation analysis")
        corr_result = await _correlation_analyst(
            cog, per_security, specs, concentration_flags, slug,
        )
        correlation_analysis  = corr_result["correlation_analysis"]
        correlation_summary   = corr_result["correlation_summary"]

        logger.info("portfolio_harness: [task] overall synthesis")
        overall = await _synthesise_overall(
            cog, slug, specs, per_security, total_val, total_cost,
            concentration_flags=concentration_flags,
            technicals=technicals,
            watchlist=watchlist,
            portfolio_targets=portfolio_targets,
            correlation_analysis=correlation_analysis,
        )

        # Write back to Nextcloud ledger
        try:
            wb_ok = await _write_back(nanobot, slug, specs, per_security)
            if not wb_ok:
                logger.warning("portfolio_harness: [task] write-back returned False")
        except Exception as exc:
            logger.warning("portfolio_harness: [task] write-back exception: %s", exc)

        # Build outputs — condensed note for Nextcloud Notes, full report for file
        today_str         = date.today().isoformat()
        full_report_path  = f"/portfolios/reports/{slug}-{today_str}-full.md"
        note_content      = _build_condensed_note(
            slug, specs, per_security, overall, total_val, total_cost,
            full_report_path=full_report_path,
            concentration_flags=concentration_flags,
            correlation_summary=correlation_summary,
        )
        full_report_md    = _build_full_report(
            slug, specs, per_security, overall, total_val, total_cost,
            correlation_text=correlation_analysis,
        )
        word_count        = len(note_content.split())
        full_word_count   = len(full_report_md.split())

        # Write full report to Nextcloud file (ensure reports/ dir exists first)
        full_report_saved = False
        try:
            mkdir_r = await nanobot.run("openclaw-nextcloud", "files_mkdir", {"path": "/portfolios/reports"})
            mkdir_res = mkdir_r.get("result") if mkdir_r.get("result") is not None else mkdir_r
            if mkdir_res.get("status") == "error":
                http_s = mkdir_res.get("http_status", 0)
                if http_s not in (405, 301):  # 405 = already exists (MKCOL idempotency)
                    logger.warning("portfolio_harness: [task] reports/ mkdir failed (HTTP %s): %s",
                                   http_s, mkdir_res.get("error", ""))
        except Exception as exc:
            logger.warning("portfolio_harness: [task] reports/ mkdir exception: %s", exc)
        try:
            fr = await nanobot.run("openclaw-nextcloud", "files_write", {
                "path":    full_report_path,
                "content": full_report_md,
            })
            if fr.get("status") == "ok":
                full_report_saved = True
                logger.info("portfolio_harness: [task] full report saved → %s", full_report_path)
            else:
                logger.warning("portfolio_harness: [task] full report save failed: %s", fr.get("error"))
        except Exception as exc:
            logger.warning("portfolio_harness: [task] full report save exception: %s", exc)

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

        # Auto-save condensed report to Nextcloud Notes
        note_title = f"Portfolio Analysis: {category.title()} ({today_str})"
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
            total_val, total_cost, word_count, full_word_count,
            note_title=note_title, note_id=note_id,
            full_report_path=full_report_path if full_report_saved else None,
        )
        await _notify_telegram(confirm_msg)

        # Spawn stress tests as background task — results arrive ~20 min later via Telegram
        if slug == "crypto" and portfolio_targets:
            asyncio.create_task(_run_stress_tests(
                cog=cog,
                nanobot=nanobot,
                specs=specs,
                per_asset_results=per_security,
                portfolio_targets=portfolio_targets,
                note_id=note_id,
                health_score=overall.get("health_score", 0),
            ))

    except Exception as exc:
        logger.error("portfolio_harness: [task] analysis failed: %s", exc, exc_info=True)
        await _notify_telegram(
            f"<b>Portfolio analysis failed</b>\n"
            f"Category: {category}\n"
            f"Error: {exc}"
        )
    finally:
        await _clear_running(qdrant)


# ── Weekly watcher ────────────────────────────────────────────────────────────

def _evaluate_signals(spec: AssetSpec, technicals, portfolio_targets: dict) -> list[dict]:
    """Deterministic signal evaluation for the weekly watcher. No LLM.

    Returns list of fired signal dicts. Empty list = clean week for this asset.
    Five signal types: STRONG_BUY, BUY, SELL_ALERT, CAUTION, DRIFT_ALERT.
    """
    signals: list[dict] = []
    if not technicals or not technicals.data_available:
        return signals

    group = _get_asset_group(spec, portfolio_targets)
    group_targets = portfolio_targets.get(group) or {}
    band_upper = float(group_targets.get("rebalance_band_upper_pct", 100) or 100)
    band_lower = float(group_targets.get("rebalance_band_lower_pct", 0) or 0)
    target_pct = float(group_targets.get("target_weight_pct", 0) or 0)

    # STRONG_BUY — monthly RSI < 30 (historically rare)
    if technicals.monthly_rsi is not None and technicals.monthly_rsi < 30:
        signals.append({
            "name": "STRONG_BUY",
            "description": f"Monthly RSI {technicals.monthly_rsi:.1f} — historically rare strong buy signal",
            "severity": "high",
        })
    elif (technicals.weekly_rsi is not None and technicals.weekly_rsi < 35
          and technicals.macd_signal_type == "bullish_crossover"):
        # BUY — weekly RSI oversold + bullish MACD crossover
        signals.append({
            "name": "BUY",
            "description": f"Weekly RSI {technicals.weekly_rsi:.1f} oversold with bullish MACD crossover",
            "severity": "medium",
        })

    # SELL_ALERT — weekly RSI > 75 while position is overweight
    if (technicals.weekly_rsi is not None and technicals.weekly_rsi > 75
            and spec.weight_pct > band_upper and band_upper < 100):
        signals.append({
            "name": "SELL_ALERT",
            "description": (
                f"Weekly RSI {technicals.weekly_rsi:.1f} overbought while {spec.display_name} "
                f"is overweight ({spec.weight_pct:.1f}% vs {band_upper:.0f}% upper band)"
            ),
            "severity": "medium",
        })

    # CAUTION — bearish MACD crossover on a significant position (>10% weight)
    if technicals.macd_signal_type == "bearish_crossover" and spec.weight_pct > 10:
        signals.append({
            "name": "CAUTION",
            "description": (
                f"Bearish MACD crossover on {spec.display_name} "
                f"({spec.weight_pct:.1f}% of portfolio)"
            ),
            "severity": "low",
        })

    # DRIFT_ALERT — weight >5% outside target band
    if target_pct and (spec.weight_pct > band_upper + 5 or spec.weight_pct < band_lower - 5):
        signals.append({
            "name": "DRIFT_ALERT",
            "description": (
                f"{spec.display_name} weight {spec.weight_pct:.1f}% significantly outside "
                f"target band ({band_lower:.0f}–{band_upper:.0f}%)"
            ),
            "severity": "medium",
        })

    return signals


async def run_portfolio_watcher_scan(cog, nanobot, qdrant, sov_wallet_url: str) -> dict:
    """Weekly deterministic RSI/MACD signal scan across all active crypto assets.

    No LLM unless a signal fires. On signal: immediate Telegram alert + single-asset
    6-agent engine at LOW priority + auto-save to Notes. Clean week: episodic log only.
    """
    today_str = date.today().isoformat()
    _sov_wallet_url = sov_wallet_url or "http://sov-wallet:3001"

    resolve = await resolve_category(nanobot, "crypto", qdrant, _sov_wallet_url)
    if resolve["status"] != "ok":
        logger.warning("portfolio_watcher: resolve_category failed: %s", resolve.get("message"))
        return {"status": "error", "message": resolve.get("message", "Could not resolve crypto portfolio")}

    specs             = resolve["specs"]
    portfolio_targets = resolve.get("portfolio_targets", {})

    _SKIP_GROUPS = {"stablecoin", "disposal", "utility", "closed", "eth_derivative"}
    signals_fired: list[dict] = []
    assets_scanned = 0

    for spec in specs:
        if spec.extra.get("asset_group") in _SKIP_GROUPS:
            continue
        assets_scanned += 1

        # Fetch technicals — pure HTTP, no GPU
        try:
            td = await _gather_technicals(spec.slug)
        except Exception as exc:
            logger.warning("portfolio_watcher: technicals fetch failed for %s: %s", spec.slug, exc)
            td = _no_td(spec.slug)

        # Evaluate signals deterministically — no LLM
        asset_signals = _evaluate_signals(spec, td, portfolio_targets)

        # Log weekly check to episodic regardless of signal
        try:
            await qdrant.store(
                collection="episodic",
                content=(
                    f"Weekly watcher scan — {spec.display_name} ({spec.slug.upper()}) "
                    f"on {today_str}: RSI weekly={td.weekly_rsi}, monthly={td.monthly_rsi}, "
                    f"MACD={td.macd_signal_type}, weight={spec.weight_pct:.1f}%, "
                    f"signals={[s['name'] for s in asset_signals]}"
                ),
                metadata={
                    "type":         "episodic",
                    "event_type":   "weekly_watcher_scan",
                    "asset":        spec.slug,
                    "weekly_rsi":   td.weekly_rsi,
                    "monthly_rsi":  td.monthly_rsi,
                    "macd_signal":  td.macd_signal_type,
                    "weight_pct":   spec.weight_pct,
                    "signals_fired": [s["name"] for s in asset_signals],
                    "ts":           datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("portfolio_watcher: episodic log failed for %s: %s", spec.slug, exc)

        if not asset_signals:
            continue

        # Signal fired — notify Director immediately
        signal_names       = " + ".join(s["name"] for s in asset_signals)
        signal_descriptions = "\n".join(f"• {s['description']}" for s in asset_signals)

        group = _get_asset_group(spec, portfolio_targets)
        gt    = portfolio_targets.get(group) or {}
        tgt   = float(gt.get("target_weight_pct", 0) or 0)
        b_lo  = float(gt.get("rebalance_band_lower_pct", 0) or 0)
        b_hi  = float(gt.get("rebalance_band_upper_pct", 100) or 100)

        rsi_w_str = f"{td.weekly_rsi:.1f}"  if td.weekly_rsi  is not None else "N/A"
        rsi_m_str = f"{td.monthly_rsi:.1f}" if td.monthly_rsi is not None else "N/A"

        await _notify_telegram(
            f"⚡ SIGNAL: {spec.display_name} — {signal_names}\n"
            f"RSI weekly: {rsi_w_str} | RSI monthly: {rsi_m_str}\n"
            f"MACD: {td.macd_signal_type or 'N/A'}\n"
            f"Weight: {spec.weight_pct:.1f}% vs target {tgt:.0f}% "
            f"(band {b_lo:.0f}–{b_hi:.0f}%)\n"
            f"{signal_descriptions}\n\n"
            f"Running full analysis… (~10 min)"
        )

        # Gather + run single-asset 6-agent engine at LOW priority
        try:
            concentration_flags = _calculate_concentration_flags(specs, portfolio_targets)
            gather_result       = await _gather_one(nanobot, cog, spec)
            synth               = await _synthesise_security(
                cog, spec, gather_result, concentration_flags=concentration_flags,
            )

            verdict_lines = [
                f"{spec.display_name} analysis: {synth.get('verdict', 'N/A')} ({synth.get('confidence', '')})",
                synth.get("rationale", ""),
            ]
            for bullet in (synth.get("summary") or [])[:3]:
                verdict_lines.append(f"• {bullet}")
            await _notify_telegram("\n".join(l for l in verdict_lines if l))

            # Save analysis to Nextcloud Notes
            note_title = f"Signal Analysis: {spec.display_name} ({today_str}) — {signal_names}"
            note_content = synth.get("full_report", f"Signal analysis for {spec.display_name}")
            try:
                nb = await nanobot.run("openclaw-nextcloud", "notes_create", {
                    "title":    note_title,
                    "content":  note_content,
                    "category": "Portfolio",
                })
                nb_r = nb.get("result") if nb.get("result") is not None else nb
                if isinstance(nb_r, dict) and nb_r.get("status") == "ok":
                    logger.info("portfolio_watcher: signal note saved for %s (id=%s)",
                                spec.slug, nb_r.get("id"))
                else:
                    logger.warning("portfolio_watcher: note save failed for %s: %s", spec.slug, nb_r)
            except Exception as exc:
                logger.warning("portfolio_watcher: note save exception for %s: %s", spec.slug, exc)

            signals_fired.append({
                "asset":   spec.slug,
                "signals": [s["name"] for s in asset_signals],
            })
        except Exception as exc:
            logger.error("portfolio_watcher: single-asset analysis failed for %s: %s",
                         spec.slug, exc, exc_info=True)
            await _notify_telegram(f"Signal analysis failed for {spec.display_name}: {exc}")

    if not signals_fired:
        logger.info("portfolio_watcher: clean scan — no signals fired across %d assets", assets_scanned)

    return {
        "status":         "ok",
        "signals_fired":  signals_fired,
        "assets_scanned": assets_scanned,
    }


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
