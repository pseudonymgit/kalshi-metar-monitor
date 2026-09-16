#!/usr/bin/env python3
"""
Temperature Trend Overlay Signal — f024
=========================================
Microstructure Trend Overlay for Kalshi weather markets.

Detects when real-time temperature is statistically approaching
a Kalshi settlement bucket boundary (integer °F) using a hybrid
approach: IEM ASOS 1-minute data for high-frequency trend estimation,
plus METAR observations (~70% fractional °F precision from °C
conversion) for sub-degree boundary proximity.

Algorithm:
  1. Load IEM ASOS 1-minute data for high-frequency trend computation
  2. Load METAR DB for decimal-precision current temperature
  3. Compute short-term trend via linear regression on ASOS observations
  4. Compute boundary proximity: distance to nearest integer °F boundary
     using decimal-precision METAR temp
  5. If trending toward boundary AND proximity < threshold AND slope magnitude > min_slope:
     calculate modifier
  6. Apply overnight boost if in 22:00-06:00 window (when boundary crosses
     are more stable due to reduced solar influence)
  7. Return modifier value (0.0 to max_boost) only if modifier > min_modifier_floor

Requirements:
  - No ML — deterministic math only
  - Handles missing observations gracefully (returns 0.0 modifier)
  - Works with integer °F input (float OK too)
  - At least 3 ASOS observations required for trend computation
  - Linear regression using simple least-squares (no scipy dependency)

B-Mode compliant. No AI/ML.
"""

import logging
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .base_signal import BaseSignal, validate_signal

logger = logging.getLogger(__name__)

# Path to databases
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ASOS_DB = os.path.join(SCRIPT_DIR, "data", "iem_asos.db")
DEFAULT_METAR_DB = os.path.join(SCRIPT_DIR, "data", "metar_backfill.db")

# Default parameter values — tuned for ASOS 5-min interval data
DEFAULT_WINDOW_SHORT = 5       # Short-term window: last N hourly observations (5 hours at hourly METAR)
DEFAULT_WINDOW_LONG = 12       # Long-term window: broader context (12 hours for hourly METAR)
DEFAULT_BOUNDARY_THRESHOLD = 0.2  # °F — max distance from boundary to activate
DEFAULT_MIN_SLOPE_MAGNITUDE = 0.3  # °F/hour — minimum trend rate to consider significant
DEFAULT_MAX_BOOST = 0.2        # Maximum confidence modifier
DEFAULT_OVERNIGHT_ONLY = True  # Only fire during overnight hours
DEFAULT_OVERNIGHT_START = 22   # 10 PM — start of overnight window
DEFAULT_OVERNIGHT_END = 6      # 6 AM — end of overnight window
DEFAULT_MIN_MODIFIER_FLOOR = 0.05  # Minimum modifier value to return signal

# Cache TTL for queries (seconds)
CACHE_TTL_SEC = 120


