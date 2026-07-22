# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 9: Full 11-signal combinatorial search + calibration + purged CV]
#


"""
Frontal Passage Detector Signal - BaseSignal Implementation

Detects frontal passages using 4 conditions from METAR data:

  - Condition A: Pressure tendency > 1.5mb in last 3-6 hours
  - Condition B: Wind direction shift > 45° from prior readings  
  - Condition C: Temperature gradient using day-over-day as proxy
  - Condition D: Temperature trend > 3°F change day-over-day (indicating front passage)

Scoring:
  - 0-1 conditions met: No signal (None, 0.0)
  - 2 conditions met: Low confidence  
  - 3 conditions met: High confidence
  - 4 conditions met: Very high confidence
"""

import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import logging

from .base_signal import BaseSignal, _safe_get, validate_signal, _window

_logger = logging.getLogger(__name__)

# Thresholds for daily data proxy to detect frontal passages
PRESSURE_CHANGE_THRESHOLD = 1.5     # mb difference day to day
WIND_SHIFT_THRESHOLD = 45.0         # degrees circular difference  
TEMP_GRADIENT_THRESHOLD_C = 3.34    # 6°F in Celsius conversion proxy for daily data
TEMP_TREND_THRESHOLD_F = 3.0        # Degrees F temperature change from day to day

# Confidence mappings
CONFIDENCE_2_CONDITIONS = 0.50      # 2/4 conditions
CONFIDENCE_3_CONDITIONS = 0.80      # 3/4 conditions (Sure Thing level)
CONFIDENCE_4_CONDITIONS = 0.90      # 4/4 conditions (High confidence)


