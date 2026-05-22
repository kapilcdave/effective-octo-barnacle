from __future__ import annotations

import os


def _env_first(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


# --- Secrets (prefer environment variables) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "your-key")

ALPACA_KEY = _env_first("ALPACA_KEY", "APCA_API_KEY_ID", "APCA-API-KEY-ID", default="your-key")
ALPACA_SECRET = _env_first(
    "ALPACA_SECRET",
    "APCA_API_SECRET_KEY",
    "APCA-API-SECRET-KEY",
    default="your-key",
)
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# --- Capital / risk knobs ---
CAPITAL = float(os.getenv("CAPITAL", "1000.0"))
NARRATIVE_THRESHOLD = float(os.getenv("NARRATIVE_THRESHOLD", "25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_DAY_MOVE_PCT = float(os.getenv("MAX_DAY_MOVE_PCT", "0.30"))
MAX_MARKET_CAP = int(float(os.getenv("MAX_MARKET_CAP", "500000000")))
MAX_FLOAT_SHARES = int(float(os.getenv("MAX_FLOAT_SHARES", "50000000")))

# --- Data sources (two only) ---
REUTERS_RSS = "https://feeds.reuters.com/reuters/businessNews"
AP_RSS = "https://rsshub.app/apnews/topics/business"
EDGAR_CURRENT_8K_ATOM = os.getenv(
    "EDGAR_CURRENT_8K_ATOM",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=100&output=atom",
)

# SEC requires a descriptive User-Agent with contact info.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "kapilda-narrative-bot/0.1 (research; contact: you@example.com)",
)

# Gemini
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

HIGH_SIGNAL_8K_ITEMS = {
    "1.01",  # Entry into a material definitive agreement.
    "1.03",  # Bankruptcy or receivership.
    "2.02",  # Results of operations and financial condition.
    "8.01",  # Other events.
}

ITEM_WEIGHTS = {
    "1.01": 30,
    "2.02": 25,
    "8.01": 20,
    "1.03": 15,
    "5.02": 5,
    "7.01": 2,
}

# --- Scraper knobs ---
RSS_MAX_ENTRIES = int(os.getenv("RSS_MAX_ENTRIES", "20"))
RSS_MAX_AGE_HOURS = float(os.getenv("RSS_MAX_AGE_HOURS", "48"))
EDGAR_ATOM_MAX_ENTRIES = int(os.getenv("EDGAR_ATOM_MAX_ENTRIES", "100"))
EDGAR_BACKFILL_ENABLED = os.getenv("EDGAR_BACKFILL_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
SEC_REQUEST_DELAY_SEC = float(os.getenv("SEC_REQUEST_DELAY_SEC", "0.12"))

RSS_CATALYST_KEYWORDS = tuple(
    kw.strip().lower()
    for kw in os.getenv(
        "RSS_CATALYST_KEYWORDS",
        "earnings,guidance,fda,partnership,bankruptcy,acquisition,merger,"
        "outlook,revenue,profit,ceo,cfo,dividend,buyback,lawsuit,regulation",
    ).split(",")
    if kw.strip()
)

# --- Watchlist (40-50 mid-cap tickers across sectors) ---
WATCHLIST = [
    # Semiconductors
    "INTC",
    "AMD",
    "MRVL",
    "QCOM",
    "AMAT",
    "KLAC",
    "LRCX",
    "ON",
    "TXN",
    # Energy
    "DVN",
    "MRO",
    "OXY",
    "SLB",
    "HAL",
    "FANG",
    "AR",
    # Biotech
    "MRNA",
    "BNTX",
    "IONS",
    "ALNY",
    "RARE",
    "ACAD",
    # Industrials
    "GE",
    "HON",
    "ETN",
    "PWR",
    "HUBB",
    "EMR",
]

WATCHLIST_SET = frozenset(WATCHLIST)
