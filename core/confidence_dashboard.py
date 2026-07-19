#!/usr/bin/env python3
"""
CONFIDENCE TRACKING DASHBOARD (v1.0) — Edge 19
Analytics module for tracking trade-level confidence, performance metrics,
and risk alert thresholds. No Flask dependency — pure analytics.

Provides:
  - Running p-value (binomial test vs 50% win rate)
  - Cumulative P&L tracking
  - Rolling Sharpe ratio
  - Rolling win rate
  - Current and maximum drawdown
  - Monte Carlo simulation (percentile P&L analysis)
  - Alert threshold checking (drawdown, win rate, Sharpe)

All computations are deterministic (no AI/ML). Uses scipy for binomial
testing and numpy for Monte Carlo simulation.

Usage:
    from core.confidence_dashboard import ConfidenceTracker

    tracker = ConfidenceTracker()
    tracker.record_trade("2026-07-18T10:00:00Z", "UP", 0.72, True, 12.50)
    tracker.get_running_p_value()
    tracker.monte_carlo_simulation(n_simulations=10000)

Self-test:
    python3 core/confidence_dashboard.py --self-test

Version: v1.0, 2026-07-18
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats as sp_stats

_LOGGER = logging.getLogger(__name__)


# ─── Alert Threshold Constants ─────────────────────────────────────────────

ALERT_DRAWDOWN_WARNING = 0.08       # 8% drawdown → warning
ALERT_WIN_RATE_MINIMUM = 0.55       # <55% win rate → warning
ALERT_SHARPE_MINIMUM = 0.5          # <0.5 rolling Sharpe → warning
ALERT_P_VALUE_SIGNIFICANCE = 0.05   # p < 0.05 → significant edge
MONTE_CARLO_DEFAULT_SIMS = 10_000
MONTE_CARLO_PERCENTILE = 5          # 5th percentile


# ─── Data Structures ──────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """Single trade record for confidence tracking."""

    timestamp: str          # ISO 8601 UTC
    direction: str          # "UP" or "DOWN"
    confidence: float       # 0.0–1.0
    was_correct: bool       # True if trade was profitable
    pnl: float             # Profit/loss in dollars


@dataclass
class AlertCondition:
    """A single alert condition triggered by threshold check."""

    alert_type: str        # "drawdown", "win_rate", "sharpe"
    severity: str           # "warning" or "critical"
    current_value: float
    threshold: float
    message: str


@dataclass
class MonteCarloResult:
    """Result of a Monte Carlo simulation run."""

    n_simulations: int
    observed_win_rate: float
    percentile_5_pnl: float
    breakeven_pnl: float
    below_breakeven: bool
    prob_loss: float        # P(cumulative P&L < 0)
    median_pnl: float
    worst_pnl: float
    best_pnl: float
    alert_triggered: bool


# ─── Confidence Tracker ────────────────────────────────────────────────────


class ConfidenceTracker:
    """
    Analytics module for tracking trade confidence and performance metrics.

    Records individual trades and computes:
      - Running p-value (binomial test vs 50% win rate)
      - Cumulative P&L
      - Rolling Sharpe ratio (over a configurable window)
      - Rolling win rate
      - Current and maximum drawdown
      - Monte Carlo simulation (percentile P&L analysis)
      - Alert threshold conditions

    No Flask dependency. All outputs are JSON-serializable dicts.
    """

    def __init__(self, trade_history: list = None):
        """
        Initialize the confidence tracker.

        Args:
            trade_history: Optional list of pre-existing trades. Each element
                should be a tuple/dict matching record_trade() parameters.
                If None, starts with an empty history.
        """
        self._trades: List[TradeRecord] = []
        self._logger = logging.getLogger(f"{__name__}.ConfidenceTracker")

        if trade_history:
            for trade in trade_history:
                if isinstance(trade, dict):
                    self.record_trade(**trade)
                elif isinstance(trade, (list, tuple)):
                    self.record_trade(*trade)

    # ─── Core Methods ──────────────────────────────────────────────────────

    def record_trade(
        self,
        timestamp: str,
        direction: str,
        confidence: float,
        was_correct: bool,
        pnl: float,
    ) -> None:
        """
        Record a single trade in the history.

        Args:
            timestamp: ISO 8601 UTC string (e.g., "2026-07-18T10:00:00Z").
            direction: Trade direction ("UP" or "DOWN").
            confidence: Confidence score 0.0–1.0.
            was_correct: Whether the trade was profitable.
            pnl: Profit/loss in dollars.
        """
        trade = TradeRecord(
            timestamp=timestamp,
            direction=direction.upper().strip(),
            confidence=float(confidence),
            was_correct=bool(was_correct),
            pnl=float(pnl),
        )
        self._trades.append(trade)
        self._logger.debug(
            "Recorded trade: %s %s confidence=%.3f correct=%s pnl=%.2f",
            timestamp, trade.direction, trade.confidence, trade.was_correct, pnl,
        )

    def _wins_losses(self) -> Tuple[int, int]:
        """Return (wins, losses) from trade history."""
        wins = sum(1 for t in self._trades if t.was_correct)
        losses = len(self._trades) - wins
        return wins, losses

    def _pnl_series(self) -> np.ndarray:
        """Return cumulative P&L at each trade point."""
        if not self._trades:
            return np.array([])
        return np.cumsum([t.pnl for t in self._trades])

    # ─── Statistical Tests ─────────────────────────────────────────────────

    def get_running_p_value(self) -> float:
        """
        Compute the running p-value from a binomial test vs 50% win rate.

        Uses scipy.stats.binomtest (exact binomial test). Returns 1.0
        if fewer than 2 trades have been recorded.

        Returns:
            One-sided p-value testing H0: win_rate = 0.5 vs H1: win_rate > 0.5.
        """
        if len(self._trades) < 2:
            return 1.0

        wins, _ = self._wins_losses()
        n = len(self._trades)

        try:
            result = sp_stats.binomtest(wins, n, p=0.5, alternative="greater")
            return float(result.pvalue)
        except Exception as exc:
            self._logger.warning("Binomial test failed: %s", exc)
            return 1.0

    def get_cumulative_pnl(self) -> float:
        """
        Compute total cumulative P&L across all recorded trades.

        Returns:
            Sum of all trade P&L values.
        """
        return sum(t.pnl for t in self._trades)

    def get_rolling_sharpe(self, window: int = 50) -> float:
        """
        Compute the rolling Sharpe ratio over the last `window` trades.

        Sharpe = mean(pnl) / std(pnl) for the window. Returns 0.0
        if insufficient data or zero variance.

        Args:
            window: Number of recent trades to include.

        Returns:
            Per-trade Sharpe ratio (not annualized).
        """
        if len(self._trades) < 2:
            return 0.0

        recent = self._trades[-window:]
        pnls = np.array([t.pnl for t in recent])

        if len(pnls) < 2:
            return 0.0

        mean_pnl = float(np.mean(pnls))
        std_pnl = float(np.std(pnls, ddof=1))

        if std_pnl == 0.0:
            return 0.0

        return round(mean_pnl / std_pnl, 6)

    def get_rolling_win_rate(self, window: int = 20) -> float:
        """
        Compute the rolling win rate over the last `window` trades.

        Args:
            window: Number of recent trades to include.

        Returns:
            Fraction of winning trades in the window (0.0–1.0).
        """
        if len(self._trades) == 0:
            return 0.0

        recent = self._trades[-window:]
        wins = sum(1 for t in recent if t.was_correct)
        return round(wins / len(recent), 6)

    def get_drawdown(self) -> Tuple[float, float]:
        """
        Compute current and maximum drawdown from the cumulative P&L curve.

        Drawdown is measured as the percentage decline from the running
        peak of cumulative P&L. If the peak is ≤ 0, drawdown is 0.

        Returns:
            Tuple of (current_drawdown_pct, max_drawdown_pct).
            Both values are non-negative decimals (e.g., 0.05 = 5%).
        """
        cum_pnl = self._pnl_series()
        if len(cum_pnl) == 0:
            return (0.0, 0.0)

        running_max = np.maximum.accumulate(cum_pnl)
        drawdowns = cum_pnl - running_max  # negative or zero

        # Convert to percentage relative to peak
        # Guard against division by zero
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdown_pct = np.where(
                running_max > 0,
                -drawdowns / running_max,
                0.0,
            )

        current_dd = float(drawdown_pct[-1])
        max_dd = float(np.max(drawdown_pct))

        return (round(current_dd, 6), round(max_dd, 6))

    def monte_carlo_simulation(self, n_simulations: int = 10000) -> dict:
        """
        Run a Monte Carlo simulation at the observed win rate.

        Simulates n_simulations random sequences of trades, each with the
        same length as the actual trade history. For each simulation:
          - Each trade wins with probability = observed win rate
          - Winning trades get +avg_winning_pnl
          - Losing trades get -avg_losing_pnl
        Computes the 5th percentile of cumulative P&L across all simulations.
        If the 5th percentile is below breakeven (0), an alert is triggered.

        Args:
            n_simulations: Number of simulated trade sequences.

        Returns:
            Dict with simulation results (MonteCarloResult fields).
        """
        if len(self._trades) < 2:
            return asdict(MonteCarloResult(
                n_simulations=n_simulations,
                observed_win_rate=0.0,
                percentile_5_pnl=0.0,
                breakeven_pnl=0.0,
                below_breakeven=False,
                prob_loss=0.0,
                median_pnl=0.0,
                worst_pnl=0.0,
                best_pnl=0.0,
                alert_triggered=False,
            ))

        n_trades = len(self._trades)
        wins, losses = self._wins_losses()
        observed_win_rate = wins / n_trades

        # Compute average win and loss magnitudes
        winning_pnls = [t.pnl for t in self._trades if t.was_correct and t.pnl > 0]
        losing_pnls = [t.pnl for t in self._trades if not t.was_correct and t.pnl < 0]

        avg_win = float(np.mean(winning_pnls)) if winning_pnls else 10.0
        avg_loss = float(np.mean(losing_pnls)) if losing_pnls else -10.0

        # Run simulations using vectorized numpy
        rng = np.random.default_rng(seed=42)  # reproducible
        # Shape: (n_simulations, n_trades)
        wins_mask = rng.random((n_simulations, n_trades)) < observed_win_rate
        sim_pnls = np.where(wins_mask, avg_win, avg_loss)
        sim_cum_pnls = np.sum(sim_pnls, axis=1)

        percentile_5 = float(np.percentile(sim_cum_pnls, MONTE_CARLO_PERCENTILE))
        median_pnl = float(np.median(sim_cum_pnls))
        worst_pnl = float(np.min(sim_cum_pnls))
        best_pnl = float(np.max(sim_cum_pnls))
        prob_loss = float(np.mean(sim_cum_pnls < 0))

        result = MonteCarloResult(
            n_simulations=n_simulations,
            observed_win_rate=round(observed_win_rate, 6),
            percentile_5_pnl=round(percentile_5, 4),
            breakeven_pnl=0.0,
            below_breakeven=percentile_5 < 0.0,
            prob_loss=round(prob_loss, 6),
            median_pnl=round(median_pnl, 4),
            worst_pnl=round(worst_pnl, 4),
            best_pnl=round(best_pnl, 4),
            alert_triggered=percentile_5 < 0.0,
        )

        return asdict(result)

    def check_alert_thresholds(self) -> list[dict]:
        """
        Check all alert thresholds and return any triggered conditions.

        Checks:
          - Drawdown > 8% → warning
          - Win rate < 55% → warning
          - Rolling Sharpe < 0.5 → warning

        Returns:
            List of AlertCondition dicts. Empty list if no alerts.
        """
        alerts: List[AlertCondition] = []

        # Drawdown check
        current_dd, max_dd = self.get_drawdown()
        if current_dd > ALERT_DRAWDOWN_WARNING:
            severity = "critical" if current_dd > 0.15 else "warning"
            alerts.append(AlertCondition(
                alert_type="drawdown",
                severity=severity,
                current_value=round(current_dd, 6),
                threshold=ALERT_DRAWDOWN_WARNING,
                message=f"Current drawdown {current_dd:.2%} exceeds {ALERT_DRAWDOWN_WARNING:.0%} threshold",
            ))

        # Win rate check
        if len(self._trades) >= 20:
            win_rate = self.get_rolling_win_rate(window=20)
            if win_rate < ALERT_WIN_RATE_MINIMUM:
                severity = "critical" if win_rate < 0.45 else "warning"
                alerts.append(AlertCondition(
                    alert_type="win_rate",
                    severity=severity,
                    current_value=round(win_rate, 6),
                    threshold=ALERT_WIN_RATE_MINIMUM,
                    message=f"Rolling win rate {win_rate:.2%} below {ALERT_WIN_RATE_MINIMUM:.0%} threshold",
                ))

        # Sharpe check
        if len(self._trades) >= 10:
            sharpe = self.get_rolling_sharpe(window=50)
            if sharpe < ALERT_SHARPE_MINIMUM:
                severity = "critical" if sharpe < 0.0 else "warning"
                alerts.append(AlertCondition(
                    alert_type="sharpe",
                    severity=severity,
                    current_value=round(sharpe, 6),
                    threshold=ALERT_SHARPE_MINIMUM,
                    message=f"Rolling Sharpe {sharpe:.3f} below {ALERT_SHARPE_MINIMUM} threshold",
                ))

        return [asdict(a) for a in alerts]

    # ─── Full Snapshot ────────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        """
        Return a complete JSON-serializable snapshot of all metrics.

        Useful for dashboard rendering or API responses.

        Returns:
            Dict with all computed metrics and alert conditions.
        """
        wins, losses = self._wins_losses()
        current_dd, max_dd = self.get_drawdown()

        return {
            "trade_count": len(self._trades),
            "wins": wins,
            "losses": losses,
            "overall_win_rate": round(wins / max(len(self._trades), 1), 6),
            "cumulative_pnl": round(self.get_cumulative_pnl(), 4),
            "running_p_value": round(self.get_running_p_value(), 6),
            "rolling_sharpe_50": self.get_rolling_sharpe(window=50),
            "rolling_win_rate_20": self.get_rolling_win_rate(window=20),
            "current_drawdown_pct": current_dd,
            "max_drawdown_pct": max_dd,
            "alerts": self.check_alert_thresholds(),
            "monte_carlo": self.monte_carlo_simulation(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ─── Self-Tests ────────────────────────────────────────────────────────────


def _self_test():
    """Run deterministic self-tests for all ConfidenceTracker components."""
    print("=" * 60)
    print("Confidence Tracking Dashboard — Self-Test")
    print("=" * 60)

    # Build a controlled trade history
    tracker = ConfidenceTracker()

    # Test 1: Empty state
    print("\n[1] Empty state...")
    assert tracker.get_cumulative_pnl() == 0.0
    assert tracker.get_running_p_value() == 1.0
    assert tracker.get_rolling_sharpe() == 0.0
    assert tracker.get_rolling_win_rate() == 0.0
    assert tracker.get_drawdown() == (0.0, 0.0)
    assert tracker.check_alert_thresholds() == []
    print("    ✓ All empty-state metrics return safe defaults")

    # Test 2: Record trades and check cumulative P&L
    print("\n[2] Recording trades...")
    test_trades = [
        ("2026-07-01T10:00:00Z", "UP", 0.72, True, 12.50),
        ("2026-07-02T10:00:00Z", "DOWN", 0.65, False, -8.00),
        ("2026-07-03T10:00:00Z", "UP", 0.80, True, 15.00),
        ("2026-07-04T10:00:00Z", "UP", 0.68, True, 10.00),
        ("2026-07-05T10:00:00Z", "DOWN", 0.55, False, -5.00),
        ("2026-07-06T10:00:00Z", "UP", 0.75, True, 14.00),
        ("2026-07-07T10:00:00Z", "UP", 0.70, True, 11.00),
        ("2026-07-08T10:00:00Z", "DOWN", 0.60, False, -7.00),
        ("2026-07-09T10:00:00Z", "UP", 0.78, True, 13.50),
        ("2026-07-10T10:00:00Z", "UP", 0.66, True, 9.50),
    ]
    for ts, direction, conf, correct, pnl in test_trades:
        tracker.record_trade(ts, direction, conf, correct, pnl)

    total_pnl = tracker.get_cumulative_pnl()
    expected_pnl = sum(t[4] for t in test_trades)
    assert abs(total_pnl - expected_pnl) < 0.01, f"P&L should be {expected_pnl}, got {total_pnl}"
    print(f"    ✓ Cumulative P&L: ${total_pnl:.2f} (expected ${expected_pnl:.2f})")

    # Test 3: Win rate
    print("\n[3] Win rate...")
    win_rate = tracker.get_rolling_win_rate(window=20)
    expected_wr = 7 / 10  # 7 wins out of 10
    assert abs(win_rate - expected_wr) < 0.001, f"Win rate should be {expected_wr}, got {win_rate}"
    print(f"    ✓ Rolling win rate (20): {win_rate:.2%} (expected {expected_wr:.2%})")

    # Test 4: Running p-value
    print("\n[4] Running p-value (binomial test)...")
    p_val = tracker.get_running_p_value()
    assert 0.0 <= p_val <= 1.0, f"p-value should be in [0,1], got {p_val}"
    # 7/10 wins — p-value should be smallish but not necessarily < 0.05
    print(f"    ✓ p-value: {p_val:.6f} (7/10 wins, one-sided binomial)")

    # Test 5: Rolling Sharpe
    print("\n[5] Rolling Sharpe...")
    sharpe = tracker.get_rolling_sharpe(window=50)
    assert isinstance(sharpe, float)
    # P&Ls: [12.5, -8, 15, 10, -5, 14, 11, -7, 13.5, 9.5]
    # mean = 6.55, std ≈ 8.82, sharpe ≈ 0.743
    assert sharpe > 0.5, f"Sharpe should be > 0.5 for this dataset, got {sharpe}"
    print(f"    ✓ Rolling Sharpe: {sharpe:.4f}")

    # Test 6: Drawdown
    print("\n[6] Drawdown...")
    current_dd, max_dd = tracker.get_drawdown()
    assert current_dd >= 0.0, "Current drawdown should be non-negative"
    assert max_dd >= current_dd, "Max drawdown >= current drawdown"
    print(f"    ✓ Current drawdown: {current_dd:.4%}")
    print(f"    ✓ Max drawdown: {max_dd:.4%}")

    # Test 7: Monte Carlo simulation
    print("\n[7] Monte Carlo simulation...")
    mc = tracker.monte_carlo_simulation(n_simulations=5000)
    assert mc["n_simulations"] == 5000
    assert mc["observed_win_rate"] == expected_wr
    assert isinstance(mc["percentile_5_pnl"], float)
    assert mc["below_breakeven"] == (mc["percentile_5_pnl"] < 0.0)
    assert mc["prob_loss"] >= 0.0 and mc["prob_loss"] <= 1.0
    assert mc["worst_pnl"] <= mc["best_pnl"]
    print(f"    ✓ Simulations: {mc['n_simulations']}")
    print(f"    ✓ Observed win rate: {mc['observed_win_rate']:.2%}")
    print(f"    ✓ 5th percentile P&L: ${mc['percentile_5_pnl']:.2f}")
    print(f"    ✓ Median P&L: ${mc['median_pnl']:.2f}")
    print(f"    ✓ P(loss): {mc['prob_loss']:.4f}")
    print(f"    ✓ Alert triggered: {mc['alert_triggered']}")

    # Test 8: Alert thresholds — with winning trades, no alerts expected
    print("\n[8] Alert thresholds (winning history)...")
    alerts = tracker.check_alert_thresholds()
    print(f"    ✓ Alerts: {len(alerts)} (expected 0 for winning history)")
    for a in alerts:
        print(f"      → {a['alert_type']}: {a['message']}")

    # Test 9: Alert thresholds — with losing trades
    print("\n[9] Alert thresholds (losing history)...")
    losing_tracker = ConfidenceTracker()
    for i in range(30):
        losing_tracker.record_trade(
            f"2026-07-{i+1:02d}T10:00:00Z",
            "UP",
            0.60,
            i % 3 == 0,  # ~33% win rate
            10.0 if i % 3 == 0 else -8.0,
        )
    alerts_losing = losing_tracker.check_alert_thresholds()
    assert len(alerts_losing) > 0, "Losing tracker should trigger alerts"
    alert_types = {a["alert_type"] for a in alerts_losing}
    assert "win_rate" in alert_types, "Should flag low win rate"
    print(f"    ✓ Alerts triggered: {len(alerts_losing)}")
    for a in alerts_losing:
        print(f"      → {a['alert_type']} [{a['severity']}]: {a['message']}")

    # Test 10: Snapshot
    print("\n[10] Full snapshot...")
    snapshot = tracker.get_snapshot()
    assert "trade_count" in snapshot
    assert "cumulative_pnl" in snapshot
    assert "running_p_value" in snapshot
    assert "monte_carlo" in snapshot
    assert "alerts" in snapshot
    print(f"    ✓ Trade count: {snapshot['trade_count']}")
    print(f"    ✓ Cumulative P&L: ${snapshot['cumulative_pnl']:.2f}")
    print(f"    ✓ Alerts: {len(snapshot['alerts'])}")
    print(f"    ✓ Monte Carlo included: {bool(snapshot['monte_carlo'])}")

    # Test 11: Initialization from trade_history
    print("\n[11] Init from trade_history...")
    history = [
        ("2026-07-01T10:00:00Z", "UP", 0.70, True, 10.0),
        ("2026-07-02T10:00:00Z", "DOWN", 0.65, False, -5.0),
    ]
    tracker2 = ConfidenceTracker(trade_history=history)
    assert tracker2.get_cumulative_pnl() == 5.0
    assert tracker2.get_rolling_win_rate() == 0.5
    print(f"    ✓ P&L from history: ${tracker2.get_cumulative_pnl():.2f}")
    print(f"    ✓ Win rate from history: {tracker2.get_rolling_win_rate():.2%}")

    print("\n" + "=" * 60)
    print("All self-tests passed ✓")
    print("=" * 60)


def _main():
    """Parse args and run self-tests or demo."""
    parser = argparse.ArgumentParser(
        description="Confidence Tracking Dashboard (Edge 19)"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run self-tests and exit",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a demo with synthetic data",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.self_test:
        _self_test()
    elif args.demo:
        _demo()
    else:
        parser.print_help()


def _demo():
    """Run a quick demo with synthetic trade data."""
    print("Confidence Tracker Demo — 25 synthetic trades\n")

    import random
    random.seed(42)

    tracker = ConfidenceTracker()
    for i in range(25):
        is_win = random.random() < 0.64  # ~64% win rate
        pnl = random.uniform(8, 15) if is_win else -random.uniform(5, 10)
        tracker.record_trade(
            f"2026-07-{i+1:02d}T10:00:00Z",
            random.choice(["UP", "DOWN"]),
            random.uniform(0.55, 0.85),
            is_win,
            round(pnl, 2),
        )

    snapshot = tracker.get_snapshot()
    print(f"Trades: {snapshot['trade_count']}")
    print(f"Wins: {snapshot['wins']}, Losses: {snapshot['losses']}")
    print(f"Win rate: {snapshot['overall_win_rate']:.2%}")
    print(f"Cumulative P&L: ${snapshot['cumulative_pnl']:.2f}")
    print(f"p-value: {snapshot['running_p_value']:.6f}")
    print(f"Sharpe (50): {snapshot['rolling_sharpe_50']:.4f}")
    print(f"Drawdown: current={snapshot['current_drawdown_pct']:.2%}, max={snapshot['max_drawdown_pct']:.2%}")
    print(f"Alerts: {len(snapshot['alerts'])}")
    mc = snapshot['monte_carlo']
    print(f"Monte Carlo: 5th pct = ${mc['percentile_5_pnl']:.2f}, P(loss) = {mc['prob_loss']:.4f}")


if __name__ == "__main__":
    _main()
