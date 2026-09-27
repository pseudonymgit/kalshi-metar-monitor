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
from core.db_utils import query_db
from functools import lru_cache
from typing import Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_BASE, "data", "metar_backfill.db")
_CACHE_PATH = os.path.join(_BASE, "data", "station_mapping.json")

# ─── CANONICAL 20-STATION SET (single source of truth) ──────────────────
# Reconciled 2026-09-26 against BOTH Kalshi live series endpoints
# (data/kalshi_official_mapping.json, 20 series) AND the METAR daily_stats /
# NWP / settlement_epochs ground truth. These are exactly the 20 Kalshi
# weather-market cities. All PROCESSING must filter to this set.
#
# KDAL is removed (Kalshi settles Dallas on KDFW / series KXHIGHTDAL).
# TEST is synthetic and is never a station.
#
# The canonical list is pinned in data/station_mapping.json ("station_count": 20
# + "stations"). The fallback below mirrors it verbatim and is used only if that
# file is unreadable.
_FALLBACK_CANONICAL_STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO',
]


def _load_canonical_stations():
    """Load the canonical 20 from data/station_mapping.json, else fallback."""
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        st = data.get('stations') if isinstance(data, dict) else None
        if isinstance(st, dict) and st:
            return sorted(st.keys())
    except Exception:
        pass
    return list(_FALLBACK_CANONICAL_STATIONS)


CANONICAL_STATIONS = _load_canonical_stations()

# Backward-compat alias (old name; its header previously mislabeled it "15").
_RESEARCH_STATION_CODES = CANONICAL_STATIONS


# Station metadata (city/state/tz) for the canonical 20, verified against
# data/station_mapping.json. Used only as a fallback if the cache is missing.
STATIC_MAPPING = {
    'KATL': {'city': 'Atlanta', 'state': 'GA', 'tz': 'America/New_York'},
    'KAUS': {'city': 'Austin', 'state': 'TX', 'tz': 'America/Chicago'},
    'KBOS': {'city': 'Boston', 'state': 'MA', 'tz': 'America/New_York'},
    'KDCA': {'city': 'Washington DC', 'state': 'DC', 'tz': 'America/New_York'},
    'KDEN': {'city': 'Denver', 'state': 'CO', 'tz': 'America/Denver'},
    'KDFW': {'city': 'Dallas', 'state': 'TX', 'tz': 'America/Chicago'},
    'KHOU': {'city': 'Houston', 'state': 'TX', 'tz': 'America/Chicago'},
    'KLAS': {'city': 'Las Vegas', 'state': 'NV', 'tz': 'America/Los_Angeles'},
    'KLAX': {'city': 'Los Angeles', 'state': 'CA', 'tz': 'America/Los_Angeles'},
    'KMDW': {'city': 'Chicago', 'state': 'IL', 'tz': 'America/Chicago'},
    'KMIA': {'city': 'Miami', 'state': 'FL', 'tz': 'America/New_York'},
    'KMSP': {'city': 'Minneapolis', 'state': 'MN', 'tz': 'America/Chicago'},
    'KMSY': {'city': 'New Orleans', 'state': 'LA', 'tz': 'America/Chicago'},
    'KNYC': {'city': 'New York', 'state': 'NY', 'tz': 'America/New_York'},
    'KOKC': {'city': 'Oklahoma City', 'state': 'OK', 'tz': 'America/Chicago'},
    'KPHL': {'city': 'Philadelphia', 'state': 'PA', 'tz': 'America/New_York'},
    'KPHX': {'city': 'Phoenix', 'state': 'AZ', 'tz': 'America/Phoenix'},
    'KSAT': {'city': 'San Antonio', 'state': 'TX', 'tz': 'America/Chicago'},
    'KSEA': {'city': 'Seattle', 'state': 'WA', 'tz': 'America/Los_Angeles'},
    'KSFO': {'city': 'San Francisco', 'state': 'CA', 'tz': 'America/Los_Angeles'},
}


