#!/usr/bin/env python3
"""
Risk Management Controls - Minimal Implementation (B1.5 Baseline Requirements)

Implements the minimum required risk management interface methods for:
- update_after_trade
- check_daily_loss
- check_drawdown  
- check_consecutive_losses

This is a minimal stub for backtest validation. It currently always passes checks
but maintains the required interface for future risk implementation.

Version: v1.0 (B1.5 baseline)
"""

from datetime import datetime, timezone
from typing import Dict, Any, Union, List
from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    """Risk control configuration parameters."""
    max_daily_loss_percent: float = 0.05  # 5% daily loss limit
    max_drawdown_percent: float = 0.15    # 15% drawdown limit  
    max_consecutive_losses: int = 5       # 5 consecutive losing trades
    initial_capital: float = 10000.0     # Starting capital for percentage calculations


# Default risk config instance
DEFAULT_RISK_CONFIG = RiskConfig()


@dataclass
class TradeResult:
    """Represents the result of a single trade."""
    trade_id: str
    pnl: float  # Profit/Loss in dollars
    is_profitable: bool
    trade_date: str  # ISO date string


@dataclass
class RiskMetrics:
    """Metrics summary for risk reporting."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    drawdown_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    peak_balance: float = 0.0
    current_balance: float = 0.0
    risk_state: 'RiskState' = None  # Reference to risk state (optional)


@dataclass 
class RiskState:
    """Current risk state for a trading session."""
    metrics: RiskMetrics = field(default_factory=RiskMetrics)
    passed_checks: Dict[str, bool] = field(default_factory=dict)
    checks: Dict[str, bool] = field(default_factory=dict)
    
    @property
    def passed(self) -> bool:
        """Return True if all risk checks passed."""
        return all(self.passed_checks.values())
    
    @property
    def ok(self) -> bool:
        """Alias for passed - return True if all risk checks passed."""
        return self.passed
    
    # Legacy compatibility constant
    OK = None  # Will be set below after class definition


# Set OK as an instance for backwards compatibility
RiskState.OK = RiskState(
    passed_checks={"default": True},
    checks={"default": True},
)


@dataclass


class RiskManager:
    """
    Minimal Risk Manager stub that implements the B1.5 baseline requirements.
    
    Methods implemented:
    - update_after_trade: Updates risk state after a trade completes
    - check_daily_loss: Verifies daily loss limits haven't been exceeded
    - check_drawdown: Checks maximum drawdown from peak equity
    - check_consecutive_losses: Monitors losing streaks
    """
    
    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()
        self.current_capital = self.config.initial_capital
        self.peak_capital = self.config.initial_capital
        self.daily_pnl = 0.0
        self.daily_trades: List[TradeResult] = []
        self.consecutive_losses = 0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        
    def update_after_trade(self, trade_result: TradeResult) -> Dict[str, Any]:
        """
        Update risk state after a trade execution or settlement.
        
        Args:
            trade_result: Details of the executed trade
            
        Returns:
            Risk state after update including pass/fail status for each risk check
        """
        self.total_trades += 1
        self.daily_pnl += trade_result.pnl
        self.daily_trades.append(trade_result)
        
        # Update capital and peak tracking
        self.current_capital += trade_result.pnl
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
            
        # Update win/loss counters
        if trade_result.is_profitable:
            self.winning_trades += 1
            self.consecutive_losses = 0  # Reset losing streak
        else:
            self.losing_trades += 1
            self.consecutive_losses += 1  # Increment losing streak
            
        # Return comprehensive risk state
        risk_state = {
            'passed': (
            self.check_daily_loss() and 
            self.check_drawdown() and 
            self.check_consecutive_losses()
        ),
            'capital': self.current_capital,
            'peak_capital': self.peak_capital,
            'drawdown_pct': self._calculate_drawdown_pct(),
            'daily_pnl': self.daily_pnl,
            'daily_loss_pct': abs(self._calculate_daily_loss_pct()),
            'consecutive_losses': self.consecutive_losses,
            'checks': {
                'daily_loss_check': self.check_daily_loss(),
                'drawdown_check': self.check_drawdown(),
                'consecutive_losses_check': self.check_consecutive_losses()
            }
        }
        
        return risk_state
    
    def check_daily_loss(self) -> bool:
        """
        Check if daily loss exceeds configured threshold.
        
        Returns:
            True if within limits, False if exceeded  
        """
        daily_loss_pct = abs(self._calculate_daily_loss_pct())
        return daily_loss_pct <= self.config.max_daily_loss_percent
    
    def check_drawdown(self) -> bool:
        """
        Check if drawdown exceeds maximum allowed level.
        
        Returns:
            True if within limits, False if exceeded
        """
        current_drawdown_pct = self._calculate_drawdown_pct()
        return current_drawdown_pct <= self.config.max_drawdown_percent
    
    def check_consecutive_losses(self) -> bool:
        """
        Check if consecutive losses exceed configured threshold.
        
        Returns:
            True if within limits, False if exceeded
        """
        return self.consecutive_losses <= self.config.max_consecutive_losses
    
    def _calculate_daily_loss_pct(self) -> float:
        """Calculate daily loss percentage relative to initial capital."""
        if self.config.initial_capital == 0:
            return 0.0
        return self.daily_pnl / self.config.initial_capital
    
    def _calculate_drawdown_pct(self) -> float:
        """Calculate current drawdown percentage from peak capital."""
        if self.peak_capital == 0:
            return 0.0
        return (self.peak_capital - self.current_capital) / self.peak_capital
    
    def reset_daily_state(self, date: str = None):
        """Reset daily tracking values."""
        self.daily_pnl = 0.0
        self.daily_trades = []
        self.consecutive_losses = 0


def is_station_approved(station: str, approved_stations: List[str] = None) -> bool:
    """Check if a station is approved for trading."""
    if approved_stations is None:
        return True  # No restrictions
    return station in approved_stations


def get_approved_stations() -> List[str]:
    """Get list of approved stations."""
    # Default: all stations approved
    return []  # Empty list means all stations allowed


def evaluate_risk_state(risk_manager: RiskManager) -> RiskState:
    """Evaluate current risk state from a RiskManager instance."""
    return RiskState(
        metrics=RiskMetrics(
            total_trades=risk_manager.total_trades,
            winning_trades=risk_manager.winning_trades,
            losing_trades=risk_manager.losing_trades,
            total_pnl=risk_manager.current_capital - risk_manager.config.initial_capital,
            daily_pnl=risk_manager.daily_pnl,
            drawdown_pct=risk_manager._calculate_drawdown_pct(),
            max_drawdown_pct=risk_manager.config.max_drawdown_percent,
            consecutive_losses=risk_manager.consecutive_losses,
        ),
        checks={
            "daily_loss": risk_manager.check_daily_loss(),
            "drawdown": risk_manager.check_drawdown(),
            "consecutive_losses": risk_manager.check_consecutive_losses(),
        },
        passed_checks={k: v for k, v in {
            "daily_loss": risk_manager.check_daily_loss(),
            "drawdown": risk_manager.check_drawdown(),
            "consecutive_losses": risk_manager.check_consecutive_losses(),
        }.items()},
    )


def risk_report(risk_manager: RiskManager) -> Dict[str, Any]:
    """Generate a risk report from a RiskManager instance."""
    state = evaluate_risk_state(risk_manager)
    return {
        "passed": state.passed,
        "metrics": {
            "total_trades": state.metrics.total_trades,
            "winning_trades": state.metrics.winning_trades,
            "losing_trades": state.metrics.losing_trades,
            "total_pnl": state.metrics.total_pnl,
            "daily_pnl": state.metrics.daily_pnl,
            "drawdown_pct": state.metrics.drawdown_pct,
            "max_drawdown_pct": state.metrics.max_drawdown_pct,
            "consecutive_losses": state.metrics.consecutive_losses,
        },
        "checks": state.checks,
    }


def format_risk_alert(risk_manager: RiskManager) -> str:
    """Format a risk alert string from a RiskManager instance."""
    state = evaluate_risk_state(risk_manager)
    alerts = []
    
    if not state.checks.get("daily_loss", True):
        alerts.append(f"Daily loss limit exceeded: {state.metrics.daily_pnl:.2f}")
    if not state.checks.get("drawdown", True):
        alerts.append(f"Drawdown limit exceeded: {state.metrics.drawdown_pct:.2%}")
    if not state.checks.get("consecutive_losses", True):
        alerts.append(f"Consecutive loss limit exceeded: {state.metrics.consecutive_losses}")
    
    if alerts:
        return "RISK ALERT: " + ", ".join(alerts)
    return "Risk checks passed"


if __name__ == "__main__":
    # Test basic functionality
    rm = RiskManager()
    
    # Simulate some trades passing through the system
    test_trades = [
        TradeResult("test_1", pnl=100.0, is_profitable=True, trade_date="2024-01-01"),
        TradeResult("test_2", pnl=-50.0, is_profitable=False, trade_date="2024-01-01"),
        TradeResult("test_3", pnl=-75.0, is_profitable=False, trade_date="2024-01-01"),
    ]
    
    for trade in test_trades:
        result = rm.update_after_trade(trade)
        print(f"Trade {trade.trade_id}: {'PASS' if result['passed'] else 'FAIL'}, Drawdown: {result['drawdown_pct']:.2%}, Consecutive Losses: {result['consecutive_losses']}")
    
    print(f"Final checks - Daily Loss: {'PASS' if result['checks']['daily_loss_check'] else 'FAIL'}, Drawdown: {'PASS' if result['checks']['drawdown_check'] else 'FAIL'}, Consecutive: {'PASS' if result['checks']['consecutive_losses_check'] else 'FAIL'}")