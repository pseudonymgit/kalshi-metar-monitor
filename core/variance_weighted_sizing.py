"""
Variance-Weighted Position Sizing — First-Principles (FP 6.1)

Hyperbolic 1/(1 + k * σ²_total) formulation from Bayesian decision theory.

Two variance sources:
    σ²_member = p̂ * (1-p̂) / 0.25  (ensemble member spread, normalized to [0,1])
    σ²_signal = inter-signal disagreement (normalized to [0,1])
    σ²_total = 0.6 * σ²_member + 0.4 * σ²_signal (weighted combination)

Formula:
    kelly_multiplier = 1.0 / (1.0 + k * σ²_total)
    f_final = edge * kelly_multiplier * 0.5  (half-Kelly)

Where k ∈ [0.5, 5.0], default 2.0.

Usage:
    from core.variance_weighted_sizing import VarianceWeightedSizer

    sizer = VarianceWeightedSizer()
    multiplier = sizer.compute_kelly_multiplier(ensemble_fraction=0.72,
                                                 signal_probabilities=[0.68, 0.75, 0.70])
    size = sizer.variance_weighted_size("gaussian", "KATL", 100.0, ...)

B-Mode R8 Cycle 4.5: Hyperbolic formulation replacing linear 1.0-disagreement.
"""

import math
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default parameters
DEFAULT_K = 2.0          # Aggressiveness parameter
FLOOR_MULTIPLIER = 0.1   # Floor on kelly_multiplier (min position)
WEIGHT_MEMBER = 0.6      # Weight for ensemble member spread
WEIGHT_SIGNAL = 0.4      # Weight for inter-signal disagreement
FRACTIONAL_KELLY = 0.5   # Half-Kelly for safety
ROUND_TRIP_FEE = 0.02    # Kalshi fee per contract ($0.02)


def compute_ensemble_variance(ensemble_fraction: float) -> float:
    """
    Compute σ²_member from ensemble member spread (FP 6.1 Section 2.1).

    Args:
        ensemble_fraction: p̂ = mean of member predictions (0.0-1.0)

    Returns:
        Normalized variance [0, 1]. 0 at extremes, 1 at p̂=0.5.
    """
    p = max(0.0, min(1.0, ensemble_fraction))
    # Bernoulli variance p*(1-p), normalized by max (0.25)
    variance = p * (1.0 - p)
    return variance / 0.25  # Normalize to [0, 1]


def compute_inter_signal_variance(signal_probabilities: List[float]) -> float:
    """
    Compute σ²_signal from inter-signal disagreement (FP 6.1 Section 2.2).

    Args:
        signal_probabilities: List of calibrated probabilities from active signals

    Returns:
        Normalized variance [0, 1]. 0 with perfect consensus, 1 with maximal disagreement.
    """
    # Filter None/missing
    active = [p for p in signal_probabilities if p is not None]
    if len(active) < 2:
        return 0.0  # No disagreement possible with < 2 signals

    # Compute variance of signal probabilities
    mean_p = sum(active) / len(active)
    variance = sum((p - mean_p) ** 2 for p in active) / len(active)

    # Normalize by max theoretical variance (0.25)
    return min(1.0, variance / 0.25)


def compute_kelly_multiplier(
    sigma2_total: float,
    k: float = DEFAULT_K,
    floor: float = FLOOR_MULTIPLIER,
) -> float:
    """
    Compute kelly_multiplier from total estimation variance (FP 6.1 Section 3).

    Formula: 1 / (1 + k * σ²_total), clamped to [floor, 1.0].

    Args:
        sigma2_total: Combined estimation variance [0, 1]
        k: Aggressiveness parameter (default 2.0)
        floor: Minimum multiplier (default 0.1)

    Returns:
        Kelly multiplier [floor, 1.0]
    """
    s2 = max(0.0, min(1.0, sigma2_total))
    raw = 1.0 / (1.0 + k * s2)
    return max(floor, raw)


