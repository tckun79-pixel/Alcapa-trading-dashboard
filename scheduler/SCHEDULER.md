# Scheduler Guide

The dashboard is read-only. The actual trading strategies run on a backend server (VPS, home server, or WSL2) via one of the methods below.

> **Goal:** Keep the trader running automatically every few hours without manual intervention.

---

## Option 1 — `cron` (Simplest)

### Prerequisites

The trader must be callable as a script. From the `alpaca-paper-trader/` directory:

```bash
# Activate venv and run
cd /path/to/alpaca-paper-trader
source venv/bin/activate
python main.py
```

### Setup

```bash
crontab -e
```

Add:

```cron
# Run trader every weekday at 9:45 AM Singapore time (1:45 AM ET)
45 9 * * 1-5 cd /home/ck_kun/alpaca-paper-trader && /home/ck_kun/alpaca-paper-trader/venv/bin/python main.py >> /home/ck_kun/alpaca-paper-trader/logs/cron.log 2>&1

# Run again at 4:00 PM SGT (market close + 30min) to catch late signals
0 16 * * 1-5 cd /home/ck_kun/alpaca-paper-trader && /home/ck_kun/alpaca-paper-trader/venv/bin/python main.py >> /home/ck_kun/alpaca-paper-trader/logs/cron.log 2>&1
```

> Note: Singapore time = ET + 12h (standard) or ET + 13h (DST). Adjust cron times accordingly.

### Verify

```bash
tail -f /home/ck_kun/alpaca-paper-trader/logs/cron.log
```

---

## Option 2 — `systemd` (Persistent Daemon)

### Service File

Create `/etc/systemd/system/alpaca-trader.service`:

```ini
[Unit]
Description=Alpaca Paper Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ck_kun
WorkingDirectory=/home/ck_kun/alpaca-paper-trader
Environment="PATH=/home/ck_kun/alpaca-paper-trader/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
Environment="APCA_API_KEY_ID=your_key_here"
Environment="APCA_API_SECRET_KEY=your_secret_here"
ExecStart=/home/ck_kun/alpaca-paper-trader/venv/bin/python main.py
Restart=on-failure
RestartSec=60
StandardOutput=append:/home/ck_kun/alpaca-paper-trader/logs/systemd.log
StandardError=append:/home/ck_kun/alpaca-paper-trader/logs/systemd.log

# Run every 6 hours via systemd timer (see Option 3) OR continuously loop
# For interval-based: uncomment ExecStartPost /bin/sleep
# For event-driven (preferred): rely on cron + this as failsafe restart

[Install]
WantedBy=multi-user.target
```

### Timer for Interval Runs

Create `/etc/systemd/system/alpaca-trader.timer`:

```ini
[Unit]
Description=Run Alpaca Trader every 6 hours

[Timer]
OnCalendar=09:00
OnCalendar=15:00
OnCalendar=21:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable alpaca-trader.timer
sudo systemctl start alpaca-trader.timer
```

### Check Status

```bash
systemctl status alpaca-trader
journalctl -u alpaca-trader -f
```

---

## Option 3 — `while True` Loop (Self-Healing)

Add to the end of `main.py`:

```python
import time, schedule

def run_loop():
    """Run trader on schedule while keeping process alive for log monitoring."""
    import schedule

    def job():
        from main import run_trader  # refactor: move run_trader to a separate function
        run_trader()

    schedule.every().day.at("09:45").do(job)
    schedule.every().day.at("16:00").do(job)

    while True:
        schedule.run_pending()
        time.sleep(60)
```

> Keep the `while True` loop in a separate launcher script so `main.py` can still be called by cron without daemonizing.

---

## Option 4 — OpenClaw Agent Scheduling

CK uses OpenClaw as the AI agent orchestrator. The OpenClaw `lobster` skill supports cron-style scheduling and event-driven workflows.

```bash
# Within OpenClaw, schedule a trader run via cron expression
/openclaw schedule add --name "Alpaca Daily Run" \
  --cron "0 9 * * 1-5" \
  --command "cd /home/ck_kun/alpaca-paper-trader && ./venv/bin/python main.py" \
  --notify-channel telegram
```

Check OpenClaw docs for the latest scheduling syntax.

---

## Health Checks & Alerts

### Simple Cron Health Check

```bash
# Check if trader ran successfully in the last 24h
*/15 * * * * find /home/ck_kun/alpaca-paper-trader/logs/ -name "trader.log" -mtime -1 | grep -q . && echo "OK" || \
  echo "ALERT: Trader log not updated" | mail -s "Alpaca Trader Alert" your@email.com
```

### Log Rotation

The trader already uses `TimedRotatingFileHandler` (midnight, 7-day backup). Ensure `logrotate` also handles `logs/trades.jsonl`:

```bash
# /etc/logrotate.d/alpaca-trader
/home/ck_kun/alpaca-paper-trader/logs/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 0644 ck_kun ck_kun
}

/home/ck_kun/alpaca-paper-trader/logs/trades.jsonl {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0644 ck_kun ck_kun
}
```

---

## Recommended Schedule (US Options)

| Time (SGT) | Time (ET) | Action |
|---|---|---|
| 09:30 | 21:30 (prev day) | Pre-market check |
| 09:45 | 21:45 | Morning run — place/update CSPs |
| 16:00 | 04:00 | Mid-session review |
| 21:30 | 09:30 | Post-close run — CC management, roll decisions |
| 23:00 | 11:00 | Final check — close any stop-loss triggered positions |

---

## File Permission Notes

- `logs/trades.jsonl` must be writable by whichever user the cron/systemd service runs as
- `data/status.json` and `data/orders.json` also need write access
- If using `sudo systemctl`, set `User=ck_kun` (not root) to avoid file ownership issues
