#!/usr/bin/env python3
"""
SIGNAL: Calendar Climatology

60-day rolling window, z-score > 1.5 threshold. Longer-term view than
gaussian (48d) with stricter threshold and capped confidence.

Based on signal_calendar_climatology() from comprehensive_split_backtest.py.
"""
import math
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, _window, _safe_get, validate_signal


class CalendarClimatologySignal(BaseSignal):
    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.window = 60
        self.z_threshold = 1.5

    @property
    def name(self) -> str:
        return "calendar_climatology"

    @property
    def min_lookback(self) -> int:
        return self.window + 1

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """60-day climatology: trade only when z-score exceeds 1.5."""
        if idx < self.window + 1:
            return None, 0.0

        window = _window(days, idx, self.window, offset=1)
        if window is None:
            return None, 0.0

        highs = [d.get('high') for d in window if d.get('high') is not None]
        if len(highs) < 2:
            return None, 0.0

        mean = sum(highs) / len(highs)
        var = sum((h - mean) ** 2 for h in highs) / (len(highs) - 1) if len(highs) > 1 else 0.01
        std = math.sqrt(var) if var > 0 else 0.01

        current = _safe_get(days, idx - 1, 'high')
        if current is None:
            return None, 0.0

        z = (current - mean) / std

        if z > self.z_threshold:
            conf = min(abs(z) / 3.0, 0.6)
            return 'down', conf
        elif z < -self.z_threshold:
            conf = min(abs(z) / 3.0, 0.6)
            return 'up', conf

        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """DB-based evaluation for calendar climatology signal."""
        import sqlite3
        import math
        
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            # Query max daily temperature for the climatological analysis
            cur = conn.cursor()
            # Get previous days for the climatological window
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high
                FROM metar_observations
                WHERE station=? AND temp_f IS NOT NULL AND date_utc < ?
                GROUP BY date_utc
                ORDER BY date_utc DESC
                LIMIT ?
            """, (station, date, self.window))

            days_data = []
            for r in reversed(cur.fetchall()):  # Reverse to get ascending order
                if r[1] is not None:  # high temp
                    days_data.append({
                        'date': r[0],
                        'high': r[1]
                    })

            # Find the current date's high temperature
            cur.execute("""
                SELECT MAX(temp_f) as high
                FROM metar_observations
                WHERE station=? AND date_utc=?
                AND temp_f IS NOT NULL
            """, (station, date))
            
            current_res = cur.fetchone()
            if current_res is None or current_res[0] is None:
                return None, 0.0
                
            current_high = current_res[0]

            if len(days_data) < 2:
                return None, 0.0

            # Use the same logic as the in-memory evaluation to stay consistent
            lows = max(0, len(days_data) - self.window)
            window_data = days_data[lows:]

            highs = [d.get('high') for d in window_data if d.get('high') is not None]
            if len(highs) < 2:
                return None, 0.0

            mean = sum(highs) / len(highs)
            var = sum((h - mean) ** 2 for h in highs) / (len(highs) - 1) if len(highs) > 1 else 0.01
            std = math.sqrt(var) if var > 0 else 0.01

            z = (current_high - mean) / std

            if z > self.z_threshold:
                conf = min(abs(z) / 3.0, 0.6)
                return 'down', conf
            elif z < -self.z_threshold:
                conf = min(abs(z) / 3.0, 0.6)
                return 'up', conf

            return None, 0.0

        finally:
            if own_conn and conn:
                conn.close()