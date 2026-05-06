from __future__ import annotations

from typing import Any

import requests

import config


def item_score(items: list[str] | set[str] | tuple[str, ...]) -> float:
    return float(sum(config.ITEM_WEIGHTS.get(str(item), 0) for item in items))


def _quote_summary(symbol: str) -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    params = {"modules": "price,defaultKeyStatistics"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    result = (((data.get("quoteSummary") or {}).get("result")) or [None])[0]
    if not isinstance(result, dict):
        raise RuntimeError("No Yahoo quoteSummary result")
    return result


def _raw_value(obj: Any) -> int | float | None:
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, dict):
        value = obj.get("raw")
        if isinstance(value, (int, float)):
            return value
    return None


def is_tradeable(ticker: str, items: list[str] | set[str] | tuple[str, ...]) -> tuple[bool, float]:
    try:
        payload = _quote_summary(ticker)
    except Exception:
        return False, 0.0

    price = payload.get("price") or {}
    stats = payload.get("defaultKeyStatistics") or {}
    market_cap = _raw_value(price.get("marketCap")) or 0
    shares_float = _raw_value(stats.get("floatShares")) or float("inf")
    score = item_score(items)

    if market_cap <= 0 or market_cap > config.MAX_MARKET_CAP:
        return False, 0.0
    if shares_float > config.MAX_FLOAT_SHARES:
        return False, 0.0
    if set(items) == {"1.03"}:
        return False, 0.0

    return score > 0, score
