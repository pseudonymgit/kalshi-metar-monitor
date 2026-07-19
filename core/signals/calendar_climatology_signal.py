#!/usr/bin/env python3
"""
SIGNAL: Calendar Climatology

60-day rolling window, z-score > 1.5 threshold. Longer-term view than
gaussian (48d) with stricter threshold and capped confidence.

Based on signal_calendar_climatology() from comprehensive_split_backtest.py.
"""
import math
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, _window, _safe_get, validate_signal


class CalendarClimatologySignal(BaseSignal):
    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.window = 60
        self.z_threshold = 1.5

    @property
    def name(self) -> str:
        return "calendar_climatology"

    @property
    def min_lookback(self) -> int:
        return self.window + 1

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """60-day climatology: trade only when z-score exceeds 1.5."""
        if idx < self.window + 1:
            return None, 0.0

        window = _window(days, idx, self.window, offset=1)
        if window is None:
            return None, 0.0

        highs = [d.get('high') for d in window if d.get('high') is not None]
        if len(highs) < 2:
            return None, 0.0

        mean = sum(highs) / len(highs)
        var = sum((h - mean) ** 2 for h in highs) / (len(highs) - 1) if len(highs) > 1 else 0.01
        std = math.sqrt(var) if var > 0 else 0.01

        current = _safe_get(days, idx - 1, 'high')
        if current is None:
            return None, 0.0

        z = (current - mean) / std

        if z > self.z_threshold:
            conf = min(abs(z) / 3.0, 0.6)
            return 'down', conf
        elif z < -self.z_threshold:
            conf = min(abs(z) / 3.0, 0.6)
            return 'up', conf

        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        return None, 0.0