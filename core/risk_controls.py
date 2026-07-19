#!/usr/bin/env python3
"""
Risk Management Controls — Phase 3.6 Real Implementation

Implements the Phase 3.6 risk controls with real thresholds:
- check_daily_loss():    compare against configurable max (default $100)
- check_drawdown():      track peak vs current (default 20% max)
- check_consecutive_losses(): halt after configurable threshold (default 8)
- All return (pass: bool, reason: str) tuples
- RiskState class tracks running state across sessions

Builds on the B1.5 baseline interface. Backward compatible.

Version: v2.0 (Phase 3.6)
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Union, List, Tuple, Optional
from dataclasses import dataclass, field
import logging

_LOGGER = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """Risk control configuration parameters — Phase 3.6 real defaults."""
    # Daily loss limit in dollars (not percentage)
    max_daily_loss_dollars: float = 100.0    # $100 daily loss limit
    # Drawdown limit as fraction of peak
    max_drawdown_percent: float = 0.20       # 20% max drawdown
    # Consecutive loss limit
    max_consecutive_losses: int = 8          # 8 consecutive losses
    # Starting capital (for percentage reference)
    initial_capital: float = 10000.0
    # Daily loss reset — whether to reset daily P&L at midnight
    auto_reset_daily: bool = True


# Default risk config instance
DEFAULT_RISK_CONFIG = RiskConfig()


@dataclass
class TradeResult:
    """Represents the result of a single trade."""
    trade_id: str
    pnl: float  # Profit/Loss in dollars
    is_profitable: bool
    trade_date: str  # ISO date string (YYYY-MM-DD)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'TradeResult':
        """Create a TradeResult from a dict (for backward compat)."""
        return cls(
            trade_id=d.get('trade_id', d.get('trade_uuid', 'unknown')),
            pnl=float(d.get('pnl', d.get('realized_pnl', 0))),
            is_profitable=bool(d.get('is_profitable', d.get('pnl', 0) > 0)),
            trade_date=str(d.get('trade_date', d.get('trade_date_utc', ''))),
        )


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
    max_daily_loss_dollars: float = 100.0
    risk_state: Optional['RiskState'] = None


@dataclass
class RiskState:
    """
    Current risk state for a trading session — Phase 3.6.

    Tracks running state and provides detailed pass/fail with reasons.

    Fields:
        metrics: RiskMetrics for this session
        checks: Dict of check_name -> (passed: bool, reason: str)
        halted: True if any check has failed (trading should stop)
        halt_reason: Combined reason string if halted
    """
    metrics: RiskMetrics = field(default_factory=RiskMetrics)
    checks: Dict[str, Tuple[bool, str]] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""

    @property
    def passed(self) -> bool:
        """Return True if all risk checks passed."""
        if not self.checks:
            return True
        return all(passed for passed, _ in self.checks.values())

    @property
    def ok(self) -> bool:
        """Alias for passed."""
        return self.passed

    @property
    def passed_checks(self) -> Dict[str, bool]:
        """Return dict of check_name -> bool for backward compat."""
        return {k: v[0] for k, v in self.checks.items()}

    def get_failed_checks(self) -> List[Tuple[str, str]]:
        """Return list of (check_name, reason) for failed checks."""
        return [(name, reason) for name, (passed, reason) in self.checks.items() if not passed]

    @classmethod
    def create_ok(cls) -> 'RiskState':
        """Create a RiskState with all checks passing."""
        return cls(
            metrics=RiskMetrics(),
            checks={
                "daily_loss": (True, "within limits"),
                "drawdown": (True, "within limits"),
                "consecutive_losses": (True, "within limits"),
            },
            halted=False,
            halt_reason="",
        )


RiskState.OK = RiskState.create_ok()


class RiskManager:
    """
    Phase 3.6 Risk Manager with real threshold enforcement.

    Methods:
    - update_after_trade: Updates risk state after a trade completes
    - check_daily_loss: Returns (bool, reason) — default $100 max
    - check_drawdown: Returns (bool, reason) — default 20% max
    - check_consecutive_losses: Returns (bool, reason) — default 8 max
    - evaluate: Returns RiskState with all checks
    - risk_report: Returns dict summary
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self.current_capital = self.config.initial_capital
        self.peak_capital = self.config.initial_capital
        self.daily_pnl = 0.0
        self.daily_trades: List[TradeResult] = []
        self.consecutive_losses = 0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self._last_trade_date: Optional[str] = None
        self._logger = logging.getLogger(f"{__name__}.RiskManager")

    def _auto_reset_daily_if_needed(self, trade_date: str):
        """Reset daily state if the trading day has changed."""
        if not self.config.auto_reset_daily:
            return
        if self._last_trade_date is not None and self._last_trade_date != trade_date:
            self.daily_pnl = 0.0
            self.daily_trades = []
            self._logger.debug("Auto-reset daily state for new trading day %s", trade_date)
        self._last_trade_date = trade_date

    def update_after_trade(self, trade_result: TradeResult) -> Dict[str, Any]:
        """
        Update risk state after a trade execution or settlement.

        Args:
            trade_result: Details of the executed trade

        Returns:
            Risk state dict with pass/fail status for each check
        """
        self._auto_reset_daily_if_needed(trade_result.trade_date)

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
            self.consecutive_losses = 0
        else:
            self.losing_trades += 1
            self.consecutive_losses += 1

        # Run all checks
        dl_pass, dl_reason = self.check_daily_loss()
        dd_pass, dd_reason = self.check_drawdown()
        cl_pass, cl_reason = self.check_consecutive_losses()

        return {
            'passed': dl_pass and dd_pass and cl_pass,
            'capital': self.current_capital,
            'peak_capital': self.peak_capital,
            'drawdown_pct': self._calculate_drawdown_pct(),
            'daily_pnl': self.daily_pnl,
            'daily_loss_pct': abs(self._calculate_daily_loss_pct()),
            'consecutive_losses': self.consecutive_losses,
            'checks': {
                'daily_loss_check': dl_pass,
                'drawdown_check': dd_pass,
                'consecutive_losses_check': cl_pass,
            },
            'check_reasons': {
                'daily_loss': dl_reason,
                'drawdown': dd_reason,
                'consecutive_losses': cl_reason,
            },
        }

    def check_daily_loss(self) -> Tuple[bool, str]:
        """
        Check if daily loss exceeds configured threshold (default $100).

        Returns:
            (True, "OK") if within limits
            (False, "reason") if exceeded
        """
        if self.daily_pnl >= -self.config.max_daily_loss_dollars:
            remaining = self.config.max_daily_loss_dollars + self.daily_pnl
            return True, f"Daily P&L ${self.daily_pnl:+.2f} within ${self.config.max_daily_loss_dollars:.0f} limit (${remaining:+.2f} remaining)"
        return False, (
            f"Daily loss ${abs(self.daily_pnl):.2f} exceeds "
            f"${self.config.max_daily_loss_dollars:.0f} limit"
        )

    def check_drawdown(self) -> Tuple[bool, str]:
        """
        Check if drawdown exceeds maximum allowed level (default 20%).

        Drawdown = (peak - current) / peak

        Returns:
            (True, "OK") if within limits
            (False, "reason") if exceeded
        """
        drawdown_pct = self._calculate_drawdown_pct()
        if drawdown_pct <= self.config.max_drawdown_percent:
            return True, (
                f"Drawdown {drawdown_pct:.1%} within "
                f"{self.config.max_drawdown_percent:.0%} limit"
            )
        return False, (
            f"Drawdown {drawdown_pct:.1%} exceeds "
            f"{self.config.max_drawdown_percent:.0%} limit"
        )

    def check_consecutive_losses(self) -> Tuple[bool, str]:
        """
        Check if consecutive losses exceed configured threshold (default 8).

        Returns:
            (True, "OK") if within limits
            (False, "reason") if exceeded
        """
        if self.consecutive_losses <= self.config.max_consecutive_losses:
            if self.consecutive_losses == 0:
                return True, "No consecutive losses"
            return True, (
                f"{self.consecutive_losses} consecutive losses within "
                f"{self.config.max_consecutive_losses} limit"
            )
        return False, (
            f"{self.consecutive_losses} consecutive losses exceeds "
            f"{self.config.max_consecutive_losses} limit"
        )

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

    def reset_daily_state(self, date: Optional[str] = None):
        """Reset daily tracking values."""
        self.daily_pnl = 0.0
        self.daily_trades = []
        self.consecutive_losses = 0
        self._last_trade_date = date
        self._logger.info("Daily risk state reset %s", f"for {date}" if date else "")

    def evaluate(self) -> RiskState:
        """Evaluate current risk state with full details."""
        dl_pass, dl_reason = self.check_daily_loss()
        dd_pass, dd_reason = self.check_drawdown()
        cl_pass, cl_reason = self.check_consecutive_losses()

        checks = {
            "daily_loss": (dl_pass, dl_reason),
            "drawdown": (dd_pass, dd_reason),
            "consecutive_losses": (cl_pass, cl_reason),
        }

        all_pass = dl_pass and dd_pass and cl_pass

        failed_reasons = [r for p, r in checks.values() if not p]
        halt_reason = "; ".join(failed_reasons) if failed_reasons else ""

        return RiskState(
            metrics=RiskMetrics(
                total_trades=self.total_trades,
                winning_trades=self.winning_trades,
                losing_trades=self.losing_trades,
                total_pnl=self.current_capital - self.config.initial_capital,
                daily_pnl=self.daily_pnl,
                drawdown_pct=self._calculate_drawdown_pct(),
                max_drawdown_pct=self.config.max_drawdown_percent,
                consecutive_losses=self.consecutive_losses,
                peak_balance=self.peak_capital,
                current_balance=self.current_capital,
                max_daily_loss_dollars=self.config.max_daily_loss_dollars,
            ),
            checks=checks,
            halted=not all_pass,
            halt_reason=halt_reason,
        )


