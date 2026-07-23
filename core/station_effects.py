#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 23.1: Station-Specific Wind→Temperature Effects — build from 24-month METAR data]
#

"""
CORE MODULE: Station-Specific Effects

Provides station-specific wind direction → temperature change mappings
derived from 24+ months of METAR observations.

Key findings (from data/station_wind_effects.json):
- KLAX: NE winds (Santa Ana) → +1.7°F warming (opposite of global northerly=cooling rule)
- KDEN: W winds (downslope) → +1.5°F warming; E winds (upslope) → +0.4°F warming
- KSEA: E winds (offshore/Cascades downslope) → +1.0°F warming; S winds → -0.6°F cooling
- KSFO: W winds (onshore marine) → -0.2°F cooling; E winds (offshore) → +0.5°F warming

Usage:
    from core.station_effects import get_wind_delta_t, is_warming_wind
    delta = get_wind_delta_t('KLAX', 45.0)  # Santa Ana: returns ~+1.7
    is_warm = is_warming_wind('KLAX', 45.0)  # True (NE winds warm LA)
"""

import json
import os
import logging
from typing import Optional, Dict, List, Tuple

_logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, '..', 'data')
_EFFECTS_FILE = os.path.join(_DATA_DIR, 'station_wind_effects.json')

# Cache for loaded effects
_effects_cache: Optional[Dict[str, Dict[str, Dict]]] = None

# Sector centers (every 22.5 degrees starting from 0)
SECTOR_CENTERS = [0, 22.5, 45, 67.5, 90, 112.5, 135, 157.5,
                  180, 202.5, 225, 247.5, 270, 292.5, 315, 337.5]

SECTOR_LABELS = {
    0: 'N', 22.5: 'NNE', 45: 'NE', 67.5: 'ENE',
    90: 'E', 112.5: 'ESE', 135: 'SE', 157.5: 'SSE',
    180: 'S', 202.5: 'SSW', 225: 'SW', 247.5: 'WSW',
    270: 'W', 292.5: 'WNW', 315: 'NW', 337.5: 'NNW'
}


def _load_effects() -> Dict[str, Dict[str, Dict]]:
    """Load station wind effects from JSON cache file."""
    global _effects_cache
    if _effects_cache is not None:
        return _effects_cache

    if not os.path.exists(_EFFECTS_FILE):
        _logger.warning(f"Station effects file not found: {_EFFECTS_FILE}")
        _effects_cache = {}
        return _effects_cache

    try:
        with open(_EFFECTS_FILE, 'r') as f:
            _effects_cache = json.load(f)
        _logger.info(f"Loaded station effects for {len(_effects_cache)} stations")
    except (json.JSONDecodeError, IOError) as e:
        _logger.error(f"Failed to load station effects: {e}")
        _effects_cache = {}

    return _effects_cache


def _nearest_sector(wind_direction_deg: float) -> str:
    """Map a wind direction to the nearest 22.5-degree sector."""
    if wind_direction_deg is None:
        return None
    # Normalize to 0-360
    direction = wind_direction_deg % 360.0
    # Find nearest sector center
    nearest = min(SECTOR_CENTERS, key=lambda x: min(abs(x - direction), abs(x + 360 - direction)))
    return str(int(nearest)) if nearest == int(nearest) else str(nearest)


def get_wind_delta_t(station: str, wind_direction_deg: float) -> Optional[float]:
    """
    Get the expected next-day temperature change (ΔT in °F) for a given
    station and wind direction, based on historical regression.

    Args:
        station: ICAO station code (e.g. 'KLAX')
        wind_direction_deg: Wind direction in degrees (0-360)

    Returns:
        Expected ΔT in °F, or None if no data for that station/sector
    """
    effects = _load_effects()
    station_data = effects.get(station.upper())
    if not station_data:
        return None

    sector = _nearest_sector(wind_direction_deg)
    if sector is None:
        return None

    sector_data = station_data.get(sector)
    if sector_data is None:
        return None

    return sector_data.get('delta_t_f')


def is_warming_wind(station: str, wind_direction_deg: float) -> Optional[bool]:
    """
    Determine if the current wind direction predicts warming (True) or
    cooling (False) for this specific station.

    Returns None if no data available.
    """
    delta = get_wind_delta_t(station, wind_direction_deg)
    if delta is None:
        return None
    return delta > 0.0


