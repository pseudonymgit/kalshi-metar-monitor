#!/usr/bin/env python3
"""SBOX paper trade settlement closer.

Calls the engine's process_settlements_for_date for recent trading dates to
close open SBOX paper trades by matching against Kalshi settlement data.

R/T (2026-10-01): this must target data/paper_trading_sbox.db (the SBOX book),
NOT the default data/paper_trading.db (empty). Earlier runs returned 0 because
they read the wrong database.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.paper_trading_engine import PaperTrader
from datetime import datetime, timezone, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SBOX_DB = os.path.join(REPO, "data", "paper_trading_sbox.db")

TRADER = PaperTrader(paper_db=SBOX_DB)

today = datetime.now(timezone.utc).date()
# Settle recent trading dates
for days_ago in range(1, 12):
    d = today - timedelta(days=days_ago)
    date_str = d.isoformat()
    try:
        TRADER.process_settlements_for_date(date_str)
    except Exception as e:
        logger.warning("%s: %s", date_str, e)

if __name__ == "__main__":
    pass