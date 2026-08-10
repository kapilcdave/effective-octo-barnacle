from __future__ import annotations

import json
import logging
import sys
from typing import Any

import alpaca
import config
from db import db_session
from runtime import get_logger


def _logger() -> logging.Logger:
    return get_logger("tradingbot.executor")


def _alpaca_request(method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deprecated: kept as a shim, use alpaca.trading_request directly."""
    return alpaca.trading_request(method, path, payload=payload)


def get_account() -> dict[str, Any]:
    return alpaca.get_account()


def _get_account_equity() -> float:
    account = get_account()
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


def build_order(signal_id: int) -> dict[str, Any]:
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
    return order_payload


def submit_signal(signal_id: int) -> dict[str, Any]:
    return alpaca.submit_order(build_order(signal_id))


def _usage(log: logging.Logger) -> None:
    log.error("usage: python executor.py account | dry-run <signal_id> | submit <signal_id>")


def run(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    log = _logger()
    if not args:
        _usage(log)
        return 2

    if args[0] == "account" and len(args) == 1:
        print(json.dumps(get_account(), indent=2, sort_keys=True))
        return 0

    # Backward compatibility: `python executor.py 123` submits signal 123.
    command = "submit" if len(args) == 1 else args[0]
    signal_arg = args[0] if len(args) == 1 else args[1]

    if command not in {"dry-run", "submit"}:
        _usage(log)
        return 2

    signal_id = int(signal_arg)
    if command == "dry-run":
        order = build_order(signal_id)
        log.info("built dry-run signal_id=%s symbol=%s qty=%s", signal_id, order["symbol"], order["qty"])
        print(json.dumps(order, indent=2, sort_keys=True))
        return 0

    order = submit_signal(signal_id)
    log.info("submitted signal_id=%s order_id=%s", signal_id, order.get("id"))
    print(json.dumps(order, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as e:
        _logger().error("%s", e)
        raise SystemExit(1)