def compute_total_variance(
    ensemble_fraction: float,
    signal_probabilities: Optional[List[float]] = None,
    w_member: float = WEIGHT_MEMBER,
    w_signal: float = WEIGHT_SIGNAL,
) -> float:
    """
    Compute combined estimation variance from both sources (FP 6.1 Section 2.3).

    Args:
        ensemble_fraction: GEFS ensemble fraction p̂
        signal_probabilities: List of secondary signal probabilities
        w_member: Weight for ensemble spread (default 0.6)
        w_signal: Weight for inter-signal disagreement (default 0.4)

    Returns:
        σ²_total ∈ [0, 1]
    """
    var_member = compute_ensemble_variance(ensemble_fraction)

    var_signal = 0.0
    if signal_probabilities and len(signal_probabilities) >= 2:
        var_signal = compute_inter_signal_variance(signal_probabilities)

    return w_member * var_member + w_signal * var_signal


def compute_variance_weighted_edge(
    probability: float,
    cost: float,
    sigma2_total: float,
    k: float = DEFAULT_K,
) -> Tuple[float, float]:
    """
    Compute edge adjusted by estimation variance (FP 6.1 Section 5).

    Args:
        probability: Estimated exceedance probability p̂
        cost: Cost per contract as fraction of $1 (e.g., 0.52 for 52¢)
        sigma2_total: Combined estimation variance
        k: Aggressiveness parameter

    Returns:
        Tuple of (edge, kelly_multiplier)
    """
    multiplier = compute_kelly_multiplier(sigma2_total, k=k)
    edge_per_contract = probability - cost - ROUND_TRIP_FEE
    edge = edge_per_contract * multiplier * FRACTIONAL_KELLY
    return edge, multiplier


