#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 23.4: Seasonal Diurnal Curve Model — expected temp by month+hour per station]
#

"""
CORE MODULE: Seasonal Diurnal Curve Model

Models the expected diurnal temperature curve for each station, used as a
baseline for anomaly detection.

The model is built from historical METAR data, computing expected temperature
for each (month, local_hour) combination. Cloud cover (from ceiling/visibility)
modulates confidence: clear conditions → more reliable curve, cloudy → less.

Usage:
    from core.seasonal_diurnal_curve import DiurnalCurveModel
    model = DiurnalCurveModel()
    expected = model.get_expected_temp('KATL', month=1, local_hour=14)
    anomaly = model.get_anomaly('KATL', month=1, local_hour=14, observed_temp=72.0)
    confidence = model.get_confidence('KATL', month=1, local_hour=14, ceiling_ft=10000)
"""

import json
import os
import logging
from typing import Dict, Optional, Tuple

_logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, '..', 'data')
_CURVE_FILE = os.path.join(_DATA_DIR, 'seasonal_diurnal_curves.json')

# Cache
_curve_cache: Optional[Dict] = None

# Station timezone offsets (UTC hours, used for conversion)
STATION_TZ = {
    'KATL': -5, 'KAUS': -6, 'KBOS': -5, 'KDAL': -6, 'KDCA': -5,
    'KDEN': -7, 'KDFW': -6, 'KHOU': -6, 'KLAS': -8, 'KLAX': -8,
    'KMDW': -6, 'KMIA': -5, 'KMSP': -6, 'KMSY': -6, 'KNYC': -5,
    'KOKC': -6, 'KPHL': -5, 'KPHX': -7, 'KSAT': -6, 'KSEA': -8, 'KSFO': -8,
}

# Cloud cover thresholds (ceiling_ft)
CEILING_CLEAR = 20000     # > 20,000 ft = clear
CEILING_HIGH_CLOUD = 12000  # 12,000-20,000 ft = high cloud
CEILING_BROKEN = 5000      # 5,000-12,000 ft = broken
CEILING_OVERCAST = 1000    # 1,000-5,000 ft = overcast
# < 1,000 ft = low ceiling / IFR

# Cloud cover confidence weights
CLOUD_CONFIDENCE = {
    'clear': 1.0,
    'high_cloud': 0.85,
    'broken': 0.70,
    'overcast': 0.55,
    'low_ceiling': 0.40,
    'unknown': 0.70,
}


def _load_curves() -> Dict:
    """Load diurnal curves from JSON cache."""
    global _curve_cache
    if _curve_cache is not None:
        return _curve_cache

    if not os.path.exists(_CURVE_FILE):
        _logger.warning(f"Diurnal curve file not found: {_CURVE_FILE}")
        _curve_cache = {}
        return _curve_cache

    try:
        with open(_CURVE_FILE, 'r') as f:
            _curve_cache = json.load(f)
        _logger.info(f"Loaded diurnal curves for {len(_curve_cache)} stations")
    except (json.JSONDecodeError, IOError) as e:
        _logger.error(f"Failed to load diurnal curves: {e}")
        _curve_cache = {}

    return _curve_cache


def _get_cloud_category(ceiling_ft: Optional[float]) -> str:
    """Classify ceiling height into cloud cover category."""
    if ceiling_ft is None:
        return 'unknown'
    if ceiling_ft >= CEILING_CLEAR:
        return 'clear'
    elif ceiling_ft >= CEILING_HIGH_CLOUD:
        return 'high_cloud'
    elif ceiling_ft >= CEILING_BROKEN:
        return 'broken'
    elif ceiling_ft >= CEILING_OVERCAST:
        return 'overcast'
    else:
        return 'low_ceiling'


