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

# D4 (2026-10-02): Dan's real bankroll is $200, not the $10k default.
TRADER = PaperTrader(paper_db=SBOX_DB, initial_balance=200.0, fee_rate=0.0205)

today = datetime.now(timezone.utc).date()
# Settle recent trading dates
for days_ago in range(0, 12):
    d = today - timedelta(days=days_ago)
    date_str = d.isoformat()
    try:
        TRADER.process_settlements_for_date(date_str)
    except Exception as e:
        logger.warning("%s: %s", date_str, e)

# D4 (2026-10-02): refresh daily_balances so the bankroll updates automatically
# on each settlement cycle (previously only process_settlements ran, never the
# reconciler, so daily_balances went stale).
#
# Scope note: reconcile ONLY the current UTC day, chaining from the last
# closing balance. We deliberately do NOT back-reconcile history — the legacy
# v3.0_sbox 9/29-9/30 trades carry pre-ladder sizing and P&L that is not valid
# on Dan's $200 book; re-deriving those days would re-inflate the bankroll.
# Legacy trade records are LEFT INTACT for Dan's decision (flagged, not deleted).
_today_str = today.isoformat()
try:
    TRADER.daily_reconciliation(_today_str)
except Exception as e:
    logger.warning("reconcile %s: %s", _today_str, e)

if __name__ == "__main__":
    pass