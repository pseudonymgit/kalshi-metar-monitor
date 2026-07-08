#!/usr/bin/env python3
"""
Pressure Signal (delta-based)

Uses day-over-day pressure change to predict temperature direction.
Rising pressure → up, falling pressure → down.
"""

from typing import Optional, Tuple, List
from .base_signal import BaseSignal


class PressureSignal(BaseSignal):
    """Pressure delta signal — 2-day pressure change."""

    @property
    def name(self) -> str:
        return "Pressure (delta)"

    @property
    def min_lookback(self) -> int:
        return 3

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        if idx < 3:
            return None, 0.0
        dp = days[idx - 1]['pressure'] - days[idx - 2]['pressure']
        if abs(dp) > 2.0:
            return ('up' if dp > 0 else 'down'), min(abs(dp) / 5.0, 0.8)
        return None, 0.0
