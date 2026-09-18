"""
Simple Trend Signal - Basic momentum indicator

Compares today's high to yesterday's high to predict momentum
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
from .base_signal import BaseSignal, _safe_get, validate_signal


class SimpleTrendSignal(BaseSignal):
    """
    Simple Trend Signal - Momentum using simple day-over-day comparison

    Logic: If current temperature high is greater than previous day high, 
           predict UP; else predict DOWN
    Confidence: 0.4 (slightly above persistence)
    Min lookback: 2
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "simple_trend"

    @property
    def min_lookback(self) -> int:
        return 2

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate simple trend signal.
        
        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys: date, high, low, dewpoint, 
                  temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.
        """
        if idx < 2:
            return None, 0.0

        prev_high = _safe_get(days, idx - 2, 'high')
        current_high = _safe_get(days, idx - 1, 'high')

        if current_high is None or prev_high is None:
            return None, 0.0

        direction = 'up' if current_high > prev_high else 'down'
        return direction, 0.4

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using DB data.
        Uses the base implementation since no special logic needed.
        """
        return super().evaluate_for_station(station, date, conn)