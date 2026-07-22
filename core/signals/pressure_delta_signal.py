# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 23.5: Physics Correction - Pressure rise → cooling (not warming)]
#

"""
Pressure Delta Signal - Exponentially-weighted pressure change

Based on pressure_signal_production.py
Uses pressure trend with exponential weighting (half-life 12 hours) as
barometric pressure change directional predictor.

Signal: if (weighted_pressure_change) > 3.0mb, predict DOWN (cooling - Expert 1 finding)
        if (weighted_pressure_change) < -3.0mb, predict UP (warming - Expert 1 finding)
        otherwise no signal

Weighting: w = 2^(-hours_ago / 12) — half-life of 12 hours
           Last 12 hours contributes ~50% of signal weight
"""

from typing import Optional, Tuple, List, Dict
import sqlite3
import math
from .base_signal import BaseSignal, _safe_get, validate_signal


class PressureDeltaSignal(BaseSignal):
    """
    Pressure Delta Signal - Uses exponentially-weighted pressure change as predictor

    Logic: COMPUTE EXPONENTIALLY-WEIGHTED AVERAGE OF RECENT PRESSURE READINGS
           (half-life 12 hours) and compare to a baseline from 3 days ago.
           PHYSICS-CORRECTED (Expert 1 finding):
           If ΔP > 3.0mb (pressure RISES) → predict DOWN (cooling from cold air advection)
           If ΔP < -3.0mb (pressure FALLS) → predict UP (warming from warm air advection)
           Otherwise no signal
    Confidence: abs(ΔP) scaled appropriately (between acceptable bounds)
    Min lookback: 4

    Phase 2: Replaced flat 3-day window with exponential weighting.
    Weight function: 2^(-hours_ago / 12) — half-life 12 hours
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "pressure_delta"

    @property
    def min_lookback(self) -> int:
        return 4  # Need 4 days ago to yesterday to calculate properly

    @staticmethod
    def _exponential_weight(hours_ago: float) -> float:
        """
        Compute exponential weight with half-life of 12 hours.

        w = exp(-days_ago * ln(2) / 0.5) where 0.5 days = half-life
        This means weight halves every 12 hours (0.5 days)

        Args:
            hours_ago: Hours before the current reference time

        Returns:
            Weight value (0.0 to 1.0)
        """
        import math
        days_ago = hours_ago / 24.0
        return math.exp(-days_ago * math.log(2) / 0.5)

    @staticmethod
    def _compute_weighted_pressure(
        idx: int, days: List[Dict], lookback_hours: int = 72
    ) -> Optional[float]:
        """
        Compute exponentially-weighted average pressure over a lookback window.

        Weights are assigned using 2^(-hours_ago / 12) and then normalized
        so they sum to 1.0.

        Args:
            idx: Current day index
            days: List of daily weather dicts
            lookback_hours: How far back to look in hours (default 72 = 3 days)

        Returns:
            Weighted average pressure, or None if insufficient data
        """
        # For daily data, approximate each day as 24 hours apart
        # Start from the most recent available day (yesterday = idx-1)
        weights = []
        pressures = []

        for offset in range(1, lookback_hours // 24 + 2):  # at least 1 lookback, up to ~4 days
            day_idx = idx - offset
            if day_idx < 0:
                break
            pressure = _safe_get(days, day_idx, 'pressure')
            if pressure is None:
                continue
            # Each day offset is ~24 hours apart; use the midpoint
            hours_ago = (offset - 0.5) * 24.0
            # Convert to days for our new exponential weighting function
            w = PressureDeltaSignal._exponential_weight(hours_ago)
            weights.append(w)
            pressures.append(pressure)

        if not pressures:
            return None

        # Normalize weights to sum to 1.0
        total_weight = sum(weights)
        if total_weight <= 0:
            return None
        normalized_weights = [w / total_weight for w in weights]

        # Compute weighted average
        weighted_pressure = sum(
            w * p for w, p in zip(normalized_weights, pressures)
        )
        return weighted_pressure

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate pressure delta signal using exponential weighting.

        Uses exponentially-weighted average of recent pressure readings
        (half-life 12 hours) compared to a baseline from 3 days ago.

        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys: date, high, low, dewpoint,
                  temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.
        """
        if idx < 4:
            return None, 0.0

        # Compute weighted recent pressure (last 3 days, exponentially weighted)
        weighted_recent = self._compute_weighted_pressure(idx, days, lookback_hours=72)

        # Baseline: pressure from 3 days ago
        three_days_ago_pressure = _safe_get(days, idx - 4, 'pressure')

        if weighted_recent is None or three_days_ago_pressure is None:
            return None, 0.0

        dp = weighted_recent - three_days_ago_pressure
        threshold = 3.0

        if abs(dp) < threshold:
            return None, 0.0

        # PHYSICS CORRECTION (Expert 1 Meteorology Finding #5):
        # When pressure rises (dp > 0), it reflects cold air advection (cooling) 
        # When pressure falls (dp < 0), it reflects warm air advection (warming)
        direction = 'down' if dp > 0 else 'up'  # Inverted from original
        confidence = min(abs(dp) / 5.0, 0.8)

        return direction, confidence

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using DB data.
        This needs to be overridden because this signal requires specific pressure data
        from multiple days ago.
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar DB

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = get_sqlite_connection(self.db_path) if self.db_path else None
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

            if target_idx < 4:
                return None, 0.0  # Not enough history for this signal

            # Use the exponentially-weighted version
            weighted_recent = self._compute_weighted_pressure(
                target_idx, days_with_pressure, lookback_hours=72
            )
            three_days_ago_pressure = days_with_pressure[target_idx - 4]['pressure']

            if weighted_recent is None or three_days_ago_pressure is None:
                return None, 0.0

            dp = weighted_recent - three_days_ago_pressure
            threshold = 3.0

            if abs(dp) < threshold:
                return None, 0.0  # No signal strong enough

            # PHYSICS CORRECTION (Phase 23.5 - Expert 1 finding):
            # Rising pressure (dp > 0) causes cooling, falling pressure warming
            direction = 'down' if dp > 0 else 'up'

            # Scale confidence based on strength of the signal
            confidence = min(abs(dp) / 5.0, 0.8)

            return direction, confidence

        finally:
            if own_conn and conn:
                conn.close()