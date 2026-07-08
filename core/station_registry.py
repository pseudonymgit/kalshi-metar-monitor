#!/usr/bin/env python3
"""
CORE MODULE: Station Registry — Authoritative Station List

Single source of truth for weather market station codes.

Fallback chain:
1. Try Kalshi discovery (will fail — DNS in container)
2. Fall back to data/station_mapping.json cache
3. Fall back to settlement_epochs table in metar_backfill.db

Usage:
    from station_registry import get_all_stations, get_station_mapping
    stations = get_all_stations()
    mapping = get_station_mapping()
"""

import json
import os
import sqlite3
from functools import lru_cache
from typing import Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_BASE, "data", "metar_backfill.db")
_CACHE_PATH = os.path.join(_BASE, "data", "station_mapping.json")

# Static mapping as ultimate fallback — verified against Kalshi settlement data
# 15 viable stations (20 verified - 4 negative-EV unmapped - 1 duplicate KDAL)
# Removed: KDAL (dup of KDFW), KLAS, KMSY, KOKC, KSAT (no price fetcher mapping → 0.50 fallback → negative-EV)
_STATIC_MAPPING = {
    "KATL": {"city_name": "Atlanta", "state": "GA", "lat": 33.6407, "lon": -84.4277},
    "KAUS": {"city_name": "Austin", "state": "TX", "lat": 30.1945, "lon": -97.6699},
    "KBOS": {"city_name": "Boston", "state": "MA", "lat": 42.3656, "lon": -71.0096},
    "KDCA": {"city_name": "Washington DC", "state": "DC", "lat": 38.8512, "lon": -77.0402},
    "KDEN": {"city_name": "Denver", "state": "CO", "lat": 39.8561, "lon": -104.6737},
    "KDFW": {"city_name": "Dallas", "state": "TX", "lat": 32.8998, "lon": -97.0403},
    "KHOU": {"city_name": "Houston", "state": "TX", "lat": 29.6454, "lon": -95.2789},
    "KJFK": {"city_name": "New York", "state": "NY", "lat": 40.6413, "lon": -73.7781},
    "KLAX": {"city_name": "Los Angeles", "state": "CA", "lat": 33.9425, "lon": -118.4081},
    "KMIA": {"city_name": "Miami", "state": "FL", "lat": 25.7959, "lon": -80.2870},
    "KMSP": {"city_name": "Minneapolis", "state": "MN", "lat": 44.8848, "lon": -93.2223},
    "KORD": {"city_name": "Chicago", "state": "IL", "lat": 41.9804, "lon": -87.9082},
    "KPHL": {"city_name": "Philadelphia", "state": "PA", "lat": 39.8744, "lon": -75.2424},
    "KPHX": {"city_name": "Phoenix", "state": "AZ", "lat": 33.4342, "lon": -112.0116},
    "KSEA": {"city_name": "Seattle", "state": "WA", "lat": 47.4502, "lon": -122.3088},
    "KSFO": {"city_name": "San Francisco", "state": "CA", "lat": 37.6213, "lon": -122.3790},
}

# Stations removed from registry (negative-EV, duplicates, or no price fetcher mapping)
# KDAL: duplicate of KDFW — Kalshi settles Dallas on KDFW
# KLAS: no price fetcher mapping → 0.50 fallback → negative-EV
# KMSY: no price fetcher mapping → 0.50 fallback → negative-EV
# KOKC: no price fetcher mapping → 0.50 fallback → negative-EV
# KSAT: no price fetcher mapping → 0.50 fallback → negative-EV
# KNYC: duplicate of KJFK — same New York market, METAR uses KJFK
# KMDW: duplicate of KORD — same Chicago market, METAR uses KORD
_REMOVED_STATIONS = {"KDAL", "KLAS", "KMSY", "KOKC", "KSAT", "KNYC", "KMDW"}


def _try_kalshi_discovery():
    """Try to discover stations from Kalshi API. Will fail in container (DNS)."""
    try:
        from core.kalshi_monitor import discover_market_derived_station_codes
        stations = discover_market_derived_station_codes(max_pages=3, page_limit=100)
        if stations:
            return stations
    except Exception:
        pass
    return None


def _try_cache_file():
    """Load station list from cache file."""
    try:
        with open(_CACHE_PATH, 'r') as f:
            data = json.load(f)
            if isinstance(data, dict) and 'stations' in data:
                return list(data['stations'].keys())
            elif isinstance(data, dict):
                return list(data.keys())
    except Exception:
        pass
    return None


