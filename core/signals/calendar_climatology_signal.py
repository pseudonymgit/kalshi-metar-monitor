#!/usr/bin/env python3
"""
Calendar Climatology Signal (60-day lookback)

Uses a 60-day rolling mean and z-score to detect when temperature
is far from the recent seasonal norm.
"""

import math
from typing import Optional, Tuple, List
import numpy as np
from .base_signal import BaseSignal


class CalendarClimatologySignal(BaseSignal):
    """Calendar climatology — 60-day rolling mean reversion."""

    @property
    def name(self) -> str:
        return "Calendar climatology (60d)"

    @property
    def min_lookback(self) -> int:
        return 60

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        if idx < 60:
            return None, 0.0
        window = days[idx - 60:idx - 1]
        highs = [d['high'] for d in window]
        mean = sum(highs) / len(highs)
        std = math.sqrt(np.var(highs, ddof=1)) if len(highs) > 1 else 0.01
        if std == 0:
            return None, 0.0
        z = (days[idx - 1]['high'] - mean) / std
        if z > 1.5:
            return 'down', min(abs(z) / 3.0, 0.6)
        elif z < -1.5:
            return 'up', min(abs(z) / 3.0, 0.6)
        return None, 0.0
