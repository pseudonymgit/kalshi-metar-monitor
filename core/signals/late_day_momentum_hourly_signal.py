#!/usr/bin/env python3
"""
Late-Day Momentum Hourly Signal

Wraps the existing late_day_momentum_hourly module for use in the
unified signal pipeline. Uses hourly METAR data to detect
late-day temperature momentum.
"""

import sys
import os
from typing import Optional, Tuple, List
import sqlite3
from .base_signal import BaseSignal

# Import the existing implementation
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))
from late_day_momentum_hourly import late_day_momentum_hourly as _ldm_hourly


class LateDayMomentumHourlySignal(BaseSignal):
    """Late-day momentum from hourly METAR data — threshold=1.7."""

    @property
    def name(self) -> str:
        return "late_day_momentum_hourly"

    @property
    def min_lookback(self) -> int:
        return 3

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """For backtest compatibility — uses day-level data."""
        if idx < 3:
            return None, 0.0
        t0 = days[idx - 1]['temp']
        t1 = days[idx - 2]['temp']
        dt = t0 - t1
        if abs(dt) > 2.0:
            return ('up' if dt > 0 else 'down'), min(abs(dt) / 4.0, 0.7)
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """Use the hourly implementation for station-date evaluation."""
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0
        try:
            direction, confidence, prob = _ldm_hourly(station, date, conn)
            return direction, confidence if confidence is not None else 0.0
        finally:
            if own_conn and conn:
                conn.close()