class DiurnalCurveModel:
    """
    Seasonal Diurnal Temperature Curve Model.

    Provides expected temperature for each station based on month and
    local hour of day. Used as a baseline for anomaly detection.
    """

    def __init__(self, data_path: str = None):
        self.data_path = data_path or _CURVE_FILE

    # ─── Core Queries ─────────────────────────────────────────────────────────

    def get_expected_temp(self, station: str, month: int,
                          local_hour: int) -> Optional[float]:
        """
        Get expected temperature for a station at a given month and local hour.

        Args:
            station: ICAO station code
            month: Month (1-12)
            local_hour: Local hour (0-23)

        Returns:
            Expected temperature in °F, or None if no data
        """
        curves = _load_curves()
        station_data = curves.get(station.upper())
        if not station_data:
            return None

        key = f"{month}_{local_hour}"
        curve = station_data.get('diurnal_curve', {}).get(key)
        if curve is None:
            return None

        return curve.get('mean')

    def get_expected_std(self, station: str, month: int,
                         local_hour: int) -> Optional[float]:
        """
        Get expected temperature standard deviation.

        Higher std = more variable temperatures at this time (less reliable baseline).

        Args:
            station: ICAO station code
            month: Month (1-12)
            local_hour: Local hour (0-23)

        Returns:
            Standard deviation in °F, or None
        """
        curves = _load_curves()
        station_data = curves.get(station.upper())
        if not station_data:
            return None

        key = f"{month}_{local_hour}"
        curve = station_data.get('diurnal_curve', {}).get(key)
        if curve is None:
            return None

        return curve.get('std')

    def get_monthly_baseline(self, station: str, month: int) -> Optional[float]:
        """
        Get monthly average temperature baseline.

        Args:
            station: ICAO station code
            month: Month (1-12)

        Returns:
            Monthly average temperature in °F, or None
        """
        curves = _load_curves()
        station_data = curves.get(station.upper())
        if not station_data:
            return None

        baseline = station_data.get('monthly_baseline', {}).get(str(month))
        if baseline is None:
            return None

        return baseline.get('mean')

    # ─── Anomaly Detection ────────────────────────────────────────────────────

    def get_anomaly(self, station: str, month: int, local_hour: int,
                    observed_temp: float) -> Optional[float]:
        """
        Compute temperature anomaly (observed - expected).

        Positive = warmer than expected, negative = cooler than expected.

        Args:
            station: ICAO station code
            month: Month (1-12)
            local_hour: Local hour (0-23)
            observed_temp: Observed temperature in °F

        Returns:
            Anomaly in °F, or None if no expected temp available
        """
        expected = self.get_expected_temp(station, month, local_hour)
        if expected is None:
            return None
        return observed_temp - expected

    def get_anomaly_zscore(self, station: str, month: int, local_hour: int,
                           observed_temp: float) -> Optional[float]:
        """
        Compute z-score of temperature anomaly.

        z = (observed - expected) / std

        |z| > 2.0 = significant anomaly
        |z| > 3.0 = extreme anomaly

        Args:
            station: ICAO station code
            month: Month (1-12)
            local_hour: Local hour (0-23)
            observed_temp: Observed temperature in °F

        Returns:
            Z-score, or None if insufficient data
        """
        expected = self.get_expected_temp(station, month, local_hour)
        std = self.get_expected_std(station, month, local_hour)
        if expected is None or std is None or std < 0.1:
            return None

        return (observed_temp - expected) / std

    # ─── Confidence Modulation ────────────────────────────────────────────────

    def get_confidence(self, station: str, month: int, local_hour: int,
                       ceiling_ft: Optional[float] = None,
                       observed_temp: Optional[float] = None) -> float:
        """
        Get confidence score for the diurnal curve model at a given time.

        Confidence factors:
        1. Observation count for the (month, hour) bin
        2. Temperature variability (std dev) — lower = more reliable
        3. Cloud cover — clear = more reliable diurnal curve
        4. Anomaly magnitude — large anomalies = less reliable

        Args:
            station: ICAO station code
            month: Month (1-12)
            local_hour: Local hour (0-23)
            ceiling_ft: Ceiling height in feet (for cloud cover assessment)
            observed_temp: Observed temperature (for anomaly magnitude check)

        Returns:
            Confidence score between 0.0 and 1.0
        """
        curves = _load_curves()
        station_data = curves.get(station.upper())
        if not station_data:
            return 0.3

        key = f"{month}_{local_hour}"
        curve = station_data.get('diurnal_curve', {}).get(key)
        if curve is None:
            return 0.3

        # Factor 1: Observation count
        obs_count = curve.get('count', 0)
        count_score = min(1.0, obs_count / 500.0)

        # Factor 2: Temperature variability
        std = curve.get('std', 5.0)
        std_score = max(0.0, 1.0 - std / 15.0)

        # Factor 3: Cloud cover
        cloud_cat = _get_cloud_category(ceiling_ft)
        cloud_score = CLOUD_CONFIDENCE.get(cloud_cat, 0.7)

        # Factor 4: Anomaly magnitude
        anomaly_score = 1.0
        if observed_temp is not None:
            expected = curve.get('mean')
            if expected is not None:
                anomaly = abs(observed_temp - expected)
                # Large anomalies reduce confidence
                anomaly_score = max(0.3, 1.0 - anomaly / 20.0)

        # Weighted combination
        confidence = (
            0.25 * count_score +
            0.20 * std_score +
            0.30 * cloud_score +
            0.25 * anomaly_score
        )

        return round(min(1.0, max(0.1, confidence)), 3)

    # ─── Utilities ────────────────────────────────────────────────────────────

    def get_station_tz_offset(self, station: str) -> int:
        """Get UTC offset in hours for a station."""
        return STATION_TZ.get(station.upper(), -5)

    def get_observation_count(self, station: str, month: int,
                              local_hour: int) -> int:
        """Get the number of observations used for this (month, hour) bin."""
        curves = _load_curves()
        station_data = curves.get(station.upper())
        if not station_data:
            return 0
        key = f"{month}_{local_hour}"
        curve = station_data.get('diurnal_curve', {}).get(key)
        if curve is None:
            return 0
        return curve.get('count', 0)

    def has_station_data(self, station: str) -> bool:
        """Check if diurnal curve data exists for a station."""
        curves = _load_curves()
        return station.upper() in curves

    def get_all_stations(self) -> list:
        """Get list of stations with diurnal curve data."""
        curves = _load_curves()
        return list(curves.keys())


