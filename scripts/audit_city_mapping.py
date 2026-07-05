#!/usr/bin/env python3
"""
Weather Engine — City/Station Mapping Audit

Run this script to inspect:
1. All cities/stations hardcoded across the codebase
2. Whether stations match the authoritative registry
3. Whether all functions agree on the station list
4. Discrepancies and a plan to fix them

Usage: python3 scripts/audit_city_mapping.py
"""

import os
import re
import sqlite3
import json
import sys
import importlib.util
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # weather-engine-source/
SRC = BASE  # The script lives in scripts/, parent is weather-engine-source/
DB_PATH = os.path.join(SRC, "data", "metar_backfill.db")
NWP_DB_PATH = os.path.join(SRC, "data", "nwp_forecasts.db")

# Import the station registry
sys.path.insert(0, SRC)
from core.station_registry import get_all_stations, get_station_mapping

# ─── 1. Scan all Python files for hardcoded station lists ───────────────────

def scan_hardcoded_stations():
    """Find all hardcoded station lists in Python files."""
    results = []

    patterns = [
        # ALL_STATIONS = [...]
        (r'(?:ALL_STATIONS|STATIONS|FDR_STATIONS|SAFE_CITIES|CITIES)\s*=\s*\[([^\]]+)\]', 'list'),
        # SAFE_CITIES = {...}
        (r'(?:SAFE_CITIES|ALL_KALSHI_STATIONS)\s*=\s*\{([^}]+)\}', 'set'),
        # KALSHI_CITIES = {...}
        (r'KALSHI_CITIES\s*=\s*\{([^}]+)\}', 'dict'),
        # Station tuples in CITIES list
        (r'\("K[A-Z]{3}",\s*"', 'city_tuple'),
    ]

    station_re = re.compile(r'K[A-Z]{3}')

    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != '__pycache__' and not d.startswith('core.backup')]
        for fname in files:
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, SRC)

            try:
                with open(fpath, 'r') as f:
                    content = f.read()
            except Exception:
                continue

            # Find station codes in the file
            stations_found = set(station_re.findall(content))

            # Check for hardcoded lists
            for pattern, ptype in patterns:
                for match in re.finditer(pattern, content):
                    block = match.group(0)
                    codes = sorted(set(station_re.findall(block)))
                    if codes:
                        # Find line number
                        line_num = content[:match.start()].count('\n') + 1
                        results.append({
                            'file': rel_path,
                            'line': line_num,
                            'type': ptype,
                            'stations': codes,
                            'count': len(codes),
                            'snippet': block[:100],
                        })

            # Also check for standalone station references (any K[A-Z]{3} pattern)
            if stations_found:
                results.append({
                    'file': rel_path,
                    'line': 0,
                    'type': 'all_references',
                    'stations': sorted(stations_found),
                    'count': len(stations_found),
                    'snippet': f'{len(stations_found)} unique station codes referenced',
                })

    return results


# ─── 2. Check Kalshi discovery (live API) ──────────────────────────────────

def check_kalshi_discovery():
    """Check if Kalshi market discovery works and what stations it returns."""
    discovery_result = {
        'available': False,
        'stations': [],
        'error': None,
        'source': 'core/kalshi_monitor.py',
    }

    try:
        import sys
        sys.path.insert(0, SRC)
        from core.kalshi_monitor import discover_market_derived_station_codes
        stations = discover_market_derived_station_codes(max_pages=3, page_limit=100)
        discovery_result['available'] = True
        discovery_result['stations'] = stations
        discovery_result['count'] = len(stations)
    except Exception as e:
        discovery_result['error'] = str(e)
        discovery_result['note'] = 'Kalshi discovery not available — likely no API credentials or network access'

    return discovery_result


# ─── 3. Check database station lists ────────────────────────────────────────

def check_metar_db_stations():
    """Get stations from the METAR backfill database."""
    result = {'available': False, 'stations': [], 'error': None}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT station FROM metar_observations ORDER BY station")
        stations = [r[0] for r in c.fetchall()]
        result['available'] = True
        result['stations'] = stations
        result['count'] = len(stations)
        conn.close()
    except Exception as e:
        result['error'] = str(e)
    return result


