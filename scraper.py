from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import feedparser
import requests

import config
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


def _yesterday_iso_date() -> str:
    return (_now_utc() - dt.timedelta(days=1)).date().isoformat()


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


def _extract_watchlist_tickers(text: str) -> set[str]:
    blob = (text or "").upper()
    found: set[str] = set()
    for match in re.finditer(r"\(([A-Z]{1,5})\)", blob):
        symbol = match.group(1)
        if symbol in config.WATCHLIST_SET:
            found.add(symbol)
    for symbol in config.WATCHLIST_SET:
        if re.search(rf"\b{re.escape(symbol)}\b", blob):
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
        self._rows: list[tuple[str, str, str, str, str, str, str]] = []

    def add(self, *, url: str, headline: str, body: str, source: str) -> None:
        fetched_at = _now_utc_iso()
        self._rows.append((_sha256(url), url, headline, body, source, fetched_at, 0))

    def flush(self) -> tuple[int, int]:
        if not self._rows:
            return 0, 0
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
            except Exception:
                duplicates += 1
        self._rows.clear()
        return inserted, duplicates


def _fetch_edgar_for_ticker(
    session: requests.Session,
    ticker: str,
    start_date: str,
) -> list[dict[str, Any]]:
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q={ticker}&forms=8-K&dateRange=custom&startdt={start_date}"
    )
    r = session.get(url, timeout=25)
    r.raise_for_status()
    payload = r.json()

    hits_obj = payload.get("hits")
    hits: list[Any] = []
    if isinstance(hits_obj, dict):
        hits = hits_obj.get("hits", []) or []
    elif isinstance(hits_obj, list):
        hits = hits_obj

    out: list[dict[str, Any]] = []
    for h in hits:
        src = h.get("_source", h) if isinstance(h, dict) else {}
        if isinstance(src, dict):
            out.append(src)
    return out


def _edgar_story_url(src: dict[str, Any]) -> str:
    for key in ("linkToFilingDetails", "linkToHtml", "linkToTxt"):
        v = src.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v

    cik = src.get("cik") or src.get("cikNumber") or src.get("cik_number")
    accession = (
        src.get("adsh")
        or src.get("accessionNumber")
        or src.get("accession_number")
        or src.get("accn")
    )
    if cik and accession:
        try:
            cik_int = int(str(cik))
            acc = str(accession).replace("-", "")
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik_int)}/{acc}/"
        except Exception:
            pass

    return "edgar-search://" + _sha256(json.dumps(src, sort_keys=True, default=str))


def _normalize_edgar_story(src: dict[str, Any], ticker: str) -> tuple[str, str, str]:
    filing_type = src.get("formType") or src.get("form") or src.get("file_type") or ""
    filed_at = src.get("filedAt") or src.get("filed_at") or src.get("filed") or ""
    title = src.get("title") or src.get("display_names") or src.get("entityName") or ""

    headline = f"{ticker} {filing_type}".strip()
    if title:
        headline = f"{headline} — {title}".strip(" —")

    summary = src.get("summary") or src.get("description") or ""
    items = sorted(_extract_8k_items(f"{headline}\n{summary}"))
    body_obj = {
        "ticker": ticker,
        "form": filing_type or "8-K",
        "filed_at": filed_at,
        "items": items,
        "summary": summary,
        "raw": src,
    }
    body = json.dumps(body_obj, ensure_ascii=False, default=str)
    url = _edgar_story_url(src)
    return url, headline, body


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
) -> bool:
    headline = (getattr(entry, "title", "") or "").strip()
    summary = (getattr(entry, "summary", "") or "").strip()
    ticker = _extract_ticker(headline)
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


def _process_edgar_search_hit(
    src: dict[str, Any],
    ticker: str,
    batch: StoryBatch,
    stats: SkipStats,
) -> bool:
    filing_type = str(src.get("formType") or src.get("form") or src.get("file_type") or "")
    if filing_type and "8-K" not in filing_type.upper():
        stats.skip("not_8k")
        return False

    url, headline, body = _normalize_edgar_story(src, ticker)
    try:
        payload = json.loads(body)
    except Exception:
        stats.skip("bad_body")
        return False
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        items = []
    if not set(items).intersection(config.HIGH_SIGNAL_8K_ITEMS):
        stats.skip("no_high_signal_item")
        return False

    batch.add(url=url, headline=headline, body=body, source="edgar")
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

        _process_edgar_atom_entry(entry, batch, stats, log=log)

    if newest_url:
        _save_feed_state(
            conn,
            feed_key=feed_key,
            last_seen_url=newest_url,
            last_seen_published=newest_published,
            etag=etag,
        )


def _scrape_edgar_backfill(
    session: requests.Session,
    batch: StoryBatch,
    stats: SkipStats,
    log: logging.Logger,
) -> None:
    start_date = _yesterday_iso_date()
    for ticker in config.WATCHLIST:
        try:
            hits = _fetch_edgar_for_ticker(session, ticker, start_date)
        except Exception as e:
            stats.skip("backfill_error")
            log.warning("edgar backfill failed ticker=%s: %s", ticker, e)
            continue

        for src in hits:
            _process_edgar_search_hit(src, ticker, batch, stats)

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


def run() -> None:
    log = _logger()
    log.info("scraper.run start")
    stats = SkipStats()

    session = _sec_session()
    inserted = 0
    try:
        with db_session() as conn:
            batch = StoryBatch(conn)

            edgar_stats = SkipStats()
            _scrape_edgar_atom(session, batch, edgar_stats, log, conn)
            stats.merge(edgar_stats)
            edgar_stats.log_summary(log, "edgar_atom")

            if config.EDGAR_BACKFILL_ENABLED:
                backfill_stats = SkipStats()
                _scrape_edgar_backfill(session, batch, backfill_stats, log)
                stats.merge(backfill_stats)
                backfill_stats.log_summary(log, "edgar_backfill")

            reuters_stats = SkipStats()
            _scrape_rss_feed(
                session,
                config.REUTERS_RSS,
                "reuters_rss",
                batch,
                reuters_stats,
                log,
                conn,
            )
            stats.merge(reuters_stats)
            reuters_stats.log_summary(log, "reuters")

            ap_stats = SkipStats()
            _scrape_rss_feed(session, config.AP_RSS, "ap_rss", batch, ap_stats, log, conn)
            stats.merge(ap_stats)
            ap_stats.log_summary(log, "ap")

            inserted, duplicates = batch.flush()
            stats.skip("duplicate", duplicates)
    finally:
        session.close()

    stats.log_summary(log, "scraper.run total")
    log.info("scraper.run done inserted=%s", inserted)


if __name__ == "__main__":
    run()
