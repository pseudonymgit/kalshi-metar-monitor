#!/usr/bin/env python3
"""
Cooling Failure Signal — Detects when expected nighttime cooling fails to materialize,
indicating warm advection or cloud cover retention.

B-Mode compliant. No AI/ML.
"""
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, _safe_get, validate_signal


class CoolingFailureSignal(BaseSignal):
    """Detects cooling failure — nighttime low does not drop as expected."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "cooling_failure"

    @property
    def min_lookback(self) -> int:
        return 2

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate cooling failure signal."""
        if idx < self.min_lookback:
            return None, 0.0

        prev_low = _safe_get(days, idx - 2, 'low')
        curr_low = _safe_get(days, idx - 1, 'low')
        prev_high = _safe_get(days, idx - 2, 'high')
        curr_high = _safe_get(days, idx - 1, 'high')

        if None in (prev_low, curr_low, prev_high, curr_high):
            return None, 0.0

        # Expected cooling: high-to-low range should be at least 10°F
        expected_drop = prev_high - prev_low
        actual_drop = curr_high - curr_low

        if actual_drop < expected_drop * 0.5 and actual_drop < 5.0:
            # Cooling failure detected — expect continued warmth
            return 'up', 0.4

        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """Station-level evaluation."""
        return None, 0.5