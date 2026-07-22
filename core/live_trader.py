"""Live Trader Module

Live trading-specific code paths.
- Real fills with actual money
- Risk controls active (kill switch, position limits, stop loss)
- Real alerting (Discord, SMS)
- No simulated quantities

Extracted during Phase 20.2 mode separation.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .trading_modes import TradingMode, TradingModeConfig, get_config_for_mode

_LOGGER = logging.getLogger(__name__)


class LiveTrader:
    """Live trading engine - real fills, risk controls active, alerting enabled."""

    def __init__(self, config: Optional[TradingModeConfig] = None, **kwargs):
        self.config = config or get_config_for_mode(TradingMode.LIVE, **kwargs)
        self.db_path = self.config.db_path
        self.trade_version = self.config.trade_version
        _LOGGER.info(
            f"LiveTrader initialized: version={self.trade_version}, db={self.db_path}"
        )

    def execute_real_trade(self, station: str, market_type: str, direction: str,
                           quantity: int, price: float) -> Dict[str, Any]:
        """Execute a real trade on Kalshi.
        
        Validates risk controls first, then places the trade.
        """
        # Validate kill switch status before trading
        kill_switch_active, reasons = self._check_kill_switches()
        if kill_switch_active:
            _LOGGER.error(f"Kill switch active - trade blocked: {reasons}")
            return {"success": False, "error": f"Kill switch active: {reasons}"}

        # Check risk limits
        risk_ok, risk_msg = self._check_risk_limits(station, market_type, quantity, price)
        if not risk_ok:
            _LOGGER.error(f"Risk limit breached: {risk_msg}")
            return {"success": False, "error": risk_msg}

        # Place the real trade via Kalshi API
        try:
            from .order_manager import process_ladder_transition
            return process_ladder_transition(station, direction, quantity, price)
        except ImportError:
            _LOGGER.error("order_manager not available for live trading")
            return {"success": False, "error": "order_manager not available"}

    def _check_kill_switches(self) -> Tuple[bool, List[str]]:
        """Check all kill switches for live trading."""
        from .risk_controls import check_kill_switches as _check
        return _check(self.db_path, {})

    def _check_risk_limits(self, station: str, market_type: str,
                           quantity: int, price: float) -> Tuple[bool, str]:
        """Check risk limits for a proposed trade."""
        from .position_sizing import validate_trade_size
        return validate_trade_size(station, market_type, quantity, price)

    def send_live_alert(self, alert_data: Dict[str, Any]) -> bool:
        """Send a live trading alert."""
        try:
            from .alert_dispatcher import dispatch_current_alert
            return dispatch_current_alert(alert_data.get("payload", {}))
        except Exception as e:
            _LOGGER.error(f"Alert dispatch failed: {e}")
            return False

    def get_live_positions(self) -> List[Dict[str, Any]]:
        """Get current live positions."""
        conn = get_sqlite_connection(self.db_path)
        try:
            c = conn.cursor()
            c.execute("SELECT * FROM positions WHERE is_open = 1")
            return [dict(row) for row in c.fetchall()]
        finally:
            conn.close()


__all__ = [
    "LiveTrader",
]