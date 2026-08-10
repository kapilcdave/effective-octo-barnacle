"""Price lookups backed by Alpaca market data.

Previously Yahoo Finance, which now returns `429 Edge: Too Many Requests` for
unauthenticated callers on both the chart and quoteSummary endpoints. Every
caller here failed closed, so no signal could ever pass the scorer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import alpaca
import config


class PriceError(RuntimeError):
    pass


def _bars(symbol: str, *, timeframe: str, start: dt.datetime, limit: int = 10000) -> list:
    body = alpaca.data_request(
        "/v2/stocks/" + symbol.upper() + "/bars",
        params={
            "timeframe": timeframe,
            "start": start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "limit": limit,
            "adjustment": "split",
            "feed": config.STOCK_FEED,
        },
    )
    bars = body.get("bars") if isinstance(body, dict) else None
    return bars or []


def latest_close_and_close_24h_ago(symbol: str) -> tuple[float, float]:
    """Returns (latest_close, close_near_24h_ago) from hourly bars."""
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    bars = _bars(symbol, timeframe="1Hour", start=start)
    pairs: list = []
    for bar in bars:
        close = bar.get("c")
        ts = bar.get("t")
        if close is None or not ts:
            continue
        try:
            parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        pairs.append((parsed, float(close)))

    if len(pairs) < 2:
        raise PriceError("insufficient bar data for " + symbol)

    pairs.sort(key=lambda p: p[0])
    latest_t, latest_c = pairs[-1]
    target = latest_t - dt.timedelta(hours=24)
    best = min(pairs, key=lambda p: abs((p[0] - target).total_seconds()))
    return latest_c, best[1]


def close_on_or_after(symbol: str, base_ts: dt.datetime, days_after: int) -> float | None:
    """First daily close on/after base_ts + days_after, or None if not yet available."""
    if base_ts.tzinfo is None:
        base_ts = base_ts.replace(tzinfo=dt.timezone.utc)
    target_date = (base_ts.astimezone(dt.timezone.utc) + dt.timedelta(days=days_after)).date()

    bars = _bars(symbol, timeframe="1Day", start=base_ts - dt.timedelta(days=1), limit=90)
    for bar in bars:
        ts = bar.get("t")
        close = bar.get("c")
        if not ts or close is None:
            continue
        try:
            bar_date = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if bar_date >= target_date:
            return float(close)
    return None


def latest_price(symbol: str) -> float | None:
    """Latest trade price, falling back to the most recent minute bar close."""
    price = alpaca.latest_stock_trade_price(symbol)
    if price:
        return price
    bars = _bars(
        symbol,
        timeframe="1Min",
        start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5),
        limit=1000,
    )
    if not bars:
        return None
    last = bars[-1].get("c")
    return float(last) if last else None


def snapshot(symbol: str) -> dict[str, Any]:
    return alpaca.data_request("/v2/stocks/" + symbol.upper() + "/snapshot", params={"feed": config.STOCK_FEED})
