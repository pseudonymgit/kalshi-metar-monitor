#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-13 A1: Fix alert dummy data - add bucket fallback to most recent available data]
# 2. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
# 3. [2026-07-06 C4: Restore KLAS/KMSY/KOKC/KSAT to station registry with Kalshi price fetcher mappings]
# 4. [2026-07-05 R4-1.6: Cluster budget caps + same-city pair hedging]
# 5. [2026-07-05 R4-1.2: Purge negative-EV markets + fix station registry + dedup]
# 6. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

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
_RESEARCH_STATION_CODES = [
    'KATL', 'KBOS', 'KDFW', 'KDEN', 'KJFK', 
    'KLAX', 'KMIA', 'KORD', 'KSEA', 'KSFO', 
    'KBNA', 'KHOU', 'KDCA', 'KPDX', 'KSLC', 
    'PHNL', 'KTPA', 'KDTW', 'KCLT', 'KMSP'
]


STATIC_MAPPING = {
    'KATL': {'city': 'Atlanta', 'state': 'GA', 'tz': 'America/New_York'},
    'KBOS': {'city': 'Boston', 'state': 'MA', 'tz': 'America/New_York'},
    'KDFW': {'city': 'Dallas-Fort Worth', 'state': 'TX', 'tz': 'America/Chicago'},
    'KDEN': {'city': 'Denver', 'state': 'CO', 'tz': 'America/Denver'},
    'KJFK': {'city': 'New York', 'state': 'NY', 'tz': 'America/New_York'},
    'KLAX': {'city': 'Los Angeles', 'state': 'CA', 'tz': 'America/Los_Angeles'},
    'KMIA': {'city': 'Miami', 'state': 'FL', 'tz': 'America/New_York'},
    'KORD': {'city': 'Chicago', 'state': 'IL', 'tz': 'America/Chicago'},
    'KSEA': {'city': 'Seattle', 'state': 'WA', 'tz': 'America/Los_Angeles'},
    'KSFO': {'city': 'San Francisco', 'state': 'CA', 'tz': 'America/Los_Angeles'},
    'KBNA': {'city': 'Nashville', 'state': 'TN', 'tz': 'America/Chicago'},
    'KHOU': {'city': 'Houston', 'state': 'TX', 'tz': 'America/Chicago'},
    'KDCA': {'city': 'Washington DC', 'state': 'VA', 'tz': 'America/New_York'},
    'KPDX': {'city': 'Portland', 'state': 'OR', 'tz': 'America/Los_Angeles'},
    'KSLC': {'city': 'Salt Lake City', 'state': 'UT', 'tz': 'America/Denver'},
    'PHNL': {'city': 'Honolulu', 'state': 'HI', 'tz': 'Pacific/Honolulu'},
    'KTPA': {'city': 'Tampa', 'state': 'FL', 'tz': 'America/New_York'},
    'KDTW': {'city': 'Detroit', 'state': 'MI', 'tz': 'America/Detroit'},
    'KCLT': {'city': 'Charlotte', 'state': 'NC', 'tz': 'America/New_York'},
    'KMSP': {'city': 'Minneapolis', 'state': 'MN', 'tz': 'America/Chicago'}
}


# Stations removed from registry (duplicates only)
_REMOVED_STATIONS = {"KDAL"}


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
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
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
    
    # 4. Static fallback using research station codes
    return sorted(_RESEARCH_STATION_CODES)


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
    for code, info in STATIC_MAPPING.items():
        result[code] = info
    
    return result


def get_station_city_name(station_code):
    """Returns the city name for a station code, or the code itself if unknown."""
    mapping = get_station_mapping()
    info = mapping.get(station_code, {})
    return info.get('city', info.get('city_name', station_code))


def get_station_coordinates(station_code):
    """Returns (lat, lon) for a station code, or (None, None) if unknown."""
    mapping = get_station_mapping()
    info = mapping.get(station_code, {})
    # Return (lat, lon) tuple from static mapping which might have them in different formats
    lat = info.get('lat') or info.get('latitude')
    lon = info.get('lon') or info.get('longitude')
    return lat, lon


# ─── R4-1.6: Correlation Clusters for Budget Caps ───────────────────────
#
# Stations grouped by shared weather system geography.
# Temperature correlation within clusters is high (ρ ≈ 0.5-0.7) because
# the same frontal systems affect all stations in the cluster.
# Cluster budget cap prevents over-concentration in one weather event.

STATION_CLUSTERS = {
    "northeast":     ["KNYC", "KPHL", "KBOS", "KDCA"],
    "gulf_south":    ["KHOU", "KMIA", "KATL", "KDFW", "KAUS", "KMSY"],
    "plains_midwest": ["KDEN", "KMSP", "KMDW", "KOKC"],
    "southwest":     ["KPHX", "KLAS"],
    "west_coast":    ["KLAX", "KSEA", "KSFO"],
    "texas":         ["KSAT"],  # San Antonio — inland TX, distinct from Gulf coast
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


def get_research_stations():
    """
    Returns the list of approved research stations for B-MODE backtesting.
    These are the 20 ICAO codes from the STATIC_MAPPING as required by the B-MODE specs.
    """
    return list(STATIC_MAPPING.keys())