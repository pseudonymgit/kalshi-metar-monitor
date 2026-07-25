#!/usr/bin/env python3
"""
C8 — Variance-Weighted Ensemble Position Sizing

Implements variance-weighted position sizing for the signal ensemble.
Lower-variance signals get higher weight in position sizing.

Key design:
- Rolling 30-day variance estimate per signal per station
- Variance weight = 1 / (variance + epsilon) normalized across signals
- Base size scaled by variance weight, not by raw confidence alone
- Interface: variance_weighted_size(signal_name, station, base_size, confidences)

Usage:
    from core.variance_weighted_sizing import VarianceWeightedSizer

    sizer = VarianceWeightedSizer()
    size = sizer.variance_weighted_size("gaussian", "KATL", 100.0, {"gaussian": 0.70, "calendar_climatology": 0.65})
    # Returns position size adjusted for signal variance
"""

import json
import logging
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────

DEFAULT_CACHE_FILE = "data/variance_weights_cache.json"
DEFAULT_ROLLING_WINDOW = 30  # days
EPSILON = 1e-6  # Small constant to avoid division by zero
MIN_VARIANCE = 0.01  # Floor for variance estimate
MAX_WEIGHT_RATIO = 5.0  # Max ratio between highest and lowest weight (prevents over-concentration)
DEFAULT_WEIGHT = 1.0  # Default weight for signals with insufficient history


