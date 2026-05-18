"""
Options (Wheel) Strategy — cash-secured put and covered call executor.

Current behavior:
- If Alpaca options_buying_power <= 0, strategy enters SIGNAL-ONLY mode.
- In signal-only mode, valid CSP/CC candidates are still scanned and saved to SQLite,
  but no live option order is submitted.
- This avoids repeated broker rejections on paper accounts where options approval exists
  but options_buying_power remains zero.
"""

import logging
from datetime import date, timedelta
from typing import Optional, List

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus

from .db import init_db, insert, fetch_open_options, close_option_position
from .utils import earnings_within_days, bs_delta, flatten_yf

logger = logging.getLogger("options_strategy")

# ─── Config ───────────────────────────────────────────────────────────────────
WHEEL_UNIVERSE    = ["AAPL", "MSFT", "NVDA", "AMD", "META", "TSLA", "AMZN"]

DTE_MIN               = 20
DTE_MAX               = 45
DELTA_MIN             = 0.15
DELTA_MAX             = 0.30
MIN_OPEN_INTEREST     = 100
MIN_BID               = 0.10
MAX_ALLOC_PCT         = 0.40
MAX_SINGLE_NOTIONAL   = 60_000
EARNINGS_GUARD        = 7
RISK_FREE_RATE        = 0.053
DEFAULT_IV            = 0.30


# ─── Account helpers ──────────────────────────────────────────────────────────

def get_account_equity(trading_client: TradingClient) -> float:
    try:
        account = trading_client.get_account()
        return float(getattr(account, "equity", 100_000.0) or 100_000.0)
    except Exception:
        return 100_000.0


def get_buying_power(trading_client: TradingClient) -> float:
    """
    General cash availability proxy for pre-checks.
    For paper accounts, cash may be populated while options_buying_power remains 0.
    """
    try:
        account = trading_client.get_account()
        for field in ["cash", "options_buying_power", "regt_buying_power", "buying_power"]:
            val = getattr(account, field, None)
            if val is not None:
                fval = float(val)
                if fval > 0:
                    return fval
        return 0.0
    except Exception as e:
        logger.warning("get_buying_power error: %s", e)
        return 0.0


def get_options_buying_power(trading_client: TradingClient) -> float:
    """
    Broker-enforced options buying power.
    Alpaca uses this for CSP validation.
    """
    try:
        account = trading_client.get_account()
        return float(getattr(account, "options_buying_power", 0) or 0)
    except Exception as e:
        logger.warning("get_options_buying_power error: %s", e)
        return 0.0


def get_position_qty(trading_client: TradingClient, symbol: str) -> int:
    try:
        for p in trading_client.get_all_positions() or []:
            if p.symbol == symbol:
                return int(float(p.qty))
    except Exception:
        pass
    return 0


