from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict

import config
from db import db_session
from prices import latest_close_and_close_24h_ago


def _logger() -> logging.Logger:
    log = logging.getLogger("tradingbot.scorer")
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


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _now_utc_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat()


def _parse_iso(ts: str) -> dt.datetime:
    # We store ISO timestamps with timezone via .isoformat() (e.g. "...+00:00")
    return dt.datetime.fromisoformat(ts)


def _hours_since(ts: dt.datetime, now: dt.datetime) -> float:
    return (now - ts).total_seconds() / 3600.0


def _window_key(ts: dt.datetime, window_minutes: int = 120) -> int:
    return int(ts.timestamp() // (window_minutes * 60))


def _get_win_rate() -> float:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM signals WHERE outcome IN ('win','loss') GROUP BY outcome"
        ).fetchall()
    counts = {r["outcome"]: int(r["n"]) for r in rows}
    wins = counts.get("win", 0)
    losses = counts.get("loss", 0)
    total = wins + losses
    if total < 20:
        return 0.5
    return wins / total if total else 0.5


def _kelly_dollar_size(win_rate: float) -> float:
    # As given: min(KELLY_FRACTION * (win_rate - (1-win_rate)/1.5), MAX_POSITION_PCT) * CAPITAL
    edge = win_rate - (1 - win_rate) / 1.5
    frac = config.KELLY_FRACTION * edge
    frac = max(0.0, min(frac, config.MAX_POSITION_PCT))
    return frac * config.CAPITAL


def _prices_ok_24h(ticker: str) -> tuple[bool, float | None]:
    """
    Returns (ok, current_price). ok means abs(move_24h) < 1%.
    """
    try:
        now_price, ref_price = latest_close_and_close_24h_ago(ticker)
    except Exception:
        return False, None
    if ref_price <= 0:
        return False, now_price
    move = abs(now_price - ref_price) / ref_price
    return move < 0.01, now_price


def _recent_bullish_tagged(now: dt.datetime) -> list[dict]:
    since = (now - dt.timedelta(days=7)).replace(microsecond=0).isoformat()
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT tickers, catalyst_type, urgency, tagged_at
            FROM tagged_stories
            WHERE sentiment = 'bullish'
              AND tagged_at >= ?
            ORDER BY tagged_at ASC
            """,
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def _first_mentioned_this_week(ticker: str, now: dt.datetime) -> str | None:
    since = (now - dt.timedelta(days=7)).replace(microsecond=0).isoformat()
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT tagged_at, tickers
            FROM tagged_stories
            WHERE tagged_at >= ?
            ORDER BY tagged_at ASC
            """,
            (since,),
        ).fetchall()
    for r in rows:
        try:
            tickers = json.loads(r["tickers"] or "[]")
        except Exception:
            tickers = []
        if ticker in {str(t).upper() for t in tickers}:
            return r["tagged_at"]
    return None


def _signal_exists_recently(ticker: str, now: dt.datetime) -> bool:
    since = (now - dt.timedelta(days=7)).replace(microsecond=0).isoformat()
    with db_session() as conn:
        row = conn.execute(
            "SELECT 1 FROM signals WHERE ticker = ? AND created_at >= ? LIMIT 1",
            (ticker, since),
        ).fetchone()
    return row is not None


def _insert_signal(
    *,
    ticker: str,
    first_mentioned: str | None,
    narrative_score: float,
    price_at_signal: float,
    suggested_size: float,
) -> None:
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO signals(
              ticker, first_mentioned, narrative_score, price_at_signal,
              price_day1, price_day3, price_day7, suggested_size,
              created_at, outcome
            )
            VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 'pending')
            """,
            (
                ticker,
                first_mentioned,
                narrative_score,
                price_at_signal,
                suggested_size,
                _now_utc_iso(),
            ),
        )


def run() -> None:
    log = _logger()
    log.info("scorer.run start")
    now = _now_utc()

    rows = _recent_bullish_tagged(now)
    if not rows:
        log.info("scorer.run no bullish tagged stories in last 7d")
        return

    # Secondary dedup happens here (same ticker + catalyst within 2h window -> keep max urgency).
    # Map: (ticker, catalyst_type, window_key) -> (urgency, tagged_at)
    buckets: dict[tuple[str, str, int], tuple[int, dt.datetime]] = {}

    for r in rows:
        try:
            tickers = json.loads(r["tickers"] or "[]")
        except Exception:
            tickers = []
        catalyst = str(r["catalyst_type"] or "other").strip()
        urgency = int(r["urgency"] or 1)
        tagged_at = _parse_iso(r["tagged_at"])
        win = _window_key(tagged_at, 120)
        for t in tickers:
            ticker = str(t).upper().strip()
            if ticker not in set(config.WATCHLIST):
                continue
            key = (ticker, catalyst, win)
            prev = buckets.get(key)
            if prev is None or urgency > prev[0]:
                buckets[key] = (urgency, tagged_at)

    # Convert buckets into narrative score per ticker.
    scores = defaultdict(float)
    for (ticker, _catalyst, _win), (urgency, tagged_at) in buckets.items():
        hours_ago = _hours_since(tagged_at, now)
        decay = 0.95 ** hours_ago
        scores[ticker] += float(urgency) * decay

    win_rate = _get_win_rate()
    suggested = _kelly_dollar_size(win_rate)

    fired = 0
    for ticker, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        if score <= config.NARRATIVE_THRESHOLD:
            continue
        if _signal_exists_recently(ticker, now):
            continue
        ok_price, now_price = _prices_ok_24h(ticker)
        if not ok_price or now_price is None:
            continue
        first = _first_mentioned_this_week(ticker, now)
        _insert_signal(
            ticker=ticker,
            first_mentioned=first,
            narrative_score=float(score),
            price_at_signal=float(now_price),
            suggested_size=float(suggested),
        )
        fired += 1
        log.info("signal fired ticker=%s score=%.2f price=%.2f size=$%.2f", ticker, score, now_price, suggested)

    log.info("scorer.run done signals_fired=%s tickers_scored=%s", fired, len(scores))


if __name__ == "__main__":
    run()

