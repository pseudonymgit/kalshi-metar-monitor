#!/usr/bin/env python3
"""
DirectionWeightedAdapter — per-signal directional-confidence asymmetry adapter.

Spec: PRE-RERUN-FULL-UNIVERSE-REMEDIATION-SPEC-2026-08-14, Section D.
Status: hypothesis-only until measured in controlled experiments (WS9 arms T3/T3a).

Formula (per spec D.2, verified by QC R3/R4):
    adjusted_log_odds[sig] = raw_log_odds[sig] * weight[sig]
    weight[sig] = 1.0 - min(max(asymmetry[sig], 0.0), 0.5)

Where:
    asymmetry[sig] = |down_confidence_mean[sig] - up_confidence_mean[sig]|
    over a rolling window of recent predictions (default: last 200 trades).

Boundary behavior:
    asymmetry == 0.0  -> weight == 1.0  (no-op; balanced directional confidence)
    asymmetry >= 0.5  -> weight == 0.5  (floor; heavily directionally imbalanced)

Monotonicity: weight decreases linearly from 1.0 to 0.5 as asymmetry rises
from 0.0 to 0.5 (D-G4).

The adapter operates AFTER log-odds conversion and AFTER calibration. It does
NOT modify raw confidence pre-conversion, does NOT change the calibration
pipeline, and does NOT change the confidence_floor check.

Multiple-comparison (predeclared family, spec D.5):
    4 comparisons (T3 vs T2, T3a vs T2, T3b vs T2, T11 vs T0)
    Bonferroni alpha' = 0.05/4 = 0.0125.
"""

import logging
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SIZE = 200
MAX_ASYMMETRY = 0.5          # clamp floor: weight == 0.5 at asymmetry >= 0.5
FLOOR_WEIGHT = 0.5


class DirectionWeightedAdapter:
    """Per-signal directional-confidence asymmetry adapter.

    Maintains a rolling window of (direction, confidence) pairs per signal
    and computes a multiplicative weight applied to each signal's log-odds
    during fusion.

    The adapter is stateful by design: it must observe a signal's recent
    predictions to estimate directional asymmetry. In evaluation, the caller
    is responsible for resetting history between folds/periods (or the
    adapter exposes reset() for that purpose).
    """

    def __init__(self, window_size: int = DEFAULT_WINDOW_SIZE):
        self.window_size = max(1, int(window_size))
        self._history: Dict[str, Deque[Tuple[str, float]]] = {}
        self._weights: Dict[str, float] = {}

    # ── History management ──────────────────────────────────────────────

    def record(self, signal_name: str, direction: str, confidence: float) -> None:
        """Record a prediction for a signal.

        Args:
            signal_name: signal identifier.
            direction: 'up' or 'down'.
            confidence: calibrated confidence for the predicted direction.
        """
        if signal_name not in self._history:
            self._history[signal_name] = deque(maxlen=self.window_size)
        self._history[signal_name].append((direction, float(confidence)))

    def reset(self) -> None:
        """Clear all history and weights (e.g., between folds/periods)."""
        self._history.clear()
        self._weights.clear()

    def reset_signal(self, signal_name: str) -> None:
        """Clear history/weights for one signal."""
        self._history.pop(signal_name, None)
        self._weights.pop(signal_name, None)

    # ── Asymmetry & weight computation ──────────────────────────────────

    def asymmetry(self, signal_name: str) -> float:
        """Return directional confidence asymmetry for a signal.

        asymmetry = |down_conf_mean - up_conf_mean| over the rolling window.
        Returns 0.0 if insufficient history (fewer than 2 predictions or
        only one direction observed).
        """
        history = self._history.get(signal_name)
        if not history or len(history) < 2:
            return 0.0

        up_conf_sum = 0.0
        down_conf_sum = 0.0
        up_n = 0
        down_n = 0
        for direction, confidence in history:
            if direction == "up":
                up_conf_sum += confidence
                up_n += 1
            elif direction == "down":
                down_conf_sum += confidence
                down_n += 1

        if up_n == 0 or down_n == 0:
            # Only one direction observed — that IS maximal directional
            # imbalance in practice; treat mean of the observed direction
            # vs 0 as the asymmetry (capped by MAX_ASYMMETRY below).
            observed_mean = (up_conf_sum if up_n else down_conf_sum) / max(up_n, down_n)
            return min(observed_mean, MAX_ASYMMETRY)

        up_mean = up_conf_sum / up_n
        down_mean = down_conf_sum / down_n
        return abs(down_mean - up_mean)

    def weight(self, signal_name: str) -> float:
        """Return adapter weight for a signal.

        weight = 1.0 - min(max(asymmetry, 0.0), 0.5)
        """
        asym = self.asymmetry(signal_name)
        clamped = min(max(asym, 0.0), MAX_ASYMMETRY)
        return 1.0 - clamped

    def compute_weights(self, signal_names: List[str]) -> Dict[str, float]:
        """Compute weights for all listed signals and cache them."""
        weights = {}
        for name in signal_names:
            w = self.weight(name)
            weights[name] = w
            logger.debug(
                "DirectionWeightedAdapter: %s asymmetry=%.4f weight=%.4f",
                name, self.asymmetry(name), w,
            )
        self._weights = weights
        return weights

    def get_weights(self) -> Dict[str, float]:
        """Return the last computed weights (empty if not yet computed)."""
        return dict(self._weights)

    def adjust_log_odds(
        self,
        signal_name: str,
        raw_log_odds: float,
        weight: Optional[float] = None,
    ) -> float:
        """Apply the adapter weight to a signal's log-odds.

        adjusted_log_odds = raw_log_odds * weight

        Args:
            signal_name: signal identifier.
            raw_log_odds: ln(conf/(1-conf)) AFTER calibration.
            weight: precomputed weight; if None, computed on the fly.

        Returns:
            adjusted log-odds.
        """
        if weight is None:
            weight = self.weight(signal_name)
        return raw_log_odds * weight

    # ── Reporting ───────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Dict[str, float]]:
        """Return per-signal asymmetry/weight summary for reporting."""
        return {
            name: {
                "asymmetry": round(self.asymmetry(name), 6),
                "weight": round(self.weight(name), 6),
                "n_history": len(hist),
            }
            for name, hist in self._history.items()
        }