def get_underlying_price(trading_client: TradingClient, symbol: str) -> Optional[float]:
    try:
        for p in trading_client.get_all_positions() or []:
            if p.symbol == symbol:
                return float(p.current_price)
    except Exception:
        pass

    try:
        raw = yf.download(
            symbol,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        df = flatten_yf(raw)
        if not df.empty and "close" in df.columns:
            return float(df["close"].iloc[-1])
    except Exception as e:
        logger.warning("get_underlying_price yfinance error for %s: %s", symbol, e)

    return None


# ─── Allocation guard ─────────────────────────────────────────────────────────

def allocation_exceeded(
    trading_client: TradingClient,
    underlying: str,
    new_notional: float,
) -> bool:
    """
    Returns True if:
      - new_notional alone exceeds MAX_SINGLE_NOTIONAL hard cap, OR
      - adding new_notional would exceed MAX_ALLOC_PCT of account equity.
    """
    if new_notional > MAX_SINGLE_NOTIONAL:
        logger.info(
            "Allocation hard cap: notional $%.0f exceeds single limit $%.0f",
            new_notional, MAX_SINGLE_NOTIONAL,
        )
        return True

    equity = get_account_equity(trading_client)
    max_notional = equity * MAX_ALLOC_PCT
    existing = fetch_open_options(underlying)
    existing_notional = sum(
        float(p["strike"] or 0) * 100 * abs(float(p["qty"] or 0))
        for p in existing
    )

    total = existing_notional + new_notional
    if total > max_notional:
        logger.info(
            "Allocation pct cap: total $%.0f > %.0f%% of equity $%.0f",
            total, MAX_ALLOC_PCT * 100, equity,
        )
        return True

    return False


# ─── Theoretical premium (Black-Scholes) ─────────────────────────────────────

def bs_premium(
    S: float, K: float, T: float, r: float, sigma: float, is_put: bool
) -> float:
    try:
        if T <= 0 or sigma <= 0:
            return 0.05
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if is_put:
            price = (K * np.exp(-r * T) * norm.cdf(-d2)) - (S * norm.cdf(-d1))
        else:
            price = (S * norm.cdf(d1)) - (K * np.exp(-r * T) * norm.cdf(d2))
        return max(0.05, round(float(price), 2))
    except Exception:
        return 0.05


# ─── Contract selection ───────────────────────────────────────────────────────

def filter_contracts(
    trading_client: TradingClient,
    underlying: str,
    contract_type: ContractType,
    underlying_price: float,
) -> List[dict]:
    """
    Fetch option contracts filtered by DTE and delta.
    If bid/open_interest are unavailable on paper, skip hard filtering on them.
    """
    exp_gte = date.today() + timedelta(days=DTE_MIN)
    exp_lte = date.today() + timedelta(days=DTE_MAX)

    req = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        expiration_date_gte=exp_gte,
        expiration_date_lte=exp_lte,
        type=contract_type,
        status=AssetStatus.ACTIVE,
    )

    try:
        result = trading_client.get_option_contracts(req)
        contracts = result.option_contracts if hasattr(result, "option_contracts") else []
    except Exception as e:
        logger.warning("get_option_contracts error for %s: %s", underlying, e)
        return []

    valid = []
    is_put = (contract_type == ContractType.PUT)

    for c in contracts:
        try:
            strike = float(getattr(c, "strike_price", 0) or 0)
            expiry = getattr(c, "expiration_date", None)
            if strike <= 0 or expiry is None:
                continue

            dte = (pd.Timestamp(str(expiry)).date() - date.today()).days
            if not (DTE_MIN <= dte <= DTE_MAX):
                continue

            T = max(dte / 365.0, 0.0001)

            raw_bid = getattr(c, "bid_price", None)
            if raw_bid is not None:
                bid = float(raw_bid)
                if bid < MIN_BID:
                    continue
            else:
                bid = None

            raw_oi = getattr(c, "open_interest", None)
            if raw_oi is not None:
                oi = int(raw_oi)
                if oi < MIN_OPEN_INTEREST:
                    continue
            else:
                oi = 0

            if bid and bid > 0:
                sigma = max(0.05, min((bid / (underlying_price * (T ** 0.5))) * 1.25, 2.0))
            else:
                sigma = DEFAULT_IV

            delta = bs_delta(underlying_price, strike, T, RISK_FREE_RATE, sigma, is_put)
            abs_delta = abs(delta)

            if not (DELTA_MIN <= abs_delta <= DELTA_MAX):
                continue

            if bid is None:
                bid = bs_premium(underlying_price, strike, T, RISK_FREE_RATE, sigma, is_put)

            valid.append({
                "contract": c,
                "symbol": getattr(c, "symbol", ""),
                "strike": strike,
                "expiry": str(expiry),
                "dte": dte,
                "bid": bid,
                "oi": oi,
                "delta": delta,
                "abs_delta": abs_delta,
                "sigma": sigma,
                "bid_is_real": (raw_bid is not None),
            })

        except Exception as e:
            logger.debug("Contract parse error: %s", e)
            continue

    target = (DELTA_MIN + DELTA_MAX) / 2.0
    valid.sort(key=lambda x: abs(x["abs_delta"] - target))

    logger.info(
        "%s %s: %d/%d contracts passed filter",
        underlying,
        "PUT" if is_put else "CALL",
        len(valid),
        len(contracts),
    )
    return valid


# ─── Signal persistence ───────────────────────────────────────────────────────

