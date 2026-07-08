#!/usr/bin/env python3
"""
Regime Signal (DTR-scaled, 15-day lookback)

Uses recent volatility regime and DTR (diurnal temperature range) to
set adaptive thresholds for mean-reversion predictions.
"""

import math
from typing import Optional, Tuple, List
import numpy as np
from .base_signal import BaseSignal


class RegimeSignal(BaseSignal):
    """Regime-adaptive threshold signal — 15-day vol + DTR scaling."""

    @property
    def name(self) -> str:
        return "Regime (DTR-scaled)"

    @property
    def min_lookback(self) -> int:
        return 31

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        if idx < 15:
            return None, 0.0
        window = days[idx - 15:idx - 1]
        highs = [d['high'] for d in window]
        mean = sum(highs) / len(highs)
        var = np.var(highs, ddof=1) if len(highs) > 1 else 0.01
        vol = math.sqrt(var)
        slope = (highs[-1] - highs[0]) / len(highs) if len(highs) >= 2 else 0

        if idx >= 1:
            dtr = days[idx - 1]['high'] - days[idx - 1]['low']
        else:
            dtr = 10.0

        if dtr > 15.0:
            threshold = 1.0
        elif dtr < 8.0:
            threshold = 0.4
        else:
            threshold = 0.8

        if vol < 1.0 and abs(slope) < threshold:
            if idx >= 31:
                w30 = days[idx - 31:idx - 1]
                h30 = [d['high'] for d in w30]
                m30 = sum(h30) / len(h30)
                dist = days[idx - 1]['high'] - m30
                if dist > 1.0:
                    conf = min(dist / 3.0, 0.8)
                    if dtr < 8.0:
                        conf *= 0.6
                    return 'down', conf
                elif dist < -1.0:
                    conf = min(abs(dist) / 3.0, 0.8)
                    if dtr < 8.0:
                        conf *= 0.6
                    return 'up', conf
        return None, 0.0
