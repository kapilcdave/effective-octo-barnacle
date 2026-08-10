"""Option contract discovery and selection.

Strategy: single-leg long calls, 30-45 DTE, delta ~0.35-0.50, with liquidity
guards on open interest and bid/ask spread.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict
from typing import Any

import alpaca
import config


class NoContractError(RuntimeError):
    """No contract passed the selection filters."""


@dataclass
class Candidate:
    symbol: str
    underlying: str
    contract_type: str
    expiration_date: str
    strike: float
    dte: int
    open_interest: int
    bid: float
    ask: float
    mid: float
    spread_pct: float
    delta: float | None
    iv: float | None
    underlying_price: float

    @property
    def cost_per_contract(self) -> float:
        """Premium in dollars for one contract (100 shares)."""
        return self.mid * 100.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["cost_per_contract"] = round(self.cost_per_contract, 2)
        return data


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def parse_expiration(value: str) -> dt.date:
    return dt.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def days_to_expiry(expiration_date: str, today: dt.date | None = None) -> int:
    return (parse_expiration(expiration_date) - (today or _today())).days


def round_to_tick(price: float) -> float:
    """Options quote in $0.01 increments under $3.00, $0.05 at/above."""
    if price < 3.0:
        return round(round(price / 0.01) * 0.01, 2)
    return round(round(price / 0.05) * 0.05, 2)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_call_contracts(underlying: str, spot: float) -> list:
    """Active, tradable calls inside the DTE and strike windows."""
    today = _today()
    window = max(0.01, config.OPTIONS_STRIKE_WINDOW_PCT)
    contracts = alpaca.list_option_contracts(
        underlying_symbols=underlying.upper(),
        type="call",
        style="american",
        status="active",
        expiration_date_gte=(today + dt.timedelta(days=config.OPTIONS_MIN_DTE)).isoformat(),
        expiration_date_lte=(today + dt.timedelta(days=config.OPTIONS_MAX_DTE)).isoformat(),
        strike_price_gte=round(spot * (1.0 - window), 2),
        strike_price_lte=round(spot * (1.0 + window), 2),
    )
    out = []
    for c in contracts:
        if not c.get("tradable") or str(c.get("status")) != "active":
            continue
        strike = _f(c.get("strike_price"))
        if strike is None or strike <= 0:
            continue
        oi = int(_f(c.get("open_interest"), 0) or 0)
        if oi < config.OPTIONS_MIN_OPEN_INTEREST:
            continue
        out.append(c)
    return out


def _build_candidate(contract: dict, snapshot: dict, spot: float, today: dt.date) -> Candidate | None:
    quote = snapshot.get("latestQuote") or {}
    bid = _f(quote.get("bp"), 0.0) or 0.0
    ask = _f(quote.get("ap"), 0.0) or 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid < config.OPTIONS_MIN_PREMIUM:
        return None
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    if spread_pct > config.OPTIONS_MAX_SPREAD_PCT:
        return None

    greeks = snapshot.get("greeks") or {}
    delta = _f(greeks.get("delta"))
    if delta is not None and not (config.OPTIONS_MIN_DELTA <= delta <= config.OPTIONS_MAX_DELTA):
        return None

    return Candidate(
        symbol=str(contract["symbol"]),
        underlying=str(contract.get("underlying_symbol") or "").upper(),
        contract_type=str(contract.get("type") or "call"),
        expiration_date=str(contract["expiration_date"])[:10],
        strike=float(_f(contract.get("strike_price")) or 0.0),
        dte=days_to_expiry(contract["expiration_date"], today),
        open_interest=int(_f(contract.get("open_interest"), 0) or 0),
        bid=round(bid, 4),
        ask=round(ask, 4),
        mid=round(mid, 4),
        spread_pct=round(spread_pct, 4),
        delta=delta,
        iv=_f(snapshot.get("impliedVolatility")),
        underlying_price=spot,
    )


def _rank_key(candidate: Candidate) -> tuple:
    """Closest to target delta first; spread as a tiebreak.

    When the feed returns no greeks, fall back to moneyness: a ~0.45-delta call
    sits slightly out of the money, so target strike ~= 1.03 * spot.
    """
    if candidate.delta is not None:
        return (0, abs(candidate.delta - config.OPTIONS_TARGET_DELTA), candidate.spread_pct)
    proxy_strike = candidate.underlying_price * 1.03
    moneyness = abs(candidate.strike - proxy_strike) / max(candidate.underlying_price, 0.01)
    return (1, moneyness, candidate.spread_pct)


def select_call(underlying: str, spot: float | None = None) -> Candidate:
    """Pick the best 30-45 DTE call for an underlying, or raise NoContractError."""
    symbol = underlying.upper()
    if spot is None:
        spot = alpaca.latest_stock_trade_price(symbol)
    if not spot or spot <= 0:
        raise NoContractError("no spot price for " + symbol)

    contracts = fetch_call_contracts(symbol, spot)
    if not contracts:
        raise NoContractError(
            "no tradable calls for {} in {}-{} DTE with OI >= {}".format(
                symbol, config.OPTIONS_MIN_DTE, config.OPTIONS_MAX_DTE, config.OPTIONS_MIN_OPEN_INTEREST
            )
        )

    by_symbol = {str(c["symbol"]): c for c in contracts}
    snapshots = alpaca.option_snapshots(by_symbol.keys())
    if not snapshots:
        raise NoContractError("no option snapshots returned for " + symbol)

    today = _today()
    candidates = []
    for contract_symbol, snapshot in snapshots.items():
        contract = by_symbol.get(contract_symbol)
        if not contract:
            continue
        candidate = _build_candidate(contract, snapshot, spot, today)
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        raise NoContractError(
            "{} calls failed delta/spread/premium filters for {}".format(len(by_symbol), symbol)
        )

    # Prefer the nearest expiry that still has a qualifying contract.
    nearest_expiry = min(c.expiration_date for c in candidates)
    shortlist = [c for c in candidates if c.expiration_date == nearest_expiry]
    return sorted(shortlist, key=_rank_key)[0]


def contract_quote(contract_symbol: str) -> dict:
    """Current bid/ask/mid plus greeks for one contract."""
    snapshots = alpaca.option_snapshots([contract_symbol])
    snapshot = snapshots.get(contract_symbol) or {}
    quote = snapshot.get("latestQuote") or {}
    trade = snapshot.get("latestTrade") or {}
    bid = _f(quote.get("bp"), 0.0) or 0.0
    ask = _f(quote.get("ap"), 0.0) or 0.0
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None
    return {
        "symbol": contract_symbol,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": _f(trade.get("p")),
        "delta": _f((snapshot.get("greeks") or {}).get("delta")),
        "iv": _f(snapshot.get("impliedVolatility")),
    }


def mark_price(quote: dict) -> float | None:
    """Best available valuation price: mid, else last trade, else bid."""
    for key in ("mid", "last", "bid"):
        value = quote.get(key)
        if value:
            return float(value)
    return None
