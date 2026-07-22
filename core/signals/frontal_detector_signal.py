#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 23.3: Frontal Detection Fix — 3-6h window from raw METAR, daily aggregate fallback]
#

"""
Frontal Passage Detector Signal — BaseSignal Implementation

DETECTS FRONTAL PASSAGES using 3-6 hour window analysis from raw METAR observations.

Phase 23.3 Fix (Expert 1 Meteorology finding):
  - Old approach: day-over-day comparisons (missed frontal passages entirely)
  - New approach: query raw METAR observations within a 3-6 hour window
  - A frontal passage typically takes 3-6 hours with 2-5 mb pressure change

Four conditions from intra-hourly METAR data:
  - Condition A: Pressure change >= 2.0 mb in last 3-6 hours
  - Condition B: Wind direction shift > 45° in last 3-6 hours
  - Condition C: Temperature gradient > 3°F change in 3-6 hours
  - Condition D: Temperature trend direction (warm/cold front identification)

Fallback: When raw METAR data is unavailable, uses day-over-day comparison.

Scoring:
  - 0-1 conditions met: No signal (None, 0.0)
  - 2 conditions met: Low confidence (0.50)
  - 3 conditions met: High confidence (0.80)
  - 4 conditions met: Very high confidence (0.90)
"""

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
import logging

from .base_signal import BaseSignal, _safe_get, validate_signal, _window
from ..sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

_logger = logging.getLogger(__name__)

# ─── Intra-hourly thresholds (3-6 hour window) ─────────────────────────────────
INTRADAY_PRESS_CHANGE_THRESHOLD = 2.0   # mb change in 3-6 hours (was 1.5 day-over-day)
INTRADAY_WIND_SHIFT_THRESHOLD = 45.0    # degrees circular difference
INTRADAY_TEMP_CHANGE_THRESHOLD_F = 3.0  # °F temperature change in 3-6 hours
INTRADAY_WINDOW_HOURS = 6               # Lookback window for raw METAR obs
INTRADAY_MIN_OBS = 3                    # Minimum observations needed for reliable trend

# ─── Daily aggregate thresholds (fallback) ─────────────────────────────────────
DAILY_PRESS_CHANGE_THRESHOLD = 1.5      # mb difference day to day
DAILY_WIND_SHIFT_THRESHOLD = 45.0       # degrees circular difference
DAILY_TEMP_GRADIENT_THRESHOLD_C = 3.34  # 6°F in Celsius
DAILY_TEMP_TREND_THRESHOLD_F = 3.0      # Degrees F temperature change day to day

# ─── Confidence mappings ───────────────────────────────────────────────────────
CONFIDENCE_2_CONDITIONS = 0.50          # 2/4 conditions
CONFIDENCE_3_CONDITIONS = 0.80          # 3/4 conditions
CONFIDENCE_4_CONDITIONS = 0.90          # 4/4 conditions


