from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import feedparser
import requests

import alpaca
import config
import edgar
from db import db_session
from runtime import get_logger


def _logger() -> logging.Logger:
    return get_logger("tradingbot.scraper")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _now_utc_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat()


@dataclass
class SkipStats:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def skip(self, reason: str, n: int = 1) -> None:
        self.counts[reason] += n

    def merge(self, other: SkipStats) -> None:
        for reason, n in other.counts.items():
            self.counts[reason] += n

    def log_summary(self, log: logging.Logger, prefix: str) -> None:
        if not self.counts:
            return
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        log.info("%s skips: %s", prefix, parts)


def _sec_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.SEC_USER_AGENT,
            "Accept": "application/json, application/atom+xml, application/xml, text/xml, */*",
        }
    )
    return session


def _extract_ticker(text: str) -> str | None:
    match = re.search(r"\(([A-Z]{1,5})\)", text or "")
    if not match:
        return None
    return match.group(1)


def _ticker_from_edgar_title(text: str, *, session: requests.Session | None = None) -> str | None:
    """EDGAR titles carry a CIK, not a ticker: `8-K - Marpai, Inc. (0001844392) (Filer)`.

    Resolve via the SEC's official CIK->ticker map, falling back to a literal
    ticker in parens for non-EDGAR sources.
    """
    for match in re.finditer(r"\((\d{4,10})\)", text or ""):
        ticker = edgar.ticker_for_cik(match.group(1), session=session)
        if ticker:
            return ticker
    return _extract_ticker(text)


# Explicit ticker notations only. A bare word-boundary match on the watchlist
# attributes every headline containing "ON", "AR" or "GE" to ON Semiconductor,
# Antero Resources or General Electric.
_TICKER_PATTERNS = (
    re.compile(r"\((?:NYSE|NASDAQ|NYSE\s*AMERICAN|AMEX|CBOE|OTCQB|OTCQX|OTC)\s*:\s*([A-Z][A-Z.]{0,5})\)"),
    re.compile(r"(?:NYSE|NASDAQ|NYSE\s*AMERICAN|AMEX|CBOE|OTCQB|OTCQX|OTC)\s*:\s*([A-Z][A-Z.]{0,5})\b"),
    re.compile(r"\(([A-Z]{1,5})\)"),
    re.compile(r"\$([A-Z]{1,5})\b"),
)


def _extract_watchlist_tickers(text: str) -> set[str]:
    blob = (text or "").upper()
    found: set[str] = set()
    for pattern in _TICKER_PATTERNS:
        for match in pattern.finditer(blob):
            symbol = match.group(1).strip(".")
            if symbol in config.WATCHLIST_SET:
                found.add(symbol)
    return found


def _extract_8k_items(text: str) -> set[str]:
    return set(re.findall(r"\b([1-9]\.\d{2})\b", text or ""))


def _entry_published(entry: Any) -> dt.datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    try:
        return dt.datetime(*parsed[:6], tzinfo=dt.UTC)
    except Exception:
        return None


def _entry_is_recent(entry: Any, max_age_hours: float) -> bool:
    published = _entry_published(entry)
    if published is None:
        return True
    age = _now_utc() - published
    return age <= dt.timedelta(hours=max_age_hours)


def _rss_matches_filters(headline: str, summary: str) -> bool:
    blob = f"{headline}\n{summary}"
    if _extract_watchlist_tickers(blob):
        return True
    if config.RSS_REQUIRE_WATCHLIST_TICKER:
        # Keyword-only matches drag in the whole newswire firehose (foreign-language
        # CFO notices, law-firm releases, microcaps), all of which get dropped later
        # by the watchlist and tradeability gates after paying for a Gemini call.
        return False
    lower = blob.lower()
    return any(kw in lower for kw in config.RSS_CATALYST_KEYWORDS)


def _load_feed_state(conn: Any, feed_key: str) -> dict[str, str | None]:
    row = conn.execute(
        "SELECT last_seen_url, last_seen_published, etag FROM scraper_state WHERE feed_key = ?",
        (feed_key,),
    ).fetchone()
    if not row:
        return {"last_seen_url": None, "last_seen_published": None, "etag": None}
    return {
        "last_seen_url": row["last_seen_url"],
        "last_seen_published": row["last_seen_published"],
        "etag": row["etag"],
    }


