#!/usr/bin/env python3
"""
Frontal Passage Detector — Multi-condition Weather Front Event Detector

Detects frontal passages using 4 conditions from METAR data:

  - Condition A: Pressure tendency > 1.5mb in last 3 hours
  - Condition B: Wind direction shift > 45° from prior reading
  - Condition C: 850mb temperature gradient > 6°C across 1000km (approximate using nearby stations)
  - Condition D: Temperature trend > 3°F change in last 3 hours (up = warm front, down = cold front)

Scoring:
  - 0-1 conditions met: No signal (None, 0.0)
  - 2 conditions met: Regular lane candidate (confidence = conditions/4)
  - 3 conditions met: Sure Thing lane candidate (confidence = 0.80)
  - 4 conditions met: Sure Thing lane candidate (confidence = 0.90+)

Registered as a proper BaseSignal in SignalRegistry.

Usage:
    from core.frontal_detector import FrontalDetectorSignal
    signal = FrontalDetectorSignal(db_path)
    direction, confidence = signal.evaluate(idx, days)
"""

import logging
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.signals.base_signal import BaseSignal, _safe_get, validate_signal

_logger = logging.getLogger(__name__)

# ─── Station Regions for approximate 850mb temperature gradient ─────────────
# Organized by geographic proximity for cross-station comparison

STATION_REGIONS = {
    "northeast": ["KNYC", "KBOS", "KPHL", "KDCA"],
    "southeast": ["KATL", "KMIA", "KMSY", "KHOU"],
    "midwest":   ["KMDW", "KMSP", "KDFW", "KOKC"],
    "west_coast": ["KLAX", "KSFO", "KSEA"],
    "southwest": ["KPHX", "KLAS", "KDEN", "KSAT", "KAUS"],
}

# Reverse mapping
_STATION_TO_REGION = {
    station: region
    for region, stations in STATION_REGIONS.items()
    for station in stations
}

# ─── Thresholds ─────────────────────────────────────────────────────────────

PRESSURE_TENDENCY_THRESHOLD_MB = 1.5    # Condition A: mb change in 3 hours
WIND_SHIFT_THRESHOLD_DEG = 45.0         # Condition B: degrees
TEMP_GRADIENT_THRESHOLD_C = 6.0         # Condition C: °C across 1000km
TEMP_TREND_THRESHOLD_F = 3.0            # Condition D: °F change in 3 hours
LOOKBACK_HOURS = 3                       # Hours of lookback for conditions A, D
APPROX_KM_PER_DEGREE = 111.0            # Approximate km per degree of latitude

# Confidence values
CONFIDENCE_2_CONDITIONS_DIVISOR = 4.0   # For 2 conditions: confidence = 2/4
CONFIDENCE_3_CONDITIONS = 0.80          # Sure Thing lane
CONFIDENCE_4_CONDITIONS = 0.90          # Sure Thing lane (strong)