# Stations that must NEVER appear in processing output.
#   KDAL — duplicate of KDFW (Dallas settles on KDFW)
#   TEST — synthetic, never a real station
_FORBIDDEN_STATIONS = {"KDAL", "TEST"}
# Backward-compat alias.
_REMOVED_STATIONS = _FORBIDDEN_STATIONS


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
        rows = query_db(_DB_PATH, "SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        stations = [r['station'] for r in rows]
        if stations:
            return stations
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def get_all_stations():
    """
    Returns the CANONICAL 20 station codes — the single processing universe.

    This is authoritative and deliberately does NOT read settlement_epochs:
    that table historically carried off-list rows (KDAL, TEST, and 9
    non-Kalshi extras), which is exactly the drift this registry now prevents.
    Forbidden stations (_FORBIDDEN_STATIONS) are always excluded.

    Collection scripts may still gather extra stations for future expansion;
    they must filter through this function (or is_canonical_station) before
    emitting any processing output.
    """
    return sorted(set(CANONICAL_STATIONS) - _FORBIDDEN_STATIONS)


def is_canonical_station(station) -> bool:
    """True only for the 20 canonical stations (KDAL/TEST excluded)."""
    return str(station).upper() in set(CANONICAL_STATIONS) - _FORBIDDEN_STATIONS


def assert_canonical_stations(stations, *, stage: str = "processing") -> list:
    """Fail loudly if any station is non-canonical.

    Call this at the end of every processing stage before emitting rows. It is
    the guard that turns silent 30-station drift into a hard, named failure.
    Returns the input list unchanged when clean.
    """
    bad = sorted({s for s in stations if not is_canonical_station(s)})
    if bad:
        raise ValueError(
            f"NON-CANONICAL STATION(S) AT '{stage}': {bad}. "
            f"Processing must emit only the canonical {len(CANONICAL_STATIONS)} "
            f"stations (KDAL/TEST forbidden)."
        )
    return list(stations)


def get_station_mapping():
    """
    Returns dict of station -> {city_name, state, lat, lon} for the canonical 20.

    Rows for forbidden stations (_FORBIDDEN_STATIONS: KDAL, TEST) are dropped.
    """
    result = {}

    # Try cache file first for full mapping (includes lat/lon)
    try:
        with open(_CACHE_PATH, 'r') as f:
            data = json.load(f)
            if isinstance(data, dict):
                src = data.get('stations', data)
                if isinstance(src, dict):
                    for code, info in src.items():
                        if is_canonical_station(code):
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

# Budget caps (bankroll-relative percentages)
CLUSTER_BUDGET_PCT = 0.18  # 18% max total exposure per cluster
CITY_PAIR_CAP_PCT = 0.08   # 8% max net exposure per city (HIGH+LOW pair)

# Backward compatibility: convert USD caps to percentages based on $10k bankroll
CLUSTER_BUDGET_USD = 1800.0  # 18% of $10k
CITY_PAIR_CAP_USD = 800.0    # 8% of $10k


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


# ════════════════════════════════════════════════════════════════════════════
# A5: Liquidity Tier Classification for Microstructure Edge Penalty
# ════════════════════════════════════════════════════════════════════════════
#
# Liquidity tiers for microstructure edge penalty (from Gray Room E6):
#   Tier 1 (liquid):     0% edge discount
#   Tier 2 (moderate):  25% edge discount
#   Tier 3 (thin):      50% edge discount
#
# The authoritative tier mapping is in scripts/sweep/tiers.py.
# This provides a registry-level reference for station classification.
#
# Config-driven override: set CONFIG_MICROSTRUCTURE_OVERRIDE to override
# tier discounts for specific stations.

STATION_LIQUIDITY_TIERS = {
    # Tier 1 — Liquid (8 cities, 0% discount)
    "KNYC": 1,  # New York City
    "KLAX": 1,  # Los Angeles
    "KMDW": 1,  # Chicago
    "KDCA": 1,  # Washington DC
    "KATL": 1,  # Atlanta
    "KDFW": 1,  # Dallas-Fort Worth
    "KPHL": 1,  # Philadelphia
    "KBOS": 1,  # Boston
    # Tier 2 — Moderate (6 cities, 25% discount)
    "KDEN": 2,  # Denver
    "KLAS": 2,  # Las Vegas
    "KSFO": 2,  # San Francisco
    "KSEA": 2,  # Seattle
    "KMIA": 2,  # Miami
    "KHOU": 2,  # Houston
    # Tier 3 — Thin (6 cities, 50% discount)
    "KPHX": 3,  # Phoenix
    "KMSP": 3,  # Minneapolis
    "KMSY": 3,  # New Orleans
    "KAUS": 3,  # Austin
    "KSAT": 3,  # San Antonio
    "KOKC": 3,  # Oklahoma City
}

# Tier microstructure discounts (config-driven overridable)
TIER_MICROSTRUCTURE_DISCOUNTS = {
    1: 0.00,  # 0% discount
    2: 0.25,  # 25% discount
    3: 0.50,  # 50% discount
}

# Config-driven override: set this dict to override tier discounts
# e.g., CONFIG_MICROSTRUCTURE_OVERRIDE = {"KOKC": 0.30}
CONFIG_MICROSTRUCTURE_OVERRIDE: dict = {}


def get_liquidity_tier(station: str) -> int:
    """Get the liquidity tier (1, 2, or 3) for a station.

    Tier 1 = liquid (0% edge discount)
    Tier 2 = moderate (25% edge discount)
    Tier 3 = thin (50% edge discount)

    Defaults to Tier 3 for unknown stations.
    """
    return STATION_LIQUIDITY_TIERS.get(station.upper(), 3)


def get_microstructure_discount(station: str) -> float:
    """Get the microstructure edge discount for a station's tier.

    Supports config-driven override via CONFIG_MICROSTRUCTURE_OVERRIDE.
    """
    if station in CONFIG_MICROSTRUCTURE_OVERRIDE:
        return CONFIG_MICROSTRUCTURE_OVERRIDE[station]
    tier = get_liquidity_tier(station)
    return TIER_MICROSTRUCTURE_DISCOUNTS.get(tier, 0.50)


def apply_microstructure_penalty(raw_edge: float, station: str) -> float:
    """Apply microstructure edge penalty by liquidity tier.

    edge_after = raw_edge * (1.0 - microstructure_discount)

    Args:
        raw_edge: Raw (gross or net) edge before penalty
        station: ICAO station code

    Returns:
        Edge after applying tier-based microstructure discount
    """
    discount = get_microstructure_discount(station)
    return raw_edge * (1.0 - discount)