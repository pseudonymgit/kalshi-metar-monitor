#!/usr/bin/env python3
"""
Warm Momentum Signal — Detects sustained warm temperature momentum
using multi-day temperature trends.

B-Mode compliant. No AI/ML.
"""
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, _safe_get, validate_signal


class WarmMomentumSignal(BaseSignal):
    """Detects sustained warm temperature momentum."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.momentum_window = 3

    @property
    def name(self) -> str:
        return "warm_momentum"

    @property
    def min_lookback(self) -> int:
        return self.momentum_window + 1

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate warm momentum signal."""
        if idx < self.min_lookback:
            return None, 0.0

        # Check consecutive warming days
        warming_days = 0
        for i in range(max(1, idx - self.momentum_window), idx):
            prev_high = _safe_get(days, i - 1, 'high')
            curr_high = _safe_get(days, i, 'high')
            if prev_high is not None and curr_high is not None and curr_high > prev_high:
                warming_days += 1

        if warming_days >= 2:
            return 'up', 0.5

        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """Station-level evaluation."""
        return None, 0.5