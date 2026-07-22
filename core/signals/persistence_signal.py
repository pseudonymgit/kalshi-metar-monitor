# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 2. [2026-07-16 T9: Build 5 Tier 1 signals + combinatorial backtest harness]
#


"""
Persistence Signal - Predicts same direction as yesterday

Based on approach_persistence from ensemble_v7_sharpe_opt.py
Yesterday's direction → predict same direction today
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
from .base_signal import BaseSignal, _safe_get, validate_signal


class PersistenceSignal(BaseSignal):
    """
    Persistence Signal - Predicts same direction as yesterday

    Logic: Yesterday's temperature movement direction predicts today's direction
    Confidence: 0.3 (weak baseline)
    Min lookback: 2
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "persistence"

    @property
    def min_lookback(self) -> int:
        return 2

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate persistence signal.
        
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
        today_high = _safe_get(days, idx - 1, 'high')

        if prev_high is None or today_high is None:
            return None, 0.0

        prev_dir = 'up' if today_high > prev_high else 'down'
        return prev_dir, 0.3

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using DB data.
        Uses the base implementation since no special pressure logic needed.
        """
        return super().evaluate_for_station(station, date, conn)