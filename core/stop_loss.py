#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Stop-Loss Monitoring — Edge 18, Phase 3 Risk Management

Implements multi-condition stop-loss monitoring with recovery protocol.

Stop conditions:
  - Primary:    20 losing trades OR 30 calendar days elapsed
  - Drawdown:   > 10% of budget → pause
  - Win rate:   rolling 20-trade < 52% → pause
  - Sharpe:     rolling 50-trade < 0.5 → re-evaluate

Recovery:
  - 7-day cooling-off period
  - 10 consecutive paper trades before resuming

Deterministic math only — no AI/ML.

Version: Edge 18 — 2026-07-18
"""

import logging
import math
from typing import Tuple, List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

PRIMARY_LOSS_LIMIT = 20
PRIMARY_DAY_LIMIT = 30
DRAWDOWN_STOP_PCT = 10.0       # percent of budget
WIN_RATE_STOP_PCT = 52.0       # rolling 20-trade minimum
SHARPE_STOP_THRESHOLD = 0.5    # rolling 50-trade minimum
WIN_RATE_WINDOW = 20
SHARPE_WINDOW = 50

COOLDOWN_DAYS = 7
PAPER_TRADES_REQUIRED = 10


@dataclass
class TradeRecord:
    """A single trade for stop-loss tracking."""
    pnl: float
    is_profitable: bool
    date_str: str        # YYYY-MM-DD
    date: datetime = field(default_factory=datetime.utcnow)


class StopLossMonitor:
    """
    Multi-condition stop-loss monitor with structured recovery protocol.
    """

    def __init__(self, budget: float = 250.0):
        self.budget = budget
        self._trades: List[TradeRecord] = []
        self._cumulative_pnl: float = 0.0
        self._peak_pnl: float = 0.0
        self._state: str = "ACTIVE"   # ACTIVE | PAUSED | RE-EVALUATE | COOLING | RECOVERING
        self._stop_date: Optional[datetime] = None
        self._stop_reason: str = ""
        self._paper_trade_results: List[bool] = []
        self._cooldown_start: Optional[datetime] = None
        _LOGGER.info("StopLossMonitor initialised — budget=$%.2f", budget)

    # ─── Public API ────────────────────────────────────────────────────────

    def record_trade(self, pnl: float, is_profitable: bool, date_str: str) -> None:
        """
        Append a trade result to the rolling history.
        """
        dt = datetime.strptime(date_str, "%Y-%m-%d") if isinstance(date_str, str) else date_str
        record = TradeRecord(
            pnl=pnl,
            is_profitable=is_profitable,
            date_str=date_str,
            date=dt,
        )
        self._trades.append(record)
        self._cumulative_pnl += pnl
        if self._cumulative_pnl > self._peak_pnl:
            self._peak_pnl = self._cumulative_pnl
        _LOGGER.debug(
            "Trade recorded — pnl=%.2f profitable=%s date=%s total_trades=%d",
            pnl, is_profitable, date_str, len(self._trades),
        )

    def check_stop_conditions(self) -> Tuple[bool, str, dict]:
        """
        Evaluate all stop conditions.

        Returns (stopped, reason, details).
        - stopped=True means trading should halt.
        - details contains per-condition diagnostics.
        """
        if self._state in ("PAUSED", "COOLING", "RECOVERING"):
            return True, self._stop_reason, {"state": self._state}

        details: Dict = {"state": self._state}

        # 1. Primary stop: total losing trades
        total_losses = sum(1 for t in self._trades if not t.is_profitable)
        details["total_losses"] = total_losses
        if total_losses >= PRIMARY_LOSS_LIMIT:
            reason = f"Primary stop: {total_losses} losing trades >= {PRIMARY_LOSS_LIMIT}"
            self._set_state("PAUSED", reason)
            details["trigger"] = "primary_loss_limit"
            _LOGGER.warning(reason)
            return True, reason, details

        # 2. Primary stop: calendar days elapsed
        if self._trades:
            first_date = self._trades[0].date
            days_elapsed = (datetime.utcnow() - first_date).days
            details["days_elapsed"] = days_elapsed
            if days_elapsed >= PRIMARY_DAY_LIMIT:
                reason = f"Primary stop: {days_elapsed} days >= {PRIMARY_DAY_LIMIT} day limit"
                self._set_state("PAUSED", reason)
                details["trigger"] = "primary_day_limit"
                _LOGGER.warning(reason)
                return True, reason, details

        # 3. Drawdown stop
        drawdown = self._calculate_drawdown()
        details["drawdown_pct"] = round(drawdown, 2)
        if drawdown > DRAWDOWN_STOP_PCT:
            reason = f"Drawdown stop: {drawdown:.1f}% > {DRAWDOWN_STOP_PCT}% of budget"
            self._set_state("PAUSED", reason)
            details["trigger"] = "drawdown"
            _LOGGER.warning(reason)
            return True, reason, details

        # 4. Win-rate stop (rolling 20)
        recent = self._trades[-WIN_RATE_WINDOW:]
        if len(recent) >= WIN_RATE_WINDOW:
            wins = sum(1 for t in recent if t.is_profitable)
            win_rate = (wins / len(recent)) * 100
            details["rolling_win_rate"] = round(win_rate, 2)
            if win_rate < WIN_RATE_STOP_PCT:
                reason = f"Win-rate stop: {win_rate:.1f}% < {WIN_RATE_STOP_PCT}% (rolling {WIN_RATE_WINDOW})"
                self._set_state("PAUSED", reason)
                details["trigger"] = "win_rate"
                _LOGGER.warning(reason)
                return True, reason, details

        # 5. Sharpe stop (rolling 50)
        recent_sharpe = self._trades[-SHARPE_WINDOW:]
        if len(recent_sharpe) >= SHARPE_WINDOW:
            sharpe = self._calculate_rolling_sharpe(recent_sharpe)
            details["rolling_sharpe"] = round(sharpe, 4)
            if sharpe < SHARPE_STOP_THRESHOLD:
                reason = f"Sharpe stop: {sharpe:.3f} < {SHARPE_STOP_THRESHOLD} (rolling {SHARPE_WINDOW})"
                self._set_state("RE-EVALUATE", reason)
                details["trigger"] = "sharpe"
                _LOGGER.warning(reason)
                return True, reason, details

        details["trigger"] = "none"
        return False, "All stop conditions clear", details

    def evaluate_state(self) -> dict:
        """
        Return a full state snapshot without triggering stops.
        """
        drawdown = self._calculate_drawdown()
        recent_wr = self._trades[-WIN_RATE_WINDOW:]
        recent_sh = self._trades[-SHARPE_WINDOW:]

        win_rate = (
            (sum(1 for t in recent_wr if t.is_profitable) / len(recent_wr)) * 100
            if recent_wr else 0.0
        )
        sharpe = (
            self._calculate_rolling_sharpe(recent_sh)
            if len(recent_sh) >= 2 else 0.0
        )

        return {
            "state": self._state,
            "total_trades": len(self._trades),
            "total_losses": sum(1 for t in self._trades if not t.is_profitable),
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "peak_pnl": round(self._peak_pnl, 2),
            "drawdown_pct": round(drawdown, 2),
            "rolling_win_rate": round(win_rate, 2),
            "rolling_sharpe": round(sharpe, 4),
            "stop_reason": self._stop_reason,
            "paper_trades_completed": len(self._paper_trade_results),
            "paper_trades_required": PAPER_TRADES_REQUIRED,
            "cooldown_active": self._cooldown_start is not None,
        }

    def attempt_recovery(self, date_str: str) -> Tuple[bool, str]:
        """
        Attempt to transition from PAUSED/RE-EVALUATE back to ACTIVE.

        Requires:
          1. 7-day cooldown since stop date
          2. 10 consecutive paper trades (tracked externally via
             record_paper_trade, or passed in bulk)

        Returns (recovered, reason).
        """
        if self._state not in ("PAUSED", "RE-EVALUATE", "COOLING", "RECOVERING"):
            return True, "Already active"

        dt = datetime.strptime(date_str, "%Y-%m-%d") if isinstance(date_str, str) else date_str

        # Phase 1: cooldown
        if self._state in ("PAUSED", "RE-EVALUATE"):
            if self._stop_date is None:
                return False, "No stop date recorded — cannot start cooldown"
            days_since_stop = (dt - self._stop_date).days
            if days_since_stop < COOLDOWN_DAYS:
                return False, (
                    f"Cooldown incomplete — {days_since_stop}/{COOLDOWN_DAYS} days elapsed"
                )
            # Cooldown satisfied → enter recovering state
            self._state = "RECOVERING"
            self._cooldown_start = self._stop_date
            _LOGGER.info(
                "Cooldown satisfied after %d days — entering RECOVERING (paper trades required: %d)",
                days_since_stop, PAPER_TRADES_REQUIRED,
            )

        # Phase 2: paper trades
        if self._state == "RECOVERING":
            if len(self._paper_trade_results) < PAPER_TRADES_REQUIRED:
                return False, (
                    f"Paper trades incomplete — "
                    f"{len(self._paper_trade_results)}/{PAPER_TRADES_REQUIRED} completed"
                )
            if not all(self._paper_trade_results):
                failed = sum(1 for r in self._paper_trade_results if not r)
                return False, (
                    f"Paper trades have {failed} failures — "
                    f"need {PAPER_TRADES_REQUIRED} consecutive wins"
                )
            # All paper trades passed → reactivate
            self._state = "ACTIVE"
            self._stop_date = None
            self._stop_reason = ""
            self._paper_trade_results = []
            self._cooldown_start = None
            _LOGGER.info("Recovery complete — monitor reactivated")
            return True, "Recovery successful — monitor ACTIVE"

        return False, f"Cannot recover from state: {self._state}"

    def record_paper_trade(self, is_profitable: bool) -> None:
        """
        Record a paper-trade result during the RECOVERING phase.
        Reset the sequence on a loss to enforce consecutive wins.
        """
        self._paper_trade_results.append(is_profitable)
        if not is_profitable:
            # Reset — must be consecutive
            self._paper_trade_results = []
            _LOGGER.warning("Paper trade failed — resetting consecutive count")

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _set_state(self, state: str, reason: str) -> None:
        self._state = state
        self._stop_reason = reason
        self._stop_date = datetime.utcnow()

    def _calculate_drawdown(self) -> float:
        """
        Drawdown as a percentage of the budget.
        """
        if self.budget <= 0:
            return 0.0
        trough = min(self._cumulative_pnl, 0.0)
        peak = max(self._peak_pnl, 0.0)
        drawdown_dollars = peak - self._cumulative_pnl
        return (drawdown_dollars / self.budget) * 100.0

    def _calculate_rolling_sharpe(self, trades: List[TradeRecord]) -> float:
        """
        Annualised Sharpe ratio from a list of trades.
        Uses the standard deviation of per-trade PnL.
        """
        if len(trades) < 2:
            return 0.0
        pnls = [t.pnl for t in trades]
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std == 0:
            return 0.0
        # Per-trade Sharpe; no annualisation factor for simplicity
        return mean_pnl / std


# ─── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("\n=== Stop-Loss Monitor — Self-Test ===\n")

    # 1. Basic — no stops triggered
    slm = StopLossMonitor(budget=250.0)
    for i in range(10):
        slm.record_trade(1.5, True, f"2026-07-{i+1:02d}")
    stopped, reason, details = slm.check_stop_conditions()
    print(f"[1] 10 winning trades → stopped={stopped}: {reason}")
    assert not stopped, "Should not stop with winning trades"

    # 2. Drawdown stop
    slm2 = StopLossMonitor(budget=250.0)
    # Build up peak then drawdown
    for i in range(5):
        slm2.record_trade(5.0, True, f"2026-07-{i+1:02d}")
    # Now lose enough to exceed 10% drawdown ($25)
    for i in range(5):
        slm2.record_trade(-6.0, False, f"2026-07-{i+6:02d}")
    stopped, reason, details = slm2.check_stop_conditions()
    print(f"[2] Drawdown test → stopped={stopped}: {reason}")
    print(f"    Details: {details}")
    assert stopped, "Should trigger drawdown stop"
    assert details["trigger"] == "drawdown"

    # 3. Win-rate stop (rolling 20 < 52%)
    slm3 = StopLossMonitor(budget=250.0)
    # 10 wins then 10 losses → 50% < 52%
    for i in range(10):
        slm3.record_trade(2.0, True, f"2026-07-{i+1:02d}")
    for i in range(10):
        slm3.record_trade(-2.0, False, f"2026-07-{i+11:02d}")
    stopped, reason, details = slm3.check_stop_conditions()
    print(f"[3] Win-rate test → stopped={stopped}: {reason}")
    assert stopped, "Should trigger win-rate stop (50% < 52%)"

    # 4. Primary loss limit (20 losing trades)
    slm4 = StopLossMonitor(budget=250.0)
    for i in range(20):
        slm4.record_trade(-1.0, False, f"2026-07-{i+1:02d}")
    stopped, reason, details = slm4.check_stop_conditions()
    print(f"[4] Primary loss limit → stopped={stopped}: {reason}")
    assert stopped, "Should trigger primary loss limit (20 losses)"

    # 5. Sharpe stop (rolling 50)
    #    Need 50 trades where rolling-20 win rate >= 52% but rolling-50 Sharpe < 0.5.
    #    Pattern: 35 wins (tiny +0.1) + 15 losses (large -0.8), arranged so the
    #    last 20 trades have 11 wins / 9 losses (55% >= 52%).
    #    Mean PnL is deeply negative → Sharpe < 0.5.
    slm5 = StopLossMonitor(budget=250.0)
    base_date = datetime(2026, 7, 1)
    # First 30 trades: 24 wins, 6 losses
    first30 = [True] * 24 + [False] * 6
    # Last 20 trades: 11 wins, 9 losses (55% win rate >= 52%)
    last20 = [True] * 11 + [False] * 9
    pattern = first30 + last20
    assert len(pattern) == 50
    for i, is_win in enumerate(pattern):
        d = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        pnl = 0.1 if is_win else -0.8
        slm5.record_trade(pnl, is_win, d)
    stopped, reason, details = slm5.check_stop_conditions()
    print(f"[5] Sharpe stop → stopped={stopped}: {reason}")
    assert stopped, "Should trigger Sharpe stop (negative Sharpe < 0.5)"
    assert details["trigger"] == "sharpe"

    # 6. Recovery protocol
    slm6 = StopLossMonitor(budget=250.0)
    for i in range(20):
        slm6.record_trade(-1.0, False, f"2026-07-{i+1:02d}")
    stopped, _, _ = slm6.check_stop_conditions()
    assert stopped
    # Attempt immediate recovery — cooldown not met
    recovered, reason = slm6.attempt_recovery("2026-07-22")
    print(f"[6a] Immediate recovery → recovered={recovered}: {reason}")
    assert not recovered, "Should fail — cooldown not met"

    # After 7 days
    recovered, reason = slm6.attempt_recovery("2026-07-29")
    print(f"[6b] Recovery after 7 days (no paper trades) → recovered={recovered}: {reason}")
    assert not recovered, "Should fail — paper trades incomplete"

    # Complete paper trades
    for _ in range(10):
        slm6.record_paper_trade(True)
    recovered, reason = slm6.attempt_recovery("2026-07-29")
    print(f"[6c] Recovery after 7 days + 10 paper trades → recovered={recovered}: {reason}")
    assert recovered, "Should recover after cooldown + paper trades"

    # 7. Paper trade failure resets
    slm7 = StopLossMonitor(budget=250.0)
    slm7._state = "RECOVERING"
    slm7._stop_date = datetime(2026, 7, 1)
    slm7._cooldown_start = datetime(2026, 7, 1)
    for _ in range(5):
        slm7.record_paper_trade(True)
    slm7.record_paper_trade(False)  # failure
    assert len(slm7._paper_trade_results) == 0, "Paper trade failure should reset count"
    print(f"[7] Paper trade failure resets count → {len(slm7._paper_trade_results)}")
    print("\n✅ All Stop-Loss Monitor tests passed.\n")
