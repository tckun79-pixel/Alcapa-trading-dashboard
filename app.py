"""
Alpaca Trading Dashboard — Streamlit Application
Monitor your Alpaca paper trading account, positions, performance, and strategies.
Streamlit Cloud-ready with secure secrets management.
"""

import streamlit as st
import os
import json
import time
import logging
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.express as px

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, GetPositionRequest
from alpaca.trading.enums import OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ─── Page Configuration ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="Alcapa Dashboard",
    page_icon="📊",
    layout="wide",
    menu_items={
        "About": "Alpaca Paper Trading Dashboard — built for CK's options strategies",
    },
)

# ─── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("dashboard")

# ─── Secrets / Env ─────────────────────────────────────────────────────────────
def get_secret(key: str, default=None):
    """Fetch from Streamlit secrets (cloud) or environment (local)."""
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)


API_KEY = get_secret("APCA_API_KEY_ID", os.getenv("APCA_API_KEY_ID"))
API_SECRET = get_secret("APCA_API_SECRET_KEY", os.getenv("APCA_API_SECRET_KEY"))
PAPER_MODE = get_secret("APCA_API_PAPER", "true").lower() == "true"

CONFIG_PATH = os.getenv("CONFIG_PATH", "config/strategy.yaml")
TRADES_LOG = os.getenv("TRADES_LOG", "logs/trades.jsonl")
STATUS_FILE = os.getenv("STATUS_FILE", "data/status.json")
AUTO_REFRESH_INTERVAL = int(get_secret("AUTO_REFRESH_INTERVAL", "60"))

# ─── Client Factory ─────────────────────────────────────────────────────────────
@st.cache_resource(ttl=300)
def get_trading_client():
    """Cached Alpaca trading client."""
    if not API_KEY or not API_SECRET:
        return None
    try:
        client = TradingClient(
            api_key=API_KEY,
            secret_key=API_SECRET,
            paper=PAPER_MODE,
        )
        # Verify credentials with a simple call
        _ = client.get_account()
        return client
    except Exception as e:
        st.error(f"Failed to connect to Alpaca: {e}")
        return None


@st.cache_resource(ttl=300)
def get_data_client():
    """Cached Alpaca data client for market data."""
    if not API_KEY or not API_SECRET:
        return None
    try:
        return StockHistoricalDataClient(
            api_key=API_KEY,
            secret_key=API_SECRET,
        )
    except Exception:
        return None

# ─── State ───────────────────────────────────────────────────────────────────────
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = None
if "strategy_config" not in st.session_state:
    st.session_state.strategy_config = None
if "positions_df" not in st.session_state:
    st.session_state.positions_df = None
if "account_data" not in st.session_state:
    st.session_state.account_data = None
if "orders_df" not in st.session_state:
    st.session_state.orders_df = None
if "trades_df" not in st.session_state:
    st.session_state.trades_df = None

# ─── Helpers ────────────────────────────────────────────────────────────────────

def format_currency(value, currency="USD"):
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def format_pct(value):
    if value is None:
        return "—"
    try:
        v = float(value)
        color = "green" if v >= 0 else "red"
        return f"<span style='color:{color}'>{v:+.2f}%</span>"
    except (TypeError, ValueError):
        return str(value)


def get_last_refreshed():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S SGT")


def fetch_account(client: TradingClient) -> Optional[Dict]:
    """Fetch account information."""
    try:
        account = client.get_account()
        return {
            "account_fractional": account.account_fractional,
            "cash": account.cash,
            "portfolio_float_pct": account.portfolio_float_pct,
            "portfolio_unrealized_pl": account.portfolio_unrealized_pl,
            "currency": account.currency,
            "buying_power": account.buying_power,
            "equity": account.equity,
            "id": account.id,
            "initial_equity": account.initial_equity,
            "sma": account.sma,
            "status": account.status,
        }
    except Exception as e:
        logger.warning(f"get_account error: {e}")
        return None


