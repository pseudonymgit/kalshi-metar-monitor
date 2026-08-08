#!/usr/bin/env python3
"""
lever_manager.py — Position Sizing via Kelly → Variance → Drawdown → Concentration

Pipeline order (monotonic decreasing risk):
  1. Continuous Kelly base   — fee_aware_kelly()
  2. Variance adjustment     — variance_adjusted_kelly() multiplier
  3. Drawdown multiplier     — halve at X% drawdown, stop at Y%
  4. Concentration cap       — max % per station / per signal

B-Mode compliant. No AI/ML inside the lever loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np


@dataclass
class LeverState:
    """Mutable state tracking for drawdown and concentration limits."""
    capital: float = 10000.0
    peak_capital: float = 10000.0
    current_drawdown_pct: float = 0.0
    station_exposure: Dict[str, float] = field(default_factory=dict)
    signal_exposure: Dict[str, float] = field(default_factory=dict)
    total_risk: float = 0.0  # sum of notional at risk across all positions


class LeverManager:
    """Manages the 4-stage position sizing pipeline.

    Parameters (from meta_config)
    -----------------------------
    kelly_fraction : float        Kelly fraction [0.1, 1.0]
    capital_base : float          Starting capital [10_000, 200_000]
    variance_sizing_enabled : bool
    variance_penalty : float      [0.5, 2.0] exponent on variance
    drawdown_halve_at : float     Drawdown % that halves position [0.05, 0.20]
    drawdown_stop_at : float      Drawdown % that stops trading [0.15, 0.40]
    max_per_station : float       [0.10, 0.50]
    max_per_signal : float        [0.05, 0.30]
    """

    def __init__(self, meta_config: Dict[str, Any]):
        self.cfg = meta_config
        self.state = LeverState(
            capital=float(meta_config.get("capital_base", 10000.0)),
            peak_capital=float(meta_config.get("capital_base", 10000.0)),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_position(
        self,
        signal_name: str,
        station: str,
        direction: str,
        confidence: float,
        edge: float,
        market_price: float = 0.5,
        variance: float = 0.25,
    ) -> int:
        """Run all 4 stages and return n_contracts (>=0).

        Returns 0 if any stage zeroes out the position (e.g. stop-loss hit).
        """
        n = self._stage1_kelly_base(signal_name, station, direction, confidence, edge, market_price)
        if n <= 0:
            return 0

        n = self._stage2_variance_adjustment(n, confidence, variance)
        if n <= 0:
            return 0

        n = self._stage3_drawdown_multiplier(n)
        if n <= 0:
            return 0

        n = self._stage4_concentration_cap(n, signal_name, station)
        return max(0, n)

    def record_trade_outcome(self, pnl: float, station: str, signal_name: str) -> None:
        """Update internal state after a trade is executed."""
        self.state.capital += pnl
        self.state.peak_capital = max(self.state.peak_capital, self.state.capital)

        dd = (self.state.peak_capital - self.state.capital) / self.state.peak_capital if self.state.peak_capital > 0 else 0.0
        self.state.current_drawdown_pct = dd

        # Track exposure
        self.state.station_exposure[station] = self.state.station_exposure.get(station, 0.0) + abs(pnl)
        self.state.signal_exposure[signal_name] = self.state.signal_exposure.get(signal_name, 0.0) + abs(pnl)

    def reset_state(self, capital: Optional[float] = None) -> None:
        """Reset drawdown and exposure tracking (e.g., between sweep configs)."""
        reset_cap = capital if capital is not None else float(self.cfg.get("capital_base", 10000.0))
        self.state = LeverState(capital=reset_cap, peak_capital=reset_cap)

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage1_kelly_base(
        self,
        signal_name: str,
        station: str,
        direction: str,
        confidence: float,
        edge: float,
        market_price: float,
    ) -> int:
        """Stage 1: Continuous Kelly from core.continuous_kelly.

        Falls back to discrete Kelly if the continuous function is unavailable
        or returns 0.
        """
        kelly_frac = float(self.cfg.get("kelly_fraction", 0.5))
        max_contracts = int(self.cfg.get("max_contracts", 500))

        try:
            from core.continuous_kelly import fee_aware_kelly
            n = fee_aware_kelly(
                pred_price=confidence,
                market_price=market_price,
                confidence=confidence,
                direction=direction,
                kelly_frac=kelly_frac,
                max_contracts=max_contracts,
                capital=self.state.capital,
                station=station,
                signal_name=signal_name,
            )
            return max(0, int(n))
        except Exception:
            pass

        # Fallback: discrete Kelly
        if edge <= 0 or market_price >= 1.0 or market_price <= 0.0:
            return 0
        kelly_pct = edge / (1.0 - market_price)
        n = int(min(max_contracts, kelly_pct * kelly_frac * (self.state.capital / 1000.0)))
        return max(1, n)

    def _stage2_variance_adjustment(
        self,
        n_contracts: int,
        confidence: float,
        variance: float,
    ) -> int:
        """Stage 2: Apply variance-adjusted Kelly scaling.

        Uses variance_adjusted_kelly() from core.variance_weighted_sizing
        when enabled. Otherwise uses a simple hyperbolic penalty.
        """
        if not self.cfg.get("variance_sizing_enabled", False):
            return n_contracts

        variance_penalty = float(self.cfg.get("variance_penalty", 1.0))

        try:
            from core.variance_weighted_sizing import variance_adjusted_kelly
            edge = abs(confidence - 0.5) * 2.0  # simple edge proxy
            n = variance_adjusted_kelly(
                capital=self.state.capital,
                edge=edge,
                variance=float(variance) * variance_penalty,
                base_kelly=float(self.cfg.get("kelly_fraction", 0.5)),
                aggressiveness_k=2.0,
                floor_multiplier=0.1,
                max_position_fraction=0.25,
            )
            return max(0, int(n))
        except Exception:
            pass

        # Fallback: hyperbolic penalty 1/(1 + k * var)
        penalty = 1.0 / (1.0 + 2.0 * float(variance) * variance_penalty)
        return max(0, int(n_contracts * penalty))

    def _stage3_drawdown_multiplier(self, n_contracts: int) -> int:
        """Stage 3: Halve or stop based on current drawdown."""
        halve_at = float(self.cfg.get("drawdown_halve_at", 0.10))
        stop_at = float(self.cfg.get("drawdown_stop_at", 0.25))
        dd = self.state.current_drawdown_pct

        if dd >= stop_at:
            return 0  # stopped
        if dd >= halve_at:
            return max(0, int(n_contracts * 0.5))
        return n_contracts

    def _stage4_concentration_cap(self, n_contracts: int, signal_name: str, station: str) -> int:
        """Stage 4: Cap position size as % of capital to prevent over-concentration."""
        max_station = float(self.cfg.get("max_per_station", 0.30))
        max_signal = float(self.cfg.get("max_per_signal", 0.15))

        station_total = self.state.station_exposure.get(station, 0.0)
        signal_total = self.state.signal_exposure.get(signal_name, 0.0)

        # Notional value of proposed position
        notional = float(n_contracts * 1.0)  # each contract = $1 max

        # Station cap: don't exceed max_station% of capital in any station
        station_cap = int(self.state.capital * max_station) if self.state.capital > 0 else 0
        station_remaining = max(0, station_cap - int(station_total))
        n = min(n_contracts, station_remaining)

        # Signal cap: don't exceed max_signal% of capital in any signal
        signal_cap = int(self.state.capital * max_signal) if self.state.capital > 0 else 0
        signal_remaining = max(0, signal_cap - int(signal_total))
        n = min(n, signal_remaining)

        return max(0, n)


# Convenience wrapper
def compute_levered_position(
    meta_config: Dict[str, Any],
    signal_name: str,
    station: str,
    direction: str,
    confidence: float,
    edge: float,
    market_price: float = 0.5,
    variance: float = 0.25,
    lever_state: Optional[LeverManager] = None,
) -> Tuple[int, LeverManager]:
    """One-shot wrapper that creates or reuses a LeverManager."""
    if lever_state is None:
        lever_state = LeverManager(meta_config)
    n = lever_state.compute_position(signal_name, station, direction, confidence, edge, market_price, variance)
    return n, lever_state