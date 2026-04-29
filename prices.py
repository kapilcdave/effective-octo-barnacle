from __future__ import annotations

import datetime as dt
from typing import Any

import requests


class PriceError(RuntimeError):
    pass


def _chart(symbol: str, *, rng: str, interval: str) -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "range": rng,
        "interval": interval,
        "includePrePost": "false",
        "events": "div,splits",
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or "chart" not in data:
        raise PriceError("Unexpected Yahoo chart response")
    err = data.get("chart", {}).get("error")
    if err:
        raise PriceError(str(err))
    return data


def latest_close_and_close_24h_ago(symbol: str) -> tuple[float, float]:
    """
    Uses 1h candles over ~5 days to approximate a 24h move.
    Returns (latest_close, close_near_24h_ago).
    """
    data = _chart(symbol, rng="5d", interval="1h")
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise PriceError("No chart result")

    ts = result.get("timestamp") or []
    closes = (
        (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    )
    if not ts or not closes or len(ts) != len(closes):
        raise PriceError("Missing timestamps/closes")

    pairs: list[tuple[int, float]] = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        pairs.append((int(t), float(c)))
    if len(pairs) < 2:
        raise PriceError("Insufficient candle data")

    latest_t, latest_c = pairs[-1]
    target_t = latest_t - 24 * 3600
    # Find candle timestamp closest to target_t.
    best = min(pairs, key=lambda p: abs(p[0] - target_t))
    return latest_c, best[1]


def close_on_or_after(symbol: str, base_ts: dt.datetime, days_after: int) -> float | None:
    """
    Uses 1d candles over a 1mo range; returns the first close on/after base_date+days_after.
    """
    data = _chart(symbol, rng="1mo", interval="1d")
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    ts = result.get("timestamp") or []
    closes = (
        (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    )
    if not ts or not closes:
        return None

    base_date = base_ts.astimezone(dt.UTC).date()
    target_date = base_date + dt.timedelta(days=days_after)

    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(int(t), tz=dt.UTC).date()
        if d >= target_date:
            return float(c)
    return None

