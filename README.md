## Narrative momentum trading bot (EDGAR + RSS only)

Runs headless, writes to SQLite, tags with Gemini, scores narrative momentum, and generates a weekly HTML digest.

### Setup

```bash
cd trading-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Set environment variables (recommended):

```bash
export GEMINI_API_KEY="..."
export SEC_USER_AGENT="yourbot/0.1 (contact: you@domain.com)"
export ALPACA_KEY="your-paper-key-id"
export ALPACA_SECRET="your-paper-secret-key"
export ALPACA_BASE_URL="https://paper-api.alpaca.markets"
```

Optional knobs:

```bash
export CAPITAL="1000"
export NARRATIVE_THRESHOLD="25"
export MAX_POSITION_PCT="0.10"
export KELLY_FRACTION="0.25"
export GEMINI_MODEL="gemini-2.0-flash"
```

### Alpaca connection

The unauthenticated Alpaca curl in their docs returns `403 Forbidden` by design. Private Trading API calls need both auth headers:

```bash
curl -X GET "https://paper-api.alpaca.markets/v2/account" \
  -H "APCA-API-KEY-ID: $ALPACA_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET"
```

This repo supports `ALPACA_KEY` / `ALPACA_SECRET`, plus Alpaca-style `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`.

Smoke-test the account connection:

```bash
.venv/bin/python executor.py account
```

Preview the order payload for a pending signal without submitting it:

```bash
.venv/bin/python executor.py dry-run <signal_id>
```

Submit the pending signal to Alpaca paper trading:

```bash
.venv/bin/python executor.py submit <signal_id>
```

Keep `ALPACA_BASE_URL=https://paper-api.alpaca.markets` until the strategy has been validated. Switching to `https://api.alpaca.markets` uses live trading credentials and can place real orders.

### Options trading (long calls)

`options_executor.py` trades the same signals as long calls instead of shares. The equity path in `executor.py` is untouched; both share one Alpaca client (`alpaca.py`).

**Requirements:** buying calls needs Alpaca **options trading level 2 or higher** (level 1 is covered calls and cash-secured puts only). Paper accounts have options enabled by default. Check yours:

```bash
.venv/bin/python options_executor.py account
```

**How a contract is chosen** (`options.py`):

1. `GET /v2/options/contracts` for active, tradable calls expiring in `OPTIONS_MIN_DTE`-`OPTIONS_MAX_DTE` days with strikes within `OPTIONS_STRIKE_WINDOW_PCT` of spot.
2. Drop anything under `OPTIONS_MIN_OPEN_INTEREST`.
3. `GET /v1beta1/options/snapshots` for quotes and greeks; drop zero/crossed quotes, premium under `OPTIONS_MIN_PREMIUM`, and spreads wider than `OPTIONS_MAX_SPREAD_PCT` of mid.
4. Keep deltas inside `[OPTIONS_MIN_DELTA, OPTIONS_MAX_DELTA]`, take the nearest qualifying expiry, and pick the strike closest to `OPTIONS_TARGET_DELTA`. If the feed returns no greeks, it falls back to the strike nearest `1.03 * spot`.

**Sizing:** `min(signal.suggested_size, equity * OPTIONS_MAX_POSITION_PCT, options_buying_power)` divided by premium × 100. Kelly sizing often lands below one contract; `OPTIONS_ALLOW_SINGLE_CONTRACT=1` then buys exactly one if it still fits under the hard cap.

Preview selection and sizing without touching the DB or placing an order:

```bash
.venv/bin/python options_executor.py pick AMD          # contract selection only
.venv/bin/python options_executor.py dry-run <signal_id>
```

Submit an entry (limit order at mid + `OPTIONS_ENTRY_SLIPPAGE_PCT`, capped at the ask, TIF `day`):

```bash
.venv/bin/python options_executor.py submit <signal_id>
```

**Exits.** Alpaca does not support bracket/OCO orders on options, so exits are enforced by a polling job rather than resting orders:

```bash
.venv/bin/python options_executor.py sync      # reconcile fills, timeouts, expiries
.venv/bin/python options_executor.py monitor   # apply exit rules, sell to close
```

`monitor` closes a position when any rule fires: mark ≥ `OPTIONS_PROFIT_TARGET_PCT` gain, mark ≤ `OPTIONS_STOP_LOSS_PCT` loss, or DTE ≤ `OPTIONS_EXIT_DTE`. `sync` records fills, cancels entry limits left unfilled after `OPTIONS_ENTRY_TIMEOUT_MIN`, and marks trades closed when a position disappears through expiry, exercise, or assignment. Both run automatically from `main.py` (every 5 and 15 minutes).

Inspect state:

```bash
.venv/bin/python options_executor.py list        # option_trades rows
.venv/bin/python options_executor.py positions   # live Alpaca option positions
.venv/bin/python options_executor.py close <trade_id> [reason]
```

Entries stay manual unless you opt in:

```bash
export OPTIONS_AUTO_TRADE="1"   # main.py then submits entries every 30 min
```

Options knobs and their defaults:

```bash
export OPTIONS_FEED="indicative"        # free feed; "opra" needs a data subscription
export OPTIONS_MIN_DTE="30"
export OPTIONS_MAX_DTE="45"
export OPTIONS_TARGET_DELTA="0.45"
export OPTIONS_MIN_DELTA="0.35"
export OPTIONS_MAX_DELTA="0.50"
export OPTIONS_MIN_OPEN_INTEREST="250"
export OPTIONS_MAX_SPREAD_PCT="0.12"
export OPTIONS_MAX_POSITION_PCT="0.10"
export OPTIONS_MAX_OPEN_POSITIONS="5"
export OPTIONS_MAX_CONTRACTS="10"
export OPTIONS_ORDER_TYPE="limit"       # or "market"
export OPTIONS_PROFIT_TARGET_PCT="1.00" # +100% on premium
export OPTIONS_STOP_LOSS_PCT="0.50"     # -50% on premium
export OPTIONS_EXIT_DTE="10"
export OPTIONS_ENTRY_TIMEOUT_MIN="20"
export OPTIONS_AUTO_TRADE="0"
```

Trades are tracked in the `option_trades` table (`submitting → pending_entry → open → closing → closed`), keyed to the originating `signals.id`.

### Run stages manually (recommended first run)

Confirm EDGAR → SQLite is flowing:

```bash
.venv/bin/python scraper.py
```

Tag (requires `GEMINI_API_KEY`):

```bash
.venv/bin/python tagger.py
```

Score:

```bash
.venv/bin/python scorer.py
```

Generate a report:

```bash
.venv/bin/python reporter.py
```

Reports are saved in `reports/` and logs go to `logs/bot.log`.

### Run continuously (scheduler)

```bash
.venv/bin/python main.py
```

### Systemd service (example)

Create `/etc/systemd/system/tradingbot.service`:

```ini
[Unit]
Description=Trading Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/kapil/effective-octo-barnacle
ExecStart=/home/kapil/effective-octo-barnacle/.venv/bin/python /home/kapil/effective-octo-barnacle/main.py
Restart=always
RestartSec=10
Environment=GEMINI_API_KEY=your-key
Environment=SEC_USER_AGENT=yourbot/0.1 (contact: you@domain.com)
Environment=ALPACA_KEY=your-paper-key-id
Environment=ALPACA_SECRET=your-paper-secret-key
Environment=ALPACA_BASE_URL=https://paper-api.alpaca.markets

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl enable tradingbot
sudo systemctl start tradingbot
journalctl -u tradingbot -f
```
