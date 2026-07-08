#!/usr/bin/env python3
"""
Gaussian Signal (48-day lookback)

Similar to reversion but with a 48-day window and tighter z-score threshold.
"""

import math
from typing import Optional, Tuple, List
import numpy as np
from .base_signal import BaseSignal


class GaussianSignal(BaseSignal):
    """Gaussian mean reversion — 48-day window, z > 1.0."""

    @property
    def name(self) -> str:
        return "Gaussian (48d)"

    @property
    def min_lookback(self) -> int:
        return 48

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        if idx < 48:
            return None, 0.0
        window = days[idx - 48:idx - 1]
        highs = [d['high'] for d in window]
        mean = sum(highs) / len(highs)
        var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
        std = math.sqrt(var) if var > 0 else 0.01
        z = (days[idx - 1]['high'] - mean) / std if std > 0 else 0
        if z > 1.0:
            return 'down', abs(z)
        elif z < -1.0:
            return 'up', abs(z)
        return None, 0.0
