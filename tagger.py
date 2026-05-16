from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from typing import Any

import requests

import config
from db import db_session
from runtime import get_logger


TAGGING_SYSTEM = (
    "You are a financial analyst. Extract structured data from filings and news.\n"
    "Respond ONLY with valid JSON. No markdown. No explanation. No preamble."
)

TAGGING_USER_TEMPLATE = """Analyse this article. Return JSON with exactly these keys:
  tickers_mentioned : array of ticker symbols, empty array if none
  sector            : one of [semiconductors, energy, biotech, industrials,
                      finance, consumer, macro, other]
  sentiment         : one of [bullish, bearish, neutral]
  catalyst_type     : one of [earnings, partnership, insider_buy, regulation,
                      product_launch, management_change, analyst_action, other]
  urgency           : integer 1-10 (10 = major market-moving event)
  one_line_thesis   : string, max 12 words, plain English

Article: {article_text}
"""


def _logger() -> logging.Logger:
    return get_logger("tradingbot.tagger")


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON object found in model response")
    return json.loads(m.group(0))


def _coerce_result(obj: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "tickers_mentioned",
        "sector",
        "sentiment",
        "catalyst_type",
        "urgency",
        "one_line_thesis",
    }
    missing = keys - set(obj.keys())
    extra = set(obj.keys()) - keys
    if missing:
        raise ValueError(f"Missing keys: {sorted(missing)}")
    if extra:
        # Be strict: easier to debug prompt drift.
        raise ValueError(f"Extra keys: {sorted(extra)}")

    tickers = obj["tickers_mentioned"]
    if not isinstance(tickers, list):
        tickers = []

    urgency = obj["urgency"]
    try:
        urgency_int = int(urgency)
    except Exception:
        urgency_int = 1
    urgency_int = max(1, min(10, urgency_int))

    thesis = str(obj["one_line_thesis"] or "").strip()
    if len(thesis.split()) > 12:
        thesis = " ".join(thesis.split()[:12])

    return {
        "tickers_mentioned": [str(t).upper().strip() for t in tickers if str(t).strip()],
        "sector": str(obj["sector"]).strip(),
        "sentiment": str(obj["sentiment"]).strip(),
        "catalyst_type": str(obj["catalyst_type"]).strip(),
        "urgency": urgency_int,
        "one_line_thesis": thesis,
    }


def _article_text(headline: str, body: str) -> str:
    # Keep payload bounded for the model.
    headline = (headline or "").strip()
    body = (body or "").strip()
    hints = _story_hints(body)
    text = (headline + "\n\n" + hints + body).strip()
    if len(text) > 8000:
        text = text[:8000] + "\n\n[TRUNCATED]"
    return text


def _story_hints(body: str) -> str:
    try:
        payload = json.loads(body)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""

    hints: list[str] = []
    ticker = payload.get("ticker")
    if isinstance(ticker, str) and ticker.strip():
        hints.append(f"ticker={ticker.strip().upper()}")
    form = payload.get("form")
    if isinstance(form, str) and form.strip():
        hints.append(f"form={form.strip()}")
    items = payload.get("items")
    if isinstance(items, list) and items:
        hints.append("items=" + ",".join(str(item) for item in items))
    if not hints:
        return ""
    return "[STRUCTURED_HINTS " + " ".join(hints) + "]\n\n"


def _fetch_untagged(limit: int = 50) -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, headline, body, source, fetched_at
            FROM raw_stories
            WHERE tagged = 0
            ORDER BY fetched_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _mark_tagged(raw_id: int, tagged_row: dict[str, Any]) -> None:
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO tagged_stories(
              raw_story_id, tickers, sector, sentiment, catalyst_type,
              urgency, one_line_thesis, tagged_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_id,
                json.dumps(tagged_row["tickers_mentioned"]),
                tagged_row["sector"],
                tagged_row["sentiment"],
                tagged_row["catalyst_type"],
                tagged_row["urgency"],
                tagged_row["one_line_thesis"],
                _now_utc_iso(),
            ),
        )
        conn.execute("UPDATE raw_stories SET tagged = 1 WHERE id = ?", (raw_id,))


def _gemini_tag(article_text: str) -> dict[str, Any]:
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "your-key":
        raise RuntimeError("GEMINI_API_KEY not set")

    prompt = TAGGING_USER_TEMPLATE.format(article_text=article_text)
    model = config.GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": config.GEMINI_API_KEY}
    payload = {
        "system_instruction": {"parts": [{"text": TAGGING_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }
    r = requests.post(url, params=params, json=payload, timeout=45)
    r.raise_for_status()
    data = r.json()
    candidates = data.get("candidates") or []
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or []) if candidates else []
    text = (parts[0].get("text") if parts and isinstance(parts[0], dict) else "") or ""
    text = text.strip()
    if not text:
        raise ValueError(f"Empty model response: {data}")
    return _coerce_result(_extract_json(text))


def run() -> None:
    log = _logger()
    log.info("tagger.run start")
    batch = _fetch_untagged(limit=50)
    if not batch:
        log.info("tagger.run no untagged stories")
        return

    ok = 0
    for row in batch:
        raw_id = int(row["id"])
        try:
            tagged = _gemini_tag(_article_text(row["headline"], row["body"]))
            try:
                payload = json.loads(row["body"] or "{}")
            except Exception:
                payload = {}
            hinted_ticker = payload.get("ticker") if isinstance(payload, dict) else None
            if not tagged["tickers_mentioned"] and isinstance(hinted_ticker, str) and hinted_ticker.strip():
                tagged["tickers_mentioned"] = [hinted_ticker.strip().upper()]
            _mark_tagged(raw_id, tagged)
            ok += 1
            log.info(
                "tagged raw_id=%s tickers=%s sentiment=%s catalyst=%s urgency=%s",
                raw_id,
                tagged["tickers_mentioned"],
                tagged["sentiment"],
                tagged["catalyst_type"],
                tagged["urgency"],
            )
        except Exception as e:
            log.warning("tagger failed raw_id=%s: %s", raw_id, e)
        time.sleep(4)

    log.info("tagger.run done tagged_ok=%s total=%s", ok, len(batch))


if __name__ == "__main__":
    run()
