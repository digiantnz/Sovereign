"""Tax Ingest Harness — NZD price enrichment.

Primary source:  CoinGecko /coins/{id}/history (free tier; recent dates ~1 yr work fine;
                 older dates return 401 Unauthorized without a Pro API key).
Fallback source: CryptoCompare histoday (free, no API key, NZD native pair, history to 2015).
                 Per-date call — one request per unique date per process lifetime via cache.

Cache policy: only SUCCESSFUL prices are cached. Failures are NOT cached so the next
harness run will retry both sources.
"""
from __future__ import annotations

import asyncio
import logging
import os  # os.environ for COINGECKO_API_KEY
from datetime import datetime
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_CG_API_KEY_ENV = "COINGECKO_API_KEY"
_CC_BASE        = "https://min-api.cryptocompare.com/data/v2/histoday"
_CALL_DELAY_S   = 0.4

# Per-unit price cache: "{coin_id}:{date_str(DD-MM-YYYY)}" → NZD price str.
# Only successful prices stored here — failures are NOT cached so retries happen each run.
_price_cache: dict[str, str] = {}

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


async def _fetch_cryptocompare(asset: str, timestamp: str) -> str | None:
    """Fetch historical NZD close price from CryptoCompare.

    Free endpoint, no API key, NZD native pair, full history to 2015.
    Returns NZD price string, or None on any failure.
    """
    try:
        dt      = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        unix_ts = int(dt.timestamp())
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                _CC_BASE,
                params={"fsym": asset.upper(), "tsym": "NZD", "toTs": unix_ts, "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()
        close = (data.get("Data", {}).get("Data") or [{}])[-1].get("close")
        if close is not None:
            return str(close)
    except Exception as exc:
        logger.warning("pricing: CryptoCompare failed for %s on %s: %s", asset, timestamp[:10], exc)
    return None


async def enrich_nzd(
    asset: str,
    timestamp: str,
    amount_decimal: "Decimal | None" = None,
) -> str | None:
    """Fetch NZD spot price from CoinGecko (with CryptoCompare fallback).

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

    # ── CryptoCompare fallback ───────────────────────────────────────────────
    if nzd_price_str is None:
        nzd_price_str = await _fetch_cryptocompare(asset, timestamp)
        if nzd_price_str:
            logger.debug(
                "pricing: CryptoCompare resolved %s on %s → %s NZD/unit",
                asset, date_str, nzd_price_str,
            )
        else:
            logger.warning(
                "pricing: both CoinGecko and CryptoCompare failed for %s on %s — will retry next run",
                asset, date_str,
            )
            return None  # Not cached — retried on next harness run

    # ── Cache and return ─────────────────────────────────────────────────────
    _price_cache[cache_key] = nzd_price_str
    if amount_decimal is not None:
        nzd_value = Decimal(nzd_price_str) * Decimal(str(amount_decimal))
        return f"${nzd_value:.2f} NZD"
    return f"${Decimal(nzd_price_str):.2f} NZD"
