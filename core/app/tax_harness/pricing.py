"""Tax Ingest Harness — NZD price enrichment.

Primary source:  CoinGecko /coins/{id}/history (free tier; recent dates ~1 yr work fine;
                 older dates return 401 Unauthorized without a Pro API key).
Fallback source: Alpha Vantage DIGITAL_CURRENCY_DAILY (requires ALPHA_VANTAGE_API_KEY).
                 Fetches the full daily price series for the asset in one API call, then
                 looks up individual dates from the in-memory cache. 25 calls/day on the
                 free demo key — one call per asset per process lifetime is sufficient.

Cache policy: only SUCCESSFUL prices are cached. Failures are NOT cached so the next
harness run will retry both sources. Within a single run, the AV series cache means we
only hit the AV API once per asset even if many dates are needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

_COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
_CG_API_KEY_ENV    = "COINGECKO_API_KEY"
_AV_BASE        = "https://www.alphavantage.co/query"
_CALL_DELAY_S   = 0.4

# Per-unit price cache: "{coin_id}:{date_str(DD-MM-YYYY)}" → NZD price str.
# Only successful prices stored here — failures are NOT cached so retries happen each run.
_price_cache: dict[str, str] = {}

# Alpha Vantage full time-series cache per asset: "ETH" → {"YYYY-MM-DD": price_str}.
# Populated once per asset per process lifetime (one API call fetches all history).
_av_series_cache: dict[str, dict[str, str]] = {}

# Asset ticker → CoinGecko coin ID
_ASSET_TO_COIN_ID: dict[str, str] = {
    "ETH":   "ethereum",
    "BTC":   "bitcoin",
    "MATIC": "matic-network",
    "SOL":   "solana",
    "ADA":   "cardano",
    "DOT":   "polkadot",
    "LINK":  "chainlink",
    "UNI":   "uniswap",
}


async def _fetch_av_series(asset: str) -> dict[str, str]:
    """Fetch full daily ETH/NZD price series from Alpha Vantage.

    Returns dict mapping "YYYY-MM-DD" → NZD close price string.
    Cached per-asset for the process lifetime. Returns {} on any failure.
    """
    key = asset.upper()
    if key in _av_series_cache:
        return _av_series_cache[key]

    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        logger.debug("pricing: ALPHA_VANTAGE_API_KEY not set — AV fallback unavailable")
        _av_series_cache[key] = {}
        return {}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(_AV_BASE, params={
                "function": "DIGITAL_CURRENCY_DAILY",
                "symbol":   key,
                "market":   "NZD",
                "apikey":   api_key,
            })
            resp.raise_for_status()
            data = resp.json()

        series_raw = data.get("Time Series (Digital Currency Daily)", {})
        if not series_raw:
            logger.warning("pricing: AV returned no time series for %s/NZD", key)
            _av_series_cache[key] = {}
            return {}

        series: dict[str, str] = {}
        for date_key, values in series_raw.items():
            close_nzd = values.get(f"4a. close (NZD)")
            if close_nzd:
                series[date_key] = close_nzd

        logger.info("pricing: AV loaded %d daily prices for %s/NZD", len(series), key)
        _av_series_cache[key] = series
        return series

    except Exception as exc:
        logger.warning("pricing: AV series fetch failed for %s: %s", key, exc)
        _av_series_cache[key] = {}
        return {}


async def enrich_nzd(
    asset: str,
    timestamp: str,
    amount_decimal: "Decimal | None" = None,
) -> str | None:
    """Fetch NZD spot price from CoinGecko (with Alpha Vantage fallback).

    Parameters
    ----------
    asset:          ticker string, e.g. "ETH"
    timestamp:      ISO8601 UTC event timestamp
    amount_decimal: Decimal amount of asset (multiplied by spot price → NZD value)

    Returns
    -------
    "$X.XX NZD" string, or None on any failure.
    NZD asset: caller must handle same-currency case (nzd_value = amount).

    Cache: only successful prices are cached. Failures are NOT cached so the next
    harness run will retry. This means CoinGecko is called for each failed date on
    every run — acceptable given the small number of unique dates in a typical FY.
    """
    if asset.upper() == "NZD":
        return None

    coin_id = _ASSET_TO_COIN_ID.get(asset.upper())
    if not coin_id:
        logger.warning("pricing: no coin ID for asset %s — skip", asset)
        return None

    try:
        dt       = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        date_str = dt.strftime("%d-%m-%Y")   # CoinGecko format: DD-MM-YYYY
        av_date  = dt.strftime("%Y-%m-%d")   # Alpha Vantage format: YYYY-MM-DD
    except Exception as exc:
        logger.warning("pricing: cannot parse timestamp %r: %s", timestamp, exc)
        return None

    cache_key = f"{coin_id}:{date_str}"
    if cache_key in _price_cache:
        nzd_price = _price_cache[cache_key]
        if amount_decimal is not None:
            return f"${Decimal(nzd_price) * Decimal(str(amount_decimal)):.2f} NZD"
        return f"${Decimal(nzd_price):.2f} NZD"

    # ── Try CoinGecko ────────────────────────────────────────────────────────
    await asyncio.sleep(_CALL_DELAY_S)
    nzd_price_str: str | None = None
    try:
        cg_key = os.environ.get(_CG_API_KEY_ENV, "")
        cg_headers = {"x-cg-demo-api-key": cg_key} if cg_key else {}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                f"{_COINGECKO_BASE}/coins/{coin_id}/history",
                params={"date": date_str, "localization": "false"},
                headers=cg_headers,
            )
            resp.raise_for_status()
            data = resp.json()

        raw = data.get("market_data", {}).get("current_price", {}).get("nzd")
        if raw is not None:
            nzd_price_str = str(raw)
        else:
            logger.warning(
                "pricing: no NZD price in CoinGecko response for %s on %s",
                asset, date_str,
            )
    except Exception as exc:
        logger.warning("pricing: CoinGecko failed for %s on %s: %s", asset, date_str, exc)

    # ── Alpha Vantage fallback ───────────────────────────────────────────────
    if nzd_price_str is None:
        series = await _fetch_av_series(asset)
        nzd_price_str = series.get(av_date)
        if nzd_price_str:
            logger.debug(
                "pricing: AV resolved %s on %s → %s NZD/unit",
                asset, av_date, nzd_price_str,
            )
        else:
            logger.warning(
                "pricing: both CoinGecko and AV failed for %s on %s — will retry next run",
                asset, date_str,
            )
            return None  # Not cached — retried on next harness run

    # ── Cache and return ─────────────────────────────────────────────────────
    _price_cache[cache_key] = nzd_price_str
    if amount_decimal is not None:
        nzd_value = Decimal(nzd_price_str) * Decimal(str(amount_decimal))
        return f"${nzd_value:.2f} NZD"
    return f"${Decimal(nzd_price_str):.2f} NZD"