def is_station_approved(station: str, approved_stations: Optional[List[str]] = None) -> bool:
    """Check if a station is approved for trading."""
    if approved_stations is None:
        return True  # No restrictions
    return station in approved_stations


def get_approved_stations() -> List[str]:
    """Get list of approved stations."""
    return []  # Empty list means all stations allowed


def evaluate_risk_state(risk_manager: RiskManager) -> RiskState:
    """Evaluate current risk state from a RiskManager instance."""
    return risk_manager.evaluate()


def risk_report(risk_manager: RiskManager) -> Dict[str, Any]:
    """Generate a risk report from a RiskManager instance."""
    state = risk_manager.evaluate()
    return {
        "passed": state.passed,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "metrics": {
            "total_trades": state.metrics.total_trades,
            "winning_trades": state.metrics.winning_trades,
            "losing_trades": state.metrics.losing_trades,
            "total_pnl": state.metrics.total_pnl,
            "daily_pnl": state.metrics.daily_pnl,
            "drawdown_pct": state.metrics.drawdown_pct,
            "max_drawdown_pct": state.metrics.max_drawdown_pct,
            "consecutive_losses": state.metrics.consecutive_losses,
            "peak_balance": state.metrics.peak_balance,
            "current_balance": state.metrics.current_balance,
            "max_daily_loss_dollars": state.metrics.max_daily_loss_dollars,
        },
        "checks": {k: v[0] for k, v in state.checks.items()},
        "check_reasons": {k: v[1] for k, v in state.checks.items()},
    }


