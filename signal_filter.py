"""Universe filter: which tickers are worth trading.

This used to gate on market cap <= $500M and float <= 50M shares via Yahoo
quoteSummary. Two problems: Yahoo now edge-blocks unauthenticated callers (429),
so the filter rejected everything; and a sub-$500M universe is incompatible with
options trading, because those names either have no listed contracts at all or
quote spreads far too wide to trade.

Alpaca serves neither market cap nor float, so the universe is now defined the
way an options strategy actually needs it: the asset must be active, tradable,
option-enabled, and liquid enough by average dollar volume.
"""

from __future__ import annotations

import datetime as dt

import alpaca
import config


def item_score(items) -> float:
    return float(sum(config.ITEM_WEIGHTS.get(str(item), 0) for item in items))


def _asset(ticker: str) -> dict | None:
    try:
        return alpaca.trading_request("GET", "/v2/assets/" + ticker.upper())
    except alpaca.AlpacaCredentialsError:
        # Never downgrade a config problem into "ticker not eligible".
        raise
    except alpaca.AlpacaError:
        return None


def _avg_dollar_volume(ticker: str, days: int) -> float | None:
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days * 2 + 10)
    try:
        body = alpaca.data_request(
            "/v2/stocks/" + ticker.upper() + "/bars",
            params={
                "timeframe": "1Day",
                "start": start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "limit": days * 2 + 10,
                "feed": config.STOCK_FEED,
            },
        )
    except alpaca.AlpacaCredentialsError:
        raise
    except alpaca.AlpacaError:
        return None

    bars = (body.get("bars") if isinstance(body, dict) else None) or []
    recent = bars[-days:] if len(bars) > days else bars
    values = []
    for bar in recent:
        close = bar.get("vw") or bar.get("c")
        volume = bar.get("v")
        if close and volume:
            values.append(float(close) * float(volume))
    if not values:
        return None
    return sum(values) / len(values)


def is_optionable(asset: dict) -> bool:
    """Alpaca tags option-eligible underlyings with the `options_enabled` attribute."""
    attributes = asset.get("attributes")
    if not isinstance(attributes, list) or not attributes:
        # Field absent entirely: cannot verify, so do not reject on this basis.
        return True
    return "options_enabled" in [str(a) for a in attributes]


def is_tradeable(ticker: str, items) -> tuple[bool, float]:
    """Returns (tradeable, item_score_bonus)."""
    symbol = str(ticker).upper().strip()
    score = item_score(items)

    # A bankruptcy filing on its own is not a bullish narrative.
    if set(str(i) for i in items) == {"1.03"}:
        return False, 0.0

    asset = _asset(symbol)
    if asset is None:
        return False, 0.0
    if str(asset.get("status")) != "active" or not asset.get("tradable"):
        return False, 0.0
    if str(asset.get("class") or asset.get("asset_class")) != "us_equity":
        return False, 0.0
    if config.REQUIRE_OPTIONABLE and not is_optionable(asset):
        return False, 0.0

    if config.MIN_AVG_DOLLAR_VOLUME > 0:
        adv = _avg_dollar_volume(symbol, config.DOLLAR_VOLUME_DAYS)
        if adv is None or adv < config.MIN_AVG_DOLLAR_VOLUME:
            return False, 0.0

    return score > 0, score
