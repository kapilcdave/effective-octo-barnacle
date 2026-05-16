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
