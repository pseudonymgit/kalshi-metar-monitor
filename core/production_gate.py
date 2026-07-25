#!/usr/bin/env python3
"""
A10 — Spike Reversion Real-Money Gate + D6 — Loss Distribution Fix

A10 — Spike Reversion Real-Money Gate:
  Spike reversion must meet all criteria before real money trading:
  - ≥58% directional accuracy in paper test
  - ≥100 trades in paper test
  - No single station >30% of trades
  - Sharpe ≥1.0
  Gate is config-locked. Default = shadow mode.
  Only unlock on explicit config change.

D6 — Fix Loss Distribution:
  Analyzes loss distribution from paper trading.
  If losses are fat-tailed (kurtosis > 3), implements a loss-limiter
  that scales down positions after 3+ consecutive losses.

Usage:
    from core.production_gate import ProductionGate, LossLimiter

    gate = ProductionGate()
    if gate.is_unlocked():
        # Enable real-money trading
        pass
    else:
        # Shadow mode only
        print(f"Gate locked: {gate.status_reason}")

    limiter = LossLimiter()
    scale = limiter.get_scale_factor()  # 1.0 normally, <1.0 after losses
"""

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── A10: Real-Money Gate ──────────────────────────────────────

# Gate config file — must exist and have enabled=true to unlock
GATE_CONFIG_PATH = "config/production_gate.json"

# Requirements for real-money unlock
MIN_ACCURACY = 0.58
MIN_TRADES = 100
MAX_STATION_CONCENTRATION = 0.30  # No single station > 30% of trades
MIN_SHARPE = 1.0


