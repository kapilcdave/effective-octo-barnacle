from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from typing import Any

import feedparser
import requests

import config
from db import db_session


def _logger() -> logging.Logger:
    log = logging.getLogger("tradingbot.scraper")
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


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _yesterday_iso_date() -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).date().isoformat()


def _extract_ticker(text: str) -> str | None:
    match = re.search(r"\(([A-Z]{1,5})\)", text or "")
    if not match:
        return None
    return match.group(1)


def _extract_8k_items(text: str) -> set[str]:
    return set(re.findall(r"\b([1-9]\.\d{2})\b", text or ""))


def _insert_raw_story(
    *,
    url: str,
    headline: str,
    body: str,
    source: str,
) -> bool:
    url_hash = _sha256(url)
    fetched_at = _now_utc_iso()
    with db_session() as conn:
        try:
            conn.execute(
                """
                INSERT INTO raw_stories(url_hash, url, headline, body, source, fetched_at, tagged)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (url_hash, url, headline, body, source, fetched_at),
            )
            return True
        except Exception:
            # Usually UNIQUE constraint on url_hash.
            return False


def _fetch_edgar_for_ticker(ticker: str, start_date: str) -> list[dict[str, Any]]:
    # Endpoint given in spec. Response format can change; keep parsing defensive.
    url = f"https://efts.sec.gov/LATEST/search-index?q={ticker}&dateRange=custom&startdt={start_date}"
    headers = {
        "User-Agent": config.SEC_USER_AGENT,
        "Accept": "application/json",
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    payload = r.json()

    # Common shapes:
    # - {"hits": {"hits": [ {"_source": {...}}, ... ] } }
    # - {"hits": [ ... ] }
    hits_obj = payload.get("hits")
    hits = []
    if isinstance(hits_obj, dict):
        hits = hits_obj.get("hits", []) or []
    elif isinstance(hits_obj, list):
        hits = hits_obj
    else:
        hits = []

    out: list[dict[str, Any]] = []
    for h in hits:
        src = h.get("_source", h) if isinstance(h, dict) else {}
        if not isinstance(src, dict):
            continue
        out.append(src)
    return out


def _edgar_story_url(src: dict[str, Any]) -> str:
    # Best-effort: SEC provides a variety of fields; try common ones.
    # If we can't build a canonical archive URL, store the search result context as url.
    for key in ("linkToFilingDetails", "linkToHtml", "linkToTxt"):
        v = src.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v

    # Some responses include "ciks" / "cik" and "adsh"/"accessionNumber".
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
            cik_str = f"{cik_int:010d}"
            acc = str(accession).replace("-", "")
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik_int)}/{acc}/"
        except Exception:
            pass

    # Fallback: hashable stable pseudo-url.
    return "edgar-search://" + _sha256(json.dumps(src, sort_keys=True, default=str))


def _normalize_edgar_story(src: dict[str, Any], ticker: str) -> tuple[str, str, str]:
    filing_type = src.get("formType") or src.get("form") or src.get("file_type") or ""
    filed_at = src.get("filedAt") or src.get("filed_at") or src.get("filed") or ""
    title = src.get("title") or src.get("display_names") or src.get("entityName") or ""

    headline = f"{ticker} {filing_type}".strip()
    if title:
        headline = f"{headline} — {title}".strip(" —")

    body_obj = {
        "ticker": ticker,
        "form": filing_type,
        "filed_at": filed_at,
        "summary": src.get("summary") or src.get("description") or "",
        "raw": src,
    }
    body = json.dumps(body_obj, ensure_ascii=False)
    url = _edgar_story_url(src)
    return url, headline, body


def _scrape_edgar(log: logging.Logger) -> int:
    headers = {"User-Agent": config.SEC_USER_AGENT}
    response = requests.get(config.EDGAR_CURRENT_8K_ATOM, headers=headers, timeout=25)
    response.raise_for_status()
    parsed = feedparser.parse(response.text)
    inserted = 0
    if getattr(parsed, "bozo", 0):
        log.warning(
            "EDGAR current feed parse bozo=1: %s",
            getattr(parsed, "bozo_exception", ""),
        )

    for entry in getattr(parsed, "entries", [])[:100]:
        headline = (getattr(entry, "title", "") or "").strip()
        summary = (getattr(entry, "summary", "") or "").strip()
        ticker = _extract_ticker(headline)
        if not ticker:
            continue

        items = sorted(_extract_8k_items(f"{headline}\n{summary}"))
        if not set(items).intersection(config.HIGH_SIGNAL_8K_ITEMS):
            continue

        url = _entry_url(entry)
        if not url:
            continue

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
        if _insert_raw_story(url=url, headline=headline, body=body, source="edgar"):
            inserted += 1
            log.info("captured edgar ticker=%s items=%s", ticker, ",".join(items))

    return inserted


def _entry_url(entry: Any) -> str | None:
    for key in ("link", "id", "guid"):
        v = getattr(entry, key, None)
        if isinstance(v, str) and v:
            return v
    if isinstance(getattr(entry, "links", None), list) and entry.links:
        href = entry.links[0].get("href")
        if isinstance(href, str) and href:
            return href
    return None


def _scrape_rss_feed(feed_url: str, source: str, log: logging.Logger) -> int:
    parsed = feedparser.parse(feed_url)
    if getattr(parsed, "bozo", 0):
        log.warning("RSS parse bozo=1 for %s: %s", feed_url, getattr(parsed, "bozo_exception", ""))
    inserted = 0
    for entry in getattr(parsed, "entries", [])[:50]:
        url = _entry_url(entry)
        if not url:
            continue
        headline = (getattr(entry, "title", "") or "").strip()
        summary = (getattr(entry, "summary", "") or "").strip()
        body = json.dumps(
            {"headline": headline, "summary": summary, "raw": dict(entry)},
            ensure_ascii=False,
            default=str,
        )
        if _insert_raw_story(url=url, headline=headline, body=body, source=source):
            inserted += 1
    return inserted


def run() -> None:
    log = _logger()
    log.info("scraper.run start")
    inserted_edgar = _scrape_edgar(log)
    inserted_reuters = _scrape_rss_feed(config.REUTERS_RSS, "rss", log)
    inserted_ap = _scrape_rss_feed(config.AP_RSS, "rss", log)
    log.info(
        "scraper.run done inserted_edgar=%s inserted_rss=%s",
        inserted_edgar,
        (inserted_reuters + inserted_ap),
    )


if __name__ == "__main__":
    run()
