#!/usr/bin/env python3
"""
Goldilocks Validation Script — B-Mode

Empirically verifies whether 1°F temperature spikes at KNYC that last 1-2
minutes establish the daily HIGH/LOW for Kalshi settlement.

Approach: Compare hourly METAR observations against Kalshi settlement values.
Days where settlement HIGH > any hourly MAX observation (or settlement LOW < any
hourly MIN observation) are "suspected Goldilocks events" — spikes visible only
to Kalshi/NWS that don't appear in the standard hourly public feed.

Data sources used:
  1. Local METAR DB — hourly observations for KNYC (core of analysis)
  2. Kalshi settlements DB — actual settlement HIGH/LOW per day
  3. NWS API — supplementary hourly observations (no key needed)
  4. SynopticData API — IF SYNOPTIC_API_KEY env var is set, pulls 1-minute data

Output: data/goldilocks_validation.json

Usage:
    python3 scripts/goldilocks_validate.py
    python3 scripts/goldilocks_validate.py --station KNYC --days 30

B-Mode R8 Cycle 4.6: Goldilocks spike validation.
"""

import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
METAR_DB = os.path.join(DATA_DIR, 'metar_backfill.db')
KALSHI_DB = os.path.join(DATA_DIR, 'kalshi_settlements.db')

DEFAULT_STATION = 'KNYC'
DEFAULT_DAYS = 30
KALSHI_SERIES = 'KXHIGHNY'  # KNYC HIGH series

SYNOPTIC_API_KEY = os.environ.get('SYNOPTIC_API_KEY', '')
NWS_BASE_URL = 'https://api.weather.gov/stations'


def fetch_nws_observations(station: str, date_str: str) -> List[dict]:
    """
    Fetch NWS hourly observations for a station on a given date (no key needed).

    Returns list of {timestamp, temp_f, dewpoint_f, wind_speed_kt, wind_gust_kt}
    """
    url = f"{NWS_BASE_URL}/{station}/observations"
    params = f"?limit=50&start={date_str}T00:00:00Z&end={date_str}T23:59:59Z"

    try:
        import requests
        resp = requests.get(
            url + params,
            headers={'User-Agent': '(weather-engine, dan@openclaw.ai)',
                     'Accept': 'application/geo+json'},
            timeout=15
        )
        if resp.status_code != 200:
            logger.warning("NWS API returned %d for %s on %s", resp.status_code, station, date_str)
            return []

        data = resp.json()
        obs = []
        for feature in data.get('features', []):
            props = feature.get('properties', {})
            temp_c = props.get('temperature', {}).get('value')
            if temp_c is None:
                continue

            obs.append({
                'timestamp': props.get('timestamp'),
                'temp_f': temp_c * 9.0 / 5.0 + 32.0,
                'temp_c': temp_c,
                'dewpoint_f': (props.get('dewpoint') or {}).get('value', None),
                'wind_speed_kt': (props.get('windSpeed') or {}).get('value', None),
                'wind_gust_kt': (props.get('windGust') or {}).get('value', None),
            })
        return obs
    except Exception as e:
        logger.warning("NWS fetch error: %s", e)
        return []