class VarianceWeightedSizer:
    """
    Variance-weighted position sizing using the hyperbolic 1/(1+kσ²) formulation.

    Manages rolling variance estimates per (signal, station) pair.
    """

    def __init__(self, k: float = DEFAULT_K):
        self.k = k
        # Cache: {(signal, station): list of recent outcomes}
        self._outcomes: Dict[Tuple[str, str], List[float]] = {}
        # Cache: {(signal, station): variance estimate}
        self._variance_cache: Dict[Tuple[str, str], float] = {}
        # Max history per signal/station
        self._max_history = 500

    def compute_multiplier_from_fraction(
        self,
        ensemble_fraction: float,
        signal_probabilities: Optional[List[float]] = None,
    ) -> float:
        """
        Quick calculation: kelly multiplier from ensemble fraction + optional signals.

        Args:
            ensemble_fraction: GEFS ensemble fraction p̂
            signal_probabilities: Optional list of signal probabilities

        Returns:
            Kelly multiplier [0.1, 1.0]
        """
        s2 = compute_total_variance(ensemble_fraction, signal_probabilities)
        return compute_kelly_multiplier(s2, k=self.k)

    def variance_weighted_size(
        self,
        signal_name: str,
        station: str,
        base_size: float,
        confidences: Dict[str, float],
        signal_probabilities: Optional[List[float]] = None,
    ) -> float:
        """
        Compute position size adjusted for signal variance.

        Args:
            signal_name: Canonical signal name
            station: ICAO station code
            base_size: Base position size in dollars
            confidences: Dict of signal_name -> confidence values
            signal_probabilities: Optional list of all active signal probabilities for inter-signal variance

        Returns:
            Adjusted position size in dollars
        """
        # Get rolling variance for this signal/station
        rolling_variance = self.get_variance(signal_name, station)

        # Get member spread variance from confidences
        ensemble_fraction = confidences.get('ensemble_fraction', 0.5)
        var_member = compute_ensemble_variance(ensemble_fraction)

        # Inter-signal variance
        var_signal = 0.0
        if signal_probabilities and len(signal_probabilities) >= 2:
            var_signal = compute_inter_signal_variance(signal_probabilities)

        # Combined variance (weighted + rolling)
        sigma2_total = (
            0.5 * (WEIGHT_MEMBER * var_member + WEIGHT_SIGNAL * var_signal)
            + 0.5 * rolling_variance
        )
        sigma2_total = min(1.0, sigma2_total)

        # Compute multiplier
        multiplier = compute_kelly_multiplier(sigma2_total, k=self.k)

        adjusted_size = base_size * multiplier
        logger.debug(
            "Variance-weighted size: signal=%s station=%s base=%.2f "
            "var_member=%.3f var_signal=%.3f rolling=%.3f s2=%.3f "
            "mult=%.3f adjusted=%.2f",
            signal_name, station, base_size,
            var_member, var_signal, rolling_variance,
            sigma2_total, multiplier, adjusted_size
        )
        return adjusted_size

    def record_outcome(self, signal_name: str, station: str, value: float) -> None:
        """
        Record an outcome for rolling variance calculation.

        Args:
            signal_name: Canonical signal name
            station: ICAO station code
            value: Outcome (e.g., P&L, confidence, or correctness bit)
        """
        key = (signal_name, station)
        if key not in self._outcomes:
            self._outcomes[key] = []
        self._outcomes[key].append(value)

        # Prune old history
        if len(self._outcomes[key]) > self._max_history:
            self._outcomes[key] = self._outcomes[key][-self._max_history:]

        # Invalidate variance cache
        self._variance_cache.pop(key, None)

    def get_variance(self, signal_name: str, station: str) -> float:
        """
        Get rolling variance estimate for a (signal, station) pair.

        Computed from recent outcomes using population variance.
        Returns 0.0 if insufficient data.
        """
        key = (signal_name, station)
        if key in self._variance_cache:
            return self._variance_cache[key]

        outcomes = self._outcomes.get(key, [])
        if len(outcomes) < 5:
            return 0.0

        variance = self._compute_variance(outcomes)
        self._variance_cache[key] = variance
        return variance

    def get_weight(self, signal_name: str, station: str) -> float:
        """
        Get variance weight (inverse of variance, normalized).

        Returns weight in [0, 1] where lower variance = higher weight.
        """
        variance = self.get_variance(signal_name, station)
        return 1.0 / (1.0 + variance * 10.0)  # Inverse relationship

    def get_all_weights(self) -> Dict[Tuple[str, str], float]:
        """Get all current variance weights."""
        return {
            key: self.get_weight(*key)
            for key in self._outcomes
        }

    def get_all_variances(self) -> Dict[Tuple[str, str], float]:
        """Get all current variance estimates."""
        return {
            key: self.get_variance(*key)
            for key in self._outcomes
        }

    def _compute_variance(self, outcomes: List[float]) -> float:
        """Compute population variance of a list of values."""
        if len(outcomes) < 2:
            return 0.0
        mean = sum(outcomes) / len(outcomes)
        return sum((v - mean) ** 2 for v in outcomes) / len(outcomes)


# ─────────────────────────────────────────────────────────────────────
# Legacy compatibility interface
# ─────────────────────────────────────────────────────────────────────

_sizer: Optional[VarianceWeightedSizer] = None


def get_sizer() -> VarianceWeightedSizer:
    """Get the module-level singleton sizer."""
    global _sizer
    if _sizer is None:
        _sizer = VarianceWeightedSizer()
    return _sizer


def compute_disagreement(ensemble_fraction: float,
                          signal_probabilities: Optional[List[float]] = None) -> float:
    """
    Legacy: compute disagreement (1 - sigma2_total) from ensemble fraction.

    Returns 0.0 (high confidence) to 1.0 (max disagreement).
    """
    sigma2 = compute_total_variance(ensemble_fraction, signal_probabilities)
    return min(1.0, sigma2)  # Disagreement = variance


def compute_kelly_multiplier_from_disagreement(disagreement: float) -> float:
    """
    Legacy: compute Kelly multiplier from disagreement value.

    Replaced the old linear (1.0 - disagreement) with hyperbolic 1/(1 + kσ²).
    """
    # Treat disagreement directly as variance
    return compute_kelly_multiplier(disagreement, k=DEFAULT_K)