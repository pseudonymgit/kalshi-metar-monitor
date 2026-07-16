"""
Pressure Delta Signal - 2-day cumulative pressure change

Based on pressure_signal_production.py
Uses 2-day cumulative barometric pressure change as directional predictor.
Signal: if (P_yesterday - P_3_days_ago) > 3.0mb, predict UP (warming)
        if (P_yesterday - P_3_days_ago) < -3.0mb, predict DOWN (cooling)
        otherwise no signal
"""

from abc import ABC
from typing import Optional, Tuple, List, Dict
import sqlite3
import math
from .base_signal import BaseSignal


class PressureDeltaSignal(BaseSignal):
    """
    Pressure Delta Signal - Uses 2-day cumulative pressure change as predictor

    Logic: Compare pressure from yesterday to 3 days ago. 
           If ΔP > 3.0mb → predict UP (warming) 
           If ΔP < -3.0mb → predict DOWN (cooling)
           Otherwise no signal
    Confidence: abs(ΔP) scaled appropriately (between acceptable bounds)
    Min lookback: 4
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "pressure_delta"

    @property
    def min_lookback(self) -> int:
        return 4  # Need 4 days ago to yesterday to calculate properly

    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate pressure delta signal.
        Note: This evaluates relative to historical pressure data in the days array.
        
        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys: date, high, low, dewpoint, 
                  temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.
        """
        if idx < 4:  # Need 3 days before current day to compare with 3 days ago
            return None, 0.0

        # Access the days according to our convention:
        # day at idx-1 is yesterday (we want the pressure from this day)
        # day at idx-4 is 3 days ago (we want the pressure from this day)
        yesterday_pressure = days[idx-1]['pressure']
        three_days_ago_pressure = days[idx-4]['pressure']

        if yesterday_pressure is None or three_days_ago_pressure is None:
            return None, 0.0

        # Calculate change in pressure from 3 days ago to yesterday
        dp = yesterday_pressure - three_days_ago_pressure
        threshold = 3.0

        if abs(dp) < threshold:
            return None, 0.0  # No signal

        direction = 'up' if dp > 0 else 'down'
        
        # Scale confidence based on strength of the signal
        # Cap maximum confidence to prevent overly strong weightings
        confidence = min(abs(dp) / 5.0, 0.8) 
        
        return direction, confidence

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using DB data.
        This needs to be overridden because this signal requires specific pressure data
        from multiple days ago.
        
        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar DB

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            # Direct query for pressure data from the specific station
            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, AVG(pressure_mb) as pressure
                FROM metar_observations
                WHERE station=? AND pressure_mb IS NOT NULL AND temp_f IS NOT NULL
                GROUP BY date_utc
                ORDER BY date_utc ASC
            """, (station,))
            
            days_with_pressure = []
            for r in cur.fetchall():
                if r[1] is None:  # pressure value
                    continue
                days_with_pressure.append({
                    'date': r[0],
                    'pressure': r[1]
                })

            # Find the index for the target date
            target_idx = None
            for i, d in enumerate(days_with_pressure):
                if d['date'] == date:
                    target_idx = i
                    break

            if target_idx is None:
                return None, 0.0

            # Now look for pressure data from the needed days
            # To evaluate today (at target_idx), we need:
            # - Yesterday: target_idx - 1 (idx-1 in standard evaluation)
            # - Three days ago: target_idx - 4 (idx-4 in standard evaluation)
            
            if target_idx < 4:
                return None, 0.0  # Not enough history for this signal

            yesterday_idx = target_idx - 1
            three_days_ago_idx = target_idx - 4

            # Verify both required days exist with pressure data
            if three_days_ago_idx < 0:
                return None, 0.0

            yesterday_pressure = days_with_pressure[yesterday_idx]['pressure']
            three_days_ago_pressure = days_with_pressure[three_days_ago_idx]['pressure']

            if yesterday_pressure is None or three_days_ago_pressure is None:
                return None, 0.0

            # Calculate pressure difference
            dp = yesterday_pressure - three_days_ago_pressure
            threshold = 3.0

            if abs(dp) < threshold:
                return None, 0.0  # No signal strong enough

            direction = 'up' if dp > 0 else 'down'
            
            # Scale confidence based on strength of the signal
            confidence = min(abs(dp) / 5.0, 0.8)
            
            return direction, confidence

        finally:
            if own_conn and conn:
                conn.close()