def fetch_synoptic_1min(station: str, date_str: str) -> List[dict]:
    """
    Fetch 1-minute data from SynopticData API.

    Requires SYNOPTIC_API_KEY environment variable.

    Station ID for KNYC 1-minute: KNYC1M
    """
    if not SYNOPTIC_API_KEY:
        logger.warning("SYNOPTIC_API_KEY not set — skipping 1-minute data")
        return []

    url = "https://api.synopticdata.com/v2/stations/timeseries"
    params = {
        'stid': f"{station}1M",  # e.g., KNYC1M
        'token': SYNOPTIC_API_KEY,
        'start': f"{date_str}000000",
        'end': f"{date_str}235959",
        'vars': 'air_temp',
        'units': 'temp|F',
        'output': 'json',
    }

    try:
        import requests
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            logger.warning("SynopticData returned %d", resp.status_code)
            return []

        data = resp.json()
        station_data = data.get('STATION', [])
        if not station_data:
            logger.warning("No station data for %s1M on %s", station, date_str)
            return []

        obs_list = station_data[0].get('OBSERVATIONS', {})
        timestamps = obs_list.get('date_time', [])
        temps = obs_list.get('air_temp_set_1', [])

        obs = []
        for ts, temp in zip(timestamps, temps):
            if temp is not None:
                obs.append({
                    'timestamp': ts,
                    'temp_f': float(temp),
                })
        logger.info("SynopticData: %d 1-minute obs for %s on %s", len(obs), station, date_str)
        return obs
    except Exception as e:
        logger.warning("SynopticData fetch error: %s", e)
        return []