def save_option_signal(underlying: str, side_label: str, best: dict, signal_only: bool):
    insert("signals", {
        "strategy": "options_strategy",
        "symbol": underlying,
        "signal_type": side_label,
        "price": best["bid"],
        "qty": 1,
        "meta": {
            "contract_symbol": best["symbol"],
            "strike": best["strike"],
            "expiry": best["expiry"],
            "dte": best["dte"],
            "delta": best["delta"],
            "sigma": best["sigma"],
            "bid_is_real": best["bid_is_real"],
            "mode": "signal_only" if signal_only else "live",
        },
        "acted": 0 if signal_only else 1,
    })


# ─── CSP ──────────────────────────────────────────────────────────────────────

def run_csp(trading_client: TradingClient, underlying: str, equity: float, signal_only: bool = False):
    if earnings_within_days(underlying, EARNINGS_GUARD):
        logger.info("CSP skip %s — earnings within %d days", underlying, EARNINGS_GUARD)
        return

    existing = fetch_open_options(underlying)
    if any(p["contract_type"] == "put" for p in existing):
        logger.info("CSP skip %s — already have open CSP", underlying)
        return

    price = get_underlying_price(trading_client, underlying)
    if not price:
        logger.warning("CSP skip %s — could not determine underlying price", underlying)
        return

    contracts = filter_contracts(trading_client, underlying, ContractType.PUT, price)
    if not contracts:
        logger.info("CSP skip %s — no valid contracts after filter", underlying)
        return

    best = contracts[0]
    notional = best["strike"] * 100
    cash_proxy = get_buying_power(trading_client)

    if cash_proxy < notional:
        logger.info(
            "CSP skip %s — insufficient cash proxy (need $%.0f have $%.0f)",
            underlying, notional, cash_proxy,
        )
        return

    if allocation_exceeded(trading_client, underlying, notional):
        logger.info("CSP skip %s — allocation limit exceeded", underlying)
        return

    save_option_signal(underlying, "cash_secured_put", best, signal_only=signal_only)

    if signal_only:
        logger.warning(
            "CSP signal-only %s: strike=%.2f exp=%s dte=%d delta=%.2f premium=%.2f",
            underlying, best["strike"], best["expiry"], best["dte"], best["delta"], best["bid"]
        )
        return

    limit_price = round(best["bid"], 2)
    contract_symbol = best["symbol"]
    bid_source = "market" if best["bid_is_real"] else "theoretical"

    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=contract_symbol,
            qty=1,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        ))

        insert("orders", {
            "strategy": "options_strategy",
            "symbol": contract_symbol,
            "alpaca_order_id": str(order.id),
            "side": "sell",
            "qty": 1,
            "order_type": "limit",
            "limit_price": limit_price,
            "status": str(order.status),
            "meta": {
                "underlying": underlying,
                "type": "csp",
                "strike": best["strike"],
                "expiry": best["expiry"],
                "dte": best["dte"],
                "delta": best["delta"],
                "sigma": best["sigma"],
                "bid_source": bid_source,
            },
        })

        insert("options_positions", {
            "symbol": contract_symbol,
            "underlying": underlying,
            "contract_type": "put",
            "strike": best["strike"],
            "expiry": best["expiry"],
            "qty": -1,
            "premium": limit_price,
            "status": "open",
            "meta": {"bid_source": bid_source},
        })

        logger.info(
            "CSP opened: %s strike=%.2f exp=%s dte=%d delta=%.2f premium=%.2f (%s bid)",
            underlying, best["strike"], best["expiry"], best["dte"],
            best["delta"], limit_price, bid_source,
        )

    except Exception as e:
        err = str(e)
        if "insufficient options buying power" in err:
            logger.error(
                "CSP blocked by broker for %s — options_buying_power insufficient: %s",
                underlying, err
            )
        else:
            logger.error("CSP order error for %s: %s", underlying, e)


# ─── Covered calls ────────────────────────────────────────────────────────────