class VarianceWeightedSizer:
    """
    Rolls a 30-day variance estimate per signal per station and uses
    it to compute position sizing weights.

    Signals with lower variance (more consistent outcomes) get higher
    weights. Signals with higher variance (less reliable) get lower weights.
    """

    def __init__(
        self,
        rolling_window: int = DEFAULT_ROLLING_WINDOW,
        cache_file: str = DEFAULT_CACHE_FILE,
    ):
        self.rolling_window = rolling_window
        self.cache_file = Path(cache_file)

        # Per-signal per-station outcome history
        # {signal_name: {station: [(timestamp, outcome_value), ...]}}
        self._outcome_history: Dict[str, Dict[str, List[Tuple[float, float]]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # Cached variance estimates
        self._variance_cache: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._cache_timestamp = 0.0
        self._cache_ttl = 3600  # 1 hour

        self._load_cache()

    # ── Public API ────────────────────────────────────────────

    def record_outcome(
        self,
        signal_name: str,
        station: str,
        outcome_value: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """
        Record a trade outcome for variance estimation.

        Args:
            signal_name: Signal name (e.g. 'gaussian', 'spike_reversion')
            station: Station code (e.g. 'KATL')
            outcome_value: Numeric outcome (1.0 = correct, 0.0 = incorrect,
                           or actual P&L as a fraction)
            timestamp: Unix timestamp (defaults to now)
        """
        if timestamp is None:
            timestamp = time.time()

        self._outcome_history[signal_name][station].append((timestamp, outcome_value))
        self._prune(signal_name, station)
        self._invalidate_cache()

    def get_variance(
        self,
        signal_name: str,
        station: str,
    ) -> float:
        """
        Get the rolling variance estimate for a signal and station.

        Args:
            signal_name: Signal name
            station: Station code

        Returns:
            Variance estimate (lower = more consistent)
        """
        self._refresh_cache()
        return self._variance_cache.get(signal_name, {}).get(station, 0.25)

    def get_weight(
        self,
        signal_name: str,
        station: str,
    ) -> float:
        """
        Get the variance-based weight for a signal and station.

        Weight = 1 / (variance + epsilon), normalized so that the average
        weight across all available signals is 1.0.

        Args:
            signal_name: Signal name
            station: Station code

        Returns:
            Weight multiplier (higher = more weight)
        """
        variance = self.get_variance(signal_name, station)
        return 1.0 / (variance + EPSILON)

    def variance_weighted_size(
        self,
        signal_name: str,
        station: str,
        base_size: float,
        all_signal_confidences: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Compute the variance-weighted position size for a signal.

        The base_size is scaled by the signal's variance weight relative
        to the ensemble average weight.

        Args:
            signal_name: Signal name to size
            station: Station code
            base_size: Base position size in dollars (from Kelly or config)
            all_signal_confidences: Dict of {signal_name: confidence} for
                                   normalization. If None, uses global average.

        Returns:
            Adjusted position size in dollars
        """
        self._refresh_cache()

        # Get variance weight for this signal
        weight = self.get_weight(signal_name, station)

        # Compute ensemble average weight for normalization
        if all_signal_confidences:
            # Normalize against active signals in this trade
            weights = []
            for sig in all_signal_confidences:
                w = self.get_weight(sig, station)
                weights.append(w)
            avg_weight = sum(weights) / len(weights) if weights else 1.0
        else:
            # Use global average of all known signal-station pairs
            all_weights = []
            for sig_name, stations in self._variance_cache.items():
                for stn, _ in stations.items():
                    all_weights.append(self.get_weight(sig_name, stn))
            avg_weight = sum(all_weights) / len(all_weights) if all_weights else 1.0

        # Compute relative weight and apply to base size
        relative_weight = weight / avg_weight if avg_weight > 0 else 1.0

        # Clamp to prevent extreme values
        relative_weight = max(
            1.0 / MAX_WEIGHT_RATIO,
            min(MAX_WEIGHT_RATIO, relative_weight),
        )

        return base_size * relative_weight

    def get_all_weights(
        self,
        station: str,
    ) -> Dict[str, float]:
        """
        Get all variance weights for a station across all signals.

        Args:
            station: Station code

        Returns:
            Dict of {signal_name: weight}
        """
        self._refresh_cache()
        weights = {}
        for signal_name in self._variance_cache:
            w = self.get_weight(signal_name, station)
            weights[signal_name] = w
        return weights

    def get_all_variances(
        self,
        station: str,
    ) -> Dict[str, float]:
        """
        Get all variance estimates for a station across all signals.

        Args:
            station: Station code

        Returns:
            Dict of {signal_name: variance}
        """
        self._refresh_cache()
        result = {}
        for signal_name, stations in self._variance_cache.items():
            if station in stations:
                result[signal_name] = stations[station]
        return result

    # ── Internal ──────────────────────────────────────────────

    def _compute_variance(self, outcomes: List[float]) -> float:
        """
        Compute the variance of a list of outcome values.

        Args:
            outcomes: List of numeric outcome values

        Returns:
            Variance (biased, N-based)
        """
        if len(outcomes) < 3:
            return 0.25  # Default: maximum uncertainty

        n = len(outcomes)
        mean = sum(outcomes) / n
        variance = sum((x - mean) ** 2 for x in outcomes) / n

        return max(MIN_VARIANCE, variance)

    def _prune(self, signal_name: str, station: str) -> None:
        """Remove outcomes older than the rolling window."""
        cutoff = time.time() - (self.rolling_window * 86400)
        history = self._outcome_history[signal_name][station]
        self._outcome_history[signal_name][station] = [
            (ts, val) for ts, val in history if ts >= cutoff
        ]

    def _invalidate_cache(self) -> None:
        """Mark the variance cache as stale."""
        self._cache_timestamp = 0.0

    def _refresh_cache(self) -> None:
        """Rebuild the variance cache if stale."""
        if time.time() - self._cache_timestamp < self._cache_ttl:
            return

        for signal_name, stations in self._outcome_history.items():
            for station, history in stations.items():
                if len(history) < 3:
                    continue
                outcomes = [val for _, val in history]
                variance = self._compute_variance(outcomes)
                self._variance_cache[signal_name][station] = variance

        self._cache_timestamp = time.time()
        self._save_cache()

    def _load_cache(self) -> None:
        """Load cached variance estimates."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                raw = data.get("variance_cache", {})
                for sig, stations in raw.items():
                    for stn, var in stations.items():
                        self._variance_cache[sig][stn] = var
                self._cache_timestamp = data.get("timestamp", 0.0)

                # Also load outcome history
                raw_history = data.get("outcome_history", {})
                for sig, stations in raw_history.items():
                    for stn, outcomes in stations.items():
                        self._outcome_history[sig][stn] = [
                            (t, v) for t, v in outcomes
                        ]
        except Exception as e:
            logger.debug(f"Failed to load variance cache: {e}")

    def _save_cache(self) -> None:
        """Save variance estimates to cache."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            serializable = {}
            for sig, stations in self._variance_cache.items():
                serializable[sig] = dict(stations)

            history_serializable = {}
            for sig, stations in self._outcome_history.items():
                history_serializable[sig] = {}
                for stn, outcomes in stations.items():
                    history_serializable[sig][stn] = [[t, v] for t, v in outcomes]

            with open(self.cache_file, "w") as f:
                json.dump({
                    "timestamp": time.time(),
                    "variance_cache": serializable,
                    "outcome_history": history_serializable,
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save variance cache: {e}")


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation."""
    sizer = VarianceWeightedSizer(cache_file="/tmp/test_variance_sizing.json")

    # Test 1: Default weight for unknown signal
    w = sizer.get_weight("unknown", "KATL")
    assert abs(w - 4.0) < 0.01, f"Expected 4.0 (1/0.25), got {w}"
    print(f"Test 1 PASS: default weight = {w:.2f}")

    # Test 2: Record low-variance signal (all correct)
    now = time.time()
    for i in range(30):
        sizer.record_outcome("gaussian", "KATL", 1.0, timestamp=now - i * 86400)
    v_low = sizer.get_variance("gaussian", "KATL")
    assert v_low < 0.02, f"Expected low variance (<0.02), got {v_low}"
    w_low = sizer.get_weight("gaussian", "KATL")
    print(f"Test 2 PASS: low-variance signal var={v_low:.4f} weight={w_low:.2f}")

    # Test 3: Record high-variance signal (mixed 50/50)
    for i in range(30):
        outcome = 1.0 if i % 2 == 0 else 0.0
        sizer.record_outcome("noise", "KATL", outcome, timestamp=now - i * 86400)
    v_high = sizer.get_variance("noise", "KATL")
    assert v_high > 0.1, f"Expected high variance (>0.1), got {v_high}"
    w_high = sizer.get_weight("noise", "KATL")
    print(f"Test 3 PASS: high-variance signal var={v_high:.4f} weight={w_high:.2f}")

    # Test 4: Variance-weighted size
    size = sizer.variance_weighted_size(
        "gaussian", "KATL", 100.0,
        all_signal_confidences={"gaussian": 0.70, "noise": 0.55},
    )
    assert size > 100.0, f"Expected >100 (low var gets boost), got {size}"
    print(f"Test 4 PASS: variance-weighted size = ${size:.2f}")

    # Test 5: High-variance signal gets smaller size
    size_noise = sizer.variance_weighted_size(
        "noise", "KATL", 100.0,
        all_signal_confidences={"gaussian": 0.70, "noise": 0.55},
    )
    assert size_noise < 100.0, f"Expected <100 (high var gets penalty), got {size_noise}"
    print(f"Test 5 PASS: high-variance size = ${size_noise:.2f}")

    # Test 6: get_all_weights
    weights = sizer.get_all_weights("KATL")
    assert "gaussian" in weights
    assert "noise" in weights
    print(f"Test 6 PASS: all weights = {len(weights)} signals")

    # Test 7: get_all_variances
    variances = sizer.get_all_variances("KATL")
    assert "gaussian" in variances
    assert "noise" in variances
    print(f"Test 7 PASS: all variances = {len(variances)} signals")

    # Test 8: Cache persistence
    sizer2 = VarianceWeightedSizer(cache_file="/tmp/test_variance_sizing.json")
    v_cached = sizer2.get_variance("gaussian", "KATL")
    assert abs(v_cached - v_low) < 0.01, f"Cache mismatch: {v_cached} vs {v_low}"
    print(f"Test 8 PASS: cache persistence, var={v_cached:.4f}")

    # Cleanup
    import glob
    for f in glob.glob("/tmp/test_variance_sizing*"):
        try:
            os.remove(f)
        except OSError:
            pass

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()