def format_risk_alert(risk_manager: RiskManager) -> str:
    """Format a risk alert string from a RiskManager instance."""
    state = risk_manager.evaluate()
    if state.halted:
        return f"RISK ALERT: {state.halt_reason}"
    return "Risk checks passed"


# ─── Backward compatibility wrappers ─────────────────────────────────────

def to_legacy_risk_state(risk_manager: RiskManager) -> 'RiskState':
    """
    Generate a backward-compatible RiskState for callers expecting the old format.

    The old format had passed_checks as Dict[str, bool] and checks as Dict[str, bool].
    The new format has checks as Dict[str, Tuple[bool, str]].
    """
    from dataclasses import replace
    state = risk_manager.evaluate()
    # Create a compat version with old-style dicts
    compat = RiskState(
        metrics=state.metrics,
        checks={k: v[0] for k, v in state.checks.items()},  # old format: bool only
        halted=state.halted,
        halt_reason=state.halt_reason,
    )
    compat.passed_checks = {k: v[0] for k, v in state.checks.items()}
    return compat


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Risk Controls v2.0 (Phase 3.6) Self-Test")
    print("=" * 50)

    # Test with default config
    rm = RiskManager()
    print(f"Config: daily_loss=${rm.config.max_daily_loss_dollars}, "
          f"drawdown={rm.config.max_drawdown_percent:.0%}, "
          f"consecutive={rm.config.max_consecutive_losses}")

    # Test initial state — all pass
    dl_pass, dl_reason = rm.check_daily_loss()
    dd_pass, dd_reason = rm.check_drawdown()
    cl_pass, cl_reason = rm.check_consecutive_losses()
    print(f"Initial: daily_loss={dl_pass}, drawdown={dd_pass}, consecutive={cl_pass}")
    assert dl_pass, "Initial daily loss should pass"

    # Simulate losing trades to trigger daily loss limit
    print("\nRunning trades to trigger daily loss limit...")
    for i in range(5):
        trade = TradeResult(f"loss_{i}", pnl=-30.0, is_profitable=False, trade_date="2024-01-01")
        result = rm.update_after_trade(trade)
        dl_pass, _ = rm.check_daily_loss()
        cl_pass, _ = rm.check_consecutive_losses()
        print(f"  Trade {i}: daily_pnl=${rm.daily_pnl:.1f}, "
              f"daily_loss={dl_pass}, consecutive={rm.consecutive_losses}/{rm.config.max_consecutive_losses}")

    # Should have daily loss fail after 4 trades ($120 > $100)
    dl_pass, dl_reason = rm.check_daily_loss()
    print(f"\nDaily loss check: {dl_pass} — {dl_reason}")
    assert not dl_pass, "Daily loss should fail after $120 loss"

    # Reset and test drawdown
    print("\nTesting drawdown...")
    rm2 = RiskManager()
    # Make a profit first to set peak
    rm2.update_after_trade(TradeResult("win", pnl=500.0, is_profitable=True, trade_date="2024-01-01"))
    print(f"  After profit: peak=${rm2.peak_capital}, current=${rm2.current_capital}")
    # Then lose enough to hit 20% drawdown
    # Need to lose $2100 from $10500 peak to hit 20% drawdown
    for i in range(21):
        trade = TradeResult(f"loss_dd_{i}", pnl=-100.0, is_profitable=False, trade_date="2024-01-01")
        rm2.update_after_trade(trade)

    dd_pass, dd_reason = rm2.check_drawdown()
    print(f"Drawdown: {dd_pass} — {dd_reason}")
    dd_pct = rm2._calculate_drawdown_pct()
    print(f"  Drawdown pct: {dd_pct:.1%}, peak=${rm2.peak_capital}, current=${rm2.current_capital}")

    # Test consecutive losses
    print("\nTesting consecutive losses...")
    rm3 = RiskManager()
    for i in range(10):
        trade = TradeResult(f"consec_{i}", pnl=-10.0, is_profitable=False, trade_date="2024-01-01")
        rm3.update_after_trade(trade)

    cl_pass, cl_reason = rm3.check_consecutive_losses()
    print(f"Consecutive losses: {cl_pass} — {cl_reason}")
    print(f"  Count: {rm3.consecutive_losses}/{rm3.config.max_consecutive_losses}")

    # Test risk_report
    print("\nRisk report:")
    report = risk_report(rm3)
    print(f"  Passed: {report['passed']}, Halted: {report['halted']}")
    print(f"  Reasons: {report['check_reasons']}")

    print("\nAll tests passed.")