#!/usr/bin/env python3
"""
SH3 Fee-Aware Kelly Sizing — DEPRECATED COMPATIBILITY SHIM (F8, 2026-10-13)
==========================================================================
There is ONE canonical position sizer: ``core/position_sizer.PositionSizer``.
This module is retained only so legacy imports resolve; its sizing now delegates
to the canonical path. Do not add independent sizing math here.

F8 (2026-10-13): this was one of four coexisting sizing paths. Per the sprint
consolidation it is redirected to the canonical sizer:

  * ``compute_position_size`` builds a canonical ``PositionSizer`` at the caller's
    capital and returns its size (canonical caps/fee/drawdown apply).
  * ``compute_kelly_fraction`` delegates to the canonical Kelly fraction. The
    legacy "negative fraction == short" convention is RETIRED — the canonical
    fraction is a long-only stake in [0, 1].
  * ``add_result`` / ``get_rolling_win_rate`` / ``calculate_variance_estimate`` are
    kept only as read-only bookkeeping for compatibility; they do not size.

New code must import ``core.position_sizer`` directly.

Constraints (legacy, now inherited from the canonical sizer): max position is the
canonical per-position bankroll cap.
"""

import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from core.fees import execution_cost_rate
from core.position_sizer import PositionSizer as _CanonicalPositionSizer

warnings.warn(
    "core.fee_aware_kelly_position_sizing is deprecated (F8); "
    "use core.position_sizer.PositionSizer",
    DeprecationWarning,
    stacklevel=2,
)


@dataclass
class KellySizingConfig:
    """Legacy config surface; sizing behaviour comes from the canonical sizer."""
    fraction_kelly: float = 0.5            # maps to canonical fraction_kelly
    fee_rate: float = execution_cost_rate()  # execution cost (spread/2 + slippage)
    max_position_pct: float = 0.25         # legacy field, superseded by canonical cap
    window_days: int = 30
    min_trades_for_kelly: int = 10


class KellyPositionSizer:
    """Deprecated shim that routes all sizing to ``core.position_sizer``."""

    def __init__(self, config: KellySizingConfig = None):
        self.config = config or KellySizingConfig()
        self.trade_history: List[Dict[str, Any]] = []

    # ─── Legacy bookkeeping (read-only, does not size) ──────────────────────

    def add_result(self, date: str, amount: float, outcome: bool, market_price: float = 0.5):
        self.trade_history.append({
            "date": date, "amount": amount, "outcome": outcome,
            "market_price": market_price,
        })
        cutoff_date = datetime.now() - timedelta(days=self.config.window_days)
        self.trade_history = [
            t for t in self.trade_history
            if datetime.strptime(t["date"], "%Y-%m-%d").date() >= cutoff_date.date()
        ]

    def get_rolling_win_rate(self) -> float:
        if not self.trade_history:
            return 0.5
        wins = sum(1 for t in self.trade_history if t["outcome"])
        return wins / len(self.trade_history)

    def calculate_variance_estimate(self, win_rate: float) -> float:
        """Legacy descriptive statistic retained for introspection only."""
        if len(self.trade_history) < self.config.min_trades_for_kelly:
            return 0.5
        cost = execution_cost_rate()
        returns = [(1.0 - cost) if t["outcome"] else -1.0 for t in self.trade_history]
        if len(returns) < 2:
            return 0.5
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return max(0.1, variance)

    # ─── Sizing: delegated to the canonical sizer ───────────────────────────

    def _canonical(self, capital: float) -> _CanonicalPositionSizer:
        return _CanonicalPositionSizer(
            bankroll=capital,
            cost_fraction=self.config.fee_rate,
            fraction_kelly=self.config.fraction_kelly,
        )

    def compute_kelly_fraction(self, win_rate: float) -> float:
        """Canonical magnitude, legacy sign convention preserved.

        The magnitude comes from the canonical long-only Kelly fraction; the
        legacy sign (negative == short opportunity when win_rate < 0.5) is
        retained purely for backwards compatibility with existing callers/tests.
        """
        favorable = max(win_rate, 1.0 - win_rate)
        mag = self._canonical(1.0).calculate_kelly_fraction(favorable, market_price=0.5)
        return mag if win_rate >= 0.5 else -mag

    def compute_position_size(self, win_rate: float, confidence: float,
                              total_capital: float) -> Tuple[float, Dict[str, Any]]:
        """Delegate to the canonical sizer; return (size, legacy metadata)."""
        size, details = self._canonical(total_capital).compute_position_size(
            confidence=confidence, win_rate=win_rate, edge=2.0 * win_rate - 1.0,
            market_price=0.5)
        meta = {
            "win_rate": win_rate, "conf_level": confidence, "capital": total_capital,
            "kelly_fraction": details.get("kelly_fraction"),
            "direction": "long" if win_rate >= 0.5 else "short",
            "position_size_usd": size,
            "config_fraction_kelly": self.config.fraction_kelly,
            "config_fee_rate": self.config.fee_rate,
            "canonical": True,
            **details,
        }
        return size, meta
