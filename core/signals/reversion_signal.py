#!/usr/bin/env python3
"""
Reversion Signal (30-day lookback)

Mean-reversion signal: if yesterday's high is far from the 30-day mean,
predict reversion. Confidence is the z-score magnitude.
"""

import math
from typing import Optional, Tuple, List
import numpy as np
from .base_signal import BaseSignal


class ReversionSignal(BaseSignal):
    """Reversion after settlement — 30-day mean reversion."""

    @property
    def name(self) -> str:
        return "Reversion (30d)"

    @property
    def min_lookback(self) -> int:
        return 31

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        if idx < 31:
            return None, 0.0
        window = days[idx - 31:idx - 1]
        highs = [d['high'] for d in window]
        mean = sum(highs) / len(highs)
        var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
        std = math.sqrt(var) if var > 0 else 0.01
        z = (days[idx - 1]['high'] - mean) / std if std > 0 else 0
        if z > 0.5:
            return 'down', abs(z)
        elif z < -0.5:
            return 'up', abs(z)
        return None, 0.0