def fetch_positions(client: TradingClient) -> pd.DataFrame:
    """Fetch all open positions."""
    try:
        positions = client.get_all_positions()
        if not positions:
            return pd.DataFrame()
        rows = []
        for p in positions:
            rows.append({
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_price": p.avg_entry_price,
                "current_price": p.current_price,
                "market_value": p.market_value,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_pl_pct": p.unrealized_plpc,
                "side": p.side,
                "qty_available": p.qty_available,
            })
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"get_all_positions error: {e}")
        return pd.DataFrame()


def fetch_orders(client: TradingClient, status="all", limit=50) -> pd.DataFrame:
    """Fetch orders."""
    try:
        status_map = {
            "open": OrderStatus.OPEN,
            "closed": OrderStatus.CLOSED,
            "all": None,
        }
        req_status = status_map.get(status)
        if req_status:
            orders_req = GetOrdersRequest(status=req_status, limit=limit)
        else:
            orders_req = GetOrdersRequest(limit=limit)
        orders = client.get_orders(orders_req)
        if not orders:
            return pd.DataFrame()
        rows = []
        for o in orders:
            rows.append({
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.qty,
                "filled_qty": o.filled_qty,
                "status": o.status,
                "order_type": o.order_type,
                "created_at": o.created_at,
                "filled_at": o.filled_at,
                "limit_price": o.limit_price,
                "stop_price": o.stop_price,
            })
        df = pd.DataFrame(rows)
        if "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"]).dt.tz_convert("Asia/Singapore")
        return df
    except Exception as e:
        logger.warning(f"get_orders error: {e}")
        return pd.DataFrame()


def load_trades_log(path: str, limit: int = 200) -> pd.DataFrame:
    """Load trades from the JSONL log file."""
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        entries = []
        with open(path, "r") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not entries:
            return pd.DataFrame()
        df = pd.DataFrame(entries)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("Asia/Singapore")
        return df
    except Exception as e:
        logger.warning(f"load_trades_log error: {e}")
        return pd.DataFrame()