def get_wind_effect_confidence(station: str, wind_direction_deg: float) -> float:
    """
    Get a confidence score (0.0-1.0) for the station-specific wind effect.

    Higher confidence when:
    - The sector has >100 observations
    - The effect magnitude is >0.5°F

    Returns:
        Confidence score between 0.0 and 1.0
    """
    effects = _load_effects()
    station_data = effects.get(station.upper())
    if not station_data:
        return 0.0

    sector = _nearest_sector(wind_direction_deg)
    if sector is None or sector not in station_data:
        return 0.0

    sd = station_data[sector]
    obs = sd.get('obs_count', 0)
    delta = abs(sd.get('delta_t_f', 0.0))

    # Base confidence from observation count
    obs_conf = min(1.0, obs / 500.0)

    # Boost from effect magnitude
    magnitude_conf = min(1.0, delta / 3.0)

    return min(1.0, 0.3 * obs_conf + 0.7 * magnitude_conf)


def get_station_summary(station: str) -> Dict:
    """
    Get a human-readable summary of wind effects for a station.

    Returns dict with:
    - station: ICAO code
    - sectors: list of ({sector, label, delta_t_f, obs_count, is_warming})
    - warming_sectors: wind directions that typically warm
    - cooling_sectors: wind directions that typically cool
    """
    effects = _load_effects()
    station_data = effects.get(station.upper())
    if not station_data:
        return {'station': station, 'sectors': [], 'warming_sectors': [], 'cooling_sectors': []}

    sectors = []
    warming = []
    cooling = []

    for sector_str, data in sorted(station_data.items(), key=lambda x: float(x[0])):
        sector_float = float(sector_str)
        label = SECTOR_LABELS.get(sector_float, f'{sector_float}°')
        entry = {
            'sector': sector_float,
            'label': label,
            'delta_t_f': data['delta_t_f'],
            'obs_count': data['obs_count'],
            'is_warming': data['delta_t_f'] > 0,
        }
        sectors.append(entry)
        if entry['is_warming']:
            warming.append(entry)
        else:
            cooling.append(entry)

    return {
        'station': station,
        'sectors': sectors,
        'warming_sectors': warming,
        'cooling_sectors': cooling,
    }


def get_global_wind_rule(wind_direction_deg: float) -> Tuple[str, float]:
    """
    Fallback global wind direction → temperature rule for stations without
    specific data. This is the standard mid-latitude Northern Hemisphere rule.

    Args:
        wind_direction_deg: Wind direction in degrees

    Returns:
        (direction, confidence) where direction is 'up' or 'down'
    """
    wd = wind_direction_deg % 360.0

    # Northerly (cooling): 315-360 and 0-90
    # Southerly (warming): 135-225
    # Easterly: variable (moderate)
    # Westerly: variable (moderate)

    if (315 <= wd <= 360) or (0 <= wd <= 90):
        # Northerly component — cooling
        nw_factor = 1.0 - abs(wd - 45) / 90.0 if wd <= 90 else abs(wd - 337.5) / 45.0
        return 'down', 0.35 + 0.15 * nw_factor
    elif 135 <= wd <= 225:
        # Southerly component — warming
        s_factor = 1.0 - abs(wd - 180) / 45.0
        return 'up', 0.35 + 0.15 * s_factor
    else:
        # Easterly (90-135) or Westerly (225-315) — variable
        # Slight westerly bias for mid-latitudes
        if 225 <= wd <= 315:
            return 'up', 0.30  # Westerly: mild warming (maritime)
        else:
            return 'down', 0.30  # Easterly: mild cooling


# ─── Test ──────────────────────────────────────────────────────────────────────

def test_station_effects():
    """Test the station-specific effects."""
    # KLAX Santa Ana test
    print("=== KLAX Santa Ana (NE wind, 45°) ===")
    delta = get_wind_delta_t('KLAX', 45.0)
    warm = is_warming_wind('KLAX', 45.0)
    print(f"  ΔT: {delta}°F, Warming: {warm}")

    # KLAX sea breeze (SW wind, 225°)
    print("=== KLAX Sea Breeze (SW wind, 225°) ===")
    delta = get_wind_delta_t('KLAX', 225.0)
    warm = is_warming_wind('KLAX', 225.0)
    print(f"  ΔT: {delta}°F, Warming: {warm}")

    # KDEN upslope (E wind, 90°)
    print("=== KDEN Upslope (E wind, 90°) ===")
    delta = get_wind_delta_t('KDEN', 90.0)
    warm = is_warming_wind('KDEN', 90.0)
    print(f"  ΔT: {delta}°F, Warming: {warm}")

    # KSEA marine push (S wind, 180°)
    print("=== KSEA Marine Push (S wind, 180°) ===")
    delta = get_wind_delta_t('KSEA', 180.0)
    warm = is_warming_wind('KSEA', 180.0)
    print(f"  ΔT: {delta}°F, Warming: {warm}")

    # Summary
    print("\n=== KLAX Summary ===")
    summary = get_station_summary('KLAX')
    for s in summary['sectors']:
        print(f"  {s['label']:4s} ({s['sector']:5.0f}°): ΔT={s['delta_t_f']:+.2f}°F  obs={s['obs_count']}")


if __name__ == '__main__':
    test_station_effects()