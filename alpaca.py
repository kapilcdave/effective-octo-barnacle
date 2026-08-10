"""Thin Alpaca REST client shared by the equity and options executors.

Trading API  -> config.ALPACA_BASE_URL (paper by default)
Market Data  -> config.ALPACA_DATA_URL
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator

import requests

import config

_TIMEOUT = 30
_MAX_SNAPSHOT_SYMBOLS = 100


class AlpacaError(RuntimeError):
    pass


class AlpacaCredentialsError(AlpacaError):
    """Missing or rejected credentials.

    Kept distinct so callers do not treat "not configured" as "not eligible":
    swallowing this is how a broken data source silently empties the pipeline.
    """


def _headers() -> dict:
    if not config.ALPACA_KEY or config.ALPACA_KEY == "your-key":
        raise AlpacaCredentialsError("ALPACA_KEY not set")
    if not config.ALPACA_SECRET or config.ALPACA_SECRET == "your-key":
        raise AlpacaCredentialsError("ALPACA_SECRET not set")
    return {
        "APCA-API-KEY-ID": config.ALPACA_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
) -> Any:
    url = base_url.rstrip("/") + path
    response = requests.request(
        method,
        url,
        headers=_headers(),
        params=params,
        json=payload,
        timeout=_TIMEOUT,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        request_id = response.headers.get("X-Request-ID", "")
        detail = response.text.strip()
        suffix = " x-request-id=" + request_id if request_id else ""
        message = "Alpaca {} {} failed: {} {}{}".format(
            method, path, response.status_code, detail, suffix
        )
        if response.status_code in (401, 403):
            raise AlpacaCredentialsError(message) from e
        raise AlpacaError(message) from e
    if not response.content:
        return {}
    return response.json()


def trading_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
) -> Any:
    return _request(config.ALPACA_BASE_URL, method, path, params=params, payload=payload)


def data_request(path: str, *, params: dict | None = None) -> Any:
    return _request(config.ALPACA_DATA_URL, "GET", path, params=params)


def is_paper() -> bool:
    return "paper" in config.ALPACA_BASE_URL


# --- Account / clock -------------------------------------------------------


def get_account() -> dict:
    return trading_request("GET", "/v2/account")


def get_clock() -> dict:
    return trading_request("GET", "/v2/clock")


def market_is_open() -> bool:
    return bool(get_clock().get("is_open"))


# --- Orders ----------------------------------------------------------------


def submit_order(payload: dict) -> dict:
    return trading_request("POST", "/v2/orders", payload=payload)


def get_order(order_id: str) -> dict:
    return trading_request("GET", "/v2/orders/" + str(order_id))


def get_order_by_client_id(client_order_id: str) -> dict | None:
    """Recovery path when a submit succeeded but the local write did not."""
    try:
        return trading_request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
        )
    except AlpacaError as e:
        if "404" in str(e):
            return None
        raise


def cancel_order(order_id: str) -> Any:
    return trading_request("DELETE", "/v2/orders/" + str(order_id))


# --- Positions -------------------------------------------------------------


def get_positions() -> list:
    result = trading_request("GET", "/v2/positions")
    return result if isinstance(result, list) else []


def get_position(symbol: str) -> dict | None:
    try:
        return trading_request("GET", "/v2/positions/" + symbol)
    except AlpacaError as e:
        if "404" in str(e):
            return None
        raise


# --- Option contracts (Trading API) ---------------------------------------


def iter_option_contracts(*, limit: int = 500, **filters: Any) -> Iterator[dict]:
    """Yield option contracts, following pagination tokens.

    Filters map straight to query params, e.g. underlying_symbols, type,
    expiration_date_gte, expiration_date_lte, strike_price_gte, strike_price_lte.
    """
    params: dict = {k: v for k, v in filters.items() if v is not None}
    params["limit"] = limit
    seen_tokens: set = set()
    while True:
        body = trading_request("GET", "/v2/options/contracts", params=params)
        for contract in body.get("option_contracts") or []:
            yield contract
        token = body.get("next_page_token") or body.get("page_token")
        if not token or token in seen_tokens:
            return
        seen_tokens.add(token)
        params["page_token"] = token


def list_option_contracts(*, limit: int = 500, **filters: Any) -> list:
    return list(iter_option_contracts(limit=limit, **filters))


# --- Option market data ----------------------------------------------------


def option_snapshots(symbols: Iterable[str], *, feed: str | None = None) -> dict:
    """Latest quote/trade/greeks keyed by contract symbol."""
    all_symbols = [s for s in dict.fromkeys(symbols) if s]
    snapshots: dict = {}
    for start in range(0, len(all_symbols), _MAX_SNAPSHOT_SYMBOLS):
        chunk = all_symbols[start : start + _MAX_SNAPSHOT_SYMBOLS]
        params = {
            "symbols": ",".join(chunk),
            "feed": feed or config.OPTIONS_FEED,
            "limit": 1000,
        }
        while True:
            body = data_request("/v1beta1/options/snapshots", params=params)
            snapshots.update(body.get("snapshots") or {})
            token = body.get("next_page_token")
            if not token:
                break
            params = dict(params, page_token=token)
    return snapshots


def latest_stock_trade_price(symbol: str) -> float | None:
    body = data_request("/v2/stocks/" + symbol + "/trades/latest", params={"feed": config.STOCK_FEED})
    price = ((body.get("trade") or {}).get("p")) if isinstance(body, dict) else None
    return float(price) if price else None


# --- News (Benzinga feed, ticker-tagged) -----------------------------------


def news(
    symbols: Iterable[str] | None = None,
    *,
    start: str | None = None,
    limit: int = 50,
    include_content: bool = False,
) -> list:
    params: dict = {"limit": min(max(limit, 1), 50), "sort": "desc"}
    if symbols:
        params["symbols"] = ",".join(dict.fromkeys(s.upper() for s in symbols if s))
    if start:
        params["start"] = start
    if include_content:
        params["include_content"] = "true"

    articles: list = []
    while True:
        body = data_request("/v1beta1/news", params=params)
        articles.extend(body.get("news") or [])
        token = body.get("next_page_token")
        if not token or len(articles) >= limit:
            break
        params = dict(params, page_token=token)
    return articles[:limit]
