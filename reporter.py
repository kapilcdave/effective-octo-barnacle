from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import jinja2

import config
from db import db_session
from prices import close_on_or_after
from runtime import get_logger


def _logger() -> logging.Logger:
    return get_logger("tradingbot.reporter")


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_iso(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


def _iso(dtobj: dt.datetime) -> str:
    return dtobj.replace(microsecond=0).isoformat()


def _signals_between(start_iso: str, end_iso: str):
    with db_session() as conn:
        return conn.execute(
            """
            SELECT *
            FROM signals
            WHERE created_at >= ? AND created_at < ?
            ORDER BY narrative_score DESC
            """,
            (start_iso, end_iso),
        ).fetchall()


def _pending_backtest_targets(now: dt.datetime):
    cutoff = _iso((now - dt.timedelta(days=7)))
    with db_session() as conn:
        return conn.execute(
            """
            SELECT *
            FROM signals
            WHERE outcome = 'pending'
              AND created_at <= ?
              AND price_day7 IS NULL
            ORDER BY created_at ASC
            """,
            (cutoff,),
        ).fetchall()


def _price_on_or_after(ticker: str, ts: dt.datetime, days: int) -> float | None:
    try:
        return close_on_or_after(ticker, ts, days)
    except Exception:
        return None


def _apply_backtests(now: dt.datetime, log: logging.Logger) -> int:
    targets = _pending_backtest_targets(now)
    if not targets:
        return 0

    updated = 0
    with db_session() as conn:
        for s in targets:
            sid = int(s["id"])
            ticker = str(s["ticker"])
            created_at = _parse_iso(s["created_at"])
            entry = float(s["price_at_signal"])

            p1 = _price_on_or_after(ticker, created_at, 1)
            p3 = _price_on_or_after(ticker, created_at, 3)
            p7 = _price_on_or_after(ticker, created_at, 7)
            if p7 is None:
                continue
            outcome = "win" if float(p7) > entry else "loss"

            conn.execute(
                """
                UPDATE signals
                SET price_day1 = COALESCE(price_day1, ?),
                    price_day3 = COALESCE(price_day3, ?),
                    price_day7 = COALESCE(price_day7, ?),
                    outcome = ?
                WHERE id = ?
                """,
                (p1, p3, p7, outcome, sid),
            )
            updated += 1
            log.info("backtest updated id=%s ticker=%s outcome=%s", sid, ticker, outcome)

    return updated


def _sector_heat_last7d(now: dt.datetime) -> dict[str, int]:
    since = _iso(now - dt.timedelta(days=7))
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT sector, COUNT(*) AS n
            FROM tagged_stories
            WHERE tagged_at >= ?
            GROUP BY sector
            ORDER BY n DESC
            """,
            (since,),
        ).fetchall()
    return {str(r["sector"] or "other"): int(r["n"]) for r in rows}


TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Narrative Momentum Weekly Digest</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; color: #111; }
    h1, h2 { margin: 0 0 10px 0; }
    .meta { color: #555; margin-bottom: 20px; }
    table { border-collapse: collapse; width: 100%; margin: 12px 0 28px 0; }
    th, td { border-bottom: 1px solid #e6e6e6; padding: 10px 8px; text-align: left; }
    th { font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; color: #333; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #f2f2f2; font-size: 12px; }
    .win { background: #e6f7ea; }
    .loss { background: #fde7e7; }
  </style>
</head>
<body>
  <h1>Narrative Momentum Weekly Digest</h1>
  <div class="meta">
    Generated {{ generated_at }} · Threshold {{ threshold }} · Watchlist {{ watchlist_n }} tickers
  </div>

  <h2>Top signals (last 7 days)</h2>
  {% if top_signals %}
  <table>
    <thead>
      <tr>
        <th>Ticker</th>
        <th>Score</th>
        <th>Entry</th>
        <th>Suggested size ($)</th>
        <th>First mentioned</th>
        <th>Created</th>
        <th>Outcome</th>
      </tr>
    </thead>
    <tbody>
      {% for s in top_signals %}
      <tr>
        <td><b>{{ s.ticker }}</b></td>
        <td>{{ "%.2f"|format(s.narrative_score or 0) }}</td>
        <td>{{ "%.2f"|format(s.price_at_signal or 0) }}</td>
        <td>{{ "%.2f"|format(s.suggested_size or 0) }}</td>
        <td>{{ s.first_mentioned or "" }}</td>
        <td>{{ s.created_at }}</td>
        <td>
          {% if s.outcome in ["win","loss"] %}
            <span class="pill {{ s.outcome }}">{{ s.outcome }}</span>
          {% else %}
            <span class="pill">pending</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p>No signals fired in the last 7 days.</p>
  {% endif %}

  <h2>Backtest updates (filled this run)</h2>
  <p>Updated {{ backtests_updated }} pending signals with day7 outcomes.</p>

  <h2>Sector heat (tag volume, last 7 days)</h2>
  {% if sector_heat %}
    <table>
      <thead><tr><th>Sector</th><th>Tagged stories</th></tr></thead>
      <tbody>
        {% for sector, n in sector_heat.items() %}
          <tr><td>{{ sector }}</td><td>{{ n }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p>No tagged stories found for the last 7 days.</p>
  {% endif %}
</body>
</html>
"""


def run() -> None:
    log = _logger()
    log.info("reporter.run start")
    now = _now_utc()
    backtests_updated = _apply_backtests(now, log)

    start = _iso(now - dt.timedelta(days=7))
    end = _iso(now)
    signals = _signals_between(start, end)
    top_signals = [dict(r) for r in signals[:5]]

    env = jinja2.Environment(autoescape=True)
    html = env.from_string(TEMPLATE).render(
        generated_at=_iso(now),
        threshold=config.NARRATIVE_THRESHOLD,
        watchlist_n=len(config.WATCHLIST),
        top_signals=top_signals,
        sector_heat=_sector_heat_last7d(now),
        backtests_updated=backtests_updated,
    )

    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"week_{now.date().isoformat().replace('-', '_')}.html"
    out_path.write_text(html, encoding="utf-8")
    log.info("report saved %s", out_path)
    log.info("reporter.run done")


if __name__ == "__main__":
    run()
