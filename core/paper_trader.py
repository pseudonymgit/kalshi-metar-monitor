"""Paper Trader Module

Paper trading-specific code paths.
- Simulation mode with fake fills
- No real money involved
- P&L tracking for paper positions
- Version-tagged analytics

Extracted during Phase 20.2 mode separation.
"""
from core.structured_logger import get_logger
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .trading_modes import TradingMode, TradingModeConfig, get_config_for_mode
from .trade_execution import TradeType, MarketSide, place_paper_trade, mark_positions_to_market
from .pnl_tracking import (
    process_settlements_for_date, daily_reconciliation,
    calculate_calibration_metrics_for_date, get_current_balance,
    get_version_performance, generate_calibration_report, compute_sharpe,
)

_LOGGER = get_logger(__name__)


class PaperTrader:
    """Paper trading engine - simulates trading with fake fills and no real money."""

    def __init__(self, config: Optional[TradingModeConfig] = None, **kwargs):
        self.config = config or get_config_for_mode(TradingMode.PAPER, **kwargs)
        self.db_path = self.config.db_path
        self.initial_balance = self.config.initial_balance
        self.balance = self.initial_balance
        self.trade_version = self.config.trade_version
        _LOGGER.info(
            f"PaperTrader initialized: version={self.trade_version}, "
            f"balance={self.initial_balance}, db={self.db_path}"
        )

    def execute_trade(self, station: str, market_type: str, signal_direction: Any,
                      trade_version: str, functionality: str, date: Optional[str] = None,
                      notes: str = "") -> Optional[Dict[str, Any]]:
        """Execute a paper trade (simulated, no real money)."""
        return place_paper_trade(
            self, station, market_type, signal_direction,
            trade_version, functionality, date, notes,
        )

    def process_settlements(self, settlement_date: str) -> Dict[str, Any]:
        """Process settlements for paper positions."""
        return process_settlements_for_date(self, settlement_date)

    def reconcile(self, reconcile_date: Optional[str] = None) -> Dict[str, Any]:
        """Run daily reconciliation for paper trading."""
        return daily_reconciliation(self, reconcile_date)

    def calculate_metrics(self, for_date: str) -> Dict[str, Any]:
        """Calculate calibration metrics for paper trading."""
        return calculate_calibration_metrics_for_date(self, for_date)

    def get_balance(self, for_date: Optional[str] = None) -> float:
        """Get current paper trading balance."""
        return get_current_balance(self, for_date)

    def get_performance(self, trade_version: str) -> Dict[str, Any]:
        """Get version-tagged performance metrics."""
        return get_version_performance(self, trade_version)

    def generate_report(self, save_path: str) -> Dict[str, Any]:
        """Generate calibration report."""
        return generate_calibration_report(self, save_path)

    def get_sharpe(self, trades: Optional[List[Dict[str, Any]]] = None) -> float:
        """Compute Sharpe ratio for paper trades."""
        return compute_sharpe(self, trades)


def daily_paper_run(run_date: Optional[str] = None) -> Dict[str, Any]:
    """Execute the daily paper trading run."""
    from .paper_trading_engine import daily_paper_run as _original_run
    return _original_run(run_date)


__all__ = [
    "PaperTrader", "daily_paper_run",
]