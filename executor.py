from __future__ import annotations

import json
import logging
import sys
from typing import Any

import requests

import config
from db import db_session


def _logger() -> logging.Logger:
    log = logging.getLogger("tradingbot.executor")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler("logs/bot.log")
    fh.setFormatter(formatter)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def _alpaca_request(method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not config.ALPACA_KEY or config.ALPACA_KEY == "your-key":
        raise RuntimeError("ALPACA_KEY not set")
    if not config.ALPACA_SECRET or config.ALPACA_SECRET == "your-key":
        raise RuntimeError("ALPACA_SECRET not set")

    headers = {
        "APCA-API-KEY-ID": config.ALPACA_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
        "Content-Type": "application/json",
    }
    url = config.ALPACA_BASE_URL.rstrip("/") + path
    response = requests.request(method, url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def _get_account_equity() -> float:
    account = _alpaca_request("GET", "/v2/account")
    return float(account["equity"])


def _load_signal(signal_id: int) -> dict[str, Any] | None:
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT id, ticker, narrative_score, price_at_signal, suggested_size, created_at, outcome
            FROM signals
            WHERE id = ?
            """,
            (signal_id,),
        ).fetchone()
    return dict(row) if row else None


def _position_value(signal: dict[str, Any], equity: float) -> float:
    suggested = float(signal["suggested_size"] or 0.0)
    max_allowed = equity * config.MAX_POSITION_PCT
    return max(0.0, min(suggested, max_allowed))


def submit_signal(signal_id: int) -> dict[str, Any]:
    signal = _load_signal(signal_id)
    if not signal:
        raise RuntimeError(f"Signal {signal_id} not found")
    if str(signal["outcome"]) != "pending":
        raise RuntimeError(f"Signal {signal_id} is not pending")

    equity = _get_account_equity()
    price = float(signal["price_at_signal"] or 0.0)
    if price <= 0:
        raise RuntimeError(f"Signal {signal_id} has invalid entry price")

    position_value = _position_value(signal, equity)
    qty = int(position_value / price)
    if qty < 1:
        raise RuntimeError(f"Signal {signal_id} size rounds to zero shares")

    order_payload = {
        "symbol": str(signal["ticker"]).upper(),
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": round(price * 0.85, 2)},
        "take_profit": {"limit_price": round(price * 1.25, 2)},
        "client_order_id": f"signal-{signal_id}",
    }
    return _alpaca_request("POST", "/v2/orders", payload=order_payload)


def run(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    log = _logger()
    if len(args) != 1:
        log.error("usage: python executor.py <signal_id>")
        return 2

    signal_id = int(args[0])
    order = submit_signal(signal_id)
    log.info("submitted signal_id=%s order_id=%s", signal_id, order.get("id"))
    print(json.dumps(order, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
