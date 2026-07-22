"""Trading Modes Module

Separates paper trading from live trading code paths.
- Paper: simulation mode, fake fills, no real money
- Live: real fills, risk controls active, kill switch, alerting
- Shared: signal pipeline, DB schemas, utility functions

Extracted during Phase 20.2 mode separation.
"""
from enum import Enum
from typing import Optional, Dict, Any
import logging

_LOGGER = logging.getLogger(__name__)


class TradingMode(Enum):
    """Trading mode enum."""
    PAPER = "paper"
    LIVE = "live"


class TradingModeConfig:
    """Configuration for a specific trading mode.
    
    Paper mode: simulation with fake fills, no real money
    Live mode: real fills, risk controls, kill switch, alerts
    """

    def __init__(self, mode: TradingMode, **kwargs):
        self.mode = mode
        self.is_paper = mode == TradingMode.PAPER
        self.is_live = mode == TradingMode.LIVE

        # Paper mode defaults
        if self.is_paper:
            self.initial_balance = kwargs.get("initial_balance", 10000.0)
            self.use_fake_fills = True
            self.risk_controls_enabled = kwargs.get("risk_controls_enabled", False)
            self.kill_switch_enabled = False
            self.alerting_enabled = kwargs.get("alerting_enabled", True)
            self.db_path = kwargs.get("db_path", "data/paper_trading.db")
            self.trade_version = kwargs.get("trade_version", "v2.0_paper")

        # Live mode defaults
        elif self.is_live:
            self.initial_balance = 0.0  # Real money, not simulated
            self.use_fake_fills = False
            self.risk_controls_enabled = True
            self.kill_switch_enabled = True
            self.alerting_enabled = True
            self.db_path = kwargs.get("db_path", "data/live_trading.db")
            self.trade_version = kwargs.get("trade_version", "v2.0_live")

    @classmethod
    def paper(cls, **kwargs) -> "TradingModeConfig":
        """Create a paper trading config."""
        return cls(TradingMode.PAPER, **kwargs)

    @classmethod
    def live(cls, **kwargs) -> "TradingModeConfig":
        """Create a live trading config."""
        return cls(TradingMode.LIVE, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "is_paper": self.is_paper,
            "is_live": self.is_live,
            "initial_balance": self.initial_balance,
            "use_fake_fills": self.use_fake_fills,
            "risk_controls_enabled": self.risk_controls_enabled,
            "kill_switch_enabled": self.kill_switch_enabled,
            "alerting_enabled": self.alerting_enabled,
            "db_path": self.db_path,
            "trade_version": self.trade_version,
        }


def get_mode_from_env() -> TradingMode:
    """Get trading mode from environment variable."""
    import os
    mode_str = os.getenv("TRADING_MODE", "paper").strip().lower()
    if mode_str in ("live", "production", "prod"):
        return TradingMode.LIVE
    return TradingMode.PAPER


def get_config_for_mode(mode: Optional[TradingMode] = None, **kwargs) -> TradingModeConfig:
    """Get the appropriate config for the given or environment mode."""
    if mode is None:
        mode = get_mode_from_env()
    return TradingModeConfig(mode, **kwargs)


__all__ = [
    "TradingMode", "TradingModeConfig", "get_mode_from_env", "get_config_for_mode",
]