def check_nwp_db_stations():
    """Get stations from the NWP forecasts database."""
    result = {'available': False, 'stations': [], 'error': None}
    try:
        conn = sqlite3.connect(NWP_DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT station FROM nwp_forecasts ORDER BY station")
        stations = [r[0] for r in c.fetchall()]
        result['available'] = True
        result['stations'] = stations
        result['count'] = len(stations)
        conn.close()
    except Exception as e:
        result['error'] = str(e)
    return result


def check_settlement_epochs():
    """Get stations from settlement_epochs table."""
    result = {'available': False, 'stations': [], 'error': None}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        stations = [r[0] for r in c.fetchall()]
        result['available'] = True
        result['stations'] = stations
        result['count'] = len(stations)
        conn.close()
    except Exception as e:
        result['error'] = str(e)
    return result


# ─── 4. Build the mapping ───────────────────────────────────────────────────

def build_consensus_mapping(hardcoded_results, kalshi_discovery, metar_db, nwp_db, settlement):
    """Build a consensus mapping and identify discrepancies."""
    all_sources = {}

    # Collect from hardcoded lists
    for r in hardcoded_results:
        if r['type'] in ('list', 'set', 'dict'):
            source = f"{r['file']}:{r['line']}"
            all_sources[source] = set(r['stations'])

    # Add databases
    if metar_db['available']:
        all_sources['METAR DB (metar_observations)'] = set(metar_db['stations'])
    if metar_db['available']:
        all_sources['METAR DB (settlement_epochs)'] = set(settlement['stations']) if settlement['available'] else set()
    if nwp_db['available']:
        all_sources['NWP DB (nwp_forecasts)'] = set(nwp_db['stations'])
    if kalshi_discovery['available']:
        all_sources['Kalshi API (live discovery)'] = set(kalshi_discovery['stations'])

    # Union of all stations
    all_stations = set()
    for stations in all_sources.values():
        all_stations.update(stations)

    # Find which sources agree
    discrepancies = []
    for source, stations in all_sources.items():
        missing_from_source = all_stations - stations
        extra_in_source = stations - (all_stations - missing_from_source)
        if missing_from_source:
            discrepancies.append({
                'source': source,
                'missing': sorted(missing_from_source),
                'has': sorted(stations),
                'count': len(stations),
            })

    return {
        'all_stations': sorted(all_stations),
        'total_unique': len(all_stations),
        'sources': {k: sorted(v) for k, v in all_sources.items()},
        'discrepancies': discrepancies,
    }


# ─── 5. Generate rectification plan ────────────────────────────────────────

def generate_plan(mapping):
    """Generate a plan to rectify discrepancies."""
    plan = []

    # Check if Kalshi discovery is the source of truth
    kalshi_stations = mapping['sources'].get('Kalshi API (live discovery)', [])
    if not kalshi_stations:
        plan.append({
            'priority': 'CRITICAL',
            'action': 'Fix Kalshi API discovery',
            'detail': 'Live Kalshi discovery is not working. This should be the single source of truth for the station list. Fix API credentials or network access.',
        })
    else:
        plan.append({
            'priority': 'DONE',
            'action': 'Kalshi discovery working',
            'detail': f'Kalshi API returns {len(kalshi_stations)} stations: {", ".join(kalshi_stations)}',
        })

    # Check hardcoded lists
    for source, stations in mapping['sources'].items():
        if source.startswith('Kalshi API'):
            continue
        if kalshi_stations:
            s_set = set(stations)
            k_set = set(kalshi_stations)
            if s_set != k_set:
                missing = k_set - s_set
                extra = s_set - k_set
                if missing or extra:
                    plan.append({
                        'priority': 'HIGH',
                        'action': f'Fix {source}',
                        'detail': f'Missing: {sorted(missing) if missing else "none"}. Extra: {sorted(extra) if extra else "none"}. Should match Kalshi discovery exactly.',
                    })

    # Check NWP collection
    nwp_stations = mapping['sources'].get('NWP DB (nwp_forecasts)', [])
    if nwp_stations and kalshi_stations:
        if set(nwp_stations) != set(kalshi_stations):
            plan.append({
                'priority': 'HIGH',
                'action': 'Fix NWP collection station list',
                'detail': f'NWP collects for {sorted(set(nwp_stations) - set(kalshi_stations))} not in Kalshi, missing {sorted(set(kalshi_stations) - set(nwp_stations))} from Kalshi.',
            })

    # Recommend architecture
    plan.append({
        'priority': 'ARCHITECTURE',
        'action': 'Replace all hardcoded station lists with Kalshi discovery',
        'detail': 'All scripts should import get_discovered_weather_market_station_mapping() from core/kalshi_monitor.py instead of hardcoding ALL_STATIONS. Add a fallback cache file for when API is unavailable.',
    })

    return plan


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("WEATHER ENGINE — CITY/STATION MAPPING AUDIT")
    print(f"Run: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # 1. Scan hardcoded stations
    print("\n1. SCANNING HARDCODED STATION LISTS...")
    hardcoded = scan_hardcoded_stations()
    hardcoded_lists = [r for r in hardcoded if r['type'] in ('list', 'set', 'dict')]
    print(f"   Found {len(hardcoded_lists)} hardcoded station lists:")
    for r in hardcoded_lists:
        print(f"   - {r['file']}:{r['line']} ({r['type']}, {r['count']} stations): {r['stations']}")

    # 2. Check Kalshi discovery
    print("\n2. CHECKING KALSHI LIVE DISCOVERY...")
    kalshi = check_kalshi_discovery()
    if kalshi['available']:
        print(f"   ✅ Kalshi discovery working: {kalshi['count']} stations")
        print(f"   Stations: {kalshi['stations']}")
    else:
        print(f"   ❌ Kalshi discovery not available: {kalshi.get('error', 'unknown')}")

    # 3. Check databases
    print("\n3. CHECKING DATABASE STATION LISTS...")
    metar = check_metar_db_stations()
    print(f"   METAR DB: {metar['count'] if metar['available'] else 'N/A'} stations")
    if metar['available']:
        print(f"   Stations: {metar['stations']}")

    settlement = check_settlement_epochs()
    print(f"   Settlement epochs: {settlement['count'] if settlement['available'] else 'N/A'} stations")
    if settlement['available']:
        print(f"   Stations: {settlement['stations']}")

    nwp = check_nwp_db_stations()
    print(f"   NWP DB: {nwp['count'] if nwp['available'] else 'N/A'} stations")
    if nwp['available']:
        print(f"   Stations: {nwp['stations']}")

    # 4. Build mapping
    print("\n4. CONSENSUS MAPPING...")
    mapping = build_consensus_mapping(hardcoded, kalshi, metar, nwp, settlement)
    print(f"   Total unique stations across all sources: {mapping['total_unique']}")
    print(f"   Sources:")
    for source, stations in mapping['sources'].items():
        print(f"   - {source}: {len(stations)} stations")

    # 5. Discrepancies
    print("\n5. DISCREPANCIES...")
    if mapping['discrepancies']:
        for d in mapping['discrepancies']:
            print(f"   ⚠️  {d['source']}: has {d['count']}, missing {d['missing']}")
    else:
        print("   ✅ All sources agree")

    # 6. Check against authoritative registry
    print("\n6. STATION REGISTRY CHECK...")
    registry_stations = get_all_stations()
    registry_mapping = get_station_mapping()
    print(f"   Registry authoritative list: {len(registry_stations)} stations")
    print(f"   Stations: {registry_stations}")
    print(f"   Source breakdown:")
    for s in registry_stations:
        info = registry_mapping.get(s, {})
        print(f"     {s} -> {info.get('city_name', '?')} (token: {info.get('kalshi_token', '?')}, verified: {info.get('verified', False)})")

    # Compare hardcoded lists against registry
    print("\n   Hardcoded lists vs registry:")
    for r in hardcoded_lists:
        hardcoded_set = set(r['stations'])
        registry_set = set(registry_stations)
        missing = registry_set - hardcoded_set
        extra = hardcoded_set - registry_set
        status = "✅ MATCH" if not missing and not extra else "⚠️  MISMATCH"
        detail = ""
        if missing:
            detail += f" missing {len(missing)} from registry"
        if extra:
            detail += f" has {len(extra)} not in registry"
        print(f"   {status} {r['file']}:{r['line']} ({r['count']} stations){detail}")

    # 7. Plan
    print("\n7. RECTIFICATION PLAN...")
    plan = generate_plan(mapping)
    plan.append({
        'priority': 'ARCHITECTURE',
        'action': 'Replace all hardcoded station lists with station_registry',
        'detail': 'All scripts should import get_all_stations() from core/station_registry.py instead of hardcoding station lists. The registry handles Kalshi discovery, cache file, and DB fallback automatically.',
    })
    for item in plan:
        print(f"   [{item['priority']}] {item['action']}")
        print(f"      {item['detail']}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Registry authoritative list: {len(registry_stations)} stations")
    print(f"  Hardcoded lists found: {len(hardcoded_lists)}")
    print(f"  Kalshi discovery: {'WORKING' if kalshi['available'] else 'NOT WORKING'}")
    print(f"  METAR DB stations: {metar.get('count', 'N/A')}")
    print(f"  NWP DB stations: {nwp.get('count', 'N/A')}")
    print(f"  Total unique stations: {mapping['total_unique']}")
    print(f"  Discrepancies: {len(mapping['discrepancies'])}")
    print(f"  Plan items: {len(plan)}")


if __name__ == "__main__":
    main()
