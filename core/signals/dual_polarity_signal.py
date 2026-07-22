#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 23.5: Dual-Polarity Signal Framework — warm-season vs cool-season]
#

"""
DUAL-POLARITY SIGNAL FRAMEWORK

Framework to handle signals that reverse polarity between warm and cool seasons,
particularly addressing the Expert 1 finding that PressureDeltaSignal direction
should be inverted: rising pressure → cooling, not warming.

Seasonal Regime Detection:
- Cool season (Oct-Apr): rising pressure → cooler temps (cold front passage)  
- Warm season (May-Sep): rising pressure → sometimes warmer temps (high pressure)
- But synoptic patterns vary by location

Physics Rules Applied:
- Pressure rise → cooling (cold air advection)
- Pressure fall → warming (warm air advection, frontal passage)
- Wind chill factor in winter → more cooling from N winds
- Thermal mass in summer → thermal lag effects

Adapted from Expert 1 Meteorology findings on signal direction reversals.
"""

import sqlite3
import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import logging

from .base_signal import BaseSignal, _safe_get, validate_signal
from core.station_effects import get_wind_delta_t, is_warming_wind
from ..sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

_logger = logging.getLogger(__name__)

# ─── Seasonal Thresholds ───────────────────────────────────────────────────────

# Define warm/cool season months
COOL_SEASON_START_MONTH = 10  # October
WARM_SEASON_START_MONTH = 5   # May  (cool season Nov-Apr, warm season May-Oct)

# Seasonal confidence modifiers
SEASON_CONFIDENCE_BIAS = 1.2  # Amplify seasonal signals by 20%
MINIMUM_CONFIDENCE_BOOST = 0.05  # Always get 5% boost from seasonality


