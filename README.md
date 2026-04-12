# Alcapa Trading Dashboard

> **Streamlit Cloud dashboard for the Alpaca paper trading system**
> 
> Companion repo: [alpaca-paper-trader](https://github.com/tckun79-pixel/alpaca-paper-trader)

A **Streamlit Cloud-ready** dashboard for monitoring the Alpaca paper trading system. View account status, positions, performance metrics, strategy controls, and trade history from any browser or mobile device.

---

## Features

- **Dashboard Overview** — Equity, cash, buying power, and open positions at a glance
- **Positions** — Live open positions and full order history with filters
- **Performance** — Equity curve, win rate, P&L distribution, strategy breakdown
- **Strategies** — See active/inactive status of WheelOptions, StockSwing, and CreditSpread strategies
- **Trade Log** — Full JSONL trade audit log with filtering by date, action, and strategy
- **Controls** — Enable/disable strategies directly from the dashboard (writes to `config/strategy.yaml`)
- **Auto-refresh** — Optional automatic page refresh (configurable 30s–5min)
- **Market Hours** — Live US equity market open/closed indicator with next open/close times
- **Responsive** — Optimized for desktop and mobile browsers

---

## Screenshots

> Dashboard Overview shows equity, P&L, positions, and today's orders.

---

## Setup

### 1. Prerequisites

- Python 3.10+
- Alpaca paper trading account ([sign up free](https://app.alpaca.markets))
- Git

### 2. Clone the Repository

```bash
git clone https://github.com/tckun79-pixel/Alcapa-trading-dashboard.git
cd Alcapa-trading-dashboard
```

### 3. Local Development

**Using a virtual environment (recommended):**

```bash
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

**Configure environment variables (local only):**

Create a `.env` file in the project root:

```bash
# .env
APCA_API_KEY_ID=your_key_id_here
APCA_API_SECRET_KEY=your_secret_key_here
APCA_API_PAPER=true
CONFIG_PATH=config/strategy.yaml       # optional, default: config/strategy.yaml
TRADES_LOG=logs/trades.jsonl           # optional
STATUS_FILE=data/status.json           # optional
```

> For local testing without Alpaca keys, the dashboard will show a setup prompt but not crash.

**Run locally:**

```bash
streamlit run app.py
```

The dashboard opens at `http://localhost:8501`.

---

## Deployment to Streamlit Cloud

### Step 1 — Fork or Push to GitHub

```bash
cd Alcapa-trading-dashboard
git init
git add .
git commit -m "Initial Alcapa Dashboard"
git branch -M main
git remote add origin https://github.com/tckun79-pixel/Alcapa-trading-dashboard.git
git push -u origin main
```

### Step 2 — Add Secrets on Streamlit Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub
2. Click **New app** → select this repo
3. Set branch to `main`, file to `app.py`
4. Expand **Advanced settings**:
   - Add the following secrets under **Secrets**:

   ```
   APCA_API_KEY_ID = "PKXXXXXXXXXXXXXXXX"
   APCA_API_SECRET_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
   APCA_API_PAPER = "true"
   AUTO_REFRESH_INTERVAL = "60"
   ```
5. Click **Deploy**

Your app will be live at `https://[username]-alcapa-trading-dashboard.streamlit.app`.

> **Important:** Never commit `.streamlit/secrets.toml` or any file containing real API keys. The `secrets.toml` in this repo is a template only.

---

## Project Structure

```
Alcapa-trading-dashboard/          ← This repo
├── app.py                        # Streamlit Cloud dashboard
├── requirements.txt              # Python dependencies
├── .streamlit/
│   ├── config.toml              # Dark theme, layout
│   └── secrets.toml             # API keys + Supabase config (gitignored)
├── supabase_setup_trader.sql     # Run this once in Supabase SQL Editor
├── scheduler/
│   └── SCHEDULER.md             # cron/systemd guide for keeping trader alive
└── README.md

alpaca-paper-trader/              ← Separate repo (trading bot)
├── main.py                       # Runs strategies
├── sync_supabase.py              # Syncs data to Supabase after each run
├── config/strategy.yaml          # Strategy parameters
├── logs/trades.jsonl            # Trade audit log
└── data/status.json             # Last run status
```

---

## Architecture

```
alpaca-paper-trader (local/backend)
   ├── main.py runs strategies
   ├── sync_supabase.py writes to:
   │      ├── trader_trades
   │      ├── trader_status
   │      └── trader_config
   └── Supabase (cloud DB)
        ↕ HTTP REST
Alcapa-trading-dashboard (Streamlit Cloud)
   └── app.py reads Supabase tables
```

---

## Supabase Setup (Required for Dashboard Data)

The dashboard reads live trader data from Supabase. This enables the Streamlit Cloud dashboard to show trader data even when the trader runs on a separate backend (e.g., home server, VPS, WSL2).

### Step 1 — Create a Supabase Project

1. Sign up at https://supabase.com (free tier is sufficient)
2. Note your **Project URL** from Settings → API (e.g. `https://xyz.supabase.co`)
3. Copy the **service_role secret** from Settings → API → `service_role secret`

### Step 2 — Run the SQL Schema

Open the Supabase SQL Editor for your project:

```
https://app.supabase.com/project/YOUR_PROJECT/_/sql
```

Paste and run the contents of:

```
supabase_setup_trader.sql   ← in this repo
```

This creates three tables with RLS policies:

| Table | Purpose |
|---|---|
| `trader_trades` | Trade event log (mirrors `logs/trades.jsonl`) |
| `trader_status` | Trader run status (mirrors `data/status.json`) |
| `trader_config` | Strategy YAML snapshot (mirrors `config/strategy.yaml`) |

### Step 3 — Configure Trader Environment

In the trader's `.env` (or shell environment):

```bash
# Trader-side: where to push data
SUPABASE_URL=https://YOUR_PROJECT_ID.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_secret_here
SUPABASE_OWNER=admin                      # must match dashboard setting
```

Then install the Supabase Python client in the trader venv:

```bash
cd alpaca-paper-trader
source venv/bin/activate
pip install supabase-py>=2.0
```

The trader calls `sync_supabase.py`'s `sync_all()` automatically at the end of each run — no manual steps needed.

### Step 4 — Add Supabase Secrets to Streamlit Cloud

In your Streamlit Cloud app settings (Advanced → Secrets):

```toml
SUPABASE_URL = "https://YOUR_PROJECT_ID.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "your_service_role_secret_here"
SUPABASE_OWNER = "admin"
```

### How the Sync Works

```
Trader runs (any backend)
  └─ writes logs/trades.jsonl, data/status.json, config/strategy.yaml
  └─ sync_supabase.py → Supabase (HTTP POST)

Dashboard reads (Streamlit Cloud)
  └─ app.py → Supabase (HTTP GET)
  └─ Falls back to local files if Supabase unavailable (local dev mode)
```

### Data Freshness

- Trades: new lines synced via cursor tracking (only new entries sent per run)
- Status: upserted per run (last run timestamp)
- Config: upserted per run (full config JSON)

### Local Development

The dashboard falls back to reading local files if Supabase credentials are not set. Run the dashboard locally while the trader runs on a server — just point `CONFIG_PATH`, `TRADES_LOG`, and `STATUS_FILE` environment variables at the local trader directory.

### Supabase Row-Level Security (RLS) Notes

All three tables use `owner TEXT DEFAULT 'admin'` with RLS policies scoped to `owner = current_user`. Since you're using the **service_role key** (bypasses RLS), all rows are visible regardless of owner tag. To restrict to the `SUPABASE_OWNER` value, either:
- Use the **anon key** instead (requires `anon` key and `auth.uid()` mapping — more setup)
- Or keep service role key (simpler for single-user)

For multi-user sharing: use anon key + per-user owner tags + shared dashboard with a user picker.

---

## Strategy Reference

| Strategy | Description | Config Key |
|---|---|---|
| **WheelOptions** | CSP entry → CC income → assignment handling | `wheel` |
| **StockSwing** | MA crossover, RSI filter, ATR stops | `stock_swing` |
| **CreditSpread** | Bull put / Bear call spreads | `credit_spread` |

---

## Security Notes

- **Paper trading only** — `APCA_API_PAPER=true` is enforced. The dashboard does not support live trading.
- **API keys as secrets** — Never hardcode or commit real API keys. Use Streamlit Cloud secrets or environment variables.
- **Config writes** — The strategy toggle in the Settings page writes directly to `config/strategy.yaml`. Use with care in production.
- **Discord confirmation** — The existing trader (`main.py`) uses Discord-based interactive trade confirmation. The dashboard is view-only for trades.

---

## Scheduler (Keeping the Trader Running)

The dashboard is **read-only** for trades. The actual trader runs separately on a backend server.

See [`scheduler/SCHEDULER.md`](scheduler/SCHEDULER.md) for a lightweight guide covering:

- `cron` for periodic runs
- `systemd` for persistent daemon-like operation
- `OpenClaw` agent scheduling for event-driven runs

---

## Suggested Improvements

### High Priority
- [ ] **Order notifications** — Push alerts to Telegram/Discord when orders fill
- [ ] **Multi-account** — Support IBKR, Longbridge, or Tiger broker views in the same dashboard
- [ ] **Watchlist** — Track HOOD, IREN, INTC, CELH, FCX with alerts on price targets

### Medium Priority
- [ ] **Trade replay** — Replay past trades from `trades.jsonl` with current prices
- [ ] **Assignment tracker** — Show cost basis and assignment price for wheel strategy shares
- [ ] **Option chain viewer** — Display nearby strikes/DTE for current underlyings
- [ ] **Backtest comparison** — Overlay backtest equity curve vs live equity curve
- [ ] **Mobile layout tuning** — Further refine mobile nav and table widths

### Nice to Have
- [ ] **PDF reports** — Export weekly performance summary as PDF
- [ ] **Paper → Live mode** — One-click switch to live trading API keys (with heavy warnings)
- [ ] **Trade journal** — Add notes/tags to individual trades
- [ ] **Greek exposure** — Aggregate delta/gamma/theta/vega across option positions
- [ ] **Custom indicators** — Plug in user-defined TA indicators

---

## License

MIT. Use freely, but paper/live trading involves financial risk — trade responsibly.
