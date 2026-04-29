from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def _default_db_path() -> Path:
    here = Path(__file__).resolve().parent
    return here / "data" / "trading.db"


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_stories (
          id         INTEGER PRIMARY KEY,
          url_hash   TEXT UNIQUE,
          url        TEXT,
          headline   TEXT,
          body       TEXT,
          source     TEXT,
          fetched_at DATETIME,
          tagged     BOOLEAN DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tagged_stories (
          id              INTEGER PRIMARY KEY,
          raw_story_id    INTEGER REFERENCES raw_stories(id) ON DELETE CASCADE,
          tickers         TEXT,
          sector          TEXT,
          sentiment       TEXT,
          catalyst_type   TEXT,
          urgency         INTEGER,
          one_line_thesis TEXT,
          tagged_at       DATETIME
        );

        CREATE TABLE IF NOT EXISTS signals (
          id              INTEGER PRIMARY KEY,
          ticker          TEXT,
          first_mentioned DATETIME,
          narrative_score REAL,
          price_at_signal REAL,
          price_day1      REAL,
          price_day3      REAL,
          price_day7      REAL,
          suggested_size  REAL,
          created_at      DATETIME,
          outcome         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_raw_stories_tagged ON raw_stories(tagged);
        CREATE INDEX IF NOT EXISTS idx_tagged_stories_tagged_at ON tagged_stories(tagged_at);
        CREATE INDEX IF NOT EXISTS idx_signals_ticker_created ON signals(ticker, created_at);
        """
    )
    conn.commit()


@contextmanager
def db_session(db_path: str | os.PathLike | None = None):
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()

