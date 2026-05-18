"""
Shared helpers: technical indicators, earnings check, position sizing.
"""

import logging
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger("utils")


# ─── yfinance DataFrame normaliser ───────────────────────────────────────────

def flatten_yf(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns returned by yfinance ≥1.0 single-symbol downloads."""
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


# ─── Technical Indicators ────────────────────────────────────────────────────

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).max()


# ─── Earnings Guard ──────────────────────────────────────────────────────────

def earnings_within_days(symbol: str, days: int = 7) -> bool:
    """
    Returns True if the symbol has an earnings date within the next `days`
    calendar days. Uses yfinance. Returns False on any error to avoid blocking trades.
    yfinance ≥1.0 returns calendar as a plain dict, not a DataFrame.
    """
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar

        if not cal:
            return False

        # yfinance ≥1.0 returns a dict; keys may be "Earnings Date" or "earningsDate"
        raw = cal.get("Earnings Date") or cal.get("earningsDate")
        if not raw:
            return False

        if not isinstance(raw, list):
            raw = [raw]

        today  = pd.Timestamp.now(tz="UTC").normalize()
        cutoff = today + pd.Timedelta(days=days)

        for d in raw:
            d = pd.Timestamp(d)
            if d.tzinfo is None:
                d = d.tz_localize("UTC")
            if today <= d <= cutoff:
                logger.info("%s has earnings within %d days on %s", symbol, days, d.date())
                return True
        return False

    except Exception as e:
        logger.warning("earnings_within_days error for %s: %s", symbol, e)
        return False


# ─── Position Sizing ─────────────────────────────────────────────────────────

def position_size_by_risk(equity: float, risk_pct: float, entry: float, stop: float) -> int:
    """
    Returns integer share count where dollar risk = equity * risk_pct.
    Returns 0 if stop >= entry (invalid setup).
    """
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    dollar_risk = equity * risk_pct
    return max(1, int(dollar_risk / risk_per_share))


# ─── Black-Scholes Delta ─────────────────────────────────────────────────────

def bs_delta(S: float, K: float, T: float, r: float, sigma: float, is_put: bool = False) -> float:
    """
    Approximate Black-Scholes delta.
    T = time to expiry in years, sigma = annualised IV (decimal), r = risk-free rate.
    """
    try:
        from scipy.stats import norm
        if T <= 0 or sigma <= 0:
            return 0.0
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        delta = norm.cdf(d1) if not is_put else norm.cdf(d1) - 1
        return round(float(delta), 4)
    except Exception:
        return 0.0
