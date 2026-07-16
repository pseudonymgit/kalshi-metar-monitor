"""
Gaussian V2 Signal - Rolling z-score reversion (30-day window)

Based on approach_gaussian_v2 from ensemble_v7_sharpe_opt.py
Detects extreme temperatures using 30-day rolling mean and std (more responsive),
predicting mean reversion
"""

from abc import ABC
from typing import Optional, Tuple, List, Dict
import sqlite3
import math
from .base_signal import BaseSignal


class GaussianV2Signal(BaseSignal):
    """
    Gaussian V2 Signal - Detects extreme temps using 30-day rolling mean/std,
    then predicts regression to mean

    Logic: Calculate z-score using 30-day rolling window. 
           If z_score > 0.5: predict DOWN (too warm, cooling expected)
           If z_score < -0.5: predict UP (too cool, warming expected)
    Confidence: abs(z_score)
    Min lookback: 31
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "gaussian_v2"

    @property
    def min_lookback(self) -> int:
        return 31

    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate gaussian v2 reversion signal.
        
        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys: date, high, low, dewpoint, 
                  temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.
        """
        if idx < 31:  # Need 30 days + current
            return None, 0.0

        # Use days from idx-31 to idx-1 (30 days before the current day at idx-1)
        window = days[idx-31:idx-1]  # 30 days before current day
        highs = [d['high'] for d in window if d['high'] is not None]
        
        if len(highs) < 30:
            return None, 0.0

        # Calculate mean and standard deviation
        mean = sum(highs) / len(highs)
        variance = sum((h - mean) ** 2 for h in highs) / len(highs)
        std = math.sqrt(variance) if variance > 0 else 0.01  # Avoid division by zero

        current = days[idx-1]['high']
        if current is None:
            return None, 0.0

        z_score = (current - mean) / std if std > 0 else 0

        # Trigger signals based on lower z-score thresholds (making it more responsive)
        if z_score > 0.5:
            # Temperature is high relative to recent mean, expect reversion down
            return 'down', abs(z_score)
        elif z_score < -0.5:
            # Temperature is low relative to recent mean, expect reversion up
            return 'up', abs(z_score)
        else:
            # Within normal range, no strong signal
            return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using DB data.
        Uses the base implementation since no special logic needed.
        """
        return super().evaluate_for_station(station, date, conn)