def _save_feed_state(
    conn: Any,
    *,
    feed_key: str,
    last_seen_url: str | None,
    last_seen_published: str | None,
    etag: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO scraper_state(feed_key, last_seen_url, last_seen_published, etag, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(feed_key) DO UPDATE SET
          last_seen_url = excluded.last_seen_url,
          last_seen_published = excluded.last_seen_published,
          etag = COALESCE(excluded.etag, scraper_state.etag),
          updated_at = excluded.updated_at
        """,
        (feed_key, last_seen_url, last_seen_published, etag, _now_utc_iso()),
    )


class StoryBatch:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._rows: list[tuple[str, str, str, str, str, str]] = []

    def add(self, *, url: str, headline: str, body: str, source: str) -> None:
        fetched_at = _now_utc_iso()
        # `tagged` is a literal 0 in the INSERT, so it must not be bound here.
        self._rows.append((_sha256(url), url, headline, body, source, fetched_at))

    def flush(self) -> tuple[int, int]:
        if not self._rows:
            return 0, 0
        log = _logger()
        inserted = 0
        duplicates = 0
        for row in self._rows:
            try:
                self._conn.execute(
                    """
                    INSERT INTO raw_stories(url_hash, url, headline, body, source, fetched_at, tagged)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    row,
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # url_hash is UNIQUE: a genuine repeat of a story we already have.
                duplicates += 1
            except Exception as e:
                # Never swallow this again: a binding/schema bug counted every
                # insert as a duplicate and silently emptied the pipeline.
                log.error("story insert failed url=%s: %s: %s", row[1], type(e).__name__, e)
        self._rows.clear()
        return inserted, duplicates


def _entry_url(entry: Any) -> str | None:
    for key in ("link", "id", "guid"):
        v = getattr(entry, key, None)
        if isinstance(v, str) and v:
            return v
    links = getattr(entry, "links", None)
    if isinstance(links, list) and links:
        href = links[0].get("href")
        if isinstance(href, str) and href:
            return href
    return None


def _http_get_feed(
    session: requests.Session,
    feed_url: str,
    *,
    feed_key: str,
    conn: Any,
) -> tuple[str | None, str | None]:
    state = _load_feed_state(conn, feed_key)
    headers: dict[str, str] = {}
    if state["etag"]:
        headers["If-None-Match"] = state["etag"]
    r = session.get(feed_url, headers=headers, timeout=25)
    if r.status_code == 304:
        return None, state["etag"]
    r.raise_for_status()
    etag = r.headers.get("ETag") or state["etag"]
    return r.text, etag


def _process_edgar_atom_entry(
    entry: Any,
    batch: StoryBatch,
    stats: SkipStats,
    *,
    log: logging.Logger,
    session: requests.Session | None = None,
) -> bool:
    headline = (getattr(entry, "title", "") or "").strip()
    summary = (getattr(entry, "summary", "") or "").strip()
    ticker = _ticker_from_edgar_title(headline, session=session)
    if not ticker:
        stats.skip("no_ticker")
        return False
    if ticker not in config.WATCHLIST_SET:
        stats.skip("not_on_watchlist")
        return False

    items = sorted(_extract_8k_items(f"{headline}\n{summary}"))
    if not set(items).intersection(config.HIGH_SIGNAL_8K_ITEMS):
        stats.skip("no_high_signal_item")
        return False

    url = _entry_url(entry)
    if not url:
        stats.skip("no_url")
        return False

    body = json.dumps(
        {
            "ticker": ticker,
            "form": "8-K",
            "items": items,
            "summary": summary,
            "raw": dict(entry),
        },
        ensure_ascii=False,
        default=str,
    )
    batch.add(url=url, headline=headline, body=body, source="edgar")
    log.info("queued edgar atom ticker=%s items=%s", ticker, ",".join(items))
    return True


def _scrape_edgar_atom(
    session: requests.Session,
    batch: StoryBatch,
    stats: SkipStats,
    log: logging.Logger,
    conn: Any,
) -> None:
    feed_key = "edgar_atom"
    text, etag = _http_get_feed(session, config.EDGAR_CURRENT_8K_ATOM, feed_key=feed_key, conn=conn)
    if text is None:
        stats.skip("not_modified")
        log.info("edgar atom feed not modified (304)")
        return

    parsed = feedparser.parse(text)
    if getattr(parsed, "bozo", 0):
        log.warning(
            "EDGAR current feed parse bozo=1: %s",
            getattr(parsed, "bozo_exception", ""),
        )

    state = _load_feed_state(conn, feed_key)
    last_seen_url = state["last_seen_url"]
    entries = list(getattr(parsed, "entries", [])[: config.EDGAR_ATOM_MAX_ENTRIES])
    newest_url: str | None = None
    newest_published: str | None = None

    for entry in entries:
        url = _entry_url(entry)
        if url and newest_url is None:
            newest_url = url
            published = _entry_published(entry)
            if published:
                newest_published = published.isoformat()

        if last_seen_url and url == last_seen_url:
            stats.skip("cursor_hit")
            break

        _process_edgar_atom_entry(entry, batch, stats, log=log, session=session)

    if newest_url:
        _save_feed_state(
            conn,
            feed_key=feed_key,
            last_seen_url=newest_url,
            last_seen_published=newest_published,
            etag=etag,
        )


def _process_edgar_filing(
    filing: dict[str, Any],
    batch: StoryBatch,
    stats: SkipStats,
    log: logging.Logger,
    session: requests.Session | None = None,
) -> bool:
    """Queue one 8-K from data.sec.gov/submissions (items already parsed by the SEC)."""
    items = sorted(str(i).strip() for i in (filing.get("items") or []) if str(i).strip())
    if not items:
        stats.skip("no_items")
        return False
    if not set(items).intersection(config.HIGH_SIGNAL_8K_ITEMS):
        stats.skip("no_high_signal_item")
        return False

    ticker = str(filing["ticker"]).upper()
    headline = "{} 8-K — {}".format(ticker, filing.get("company") or "")
    description = str(filing.get("description") or "").strip()
    if description:
        headline = "{}: {}".format(headline, description)

    # Item codes alone carry no direction: a beat and a miss are both "2.02".
    content = ""
    content_url = None
    if config.EDGAR_FETCH_DOCUMENT_TEXT:
        content, content_url = edgar.filing_content_text(
            filing, session=session, max_chars=config.EDGAR_DOC_MAX_CHARS
        )
        if not content:
            stats.skip("no_document_text")
            log.info("no document text ticker=%s acc=%s", ticker, filing.get("accession"))

    body = json.dumps(
        {
            "ticker": ticker,
            "form": "8-K",
            "items": items,
            "filed_at": filing.get("accepted_at") or filing.get("filing_date"),
            "summary": "Items " + ", ".join(items),
            "content": content,
            "content_url": content_url,
            "raw": filing,
        },
        ensure_ascii=False,
        default=str,
    )
    batch.add(url=str(filing["url"]), headline=headline, body=body, source="edgar")
    log.info(
        "queued edgar filing ticker=%s date=%s items=%s doc=%s chars=%s",
        ticker,
        filing.get("filing_date"),
        ",".join(items),
        (content_url or "-").rsplit("/", 1)[-1],
        len(content),
    )
    return True


def _scrape_edgar_backfill(
    session: requests.Session,
    batch: StoryBatch,
    stats: SkipStats,
    log: logging.Logger,
) -> None:
    """Per-ticker 8-K pull straight from the SEC submissions API.

    Replaces the old efts.sec.gov full-text search, which matched any filing
    merely *mentioning* the ticker (AMD returned Spansion filings from 2005)
    and ignored the date window.
    """
    since = (_now_utc() - dt.timedelta(days=config.EDGAR_BACKFILL_DAYS)).date()
    for ticker in config.WATCHLIST:
        try:
            filings = edgar.recent_8k_filings(ticker, since=since, session=session)
        except Exception as e:
            stats.skip("backfill_error")
            log.warning("edgar backfill failed ticker=%s: %s", ticker, e)
            continue

        for filing in filings:
            _process_edgar_filing(filing, batch, stats, log, session)

        time.sleep(config.SEC_REQUEST_DELAY_SEC)


def _scrape_rss_feed(
    session: requests.Session,
    feed_url: str,
    feed_key: str,
    batch: StoryBatch,
    stats: SkipStats,
    log: logging.Logger,
    conn: Any,
) -> None:
    text, etag = _http_get_feed(session, feed_url, feed_key=feed_key, conn=conn)
    if text is None:
        stats.skip("not_modified")
        log.info("rss feed not modified feed=%s", feed_key)
        return

    parsed = feedparser.parse(text)
    if getattr(parsed, "bozo", 0):
        log.warning(
            "RSS parse bozo=1 for %s: %s",
            feed_url,
            getattr(parsed, "bozo_exception", ""),
        )

    state = _load_feed_state(conn, feed_key)
    last_seen_url = state["last_seen_url"]
    entries = list(getattr(parsed, "entries", [])[: config.RSS_MAX_ENTRIES])
    newest_url: str | None = None
    newest_published: str | None = None

    for entry in entries:
        url = _entry_url(entry)
        if url and newest_url is None:
            newest_url = url
            published = _entry_published(entry)
            if published:
                newest_published = published.isoformat()

        if last_seen_url and url == last_seen_url:
            stats.skip("cursor_hit")
            break

        if not url:
            stats.skip("no_url")
            continue

        if not _entry_is_recent(entry, config.RSS_MAX_AGE_HOURS):
            stats.skip("too_old")
            continue

        headline = (getattr(entry, "title", "") or "").strip()
        summary = (getattr(entry, "summary", "") or "").strip()
        if not _rss_matches_filters(headline, summary):
            stats.skip("rss_filter")
            continue

        tickers = sorted(_extract_watchlist_tickers(f"{headline}\n{summary}"))
        body = json.dumps(
            {
                "headline": headline,
                "summary": summary,
                "tickers": tickers,
                "ticker": tickers[0] if tickers else None,
                "raw": dict(entry),
            },
            ensure_ascii=False,
            default=str,
        )
        batch.add(url=url, headline=headline, body=body, source="rss")

    if newest_url:
        _save_feed_state(
            conn,
            feed_key=feed_key,
            last_seen_url=newest_url,
            last_seen_published=newest_published,
            etag=etag,
        )


def _scrape_alpaca_news(
    batch: StoryBatch,
    stats: SkipStats,
    log: logging.Logger,
) -> None:
    """Alpaca's Benzinga news feed, filtered to watchlist symbols.

    Articles arrive already tagged with tickers, so there is no ticker guessing
    and no foreign-language newswire noise.
    """
    start = (_now_utc() - dt.timedelta(hours=config.ALPACA_NEWS_LOOKBACK_HOURS)).replace(microsecond=0)
    articles = alpaca.news(
        sorted(config.WATCHLIST_SET),
        start=start.isoformat().replace("+00:00", "Z"),
        limit=config.ALPACA_NEWS_LIMIT,
    )

    for article in articles:
        symbols = [str(s).upper() for s in (article.get("symbols") or [])]
        watchlist_hits = sorted(set(symbols) & config.WATCHLIST_SET)
        if not watchlist_hits:
            stats.skip("not_on_watchlist")
            continue

        url = str(article.get("url") or "").strip()
        headline = str(article.get("headline") or "").strip()
        if not headline:
            stats.skip("no_headline")
            continue
        if not url:
            url = "alpaca-news://" + str(article.get("id") or _sha256(headline))

        body = json.dumps(
            {
                "ticker": watchlist_hits[0],
                "tickers": watchlist_hits,
                "form": "news",
                "summary": str(article.get("summary") or ""),
                "filed_at": article.get("created_at"),
                "raw": {
                    "id": article.get("id"),
                    "source": article.get("source"),
                    "author": article.get("author"),
                    "symbols": symbols,
                },
            },
            ensure_ascii=False,
            default=str,
        )
        batch.add(url=url, headline=headline, body=body, source="alpaca_news")
        log.info("queued alpaca news tickers=%s %s", ",".join(watchlist_hits), headline[:60])


def _run_source(
    name: str,
    fn: Any,
    stats: SkipStats,
    log: logging.Logger,
) -> None:
    """Run one source in isolation.

    A single dead feed must never abort the run: the old code let a DNS failure
    on the Reuters feed propagate out of run(), so batch.flush() never executed
    and nothing was committed at all.
    """
    source_stats = SkipStats()
    try:
        fn(source_stats)
    except Exception as e:
        source_stats.skip("source_error")
        log.warning("source failed %s: %s: %s", name, type(e).__name__, e)
    stats.merge(source_stats)
    source_stats.log_summary(log, name)


def run() -> None:
    log = _logger()
    log.info("scraper.run start")
    stats = SkipStats()

    session = _sec_session()
    inserted = 0
    duplicates = 0
    try:
        with db_session() as conn:
            batch = StoryBatch(conn)

            _run_source(
                "edgar_atom",
                lambda s: _scrape_edgar_atom(session, batch, s, log, conn),
                stats,
                log,
            )

            if config.EDGAR_BACKFILL_ENABLED:
                _run_source(
                    "edgar_backfill",
                    lambda s: _scrape_edgar_backfill(session, batch, s, log),
                    stats,
                    log,
                )

            for feed_key, feed_url in config.RSS_FEEDS:
                _run_source(
                    feed_key,
                    lambda s, u=feed_url, k=feed_key: _scrape_rss_feed(
                        session, u, k, batch, s, log, conn
                    ),
                    stats,
                    log,
                )

            if config.ALPACA_NEWS_ENABLED:
                _run_source(
                    "alpaca_news",
                    lambda s: _scrape_alpaca_news(batch, s, log),
                    stats,
                    log,
                )

            inserted, duplicates = batch.flush()
            stats.skip("duplicate", duplicates)
    finally:
        session.close()

    stats.log_summary(log, "scraper.run total")
    log.info("scraper.run done inserted=%s duplicates=%s", inserted, duplicates)


if __name__ == "__main__":
    run()
