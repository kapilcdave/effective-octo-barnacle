"""SEC EDGAR helpers: CIK <-> ticker mapping and per-company 8-K filings.

EDGAR identifies companies by CIK, never by ticker, so the atom feed and the
submissions API both need the official mapping from company_tickers.json.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

import config
from runtime import get_logger

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

_CACHE_PATH = Path(__file__).resolve().parent / "data" / "cik_tickers.json"
_CACHE_MAX_AGE_HOURS = 24.0

_memo: dict[str, Any] = {}


def _logger() -> logging.Logger:
    return get_logger("tradingbot.edgar")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": config.SEC_USER_AGENT, "Accept": "application/json"})
    return session


def _cache_is_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    age_hours = (time.time() - _CACHE_PATH.stat().st_mtime) / 3600.0
    return age_hours < _CACHE_MAX_AGE_HOURS


def _download_map(session: requests.Session | None = None) -> dict:
    own = session is None
    session = session or _session()
    try:
        r = session.get(COMPANY_TICKERS_URL, timeout=30)
        r.raise_for_status()
        payload = r.json()
    finally:
        if own:
            session.close()

    # {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    by_cik: dict[str, str] = {}
    for row in payload.values() if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper().strip()
        cik = row.get("cik_str")
        if not ticker or cik is None:
            continue
        try:
            # First occurrence wins: company_tickers.json lists share classes
            # under one CIK (GOOG/GOOGL, BRK.A/BRK.B) and the earlier entry is
            # the primary listing.
            by_cik.setdefault(str(int(cik)), ticker)
        except (TypeError, ValueError):
            continue
    return by_cik


def cik_ticker_map(*, session: requests.Session | None = None, refresh: bool = False) -> dict:
    """{cik_as_int_string: TICKER}, cached on disk for a day."""
    if not refresh and "by_cik" in _memo:
        return _memo["by_cik"]

    if not refresh and _cache_is_fresh():
        try:
            by_cik = json.loads(_CACHE_PATH.read_text())
            if isinstance(by_cik, dict) and by_cik:
                _memo["by_cik"] = by_cik
                return by_cik
        except Exception:
            pass

    by_cik = _download_map(session)
    if by_cik:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(by_cik))
        _logger().info("cik map refreshed entries=%s", len(by_cik))
    _memo["by_cik"] = by_cik
    return by_cik


def ticker_for_cik(cik: Any, *, session: requests.Session | None = None) -> str | None:
    try:
        key = str(int(str(cik).strip()))
    except (TypeError, ValueError):
        return None
    return cik_ticker_map(session=session).get(key)


def cik_for_ticker(ticker: str, *, session: requests.Session | None = None) -> int | None:
    target = str(ticker).upper().strip()
    by_cik = cik_ticker_map(session=session)
    reverse = _memo.get("by_ticker")
    if reverse is None or _memo.get("by_ticker_src") is not by_cik:
        reverse = {v: int(k) for k, v in by_cik.items()}
        _memo["by_ticker"] = reverse
        _memo["by_ticker_src"] = by_cik
    return reverse.get(target)


def _filing_url(cik: int, accession: str, primary_doc: str | None) -> str:
    acc = str(accession).replace("-", "")
    base = "https://www.sec.gov/Archives/edgar/data/{}/{}".format(int(cik), acc)
    return base + "/" + primary_doc if primary_doc else base + "/"


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def filing_text(url: str, *, session: requests.Session | None = None, max_chars: int = 8000) -> str:
    """Plain text of a filing document.

    Item codes alone tell a model nothing about direction: an earnings beat and a
    miss are both `2.02`. Sentiment requires the actual document body.
    """
    own = session is None
    session = session or _session()
    try:
        r = session.get(url, timeout=30, headers={"Accept": "text/html,application/xhtml+xml,*/*"})
        r.raise_for_status()
        raw = r.text
    finally:
        if own:
            session.close()

    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = _XBRL_TOKEN_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _NL_RE.sub("\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + " [TRUNCATED]"
    return text


# Real-world exhibit filenames: exhibit9912026q2pressrelea.htm, d56853dex991.htm,
# etn06302026exhibit99.htm. No delimiter is guaranteed before "ex"/"exhibit", so
# anchoring on one drops most of them.
_EXHIBIT_991_RE = re.compile(r"(?:ex|exhibit)[-_. ]?99[-_. ]?0?1", re.IGNORECASE)
_EXHIBIT_99_RE = re.compile(r"(?:ex|exhibit)[-_. ]?99", re.IGNORECASE)
_DOC_SUFFIXES = (".htm", ".html", ".txt")
# XBRL inline junk that survives tag stripping: "us-gaap:CommonStockMember".
_XBRL_TOKEN_RE = re.compile(r"\b[a-zA-Z][\w-]*:[A-Za-z0-9._-]+\b")


def filing_directory_url(cik: int, accession: str) -> str:
    return "https://www.sec.gov/Archives/edgar/data/{}/{}".format(
        int(cik), str(accession).replace("-", "")
    )


def press_release_url(
    cik: int,
    accession: str,
    *,
    session: requests.Session | None = None,
) -> str | None:
    """URL of the Exhibit 99.x press release in a filing, if there is one.

    For an Item 2.02 earnings 8-K the body is cover-page boilerplate that merely
    references the release; the numbers a model needs are in the exhibit.
    `index.json`'s `type` field is only an icon name, so match on filename.
    """
    own = session is None
    session = session or _session()
    base = filing_directory_url(cik, accession)
    try:
        r = session.get(base + "/index.json", timeout=30)
        r.raise_for_status()
        items = ((r.json().get("directory") or {}).get("item")) or []
    except Exception:
        return None
    finally:
        if own:
            session.close()

    names = [
        str(item.get("name") or "")
        for item in items
        if str(item.get("name") or "").lower().endswith(_DOC_SUFFIXES)
    ]
    for pattern in (_EXHIBIT_991_RE, _EXHIBIT_99_RE):
        for name in names:
            if pattern.search(name):
                return base + "/" + name
    return None


def filing_content_text(
    filing: dict,
    *,
    session: requests.Session | None = None,
    max_chars: int = 8000,
) -> tuple[str, str | None]:
    """(text, source_url) preferring the Exhibit 99 press release."""
    own = session is None
    session = session or _session()
    try:
        url = None
        if filing.get("cik") and filing.get("accession"):
            url = press_release_url(int(filing["cik"]), str(filing["accession"]), session=session)
            if url:
                time.sleep(config.SEC_REQUEST_DELAY_SEC)
        url = url or filing.get("url")
        if not url:
            return "", None
        return filing_text(str(url), session=session, max_chars=max_chars), str(url)
    except Exception:
        return "", None
    finally:
        if own:
            session.close()


def recent_8k_filings(
    ticker: str,
    *,
    since: dt.date,
    session: requests.Session | None = None,
    max_filings: int = 50,
) -> list:
    """8-K filings for one ticker filed on/after `since`, newest first.

    Uses data.sec.gov/submissions, which returns the SEC's own parsed item list
    per filing. No full-text search, so no cross-company contamination.
    """
    own = session is None
    session = session or _session()
    try:
        cik = cik_for_ticker(ticker, session=session)
        if cik is None:
            return []
        r = session.get(SUBMISSIONS_URL.format(cik=cik), timeout=30)
        r.raise_for_status()
        payload = r.json()
    finally:
        if own:
            session.close()

    recent = ((payload.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    company = str(payload.get("name") or ticker)
    since_iso = since.isoformat()

    out = []
    for i, form in enumerate(forms):
        if str(form).upper() != "8-K":
            continue
        filing_date = str((recent.get("filingDate") or [""])[i] or "")
        if filing_date < since_iso:
            continue
        items = [
            item.strip()
            for item in str((recent.get("items") or [""])[i] or "").split(",")
            if item.strip()
        ]
        accession = str((recent.get("accessionNumber") or [""])[i] or "")
        primary_doc = (recent.get("primaryDocument") or [""])[i] or ""
        out.append(
            {
                "ticker": ticker.upper(),
                "cik": cik,
                "company": company,
                "form": "8-K",
                "filing_date": filing_date,
                "accepted_at": str((recent.get("acceptanceDateTime") or [""])[i] or ""),
                "items": items,
                "accession": accession,
                "description": str((recent.get("primaryDocDescription") or [""])[i] or ""),
                "url": _filing_url(cik, accession, primary_doc),
            }
        )
        if len(out) >= max_filings:
            break
    return out