class ProductionGate:
    """
    Production gate for spike reversion real-money trading.

    Config-locked: the gate can only be unlocked by writing to
    config/production_gate.json. Default behavior is shadow mode.

    The gate checks:
    1. Paper test accuracy ≥ 58%
    2. Total paper test trades ≥ 100
    3. No single station > 30% of all trades
    4. Sharpe ratio ≥ 1.0
    """

    def __init__(self, config_path: str = GATE_CONFIG_PATH):
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load gate config from file."""
        try:
            if self.config_path.exists():
                with open(self.config_path, "r") as f:
                    self._config = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load production gate config: {e}")
            self._config = {}

    def is_unlocked(self) -> bool:
        """
        Check if the production gate is unlocked.

        Returns:
            True only if config explicitly enables real-money trading.
        """
        # Config must exist with enabled=true
        enabled = self._config.get("enabled", False)
        if not enabled:
            return False

        # Check explicit override
        override = os.environ.get("SPIKE_REVERSION_REAL_MONEY", "")
        if override == "1" or override.lower() == "true":
            return True

        return enabled

    def meets_requirements(
        self,
        accuracy: float,
        total_trades: int,
        station_trade_counts: Dict[str, int],
        sharpe: float,
    ) -> Tuple[bool, List[str]]:
        """
        Check if spike reversion meets all requirements for real-money.

        Args:
            accuracy: Directional accuracy from paper test
            total_trades: Total trades from paper test
            station_trade_counts: Dict of {station: trade_count}
            sharpe: Sharpe ratio

        Returns:
            (meets_requirements, list_of_failures)
        """
        failures = []

        if accuracy < MIN_ACCURACY:
            failures.append(
                f"Accuracy {accuracy:.2%} < {MIN_ACCURACY:.0%} (need {MIN_ACCURACY:.0%})"
            )

        if total_trades < MIN_TRADES:
            failures.append(
                f"Trades {total_trades} < {MIN_TRADES} (need {MIN_TRADES})"
            )

        # Check station concentration
        if total_trades > 0:
            for station, count in station_trade_counts.items():
                fraction = count / total_trades
                if fraction > MAX_STATION_CONCENTRATION:
                    failures.append(
                        f"Station {station} has {fraction:.1%} of trades "
                        f"({MAX_STATION_CONCENTRATION:.0%} max)"
                    )

        if sharpe < MIN_SHARPE:
            failures.append(
                f"Sharpe {sharpe:.2f} < {MIN_SHARPE} (need {MIN_SHARPE})"
            )

        if not failures:
            return True, []
        return False, failures

    @property
    def mode(self) -> str:
        """Get current mode string."""
        if self.is_unlocked():
            return "real_money"
        return "shadow"

    @property
    def status_reason(self) -> str:
        """Get human-readable status explanation."""
        if self.is_unlocked():
            return "Production gate UNLOCKED — real-money trading enabled"
        if self._config.get("enabled") is False:
            return "Production gate LOCKED by config"
        return "Production gate LOCKED (default shadow mode)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "is_unlocked": self.is_unlocked(),
            "requirements": {
                "min_accuracy": MIN_ACCURACY,
                "min_trades": MIN_TRADES,
                "max_station_concentration": MAX_STATION_CONCENTRATION,
                "min_sharpe": MIN_SHARPE,
            },
        }


# ─── D6: Loss Distribution Limiter ─────────────────────────────

# Loss streak threshold before scaling down
CONSECUTIVE_LOSS_TRIGGER = 3

# Scale-down factors
SCALE_AFTER_3_LOSSES = 0.5    # 50% reduction after 3 losses
SCALE_AFTER_5_LOSSES = 0.25   # 75% reduction after 5 losses
SCALE_AFTER_7_LOSSES = 0.0    # Full halt after 7 losses

# Recovery
RECOVERY_WINS_NEEDED = 2      # Number of wins needed to restore full scale


class LossLimiter:
    """
    Loss distribution analyzer and position sizing limiter.

    Tracks consecutive losses and scales down position sizes
    to prevent catastrophic drawdown from fat-tailed losses.
    """

    def __init__(self):
        self._consecutive_losses = 0
        self._consecutive_wins_since_scale = 0
        self._is_scaled = False
        self._loss_history: List[float] = []  # Magnitude of each loss
        self._win_history: List[float] = []   # Magnitude of each win

    def record_outcome(self, was_loss: bool, magnitude: float = 0.0) -> None:
        """
        Record a trade outcome for loss streak tracking.

        Args:
            was_loss: True if the trade was a loss
            magnitude: Magnitude of the loss/win (e.g., P&L in dollars)
        """
        if was_loss:
            self._consecutive_losses += 1
            self._consecutive_wins_since_scale = 0
            self._loss_history.append(abs(magnitude))
        else:
            self._consecutive_losses = 0
            if self._is_scaled:
                self._consecutive_wins_since_scale += 1
            self._win_history.append(abs(magnitude) if magnitude else 1.0)

    def get_scale_factor(self) -> float:
        """
        Get the current position sizing scale factor based on loss streak.

        Returns:
            Scale factor [0.0, 1.0]:
            - 1.0: Normal sizing (no streak or recovered)
            - 0.5: After 3 consecutive losses
            - 0.25: After 5 consecutive losses
            - 0.0: After 7 consecutive losses (halt)
        """
        if self._consecutive_losses >= 7:
            self._is_scaled = True
            return SCALE_AFTER_7_LOSSES

        if self._consecutive_losses >= 5:
            self._is_scaled = True
            return SCALE_AFTER_5_LOSSES

        if self._consecutive_losses >= CONSECUTIVE_LOSS_TRIGGER:
            self._is_scaled = True
            return SCALE_AFTER_3_LOSSES

        # Check if we've recovered
        if self._is_scaled and self._consecutive_wins_since_scale >= RECOVERY_WINS_NEEDED:
            self._is_scaled = False
            self._consecutive_wins_since_scale = 0
            return 1.0

        if self._is_scaled:
            # Still in recovery — stay at current scale
            return SCALE_AFTER_3_LOSSES

        return 1.0

    def is_halted(self) -> bool:
        """Check if the limiter has halted trading."""
        return self._consecutive_losses >= 7

    def get_loss_kurtosis(self) -> float:
        """
        Compute the kurtosis of the loss distribution.

        Kurtosis > 3 indicates fat-tailed losses (heavier tails than normal).

        Returns:
            Excess kurtosis (subtracting 3, so >0 = fat-tailed)
        """
        if len(self._loss_history) < 30:  # B-Mode R8 Cycle 2.5: increased from 4 for statistical stability
            return 0.0  # Not enough data

        n = len(self._loss_history)
        mean = sum(self._loss_history) / n
        if mean == 0:
            return 0.0

        variance = sum((x - mean) ** 2 for x in self._loss_history) / n
        if variance == 0:
            return 0.0

        fourth_moment = sum((x - mean) ** 4 for x in self._loss_history) / n
        kurtosis = fourth_moment / (variance ** 2)

        return kurtosis - 3.0  # Excess kurtosis

    def is_fat_tailed(self) -> bool:
        """Check if loss distribution is fat-tailed (excess kurtosis > 0, i.e. raw kurtosis > 3)."""
        return self.get_loss_kurtosis() > 0.0

    def get_status(self) -> Dict[str, Any]:
        """Get full loss limiter status."""
        return {
            "consecutive_losses": self._consecutive_losses,
            "is_scaled": self._is_scaled,
            "scale_factor": self.get_scale_factor(),
            "is_halted": self.is_halted(),
            "loss_kurtosis": round(self.get_loss_kurtosis(), 4),
            "is_fat_tailed": self.is_fat_tailed(),
            "losses_analyzed": len(self._loss_history),
            "wins_analyzed": len(self._win_history),
        }

    def reset(self) -> None:
        """Reset all state."""
        self._consecutive_losses = 0
        self._consecutive_wins_since_scale = 0
        self._is_scaled = False
        self._loss_history.clear()
        self._win_history.clear()


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation."""

    # ── A10 Tests ──
    gate = ProductionGate(config_path="/tmp/test_prod_gate.json")

    # Test 1: Default locked
    assert not gate.is_unlocked()
    assert gate.mode == "shadow"
    print(f"Test 1 PASS: gate locked, mode={gate.mode}")

    # Test 2: Requirements check — all pass
    passes, failures = gate.meets_requirements(
        accuracy=0.62,
        total_trades=150,
        station_trade_counts={"KATL": 30, "KBOS": 40, "KNYC": 35, "KLAX": 45},
        sharpe=1.2,
    )
    assert passes, f"Expected pass, got {failures}"
    print(f"Test 2 PASS: requirements met")

    # Test 3: Requirements check — accuracy fail
    passes, failures = gate.meets_requirements(
        accuracy=0.55,
        total_trades=150,
        station_trade_counts={"KATL": 50, "KBOS": 50, "KNYC": 50},
        sharpe=1.2,
    )
    assert not passes
    assert any("Accuracy" in f for f in failures)
    print(f"Test 3 PASS: accuracy fail detected")

    # Test 4: Requirements check — station concentration fail
    passes, failures = gate.meets_requirements(
        accuracy=0.65,
        total_trades=100,
        station_trade_counts={"KATL": 80, "KBOS": 10, "KNYC": 10},
        sharpe=1.5,
    )
    assert not passes
    assert any("Station" in f for f in failures)
    print(f"Test 4 PASS: concentration fail detected")

    # Test 5: Requirements check — sharpe fail
    passes, failures = gate.meets_requirements(
        accuracy=0.65,
        total_trades=150,
        station_trade_counts={"KATL": 50, "KBOS": 50, "KNYC": 50},
        sharpe=0.8,
    )
    assert not passes
    assert any("Sharpe" in f for f in failures)
    print(f"Test 5 PASS: sharpe fail detected")

    # ── D6 Tests ──
    limiter = LossLimiter()

    # Test 6: No losses → scale = 1.0
    assert limiter.get_scale_factor() == 1.0
    print(f"Test 6 PASS: no losses, scale={limiter.get_scale_factor()}")

    # Test 7: 3 consecutive losses → 0.5
    for i in range(3):
        limiter.record_outcome(was_loss=True, magnitude=-10.0)
    assert limiter.get_scale_factor() == 0.5
    assert not limiter.is_halted()
    print(f"Test 7 PASS: 3 losses, scale={limiter.get_scale_factor()}")

    # Test 8: 5 consecutive losses → 0.25
    for i in range(2):
        limiter.record_outcome(was_loss=True, magnitude=-15.0)
    assert limiter.get_scale_factor() == 0.25
    assert not limiter.is_halted()
    print(f"Test 8 PASS: 5 losses, scale={limiter.get_scale_factor()}")

    # Test 9: 7 consecutive losses → 0.0 (halt)
    for i in range(2):
        limiter.record_outcome(was_loss=True, magnitude=-20.0)
    assert limiter.get_scale_factor() == 0.0
    assert limiter.is_halted()
    print(f"Test 9 PASS: 7 losses, halted")

    # Test 10: Recovery
    limiter.reset()
    for i in range(3):
        limiter.record_outcome(was_loss=True, magnitude=-10.0)
    assert limiter.get_scale_factor() == 0.5  # Scaled
    # Win, win — should recover
    limiter.record_outcome(was_loss=False, magnitude=5.0)
    assert limiter.get_scale_factor() == 0.5  # Still in recovery
    limiter.record_outcome(was_loss=False, magnitude=8.0)
    assert limiter.get_scale_factor() == 1.0  # Recovered!
    print(f"Test 10 PASS: recovery, scale={limiter.get_scale_factor()}")

    # Test 11: Kurtosis
    limiter2 = LossLimiter()
    for i in range(10):
        limiter2.record_outcome(was_loss=True, magnitude=10.0)
    k = limiter2.get_loss_kurtosis()
    assert abs(k) < 0.5, f"Expected near 0, got {k}"
    print(f"Test 11 PASS: uniform losses kurtosis={k:.2f}")

    # Test 12: Fat-tailed detection
    limiter3 = LossLimiter()
    # Most losses are small, but 1 is huge (fat tail)
    for i in range(9):
        limiter3.record_outcome(was_loss=True, magnitude=1.0)
    limiter3.record_outcome(was_loss=True, magnitude=50.0)  # Outlier
    assert limiter3.is_fat_tailed(), f"Expected fat-tailed"
    print(f"Test 12 PASS: fat-tailed detection, kurtosis={limiter3.get_loss_kurtosis():.2f}")

    # Test 13: Gate to_dict
    d = gate.to_dict()
    assert "mode" in d
    assert "requirements" in d
    print(f"Test 13 PASS: to_dict={d['mode']}")

    # Test 14: Limiter status
    status = limiter.get_status()
    assert "consecutive_losses" in status
    assert "scale_factor" in status
    assert "is_halted" in status
    print(f"Test 14 PASS: status={status['consecutive_losses']} losses")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()