def _try_settlement_epochs():
    """Load station list from settlement_epochs table in metar_backfill.db."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        stations = [r[0] for r in c.fetchall()]
        conn.close()
        if stations:
            return stations
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def get_all_stations():
    """
    Returns the authoritative list of viable station codes.
    
    Stations in _REMOVED_STATIONS are filtered out regardless of source.
    These are negative-EV markets (no price fetcher mapping → 0.50 fallback)
    or duplicates (e.g., KDAL duplicates KDFW).
    
    Fallback chain:
    1. Kalshi discovery (will fail — DNS in container)
    2. Cache file (data/station_mapping.json)
    3. Settlement epochs table in metar_backfill.db
    4. Static hardcoded list (verified against Kalshi)
    """
    # 1. Try Kalshi discovery
    stations = _try_kalshi_discovery()
    if stations:
        return sorted(set(stations) - _REMOVED_STATIONS)
    
    # 2. Try cache file
    stations = _try_cache_file()
    if stations:
        return sorted(set(stations) - _REMOVED_STATIONS)
    
    # 3. Try settlement epochs table
    stations = _try_settlement_epochs()
    if stations:
        return sorted(set(stations) - _REMOVED_STATIONS)
    
    # 4. Static fallback
    return sorted(list(_STATIC_MAPPING.keys()))


@lru_cache(maxsize=1)
def get_station_mapping():
    """
    Returns dict of station -> {city_name, state, lat, lon}.
    
    Stations in _REMOVED_STATIONS are filtered out.
    
    Uses the same fallback chain as get_all_stations().
    """
    result = {}
    
    # Try cache file first for full mapping (includes lat/lon)
    try:
        with open(_CACHE_PATH, 'r') as f:
            data = json.load(f)
            if isinstance(data, dict):
                if 'stations' in data:
                    for code, info in data['stations'].items():
                        if code not in _REMOVED_STATIONS:
                            result[code] = info
                else:
                    for code, info in data.items():
                        if code not in _REMOVED_STATIONS:
                            result[code] = info
                if result:
                    return result
    except Exception:
        pass
    
    # Fall back to static mapping
    for code, info in _STATIC_MAPPING.items():
        result[code] = info
    
    return result


def get_station_city_name(station_code):
    """Returns the city name for a station code, or the code itself if unknown."""
    mapping = get_station_mapping()
    info = mapping.get(station_code, {})
    return info.get('city_name', station_code)


def get_station_coordinates(station_code):
    """Returns (lat, lon) for a station code, or (None, None) if unknown."""
    mapping = get_station_mapping()
    info = mapping.get(station_code, {})
    return info.get('lat'), info.get('lon')


# ─── R4-1.6: Correlation Clusters for Budget Caps ───────────────────────
#
# Stations grouped by shared weather system geography.
# Temperature correlation within clusters is high (ρ ≈ 0.5-0.7) because
# the same frontal systems affect all stations in the cluster.
# Cluster budget cap prevents over-concentration in one weather event.

STATION_CLUSTERS = {
    "northeast":     ["KJFK", "KPHL", "KBOS", "KDCA"],
    "gulf_south":    ["KHOU", "KMIA", "KATL", "KDFW", "KAUS"],
    "plains_midwest": ["KDEN", "KMSP", "KORD"],
    "southwest":     ["KPHX"],
    "west_coast":    ["KLAX", "KSEA", "KSFO"],
}

# Reverse mapping: station → cluster name
_STATION_TO_CLUSTER = {
    station: cluster for cluster, stations in STATION_CLUSTERS.items()
    for station in stations
}

# Budget caps
CLUSTER_BUDGET_USD = 30.0   # max total exposure per cluster
CITY_PAIR_CAP_USD = 12.0     # max net exposure per city (HIGH+LOW pair)


def get_cluster_for_station(station: str) -> Optional[str]:
    """Returns the cluster name for a station, or None if unclustered."""
    return _STATION_TO_CLUSTER.get(station.upper())


def get_cluster_stations(cluster_name: str) -> list:
    """Returns all stations in a cluster."""
    return STATION_CLUSTERS.get(cluster_name, [])


def validate_station_registry():
    """
    Validate that every station in the registry has a corresponding
    Kalshi price fetcher mapping. Returns a dict with validation results.
    
    This is the station registry validation gate — stations without
    a price fetcher mapping get 0.50 fallback and are guaranteed negative-EV.
    """
    try:
        from kalshi_price_fetcher import STATION_TO_KALSHI_CODE
    except ImportError:
        return {"error": "kalshi_price_fetcher not available", "valid": False}
    
    registry_stations = set(get_all_stations())
    fetcher_stations = set(STATION_TO_KALSHI_CODE.keys())
    
    unmapped = registry_stations - fetcher_stations
    extra = fetcher_stations - registry_stations
    
    return {
        "valid": len(unmapped) == 0,
        "registry_count": len(registry_stations),
        "fetcher_count": len(fetcher_stations),
        "unmapped_stations": sorted(unmapped),
        "extra_in_fetcher": sorted(extra),
        "removed_stations": sorted(_REMOVED_STATIONS),
    }


if __name__ == "__main__":
    stations = get_all_stations()
    mapping = get_station_mapping()
    print(f"Station Registry: {len(stations)} stations")
    for s in stations:
        info = mapping.get(s, {})
        print(f"  {s} -> {info.get('city_name', '?')}, {info.get('state', '?')} ({info.get('lat', '?')}, {info.get('lon', '?')})")
