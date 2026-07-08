#!/usr/bin/env python3
"""
Goldilocks Signal (R4-1.5)

Confidence-split signal with asymmetric up/down thresholds.
Z-score from 30-day mean, with tighter down threshold.
"""

import math
from typing import Optional, Tuple, List
import numpy as np
from .base_signal import BaseSignal


class GoldilocksSignal(BaseSignal):
    """Goldilocks signal — asymmetric up/down confidence thresholds."""

    @property
    def name(self) -> str:
        return "Goldilocks [R4-1.5]"

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
        if z > 1.0:
            conf = min(0.40 + abs(z) * 0.10, 0.75)
            return 'down', conf
        elif z < -1.0:
            conf = min(0.25 + abs(z) * 0.08, 0.60)
            return 'up', conf
        return None, 0.0
