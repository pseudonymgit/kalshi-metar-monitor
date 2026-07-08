#!/usr/bin/env python3
"""
Gaussian v2 Signal (30-day lookback)

Tighter z-score threshold (0.5) with shorter window for more responsive signals.
"""

import math
from typing import Optional, Tuple, List
import numpy as np
from .base_signal import BaseSignal


class GaussianV2Signal(BaseSignal):
    """Gaussian v2 mean reversion — 30-day window, z > 0.5."""

    @property
    def name(self) -> str:
        return "Gaussian v2 (30d)"

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
