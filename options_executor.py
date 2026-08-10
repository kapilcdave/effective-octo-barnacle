"""Options execution: long calls off bullish narrative signals.

Alpaca options orders cannot use bracket/OCO classes, so exits are enforced by
the `monitor` job (profit target / stop loss / DTE floor) and fills are
reconciled by the `sync` job.

CLI:
    python options_executor.py account
    python options_executor.py pick <TICKER>
    python options_executor.py dry-run <signal_id>
    python options_executor.py submit <signal_id> [--force]
    python options_executor.py sync
    python options_executor.py monitor
    python options_executor.py list
    python options_executor.py positions
    python options_executor.py close <trade_id> [reason]
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from typing import Any

import alpaca
import config
import options
from db import db_session
from runtime import get_logger

CONTRACT_MULTIPLIER = 100.0

STATUS_SUBMITTING = "submitting"
STATUS_PENDING_ENTRY = "pending_entry"
STATUS_OPEN = "open"
STATUS_CLOSING = "closing"
STATUS_CLOSED = "closed"
STATUS_CANCELED = "canceled"
STATUS_ERROR = "error"

_OPEN_ORDER_STATES = {"new", "accepted", "held", "accepted_for_bidding", "pending_new", "partially_filled"}
_DEAD_ORDER_STATES = {"canceled", "expired", "rejected", "suspended", "stopped", "done_for_day"}


def _logger() -> logging.Logger:
    return get_logger("tradingbot.options_executor")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now().replace(microsecond=0).isoformat()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(text[:26] + "+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


# --- Account preflight ------------------------------------------------------


def options_account() -> dict:
    """Account fields that matter for options, plus the trading level check."""
    account = alpaca.get_account()
    level = int(_f(account.get("options_trading_level"), 0))
    return {
        "account_number": account.get("account_number"),
        "paper": alpaca.is_paper(),
        "status": account.get("status"),
        "equity": _f(account.get("equity")),
        "buying_power": _f(account.get("buying_power")),
        "options_buying_power": _f(account.get("options_buying_power"), _f(account.get("buying_power"))),
        "options_trading_level": level,
        "options_approved_level": int(_f(account.get("options_approved_level"), 0)),
        "trading_blocked": bool(account.get("trading_blocked")),
    }


def _assert_can_buy_options(account: dict) -> None:
    if account["trading_blocked"]:
        raise RuntimeError("account trading is blocked")
    level = account["options_trading_level"]
    if level < config.OPTIONS_MIN_TRADING_LEVEL:
        raise RuntimeError(
            "options_trading_level={} but buying calls needs level >= {} "
            "(level 1 is covered calls / cash-secured puts only)".format(
                level, config.OPTIONS_MIN_TRADING_LEVEL
            )
        )


# --- DB helpers ------------------------------------------------------------


def _load_signal(signal_id: int) -> dict | None:
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


def _trades_by_status(statuses: tuple) -> list:
    placeholders = ",".join("?" for _ in statuses)
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM option_trades WHERE status IN ({}) ORDER BY id".format(placeholders),
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def _load_trade(trade_id: int) -> dict | None:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM option_trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def _live_trade_for_signal(signal_id: int) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT * FROM option_trades
            WHERE signal_id = ? AND status IN (?, ?, ?, ?)
            ORDER BY id DESC LIMIT 1
            """,
            (signal_id, STATUS_SUBMITTING, STATUS_PENDING_ENTRY, STATUS_OPEN, STATUS_CLOSING),
        ).fetchone()
    return dict(row) if row else None


