"""Tax Ingest Harness — NZD price enrichment via CoinGecko (direct HTTP).

CoinGecko free endpoint: /coins/{id}/history?date=DD-MM-YYYY
No API key required for the free tier. Timeout 12 s.
Failure → returns None; caller tags event pricing_unresolved and continues.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"

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


async def enrich_nzd(
    asset: str,
    timestamp: str,
    amount_decimal: "Decimal | None" = None,
) -> str | None:
    """Fetch NZD spot price from CoinGecko and return the NZD value for amount.

    Parameters
    ----------
    asset:          ticker string, e.g. "ETH"
    timestamp:      ISO8601 UTC event timestamp
    amount_decimal: Decimal amount of asset (multiplied by spot price → NZD value)

    Returns
    -------
    "$X.XX NZD" string, or None on any failure.
    NZD asset: caller must handle same-currency case (nzd_value = amount).
    """
    if asset.upper() == "NZD":
        return None  # caller handles same-currency

    coin_id = _ASSET_TO_COIN_ID.get(asset.upper())
    if not coin_id:
        logger.warning("pricing: no CoinGecko ID for asset %s — skip", asset)
        return None

    # CoinGecko /history requires DD-MM-YYYY
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        date_str = dt.strftime("%d-%m-%Y")
    except Exception as exc:
        logger.warning("pricing: cannot parse timestamp %r: %s", timestamp, exc)
        return None

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                f"{_COINGECKO_BASE}/coins/{coin_id}/history",
                params={"date": date_str, "localization": "false"},
            )
            resp.raise_for_status()
            data = resp.json()

        nzd_price = (
            data.get("market_data", {})
                .get("current_price", {})
                .get("nzd")
        )
        if nzd_price is None:
            logger.warning(
                "pricing: no NZD price in CoinGecko response for %s on %s",
                asset, date_str,
            )
            return None

        if amount_decimal is not None:
            nzd_value = Decimal(str(nzd_price)) * Decimal(str(amount_decimal))
            return f"${nzd_value:.2f} NZD"

        # No amount supplied — return per-unit price string
        return f"${Decimal(str(nzd_price)):.2f} NZD"

    except Exception as exc:
        logger.warning(
            "pricing: CoinGecko failed for %s on %s: %s", asset, date_str, exc
        )
        return None
