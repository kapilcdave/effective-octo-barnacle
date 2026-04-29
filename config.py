from __future__ import annotations

import os

# --- Secrets (prefer environment variables) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "your-key")

ALPACA_KEY = os.getenv("ALPACA_KEY", "your-key")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "your-key")

# --- Capital / risk knobs ---
CAPITAL = float(os.getenv("CAPITAL", "1000.0"))
NARRATIVE_THRESHOLD = float(os.getenv("NARRATIVE_THRESHOLD", "25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))

# --- Data sources (two only) ---
REUTERS_RSS = "https://feeds.reuters.com/reuters/businessNews"
AP_RSS = "https://rsshub.app/apnews/topics/business"

# SEC requires a descriptive User-Agent with contact info.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "kapilda-narrative-bot/0.1 (research; contact: you@example.com)",
)

# Gemini
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

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