def load_status_json(path: str) -> Dict:
    """Load the status JSON file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_strategy_config(path: str) -> Dict:
    """Load the strategy YAML config."""
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def get_equity_curve(trades_df: pd.DataFrame, initial_balance: float = 100000.0) -> pd.DataFrame:
    """Build equity curve from trade log."""
    if trades_df.empty or "timestamp" not in trades_df.columns:
        return pd.DataFrame()
    # Filter closed trades with pnl
    closed = trades_df[trades_df["action"] == "close"].copy()
    if closed.empty:
        return pd.DataFrame()
    closed = closed.sort_values("timestamp")
    equity = [initial_balance]
    times = [pd.Timestamp(closed["timestamp"].iloc[0]) if not closed.empty else pd.Timestamp.now()]
    for _, row in closed.iterrows():
        pnl = float(row.get("pnl") or 0)
        equity.append(equity[-1] + pnl)
        times.append(row["timestamp"])
    if len(equity) < 2:
        return pd.DataFrame()
    return pd.DataFrame({"timestamp": times[1:], "equity": equity[1:]})


def get_performance_metrics(trades_df: pd.DataFrame, account_df: pd.DataFrame, initial_balance: float = 100000.0) -> Dict:
    """Compute performance metrics from trade log."""
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
    all_trades = len(trades_df)
    closed = trades_df[trades_df["action"] == "close"]
    closed_count = len(closed)
    metrics["total_trades"] = all_trades
    metrics["closed_trades"] = closed_count
    if closed_count == 0:
        return metrics
    pnl_vals = closed["pnl"].dropna().astype(float)
    if pnl_vals.empty:
        return metrics
    metrics["winning_trades"] = int((pnl_vals > 0).sum())
    metrics["losing_trades"] = int((pnl_vals < 0).sum())
    metrics["total_pnl"] = float(pnl_vals.sum())
    metrics["win_rate"] = metrics["winning_trades"] / closed_count * 100
    wins = pnl_vals[pnl_vals > 0]
    losses = pnl_vals[pnl_vals < 0]
    if not wins.empty:
        metrics["avg_win"] = float(wins.mean())
    if not losses.empty:
        metrics["avg_loss"] = float(losses.mean())
    metrics["avg_pnl"] = float(pnl_vals.mean())
    return metrics


def get_strategy_status(config: Dict) -> pd.DataFrame:
    """Build strategy status table from config."""
    strategies = []
    for name, section in config.items():
        if name == "account":
            continue
        if isinstance(section, dict):
            strategies.append({
                "strategy": name,
                "enabled": "✅ Active" if section.get("enabled", False) else "❌ Disabled",
                "underlying": section.get("underlying", section.get("symbols", "—")),
                "data_source": section.get("data_source", "—"),
                "confirm_required": "🔐 Yes" if section.get("require_confirmation", False) else "🔓 No",
            })
    return pd.DataFrame(strategies)


def get_clock(client: TradingClient) -> Dict:
    """Get market clock / trading hours."""
    try:
        clock = client.get_clock()
        return {
            "is_open": clock.is_open,
            "next_open": clock.next_open,
            "next_close": clock.next_close,
            "timestamp": clock.timestamp,
        }
    except Exception:
        return {}


# ─── Sidebar ────────────────────────────────────────────────────────────────────
def render_sidebar():
    with st.sidebar:
        st.title("📊 Alcapa Dashboard")
        st.markdown("---")
        st.caption(f"Last updated: {get_last_refreshed()}")
        if st.button("🔄 Refresh Data"):
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")
        st.subheader("Auto-refresh")
        interval = st.selectbox(
            "Interval",
            options=[0, 30, 60, 120, 300],
            format_func=lambda x: "Off" if x == 0 else f"{x}s",
            index=2,
        )
        if interval > 0:
            time.sleep(interval)
            st.rerun()

        st.markdown("---")
        st.subheader("Account")
        client = get_trading_client()
        if client:
            try:
                account = client.get_account()
                st.metric("Equity", format_currency(account.equity))
                st.metric("Cash", format_currency(account.cash))
                st.metric("Buying Power", format_currency(account.buying_power))
            except Exception as e:
                st.error(f"Account error: {e}")
        else:
            st.warning("Connect your Alpaca API keys to see account info.")

        st.markdown("---")
        st.subheader("Market Status")
        if client:
            try:
                clock = get_clock(client)
                if clock:
                    status = "🟢 OPEN" if clock["is_open"] else "🔴 CLOSED"
                    st.write(f"**US Equities:** {status}")
                    if not clock["is_open"]:
                        if clock.get("next_open"):
                            st.caption(f"Next open: {clock['next_open']}")
                        if clock.get("next_close"):
                            st.caption(f"Next close: {clock['next_close']}")
            except Exception:
                st.info("Market clock unavailable")
        st.markdown("---")
        st.caption("Streamlit Cloud ready | Paper trading only")


# ─── Page: Dashboard Overview ────────────────────────────────────────────────────
def render_overview(client: TradingClient):
    st.title("📈 Dashboard Overview")

    account = fetch_account(client)
    positions = fetch_positions(client)
    today_trades = pd.DataFrame()  # filtered separately

    # Account metrics row
    col1, col2, col3, col4 = st.columns(4)
    if account:
        initial = float(account.get("initial_equity", initial_balance) or initial_balance)
        equity = float(account.get("equity", initial))
        pnl = equity - initial
        pnl_pct = (pnl / initial) * 100 if initial > 0 else 0
        with col1:
            st.metric("Equity", format_currency(equity), delta=format_currency(pnl))
        with col2:
            st.metric("Cash", format_currency(account.get("cash")))
        with col3:
            st.metric("Buying Power", format_currency(account.get("buying_power")))
        with col4:
            if positions is not None and not positions.empty:
                total_mv = positions["market_value"].astype(float).sum()
                st.metric("Positions Value", format_currency(total_mv))
            else:
                st.metric("Positions Value", "—")
    else:
        st.warning("Could not fetch account data. Check your API keys.")

    st.markdown("---")

    # Positions summary
    if not positions.empty:
        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("Open Positions")
            pos_display = positions.copy()
            pos_display["unrealized_pl_pct"] = pos_display["unrealized_pl_pct"].apply(
                lambda x: f"{float(x):+.2f}%" if x else "—"
            )
            st.dataframe(
                pos_display[["symbol", "qty", "avg_price", "current_price", "market_value", "unrealized_pl", "unrealized_pl_pct", "side"]],
                use_container_width=True,
                hide_index=True,
            )
        with col_b:
            st.subheader("Position P&L")
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            for _, row in positions.iterrows():
                sym = row["symbol"]
                pl = float(row.get("unrealized_pl") or 0)
                color = "#22c55e" if pl >= 0 else "#ef4444"
                fig.add_trace(
                    go.Bar(
                        x=[sym],
                        y=[pl],
                        marker_color=color,
                        name=sym,
                    ),
                    secondary_y=False,
                )
            fig.update_layout(
                title="Unrealized P&L by Symbol",
                height=300,
                showlegend=False,
                margin=dict(l=20, r=20, t=40, b=20),
            )
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No open positions.")

    st.markdown("---")

    # Today's activity from orders
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    orders_today = fetch_orders(client, status="all", limit=100)
    if not orders_today.empty and "created_at" in orders_today.columns:
        orders_today = orders_today[orders_today["created_at"] >= today_start]
    if not orders_today.empty:
        st.subheader("Today's Orders")
        display_cols = ["symbol", "side", "qty", "filled_qty", "status", "created_at"]
        avail = [c for c in display_cols if c in orders_today.columns]
        st.dataframe(orders_today[avail], use_container_width=True, hide_index=True)
    else:
        st.info("No orders today.")


# ─── Page: Positions ───────────────────────────────────────────────────────────
def render_positions(client: TradingClient):
    st.title("💼 Positions")
    st.caption(f"Last updated: {get_last_refreshed()}")

    tab1, tab2 = st.tabs(["📋 Open Positions", "📜 Order History"])

    with tab1:
        positions = fetch_positions(client)
        if not positions.empty:
            col_sym, col_side = st.columns(2)
            with col_sym:
                filter_sym = st.text_input("Filter by symbol", "").upper()
            with col_side:
                filter_side = st.selectbox("Side", options=["All", "long", "short"])
            df = positions
            if filter_sym:
                df = df[df["symbol"].str.contains(filter_sym, na=False)]
            if filter_side != "All":
                df = df[df["side"] == filter_side]

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
            )
            st.download_button(
                "Download CSV",
                df.to_csv(index=False),
                "positions.csv",
                "text/csv",
            )
        else:
            st.info("No open positions.")

    with tab2:
        orders = fetch_orders(client, status="all", limit=200)
        if not orders.empty:
            st.dataframe(orders, use_container_width=True, hide_index=True)
            st.download_button(
                "Download CSV",
                orders.to_csv(index=False),
                "orders.csv",
                "text/csv",
            )
        else:
            st.info("No order history.")


# ─── Page: Performance ─────────────────────────────────────────────────────────
def render_performance():
    st.title("📊 Performance")

    trades_df = load_trades_log(TRADES_LOG)
    account = None
    try:
        client = get_trading_client()
        if client:
            account = fetch_account(client)
    except Exception:
        pass

    initial_balance = 100000.0
    if account:
        try:
            ib = account.get("initial_equity")
            if ib:
                initial_balance = float(ib)
        except Exception:
            pass

    col1, col2, col3, col4, col5 = st.columns(5)

    # Compute metrics
    metrics = get_performance_metrics(trades_df, account, initial_balance)
    equity_curve = get_equity_curve(trades_df, initial_balance)

    with col1:
        st.metric("Total Trades", metrics["total_trades"])
    with col2:
        st.metric("Closed Trades", metrics["closed_trades"])
    with col3:
        st.metric("Win Rate", f"{metrics['win_rate']:.1f}%")
    with col4:
        clr = "#22c55e" if metrics["total_pnl"] >= 0 else "#ef4444"
        st.markdown(f"<div style='text-align:center'><big style='color:{clr}'>{metrics['total_pnl']:+.2f}</big><br><small>Total P&L</small></div>", unsafe_allow_html=True)
    with col5:
        st.metric("Avg P&L / Trade", f"{metrics['avg_pnl']:+.2f}")

    st.markdown("---")

    # Equity Curve
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
        st.info("No equity curve data yet. Trade log is empty or no closed trades with P&L recorded.")

    # P&L Distribution
    if not trades_df.empty:
        closed = trades_df[trades_df["action"] == "close"]
        pnl_vals = closed["pnl"].dropna().astype(float)
        if not pnl_vals.empty:
            st.subheader("P&L Distribution")
            fig2 = go.Figure()
            colors = ["#22c55e" if v >= 0 else "#ef4444" for v in pnl_vals]
            fig2.add_trace(
                go.Histogram(
                    x=pnl_vals,
                    marker_color=colors,
                    nbinsx=20,
                    name="P&L",
                )
            )
            fig2.update_layout(
                height=280,
                margin=dict(l=20, r=20, t=20, b=20),
                xaxis_title="P&L (USD)",
                yaxis_title="Count",
            )
            st.plotly_chart(fig2, use_container_width=True)

    # Strategy breakdown
    if not trades_df.empty:
        st.subheader("P&L by Strategy")
        grp = trades_df[trades_df["action"] == "close"].groupby("strategy")["pnl"].agg(["sum", "count"]).reset_index()
        if not grp.empty:
            fig3 = go.Figure()
            fig3.add_trace(
                go.Bar(
                    x=grp["strategy"],
                    y=grp["sum"],
                    marker_color=["#22c55e" if s >= 0 else "#ef4444" for s in grp["sum"]],
                    text=grp["sum"].apply(lambda x: f"${x:+.2f}"),
                )
            )
            fig3.update_layout(height=250, margin=dict(l=20, r=20, t=20, b=20))
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.info("No closed trades to analyze by strategy.")

    # Win/Loss summary
    if metrics["winning_trades"] > 0 or metrics["losing_trades"] > 0:
        st.subheader("Win/Loss Summary")
        col_w, col_l = st.columns(2)
        with col_w:
            st.metric("Winning Trades", metrics["winning_trades"], delta=f"Avg win: ${metrics['avg_win']:.2f}")
        with col_l:
            st.metric("Losing Trades", metrics["losing_trades"], delta=f"Avg loss: ${metrics['avg_loss']:.2f}")


# ─── Page: Strategies ───────────────────────────────────────────────────────────
def render_strategies():
    st.title("⚙️ Strategy Status")

    config = load_strategy_config(CONFIG_PATH)
    if not config:
        st.warning(f"Could not load config from `{CONFIG_PATH}`. Ensure strategy.yaml exists or set CONFIG_PATH env var.")
        return

    st.subheader("Active Strategies")
    df = get_strategy_status(config)
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No strategies found in config.")

    st.markdown("---")

    # Detailed strategy parameters
    st.subheader("Strategy Parameters")
    for name, section in config.items():
        if name == "account" or not isinstance(section, dict):
            continue
        with st.expander(f"⚙️ {name}", expanded=False):
            if isinstance(section, dict):
                # Flatten for display
                rows = []
                for k, v in section.items():
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v)
                    rows.append({"parameter": k, "value": v})
                st.table(pd.DataFrame(rows))


# ─── Page: Trade Log ─────────────────────────────────────────────────────────────
def render_trade_log():
    st.title("📋 Trade Log")

    trades_df = load_trades_log(TRADES_LOG)
    st.caption(f"Loaded {len(trades_df)} entries | Source: `{TRADES_LOG}`")

    if not trades_df.empty:
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            action_filter = st.multiselect(
                "Action",
                options=["open", "close", "error"],
                default=["open", "close", "error"],
            )
        with col_f2:
            strat_filter = st.multiselect(
                "Strategy",
                options=sorted(trades_df["strategy"].dropna().unique().tolist()),
                default=[],
            )
        with col_f3:
            date_range = st.date_input(
                "Date range",
                value=(datetime.now() - timedelta(days=30), datetime.now()),
            )

        df = trades_df.copy()
        if action_filter:
            df = df[df["action"].isin(action_filter)]
        if strat_filter:
            df = df[df["strategy"].isin(strat_filter)]
        if len(date_range) == 2:
            start, end = date_range
            df = df[
                (df["timestamp"].dt.date >= start)
                & (df["timestamp"].dt.date <= end)
            ]

        st.dataframe(
            df.sort_values("timestamp", ascending=False),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "Download Full Log (CSV)",
            df.to_csv(index=False),
            "trades_export.csv",
            "text/csv",
        )
    else:
        st.info(f"No trades logged yet at `{TRADES_LOG}`.")


# ─── Page: Settings ──────────────────────────────────────────────────────────────
def render_settings():
    st.title("🔧 Settings & Controls")

    st.subheader("API Configuration")
    client = get_trading_client()
    if client:
        st.success("✅ Connected to Alpaca")
        try:
            account = client.get_account()
            st.write(f"**Account ID:** `{account.id}`")
            st.write(f"**Status:** `{account.status}`")
            st.write(f"**Initial Equity:** {format_currency(account.initial_equity)}")
            st.write(f"**Currency:** `{account.currency}`")
        except Exception as e:
            st.error(f"Account details error: {e}")
    else:
        st.error("❌ Not connected. Ensure `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` are set via Streamlit secrets or environment variables.")

    st.markdown("---")
    st.subheader("Trader Status")
    status = load_status_json(STATUS_FILE)
    if status:
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            st.write(f"**Last Run:** `{status.get('last_run', '—')}`")
            st.write(f"**Status:** `{status.get('status', '—')}`")
        with col_s2:
            dur = status.get("duration_sec")
            st.write(f"**Duration:** `{dur:.2f}s`" if dur else "**Duration:** —")
    else:
        st.info(f"No status file at `{STATUS_FILE}`. Run the trader to see status here.")

    st.markdown("---")
    st.subheader("Strategy Controls")
    st.warning("⚠️ Changing strategy settings here updates the live config file. Be careful!")
    config = load_strategy_config(CONFIG_PATH)
    if config:
        for name, section in config.items():
            if name == "account" or not isinstance(section, dict):
                continue
            enabled = section.get("enabled", False)
            new_state = st.checkbox(f"Enable {name}", value=enabled, key=f"toggle_{name}")
            if new_state != enabled:
                section["enabled"] = new_state
                try:
                    import yaml
                    with open(CONFIG_PATH, "w") as f:
                        yaml.safe_dump(config, f, sort_keys=False)
                    st.success(f"{name} {'enabled' if new_state else 'disabled'}")
                except Exception as e:
                    st.error(f"Failed to update config: {e}")
    else:
        st.warning("Config file not accessible.")

    st.markdown("---")
    st.subheader("Environment Info")
    st.write(f"**Config path:** `{CONFIG_PATH}`")
    st.write(f"**Trades log:** `{TRADES_LOG}`")
    st.write(f"**Status file:** `{STATUS_FILE}`")
    st.write(f"**Paper mode:** `{PAPER_MODE}`")


# ─── Main ───────────────────────────────────────────────────────────────────────
def main():
    render_sidebar()

    # Guard: require API keys
    if not API_KEY or not API_SECRET:
        st.error("API credentials not configured. Set `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in Streamlit secrets or environment.")
        st.stop()

    client = get_trading_client()
    if not client:
        st.error("Could not connect to Alpaca. Check your API keys and try again.")
        st.stop()

    # Navigation
    page = st.navigation([
        st.Page(render_overview, title="Overview", icon="🏠"),
        st.Page(render_positions, title="Positions", icon="💼"),
        st.Page(render_performance, title="Performance", icon="📊"),
        st.Page(render_strategies, title="Strategies", icon="⚙️"),
        st.Page(render_trade_log, title="Trade Log", icon="📋"),
        st.Page(render_settings, title="Settings", icon="🔧"),
    ])
    page.run()


if __name__ == "__main__":
    main()