class DualPolaritySignal(BaseSignal):
    """
    Base class for dual-polarity signals that invert direction between
    warm and cool seasons.

    Designed to wrap existing signals and apply seasonal corrections.
    """

    def __init__(self, wrapped_signal: BaseSignal, db_path: str = None):
        super().__init__(db_path)
        self.wrapped_signal = wrapped_signal

    @property
    def name(self) -> str:
        return f"dual_{self.wrapped_signal.name}"

    @property
    def min_lookback(self) -> int:
        return self.wrapped_signal.min_lookback

    def detect_season(self, date: str) -> str:
        """
        Detect warm/cool season based on date.

        Args:
            date: ISO date string 'YYYY-MM-DD'

        Returns:
            'warm' or 'cool'
        """
        month = int(date.split('-')[1])
        # Warm season: May through September (5-9)
        if WARM_SEASON_START_MONTH <= month <= 9:
            return 'warm'
        else:
            return 'cool'

    def adjust_for_seasonality(self, direction: Optional[str], confidence: float,
                               season: str, station: str = None) -> Tuple[Optional[str], float]:
        """
        Adjust signal direction and confidence based on season.

        Warm season may reverse cool season patterns.

        Args:
            direction: Original direction ('up' or 'down')
            confidence: Original confidence [0.0, 1.0]
            season: 'warm' or 'cool'
            station: Station code for location-specific adjustments

        Returns:
            (adjusted_direction, adjusted_confidence)
        """
        if not direction:
            return None, 0.0

        # Default: no adjustment
        adj_direction = direction
        adj_confidence = confidence

        # Apply season-specific rules (these come from Expert 1 findings)
        for adj_rule in self._get_seasonal_rules(season, station):
            adj_direction, adj_confidence = adj_rule(adj_direction, adj_confidence)

        return adj_direction, adj_confidence

    def _get_seasonal_rules(self, season: str, station: str = None) -> List[callable]:
        """
        Get season-specific adjustment rules by station and season.

        Applies Physics Corrections identified by Expert 1:
        - Cool: Rising pressure → cooling (cold fronts) = direction maintained
        - Cool: Warm fronts → warming = direction maintained
        - Warm: Can have mixed signals but physics still holds
        """
        rules = []

        if season == 'cool':
            # Expert 1: In cold season, rising pressure usually indicates cool front passage
            # So pressure deltas reverse directionally
            # BUT more importantly, the physics is: rising pressure → advection of colder air
            def cool_season_rule(orig_dir, orig_conf):
                # Apply confidence boost for cold season where physics is clearer
                return orig_dir, min(1.0, orig_conf * SEASON_CONFIDENCE_BIAS)
            rules.append(cool_season_rule)
        
        elif season == 'warm':
            # In warm season, signals can be mixed depending on location
            # Some high pressure systems warm (ridges), others cool (differential heating)
            def warm_season_rule(orig_dir, orig_conf):
                # Reduce confidence slightly as warmth adds ambiguity
                return orig_dir, orig_conf * 0.9
            rules.append(warm_season_rule)
        else:
            # Season unknown → reduce confidence
            def unknown_season_rule(orig_dir, orig_conf):
                return orig_dir, orig_conf * 0.8
            rules.append(unknown_season_rule)

        if station:
            # Location-specific adjustments could be added here
            # These would come from the station-effects module
            station_delta = get_wind_delta_t(station, 0)  # Example: look up current wind effect
            if station_delta is not None:
                def station_rule(orig_dir, orig_conf):
                    # If this station is currently showing a warming wind pattern,
                    # it may amplify warming signals (in winter) or dampen them (summer)
                    if station_delta > 0.5:  # Warming wind
                        multiplier = 1.1 if orig_dir == 'up' else 1.05
                    elif station_delta < -0.5:  # Cooling wind
                        multiplier = 1.1 if orig_dir == 'down' else 1.05
                    else:
                        multiplier = 1.0
                    return orig_dir, min(1.0, orig_conf * multiplier)
                    
                rules.append(station_rule)

        return rules

    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate the wrapped signal and adjust for seasonal effects.

        This is the standard interface for backtesting.
        """
        if idx < self.min_lookback:
            return None, 0.0

        # Get date for season detection
        target_date = days[idx]['date'] if isinstance(days[idx]['date'], str) else str(days[idx]['date'])

        # Evaluate wrapped signal
        direction, confidence = self.wrapped_signal.evaluate(idx, days)

        # Adjust for season
        season = self.detect_season(target_date)
        adj_direction, adj_confidence = self.adjust_for_seasonality(
            direction, confidence, season
        )

        _logger.debug(
            f"DualPolarity[{self.wrapped_signal.name}]: season={season}, "
            f"orig=({direction},{confidence:.3f}), adj=({adj_direction},{adj_confidence:.3f})"
        )

        return adj_direction, adj_confidence

    def evaluate_for_station(self, station: str, date: str, 
                            conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate for specific station and date with seasonal adjustments.

        Maintains compatibility with standard signal interface.
        """
        # Get date-based season
        season = self.detect_season(date)

        # Evaluate wrapped signal
        direction, confidence = self.wrapped_signal.evaluate_for_station(
            station, date, conn
        )

        # Adjust with location-specific info
        adj_direction, adj_confidence = self.adjust_for_seasonality(
            direction, confidence, season, station
        )

        return adj_direction, adj_confidence