class TemperatureTrendSignal(BaseSignal):
    """f024 — Microstructure Trend Overlay for Kalshi weather markets.

    Detects when temperature is statistically approaching a Kalshi settlement
    bucket boundary using a hybrid approach: IEM ASOS 1-minute data for
    high-frequency trend estimation, plus METAR observations for sub-degree
    boundary proximity detection.

    Applied as a confidence modifier by returning a small boost (0.0 to max_boost)
    that can be added to the ensemble confidence when boundary-crossing conditions
    are detected. Not a standalone signal — returns modifier as pseudo-confidence
    with direction derived from the trend.

    Parameters:
        window_short: Number of most-recent ASOS observations for short-term trend (default 8)
        window_long: Number of ASOS observations for long-term context (default 24)
        boundary_threshold: Max °F distance to nearest integer boundary to activate (default 0.2)
        min_slope_magnitude: Minimum trend rate (°F/hour) to consider significant (default 0.3)
        max_boost: Maximum confidence modifier value (default 0.2)
        overnight_only: Only fire during overnight window (default True)
        overnight_start: Hour (24h) to start overnight window (default 22)
        overnight_end: Hour (24h) to end overnight window (default 6)
        min_modifier_floor: Minimum modifier value to return signal (default 0.05)
    """

    def __init__(self, db_path: str = None,
                 asos_db_path: str = None, metar_db_path: str = None,
                 window_short: int = DEFAULT_WINDOW_SHORT,
                 window_long: int = DEFAULT_WINDOW_LONG,
                 boundary_threshold: float = DEFAULT_BOUNDARY_THRESHOLD,
                 min_slope_magnitude: float = DEFAULT_MIN_SLOPE_MAGNITUDE,
                 max_boost: float = DEFAULT_MAX_BOOST,
                 overnight_only: bool = DEFAULT_OVERNIGHT_ONLY,
                 overnight_start: int = DEFAULT_OVERNIGHT_START,
                 overnight_end: int = DEFAULT_OVERNIGHT_END,
                 min_modifier_floor: float = DEFAULT_MIN_MODIFIER_FLOOR):
        super().__init__(db_path)
        self.asos_db_path = asos_db_path or DEFAULT_ASOS_DB
        self.metar_db_path = metar_db_path or DEFAULT_METAR_DB
        self._asos_db_exists = os.path.exists(self.asos_db_path)
        self._metar_db_exists = os.path.exists(self.metar_db_path)

        # Sweepable parameters
        self.window_short = window_short
        self.window_long = window_long
        self.boundary_threshold = boundary_threshold
        self.min_slope_magnitude = min_slope_magnitude
        self.max_boost = max_boost
        self.overnight_only = overnight_only
        self.overnight_start = overnight_start
        self.overnight_end = overnight_end
        self.min_modifier_floor = min_modifier_floor

        # Observation cache: {source:station:date: (timestamp, observations)}
        self._obs_cache: Dict[str, Tuple[float, List[Dict]]] = {}

    @property
    def name(self) -> str:
        return "temperature_trend"

    @property
    def min_lookback(self) -> int:
        return 1  # Only needs current day's data

    # ═══════════════════════════════════════════════════════════════════
    # BaseSignal interface
    # ═══════════════════════════════════════════════════════════════════

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate temperature trend signal at day index `idx`.

        The standard days-only interface (daily aggregated data) doesn't carry
        intraday observations needed for microstructure boundary detection.
        This returns (None, 0.0) by default.

        For real evaluation, use evaluate_for_station().
        """
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str,
                              asos_conn: sqlite3.Connection = None,
                              metar_conn: sqlite3.Connection = None,
                              current_temp_f: float = None,
                              current_hour: int = None) -> Tuple[Optional[str], float]:
        """
        Evaluate temperature trend overlay for a specific station and date.

        Hybrid approach:
          - Trend computation: IEM ASOS 1-minute data (high frequency)
          - Boundary proximity: METAR backfill data (decimal °F precision)

        Args:
            station: ICAO station code (e.g. 'KMDW')
            date: ISO date string 'YYYY-MM-DD'
            asos_conn: Optional SQLite connection for ASOS 1-min DB
            metar_conn: Optional SQLite connection for METAR DB
            current_temp_f: Current temperature in °F (if available from live feed)
            current_hour: Current hour in 24h format (UTC). If None, uses most
                          recent observation's hour.

        Returns:
            (direction, modifier) where:
              - direction: 'up' if trending toward upper boundary, 'down' if toward lower,
                           None if no signal
              - modifier: confidence boost value [0.0, max_boost], 0.0 if no signal
        """
        if not self._asos_db_exists and not self._metar_db_exists:
            return None, 0.0

        # ── Load ASOS 1-minute data for trend computation ──
        if self._asos_db_exists:
            asos_observations = self._load_asos_trend_data(station, date, asos_conn)
        else:
            asos_observations = []

        if not asos_observations or len(asos_observations) < 3:
            return None, 0.0

        # Extract ASOS temperature readings for trend
        asos_temps = self._extract_asos_temperature_series(asos_observations)

        if len(asos_temps) < 3:
            return None, 0.0

        # ── Load METAR data for decimal-precision current temp ──
        if self._metar_db_exists and current_temp_f is None:
            metar_observations = self._load_metar_data(station, date, metar_conn)
            if metar_observations:
                metar_temps = self._extract_temperature_series(metar_observations)
                if metar_temps:
                    # Use the latest METAR observation for decimal precision
                    current_temp_f = metar_temps[-1][1]

        # Use the latest ASOS temp as fallback if no METAR data
        if current_temp_f is None:
            current_temp_f = asos_temps[-1][1]

        # Determine current hour if not provided
        if current_hour is None:
            current_hour = asos_temps[-1][0].hour

        # ── Overnight gate ──
        if self.overnight_only:
            if not self._is_overnight(current_hour):
                return None, 0.0

        # ── Compute short-term trend from ASOS data ──
        short_temps = asos_temps[-self.window_short:]
        short_slope = self._compute_linear_slope(short_temps)

        if short_slope is None:
            return None, 0.0

        # ── Minimum slope magnitude check ──
        if abs(short_slope) < self.min_slope_magnitude:
            return None, 0.0

        # ── Determine trend direction (toward which boundary) ──
        nearest_upper = math.ceil(current_temp_f)
        nearest_lower = math.floor(current_temp_f)

        distance_to_upper = nearest_upper - current_temp_f
        distance_to_lower = current_temp_f - nearest_lower

        # Is the trend headed toward a boundary?
        trending_toward_upper = short_slope > 0 and distance_to_upper <= self.boundary_threshold
        trending_toward_lower = short_slope < 0 and distance_to_lower <= self.boundary_threshold

        if not trending_toward_upper and not trending_toward_lower:
            return None, 0.0

        # ── Compute distance to the approached boundary ──
        if trending_toward_upper:
            boundary_distance = distance_to_upper
            direction = 'up'
        else:
            boundary_distance = distance_to_lower
            direction = 'down'

        # ── Compute modifier ──
        modifier = self._compute_modifier(
            boundary_distance=boundary_distance,
            slope=short_slope,
            direction=direction,
            current_hour=current_hour,
            num_obs=len(asos_temps)
        )

        if modifier <= 0.0:
            return None, 0.0

        return direction, round(min(modifier, self.max_boost), 4)

    # ═══════════════════════════════════════════════════════════════════
    # ASOS 1-Minute Data Loading (for trend computation)
    # ═══════════════════════════════════════════════════════════════════

    def _load_asos_trend_data(self, station: str, date: str,
                               conn: sqlite3.Connection = None) -> List[Dict]:
        """Load IEM ASOS 1-minute observations for trend computation.

        Returns list of dicts with keys: valid, tmpf.
        """
        cache_key = f"asos:{station}:{date}"
        now = datetime.now(timezone.utc).timestamp()
        if cache_key in self._obs_cache:
            cached_at, cached_data = self._obs_cache[cache_key]
            if now - cached_at < CACHE_TTL_SEC:
                return cached_data

        own_conn = conn is None
        if own_conn:
            if not os.path.exists(self.asos_db_path):
                return []
            conn = sqlite3.connect(self.asos_db_path)

        try:
            cur = conn.execute("""
                SELECT valid, tmpf
                FROM asos_observations
                WHERE station = ? AND DATE(valid) = ?
                AND tmpf IS NOT NULL
                ORDER BY valid ASC
            """, (station, date))

            observations = []
            for row in cur.fetchall():
                observations.append({
                    "valid": row[0],
                    "tmpf": row[1],
                })

            self._obs_cache[cache_key] = (now, observations)
            return observations

        except Exception as e:
            logger.debug(f"Error loading ASOS data for {station} {date}: {e}")
            return []
        finally:
            if own_conn and conn:
                conn.close()

    def _extract_asos_temperature_series(self, observations: List[Dict]) -> List[Tuple[datetime, float]]:
        """Extract (datetime, temp_f) pairs from ASOS observations.

        ASOS timestamps are in format 'YYYY-MM-DD HH:MM'.
        """
        series = []
        for obs in observations:
            temp = obs.get("tmpf")
            if temp is None:
                continue
            ts = obs.get("valid")
            if ts is None:
                continue
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                try:
                    dt = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    continue
            series.append((dt, temp))

        series.sort(key=lambda x: x[0])
        return series

    # ═══════════════════════════════════════════════════════════════════
    # METAR Data Loading (for decimal-precision current temp)
    # ═══════════════════════════════════════════════════════════════════

    def _load_metar_data(self, station: str, date: str,
                          conn: sqlite3.Connection = None) -> List[Dict]:
        """Load hourly METAR observations for decimal-precision temperature.

        Returns list of dicts with keys: timestamp_utc, temp_f.
        """
        cache_key = f"metar:{station}:{date}"
        now = datetime.now(timezone.utc).timestamp()
        if cache_key in self._obs_cache:
            cached_at, cached_data = self._obs_cache[cache_key]
            if now - cached_at < CACHE_TTL_SEC:
                return cached_data

        own_conn = conn is None
        if own_conn:
            if not os.path.exists(self.metar_db_path):
                return []
            conn = sqlite3.connect(self.metar_db_path)

        try:
            cur = conn.execute("""
                SELECT timestamp_utc, temp_f
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                AND temp_f IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, date))

            observations = []
            for row in cur.fetchall():
                observations.append({
                    "timestamp_utc": row[0],
                    "temp_f": row[1],
                })

            self._obs_cache[cache_key] = (now, observations)
            return observations

        except Exception as e:
            logger.debug(f"Error loading METAR data for {station} {date}: {e}")
            return []
        finally:
            if own_conn and conn:
                conn.close()

    def _extract_temperature_series(self, observations: List[Dict]) -> List[Tuple[datetime, float]]:
        """Extract (datetime, temp_f) pairs from METAR observations.

        METAR timestamps are in ISO 8601 format (e.g. '2024-01-01T01:00:00+00:00').
        Handles fallback for non-ISO timestamps.
        """
        series = []
        for obs in observations:
            temp = obs.get("temp_f")
            if temp is None:
                continue
            ts = obs.get("timestamp_utc") or obs.get("valid")
            if ts is None:
                continue
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    continue
            series.append((dt, temp))

        series.sort(key=lambda x: x[0])
        return series

    # ═══════════════════════════════════════════════════════════════════
    # Linear Regression (Simple Least-Squares — No Scipy)
    # ═══════════════════════════════════════════════════════════════════

    def _compute_linear_slope(self, readings: List[Tuple[datetime, float]]) -> Optional[float]:
        """Compute linear regression slope using simple least-squares.

        Implements: slope = (n*sum(xy) - sum(x)*sum(y)) / (n*sum(x²) - sum(x)²)

        Args:
            readings: List of (datetime, temp_f) tuples, sorted by time

        Returns:
            Slope in °F per hour, or None if insufficient data (< 3 readings)
        """
        if len(readings) < 3:
            return None

        # Convert timestamps to hours relative to first observation
        base_time = readings[0][0]
        n = len(readings)

        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0

        for dt, temp in readings:
            x_hours = (dt - base_time).total_seconds() / 3600.0
            y = float(temp)

            sum_x += x_hours
            sum_y += y
            sum_xy += x_hours * y
            sum_x2 += x_hours * x_hours

        denominator = n * sum_x2 - sum_x * sum_x
        if denominator == 0:
            return None  # All x values identical (shouldn't happen with timestamps)

        slope = (n * sum_xy - sum_x * sum_y) / denominator
        return slope

    # ═══════════════════════════════════════════════════════════════════
    # Overnight Detection
    # ═══════════════════════════════════════════════════════════════════

    def _is_overnight(self, hour: int) -> bool:
        """Check if the given hour is within the overnight window.

        Handles wrap-around: e.g., 22:00 to 06:00 means
        22, 23, 0, 1, 2, 3, 4, 5 are overnight.

        Args:
            hour: Hour in 24h format (0-23)

        Returns:
            True if within overnight window
        """
        if self.overnight_start <= self.overnight_end:
            return self.overnight_start <= hour <= self.overnight_end
        else:
            return hour >= self.overnight_start or hour <= self.overnight_end

    # ═══════════════════════════════════════════════════════════════════
    # Modifier Computation
    # ═══════════════════════════════════════════════════════════════════

    def _compute_modifier(self, boundary_distance: float, slope: float,
                           direction: str, current_hour: int,
                           num_obs: int) -> float:
        """Compute the confidence modifier based on boundary proximity and trend.

        The modifier is a function of:
          - Distance to boundary (closer = stronger)
          - Slope magnitude (steeper = more confident approach)
          - Overnight boost (stable conditions = less noise)
          - Number of observations (more = more reliable)

        Args:
            boundary_distance: Distance in °F to the approached boundary [0, threshold]
            slope: Linear regression slope in °F/hour
            direction: 'up' or 'down'
            current_hour: Current hour (24h format)
            num_obs: Number of temperature observations available

        Returns:
            Modifier value [0.0, max_boost]
        """
        # ── Inverse-distance weighting ──
        proximity_weight = 1.0 - (boundary_distance / self.boundary_threshold)
        proximity_weight = max(0.0, min(1.0, proximity_weight))

        # ── Slope magnitude factor ──
        abs_slope = abs(slope)
        slope_weight = min(1.0, abs_slope / 1.0)

        # Additional check: ensure slope meets minimum magnitude requirement
        if abs_slope < self.min_slope_magnitude:
            slope_weight = 0.0

        # ── Overnight boost ──
        overnight_boost = 1.0
        if self._is_overnight(current_hour):
            overnight_boost = 1.2

        # ── Observation count confidence ──
        obs_weight = min(1.0, num_obs / 30.0)

        # ── Combine into final modifier ──
        modifier = (proximity_weight * 0.6 +
                    slope_weight * 0.25 +
                    obs_weight * 0.15) * overnight_boost * self.max_boost

        # ── Apply minimum modifier floor ──
        if modifier < self.min_modifier_floor:
            return 0.0

        return max(0.0, min(modifier, self.max_boost))

    # ═══════════════════════════════════════════════════════════════════
    # Audit / Summary Helpers
    # ═══════════════════════════════════════════════════════════════════

    def get_trend_summary(self, station: str, date: str) -> Dict:
        """Get a human-readable summary of trend conditions for a station/date.

        Useful for debugging and auditing.

        Returns a dict with:
          - station, date
          - has_data: whether ASOS data exists
          - signal_fired: whether the signal produced a modifier > 0
          - direction: 'up' or 'down' (or None)
          - modifier: computed modifier value
          - asos_readings_count: number of ASOS temp observations available
          - metar_temp: decimal-precision temp from METAR DB (if available)
          - short_slope: short-term trend slope (°F/hour)
          - boundary_distance: distance to nearest boundary (°F)
          - is_overnight: whether current hour is in overnight window
          - params: current parameter values
        """
        result = {
            "station": station,
            "date": date,
            "has_asos_data": False,
            "has_metar_data": False,
            "signal_fired": False,
            "direction": None,
            "modifier": 0.0,
            "asos_readings_count": 0,
            "metar_temp": None,
            "short_slope": None,
            "boundary_distance": None,
            "is_overnight": False,
            "params": {
                "window_short": self.window_short,
                "window_long": self.window_long,
                "boundary_threshold": self.boundary_threshold,
                "min_slope_magnitude": self.min_slope_magnitude,
                "max_boost": self.max_boost,
                "overnight_only": self.overnight_only,
                "min_modifier_floor": self.min_modifier_floor,
            }
        }

        # Load ASOS data for trend
        asos_obs = self._load_asos_trend_data(station, date)
        if not asos_obs:
            return result

        result["has_asos_data"] = True
        asos_temps = self._extract_asos_temperature_series(asos_obs)
        result["asos_readings_count"] = len(asos_temps)

        if len(asos_temps) < 3:
            return result

        # Compute short-term trend
        short_temps = asos_temps[-self.window_short:]
        result["short_slope"] = self._compute_linear_slope(short_temps)

        if result["short_slope"] is None:
            return result

        # Check overnight
        current_hour = asos_temps[-1][0].hour
        result["is_overnight"] = self._is_overnight(current_hour)

        if self.overnight_only and not result["is_overnight"]:
            return result

        # Get decimal-precision current temp from METAR
        current_temp = asos_temps[-1][1]  # fallback
        metar_obs = self._load_metar_data(station, date)
        if metar_obs:
            metar_temps = self._extract_temperature_series(metar_obs)
            if metar_temps:
                current_temp = metar_temps[-1][1]
                result["metar_temp"] = current_temp
                result["has_metar_data"] = True

        # Boundary distances
        nearest_upper = math.ceil(current_temp)
        nearest_lower = math.floor(current_temp)

        dist_upper = nearest_upper - current_temp
        dist_lower = current_temp - nearest_lower

        slope = result["short_slope"]
        if slope > 0 and dist_upper <= self.boundary_threshold:
            result["boundary_distance"] = dist_upper
            result["direction"] = 'up'
        elif slope < 0 and dist_lower <= self.boundary_threshold:
            result["boundary_distance"] = dist_lower
            result["direction"] = 'down'

        if result["direction"] and result["boundary_distance"] is not None:
            modifier = self._compute_modifier(
                boundary_distance=result["boundary_distance"],
                slope=slope,
                direction=result["direction"],
                current_hour=current_hour,
                num_obs=len(asos_temps)
            )
            result["modifier"] = round(modifier, 4)
            result["signal_fired"] = modifier > 0.0

        return result

    def has_asos_data(self, station: str, date: str) -> bool:
        """Quick check if ASOS data exists for a station/date."""
        if not self._asos_db_exists:
            return False
        try:
            conn = sqlite3.connect(self.asos_db_path)
            cur = conn.execute(
                "SELECT COUNT(*) FROM asos_observations WHERE station = ? AND DATE(valid) = ?",
                (station, date)
            )
            count = cur.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def has_metar_data(self, station: str, date: str) -> bool:
        """Quick check if METAR data exists for a station/date."""
        if not self._metar_db_exists:
            return False
        try:
            conn = sqlite3.connect(self.metar_db_path)
            cur = conn.execute(
                "SELECT COUNT(*) FROM metar_observations WHERE station = ? AND date_utc = ?",
                (station, date)
            )
            count = cur.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def create_temperature_trend_signal(
    db_path: str = None,
    asos_db_path: str = None,
    metar_db_path: str = None,
    window_short: int = DEFAULT_WINDOW_SHORT,
    window_long: int = DEFAULT_WINDOW_LONG,
    boundary_threshold: float = DEFAULT_BOUNDARY_THRESHOLD,
    min_slope_magnitude: float = DEFAULT_MIN_SLOPE_MAGNITUDE,
    max_boost: float = DEFAULT_MAX_BOOST,
    overnight_only: bool = DEFAULT_OVERNIGHT_ONLY,
    overnight_start: int = DEFAULT_OVERNIGHT_START,
    overnight_end: int = DEFAULT_OVERNIGHT_END,
    min_modifier_floor: float = DEFAULT_MIN_MODIFIER_FLOOR,
) -> TemperatureTrendSignal:
    """Factory function to create a TemperatureTrendSignal instance."""
    return TemperatureTrendSignal(
        db_path=db_path,
        asos_db_path=asos_db_path,
        metar_db_path=metar_db_path,
        window_short=window_short,
        window_long=window_long,
        boundary_threshold=boundary_threshold,
        min_slope_magnitude=min_slope_magnitude,
        max_boost=max_boost,
        overnight_only=overnight_only,
        overnight_start=overnight_start,
        overnight_end=overnight_end,
        min_modifier_floor=min_modifier_floor,
    )