def _live_trade_count() -> int:
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM option_trades
            WHERE status IN (?, ?, ?, ?)
            """,
            (STATUS_SUBMITTING, STATUS_PENDING_ENTRY, STATUS_OPEN, STATUS_CLOSING),
        ).fetchone()
    return int(row["n"]) if row else 0


def _update_trade(trade_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now_iso()
    assignments = ", ".join("{} = ?".format(k) for k in fields)
    with db_session() as conn:
        conn.execute(
            "UPDATE option_trades SET {} WHERE id = ?".format(assignments),
            tuple(fields.values()) + (trade_id,),
        )


def _insert_trade(*, signal_id: int | None, candidate: options.Candidate, qty: int, limit_price: float | None) -> int:
    now = _now_iso()
    with db_session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO option_trades(
              signal_id, underlying, contract_symbol, contract_type, expiration_date, strike,
              qty, entry_limit_price, entry_underlying_price, entry_delta, entry_iv,
              status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                candidate.underlying,
                candidate.symbol,
                candidate.contract_type,
                candidate.expiration_date,
                candidate.strike,
                qty,
                limit_price,
                candidate.underlying_price,
                candidate.delta,
                candidate.iv,
                STATUS_SUBMITTING,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


# --- Sizing ----------------------------------------------------------------


def entry_limit_price(candidate: options.Candidate) -> float:
    """Pay up to `slippage` over mid, never above the ask."""
    padded = candidate.mid * (1.0 + config.OPTIONS_ENTRY_SLIPPAGE_PCT)
    return options.round_to_tick(min(candidate.ask, padded))


def exit_limit_price(quote: dict) -> float:
    """Sell at most `slippage` under mid, never below the bid."""
    mid = quote.get("mid") or options.mark_price(quote) or 0.0
    bid = _f(quote.get("bid"))
    shaded = mid * (1.0 - config.OPTIONS_EXIT_SLIPPAGE_PCT)
    return options.round_to_tick(max(bid, shaded)) if mid > 0 else options.round_to_tick(bid)


def size_contracts(price_per_share: float, suggested_size: float, account: dict) -> tuple:
    """Returns (qty, detail dict). qty == 0 means the trade is not affordable."""
    cost_per_contract = price_per_share * CONTRACT_MULTIPLIER
    hard_cap = account["equity"] * config.OPTIONS_MAX_POSITION_PCT
    options_bp = account["options_buying_power"]
    budget = min(max(suggested_size, 0.0), hard_cap, options_bp)

    qty = int(budget // cost_per_contract) if cost_per_contract > 0 else 0
    rounded_up = False
    if qty == 0 and config.OPTIONS_ALLOW_SINGLE_CONTRACT:
        if 0 < cost_per_contract <= min(hard_cap, options_bp):
            qty = 1
            rounded_up = True
    qty = min(qty, config.OPTIONS_MAX_CONTRACTS)

    detail = {
        "cost_per_contract": round(cost_per_contract, 2),
        "suggested_size": round(suggested_size, 2),
        "hard_cap": round(hard_cap, 2),
        "options_buying_power": round(options_bp, 2),
        "budget": round(budget, 2),
        "qty": qty,
        "total_cost": round(qty * cost_per_contract, 2),
        "rounded_up_to_one": rounded_up,
    }
    return qty, detail


# --- Entry -----------------------------------------------------------------


def _build_plan(signal_id: int, *, force: bool = False) -> tuple:
    """Select a contract and size it. No orders, no DB writes.

    Returns (candidate, plan) where plan is JSON-serializable.
    """
    signal = _load_signal(signal_id)
    if not signal:
        raise RuntimeError("Signal {} not found".format(signal_id))
    if str(signal["outcome"]) != "pending" and not force:
        raise RuntimeError("Signal {} is not pending (outcome={})".format(signal_id, signal["outcome"]))

    existing = _live_trade_for_signal(signal_id)
    if existing and not force:
        raise RuntimeError(
            "Signal {} already has option trade {} (status={})".format(
                signal_id, existing["id"], existing["status"]
            )
        )

    live = _live_trade_count()
    if live >= config.OPTIONS_MAX_OPEN_POSITIONS and not force:
        raise RuntimeError(
            "{} live option trades >= OPTIONS_MAX_OPEN_POSITIONS={}".format(
                live, config.OPTIONS_MAX_OPEN_POSITIONS
            )
        )

    account = options_account()
    _assert_can_buy_options(account)

    ticker = str(signal["ticker"]).upper()
    spot = alpaca.latest_stock_trade_price(ticker) or _f(signal["price_at_signal"]) or None
    candidate = options.select_call(ticker, spot)

    use_limit = config.OPTIONS_ORDER_TYPE == "limit"
    limit_price = entry_limit_price(candidate) if use_limit else None
    # Size against the ask for market orders so the estimate is conservative.
    price_basis = limit_price if limit_price else candidate.ask
    qty, sizing = size_contracts(price_basis, _f(signal["suggested_size"]), account)
    if qty < 1:
        raise RuntimeError(
            "Signal {} {}: one {} contract costs ${:.2f}, budget ${:.2f}".format(
                signal_id, ticker, candidate.symbol, sizing["cost_per_contract"], sizing["budget"]
            )
        )

    return candidate, {
        "signal_id": signal_id,
        "ticker": ticker,
        "narrative_score": _f(signal["narrative_score"]),
        "contract": candidate.as_dict(),
        "sizing": sizing,
        "order": _order_payload(candidate.symbol, qty, "buy", limit_price),
        "account": account,
    }


def plan_entry(signal_id: int, *, force: bool = False) -> dict:
    return _build_plan(signal_id, force=force)[1]


def _order_payload(
    contract_symbol: str,
    qty: int,
    side: str,
    limit_price: float | None,
    *,
    client_order_id: str | None = None,
) -> dict:
    payload = {
        "symbol": contract_symbol,
        "qty": str(int(qty)),
        "side": side,
        "type": "limit" if limit_price else "market",
        "time_in_force": "day",
    }
    if limit_price:
        payload["limit_price"] = "{:.2f}".format(limit_price)
    if client_order_id:
        payload["client_order_id"] = client_order_id
    return payload


def submit_entry(signal_id: int, *, force: bool = False) -> dict:
    log = _logger()
    candidate, plan = _build_plan(signal_id, force=force)

    if not force and not alpaca.market_is_open():
        raise RuntimeError("market is closed; options trade only during regular hours (use --force to override)")

    qty = plan["sizing"]["qty"]
    trade_id = _insert_trade(
        signal_id=signal_id,
        candidate=candidate,
        qty=qty,
        limit_price=_f(plan["order"].get("limit_price")) or None,
    )

    # Deterministic so `sync` can recover an order submitted before the local write.
    client_order_id = "opt-t{}-e".format(trade_id)
    payload = dict(plan["order"], client_order_id=client_order_id)
    try:
        order = alpaca.submit_order(payload)
    except Exception as e:
        _update_trade(trade_id, status=STATUS_ERROR, exit_reason=str(e)[:500])
        log.error("entry submit failed trade_id=%s signal_id=%s: %s", trade_id, signal_id, e)
        raise

    _update_trade(
        trade_id,
        entry_order_id=str(order.get("id")),
        status=STATUS_PENDING_ENTRY,
        opened_at=_now_iso(),
    )
    log.info(
        "entry submitted trade_id=%s signal_id=%s %s qty=%s limit=%s order_id=%s",
        trade_id,
        signal_id,
        candidate.symbol,
        qty,
        payload.get("limit_price", "market"),
        order.get("id"),
    )
    return {"trade_id": trade_id, "client_order_id": client_order_id, "order": order, "plan": plan}


# --- Reconciliation --------------------------------------------------------


def _resolve_order(trade: dict, *, entry: bool) -> dict | None:
    order_id = trade["entry_order_id"] if entry else trade["exit_order_id"]
    if order_id:
        try:
            return alpaca.get_order(str(order_id))
        except alpaca.AlpacaError:
            return None
    if entry:
        # Submitted but never recorded: recover via the deterministic client id.
        return alpaca.get_order_by_client_id("opt-t{}-e".format(trade["id"]))
    return None


def _settle_entry(trade: dict, order: dict) -> None:
    log = _logger()
    trade_id = trade["id"]
    status = str(order.get("status"))
    filled_qty = int(_f(order.get("filled_qty")))
    fill_price = _f(order.get("filled_avg_price"))

    if status == "filled" or (status in _DEAD_ORDER_STATES and filled_qty > 0):
        _update_trade(
            trade_id,
            status=STATUS_OPEN,
            qty=filled_qty or trade["qty"],
            entry_price=fill_price or trade["entry_limit_price"],
            opened_at=(_parse_ts(order.get("filled_at")) or _now()).replace(microsecond=0).isoformat(),
        )
        log.info("entry filled trade_id=%s %s qty=%s @ %.2f", trade_id, trade["contract_symbol"], filled_qty, fill_price)
        return

    if status in _DEAD_ORDER_STATES:
        _update_trade(trade_id, status=STATUS_CANCELED, exit_reason="entry_" + status, closed_at=_now_iso())
        log.info("entry %s trade_id=%s %s", status, trade_id, trade["contract_symbol"])
        return

    if status in _OPEN_ORDER_STATES:
        submitted = _parse_ts(order.get("submitted_at")) or _parse_ts(trade["opened_at"])
        if submitted is None:
            return
        age_min = (_now() - submitted).total_seconds() / 60.0
        if age_min >= config.OPTIONS_ENTRY_TIMEOUT_MIN:
            try:
                alpaca.cancel_order(str(order.get("id")))
                log.info("entry timeout cancel trade_id=%s age=%.1fmin", trade_id, age_min)
            except alpaca.AlpacaError as e:
                log.warning("entry cancel failed trade_id=%s: %s", trade_id, e)


def _settle_exit(trade: dict, order: dict) -> None:
    log = _logger()
    trade_id = trade["id"]
    status = str(order.get("status"))
    filled_qty = int(_f(order.get("filled_qty")))
    fill_price = _f(order.get("filled_avg_price"))

    if status == "filled" or (status in _DEAD_ORDER_STATES and filled_qty > 0):
        entry_price = _f(trade["entry_price"])
        qty = filled_qty or int(_f(trade["qty"]))
        pnl = (fill_price - entry_price) * CONTRACT_MULTIPLIER * qty
        _update_trade(
            trade_id,
            status=STATUS_CLOSED,
            exit_price=fill_price,
            pnl=round(pnl, 2),
            closed_at=(_parse_ts(order.get("filled_at")) or _now()).replace(microsecond=0).isoformat(),
        )
        log.info(
            "exit filled trade_id=%s %s @ %.2f pnl=$%.2f reason=%s",
            trade_id,
            trade["contract_symbol"],
            fill_price,
            pnl,
            trade["exit_reason"],
        )
        return

    if status in _DEAD_ORDER_STATES:
        # Let the monitor try again on the next pass.
        _update_trade(trade_id, status=STATUS_OPEN, exit_order_id=None)
        log.info("exit %s trade_id=%s, will retry", status, trade_id)


def sync() -> dict:
    """Reconcile local trade rows with Alpaca orders and positions."""
    log = _logger()
    summary = {"entries": 0, "exits": 0, "vanished": 0}

    for trade in _trades_by_status((STATUS_SUBMITTING, STATUS_PENDING_ENTRY)):
        order = _resolve_order(trade, entry=True)
        if order is None:
            if str(trade["status"]) == STATUS_SUBMITTING:
                _update_trade(trade["id"], status=STATUS_CANCELED, exit_reason="entry_order_missing")
            continue
        if not trade["entry_order_id"]:
            _update_trade(trade["id"], entry_order_id=str(order.get("id")))
            trade["entry_order_id"] = str(order.get("id"))
        _settle_entry(trade, order)
        summary["entries"] += 1

    for trade in _trades_by_status((STATUS_CLOSING,)):
        order = _resolve_order(trade, entry=False)
        if order is None:
            _update_trade(trade["id"], status=STATUS_OPEN, exit_order_id=None)
            continue
        _settle_exit(trade, order)
        summary["exits"] += 1

    # Positions can disappear without a fill: expiry, exercise, assignment.
    open_trades = _trades_by_status((STATUS_OPEN,))
    if open_trades:
        held = {str(p.get("symbol")) for p in alpaca.get_positions()}
        for trade in open_trades:
            if str(trade["contract_symbol"]) in held:
                continue
            _update_trade(
                trade["id"],
                status=STATUS_CLOSED,
                exit_reason="position_gone_expired_or_assigned",
                closed_at=_now_iso(),
            )
            summary["vanished"] += 1
            log.warning(
                "open trade_id=%s %s has no Alpaca position; marked closed",
                trade["id"],
                trade["contract_symbol"],
            )

    log.info("options sync %s", summary)
    return summary


# --- Exits -----------------------------------------------------------------


def exit_decision(trade: dict, quote: dict) -> tuple:
    """Returns (reason, pnl_pct) where reason is None when the trade should stay open."""
    entry_price = _f(trade["entry_price"])
    mark = options.mark_price(quote)
    if mark is None or entry_price <= 0:
        return None, None

    pnl_pct = (mark - entry_price) / entry_price
    dte = options.days_to_expiry(trade["expiration_date"])

    if pnl_pct >= config.OPTIONS_PROFIT_TARGET_PCT:
        return "profit_target", pnl_pct
    if pnl_pct <= -abs(config.OPTIONS_STOP_LOSS_PCT):
        return "stop_loss", pnl_pct
    if dte <= config.OPTIONS_EXIT_DTE:
        return "dte_floor", pnl_pct
    return None, pnl_pct


def close_trade(trade_id: int, reason: str = "manual", *, force: bool = False) -> dict:
    log = _logger()
    trade = _load_trade(trade_id)
    if not trade:
        raise RuntimeError("option trade {} not found".format(trade_id))
    if str(trade["status"]) != STATUS_OPEN and not force:
        raise RuntimeError("trade {} is {}, not open".format(trade_id, trade["status"]))

    contract_symbol = str(trade["contract_symbol"])
    position = alpaca.get_position(contract_symbol)
    if position is None:
        _update_trade(
            trade_id, status=STATUS_CLOSED, exit_reason="position_gone_expired_or_assigned", closed_at=_now_iso()
        )
        raise RuntimeError("no Alpaca position for {}; marked closed".format(contract_symbol))

    qty = min(int(_f(trade["qty"])) or 1, abs(int(_f(position.get("qty")))))
    quote = options.contract_quote(contract_symbol)
    use_limit = config.OPTIONS_ORDER_TYPE == "limit" and (quote.get("bid") or 0) > 0
    limit_price = exit_limit_price(quote) if use_limit else None
    payload = _order_payload(
        contract_symbol,
        qty,
        "sell",
        limit_price,
        client_order_id="opt-t{}-x{}".format(trade_id, int(time.time())),
    )

    order = alpaca.submit_order(payload)
    _update_trade(trade_id, status=STATUS_CLOSING, exit_order_id=str(order.get("id")), exit_reason=reason)
    log.info(
        "exit submitted trade_id=%s %s qty=%s limit=%s reason=%s",
        trade_id,
        contract_symbol,
        qty,
        payload.get("limit_price", "market"),
        reason,
    )
    return order


def monitor() -> dict:
    """Enforce profit target / stop loss / DTE floor on open trades."""
    log = _logger()
    sync()

    open_trades = _trades_by_status((STATUS_OPEN,))
    summary = {"checked": len(open_trades), "closed": 0}
    if not open_trades:
        log.info("options monitor: no open trades")
        return summary

    market_open = alpaca.market_is_open()
    for trade in open_trades:
        quote = options.contract_quote(str(trade["contract_symbol"]))
        reason, pnl_pct = exit_decision(trade, quote)
        if pnl_pct is not None:
            log.info(
                "monitor trade_id=%s %s mark=%s pnl=%.1f%% dte=%s reason=%s",
                trade["id"],
                trade["contract_symbol"],
                options.mark_price(quote),
                pnl_pct * 100.0,
                options.days_to_expiry(trade["expiration_date"]),
                reason or "hold",
            )
        if not reason:
            continue
        if not market_open:
            log.info("exit deferred trade_id=%s reason=%s: market closed", trade["id"], reason)
            continue
        try:
            close_trade(int(trade["id"]), reason)
            summary["closed"] += 1
        except Exception as e:
            log.error("exit failed trade_id=%s reason=%s: %s", trade["id"], reason, e)

    log.info("options monitor %s", summary)
    return summary


# --- Optional automation ---------------------------------------------------


def auto_trade() -> dict:
    """Submit entries for untraded pending signals. Off unless OPTIONS_AUTO_TRADE=1."""
    log = _logger()
    if not config.OPTIONS_AUTO_TRADE:
        log.info("options auto_trade disabled (OPTIONS_AUTO_TRADE=0)")
        return {"submitted": 0, "skipped": 0}

    if not alpaca.market_is_open():
        log.info("options auto_trade: market closed")
        return {"submitted": 0, "skipped": 0}

    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT s.id
            FROM signals s
            LEFT JOIN option_trades t ON t.signal_id = s.id
            WHERE s.outcome = 'pending' AND t.id IS NULL
            ORDER BY s.narrative_score DESC, s.id DESC
            """
        ).fetchall()

    submitted = 0
    skipped = 0
    for row in rows:
        if _live_trade_count() >= config.OPTIONS_MAX_OPEN_POSITIONS:
            log.info("options auto_trade: position cap reached")
            break
        try:
            submit_entry(int(row["id"]))
            submitted += 1
        except Exception as e:
            skipped += 1
            log.info("options auto_trade skip signal_id=%s: %s", row["id"], e)

    log.info("options auto_trade submitted=%s skipped=%s", submitted, skipped)
    return {"submitted": submitted, "skipped": skipped}


