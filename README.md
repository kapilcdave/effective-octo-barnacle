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
```

Optional knobs:

```bash
export CAPITAL="1000"
export NARRATIVE_THRESHOLD="25"
export MAX_POSITION_PCT="0.10"
export KELLY_FRACTION="0.25"
export GEMINI_MODEL="gemini-2.0-flash"
```

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
WorkingDirectory=/home/k/ka/kapilda/trading-bot
ExecStart=/home/k/ka/kapilda/trading-bot/.venv/bin/python /home/k/ka/kapilda/trading-bot/main.py
Restart=always
RestartSec=10
Environment=GEMINI_API_KEY=your-key
Environment=SEC_USER_AGENT=yourbot/0.1 (contact: you@domain.com)

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl enable tradingbot
sudo systemctl start tradingbot
journalctl -u tradingbot -f
```