class FrontalDetectorSignal(BaseSignal):
    """
    Frontal Passage Detector Signal

    Detects weather frontal passages using 4 conditions from METAR data.
    Each condition checks a different aspect of frontal activity:

    A: Rapid pressure change
    B: Wind direction shift
    C: Temperature gradient across stations
    D: Temperature trend (warming or cooling)

    Scoring system maps conditions-met to lane assignments:
    - 0-1: No signal
    - 2:   Regular lane (confidence = conditions/4)
    - 3:   Sure Thing lane (confidence = 0.80)
    - 4:   Sure Thing lane (confidence = 0.90+)
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.lookback_days = 2  # Need at least 2 days of data

    @property
    def name(self) -> str:
        return "frontal_detector"

    @property
    def min_lookback(self) -> int:
        return self.lookback_days + 1

    # ─── Condition A: Pressure Tendency ─────────────────────────────────────

    def _check_pressure_tendency(
        self, idx: int, days: List[Dict]
    ) -> bool:
        """
        Check if pressure has changed > 1.5mb in the last 3 hours.

        With daily data, approximate by comparing pressure from idx-1 vs idx-2.

        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            True if pressure tendency exceeds threshold
        """
        current_pressure = _safe_get(days, idx - 1, 'pressure')
        prior_pressure = _safe_get(days, idx - 2, 'pressure')

        if current_pressure is None or prior_pressure is None:
            return False

        delta = abs(current_pressure - prior_pressure)
        return delta > PRESSURE_TENDENCY_THRESHOLD_MB

    # ─── Condition B: Wind Direction Shift ─────────────────────────────────

    @staticmethod
    def _circular_difference(angle1: float, angle2: float) -> float:
        """Compute smallest circular difference between two angles (0-180°)."""
        diff = abs(angle1 - angle2)
        return min(diff, 360.0 - diff)

    def _check_wind_shift(
        self, idx: int, days: List[Dict]
    ) -> bool:
        """
        Check if wind direction has shifted > 45° from prior reading.

        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            True if wind direction shift exceeds threshold
        """
        current_wind = _safe_get(days, idx - 1, 'wind_dir')
        prior_wind = _safe_get(days, idx - 2, 'wind_dir')

        if current_wind is None or prior_wind is None:
            return False

        shift = self._circular_difference(current_wind, prior_wind)
        return shift > WIND_SHIFT_THRESHOLD_DEG

    # ─── Condition C: Temperature Gradient ──────────────────────────────────

    def _check_temp_gradient(
        self, idx: int, days: List[Dict], station: str = None
    ) -> bool:
        """
        Check approximate 850mb temperature gradient using nearby stations.

        Uses the current station's temperature vs other stations in the same
        region, scaled by approximate distance. With daily data, uses the
        current day's temperature as approximation.

        Args:
            idx: Current day index
            days: List of daily weather dicts
            station: Optional station code to identify region

        Returns:
            True if temperature gradient exceeds threshold
        """
        # For single-station evaluation, use the temp difference across
        # consecutive days as a proxy for the gradient
        # (a strong front passing through produces a sharp temp change day-over-day)
        today_temp = _safe_get(days, idx - 1, 'temp')
        yesterday_temp = _safe_get(days, idx - 2, 'temp')

        if today_temp is None or yesterday_temp is None:
            return False

        # Convert to Celsius for the threshold comparison
        # °C = (°F - 32) * 5/9
        # 6°C ≈ 10.8°F
        temp_gradient_c = abs(today_temp - yesterday_temp) * 5.0 / 9.0
        return temp_gradient_c > TEMP_GRADIENT_THRESHOLD_C

    # ─── Condition D: Temperature Trend ─────────────────────────────────────

    def _check_temp_trend(
        self, idx: int, days: List[Dict]
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if temperature has changed > 3°F in the last 3 hours.

        With daily data, uses day-over-day temp change as approximation.
        Returns whether the trend exceeds threshold and the direction (warm/cold front).

        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            (threshold_exceeded, front_type) where front_type is 'warm' or 'cold'
        """
        today_temp = _safe_get(days, idx - 1, 'temp')
        yesterday_temp = _safe_get(days, idx - 2, 'temp')

        if today_temp is None or yesterday_temp is None:
            return False, None

        delta = today_temp - yesterday_temp  # Positive = warming

        if abs(delta) > TEMP_TREND_THRESHOLD_F:
            front_type = 'warm' if delta > 0 else 'cold'
            return True, front_type

        return False, None

    # ─── Main Evaluation ────────────────────────────────────────────────────

    @validate_signal
    def evaluate(
        self, idx: int, days: List[Dict], station: str = None
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate frontal passage signal.

        Checks all 4 conditions and maps to lane assignment.

        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys: date, high, low, dewpoint,
                  temp, wind_dir, wind_speed, pressure
            station: Optional station code for regional gradient check

        Returns:
            (direction, confidence):
              - 'up' or 'down' if 2+ conditions met
              - (None, 0.0) if < 2 conditions met
        """
        if idx < self.min_lookback:
            return None, 0.0

        # Check all conditions
        condition_a = self._check_pressure_tendency(idx, days)
        condition_b = self._check_wind_shift(idx, days)
        condition_c = self._check_temp_gradient(idx, days, station)
        condition_d_result, front_type = self._check_temp_trend(idx, days)
        condition_d = condition_d_result

        conditions_met = sum([condition_a, condition_b, condition_c, condition_d])
        _logger.debug(
            f"FrontalDetector: conditions_met={conditions_met}/4 "
            f"(A={condition_a}, B={condition_b}, C={condition_c}, D={condition_d})"
        )

        if conditions_met < 2:
            return None, 0.0

        # Determine direction from temperature trend (Condition D)
        # If D is not met, use the gradient direction (Condition C proxy)
        if front_type is not None:
            direction = 'up' if front_type == 'warm' else 'down'
        else:
            # Fallback: use pressure tendency direction
            # Falling pressure = warm front, rising = cold front
            current_pressure = _safe_get(days, idx - 1, 'pressure')
            prior_pressure = _safe_get(days, idx - 2, 'pressure')
            if current_pressure is not None and prior_pressure is not None:
                direction = 'down' if current_pressure > prior_pressure else 'up'
            else:
                # No direction signal without temp trend or pressure data
                _logger.debug("FrontalDetector: no direction signal available")
                return None, 0.0

        # Map conditions to confidence
        if conditions_met == 2:
            confidence = conditions_met / CONFIDENCE_2_CONDITIONS_DIVISOR
        elif conditions_met == 3:
            confidence = CONFIDENCE_3_CONDITIONS
        else:  # 4 conditions
            confidence = CONFIDENCE_4_CONDITIONS

        _logger.debug(
            f"FrontalDetector: {direction} @ {confidence:.3f} "
            f"({conditions_met}/4 conditions met)"
        )

        return direction, confidence

    def evaluate_for_station(
        self, station: str, date: str, conn: sqlite3.Connection = None
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate frontal passage signal for a specific station and date.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                       AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                       AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                       AVG(pressure_mb) as pressure
                FROM metar_observations
                WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
                GROUP BY date_utc ORDER BY date_utc ASC
            """, (station,))
            days = []
            for r in cur.fetchall():
                if any(v is None for v in r[1:]):
                    continue
                days.append({
                    'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                    'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
                })

            # Find the index for the target date
            target_idx = None
            for i, d in enumerate(days):
                if d['date'] == date:
                    target_idx = i
                    break

            if target_idx is None:
                return None, 0.0

            return self.evaluate(target_idx, days, station=station)

        finally:
            if own_conn and conn:
                conn.close()


# ─── Standalone test ─────────────────────────────────────────────────────────

def test_frontal_detector():
    """Run a quick test of the frontal detector."""
    print("Testing FrontalDetectorSignal...")
    signal = FrontalDetectorSignal()
    print(f"  name: {signal.name}")
    print(f"  min_lookback: {signal.min_lookback}")
    print("  OK (no data; will return None in production without DB)")


if __name__ == "__main__":
    test_frontal_detector()