# --- CLI -------------------------------------------------------------------


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _list_trades() -> list:
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, signal_id, underlying, contract_symbol, expiration_date, strike, qty,
                   entry_price, exit_price, pnl, status, exit_reason, opened_at, closed_at
            FROM option_trades ORDER BY id DESC LIMIT 50
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _option_positions() -> list:
    return [p for p in alpaca.get_positions() if str(p.get("asset_class")) == "us_option"]


_USAGE = (
    "usage: python options_executor.py "
    "account | pick <TICKER> | dry-run <signal_id> | submit <signal_id> [--force] | "
    "sync | monitor | auto-trade | list | positions | close <trade_id> [reason]"
)


def run(argv: list | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    log = _logger()
    if not args:
        log.error(_USAGE)
        return 2

    command = args[0]
    force = "--force" in args
    rest = [a for a in args[1:] if a != "--force"]

    if command == "account":
        _print(options_account())
        return 0

    if command == "pick" and rest:
        _print(options.select_call(rest[0].upper()).as_dict())
        return 0

    if command == "dry-run" and rest:
        _print(plan_entry(int(rest[0]), force=force))
        return 0

    if command == "submit" and rest:
        result = submit_entry(int(rest[0]), force=force)
        _print({"trade_id": result["trade_id"], "order": result["order"]})
        return 0

    if command == "sync":
        _print(sync())
        return 0

    if command == "monitor":
        _print(monitor())
        return 0

    if command == "auto-trade":
        _print(auto_trade())
        return 0

    if command == "list":
        _print(_list_trades())
        return 0

    if command == "positions":
        _print(_option_positions())
        return 0

    if command == "close" and rest:
        reason = rest[1] if len(rest) > 1 else "manual"
        _print(close_trade(int(rest[0]), reason, force=force))
        return 0

    log.error(_USAGE)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as e:
        _logger().error("%s", e)
        raise SystemExit(1)