class FrontalDetectorSignal(BaseSignal):
    """
    Frontal Passage Detector as BaseSignal implementation.

    Phase 23.3: Uses 3-6 hour window from raw METAR observations for
    frontal detection. Falls back to daily aggregate comparison when
    raw data is unavailable.

    The 3-6 hour window captures the actual frontal passage timescale
    (pressure change of 2-5 mb, wind shift > 45°, temp gradient > 3°F).
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.lookback_days = 2

    @property
    def name(self) -> str:
        return "frontal_detector"

    @property
    def min_lookback(self) -> int:
        return 3

    # ─── Circular Difference Helper ───────────────────────────────────────────

    @staticmethod
    def _circular_difference(angle1: float, angle2: float) -> float:
        """Compute smallest circular difference between two angles (0-180°)."""
        if angle1 is None or angle2 is None:
            return 0.0
        diff = abs(angle1 - angle2)
        return min(diff, 360.0 - diff)

    # ─── Raw METAR Query Methods (3-6 hour window) ────────────────────────────

    def _get_metar_window(self, station: str, date_utc: str,
                          window_hours: int = INTRADAY_WINDOW_HOURS,
                          conn: sqlite3.Connection = None) -> Optional[List[Dict]]:
        """
        Query raw METAR observations for a station within a time window.

        Args:
            station: ICAO station code
            date_utc: Target date string 'YYYY-MM-DD'
            window_hours: Hours to look back from end of date
            conn: Optional DB connection

        Returns:
            List of observation dicts sorted by timestamp, or None if error
        """
        own_conn = conn is None
        if own_conn:
            if not self.db_path:
                return None
            conn = get_sqlite_connection(self.db_path)

        try:
            cur = conn.cursor()
            # Compute window: from (date_end - window_hours) to date_end
            # Date end is 23:59:59 UTC on the target date
            window_start = f"{date_utc} {max(0, 24 - window_hours):02d}:00:00"

            cur.execute("""
                SELECT timestamp_utc, temp_f, pressure_mb, wind_direction_deg,
                       wind_speed_kt, dewpoint_f
                FROM metar_observations
                WHERE station = ?
                  AND date_utc = ?
                  AND timestamp_utc >= ?
                  AND temp_f IS NOT NULL
                  AND pressure_mb IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, date_utc, window_start))

            obs = []
            for r in cur.fetchall():
                obs.append({
                    'timestamp': r[0],
                    'temp_f': r[1],
                    'pressure_mb': r[2],
                    'wind_dir_deg': r[3],
                    'wind_speed_kt': r[4],
                    'dewpoint_f': r[5],
                })

            if len(obs) < INTRADAY_MIN_OBS:
                return None

            return obs

        except Exception as e:
            _logger.warning(f"FrontalDetector query error for {station} on {date_utc}: {e}")
            return None
        finally:
            if own_conn and conn:
                conn.close()

    def _check_intraday_pressure_change(self, obs: List[Dict]) -> bool:
        """
        Check pressure change over the observation window.

        Condition A: Pressure change >= 2.0 mb in 3-6 hours.
        Computes the difference between first and last observation.

        Args:
            obs: List of METAR observation dicts, sorted by timestamp

        Returns:
            True if pressure change exceeds intraday threshold
        """
        if len(obs) < 2:
            return False

        first_press = obs[0]['pressure_mb']
        last_press = obs[-1]['pressure_mb']

        if first_press is None or last_press is None:
            return False

        pressure_change = abs(last_press - first_press)
        return pressure_change >= INTRADAY_PRESS_CHANGE_THRESHOLD

    def _check_intraday_wind_shift(self, obs: List[Dict]) -> bool:
        """
        Check wind direction shift over the observation window.

        Condition B: Wind direction shift > 45° in 3-6 hours.
        Uses first vs last observation with valid wind data.

        Args:
            obs: List of METAR observation dicts, sorted by timestamp

        Returns:
            True if wind shift exceeds intraday threshold
        """
        # Filter for observations with valid wind data
        valid_obs = [o for o in obs if o['wind_dir_deg'] is not None]
        if len(valid_obs) < 2:
            return False

        first_wind = valid_obs[0]['wind_dir_deg']
        last_wind = valid_obs[-1]['wind_dir_deg']

        if first_wind is None or last_wind is None:
            return False

        wind_shift = self._circular_difference(first_wind, last_wind)
        return wind_shift > INTRADAY_WIND_SHIFT_THRESHOLD

    def _check_intraday_temp_gradient(self, obs: List[Dict]) -> Tuple[bool, Optional[str]]:
        """
        Check temperature gradient and trend over the observation window.

        Conditions C & D:
        - Temperature change > 3°F in 3-6 hours → gradient met
        - Direction of change determines warm/cold front

        Args:
            obs: List of METAR observation dicts, sorted by timestamp

        Returns:
            (gradient_exceeded, front_type) where front_type is 'warm' or 'cold'
        """
        if len(obs) < 2:
            return False, None

        first_temp = obs[0]['temp_f']
        last_temp = obs[-1]['temp_f']

        if first_temp is None or last_temp is None:
            return False, None

        temp_change = last_temp - first_temp
        abs_change = abs(temp_change)

        if abs_change >= INTRADAY_TEMP_CHANGE_THRESHOLD_F:
            front_type = 'warm' if temp_change > 0 else 'cold'
            return True, front_type

        return False, None

    # ─── Daily Aggregate Fallback Methods ─────────────────────────────────────

    def _check_daily_pressure_change(self, idx: int, days: List[Dict]) -> bool:
        """Fallback: day-over-day pressure change check."""
        current_pressure = _safe_get(days, idx - 1, 'pressure')
        previous_pressure = _safe_get(days, idx - 2, 'pressure')
        if current_pressure is None or previous_pressure is None:
            return False
        return abs(current_pressure - previous_pressure) > DAILY_PRESS_CHANGE_THRESHOLD

    def _check_daily_wind_shift(self, idx: int, days: List[Dict]) -> bool:
        """Fallback: day-over-day wind shift check."""
        current_wind = _safe_get(days, idx - 1, 'wind_dir')
        previous_wind = _safe_get(days, idx - 2, 'wind_dir')
        if current_wind is None or previous_wind is None:
            return False
        return self._circular_difference(current_wind, previous_wind) > DAILY_WIND_SHIFT_THRESHOLD

    def _check_daily_temp_gradient(self, idx: int, days: List[Dict]) -> bool:
        """Fallback: day-over-day temperature gradient proxy."""
        current_temp = _safe_get(days, idx - 1, 'temp')
        previous_temp = _safe_get(days, idx - 2, 'temp')
        if current_temp is None or previous_temp is None:
            return False
        temp_change_c = abs(current_temp - previous_temp) * 5.0 / 9.0
        return temp_change_c > DAILY_TEMP_GRADIENT_THRESHOLD_C

    def _check_daily_temp_trend(self, idx: int, days: List[Dict]) -> Tuple[bool, Optional[str]]:
        """Fallback: day-over-day temperature trend."""
        current_temp = _safe_get(days, idx - 1, 'temp')
        previous_temp = _safe_get(days, idx - 2, 'temp')
        if current_temp is None or previous_temp is None:
            return False, None
        temp_change_f = abs(current_temp - previous_temp)
        if temp_change_f > DAILY_TEMP_TREND_THRESHOLD_F:
            direction = 'warm' if current_temp > previous_temp else 'cold'
            return True, direction
        return False, None

    # ─── Main Evaluation ──────────────────────────────────────────────────────

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate frontal passage signal.

        Phase 23.3: Uses 3-6 hour window analysis from raw METAR observations
        when available. Falls back to daily aggregate comparison.

        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys:
                date, high, low, dewpoint, temp, wind_dir, wind_speed, pressure

        Returns:
            (direction, confidence):
              - (None, 0.0) if < 2 conditions met or insufficient data
              - ('up'/'down', confidence) based on front type and conditions met
        """
        if idx < self.min_lookback:
            return None, 0.0

        # Try intraday analysis first (requires DB connection and station context)
        # The evaluate() method doesn't have station context, so we use the
        # daily aggregate approach here. The evaluate_for_station() method
        # uses the superior intraday approach.

        condition_a = self._check_daily_pressure_change(idx, days)
        condition_b = self._check_daily_wind_shift(idx, days)
        condition_c = self._check_daily_temp_gradient(idx, days)
        condition_d_result, front_type = self._check_daily_temp_trend(idx, days)
        condition_d = condition_d_result

        return self._compute_signal(condition_a, condition_b, condition_c,
                                    condition_d, front_type, idx, days)

    def evaluate_for_station(self, station: str, date: str,
                             conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate frontal passage signal for a specific station and date.

        Uses 3-6 hour window analysis from raw METAR observations for
        superior frontal detection. Falls back to daily aggregate.

        Args:
            station: ICAO station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            if not self.db_path:
                return None, 0.0
            conn = get_sqlite_connection(self.db_path)

        try:
            # Try intraday METAR window analysis
            obs = self._get_metar_window(station, date, conn=conn)

            if obs and len(obs) >= INTRADAY_MIN_OBS:
                condition_a = self._check_intraday_pressure_change(obs)
                condition_b = self._check_intraday_wind_shift(obs)
                condition_c, front_type = self._check_intraday_temp_gradient(obs)
                condition_d = condition_c  # Same condition, gradient = trend

                _logger.debug(
                    f"FrontalDetector[{station}/{date}]: Intraday {len(obs)} obs "
                    f"(P={condition_a}, W={condition_b}, T={condition_c})"
                )

                # With intraday data, conditions C and D are combined
                # So 2/3 conditions = frontal passage
                conditions_met = sum([condition_a, condition_b, condition_c])

                if conditions_met < 2:
                    return None, 0.0

                # Determine direction
                direction = self._determine_direction(
                    front_type, obs, condition_a, condition_b, condition_c
                )

                if direction is None:
                    return None, 0.0

                confidence = self._map_confidence(conditions_met, is_intraday=True)

                _logger.info(
                    f"FrontalDetector[{station}/{date}]: {direction} @ {confidence:.3f}, "
                    f"{conditions_met}/3 intraday conditions"
                )
                return direction, confidence

            # Fallback to daily aggregate
            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                       AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                       AVG(wind_direction_deg) as wind_dir,
                       AVG(wind_speed_kt) as wind_speed,
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
                    'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
                })

            target_idx = next((i for i, d in enumerate(days) if d['date'] == date), None)
            if target_idx is None or target_idx < self.min_lookback:
                return None, 0.0

            return self.evaluate(target_idx, days)

        except Exception as e:
            _logger.error(f"FrontalDetector evaluate_for_station error: {e}")
            return None, 0.0
        finally:
            if own_conn and conn:
                conn.close()

    # ─── Signal Computation Helpers ───────────────────────────────────────────

    def _compute_signal(self, condition_a: bool, condition_b: bool,
                        condition_c: bool, condition_d: bool,
                        front_type: Optional[str], idx: int,
                        days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Compute final signal from condition flags.

        Args:
            condition_a-d: Individual condition flags
            front_type: 'warm', 'cold', or None
            idx: Current day index
            days: Daily weather data

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        conditions_met = sum([condition_a, condition_b, condition_c, condition_d])

        _logger.debug(
            f"FrontalDetector: {conditions_met}/4 conditions "
            f"(P={condition_a}, W={condition_b}, Tg={condition_c}, Tt={condition_d})"
        )

        if conditions_met < 2:
            return None, 0.0

        direction = self._determine_direction_from_daily(
            front_type, condition_a, condition_b, condition_c, condition_d, idx, days
        )

        if direction is None:
            return None, 0.0

        confidence = self._map_confidence(conditions_met, is_intraday=False)

        _logger.info(
            f"FrontalDetector: {direction} @ {confidence:.3f}, {conditions_met}/4 conditions"
        )
        return direction, confidence

    def _determine_direction_from_daily(
        self, front_type: Optional[str],
        condition_a: bool, condition_b: bool,
        condition_c: bool, condition_d: bool,
        idx: int, days: List[Dict]
    ) -> Optional[str]:
        """
        Determine direction from daily aggregate conditions.

        Priority: temperature trend > pressure tendency > wind shift
        """
        if front_type:
            return 'up' if front_type == 'warm' else 'down'

        # Fallback to pressure tendency
        current_pressure = _safe_get(days, idx - 1, 'pressure')
        previous_pressure = _safe_get(days, idx - 2, 'pressure')

        if current_pressure is not None and previous_pressure is not None:
            if current_pressure < previous_pressure:
                return 'up'    # Falling pressure → warming
            elif current_pressure > previous_pressure:
                return 'down'  # Rising pressure → cooling

        _logger.warning("FrontalDetector: Conditions met but unable to determine direction")
        return None

    def _determine_direction(self, front_type: Optional[str],
                             obs: List[Dict],
                             condition_a: bool, condition_b: bool,
                             condition_c: bool) -> Optional[str]:
        """
        Determine frontal passage direction from intraday conditions.

        Priority:
        1. Temperature trend (most direct)
        2. Pressure tendency (synoptic pattern)
        3. Wind shift (sector-based)

        Physics-correct rules:
        - Cold front: temp drops, pressure rises, wind shifts NW→SW
        - Warm front: temp rises, pressure falls, wind shifts SE→SW
        """
        if front_type:
            return 'up' if front_type == 'warm' else 'down'

        # Pressure tendency
        if len(obs) >= 2:
            first_p = obs[0]['pressure_mb']
            last_p = obs[-1]['pressure_mb']
            if first_p is not None and last_p is not None:
                if last_p < first_p and abs(last_p - first_p) >= INTRADAY_PRESS_CHANGE_THRESHOLD:
                    return 'up'    # Falling pressure → warm front
                elif last_p > first_p and abs(last_p - first_p) >= INTRADAY_PRESS_CHANGE_THRESHOLD:
                    return 'down'  # Rising pressure → cold front

        # Wind shift direction
        valid_obs = [o for o in obs if o['wind_dir_deg'] is not None]
        if len(valid_obs) >= 2:
            first_w = valid_obs[0]['wind_dir_deg']
            last_w = valid_obs[-1]['wind_dir_deg']
            shift = self._circular_difference(first_w, last_w)
            if shift > INTRADAY_WIND_SHIFT_THRESHOLD:
                # If shift towards N (cooler direction), predict down
                if (315 <= last_w <= 360) or (0 <= last_w <= 90):
                    return 'down'
                else:
                    return 'up'

        return None

    def _map_confidence(self, conditions_met: int, is_intraday: bool = False) -> float:
        """
        Map number of conditions met to confidence level.

        Intraday analysis gets a confidence boost because the
        3-6 hour window is more reliable than daily aggregates.

        Args:
            conditions_met: Number of conditions met (2-4)
            is_intraday: Whether this is from intraday analysis

        Returns:
            Confidence score between 0.0 and 1.0
        """
        if is_intraday:
            # Intraday: 3 conditions possible (C+D combined)
            if conditions_met >= 3:
                return 0.85
            elif conditions_met == 2:
                return 0.55
            return 0.0
        else:
            if conditions_met == 4:
                return CONFIDENCE_4_CONDITIONS
            elif conditions_met == 3:
                return CONFIDENCE_3_CONDITIONS
            elif conditions_met == 2:
                return CONFIDENCE_2_CONDITIONS
            return 0.0


def test_signal():
    """Quick test function."""
    signal = FrontalDetectorSignal()
    print(f"Name: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")


if __name__ == "__main__":
    test_signal()