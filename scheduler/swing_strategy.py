"""
Swing Strategy — daily + 30-min scan, long-only signals, ATR stops.
Uses yfinance for historical bars to avoid Alpaca free-tier SIP restriction.
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from .db import init_db, insert, fetch_open_positions, close_position
from .utils import (
    flatten_yf, sma, ema, atr, rolling_high,
    earnings_within_days, position_size_by_risk,
)

logger = logging.getLogger("swing_strategy")

# ─── Config ───────────────────────────────────────────────────────────────────
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMD", "META",
    "GOOGL", "AMZN", "TSLA", "JPM", "V",
    "UNH", "XOM", "LLY", "AVGO", "MA",
]

MAX_POSITIONS    = 5
RISK_PCT         = 0.005   # 0.5% equity risk per trade
ATR_STOP_MULT    = 1.5     # stop = entry − ATR × multiplier
DAILY_PERIOD     = "300d"  # yfinance period string for daily bars
INTRA_PERIOD     = "60d"   # yfinance period string for 30-min bars
VOLUME_RATIO_MIN = 1.2     # 30-min recent volume must be ≥ 1.2× rolling avg
EARNINGS_GUARD   = 7


# ─── Data Fetching ────────────────────────────────────────────────────────────

def fetch_daily_bars(data_client: StockHistoricalDataClient, symbol: str) -> pd.DataFrame:
    """
    Download daily OHLCV from yfinance.
    data_client is accepted for interface compatibility but not used here
    because free-tier Alpaca paper blocks recent SIP data.
    """
    try:
        raw = yf.download(
            symbol,
            period=DAILY_PERIOD,
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        df = flatten_yf(raw)
        if df.empty:
            logger.warning("fetch_daily_bars: empty result for %s", symbol)
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
    except Exception as e:
        logger.warning("fetch_daily_bars error for %s: %s", symbol, e)
        return pd.DataFrame()


def fetch_30min_bars(data_client: StockHistoricalDataClient, symbol: str) -> pd.DataFrame:
    """
    Download 30-minute OHLCV from yfinance (max 60-day history on free tier).
    """
    try:
        raw = yf.download(
            symbol,
            period=INTRA_PERIOD,
            interval="30m",
            auto_adjust=True,
            progress=False,
        )
        df = flatten_yf(raw)
        if df.empty:
            logger.warning("fetch_30min_bars: empty result for %s", symbol)
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
    except Exception as e:
        logger.warning("fetch_30min_bars error for %s: %s", symbol, e)
        return pd.DataFrame()


# ─── Signal Logic ─────────────────────────────────────────────────────────────

def compute_signal(daily: pd.DataFrame, intraday: pd.DataFrame, symbol: str) -> Optional[dict]:
    """
    Returns a signal dict if all entry conditions pass, else None.

    Conditions:
      1. price > SMA50 > SMA200  (trend filter)
      2. pullback to ≤1% of EMA20  OR  breakout above prior 20-day high  (entry trigger)
      3. 30-min recent volume ≥ VOLUME_RATIO_MIN × rolling average  (volume filter)
      4. ATR-based stop is below current price  (valid risk)
    """
    if len(daily) < 210:
        logger.debug("%s: insufficient bars (%d)", symbol, len(daily))
        return None

    close   = daily["close"]
    s50     = sma(close, 50).iloc[-1]
    s200    = sma(close, 200).iloc[-1]
    e20     = ema(close, 20).iloc[-1]
    h20     = rolling_high(close, 20).iloc[-2]   # prior 20-day high, exclude today
    current = close.iloc[-1]
    atr14   = atr(daily, 14).iloc[-1]

    if pd.isna(s50) or pd.isna(s200) or pd.isna(e20) or pd.isna(atr14):
        return None

    # 1. Trend filter
    if not (current > s50 > s200):
        return None

    # 2. Entry trigger
    pullback_to_ema = abs(current - e20) / e20 < 0.01
    breakout_above  = current > h20 * 1.001

    if not (pullback_to_ema or breakout_above):
        return None

    # 3. Volume filter on 30-min bars
    if not intraday.empty and len(intraday) >= 10:
        avg_vol    = intraday["volume"].iloc[-30:].mean() if len(intraday) >= 30 else intraday["volume"].mean()
        recent_vol = intraday["volume"].iloc[-10:].mean()
        if avg_vol > 0 and (recent_vol / avg_vol) < VOLUME_RATIO_MIN:
            logger.debug("%s failed volume filter (ratio=%.2f)", symbol, recent_vol / avg_vol)
            return None

    # 4. Stop validity
    stop = current - atr14 * ATR_STOP_MULT
    if stop >= current:
        return None

    signal_type = "pullback_ema20" if pullback_to_ema else "breakout_20d_high"

    return {
        "strategy":    "swing_strategy",
        "symbol":      symbol,
        "signal_type": signal_type,
        "price":       round(float(current), 4),
        "atr":         round(float(atr14), 4),
        "stop_loss":   round(float(stop), 4),
        "meta": {
            "sma50":  round(float(s50), 4),
            "sma200": round(float(s200), 4),
            "ema20":  round(float(e20), 4),
            "high20": round(float(h20), 4),
        },
    }


# ─── Order Execution ──────────────────────────────────────────────────────────

def submit_swing_order(
    trading_client: TradingClient,
    signal: dict,
    equity: float,
) -> Optional[dict]:
    qty = position_size_by_risk(
        equity=equity,
        risk_pct=RISK_PCT,
        entry=signal["price"],
        stop=signal["stop_loss"],
    )
    if qty <= 0:
        logger.warning("Position size 0 for %s, skipping", signal["symbol"])
        return None

    limit_price = round(signal["price"] * 1.002, 2)   # max 0.2% slippage

    order_req = LimitOrderRequest(
        symbol=signal["symbol"],
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    try:
        order = trading_client.submit_order(order_req)
        order_row = {
            "strategy":        "swing_strategy",
            "symbol":          signal["symbol"],
            "alpaca_order_id": str(order.id),
            "side":            "buy",
            "qty":             qty,
            "order_type":      "limit",
            "limit_price":     limit_price,
            "status":          str(order.status),
            "meta":            signal,
        }
        insert("orders", order_row)
        insert("positions", {
            "strategy":    "swing_strategy",
            "symbol":      signal["symbol"],
            "qty":         qty,
            "entry_price": limit_price,
            "stop_loss":   signal["stop_loss"],
            "status":      "open",
            "meta":        signal,
        })
        signal["qty"] = qty
        logger.info(
            "Swing order placed: %s x%d @ %.2f | stop %.2f",
            signal["symbol"], qty, limit_price, signal["stop_loss"],
        )
        return order_row
    except Exception as e:
        logger.error("submit_swing_order error for %s: %s", signal["symbol"], e)
        return None


# ─── Stop-Loss Monitor ────────────────────────────────────────────────────────

def check_swing_stops(trading_client: TradingClient, data_client: StockHistoricalDataClient):
    """Check each open swing position; market-sell if current price ≤ stop."""
    for pos in fetch_open_positions("swing_strategy"):
        symbol = pos["symbol"]
        stop   = float(pos["stop_loss"])
        pos_id = pos["id"]

        daily = fetch_daily_bars(data_client, symbol)
        if daily.empty:
            continue

        current = float(daily["close"].iloc[-1])
        if current <= stop:
            logger.info("STOP triggered: %s price=%.2f stop=%.2f", symbol, current, stop)
            try:
                trading_client.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=int(pos["qty"]),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                pnl = (current - float(pos["entry_price"])) * float(pos["qty"])
                close_position(pos_id, current, pnl)
                insert("fills", {
                    "strategy":     "swing_strategy",
                    "symbol":       symbol,
                    "filled_qty":   pos["qty"],
                    "filled_price": current,
                    "side":         "sell",
                    "meta":         {"reason": "stop_loss"},
                })
            except Exception as e:
                logger.error("stop sell error for %s: %s", symbol, e)


# ─── Main Entry ───────────────────────────────────────────────────────────────

def run_swing(trading_client: TradingClient, data_client: StockHistoricalDataClient):
    logger.info("=== Swing strategy run started ===")
    init_db()

    account  = trading_client.get_account()
    equity   = float(getattr(account, "equity", 100_000.0) or 100_000.0)

    open_pos         = fetch_open_positions("swing_strategy")
    occupied_symbols = {p["symbol"] for p in open_pos}
    slots_available  = MAX_POSITIONS - len(open_pos)

    check_swing_stops(trading_client, data_client)

    if slots_available <= 0:
        logger.info("Max positions reached (%d), skipping scan", MAX_POSITIONS)
        return

    signals_placed = 0
    for symbol in WATCHLIST:
        if signals_placed >= slots_available:
            break
        if symbol in occupied_symbols:
            continue
        if earnings_within_days(symbol, EARNINGS_GUARD):
            logger.info("Skipping %s — earnings within %d days", symbol, EARNINGS_GUARD)
            continue

        daily  = fetch_daily_bars(data_client, symbol)
        intra  = fetch_30min_bars(data_client, symbol)
        signal = compute_signal(daily, intra, symbol)

        if signal:
            insert("signals", {**signal, "acted": 0})
            order = submit_swing_order(trading_client, signal, equity)
            if order:
                insert("signals", {**signal, "acted": 1})
                signals_placed += 1

    logger.info("=== Swing strategy run complete: %d signals acted on ===", signals_placed)
