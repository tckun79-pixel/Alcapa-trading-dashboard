"""
SQLite schema and helper functions for strategy persistence.
"""

import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "alcapa.db"
logger = logging.getLogger("db")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            strategy    TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            price       REAL,
            atr         REAL,
            stop_loss   REAL,
            qty         INTEGER,
            meta        TEXT,
            acted       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            strategy        TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            alpaca_order_id TEXT,
            side            TEXT,
            qty             REAL,
            order_type      TEXT,
            limit_price     REAL,
            status          TEXT,
            meta            TEXT
        );

        CREATE TABLE IF NOT EXISTS fills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            strategy        TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            alpaca_order_id TEXT,
            filled_qty      REAL,
            filled_price    REAL,
            side            TEXT,
            meta            TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_open         TEXT NOT NULL,
            ts_close        TEXT,
            strategy        TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            qty             REAL,
            entry_price     REAL,
            exit_price      REAL,
            stop_loss       REAL,
            pnl             REAL,
            status          TEXT DEFAULT 'open',
            meta            TEXT
        );

        CREATE TABLE IF NOT EXISTS options_positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_open         TEXT NOT NULL,
            ts_close        TEXT,
            symbol          TEXT NOT NULL,
            underlying      TEXT NOT NULL,
            contract_type   TEXT NOT NULL,
            strike          REAL,
            expiry          TEXT,
            qty             REAL,
            premium         REAL,
            close_premium   REAL,
            pnl             REAL,
            status          TEXT DEFAULT 'open',
            assignment      INTEGER DEFAULT 0,
            meta            TEXT
        );
        """)
    logger.info("DB initialised at %s", DB_PATH)


# Tables that use ts_open instead of ts
_TS_OPEN_TABLES = {"options_positions", "positions"}

def insert(table: str, row: Dict[str, Any]):
    if table in _TS_OPEN_TABLES:
        row.setdefault("ts_open", datetime.utcnow().isoformat())
    else:
        row.setdefault("ts", datetime.utcnow().isoformat())
    if "meta" in row and isinstance(row["meta"], dict):
        row["meta"] = json.dumps(row["meta"])
    cols = ", ".join(row.keys())
    placeholders = ", ".join(["?"] * len(row))
    with get_conn() as conn:
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(row.values()))


def fetch_open_positions(strategy: Optional[str] = None):
    q = "SELECT * FROM positions WHERE status='open'"
    params = []
    if strategy:
        q += " AND strategy=?"
        params.append(strategy)
    with get_conn() as conn:
        return conn.execute(q, params).fetchall()


def fetch_open_options(underlying: Optional[str] = None):
    q = "SELECT * FROM options_positions WHERE status='open'"
    params = []
    if underlying:
        q += " AND underlying=?"
        params.append(underlying)
    with get_conn() as conn:
        return conn.execute(q, params).fetchall()


def close_position(position_id: int, exit_price: float, pnl: float):
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET status='closed', ts_close=?, exit_price=?, pnl=? WHERE id=?",
            [datetime.utcnow().isoformat(), exit_price, pnl, position_id],
        )


def close_option_position(pos_id: int, close_premium: float, pnl: float, assignment: int = 0):
    with get_conn() as conn:
        conn.execute(
            "UPDATE options_positions SET status='closed', ts_close=?, close_premium=?, pnl=?, assignment=? WHERE id=?",
            [datetime.utcnow().isoformat(), close_premium, pnl, assignment, pos_id],
        )