class SeasonalRegimeClassifier(BaseSignal):
    """
    Classify the seasonal temperature regime for a given date and location.

    Helps determine whether to expect sign reversals from temperature
    sensitivity changes throughout the year.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        # For regime classification, 1 day is sufficient
        self.lookback_days = 1

    @property
    def name(self) -> str:
        return "seasonal_regime"

    @property
    def min_lookback(self) -> int:
        return 1

    def classify_regime(self, temp_f: float, month: int, 
                        location: str = 'typical') -> Tuple[str, float, str]:
        """
        Classify seasonal regime based on temperature, month, and location.

        Args:
            temp_f: Current temperature in °F
            month: Current month (1-12)
            location: General location descriptor

        Returns:
            (regime_name, confidence, description)
        """
        # Use simple temperature + month heuristics
        is_northern_hemisphere = True  # All our stations are in NH
        is_deep_winter = month in [12, 1, 2] and temp_f < 40
        is_early_winter = month in [10, 11] and temp_f < 50
        is_late_winter_spring = month in [3, 4] and temp_f < 60
        is_summer = month in [5, 6, 7, 8] and temp_f > 70
        is_early_fall = month in [9] or (month in [10] and temp_f >= 50)

        if is_deep_winter:
            regime = 'deep_winter'
            confidence = 0.9
            desc = 'Cold season - cold air advection dominates pressure-temperature relations'
        elif is_early_winter or is_late_winter_spring:
            regime = 'transition_cold'
            confidence = 0.7
            desc = 'Cooling transition - pressure rise indicates cold air mass movement' 
        elif is_summer:
            regime = 'warm_season'
            confidence = 0.85
            desc = 'Warm season - pressure gradients drive convection patterns'
        elif is_early_fall:
            regime = 'transition_warm'
            confidence = 0.7
            desc = 'Warming transition - warm fronts and heat retention effects'
        else:
            regime = 'indeterminate'  
            confidence = 0.3
            desc = 'Intermediate temperature - mixed regime effects'

        return regime, confidence, desc

    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate seasonal regime for current date.

        Returns regime name as direction, confidence as certainty.
        """
        if idx < 1:
            return 'indeterminate', 0.3

        current_year, current_month = days[idx]['date'][:7].split('-')
        current_month = int(current_month)
        current_temp = _safe_get(days, idx, 'temp')
        
        if current_temp is None:
            return 'indeterminate', 0.3

        regime, confidence, _ = self.classify_regime(current_temp, current_month)
        return regime, confidence


# ─── Physics-Corrected Pressure Signal ─────────────────────────────────────────

