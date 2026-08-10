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
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "")


# --- Capital / risk knobs ---
CAPITAL = float(os.getenv("CAPITAL", "1000.0"))
NARRATIVE_THRESHOLD = float(os.getenv("NARRATIVE_THRESHOLD", "25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_DAY_MOVE_PCT = float(os.getenv("MAX_DAY_MOVE_PCT", "0.30"))

# --- Universe filter ---
# The old micro-cap gate (MAX_MARKET_CAP / MAX_FLOAT_SHARES via Yahoo) is gone:
# Yahoo edge-blocks unauthenticated requests, Alpaca serves no market cap or
# float, and sub-$500M names have no tradeable options. The universe is now
# defined by liquidity and option availability instead.
REQUIRE_OPTIONABLE = _env_bool("REQUIRE_OPTIONABLE", "1")
MIN_AVG_DOLLAR_VOLUME = float(os.getenv("MIN_AVG_DOLLAR_VOLUME", "20000000"))
DOLLAR_VOLUME_DAYS = int(os.getenv("DOLLAR_VOLUME_DAYS", "20"))

# --- Options trading (long calls off bullish signals) ---
# Alpaca options level 2 or higher is required to BUY calls/puts.
# Level 1 only allows covered calls and cash-secured puts.
OPTIONS_MIN_TRADING_LEVEL = int(os.getenv("OPTIONS_MIN_TRADING_LEVEL", "2"))
# "indicative" is the free feed (delayed trades, derived quotes). "opra" needs a subscription.
OPTIONS_FEED = os.getenv("OPTIONS_FEED", "indicative")
STOCK_FEED = os.getenv("STOCK_FEED", "iex")

# Contract selection
OPTIONS_MIN_DTE = int(os.getenv("OPTIONS_MIN_DTE", "30"))
OPTIONS_MAX_DTE = int(os.getenv("OPTIONS_MAX_DTE", "45"))
OPTIONS_TARGET_DELTA = float(os.getenv("OPTIONS_TARGET_DELTA", "0.45"))
OPTIONS_MIN_DELTA = float(os.getenv("OPTIONS_MIN_DELTA", "0.35"))
OPTIONS_MAX_DELTA = float(os.getenv("OPTIONS_MAX_DELTA", "0.50"))
# Strike search window around spot, as a fraction of spot.
OPTIONS_STRIKE_WINDOW_PCT = float(os.getenv("OPTIONS_STRIKE_WINDOW_PCT", "0.25"))
# Liquidity guards.
OPTIONS_MIN_OPEN_INTEREST = int(os.getenv("OPTIONS_MIN_OPEN_INTEREST", "250"))
OPTIONS_MAX_SPREAD_PCT = float(os.getenv("OPTIONS_MAX_SPREAD_PCT", "0.12"))
OPTIONS_MIN_PREMIUM = float(os.getenv("OPTIONS_MIN_PREMIUM", "0.10"))

# Sizing / entry
OPTIONS_MAX_POSITION_PCT = float(os.getenv("OPTIONS_MAX_POSITION_PCT", str(MAX_POSITION_PCT)))
OPTIONS_MAX_OPEN_POSITIONS = int(os.getenv("OPTIONS_MAX_OPEN_POSITIONS", "5"))
OPTIONS_MAX_CONTRACTS = int(os.getenv("OPTIONS_MAX_CONTRACTS", "10"))
# Kelly sizing cannot express fractions of a contract. When the suggested size is
# smaller than one contract, still buy one if it fits inside the hard position cap.
OPTIONS_ALLOW_SINGLE_CONTRACT = _env_bool("OPTIONS_ALLOW_SINGLE_CONTRACT", "1")
OPTIONS_ORDER_TYPE = os.getenv("OPTIONS_ORDER_TYPE", "limit").strip().lower()
# Limit price padding above mid when buying (fraction of mid).
OPTIONS_ENTRY_SLIPPAGE_PCT = float(os.getenv("OPTIONS_ENTRY_SLIPPAGE_PCT", "0.03"))
OPTIONS_EXIT_SLIPPAGE_PCT = float(os.getenv("OPTIONS_EXIT_SLIPPAGE_PCT", "0.03"))

# Exit rules (no bracket orders for options, so a monitor job enforces these).
OPTIONS_PROFIT_TARGET_PCT = float(os.getenv("OPTIONS_PROFIT_TARGET_PCT", "1.00"))
OPTIONS_STOP_LOSS_PCT = float(os.getenv("OPTIONS_STOP_LOSS_PCT", "0.50"))
OPTIONS_EXIT_DTE = int(os.getenv("OPTIONS_EXIT_DTE", "10"))
# Cancel unfilled entry limit orders after this many minutes.
OPTIONS_ENTRY_TIMEOUT_MIN = float(os.getenv("OPTIONS_ENTRY_TIMEOUT_MIN", "20"))
# Off by default: entries stay manual until you trust the pipeline.
OPTIONS_AUTO_TRADE = _env_bool("OPTIONS_AUTO_TRADE", "0")

# --- Data sources ---
# Verified 2026-08: feeds.reuters.com no longer resolves (Reuters killed public
# RSS) and rsshub.app/apnews returns 403. The defaults below were checked live
# and parse. Override with RSS_FEEDS="key|url,key|url".
_DEFAULT_RSS_FEEDS = ",".join(
    [
        "nasdaq_markets|https://www.nasdaq.com/feed/rssoutbound?category=Markets",
        "marketwatch_top|https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "globenewswire|https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
        "prnewswire_fin|https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    ]
)


def _parse_feeds(raw: str) -> tuple:
    feeds = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "|" not in chunk:
            continue
        key, _, url = chunk.partition("|")
        key, url = key.strip(), url.strip()
        if key and url:
            feeds.append((key, url))
    return tuple(feeds)


RSS_FEEDS = _parse_feeds(os.getenv("RSS_FEEDS", _DEFAULT_RSS_FEEDS))

# Alpaca's Benzinga-backed news feed: ticker-tagged, so no ticker guessing.
ALPACA_NEWS_ENABLED = _env_bool("ALPACA_NEWS_ENABLED", "1")
ALPACA_NEWS_LOOKBACK_HOURS = float(os.getenv("ALPACA_NEWS_LOOKBACK_HOURS", "24"))
ALPACA_NEWS_LIMIT = int(os.getenv("ALPACA_NEWS_LIMIT", "50"))

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
# Verified 2026-08 against a free-tier key: gemini-2.0-flash and
# gemini-2.0-flash-lite return 429 RESOURCE_EXHAUSTED, and gemini-2.5-flash*
# return 404 "no longer available to new users" despite appearing in ListModels.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "800"))
GEMINI_MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))
# Only set this for thinking models (e.g. gemini-flash-latest), which otherwise
# spend the whole output budget on hidden reasoning and return empty candidates.
# gemini-flash-lite-latest rejects the field with 400 INVALID_ARGUMENT.
GEMINI_DISABLE_THINKING = _env_bool("GEMINI_DISABLE_THINKING", "0")

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
# How far back the per-ticker submissions pull looks for 8-Ks.
EDGAR_BACKFILL_DAYS = int(os.getenv("EDGAR_BACKFILL_DAYS", "3"))
# Fetch the filing document (preferring the Exhibit 99 press release) so the
# tagger sees real content instead of bare item codes.
EDGAR_FETCH_DOCUMENT_TEXT = _env_bool("EDGAR_FETCH_DOCUMENT_TEXT", "1")
EDGAR_DOC_MAX_CHARS = int(os.getenv("EDGAR_DOC_MAX_CHARS", "8000"))
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

# Require an explicit ticker notation from the watchlist, e.g. "(NASDAQ: AMD)".
# Set to 0 to fall back to the catalyst-keyword firehose.
RSS_REQUIRE_WATCHLIST_TICKER = _env_bool("RSS_REQUIRE_WATCHLIST_TICKER", "1")

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