def run_covered_call(trading_client: TradingClient, underlying: str, equity: float, signal_only: bool = False):
    if earnings_within_days(underlying, EARNINGS_GUARD):
        logger.info("CC skip %s — earnings within %d days", underlying, EARNINGS_GUARD)
        return

    qty = get_position_qty(trading_client, underlying)
    if qty < 100:
        logger.info("CC skip %s — only %d shares held (need 100)", underlying, qty)
        return

    existing = fetch_open_options(underlying)
    if any(p["contract_type"] == "call" for p in existing):
        logger.info("CC skip %s — already have open covered call", underlying)
        return

    price = get_underlying_price(trading_client, underlying)
    if not price:
        logger.warning("CC skip %s — could not determine underlying price", underlying)
        return

    contracts = filter_contracts(trading_client, underlying, ContractType.CALL, price)
    if not contracts:
        logger.info("CC skip %s — no valid contracts after filter", underlying)
        return

    best = contracts[0]
    save_option_signal(underlying, "covered_call", best, signal_only=signal_only)

    if signal_only:
        logger.warning(
            "CC signal-only %s: strike=%.2f exp=%s dte=%d delta=%.2f premium=%.2f",
            underlying, best["strike"], best["expiry"], best["dte"], best["delta"], best["bid"]
        )
        return

    limit_price = round(best["bid"], 2)
    contract_symbol = best["symbol"]
    bid_source = "market" if best["bid_is_real"] else "theoretical"

    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=contract_symbol,
            qty=1,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        ))

        insert("orders", {
            "strategy": "options_strategy",
            "symbol": contract_symbol,
            "alpaca_order_id": str(order.id),
            "side": "sell",
            "qty": 1,
            "order_type": "limit",
            "limit_price": limit_price,
            "status": str(order.status),
            "meta": {
                "underlying": underlying,
                "type": "covered_call",
                "strike": best["strike"],
                "expiry": best["expiry"],
                "dte": best["dte"],
                "delta": best["delta"],
                "sigma": best["sigma"],
                "bid_source": bid_source,
            },
        })

        insert("options_positions", {
            "symbol": contract_symbol,
            "underlying": underlying,
            "contract_type": "call",
            "strike": best["strike"],
            "expiry": best["expiry"],
            "qty": -1,
            "premium": limit_price,
            "status": "open",
            "meta": {"bid_source": bid_source},
        })

        logger.info(
            "Covered call opened: %s strike=%.2f exp=%s dte=%d delta=%.2f premium=%.2f (%s bid)",
            underlying, best["strike"], best["expiry"], best["dte"],
            best["delta"], limit_price, bid_source,
        )

    except Exception as e:
        logger.error("Covered call order error for %s: %s", underlying, e)


# ─── Assignment handler ───────────────────────────────────────────────────────

def check_assignments(trading_client: TradingClient):
    for pos in fetch_open_options():
        if pos["contract_type"] != "put":
            continue

        expiry = pd.Timestamp(str(pos["expiry"])).date()
        if expiry > date.today():
            continue

        underlying = pos["underlying"]
        qty = get_position_qty(trading_client, underlying)
        premium = float(pos["premium"] or 0)

        if qty >= 100:
            logger.info("Assignment detected: %s — %d shares now held", underlying, qty)
            close_option_position(
                pos_id=pos["id"],
                close_premium=0.0,
                pnl=-(float(pos["strike"] or 0) * 100 - premium * 100),
                assignment=1,
            )
        else:
            logger.info("CSP expired OTM: %s — retaining premium $%.2f", underlying, premium * 100)
            close_option_position(
                pos_id=pos["id"],
                close_premium=0.0,
                pnl=premium * 100,
                assignment=0,
            )


# ─── Main entry ───────────────────────────────────────────────────────────────

def run_options(trading_client: TradingClient):
    logger.info("=== Options strategy run started ===")
    init_db()

    equity = get_account_equity(trading_client)
    obp = get_options_buying_power(trading_client)

    logger.info("Account equity: $%.2f", equity)
    logger.info("Options buying power: $%.2f", obp)

    signal_only = obp <= 0
    if signal_only:
        logger.warning(
            "Signal-only mode enabled — options_buying_power is 0, so live option orders are disabled"
        )

    check_assignments(trading_client)

    for underlying in WHEEL_UNIVERSE:
        run_csp(trading_client, underlying, equity, signal_only=signal_only)
        run_covered_call(trading_client, underlying, equity, signal_only=signal_only)

    logger.info("=== Options strategy run complete ===")
