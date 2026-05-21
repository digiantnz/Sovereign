"""Alpha Vantage financial data adapter.

All methods return {"status": "ok", "data": {...}, "_trust": "untrusted_external"}
or {"status": "error", "error": "...", "data": None, "_trust": "untrusted_external"}.
Route through research_harness or a dedicated intent — never call directly from the cognitive loop.
"""

import os
import httpx

AV_BASE_URL = "https://www.alphavantage.co/query"
TIMEOUT = 12.0


class AlphaVantageAdapter:
    def __init__(self):
        self._api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")

    def _ok(self, data: dict) -> dict:
        return {"status": "ok", "data": data, "_trust": "untrusted_external"}

    def _err(self, msg: str) -> dict:
        return {"status": "error", "error": msg, "data": None, "_trust": "untrusted_external"}

    async def _get(self, params: dict) -> dict:
        if not self._api_key:
            return self._err("ALPHA_VANTAGE_API_KEY not configured")
        params["apikey"] = self._api_key
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(AV_BASE_URL, params=params)
            r.raise_for_status()
            return r.json()

    async def get_quote(self, symbol: str) -> dict:
        """Real-time/latest quote via GLOBAL_QUOTE."""
        try:
            raw = await self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
            if isinstance(raw, dict) and raw.get("status") == "error":
                return raw
            note = raw.get("Note") or raw.get("Information")
            if note:
                return self._err(note)
            q = raw.get("Global Quote", {})
            if not q.get("01. symbol"):
                return self._err(f"no quote data for {symbol}")
            return self._ok({
                "symbol": q.get("01. symbol"),
                "price": q.get("05. price"),
                "change": q.get("09. change"),
                "change_pct": q.get("10. change percent"),
                "volume": q.get("06. volume"),
                "latest_trading_day": q.get("07. latest trading day"),
                "prev_close": q.get("08. previous close"),
            })
        except Exception as exc:
            return self._err(str(exc))

    async def get_technicals(
        self, symbol: str, indicator: str = "RSI", interval: str = "daily"
    ) -> dict:
        """Technical indicator — RSI, MACD, BBANDS, ADX, SMA, EMA."""
        try:
            raw = await self._get({
                "function": indicator.upper(),
                "symbol": symbol,
                "interval": interval,
                "time_period": 14,
                "series_type": "close",
            })
            if isinstance(raw, dict) and raw.get("status") == "error":
                return raw
            note = raw.get("Note") or raw.get("Information")
            if note:
                return self._err(note)
            key = f"Technical Analysis: {indicator.upper()}"
            data = raw.get(key, {})
            if not data:
                return self._err(f"no {indicator} data for {symbol}")
            latest_date = max(data.keys())
            return self._ok({
                "symbol": symbol,
                "indicator": indicator.upper(),
                "interval": interval,
                "latest_date": latest_date,
                "values": data[latest_date],
            })
        except Exception as exc:
            return self._err(str(exc))

    async def get_fundamentals(self, symbol: str) -> dict:
        """Company overview — P/E, EPS, market cap, sector, description."""
        try:
            raw = await self._get({"function": "OVERVIEW", "symbol": symbol})
            if isinstance(raw, dict) and raw.get("status") == "error":
                return raw
            note = raw.get("Note") or raw.get("Information")
            if note:
                return self._err(note)
            if not raw.get("Symbol"):
                return self._err(f"no fundamental data for {symbol}")
            keys = [
                "Symbol", "Name", "Description", "Sector", "Industry",
                "MarketCapitalization", "PERatio", "EPS", "DividendYield",
                "52WeekHigh", "52WeekLow", "Beta", "ProfitMargin",
                "ReturnOnEquityTTM", "RevenuePerShareTTM", "QuarterlyEarningsGrowthYOY",
            ]
            return self._ok({k: raw[k] for k in keys if raw.get(k)})
        except Exception as exc:
            return self._err(str(exc))

    async def get_news_sentiment(self, tickers: str) -> dict:
        """News sentiment for one or more tickers (comma-separated)."""
        try:
            raw = await self._get({
                "function": "NEWS_SENTIMENT",
                "tickers": tickers,
                "limit": 10,
            })
            if isinstance(raw, dict) and raw.get("status") == "error":
                return raw
            note = raw.get("Note") or raw.get("Information")
            if note:
                return self._err(note)
            feed = raw.get("feed", [])
            if not feed:
                return self._err(f"no news sentiment data for {tickers}")
            articles = [
                {
                    "title": a.get("title"),
                    "source": a.get("source"),
                    "published": a.get("time_published"),
                    "overall_sentiment": a.get("overall_sentiment_label"),
                    "overall_score": a.get("overall_sentiment_score"),
                    "url": a.get("url"),
                }
                for a in feed[:10]
            ]
            return self._ok({"tickers": tickers, "count": len(articles), "articles": articles})
        except Exception as exc:
            return self._err(str(exc))

    async def get_commodity_price(self, commodity: str = "WTI") -> dict:
        """Commodity price — WTI, BRENT, NATURAL_GAS, COPPER, WHEAT, CORN etc."""
        try:
            raw = await self._get({"function": commodity.upper(), "interval": "monthly"})
            if isinstance(raw, dict) and raw.get("status") == "error":
                return raw
            note = raw.get("Note") or raw.get("Information")
            if note:
                return self._err(note)
            data = raw.get("data", [])
            if not data:
                return self._err(f"no commodity data for {commodity}")
            latest = data[0]
            return self._ok({
                "commodity": commodity.upper(),
                "date": latest.get("date"),
                "value": latest.get("value"),
                "unit": raw.get("unit"),
                "name": raw.get("name"),
            })
        except Exception as exc:
            return self._err(str(exc))

    async def get_economic_indicator(self, indicator: str = "REAL_GDP") -> dict:
        """Macroeconomic indicator — REAL_GDP, CPI, INFLATION, FEDERAL_FUNDS_RATE, UNEMPLOYMENT etc."""
        try:
            raw = await self._get({"function": indicator.upper(), "interval": "annual"})
            if isinstance(raw, dict) and raw.get("status") == "error":
                return raw
            note = raw.get("Note") or raw.get("Information")
            if note:
                return self._err(note)
            data = raw.get("data", [])
            if not data:
                return self._err(f"no data for {indicator}")
            latest = data[0]
            return self._ok({
                "indicator": indicator.upper(),
                "date": latest.get("date"),
                "value": latest.get("value"),
                "unit": raw.get("unit"),
                "name": raw.get("name"),
            })
        except Exception as exc:
            return self._err(str(exc))

    async def health_check(self) -> dict:
        return await self.get_quote("IBM")
