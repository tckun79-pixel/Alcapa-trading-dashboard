"""
Entry point: run both strategies in sequence.
Usage: python run_scheduler.py
"""

import logging
import os
import time
from datetime import datetime

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from scheduler.db import init_db
from scheduler.swing_strategy import run_swing
from scheduler.options_strategy import run_options

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("run_scheduler")

API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
PAPER      = os.getenv("APCA_API_PAPER", "true").lower() == "true"

if not API_KEY or not API_SECRET:
    raise RuntimeError("APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set in environment.")

trading_client = TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=PAPER)
data_client    = StockHistoricalDataClient(api_key=API_KEY, secret_key=API_SECRET)

init_db()

if __name__ == "__main__":
    logger.info("Scheduler started at %s", datetime.now().isoformat())
    start = time.time()
    try:
        run_swing(trading_client, data_client)
        run_options(trading_client)
    except Exception as e:
        logger.exception("Scheduler error: %s", e)
    finally:
        logger.info("Scheduler completed in %.1fs", time.time() - start)
