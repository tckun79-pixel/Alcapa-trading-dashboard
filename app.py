"""
Alcapa Trading Dashboard — Streamlit Application
Local-debug friendly version for WSL2 / Streamlit.

Integrated additions:
- Scheduler DB page backed by SQLite
- Wheel signal viewer
- Swing signal viewer
- Scheduler orders / fills / positions viewer
- Small scheduler P&L chart
- Earnings-blocked panel
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

st.set_page_config(
    page_title="Alcapa Dashboard",
    page_icon="📊",
    layout="wide",
    menu_items={"About": "Alpaca Paper Trading Dashboard — local debug build"},
)

logger = logging.getLogger("dashboard")

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_SECRETS = PROJECT_ROOT / ".streamlit" / "secrets.toml"
GLOBAL_SECRETS = Path.home() / ".streamlit" / "secrets.toml"
DB_PATH = PROJECT_ROOT / "data" / "alcapa.db"


def has_any_secrets_file() -> bool:
    return PROJECT_SECRETS.exists() or GLOBAL_SECRETS.exists()


def get_secret(key: str, default=None):
    env_val = os.getenv(key)
    if env_val not in (None, ""):
        return env_val

    if has_any_secrets_file():
        try:
            return st.secrets[key]
        except Exception:
            pass

    return default


API_KEY = get_secret("APCA_API_KEY_ID")
API_SECRET = get_secret("APCA_API_SECRET_KEY")
PAPER_MODE = str(get_secret("APCA_API_PAPER", "true")).lower() == "true"

CONFIG_PATH = get_secret("CONFIG_PATH", "config/strategy.yaml")
TRADES_LOG = get_secret("TRADES_LOG", "logs/trades.jsonl")
STATUS_FILE = get_secret("STATUS_FILE", "data/status.json")
SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = get_secret("SUPABASE_SERVICE_ROLE_KEY") or get_secret("SUPABASE_ANON_KEY")


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def file_exists(path: str) -> bool:
    try:
        return Path(path).exists()
    except Exception:
        return False


def to_sgt(series: pd.Series) -> pd.Series:
    ts = pd.to_datetime(series, errors="coerce", utc=True)
    return ts.dt.tz_convert("Asia/Singapore")


def format_currency(value):
    if value is None or value == "":
        return "—"
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return str(value)


def get_last_refreshed():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S SGT")


def has_cols(df: pd.DataFrame, cols) -> bool:
    return all(c in df.columns for c in cols)


def _supabase_headers():
    if not SUPABASE_SERVICE_ROLE_KEY:
        return {}
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def query_supabase(table: str, params: dict = None) -> Optional[list]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    try:
        response = requests.get(
            url,
            headers=_supabase_headers(),
            params=params or {},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.warning(f"Supabase query error for {table}: {e}")
        return None


@st.cache_resource(ttl=300)
def get_trading_client():
    if not API_KEY or not API_SECRET:
        return None
    try:
        client = TradingClient(
            api_key=API_KEY,
            secret_key=API_SECRET,
            paper=PAPER_MODE,
        )
        client.get_account()
        return client
    except Exception as e:
        logger.warning(f"Failed to connect to Alpaca: {e}")
        return None


@st.cache_data(ttl=10)
def run_db_query(query: str) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(query, conn)
    except Exception as e:
        logger.warning(f"SQLite query error: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()

    return df


def db_exists() -> bool:
    return DB_PATH.exists()


def load_db_summary():
    query = """
    select 'signals' as table_name, count(*) as row_count from signals
    union all
    select 'orders' as table_name, count(*) as row_count from orders
    union all
    select 'fills' as table_name, count(*) as row_count from fills
    union all
    select 'positions' as table_name, count(*) as row_count from positions
    union all
    select 'options_positions' as table_name, count(*) as row_count from options_positions
    """
    return run_db_query(query)


def load_db_wheel_signals(limit: int = 20):
    query = f"""
    select
      id,
      ts,
      symbol,
      signal_type,
      round(price, 2) as premium,
      qty,
      acted,
      json_extract(meta, '$.contract_symbol') as contract_symbol,
      json_extract(meta, '$.strike') as strike,
      json_extract(meta, '$.expiry') as expiry,
      json_extract(meta, '$.dte') as dte,
      json_extract(meta, '$.delta') as delta,
      json_extract(meta, '$.sigma') as sigma,
      json_extract(meta, '$.bid_is_real') as bid_is_real,
      json_extract(meta, '$.mode') as mode
    from signals
    where strategy = 'options_strategy'
    order by id desc
    limit {int(limit)}
    """
    df = run_db_query(query)
    if not df.empty and "ts" in df.columns:
        df["ts"] = to_sgt(df["ts"])
    return df


def load_db_swing_signals(limit: int = 20):
    query = f"""
    select
      id,
      ts,
      symbol,
      signal_type,
      round(price, 2) as price,
      round(atr, 2) as atr,
      round(stop_loss, 2) as stop_loss,
      qty,
      acted,
      json_extract(meta, '$.sma50') as sma50,
      json_extract(meta, '$.sma200') as sma200,
      json_extract(meta, '$.ema20') as ema20,
      json_extract(meta, '$.high20') as high20
    from signals
    where strategy = 'swing_strategy'
    order by id desc
    limit {int(limit)}
    """
    df = run_db_query(query)
    if not df.empty and "ts" in df.columns:
        df["ts"] = to_sgt(df["ts"])
    return df


def load_db_orders(limit: int = 50):
    query = f"""
    select
      id,
      ts,
      strategy,
      symbol,
      alpaca_order_id,
      side,
      qty,
      order_type,
      limit_price,
      status
    from orders
    order by id desc
    limit {int(limit)}
    """
    df = run_db_query(query)
    if not df.empty and "ts" in df.columns:
        df["ts"] = to_sgt(df["ts"])
    return df


def load_db_fills(limit: int = 50):
    query = f"""
    select
      id,
      ts,
      strategy,
      symbol,
      alpaca_order_id,
      filled_qty,
      filled_price,
      side
    from fills
    order by id desc
    limit {int(limit)}
    """
    df = run_db_query(query)
    if not df.empty and "ts" in df.columns:
        df["ts"] = to_sgt(df["ts"])
    return df


def load_db_stock_positions():
    query = """
    select
      id,
      ts_open,
      ts_close,
      strategy,
      symbol,
      qty,
      entry_price,
      exit_price,
      stop_loss,
      pnl,
      status
    from positions
    order by id desc
    """
    df = run_db_query(query)
    if not df.empty:
        if "ts_open" in df.columns:
            df["ts_open"] = to_sgt(df["ts_open"])
        if "ts_close" in df.columns:
            df["ts_close"] = to_sgt(df["ts_close"])
    return df


def load_db_option_positions():
    query = """
    select
      id,
      ts_open,
      ts_close,
      symbol,
      underlying,
      contract_type,
      strike,
      expiry,
      qty,
      premium,
      close_premium,
      pnl,
      status,
      assignment
    from options_positions
    order by id desc
    """
    df = run_db_query(query)
    if not df.empty:
        if "ts_open" in df.columns:
            df["ts_open"] = to_sgt(df["ts_open"])
        if "ts_close" in df.columns:
            df["ts_close"] = to_sgt(df["ts_close"])
    return df


def load_scheduler_pnl_summary():
    query = """
    select
      symbol,
      strategy,
      qty,
      entry_price,
      exit_price,
      pnl,
      status
    from positions
    where pnl is not null

    union all

    select
      underlying as symbol,
      'options_strategy' as strategy,
      qty,
      premium as entry_price,
      close_premium as exit_price,
      pnl,
      status
    from options_positions
    where pnl is not null
    """
    df = run_db_query(query)
    if not df.empty:
        if "pnl" in df.columns:
            df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
        if "qty" in df.columns:
            df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    return df


def load_earnings_blocked_signals(limit: int = 50):
    query = f"""
    select
      id,
      ts,
      strategy,
      symbol,
      signal_type,
      json_extract(meta, '$.reason') as reason,
      json_extract(meta, '$.guard_days') as guard_days,
      json_extract(meta, '$.side') as side
    from signals
    where signal_type = 'earnings_blocked'
    order by id desc
    limit {int(limit)}
    """
    df = run_db_query(query)
    if not df.empty and "ts" in df.columns:
        df["ts"] = to_sgt(df["ts"])
    return df


def load_recent_options_candidates(limit: int = 50):
    query = f"""
    select
      id,
      ts,
      symbol,
      signal_type,
      round(price, 2) as premium,
      json_extract(meta, '$.contract_symbol') as contract_symbol,
      json_extract(meta, '$.strike') as strike,
      json_extract(meta, '$.expiry') as expiry,
      json_extract(meta, '$.dte') as dte,
      json_extract(meta, '$.delta') as delta,
      json_extract(meta, '$.mode') as mode,
      acted
    from signals
    where strategy = 'options_strategy'
    order by id desc
    limit {int(limit)}
    """
    df = run_db_query(query)
    if not df.empty and "ts" in df.columns:
        df["ts"] = to_sgt(df["ts"])
    return df


def metric_value(df: pd.DataFrame, table_name: str) -> int:
    if df.empty:
        return 0
    row = df[df["table_name"] == table_name]
    if row.empty:
        return 0
    return int(row["row_count"].iloc[0])


def fetch_account(client: TradingClient) -> Optional[Dict]:
    if not client:
        return None
    try:
        account = client.get_account()
        return {
            "cash": getattr(account, "cash", None),
            "buying_power": getattr(account, "buying_power", None),
            "equity": getattr(account, "equity", None),
            "initial_equity": getattr(account, "initial_equity", None) or getattr(account, "last_equity", None),
            "last_equity": getattr(account, "last_equity", None),
            "currency": getattr(account, "currency", None),
            "status": getattr(account, "status", None),
            "id": getattr(account, "id", None),
            "options_buying_power": getattr(account, "options_buying_power", None),
            "options_approved_level": getattr(account, "options_approved_level", None),
            "options_trading_level": getattr(account, "options_trading_level", None),
        }
    except Exception as e:
        logger.warning(f"get_account error: {e}")
        return None


def fetch_positions(client: TradingClient) -> pd.DataFrame:
    if not client:
        return pd.DataFrame()

    try:
        positions = client.get_all_positions()
        rows = []
        for p in positions or []:
            rows.append(
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_price": p.avg_entry_price,
                    "current_price": p.current_price,
                    "market_value": p.market_value,
                    "unrealized_pl": p.unrealized_pl,
                    "unrealized_pl_pct": p.unrealized_plpc,
                    "side": p.side,
                    "qty_available": p.qty_available,
                }
            )
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"get_all_positions error: {e}")
        return pd.DataFrame()


def fetch_orders(client: TradingClient, status="all", limit=100) -> pd.DataFrame:
    if not client:
        return pd.DataFrame()

    try:
        status_map = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }

        request = GetOrdersRequest(
            status=status_map.get(status, QueryOrderStatus.ALL),
            limit=limit,
        )

        orders = client.get_orders(filter=request)

        rows = []
        for o in orders or []:
            rows.append(
                {
                    "id": str(getattr(o, "id", "")),
                    "symbol": getattr(o, "symbol", None),
                    "side": str(getattr(o, "side", "")),
                    "qty": getattr(o, "qty", None),
                    "filled_qty": getattr(o, "filled_qty", None),
                    "status": str(getattr(o, "status", "")),
                    "order_type": str(getattr(o, "order_type", "")),
                    "created_at": getattr(o, "created_at", None),
                    "filled_at": getattr(o, "filled_at", None),
                    "limit_price": getattr(o, "limit_price", None),
                    "stop_price": getattr(o, "stop_price", None),
                }
            )

        df = pd.DataFrame(rows)
        if not df.empty:
            if "created_at" in df.columns:
                df["created_at"] = to_sgt(df["created_at"])
            if "filled_at" in df.columns:
                df["filled_at"] = to_sgt(df["filled_at"])
        return df
    except Exception as e:
        logger.warning(f"get_orders error: {e}")
        return pd.DataFrame()


def get_clock(client: TradingClient) -> Dict:
    if not client:
        return {}
    try:
        clock = client.get_clock()
        return {
            "is_open": clock.is_open,
            "next_open": clock.next_open,
            "next_close": clock.next_close,
            "timestamp": clock.timestamp,
        }
    except Exception as e:
        logger.warning(f"get_clock error: {e}")
        return {}


def load_trades_log(path: str, limit: int = 500) -> pd.DataFrame:
    rows = query_supabase(
        "trader_trades",
        params={"select": "*", "order": "created_at.desc", "limit": str(limit)},
    )
    if rows:
        df = pd.DataFrame(rows)
        if "timestamp" in df.columns:
            df["timestamp"] = to_sgt(df["timestamp"])
        elif "created_at" in df.columns:
            df["timestamp"] = to_sgt(df["created_at"])
        return df

    if not file_exists(path):
        return pd.DataFrame()

    try:
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        df = pd.DataFrame(entries)
        if "timestamp" in df.columns:
            df["timestamp"] = to_sgt(df["timestamp"])
        elif "created_at" in df.columns:
            df["timestamp"] = to_sgt(df["created_at"])
        return df
    except Exception as e:
        logger.warning(f"load_trades_log error: {e}")
        return pd.DataFrame()


def load_status_json(path: str) -> Dict:
    rows = query_supabase(
        "trader_status",
        params={"select": "*", "order": "created_at.desc", "limit": "1"},
    )
    if rows:
        row = rows[0]
        return {
            "last_run": row.get("last_run"),
            "status": row.get("status"),
            "duration_sec": row.get("duration_sec"),
            "extra": row.get("extra") or {},
        }

    if not file_exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"load_status_json error: {e}")
        return {}


def load_strategy_config(path: str) -> Dict:
    rows = query_supabase(
        "trader_config",
        params={"select": "*", "order": "updated_at.desc", "limit": "1"},
    )
    if rows:
        row = rows[0]
        return row.get("config_json", {}) or {}

    if not file_exists(path):
        return {}

    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"load_strategy_config error: {e}")
        return {}


def get_equity_curve(trades_df: pd.DataFrame, initial_balance: float = 100000.0) -> pd.DataFrame:
    if trades_df.empty or not has_cols(trades_df, ["timestamp", "action", "pnl"]):
        return pd.DataFrame()

    closed = trades_df[trades_df["action"] == "close"].copy()
    if closed.empty:
        return pd.DataFrame()

    closed = closed.sort_values("timestamp")
    running = initial_balance
    curve = []

    for _, row in closed.iterrows():
        running += safe_float(row.get("pnl"), 0.0)
        curve.append({"timestamp": row["timestamp"], "equity": running})

    return pd.DataFrame(curve)


def get_performance_metrics(trades_df: pd.DataFrame, initial_balance: float = 100000.0) -> Dict:
    metrics = {
        "total_trades": 0,
        "closed_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "avg_pnl": 0.0,
    }

    if trades_df.empty:
        return metrics

    metrics["total_trades"] = len(trades_df)

    if not has_cols(trades_df, ["action", "pnl"]):
        return metrics

    closed = trades_df[trades_df["action"] == "close"].copy()
    metrics["closed_trades"] = len(closed)
    if closed.empty:
        return metrics

    pnl_vals = pd.to_numeric(closed["pnl"], errors="coerce").dropna()
    if pnl_vals.empty:
        return metrics

    metrics["winning_trades"] = int((pnl_vals > 0).sum())
    metrics["losing_trades"] = int((pnl_vals < 0).sum())
    metrics["total_pnl"] = float(pnl_vals.sum())
    metrics["win_rate"] = (metrics["winning_trades"] / len(pnl_vals)) * 100 if len(pnl_vals) else 0.0

    wins = pnl_vals[pnl_vals > 0]
    losses = pnl_vals[pnl_vals < 0]

    if not wins.empty:
        metrics["avg_win"] = float(wins.mean())
    if not losses.empty:
        metrics["avg_loss"] = float(losses.mean())
    metrics["avg_pnl"] = float(pnl_vals.mean())

    return metrics


def get_strategy_status(config: Dict) -> pd.DataFrame:
    strategies = []
    for name, section in (config or {}).items():
        if name == "account" or not isinstance(section, dict):
            continue
        strategies.append(
            {
                "strategy": name,
                "enabled": "✅ Active" if section.get("enabled", False) else "❌ Disabled",
                "underlying": section.get("underlying", section.get("symbols", "—")),
                "data_source": section.get("data_source", "—"),
                "confirm_required": "🔐 Yes" if section.get("require_confirmation", False) else "🔓 No",
            }
        )
    return pd.DataFrame(strategies)


def render_sidebar():
    with st.sidebar:
        st.title("📊 Alcapa Dashboard")
        st.caption(f"Last updated: {get_last_refreshed()}")

        if st.button("🔄 Refresh now"):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")
        page = st.radio(
            "Navigate",
            ["Overview", "Positions", "Performance", "Strategies", "Trade Log", "Scheduler DB", "Settings"],
        )

        st.markdown("---")
        st.subheader("Environment")
        st.write(f"**Paper mode:** `{PAPER_MODE}`")
        st.write(f"**Project secrets:** `{PROJECT_SECRETS}`")
        st.write(f"**Secrets file found:** `{'Yes' if has_any_secrets_file() else 'No'}`")
        st.write(f"**SQLite DB:** `{DB_PATH}`")
        st.write(f"**DB exists:** `{'Yes' if db_exists() else 'No'}`")

        st.markdown("---")
        st.subheader("Account")
        client = get_trading_client()
        if client:
            account = fetch_account(client)
            if account:
                st.metric("Equity", format_currency(account.get("equity")))
                st.metric("Cash", format_currency(account.get("cash")))
                st.metric("Buying Power", format_currency(account.get("buying_power")))
                st.metric("Options BP", format_currency(account.get("options_buying_power")))
            else:
                st.warning("Connected, but account data could not be fetched.")
        else:
            st.info("No Alpaca connection yet. Add API keys in .streamlit/secrets.toml or env vars.")

        st.markdown("---")
        st.subheader("Market Status")
        if client:
            clock = get_clock(client)
            if clock:
                status = "🟢 OPEN" if clock["is_open"] else "🔴 CLOSED"
                st.write(f"**US Equities:** {status}")
                if clock.get("next_open"):
                    st.caption(f"Next open: {clock['next_open']}")
                if clock.get("next_close"):
                    st.caption(f"Next close: {clock['next_close']}")
            else:
                st.caption("Market clock unavailable.")
        else:
            st.caption("Market clock unavailable without API credentials.")

    return page


def render_overview():
    st.title("📈 Dashboard Overview")

    client = get_trading_client()
    if not client:
        st.warning("Add Alpaca paper API credentials to load live account data.")
        return

    account = fetch_account(client)
    positions = fetch_positions(client)

    col1, col2, col3, col4, col5 = st.columns(5)
    if account:
        initial = safe_float(account.get("initial_equity") or account.get("last_equity"), 100000.0)
        equity = safe_float(account.get("equity"), initial)
        pnl = equity - initial

        with col1:
            st.metric("Equity", format_currency(equity), delta=format_currency(pnl))
        with col2:
            st.metric("Cash", format_currency(account.get("cash")))
        with col3:
            st.metric("Buying Power", format_currency(account.get("buying_power")))
        with col4:
            st.metric("Options BP", format_currency(account.get("options_buying_power")))
        with col5:
            if not positions.empty and "market_value" in positions.columns:
                total_mv = pd.to_numeric(positions["market_value"], errors="coerce").fillna(0).sum()
                st.metric("Positions Value", format_currency(total_mv))
            else:
                st.metric("Positions Value", "—")
    else:
        st.warning("Could not fetch account data.")

    st.markdown("---")

    if not positions.empty:
        col_a, col_b = st.columns(2)

        with col_a:
            st.subheader("Open Positions")
            pos_display = positions.copy()
            if "unrealized_pl_pct" in pos_display.columns:
                pos_display["unrealized_pl_pct"] = pos_display["unrealized_pl_pct"].apply(
                    lambda x: f"{safe_float(x) * 100:+.2f}%" if x not in (None, "") else "—"
                )
            st.dataframe(pos_display, use_container_width=True, hide_index=True)

        with col_b:
            st.subheader("Position P&L")
            fig = make_subplots(specs=[[{"secondary_y": False}]])
            for _, row in positions.iterrows():
                sym = row.get("symbol", "—")
                pl = safe_float(row.get("unrealized_pl"), 0.0)
                color = "#22c55e" if pl >= 0 else "#ef4444"
                fig.add_trace(go.Bar(x=[sym], y=[pl], marker_color=color, name=sym))
            fig.update_layout(
                title="Unrealized P&L by Symbol",
                height=320,
                showlegend=False,
                margin=dict(l=20, r=20, t=40, b=20),
            )
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No open positions.")

    st.markdown("---")

    orders_today = fetch_orders(client, status="all", limit=100)
    if not orders_today.empty and "created_at" in orders_today.columns:
        today_start = pd.Timestamp.now(tz="Asia/Singapore").normalize()
        orders_today = orders_today[orders_today["created_at"] >= today_start]

    if not orders_today.empty:
        st.subheader("Today's Orders")
        st.dataframe(orders_today, use_container_width=True, hide_index=True)
    else:
        st.info("No orders today.")

    st.markdown("---")

    if db_exists():
        wheel_df = load_db_wheel_signals(5)
        if not wheel_df.empty:
            st.subheader("Latest Wheel Signals")
            preview_cols = [
                c for c in [
                    "ts", "symbol", "contract_symbol", "strike", "expiry",
                    "dte", "delta", "premium", "mode", "acted"
                ] if c in wheel_df.columns
            ]
            st.dataframe(wheel_df[preview_cols], use_container_width=True, hide_index=True)


def render_positions():
    st.title("💼 Positions")

    client = get_trading_client()
    if not client:
        st.warning("Add Alpaca API credentials to load positions and orders.")
        return

    tab1, tab2 = st.tabs(["📋 Open Positions", "📜 Order History"])

    with tab1:
        positions = fetch_positions(client)
        if not positions.empty:
            col1, col2 = st.columns(2)
            with col1:
                filter_sym = st.text_input("Filter by symbol", "").upper()
            with col2:
                filter_side = st.selectbox("Side", ["All", "long", "short"])

            df = positions.copy()
            if filter_sym and "symbol" in df.columns:
                df = df[df["symbol"].astype(str).str.contains(filter_sym, na=False)]
            if filter_side != "All" and "side" in df.columns:
                df = df[df["side"].astype(str) == filter_side]

            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button("Download CSV", df.to_csv(index=False), "positions.csv", "text/csv")
        else:
            st.info("No open positions.")

    with tab2:
        orders = fetch_orders(client, status="all", limit=200)
        if not orders.empty:
            st.dataframe(orders, use_container_width=True, hide_index=True)
            st.download_button("Download CSV", orders.to_csv(index=False), "orders.csv", "text/csv")
        else:
            st.info("No order history.")


def render_performance():
    st.title("📊 Performance")

    trades_df = load_trades_log(TRADES_LOG)
    client = get_trading_client()
    account = fetch_account(client) if client else None

    initial_balance = safe_float(account.get("initial_equity") or account.get("last_equity"), 100000.0) if account else 100000.0
    metrics = get_performance_metrics(trades_df, initial_balance)
    equity_curve = get_equity_curve(trades_df, initial_balance)

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Trades", metrics["total_trades"])
    col2.metric("Closed Trades", metrics["closed_trades"])
    col3.metric("Win Rate", f"{metrics['win_rate']:.1f}%")
    col4.metric("Total P&L", f"{metrics['total_pnl']:+.2f}")
    col5.metric("Avg P&L / Trade", f"{metrics['avg_pnl']:+.2f}")

    st.markdown("---")

    if not equity_curve.empty:
        st.subheader("Equity Curve")
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=equity_curve["timestamp"],
                y=equity_curve["equity"],
                mode="lines",
                fill="tozeroy",
                line=dict(color="#3b82f6", width=2),
                fillcolor="rgba(59,130,246,0.15)",
                name="Equity",
            )
        )
        fig.update_layout(
            height=350,
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis_title="Date",
            yaxis_title="USD",
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No equity curve data yet.")

    if not trades_df.empty and has_cols(trades_df, ["action", "pnl"]):
        closed = trades_df[trades_df["action"] == "close"].copy()
        pnl_vals = pd.to_numeric(closed["pnl"], errors="coerce").dropna()
        if not pnl_vals.empty:
            st.subheader("P&L Distribution")
            fig2 = go.Figure()
            fig2.add_trace(go.Histogram(x=pnl_vals, nbinsx=20, name="P&L"))
            fig2.update_layout(
                height=280,
                margin=dict(l=20, r=20, t=20, b=20),
                xaxis_title="P&L (USD)",
                yaxis_title="Count",
            )
            st.plotly_chart(fig2, use_container_width=True)

    if not trades_df.empty and has_cols(trades_df, ["action", "strategy", "pnl"]):
        st.subheader("P&L by Strategy")
        grp = (
            trades_df[trades_df["action"] == "close"]
            .groupby("strategy")["pnl"]
            .agg(["sum", "count"])
            .reset_index()
        )
        if not grp.empty:
            grp["sum"] = pd.to_numeric(grp["sum"], errors="coerce").fillna(0.0)
            fig3 = go.Figure()
            fig3.add_trace(
                go.Bar(
                    x=grp["strategy"],
                    y=grp["sum"],
                    marker_color=["#22c55e" if s >= 0 else "#ef4444" for s in grp["sum"]],
                    text=grp["sum"].apply(lambda x: f"${x:+.2f}"),
                )
            )
            fig3.update_layout(height=280, margin=dict(l=20, r=20, t=20, b=20))
            st.plotly_chart(fig3, use_container_width=True)


def render_strategies():
    st.title("⚙️ Strategy Status")

    config = load_strategy_config(CONFIG_PATH)
    if not config:
        st.warning(f"Could not load config from `{CONFIG_PATH}`.")
        return

    st.subheader("Active Strategies")
    df = get_strategy_status(config)
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No strategies found.")

    st.markdown("---")
    st.subheader("Strategy Parameters")

    for name, section in config.items():
        if name == "account" or not isinstance(section, dict):
            continue
        with st.expander(f"⚙️ {name}", expanded=False):
            rows = []
            for k, v in section.items():
                if isinstance(v, (dict, list)):
                    v = json.dumps(v)
                rows.append({"parameter": k, "value": v})
            st.table(pd.DataFrame(rows))


def render_trade_log():
    st.title("📋 Trade Log")

    trades_df = load_trades_log(TRADES_LOG)
    st.caption(f"Loaded {len(trades_df)} entries | Source: `{TRADES_LOG}`")

    if trades_df.empty:
        st.info(f"No trades logged yet at `{TRADES_LOG}`.")
        return

    df = trades_df.copy()

    cols = st.columns(3)

    with cols[0]:
        if "action" in df.columns:
            action_options = sorted(df["action"].dropna().astype(str).unique().tolist())
            action_filter = st.multiselect("Action", options=action_options, default=action_options)
        else:
            action_filter = []

    with cols[1]:
        if "strategy" in df.columns:
            strat_options = sorted(df["strategy"].dropna().astype(str).unique().tolist())
            strat_filter = st.multiselect("Strategy", options=strat_options, default=[])
        else:
            strat_filter = []

    with cols[2]:
        if "timestamp" in df.columns:
            start_default = (datetime.now() - timedelta(days=30)).date()
            end_default = datetime.now().date()
            date_range = st.date_input("Date range", value=(start_default, end_default))
        else:
            date_range = ()

    if action_filter and "action" in df.columns:
        df = df[df["action"].astype(str).isin(action_filter)]
    if strat_filter and "strategy" in df.columns:
        df = df[df["strategy"].astype(str).isin(strat_filter)]
    if len(date_range) == 2 and "timestamp" in df.columns:
        start, end = date_range
        df = df[(df["timestamp"].dt.date >= start) & (df["timestamp"].dt.date <= end)]

    if "timestamp" in df.columns:
        df = df.sort_values("timestamp", ascending=False)

    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("Download CSV", df.to_csv(index=False), "trades_export.csv", "text/csv")


def render_scheduler_db():
    st.title("🗄️ Scheduler DB")

    if not db_exists():
        st.warning(f"Database not found at `{DB_PATH}`. Run the scheduler first.")
        return

    summary_df = load_db_summary()

    top1, top2, top3 = st.columns([3, 2, 2])
    with top1:
        st.caption(f"SQLite database: `{DB_PATH}`")
    with top2:
        st.metric("Signals", metric_value(summary_df, "signals"))
    with top3:
        st.metric("Orders", metric_value(summary_df, "orders"))

    m1, m2, m3 = st.columns(3)
    m1.metric("Fills", metric_value(summary_df, "fills"))
    m2.metric("Stock positions", metric_value(summary_df, "positions"))
    m3.metric("Option positions", metric_value(summary_df, "options_positions"))

    st.markdown("---")

    c1, c2, c3 = st.columns(3)
    with c1:
        row_limit = st.slider("Rows per table", min_value=5, max_value=100, value=20, step=5)
    with c2:
        show_only_signal_only = st.checkbox("Wheel: signal_only only", value=False)
    with c3:
        show_only_unacted = st.checkbox("Signals: acted = 0 only", value=False)

    wheel_df = load_db_wheel_signals(row_limit)
    swing_df = load_db_swing_signals(row_limit)
    orders_df = load_db_orders(row_limit)
    fills_df = load_db_fills(row_limit)
    stock_positions_df = load_db_stock_positions()
    option_positions_df = load_db_option_positions()

    if show_only_signal_only and not wheel_df.empty and "mode" in wheel_df.columns:
        wheel_df = wheel_df[wheel_df["mode"] == "signal_only"]

    if show_only_unacted:
        if not wheel_df.empty and "acted" in wheel_df.columns:
            wheel_df = wheel_df[wheel_df["acted"] == 0]
        if not swing_df.empty and "acted" in swing_df.columns:
            swing_df = swing_df[swing_df["acted"] == 0]

    st.markdown("---")
    st.subheader("Scheduler P&L")

    pnl_df = load_scheduler_pnl_summary()
    if pnl_df.empty:
        st.info("No closed scheduler positions with P&L yet.")
    else:
        pnl_grouped = (
            pnl_df.groupby("symbol", as_index=False)["pnl"]
            .sum()
            .sort_values("pnl", ascending=False)
        )

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                x=pnl_grouped["symbol"],
                y=pnl_grouped["pnl"],
                marker_color=["#22c55e" if v >= 0 else "#ef4444" for v in pnl_grouped["pnl"]],
                text=[f"${v:,.2f}" for v in pnl_grouped["pnl"]],
                textposition="outside",
            )
        )
        fig.update_layout(
            height=280,
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis_title="Symbol",
            yaxis_title="P&L (USD)",
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.subheader("Earnings-blocked panel")

    earnings_df = load_earnings_blocked_signals(limit=20)
    if earnings_df.empty:
        st.caption("No earnings-blocked rows are currently stored in SQLite.")
        st.info("Current scheduler logs earnings skips, but will only appear here after those skips are persisted as signal rows.")
    else:
        st.dataframe(earnings_df, use_container_width=True, hide_index=True)

    st.subheader("Recent options candidates")
    recent_candidates_df = load_recent_options_candidates(limit=row_limit)
    if recent_candidates_df.empty:
        st.info("No recent options candidates found.")
    else:
        st.dataframe(recent_candidates_df, use_container_width=True, hide_index=True)

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Wheel signals",
        "Swing signals",
        "Orders",
        "Positions",
        "Fills",
    ])

    with tab1:
        st.subheader("Wheel signals")
        if wheel_df.empty:
            st.info("No wheel signals found.")
        else:
            st.dataframe(wheel_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download wheel signals CSV",
                wheel_df.to_csv(index=False),
                "wheel_signals.csv",
                "text/csv",
                key="wheel_signals_csv",
            )

    with tab2:
        st.subheader("Swing signals")
        if swing_df.empty:
            st.info("No swing signals found.")
        else:
            st.dataframe(swing_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download swing signals CSV",
                swing_df.to_csv(index=False),
                "swing_signals.csv",
                "text/csv",
                key="swing_signals_csv",
            )

    with tab3:
        st.subheader("Scheduler orders")
        if orders_df.empty:
            st.info("No scheduler orders found.")
        else:
            st.dataframe(orders_df, use_container_width=True, hide_index=True)

    with tab4:
        st.subheader("Scheduler positions")
        left, right = st.columns(2)

        with left:
            st.markdown("#### Stock positions")
            if stock_positions_df.empty:
                st.info("No stock positions found.")
            else:
                st.dataframe(stock_positions_df, use_container_width=True, hide_index=True)

        with right:
            st.markdown("#### Option positions")
            if option_positions_df.empty:
                st.info("No option positions found.")
            else:
                st.dataframe(option_positions_df, use_container_width=True, hide_index=True)

    with tab5:
        st.subheader("Scheduler fills")
        if fills_df.empty:
            st.info("No fills found.")
        else:
            st.dataframe(fills_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    st.subheader("Latest wheel candidate details")
    if wheel_df.empty:
        st.info("No wheel candidate metadata available.")
    else:
        preview_cols = [
            c for c in [
                "ts", "symbol", "contract_symbol", "strike", "expiry",
                "dte", "delta", "premium", "mode", "acted"
            ] if c in wheel_df.columns
        ]
        st.dataframe(wheel_df[preview_cols], use_container_width=True, hide_index=True)


def render_settings():
    st.title("🔧 Settings & Controls")

    st.subheader("API Configuration")
    client = get_trading_client()
    if client:
        account = fetch_account(client)
        st.success("Connected to Alpaca")
        if account:
            st.write(f"**Account ID:** `{account.get('id', '—')}`")
            st.write(f"**Status:** `{account.get('status', '—')}`")
            st.write(f"**Initial/Last Equity:** {format_currency(account.get('initial_equity') or account.get('last_equity'))}")
            st.write(f"**Currency:** `{account.get('currency', '—')}`")
            st.write(f"**Options Approved Level:** `{account.get('options_approved_level', '—')}`")
            st.write(f"**Options Trading Level:** `{account.get('options_trading_level', '—')}`")
            st.write(f"**Options Buying Power:** {format_currency(account.get('options_buying_power'))}")
    else:
        st.warning("Not connected. Add `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`.")

    st.markdown("---")
    st.subheader("Trader Status")
    status = load_status_json(STATUS_FILE)
    if status:
        col1, col2 = st.columns(2)
        col1.write(f"**Last Run:** `{status.get('last_run', '—')}`")
        col1.write(f"**Status:** `{status.get('status', '—')}`")
        dur = status.get("duration_sec")
        col2.write(f"**Duration:** `{dur:.2f}s`" if isinstance(dur, (int, float)) else "**Duration:** —")
    else:
        st.info(f"No status file at `{STATUS_FILE}`.")

    st.markdown("---")
    st.subheader("Environment Info")
    st.write(f"**Config path:** `{CONFIG_PATH}`")
    st.write(f"**Trades log:** `{TRADES_LOG}`")
    st.write(f"**Status file:** `{STATUS_FILE}`")
    st.write(f"**Paper mode:** `{PAPER_MODE}`")
    st.write(f"**Project secrets path:** `{PROJECT_SECRETS}`")
    st.write(f"**Global secrets path:** `{GLOBAL_SECRETS}`")
    st.write(f"**Secrets file detected:** `{'Yes' if has_any_secrets_file() else 'No'}`")
    st.write(f"**SQLite DB path:** `{DB_PATH}`")
    st.write(f"**SQLite DB exists:** `{'Yes' if db_exists() else 'No'}`")


def main():
    page = render_sidebar()

    if page == "Overview":
        render_overview()
    elif page == "Positions":
        render_positions()
    elif page == "Performance":
        render_performance()
    elif page == "Strategies":
        render_strategies()
    elif page == "Trade Log":
        render_trade_log()
    elif page == "Scheduler DB":
        render_scheduler_db()
    elif page == "Settings":
        render_settings()


if __name__ == "__main__":
    main()
