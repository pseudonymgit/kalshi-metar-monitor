#!/usr/bin/env python3
"""
SIGNAL: Regime (DTR-scaled)

Detects stable temperature regimes (low volatility, low slope) and trades
reversion to the 30-day mean when temperature deviates significantly.

Based on the original approach_regime() from ensemble_v8/v9.
"""
import math
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, _window, _safe_get, validate_signal


class RegimeSignal(BaseSignal):
    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.lookback_vol = 15  # Days for volatility/slope window
        self.lookback_mean = 30  # Days for mean reversion window

    @property
    def name(self) -> str:
        return "regime"

    @property
    def min_lookback(self) -> int:
        return self.lookback_mean + 1

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Regime-based signal: trade reversion only in stable regimes."""
        if idx < self.lookback_mean + 1:
            return None, 0.0

        # Volatility/slope window (15 days before idx-1)
        vol_window = _window(days, idx, self.lookback_vol, offset=1)
        if vol_window is None:
            return None, 0.0

        vol_highs = [d.get('high') for d in vol_window if d.get('high') is not None]
        if len(vol_highs) < 2:
            return None, 0.0

        vol_mean = sum(vol_highs) / len(vol_highs)
        vol_var = sum((h - vol_mean) ** 2 for h in vol_highs) / (len(vol_highs) - 1) if len(vol_highs) > 1 else 0.01
        vol = math.sqrt(vol_var) if vol_var > 0 else 0.01
        slope = (vol_highs[-1] - vol_highs[0]) / len(vol_highs) if len(vol_highs) >= 2 else 0

        # Only trade in stable regimes (low volatility, low slope)
        if vol >= 1.0 or abs(slope) >= 0.5:
            return None, 0.0

        # Mean reversion to 30-day mean
        mean_window = _window(days, idx, self.lookback_mean, offset=1)
        if mean_window is None:
            return None, 0.0

        mean_highs = [d.get('high') for d in mean_window if d.get('high') is not None]
        if not mean_highs:
            return None, 0.0

        mean_30 = sum(mean_highs) / len(mean_highs)

        current = _safe_get(days, idx - 1, 'high')
        if current is None:
            return None, 0.0

        dist = current - mean_30

        if dist > 1.0:
            conf = min(max(dist / 3.0, 0.1), 0.8)
            return 'down', conf
        elif dist < -1.0:
            conf = min(max(abs(dist) / 3.0, 0.1), 0.8)
            return 'up', conf

        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """DB-based evaluation (not implemented for this signal)."""
        return None, 0.0