class CorrectedPressureDeltaSignal(BaseSignal):
    """
    Pressure Delta Signal with physics-corrected direction mapping.

    Physics Finding: Expert 1 identified that pressure rise → cooling,
    not warming, contrary to original signal logic.

    Synoptic rules:
    - Rising pressure (gradual): cold air advection → cooling temperatures
    - Falling pressure (gradual): warm air advection → warming temperatures
    - Rapid changes often associated with fronts, adding complexity

    This corrects the original PressureDeltaSignal physics.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.lookback_hours = 72  # 3 days

    @property  
    def name(self) -> str:
        return "pressure_delta_phy_corrected"

    @property
    def min_lookback(self) -> int:
        return 4  # Need 4 days to compare with 3-days prior

    @staticmethod
    def _exponential_weight(hours_ago: float) -> float:
        """Weight function with 12-hour half-life."""
        days_ago = hours_ago / 24.0
        return math.exp(-days_ago * math.log(2) / 0.5)

    @staticmethod
    def _compute_weighted_pressure(
        idx: int, days: List[Dict], lookback_hours: int = 72
    ) -> Optional[float]:
        """Compute exponentially-weighted average pressure."""
        weights = []
        pressures = []

        for offset in range(1, lookback_hours // 24 + 2):
            day_idx = idx - offset
            if day_idx < 0:
                break
            pressure = _safe_get(days, day_idx, 'pressure')
            if pressure is None:
                continue
            hours_ago = (offset - 0.5) * 24.0
            w = CorrectedPressureDeltaSignal._exponential_weight(hours_ago)
            weights.append(w)
            pressures.append(pressure)

        if not pressures:
            return None

        total_weight = sum(weights)
        if total_weight <= 0:
            return None
        normalized_weights = [w / total_weight for w in weights]

        weighted_pressure = sum(
            w * p for w, p in zip(normalized_weights, pressures)
        )
        return weighted_pressure

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate pressure delta signal with corrected physics.

        Physics Rule: Rising pressure → cooling temperature
                      Falling pressure → warming temperature
        """
        if idx < 4:
            return None, 0.0

        weighted_recent = self._compute_weighted_pressure(idx, days, lookback_hours=72)
        three_days_ago_pressure = _safe_get(days, idx - 4, 'pressure')

        if weighted_recent is None or three_days_ago_pressure is None:
            return None, 0.0

        # dp = weighted_recent - three_days_ago_pressure  
        # Positive: pressure increased (rose)
        # Physics: when pressure RISES, it typically cools (cold air advection)
        dp = weighted_recent - three_days_ago_pressure
        threshold = 3.0

        if abs(dp) < threshold:
            return None, 0.0

        # PHYSICS CORRECTION (Expert 1 Finding):
        # Original logic: rising pressure → 'up' (warming)
        # Correct physics: rising pressure → 'down' (cooling)
        direction = 'down' if dp > 0 else 'up'  # Falling pressure → 'up' (warming)
        
        confidence = min(abs(dp) / 5.0, 0.8)

        # Apply seasonal correction based on synoptic regime
        # In cold season: pressure rise definitely cools
        # In hot season: can be mixed, but cooling trend still holds in most cases
        current_month = int(days[idx]['date'].split('-')[1])
        base_confidence = confidence
        
        if current_month >= 10 or current_month <= 4:  # Cool season
            # Increase confidence - cold season pressure-temperature relationships are strong
            confidence = min(1.0, confidence * 1.15)
        elif 5 <= current_month <= 9:  # Warm season
            # Might be somewhat weaker in some synoptic situations
            # Still use the corrected physics though
            confidence = confidence  # Keep core physics, but don't boost

        return direction, confidence

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate for specific station with direct DB access.
        """
        own_conn = conn is None
        if own_conn:
            conn = get_sqlite_connection(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            # Direct DB query
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
                if r[1] is None:
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
                return None, 0.0

            weighted_recent = self._compute_weighted_pressure(
                target_idx, days_with_pressure, lookback_hours=72
            )
            three_days_ago_pressure = days_with_pressure[target_idx - 4]['pressure']

            if weighted_recent is None or three_days_ago_pressure is None:
                return None, 0.0

            dp = weighted_recent - three_days_ago_pressure
            threshold = 3.0

            if abs(dp) < threshold:
                return None, 0.0

            # PHYSICS CORRECTION: Rising pressure → cooling
            direction = 'down' if dp > 0 else 'up'  # Falling pressure → warming

            confidence = min(abs(dp) / 5.0, 0.8)

            # Apply seasonal correction
            current_month = int(date.split('-')[1])
            if current_month >= 10 or current_month <= 4:
                confidence = min(1.0, confidence * 1.15)

            return direction, confidence

        finally:
            if own_conn and conn:
                conn.close()


def create_dual_pressure_signal(underlying_pressure_signal: BaseSignal, db_path: str = None):
    """
    Factory function to create a dual-polarity pressure signal.
    """
    return DualPolaritySignal(underlying_pressure_signal, db_path)


def test_framework():
    """Basic test of the dual-polarity framework."""
    print("=== Dual-Polarity Signal Framework Test ===")
    
    # Test season detection
    dp = DualPolaritySignal(None)
    print(f"Season for Jan 1 (winter): {dp.detect_season('2024-01-15')}")
    print(f"Season for Jul 1 (summer): {dp.detect_season('2024-07-15')}")
    
    # Test seasonal adjustments
    direction, conf = dp.adjust_for_seasonality('up', 0.6, 'cool', 'KLAX')
    print(f"Cool season: 'up' @ 0.6 → {direction} @ {conf:.2f}")
    
    direction, conf = dp.adjust_for_seasonality('up', 0.6, 'warm', 'KLAX')
    print(f"Warm season: 'up' @ 0.6 → {direction} @ {conf:.2f}")


if __name__ == "__main__":
    test_framework()