class FrontalDetectorSignal(BaseSignal):
    """
    Frontal Passage Detector as BaseSignal implementation.
    
    Unlike the raw hourly-based detector, this version uses daily proxies from aggregated 
    METAR observations to identify potential frontal passages based on rapid temperature
    and pressure changes that indicate front passage.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.lookback_days = 2  # Need prior 2 days minimum

    @property
    def name(self) -> str:
        return "frontal_detector"

    @property
    def min_lookback(self) -> int:
        return 3  # Need 3 days (current and 2 previous) to see changes

    # ─── Helper Methods ───────────────────────────────────────────────────────

    @staticmethod
    def _circular_difference(angle1: float, angle2: float) -> float:
        """Compute smallest circular difference between two angles (0-180°)."""
        if angle1 is None or angle2 is None:
            return 0.0
        diff = abs(angle1 - angle2)
        return min(diff, 360.0 - diff)

    # ─── Condition A: Pressure Change ─────────────────────────────────────────

    def _check_pressure_change(
        self, idx: int, days: List[Dict]
    ) -> bool:
        """
        Check if pressure has changed significantly day to day.
        
        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            True if pressure change exceeds threshold
        """
        current_pressure = _safe_get(days, idx - 1, 'pressure')
        previous_pressure = _safe_get(days, idx - 2, 'pressure')
        
        if current_pressure is None or previous_pressure is None:
            return False

        # Check if pressure changed by more than threshold
        pressure_change = abs(current_pressure - previous_pressure)
        return pressure_change > PRESSURE_CHANGE_THRESHOLD

    # ─── Condition B: Wind Direction Shift ────────────────────────────────────

    def _check_wind_shift(
        self, idx: int, days: List[Dict]
    ) -> bool:
        """
        Check if wind direction has shifted significantly.

        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            True if wind shift exceeds threshold
        """
        current_wind = _safe_get(days, idx - 1, 'wind_dir')
        previous_wind = _safe_get(days, idx - 2, 'wind_dir')

        if current_wind is None or previous_wind is None:
            return False

        wind_shift = self._circular_difference(current_wind, previous_wind)
        return wind_shift > WIND_SHIFT_THRESHOLD

    # ─── Condition C: Temperature Gradient Proxy ──────────────────────────────

    def _check_temp_gradient_proxy(
        self, idx: int, days: List[Dict]
    ) -> bool:
        """
        Proxy for temperature gradient using day-over-day temp change.

        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            True if temperature change exceeds gradient threshold proxy
        """
        current_temp = _safe_get(days, idx - 1, 'temp')
        previous_temp = _safe_get(days, idx - 2, 'temp')

        if current_temp is None or previous_temp is None:
            return False

        # Convert daily temp change to proxy of gradient
        temp_change_c = abs(current_temp - previous_temp) * 5.0 / 9.0  # F to C
        return temp_change_c > TEMP_GRADIENT_THRESHOLD_C

    # ─── Condition D: Temperature Trend ───────────────────────────────────────

    def _check_temp_trend(
        self, idx: int, days: List[Dict]
    ) -> Tuple[bool, Optional[str]]:
        """
        Check temperature trend direction for warm vs cold front identification.

        Args:
            idx: Current day index
            days: List of daily weather dicts

        Returns:
            (threshold_exceeded, front_direction) where direction is 'warm' or 'cold'
        """
        current_temp = _safe_get(days, idx - 1, 'temp')
        previous_temp = _safe_get(days, idx - 2, 'temp')

        if current_temp is None or previous_temp is None:
            return False, None

        temp_change_f = abs(current_temp - previous_temp)

        if temp_change_f > TEMP_TREND_THRESHOLD_F:
            # Determine front type based on direction
            direction = 'warm' if current_temp > previous_temp else 'cold'
            return True, direction

        return False, None

    # ─── Main Evaluation ──────────────────────────────────────────────────────

    @validate_signal
    def evaluate(
        self, idx: int, days: List[Dict]
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate frontal passage signal from daily METAR data.

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

        # Check all four conditions
        condition_a = self._check_pressure_change(idx, days)
        condition_b = self._check_wind_shift(idx, days)
        condition_c = self._check_temp_gradient_proxy(idx, days)
        condition_d_result, front_type = self._check_temp_trend(idx, days)
        condition_d = condition_d_result

        # Count how many conditions are met
        conditions_met = sum([
            condition_a, 
            condition_b, 
            condition_c, 
            condition_d
        ])

        _logger.debug(
            f"FrontalDetector: {conditions_met}/4 conditions "
            f"(P={condition_a}, W={condition_b}, Tg={condition_c}, Tt={condition_d})"
        )

        # Need at least 2 conditions to consider a frontal passage
        if conditions_met < 2:
            return None, 0.0

        # Determine direction based on front type from temp trend
        if front_type:
            direction = 'up' if front_type == 'warm' else 'down'  # Warm fronts often up, cold fronts often down
        else:
            # If temperature trend doesn't give us direction, 
            # fallback to pressure tendency: falling pressure suggests warming (up)
            current_pressure = _safe_get(days, idx - 1, 'pressure')
            previous_pressure = _safe_get(days, idx - 2, 'pressure')
            
            if current_pressure is not None and previous_pressure is not None:
                if current_pressure < previous_pressure:  # Falling pressure usually means warming
                    direction = 'up'
                elif current_pressure > previous_pressure:  # Rising often means cooling
                    direction = 'down' 
                else:
                    # Indeterminate if no pressure trend either
                    _logger.warning("FrontalDetector: Conditions met but unable to determine direction")
                    return None, 0.0
            else:
                # Without pressure data, we can't determine direction safely
                _logger.warning("FrontalDetector: Insufficient pressure data to determine direction")
                return None, 0.0

        # Map conditions met to confidence level
        if conditions_met == 2:
            confidence = CONFIDENCE_2_CONDITIONS
        elif conditions_met == 3:
            confidence = CONFIDENCE_3_CONDITIONS
        else:  # 4 conditions
            confidence = CONFIDENCE_4_CONDITIONS

        _logger.info(
            f"FrontalDetector: {direction} @ {confidence:.3f}, {conditions_met}/4 conditions"
        )

        return direction, confidence


def test_signal():
    """Quick test function."""
    signal = FrontalDetectorSignal()
    print(f"Name: {signal.name}")
    print(f"Min lookback: {signal.min_lookback}")
    

if __name__ == "__main__":
    test_signal()