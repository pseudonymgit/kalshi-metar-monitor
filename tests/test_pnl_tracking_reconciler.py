"""Regression test — PnL tracking settlement-column migration binding.

R/T (2026-10-02): `core/pnl_tracking.py::daily_reconciliation` called
`self._migrate_settlement_columns(reconcile_date)`, but that helper is a
module-level function that is NEVER bound to PaperTrader. Every reconcile
therefore raised `AttributeError: 'PaperTrader' object has no attribute
'_migrate_settlement_columns'` (caught + logged, so it regressed silently).

This test locks the fix: the migration call must resolve against the module
function, and daily_reconciliation must run without that AttributeError.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import pnl_tracking  # noqa: E402


@pytest.mark.unit
class TestSettlementColumnMigrationBinding:
    def test_migrate_helper_is_module_level_callable(self):
        """The helper must be importable/callable as a module function."""
        assert hasattr(pnl_tracking, "_migrate_settlement_columns")
        assert callable(pnl_tracking._migrate_settlement_columns)

    def test_migrate_helper_is_not_a_papertrader_method(self):
        """It is NOT a bound method — callers must not use `self.` for it.

        If this ever becomes a real method, update daily_reconciliation too;
        until then, `self._migrate_settlement_columns` must not be relied on.
        """
        from core.paper_trading_engine import PaperTrader

        assert not hasattr(PaperTrader, "_migrate_settlement_columns")

    def test_daily_reconciliation_does_not_raise_attribute_error(self, tmp_path, monkeypatch):
        """End-to-end guard: daily_reconciliation must not raise the old
        AttributeError and must create the settlement columns.

        The settlement + mark-to-market steps are stubbed so the reconciler
        reaches the migration call site without needing external market data —
        the regression under test is purely the helper binding.
        """
        from core.paper_trading_engine import PaperTrader

        paper_db = str(tmp_path / "paper_trading_test.db")
        # Keep the engine off the production data dir; use an empty METAR path.
        metar_db = str(tmp_path / "metar_test.db")
        monkeypatch.setenv("OPENCLAW_TESTS_ALLOW_PROD_WRITE", "1")  # trade_journal init

        # Capture log records so we can assert the exact failure never returns.
        import logging

        records = []

        class _Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("core.paper_trading_engine")
        logger.addHandler(handler)
        try:
            trader = PaperTrader(paper_db=paper_db, metar_db=metar_db, initial_balance=200.0)
            # Isolate the regression: skip data-dependent pre-steps so control
            # definitely reaches the _migrate_settlement_columns call site.
            trader.process_settlements_for_date = lambda *a, **k: None
            trader.mark_positions_to_market = lambda *a, **k: None
            # Directly exercise the reconciler that used to blow up.
            trader.daily_reconciliation("2026-10-02")
        finally:
            logger.removeHandler(handler)

        # No AttributeError about the migration helper in any log record.
        bad = [r for r in records if "_migrate_settlement_columns" in str(r.getMessage())]
        assert not bad, f"unexpected migration binding error: {[r.getMessage() for r in bad]}"

        # The migration must actually have run: settlement columns exist.
        conn = sqlite3.connect(paper_db)
        try:
            trade_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
            bal_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_balances)")}
        finally:
            conn.close()
        assert "settlement_confirmed" in trade_cols
        assert "settlement_outcome" in trade_cols
        assert "paper_pnl" in bal_cols
        assert "settlement_confirmed_pnl" in bal_cols