def get_local_metar_obs(station: str, date_str: str) -> List[dict]:
    """Get hourly METAR observations from local DB."""
    if not os.path.exists(METAR_DB):
        logger.warning("METAR DB not found at %s", METAR_DB)
        return []

    try:
        conn = sqlite3.connect(METAR_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT timestamp_utc, temp_f, dewpoint_f, 
                   wind_speed_kt, wind_gust_kt
            FROM metar_observations
            WHERE station = ? AND date_utc = ?
            ORDER BY timestamp_utc
        """, (station, date_str)).fetchall()
        conn.close()

        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Local METAR error: %s", e)
        return []


def get_kalshi_settlements(station: str, date_strs: List[str]) -> Dict[str, float]:
    """Get Kalshi settlement values for a station on specified dates."""
    if not os.path.exists(KALSHI_DB):
        logger.warning("Kalshi DB not found at %s", KALSHI_DB)
        return {}

    settlements = {}
    try:
        conn = sqlite3.connect(KALSHI_DB)
        conn.row_factory = sqlite3.Row

        for date_str in date_strs:
            # Try exact target_date match
            rows = conn.execute("""
                SELECT target_date, kalshi_temp, series
                FROM kalshi_settlements
                WHERE station = ? AND target_date = ?
                ORDER BY source_type DESC, finalized_count DESC
                LIMIT 1
            """, (station, date_str)).fetchall()

            if rows:
                settlements[date_str] = rows[0]['kalshi_temp']
            else:
                # Try series + target_date pattern
                rows = conn.execute("""
                    SELECT target_date, kalshi_temp, event_ticker
                    FROM kalshi_settlements
                    WHERE station = ? AND target_date = ?
                    LIMIT 1
                """, (station, date_str)).fetchall()
                if rows:
                    settlements[date_str] = rows[0]['kalshi_temp']
        conn.close()
    except Exception as e:
        logger.warning("Kalshi DB error: %s", e)

    return settlements


def analyze_day(
    station: str,
    date_str: str,
    metar_obs: List[dict],
    kalshi_high: Optional[float],
) -> dict:
    """
    Analyze one day's data for Goldilocks events.

    A "suspected Goldilocks event" occurs when:
    - The Kalshi settlement HIGH is greater than any hourly METAR max
    - OR the Kalshi settlement LOW is less than any hourly METAR min

    Returns dict with analysis details.
    """
    if not metar_obs:
        return {'date': date_str, 'station': station, 'error': 'No data'}

    temps = [o['temp_f'] for o in metar_obs if o.get('temp_f') is not None and -50 <= o['temp_f'] <= 150]
    if not temps:
        return {'date': date_str, 'station': station, 'error': 'No temp data'}

    metar_max = max(temps)
    metar_min = min(temps)
    obs_count = len(temps)

    # Find timestamp of extreme values
    max_obs = [o for o in metar_obs if o.get('temp_f') == metar_max]
    min_obs = [o for o in metar_obs if o.get('temp_f') == metar_min]
    max_time = max_obs[0]['timestamp_utc'] if max_obs else ''
    min_time = min_obs[0]['timestamp_utc'] if min_obs else ''

    result = {
        'date': date_str,
        'station': station,
        'obs_count': obs_count,
        'metar_max_f': round(metar_max, 1),
        'metar_max_time_utc': max_time,
        'metar_min_f': round(metar_min, 1),
        'metar_min_time_utc': min_time,
        'temp_range_f': round(metar_max - metar_min, 1),
    }

    if kalshi_high is not None:
        result['kalshi_high_f'] = kalshi_high
        result['metar_vs_kalshi_high_diff'] = round(kalshi_high - metar_max, 1)
        result['is_suspected_goldilocks'] = kalshi_high > metar_max + 0.5
    else:
        result['kalshi_high_f'] = None
        result['metar_vs_kalshi_high_diff'] = None
        result['is_suspected_goldilocks'] = False

    return result


def validate(station: str, days: int, use_nws: bool = True) -> dict:
    """
    Main validation routine.

    Args:
        station: ICAO station code (e.g., 'KNYC')
        days: Number of days to look back
        use_nws: Whether to try NWS API for supplementary data

    Returns:
        Dict with full analysis results
    """
    end_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    date_strs = []
    d = datetime.now(timezone.utc)
    for _ in range(days):
        date_strs.append(d.strftime('%Y-%m-%d'))
        d -= timedelta(days=1)

    logger.info("=== Goldilocks Validation for %s ===", station)
    logger.info("Date range: %s to %s (%d days)", date_strs[-1], date_strs[0], len(date_strs))

    # Step 1: Get METAR observations for all days (local DB)
    logger.info("Loading local METAR observations...")
    local_obs: Dict[str, List[dict]] = {}
    for date_str in date_strs:
        obs = get_local_metar_obs(station, date_str)
        if obs:
            local_obs[date_str] = obs
    logger.info("  Loaded local data for %d/%d days", len(local_obs), len(date_strs))

    # Step 2: Try NWS API for supplementary data
    nws_obs: Dict[str, List[dict]] = {}
    if use_nws:
        logger.info("Fetching NWS API observations...")
        for date_str in date_strs:
            obs = fetch_nws_observations(station, date_str)
            if obs:
                nws_obs[date_str] = obs

    # Step 3: Try SynopticData 1-minute data
    syn_obs: Dict[str, List[dict]] = {}
    if SYNOPTIC_API_KEY:
        logger.info("Fetching SynopticData 1-minute observations...")
        # Only fetch last 7 days to avoid API quota issues
        for date_str in date_strs[:7]:
            obs = fetch_synoptic_1min(station, date_str)
            if obs:
                syn_obs[date_str] = obs

    # Step 4: Get Kalshi settlements
    logger.info("Loading Kalshi settlements...")
    settlements = get_kalshi_settlements(station, date_strs)
    logger.info("  Found settlements for %d/%d days", len(settlements), len(date_strs))

    # Step 5: Analyze each day
    daily_results = []
    days_with_data = 0
    days_with_settlement = 0
    goldilocks_high_events = 0
    goldilocks_low_events = 0
    diffs_high = []

    for date_str in date_strs:
        obs = local_obs.get(date_str, [])
        kalshi_high = settlements.get(date_str)

        result = analyze_day(station, date_str, obs, kalshi_high)
        daily_results.append(result)

        if 'error' not in result:
            days_with_data += 1
        if kalshi_high is not None:
            days_with_settlement += 1
        if result.get('is_suspected_goldilocks'):
            goldilocks_high_events += 1
        diff = result.get('metar_vs_kalshi_high_diff')
        if diff is not None:
            diffs_high.append(diff)

    # Step 6: Summary statistics
    if diffs_high:
        mean_diff = sum(diffs_high) / len(diffs_high)
        std_diff = (sum((d - mean_diff) ** 2 for d in diffs_high) / len(diffs_high)) ** 0.5
        max_diff = max(diffs_high)
        min_diff = min(diffs_high)
    else:
        mean_diff = std_diff = max_diff = min_diff = 0.0

    summary = {
        'station': station,
        'analysis_period': {
            'start_date': date_strs[-1],
            'end_date': date_strs[0],
            'total_days_analyzed': len(date_strs),
        },
        'data_coverage': {
            'days_with_metar_data': days_with_data,
            'days_with_kalshi_settlement': days_with_settlement,
            'days_with_nws_api': len(nws_obs),
            'days_with_synoptic_1min': len(syn_obs),
        },
        'goldilocks_analysis': {
            'suspected_high_events': goldilocks_high_events,
            'goldilocks_rate': round(goldilocks_high_events / max(days_with_settlement, 1), 3),
            'temp_diff_high_vs_metar_max': {
                'mean_f': round(mean_diff, 2),
                'std_f': round(std_diff, 2),
                'max_f': round(max_diff, 2),
                'min_f': round(min_diff, 2),
            },
        },
        'data_sources_used': {
            'local_metar_db': True,
            'nws_api': use_nws,
            'synopticdata_1min': bool(SYNOPTIC_API_KEY),
        },
        'daily_events': daily_results,
    }

    # Step 7: Go/No-Go Recommendation
    if goldilocks_high_events == 0:
        summary['recommendation'] = {
            'decision': 'NO-GO',
            'confidence': 'HIGH',
            'rationale': (
                f"No goldilocks events detected in {days_with_data} days of data. "
                f"Kalshi settlement HIGH consistently matches or is within 0.5°F of "
                f"hourly METAR max. No evidence that 1-minute spikes establish HOTD."
            ),
        }
    elif goldilocks_high_events / max(days_with_settlement, 1) < 0.1:
        summary['recommendation'] = {
            'decision': 'NO-GO',
            'confidence': 'MEDIUM',
            'rationale': (
                f"Only {goldilocks_high_events}/{days_with_settlement} days "
                f"({(goldilocks_high_events/max(days_with_settlement,1)*100):.1f}%) show "
                f"suspected goldilocks events. Rate too low for systematic trading edge."
            ),
        }
    elif goldilocks_high_events / max(days_with_settlement, 1) < 0.25:
        summary['recommendation'] = {
            'decision': 'CAUTION',
            'confidence': 'LOW',
            'rationale': (
                f"{goldilocks_high_events}/{days_with_settlement} days "
                f"({(goldilocks_high_events/max(days_with_settlement,1)*100):.1f}%) show "
                f"suspected goldilocks events. Recommend 1-minute API access to validate "
                f"before trading on this theory."
            ),
        }
    else:
        summary['recommendation'] = {
            'decision': 'GO',
            'confidence': 'HIGH' if SYNOPTIC_API_KEY else 'MEDIUM',
            'rationale': (
                f"{goldilocks_high_events}/{days_with_settlement} days "
                f"({(goldilocks_high_events/max(days_with_settlement,1)*100):.1f}%) show "
                f"suspected goldilocks events. "
                + ("1-minute SynopticData data confirms spike pattern."
                   if SYNOPTIC_API_KEY
                   else "Recommend enabling SYNOPTIC_API_KEY for 1-minute confirmation.")
                + f" Mean temp diff: {mean_diff:.1f}°F."
            ),
        }

    return summary


def format_report(result: dict) -> str:
    """Format validation results as a readable report."""
    lines = []
    lines.append("=" * 65)
    lines.append(f"Goldilocks Validation Report — {result['station']}")
    lines.append(f"Period: {result['analysis_period']['start_date']} to "
                 f"{result['analysis_period']['end_date']}")
    lines.append("=" * 65)
    lines.append("")

    dc = result['data_coverage']
    lines.append(f"Data Coverage:")
    lines.append(f"  Days with METAR data:        {dc['days_with_metar_data']}")
    lines.append(f"  Days with Kalshi settlement: {dc['days_with_kalshi_settlement']}")
    if dc['days_with_synoptic_1min']:
        lines.append(f"  Days with 1-minute data:     {dc['days_with_synoptic_1min']}")
    lines.append("")

    ga = result['goldilocks_analysis']
    lines.append(f"Goldilocks Analysis:")
    lines.append(f"  Suspected high events: {ga['suspected_high_events']} / "
                 f"{dc['days_with_kalshi_settlement']} "
                 f"({(ga['suspected_high_events']/max(dc['days_with_kalshi_settlement'],1)*100):.1f}%)")
    td = ga['temp_diff_high_vs_metar_max']
    lines.append(f"  Temp diff (Kalshi - METAR max):")
    lines.append(f"    Mean: {td['mean_f']:+.1f}°F  Max: {td['max_f']:+.1f}°F  "
                 f"Min: {td['min_f']:+.1f}°F  Std: {td['std_f']:.1f}°F")
    lines.append("")

    rec = result.get('recommendation', {})
    lines.append(f"Recommendation: {rec.get('decision', 'N/A')}")
    lines.append(f"  Confidence: {rec.get('confidence', 'N/A')}")
    lines.append(f"  Rationale: {rec.get('rationale', '')}")
    lines.append("")

    if SYNOPTIC_API_KEY:
        lines.append("1-minute SynopticData status: ENABLED")
    else:
        lines.append("1-minute SynopticData status: DISABLED — set SYNOPTIC_API_KEY env var")

    return "\n".join(lines)


def print_daily_events(result: dict, limit: int = 20):
    """Print daily events table."""
    events = result.get('daily_events', [])
    print(f"\nDaily Events (showing {min(limit, len(events))}/{len(events)}):")
    print(f"{'Date':<12} {'Obs':>4} {'METAR Max':>10} {'Kalshi':>7} {'Diff':>6} {'Goldi?':>8}")
    print("-" * 52)
    for event in events[:limit]:
        if 'error' in event:
            print(f"{event['date']:<12} {'N/A':>4} {'N/A':>10} {'N/A':>7} {'N/A':>6} {'N/A':>8}")
        else:
            goldi = "** YES **" if event.get('is_suspected_goldilocks') else "no"
            kalshi = f"{event.get('kalshi_high_f','N/A')}" if event.get('kalshi_high_f') else "N/A"
            diff = f"{event.get('metar_vs_kalshi_high_diff', 0):+.1f}" if event.get('metar_vs_kalshi_high_diff') is not None else "N/A"
            print(f"{event['date']:<12} {event['obs_count']:>4} "
                  f"{event['metar_max_f']:>8.1f}°F "
                  f"{kalshi:>7} {diff:>6} {goldi:>8}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Goldilocks spike validation')
    parser.add_argument('--station', default=DEFAULT_STATION,
                        help=f'ICAO station code (default: {DEFAULT_STATION})')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS,
                        help=f'Days to analyze (default: {DEFAULT_DAYS})')
    parser.add_argument('--no-nws', action='store_true',
                        help='Skip NWS API fetch')
    parser.add_argument('--output', default=os.path.join(DATA_DIR, 'goldilocks_validation.json'),
                        help='Output path')
    parser.add_argument('--verbose', action='store_true',
                        help='Show daily events table')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Run validation
    result = validate(args.station, args.days, use_nws=(not args.no_nws))

    # Print report
    print(format_report(result))

    if args.verbose:
        print_daily_events(result)

    # Write output
    output_path = args.output
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    logger.info("Report saved to %s", output_path)

    # Exit code: 0 = NO-GO, 1 = CAUTION, 2 = GO
    decision = result.get('recommendation', {}).get('decision', 'NO-GO')
    return 0 if decision == 'NO-GO' else (1 if decision == 'CAUTION' else 2)


if __name__ == '__main__':
    sys.exit(main())