# ─── Test ──────────────────────────────────────────────────────────────────────

def test_model():
    """Test the diurnal curve model."""
    model = DiurnalCurveModel()

    # KATL January noon (local)
    print("=== KATL January 12:00 Local ===")
    expected = model.get_expected_temp('KATL', 1, 12)
    std = model.get_expected_std('KATL', 1, 12)
    monthly = model.get_monthly_baseline('KATL', 1)
    print(f"  Expected: {expected}°F, Std: {std}°F, Monthly baseline: {monthly}°F")

    # Check different times
    for hour in [0, 6, 12, 18]:
        t = model.get_expected_temp('KATL', 1, hour)
        print(f"  KATL Jan {hour:02d}:00 = {t}°F")

    for hour in [0, 6, 12, 18]:
        t = model.get_expected_temp('KATL', 7, hour)
        print(f"  KATL Jul {hour:02d}:00 = {t}°F")

    # Anomaly test
    print("\n=== Anomaly Test ===")
    anomaly = model.get_anomaly('KATL', 1, 14, 72.0)
    zscore = model.get_anomaly_zscore('KATL', 1, 14, 72.0)
    print(f"  Observed 72°F at KATL Jan 14:00: anomaly={anomaly:.1f}°F, z={zscore:.2f}")

    # Confidence
    print("\n=== Confidence Test ===")
    conf_clear = model.get_confidence('KATL', 1, 14, ceiling_ft=25000)
    conf_overcast = model.get_confidence('KATL', 1, 14, ceiling_ft=1000)
    conf_unknown = model.get_confidence('KATL', 1, 14)
    print(f"  Clear (25kft): {conf_clear}")
    print(f"  Overcast (1kft): {conf_overcast}")
    print(f"  Unknown ceiling: {conf_unknown}")


if __name__ == '__main__':
    test_model()