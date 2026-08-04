"""Sovereign Daily Technicals + BTC.D Alt-Season Watcher

Daily-resolution sibling to research_harness.py's weekly/monthly technicals path.
Builds its own daily price history (Yahoo Finance) and, for the first time, tracks
BTC dominance (BTC.D, CoinGecko) to fire an alt-season signal on a declining cross
below 60%/55%. Additive — does not change the existing weekly/monthly path.

Public entry point:
  run_daily_technicals_capture(qdrant) -> dict

Gap-marker vocabulary (reused verbatim from WorldMonitor's staleness-gate
discipline — see worldmonitor_memory.py): data_available/gap/age_known/
assertable/suppressed/gate_reason. Gap markers are written to the same `_key`
slot a real sample would use, so the next successful run's upsert-by-key
naturally overwrites it (self-healing).
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from config import cfg as _cfg
from monitoring.research_harness import (
    YAHOO_SYMBOLS,
    _calculate_macd,
    _calculate_rsi,
    _classify_macd_crossover,
    _classify_volume_trend,
    _price_vs_ma,
)
from monitoring.portfolio_analysis_harness import _notify_telegram

logger = logging.getLogger(__name__)

# Same namespace UUID used everywhere else for deterministic META points
# (rextrics, worldmonitor snapshot, structural-synthesis cursor).
_NAMESPACE = uuid.UUID("7d3f1c2a-4b5e-6f7a-8c9d-0e1f2a3b4c5d")
_ZERO_VEC = [0.0] * 768

# eth_btc_ratio is derived from the eth/btc legs already captured in the same run —
# it is never fetched as its own Yahoo call in the daily path (unlike the weekly path).
_DERIVED_DAILY_SLUGS = {"eth_btc_ratio"}

_DOMINANCE_HEALTH_KEY = "meta:dominance_source_health:last_alert"
_TRIGGER_STATE_KEY = "meta:dominance_trigger:state"


def _gap_marker(reason: str) -> dict:
    return {
        "data_available": False,
        "gap": True,
        "age_known": True,
        "assertable": False,
        "suppressed": False,
        "gate_reason": reason,
    }


async def _upsert_meta(qdrant, key: str, fields: dict) -> None:
    """Direct zero-vector META upsert — same deterministic-UUID5 pattern as rextrics.py."""
    from qdrant_client.models import PointStruct
    point_id = str(uuid.uuid5(_NAMESPACE, key))
    now = datetime.now(timezone.utc).isoformat()
    await qdrant.archive_client.upsert(
        collection_name="meta",
        points=[PointStruct(
            id=point_id,
            vector=_ZERO_VEC,
            payload={"_key": key, "last_updated": now, **fields},
        )],
    )


# ── Price snapshots ────────────────────────────────────────────────────────────

def _parse_daily_bars(data: dict) -> list[dict]:
    """Extract per-day {date, close, volume} dicts from a Yahoo v8 chart response.

    Distinct from research_harness._parse_bars, which drops per-bar dates — that
    function only needs closes for weekly/monthly RSI/MACD calculation. This daily
    path needs each bar's own date to write it to its own date-keyed slot.
    """
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return []
    r = result[0]
    timestamps = r.get("timestamp", []) or []
    quote = (r.get("indicators") or {}).get("quote") or [{}]
    q = quote[0]
    closes  = q.get("close",  []) or []
    volumes = q.get("volume", []) or []
    bars = []
    for ts, c, v in zip(timestamps, closes, volumes):
        if c is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        bars.append({"date": d, "close": c, "volume": v or 0})
    return bars


async def _fetch_daily_bars(symbol: str) -> list[dict]:
    """Fetch trailing 5 daily bars from Yahoo Finance v8 chart API. Never raises."""
    import httpx as _httpx
    base = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with _httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            r = await client.get(base, params={"interval": "1d", "range": "5d", "includePrePost": "false"})
        r.raise_for_status()
        return _parse_daily_bars(r.json())
    except Exception as exc:
        logger.warning("_fetch_daily_bars: failed for %s — %s", symbol, exc)
        return []


async def _write_price_gap(qdrant, slug: str, date_str: str, reason: str) -> None:
    await qdrant.store(
        content=f"Daily price snapshot for {slug} on {date_str}: gap — {reason}",
        collection="episodic",
        metadata={
            "event_type": "price_snapshot",
            "slug": slug,
            "date": date_str,
            **_gap_marker(reason),
            "_key": f"price_snapshot:{slug}:{date_str}",
        },
    )


async def _capture_price_snapshot(qdrant, slug: str, symbol: str) -> dict:
    """Fetch trailing daily bars for one asset and upsert each to its own date-keyed slot.

    Self-healing: writing all ~5 returned days (not just today) means a single missed
    run naturally backfills once the next run's 5-day window covers the gap. On total
    fetch failure, writes exactly one gap marker for today's date only — never touches
    days outside the failed attempt's window. The qdrant.store() calls are wrapped
    locally (not left to the top-level gather's return_exceptions backstop) so a
    persist failure downgrades this one asset to status="error" with a real reason,
    rather than surfacing as an opaque exception with no diagnostic value.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    bars = await _fetch_daily_bars(symbol)
    if not bars:
        try:
            await _write_price_gap(qdrant, slug, today, f"fetch_failed: no bars returned for {symbol}")
        except Exception as exc:
            logger.warning("_capture_price_snapshot: gap-write failed for %s — %s", slug, exc)
            return {"slug": slug, "status": "error", "error": str(exc) or type(exc).__name__}
        return {"slug": slug, "status": "gap", "date": today}

    try:
        for bar in bars:
            await qdrant.store(
                content=(
                    f"Daily price snapshot for {slug} ({symbol}) on {bar['date']}: "
                    f"close {bar['close']}, volume {bar['volume']}."
                ),
                collection="episodic",
                metadata={
                    "event_type": "price_snapshot",
                    "slug": slug,
                    "symbol": symbol,
                    "date": bar["date"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "source": "yahoo_finance",
                    "data_available": True,
                    "gap": False,
                    "_key": f"price_snapshot:{slug}:{bar['date']}",
                },
            )
    except Exception as exc:
        logger.warning("_capture_price_snapshot: store failed for %s — %s", slug, exc)
        return {"slug": slug, "status": "error", "error": str(exc) or type(exc).__name__}
    return {"slug": slug, "status": "ok", "days_written": len(bars)}


# Bounds fan-out to Yahoo + the embed/qdrant write path. Live-tested at full
# unbounded concurrency (35 assets) and found to blow ~80% of fetches out with
# httpx.ReadTimeout — bursting that many simultaneous requests at the same
# upstream host (and, on the gap-write path, at ollama-embed) is not safe in
# practice despite being "just HTTP". Matches the existing sovereign-browser
# precedent (capped at 4 after an observed 18-deep-burst empty-result incident).
_CAPTURE_CONCURRENCY = 6


async def _capture_all_price_snapshots(qdrant) -> list[dict]:
    """Bounded-concurrency fetch for all wired assets."""
    assets = [
        (slug, symbol) for slug, symbol in YAHOO_SYMBOLS.items()
        if symbol is not None and slug not in _DERIVED_DAILY_SLUGS
    ]
    sem = asyncio.Semaphore(_CAPTURE_CONCURRENCY)

    async def _bounded(slug: str, symbol: str) -> dict:
        async with sem:
            return await _capture_price_snapshot(qdrant, slug, symbol)

    results = await asyncio.gather(
        *[_bounded(slug, symbol) for slug, symbol in assets],
        return_exceptions=True,
    )
    out = []
    for (slug, _symbol), res in zip(assets, results):
        if isinstance(res, Exception):
            logger.warning("_capture_all_price_snapshots: %s raised %s", slug, res)
            out.append({"slug": slug, "status": "error", "error": str(res) or type(res).__name__})
        else:
            out.append(res)
    return out


async def _derive_eth_btc_ratio_snapshot(qdrant, date_str: str) -> dict:
    """Derive eth_btc_ratio from the already-captured eth/btc legs — never fetched as a
    third redundant Yahoo ticker. Gaps propagate: if either leg is missing or is itself
    a gap marker, the ratio is written as a gap too, never silently computed from a
    partial leg. Empirically validated against Yahoo's own direct ETH-BTC quote (see
    plan) — matches within ~0.03% on real days, and both sides go None together on a
    genuinely missing day.
    """
    eth = await qdrant.retrieve_by_key(f"price_snapshot:eth:{date_str}")
    btc = await qdrant.retrieve_by_key(f"price_snapshot:btc:{date_str}")

    eth_gap = eth is None or eth.get("gap")
    btc_gap = btc is None or btc.get("gap")

    if eth_gap or btc_gap:
        reason = "leg_gap: both" if (eth_gap and btc_gap) else ("leg_gap: eth" if eth_gap else "leg_gap: btc")
        await _write_price_gap(qdrant, "eth_btc_ratio", date_str, reason)
        return {"slug": "eth_btc_ratio", "status": "gap", "date": date_str}

    ratio = eth["close"] / btc["close"]
    await qdrant.store(
        content=f"Daily ETH/BTC ratio derived for {date_str}: {ratio}.",
        collection="episodic",
        metadata={
            "event_type": "price_snapshot",
            "slug": "eth_btc_ratio",
            "date": date_str,
            "close": ratio,
            "volume": 0,
            "source": "derived_from_legs",
            "data_available": True,
            "gap": False,
            "_key": f"price_snapshot:eth_btc_ratio:{date_str}",
        },
    )
    return {"slug": "eth_btc_ratio", "status": "ok", "ratio": ratio}


# ── Daily technicals read path (Build 2) ─────────────────────────────────────────

@dataclass
class DailyTechnicalData:
    """Sibling to research_harness.TechnicalData, scaled for daily rather than weekly/monthly
    bars — kept as its own type rather than reusing TechnicalData since that type's field
    names (weekly_rsi, price_vs_50w_ma_pct) would misrepresent daily-resolution values.
    """
    symbol:             str
    daily_rsi:          float | None
    macd_line:          float | None
    macd_signal:        float | None
    macd_histogram:     float | None
    macd_signal_type:   str | None    # "bullish_crossover" | "bearish_crossover" | "neutral"
    price_vs_ma20_pct:  float | None
    volume_trend:       str | None    # "increasing" | "decreasing" | "neutral"
    data_available:     bool


def _no_daily_td(slug: str) -> "DailyTechnicalData":
    return DailyTechnicalData(
        symbol=slug, data_available=False,
        daily_rsi=None, macd_line=None, macd_signal=None, macd_histogram=None,
        macd_signal_type=None, price_vs_ma20_pct=None, volume_trend=None,
    )


async def _query_daily_bars(qdrant, event_type: str, slug: str | None = None,
                             limit: int = 100) -> list[dict]:
    """Scroll accumulated daily records for one event_type (optionally filtered by slug),
    gap markers excluded, sorted date ascending — mirrors research_harness.
    _query_technical_trend's scroll+sort shape. Shared by both the per-asset price_snapshot
    series and the single dominance_snapshot series (slug=None).
    """
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue
    must = [FieldCondition(key="event_type", match=MatchValue(value=event_type))]
    if slug is not None:
        must.append(FieldCondition(key="slug", match=MatchValue(value=slug)))
    points, _ = await qdrant.archive_client.scroll(
        collection_name="episodic",
        scroll_filter=Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    bars = [p.payload for p in points if not p.payload.get("gap")]
    bars.sort(key=lambda b: b.get("date", ""))
    return bars


_MIN_RSI_SAMPLES = 15    # _calculate_rsi(period=14) needs period+1 closes
_MIN_MACD_SAMPLES = 35   # _calculate_macd default needs slow(26)+signal(9)


async def _gather_technicals_daily(qdrant, slug: str) -> "DailyTechnicalData":
    """Sibling to research_harness._gather_technicals — reads from the daily price_snapshot /
    dominance_snapshot store built by Build 1, never fetches.

    Degrade gate: data_available=True once >=15 daily closes exist (enough for RSI-14);
    macd_* stay None/"neutral" until >=35 samples exist. Below 15 samples: returns the full
    sentinel silently, no exception, no alert — mirrors _no_td()'s short-circuit-on-unmapped-
    slug shape. No signal-band evaluation here (compute + log only) — a full daily alerting
    layer is future work once Build 4's shadow-compare validates these numbers are trustworthy.
    """
    slug = slug.lower()
    if slug == "btc_dominance":
        bars = await _query_daily_bars(qdrant, "dominance_snapshot")
        value_key, symbol, has_volume = "btc_dominance_pct", "BTC.D", False
    else:
        symbol = YAHOO_SYMBOLS.get(slug)
        if symbol is None:
            return _no_daily_td(slug)
        bars = await _query_daily_bars(qdrant, "price_snapshot", slug=slug)
        value_key, has_volume = "close", True

    closes = [b[value_key] for b in bars if value_key in b]
    if len(closes) < _MIN_RSI_SAMPLES:
        return _no_daily_td(slug)

    daily_rsi     = _calculate_rsi(closes, period=14)
    price_vs_ma20 = _price_vs_ma(closes, 20)
    volume_trend  = _classify_volume_trend(bars) if has_volume else None

    macd_line = macd_signal = macd_histogram = None
    macd_signal_type = "neutral"
    if len(closes) >= _MIN_MACD_SAMPLES:
        macd_line, macd_signal, macd_histogram, prev_hist = _calculate_macd(closes)
        macd_signal_type = _classify_macd_crossover(prev_hist, macd_histogram)

    return DailyTechnicalData(
        symbol=symbol,
        daily_rsi=daily_rsi,
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=macd_histogram,
        macd_signal_type=macd_signal_type,
        price_vs_ma20_pct=price_vs_ma20,
        volume_trend=volume_trend,
        data_available=True,
    )


# ── BTC dominance ───────────────────────────────────────────────────────────────

async def _fetch_btc_dominance(qdrant) -> float | None:
    """Fetch BTC dominance % from CoinGecko /global, resolved from the MIP-stored
    endpoint (semantic:provider:coingecko:global — first real consumer of a
    MIP-stored endpoint anywhere in the codebase). Never raises — any failure
    (missing MIP entry, HTTP error, malformed JSON, missing key) returns None.
    """
    import httpx as _httpx
    try:
        endpoint = await qdrant.retrieve_by_key("semantic:provider:coingecko:global")
        url = (endpoint or {}).get("value") or "https://api.coingecko.com/api/v3/global"
        headers = {}
        api_key = os.environ.get("COINGECKO_API_KEY")
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        async with _httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        return data["data"]["market_cap_percentage"]["btc"]
    except Exception as exc:
        logger.warning("_fetch_btc_dominance: failed — %s", exc)
        return None


async def _check_dominance_source_health(qdrant) -> None:
    """Fire a one-time-per-incident alert if the last 3 dominance_snapshot records
    are all gap markers — a dead upstream source is itself worth alerting on, not
    just silently degrading (mirrors WorldMonitor's staleness-surfaces-as-signal
    discipline). Dedup via a last-alerted-date marker so this doesn't re-fire every
    day the outage continues, only on first crossing the 3-day threshold.
    """
    try:
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        points, _ = await qdrant.archive_client.scroll(
            collection_name="episodic",
            scroll_filter=Filter(must=[
                FieldCondition(key="event_type", match=MatchValue(value="dominance_snapshot")),
            ]),
            limit=3,
            with_payload=True,
            with_vectors=False,
        )
        if len(points) < 3:
            return
        points.sort(key=lambda p: p.payload.get("date", ""), reverse=True)
        last_3 = points[:3]
        if not all(p.payload.get("gap") for p in last_3):
            return

        newest_gap_date = last_3[0].payload.get("date")
        already_alerted = await qdrant.retrieve_by_key(_DOMINANCE_HEALTH_KEY)
        if already_alerted and already_alerted.get("alerted_for_date") == newest_gap_date:
            return

        await _notify_telegram(
            "⚠️ BTC.D dominance source has failed 3 days running (CoinGecko /global "
            "unreachable). Daily alt-season watcher has no fresh dominance data."
        )
        await _upsert_meta(qdrant, _DOMINANCE_HEALTH_KEY, {"alerted_for_date": newest_gap_date})
    except Exception as exc:
        logger.warning("_check_dominance_source_health: failed — %s", exc)


async def _capture_dominance_snapshot(qdrant) -> dict:
    """Write dominance_snapshot:{date} or a gap marker. Never raises — a dominance
    failure never aborts the rest of the daily capture pass. Calls the alt-season
    trigger evaluator on a successful write only (a gap marker never feeds the
    trigger — there is no real value to evaluate).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    pct = await _fetch_btc_dominance(qdrant)

    if pct is None:
        await qdrant.store(
            content=f"BTC dominance snapshot for {today}: gap — dominance fetch failed.",
            collection="episodic",
            metadata={
                "event_type": "dominance_snapshot",
                "date": today,
                **_gap_marker("fetch_failed: coingecko /global unavailable"),
                "_key": f"dominance_snapshot:{today}",
            },
        )
        await _check_dominance_source_health(qdrant)
        return {"status": "gap", "date": today}

    await qdrant.store(
        content=f"BTC dominance snapshot for {today}: {pct:.2f}%.",
        collection="episodic",
        metadata={
            "event_type": "dominance_snapshot",
            "date": today,
            "btc_dominance_pct": pct,
            "source": "coingecko_global",
            "data_available": True,
            "gap": False,
            "_key": f"dominance_snapshot:{today}",
        },
    )
    await _check_dominance_source_health(qdrant)
    await _evaluate_alt_season_trigger(qdrant, pct)
    return {"status": "ok", "date": today, "btc_dominance_pct": pct}


# ── Alt-season trigger (evaluate-on-write) ───────────────────────────────────────

async def _evaluate_alt_season_trigger(qdrant, dominance_value: float) -> None:
    """Notify-then-persist state machine for the BTC.D alt-season signal.

    Ordering is deliberate, not stylistic: the Telegram send happens BEFORE the
    corresponding fired flag is persisted. If the process crashes or the send fails
    between compute and persist, the flag stays False and the next run's evaluation
    re-attempts the fire — a duplicate alert is a far better failure mode than a
    signal permanently swallowed by a state flag that says "already handled" when
    the Director was never actually told.
    """
    state = await qdrant.retrieve_by_key(_TRIGGER_STATE_KEY) or {}
    armed      = state.get("armed", False)
    fired_60   = state.get("fired_60", False)
    fired_55   = state.get("fired_55", False)
    last_value = state.get("last_value")
    last_fired = state.get("last_fired")

    slope = (dominance_value - last_value) if last_value is not None else 0.0

    alert_high = _cfg.dominance.alert_high
    alert_low  = _cfg.dominance.alert_low
    rearm      = _cfg.dominance.rearm

    # Re-arm — unconditional, checked first. No Telegram fire on this branch.
    if dominance_value >= rearm:
        armed, fired_60, fired_55 = True, False, False

    # 60-cross — notify, then persist only if the send actually succeeded.
    if armed and dominance_value < alert_high and slope < 0 and not fired_60:
        try:
            await _notify_telegram(
                f"🔔 ALT_SEASON_SIGNAL: BTC dominance crossed below {alert_high:.0f}% "
                f"({dominance_value:.2f}%, declining) — first alt-season tier.",
                raise_on_failure=True,
            )
            fired_60 = True
            last_fired = datetime.now(timezone.utc).date().isoformat()
        except Exception as exc:
            logger.warning(
                "_evaluate_alt_season_trigger: 60-tier notify failed, will retry next run — %s", exc
            )

    # 55-cross — independent of fired_60 (a large single-day drop can skip past 55
    # directly); same notify-then-persist ordering.
    if armed and dominance_value < alert_low and slope < 0 and not fired_55:
        try:
            await _notify_telegram(
                f"🔔 ALT_SEASON_SIGNAL (second alert): BTC dominance crossed below "
                f"{alert_low:.0f}% ({dominance_value:.2f}%, declining).",
                raise_on_failure=True,
            )
            fired_55 = True
            last_fired = datetime.now(timezone.utc).date().isoformat()
        except Exception as exc:
            logger.warning(
                "_evaluate_alt_season_trigger: 55-tier notify failed, will retry next run — %s", exc
            )

    # last_value/last_updated persisted unconditionally so slope calc stays current
    # next run — no ordering constraint here, it isn't gating an alert.
    await _upsert_meta(qdrant, _TRIGGER_STATE_KEY, {
        "armed": armed,
        "fired_60": fired_60,
        "fired_55": fired_55,
        "last_value": dominance_value,
        "last_fired": last_fired,
    })


# ── Shadow-compare (Build 4) ──────────────────────────────────────────────────────
#
# Raw daily-vs-weekly RSI/MACD divergence is expected and not itself meaningful — a 14-day
# RSI and a 14-week RSI measure fundamentally different things. The only comparison that
# means anything, and the only one stored, is daily-resampled-to-weekly vs Yahoo-weekly
# (research_harness.py's existing technical_snapshot record, read back — never re-fetched).

_MIN_WEEKLY_RSI_SAMPLES  = 15   # weekly bars, not daily — _calculate_rsi(period=14) needs 15
_MIN_WEEKLY_MACD_SAMPLES = 35   # weekly bars — _calculate_macd default needs slow(26)+signal(9)


def _resample_to_weekly(daily_bars: list[dict]) -> list[dict]:
    """Groups date-ascending daily price_snapshot bars by ISO week, keeping each week's
    last close (last bar wins per week since the input is already date-ascending). Pure
    function — no I/O, no side effects.
    """
    weeks: dict[str, dict] = {}
    for b in daily_bars:
        d = datetime.fromisoformat(b["date"]).date()
        iso_year, iso_week, _ = d.isocalendar()
        weeks[f"{iso_year}-W{iso_week:02d}"] = b
    return [{"week": wk, "date": b["date"], "close": b["close"]} for wk, b in weeks.items()]


async def _query_latest_technical_snapshot(qdrant, slug: str) -> dict | None:
    """Reads back the most recent weekly/monthly technical_snapshot record already written
    by research_harness.py's existing path — the shadow-compare comparand. Never re-fetches
    Yahoo. Distinct field name from price_snapshot's "date" — technical_snapshot uses
    "snapshot_date" (see research_harness._store_technical_snapshot).
    """
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue
    points, _ = await qdrant.archive_client.scroll(
        collection_name="episodic",
        scroll_filter=Filter(must=[
            FieldCondition(key="event_type", match=MatchValue(value="technical_snapshot")),
            FieldCondition(key="slug",       match=MatchValue(value=slug)),
        ]),
        limit=10,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return None
    points.sort(key=lambda p: p.payload.get("snapshot_date", ""), reverse=True)
    return points[0].payload


async def _run_shadow_compare(qdrant, slug: str) -> dict | None:
    """Resamples the accumulated daily price_snapshot series to weekly and diffs against
    the existing weekly technical_snapshot path. Returns None (no store, no error) if there
    isn't yet enough daily history to resample to 15+ weekly bars, or no weekly comparand
    exists yet — this is the expected steady state for a long while after Build 1 first
    starts accumulating history, not a fault condition.
    """
    daily_bars = await _query_daily_bars(qdrant, "price_snapshot", slug=slug)
    weekly_bars = _resample_to_weekly(daily_bars)
    weekly_closes = [w["close"] for w in weekly_bars]

    if len(weekly_closes) < _MIN_WEEKLY_RSI_SAMPLES:
        return None

    comparand = await _query_latest_technical_snapshot(qdrant, slug)
    if comparand is None:
        return None

    resampled_rsi = _calculate_rsi(weekly_closes, period=14)
    resampled_macd_line = resampled_macd_signal = resampled_macd_histogram = None
    if len(weekly_closes) >= _MIN_WEEKLY_MACD_SAMPLES:
        resampled_macd_line, resampled_macd_signal, resampled_macd_histogram, _ = \
            _calculate_macd(weekly_closes)

    comparand_rsi = comparand.get("weekly_rsi")
    comparand_macd_line = comparand.get("macd_line")
    comparand_date = comparand.get("snapshot_date")

    rsi_delta = (
        round(resampled_rsi - comparand_rsi, 2)
        if resampled_rsi is not None and comparand_rsi is not None else None
    )
    macd_line_delta = (
        round(resampled_macd_line - comparand_macd_line, 6)
        if resampled_macd_line is not None and comparand_macd_line is not None else None
    )
    comparand_date_offset_days = None
    if comparand_date:
        try:
            comparand_date_offset_days = (
                datetime.now(timezone.utc).date() - datetime.fromisoformat(comparand_date).date()
            ).days
        except ValueError:
            pass

    today = datetime.now(timezone.utc).date().isoformat()
    record = {
        "event_type": "shadow_compare",
        "slug": slug,
        "date": today,
        "resampled_weekly_rsi": resampled_rsi,
        "resampled_macd_line": resampled_macd_line,
        "resampled_macd_signal": resampled_macd_signal,
        "resampled_macd_histogram": resampled_macd_histogram,
        "comparand_weekly_rsi": comparand_rsi,
        "comparand_macd_line": comparand_macd_line,
        "comparand_date": comparand_date,
        "comparand_date_offset_days": comparand_date_offset_days,
        "rsi_delta": rsi_delta,
        "macd_line_delta": macd_line_delta,
        "_key": f"shadow_compare:{slug}:{today}",
    }
    await qdrant.store(
        content=(
            f"Shadow-compare for {slug} on {today}: resampled weekly RSI {resampled_rsi} "
            f"vs weekly-path RSI {comparand_rsi} (delta {rsi_delta}); resampled MACD line "
            f"{resampled_macd_line} vs weekly-path MACD line {comparand_macd_line} "
            f"(delta {macd_line_delta}). Comparand from {comparand_date}."
        ),
        collection="episodic",
        metadata=record,
    )
    return record


async def _run_all_shadow_compares(qdrant) -> list[dict]:
    """Sequential per-asset shadow-compare. Deliberately not concurrency-bounded like
    _capture_all_price_snapshots — this is a Qdrant scroll+store path (no external HTTP
    fetch per asset), so the Build 1 ReadTimeout-storm risk doesn't apply here; sequential
    matches the existing weekly watcher's own precedent for store/embed-heavy paths.
    Per-asset failures are isolated (logged, skipped) so one bad asset never aborts the rest.
    """
    assets = [slug for slug, symbol in YAHOO_SYMBOLS.items() if symbol is not None]
    results = []
    for slug in assets:
        try:
            r = await _run_shadow_compare(qdrant, slug)
            if r is not None:
                results.append(r)
        except Exception as exc:
            logger.warning("_run_all_shadow_compares: %s failed — %s", slug, exc)
    return results


# ── Completeness reminder (Build 5) ───────────────────────────────────────────────
#
# No data-condition schedule type exists in task_scheduler.py (compute_next_due() supports
# only cron/interval/one_time) — so this condition is checked inline in the daily capture
# task itself, and on first-met writes a one-off PROSPECTIVE proposal rather than a new
# recurring schedule. Matches self_improvement.py's _write_proposal()/
# _existing_pending_proposal() and cognition/subjects.py's propose_successor_thesis()
# precedents. Cannot realistically fire for ~4 years from a cold start.

_COMPLETENESS_MONTHS = 48
_COMPLETENESS_DAYS = _COMPLETENESS_MONTHS * 30  # threshold only, not a calendar calculation


async def _query_all_price_records(qdrant, slug: str, limit: int = 2000) -> list[dict]:
    """Raw scroll of ALL price_snapshot records for slug, including gap markers — unlike
    _query_daily_bars, which filters them out. The completeness check needs to detect the
    presence of a gap, not just read real closes.
    """
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue
    points, _ = await qdrant.archive_client.scroll(
        collection_name="episodic",
        scroll_filter=Filter(must=[
            FieldCondition(key="event_type", match=MatchValue(value="price_snapshot")),
            FieldCondition(key="slug",       match=MatchValue(value=slug)),
        ]),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    bars = [p.payload for p in points]
    bars.sort(key=lambda b: b.get("date", ""))
    return bars


async def _existing_completeness_proposal(qdrant, slug: str) -> bool:
    """True if a data_completeness_proposal for this slug already exists, in any status —
    mirrors self_improvement.py's _existing_pending_proposal() dedup-guard pattern. Checked
    before writing so this doesn't re-fire every day once the condition is first met; no
    status filter (unlike that precedent) since this condition never reverts to unmet once
    true, so "ever proposed" is a correct forever-dedup here.
    """
    from execution.adapters.qdrant import PROSPECTIVE
    from qdrant_client.http.models import Filter, FieldCondition, MatchValue
    try:
        points, _ = await qdrant.archive_client.scroll(
            collection_name=PROSPECTIVE,
            scroll_filter=Filter(must=[
                FieldCondition(key="type", match=MatchValue(value="data_completeness_proposal")),
                FieldCondition(key="slug", match=MatchValue(value=slug)),
            ]),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(points) > 0
    except Exception as exc:
        logger.warning("_existing_completeness_proposal: dedup check failed for %s — %s", slug, exc)
        return False  # fail-open — matches self_improvement.py's own precedent


async def _check_completeness_reminder(qdrant) -> None:
    """Per wired asset, checks whether ~48 months of gapless daily price_snapshot history
    has accumulated — the trigger for eventually cutting the daily technicals path off
    Yahoo's 5-day rolling window onto a self-sufficient history. On first-met, writes a
    one-off `data_completeness_proposal` to PROSPECTIVE (status=pending_approval, dedup-
    guarded). Never raises — a failure on one asset never blocks the rest of the check
    or the daily capture pass it runs inside.
    """
    from execution.adapters.qdrant import PROSPECTIVE
    assets = [slug for slug, symbol in YAHOO_SYMBOLS.items() if symbol is not None]

    for slug in assets:
        try:
            records = await _query_all_price_records(qdrant, slug)
            if not records:
                continue
            oldest_date = records[0].get("date")
            if not oldest_date:
                continue
            age_days = (
                datetime.now(timezone.utc).date() - datetime.fromisoformat(oldest_date).date()
            ).days
            if age_days < _COMPLETENESS_DAYS:
                continue
            if any(r.get("gap") for r in records):
                continue  # not gapless — a real gap exists somewhere in the window

            if await _existing_completeness_proposal(qdrant, slug):
                continue

            await qdrant.store(
                content=(
                    f"Data completeness reached for {slug}: {age_days} days of gapless daily "
                    f"price history accumulated (since {oldest_date}). Candidate to cut over "
                    f"the daily technicals path off Yahoo's 5-day rolling window onto this "
                    f"self-sufficient history."
                ),
                collection=PROSPECTIVE,
                metadata={
                    "type": "data_completeness_proposal",
                    "slug": slug,
                    "status": "pending_approval",
                    "oldest_date": oldest_date,
                    "age_days": age_days,
                    "created_ts": datetime.now(timezone.utc).isoformat(),
                    "_key": f"prospective:data_completeness:{slug}",
                },
            )
        except Exception as exc:
            logger.warning("_check_completeness_reminder: failed for %s — %s", slug, exc)


# ── Task entry point ─────────────────────────────────────────────────────────────

async def run_daily_technicals_capture(qdrant) -> dict:
    """Daily orchestrator: parallel price-snapshot capture for all wired assets →
    ETH/BTC leg-derived ratio → BTC.D dominance snapshot + alt-season evaluate-on-write →
    per-asset shadow-compare (resampled-daily-to-weekly vs the existing weekly path) →
    completeness-reminder check (~48-month gapless history, one-off PROSPECTIVE proposal).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    price_results = await _capture_all_price_snapshots(qdrant)
    ratio_result = await _derive_eth_btc_ratio_snapshot(qdrant, today)
    dominance_result = await _capture_dominance_snapshot(qdrant)
    shadow_results = await _run_all_shadow_compares(qdrant)
    await _check_completeness_reminder(qdrant)

    gap_count = sum(1 for r in price_results if r.get("status") in ("gap", "error"))
    if ratio_result.get("status") == "gap":
        gap_count += 1
    if dominance_result.get("status") == "gap":
        gap_count += 1

    return {
        "status": "ok",
        "date": today,
        "count": gap_count,
        "assets_captured": len(price_results),
        "dominance": dominance_result,
        "eth_btc_ratio": ratio_result,
        "shadow_compares_written": len(shadow_results),
    }
