#!/usr/bin/env python3
"""
Goldilocks Validation Script — B-Mode R8 (IEM-powered)

Validates whether sub-hour temperature spikes at KNYC that are invisible on the
public hourly METAR feed establish the daily HIGH used for Kalshi settlement.

Data pipeline:
  IEM ASOS  (KNYC)  — 1-minute sensor-level data that feeds the NWS CLI settlement
  METAR DB  (KNYC)  — public hourly observations
  Kalshi DB (KNYC)  — actual settlement HIGHs

Goldilocks event (genuine spike that matters):
  1. IEM ASOS max > METAR hourly max by ≥1°F   => spike was invisible on public feed
  2. |IEM ASOS max - Kalshi settlement| ≤ 1°F  => spike established the settlement

If condition 1 is true but 2 is false: the spike was invisible but didn't matter.
If condition 2 is true but 1 is false: Kalshi matches the hourly max anyway.

Usage:
    python3 scripts/goldilocks_validate.py
    python3 scripts/goldilocks_validate.py --days 30 --verbose

Output: data/goldilocks_validation.json
"""

import csv
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from urllib.parse import urlencode

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
METAR_DB = os.path.join(DATA_DIR, 'metar_backfill.db')
KALSHI_DB = os.path.join(DATA_DIR, 'kalshi_settlements.db')
IEM_ASOS_URL = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'

DEFAULT_STATION = 'KNYC'
DEFAULT_DAYS = 90

# KNYC (Central Park) is US/Eastern. Kalshi settlement dates are local trading dates.
# IEM timestamps are UTC. We convert UTC timestamps to EST/EDT local dates.
STATION_TIMEZONES = {
    'KNYC': 'US/Eastern',
    'KJFK': 'US/Eastern',
    'KLGA': 'US/Eastern',
}


def utc_to_local_date(ts_utc: str, station: str) -> str:
    """
    Convert a UTC timestamp to local date (the Kalshi trading date).

    For US/Eastern:
      - Standard Time (EST, UTC-5): 05:00 UTC = 00:00 EST
      - Daylight Time (EDT, UTC-4): 04:00 UTC = 00:00 EDT

    Local date shifts forward vs UTC for timestamps before 05:00 UTC (EST)
    or 04:00 UTC (EDT).
    """
    try:
        # Handle both "2026-07-25 00:51" and "2026-07-25T00:51:00+00:00"
        clean = ts_utc[:19].replace('T', ' ')
        dt_utc = datetime.strptime(clean, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return ts_utc[:10]  # fallback

    # Simple heuristic: US/Eastern local date
    # EST (Nov-Mar): UTC-5, so 0:00-4:59 UTC UTC belongs to previous local day
    # EDT (Mar-Nov): UTC-4, so 0:00-3:59 UTC belongs to previous local day
    month = dt_utc.month
    hour = dt_utc.hour

    if 4 <= month <= 10:  # EDT (simplified — doesn't account for exact transition days)
        offset = 4
    else:
        offset = 5

    if hour < offset:
        # This UTC timestamp falls on the PREVIOUS local date
        local_dt = dt_utc - timedelta(hours=offset)
    else:
        local_dt = dt_utc - timedelta(hours=offset)

    return local_dt.strftime('%Y-%m-%d')


def fetch_iem_asos(station: str, start_date: str, end_date: str) -> List[dict]:
    """
    Fetch 1-minute ASOS observations from IEM (free, no API key).

    Returns all observations (1-min resolution where available) with temp_f.
    The IEM ASOS data is the same sensor-level data that feeds the NWS CLI
    settlement product.

    Args:
        station: ICAO station code (KNYC, KJFK, KLGA, etc.)
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
    """
    params = {
        'station': station,
        'data': 'tmpf',  # temperature in °F (direct from ASOS sensor)
        'year1': int(start_date[:4]),
        'month1': int(start_date[5:7]),
        'day1': int(start_date[8:10]),
        'year2': int(end_date[:4]),
        'month2': int(end_date[5:7]),
        'day2': int(end_date[8:10]),
        'tz': 'Etc/UTC',
        'format': 'onlycomma',
        'latlon': 'no',
        'elev': 'no',
        # missing=M keeps placeholder rows for 5-min slots with no temp
        # trace=T keeps 0.0001°F as trace precipitation (irrelevant for temp)
        'missing': 'M',
        'trace': 'T',
    }
    url = f"{IEM_ASOS_URL}?{urlencode(params)}"

    try:
        import requests
        resp = requests.get(url, timeout=60)
        if resp.status_code != 200:
            logger.warning("IEM returned %d for %s", resp.status_code, station)
            return []

        reader = csv.DictReader(io.StringIO(resp.text))
        obs = []
        for row in reader:
            temp_str = row.get('tmpf', '').strip()
            valid = row.get('valid', '').strip()
            if temp_str and temp_str not in ('M', '') and valid:
                try:
                    temp_f = float(temp_str)
                    if -50 <= temp_f <= 150:
                        local_date = utc_to_local_date(valid, station)
                        obs.append({
                            'timestamp_utc': valid,
                            'temp_f': temp_f,
                            'local_date': local_date,
                        })
                except (ValueError, TypeError):
                    pass

        logger.info("IEM %s %s to %s: %d total obs",
                    station, start_date, end_date, len(obs))
        return obs
    except Exception as e:
        logger.warning("IEM fetch error for %s: %s", station, e)
        return []


def fetch_metar_hourly(station: str) -> Dict[str, List[dict]]:
    """
    Fetch METAR hourly observations from local DB.

    Returns dict of {local_date: [obs]} grouped by local date.

    The METAR DB stores UTC dates. We convert to local trading dates
    for proper comparison with Kalshi settlements.
    """
    if not os.path.exists(METAR_DB):
        logger.warning("METAR DB not found at %s", METAR_DB)
        return {}

    obs_by_local_date: Dict[str, List[dict]] = {}
    try:
        conn = sqlite3.connect(METAR_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT date_utc, timestamp_utc, temp_f
            FROM metar_observations
            WHERE station = ? AND temp_f IS NOT NULL
              AND temp_f >= -50 AND temp_f <= 150
            ORDER BY timestamp_utc
        """, (station,)).fetchall()
        conn.close()

        for row in rows:
            ts_utc = row['timestamp_utc']
            local_date = utc_to_local_date(ts_utc, station)
            if local_date not in obs_by_local_date:
                obs_by_local_date[local_date] = []
            obs_by_local_date[local_date].append({
                'timestamp_utc': ts_utc,
                'temp_f': row['temp_f'],
            })

        logger.info("METAR hourly: %d total obs across %d local dates",
                    sum(len(v) for v in obs_by_local_date.values()),
                    len(obs_by_local_date))
        return obs_by_local_date
    except Exception as e:
        logger.warning("METAR DB error: %s", e)
        return {}


def get_kalshi_settlements(station: str) -> Dict[str, float]:
    """
    Get Kalshi settlement values for a station, keyed by local date.

    Kalshi settlement dates (target_date) are already in local trading date.
    """
    if not os.path.exists(KALSHI_DB):
        logger.warning("Kalshi DB not found at %s", KALSHI_DB)
        return {}

    settlements = {}
    try:
        conn = sqlite3.connect(KALSHI_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT target_date, kalshi_temp
            FROM kalshi_settlements
            WHERE station = ?
            ORDER BY target_date
        """, (station,)).fetchall()
        conn.close()

        for row in rows:
            td = row['target_date']
            # Prefer finalized over historical_api when both exist
            if td not in settlements:
                settlements[td] = row['kalshi_temp']

        logger.info("Kalshi settlements: %d dates for %s", len(settlements), station)
        return settlements
    except Exception as e:
        logger.warning("Kalshi DB error: %s", e)
        return {}


def group_by_local_date(obs: List[dict]) -> Dict[str, List[dict]]:
    """Group IEM observations by local_date."""
    groups: Dict[str, List[dict]] = {}
    for o in obs:
        ld = o.get('local_date', o['timestamp_utc'][:10])
        if ld not in groups:
            groups[ld] = []
        groups[ld].append(o)
    return groups


def validate(station: str, days: int) -> dict:
    """
    Main validation pipeline.

    1. Fetch IEM ASOS data (the 1-minute sensor-level feed that drives NWS CLI)
    2. Fetch METAR hourly data (the public feed)
    3. Fetch Kalshi settlements
    4. Compare: IEM max vs METAR max vs Kalshi HIGH for each local trading date
    5. Count Goldilocks events where IEM > METAR AND IEM ≈ Kalshi
    """
    end_date = datetime.now(timezone.utc)
    start_dt = end_date - timedelta(days=days)
    start_str = start_dt.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    logger.info("=" * 60)
    logger.info("Goldilocks Validation — B-Mode R8 (IEM-powered)")
    logger.info("Station: %s  Range: %s to %s (%d days)",
                station, start_str, end_str, days)
    logger.info("=" * 60)

    # === Step 1: Fetch IEM ASOS data (1-minute sensor feed) ===
    logger.info("Fetching IEM ASOS data for %s...", station)
    iem_obs = fetch_iem_asos(station, start_str, end_str)
    iem_by_date = group_by_local_date(iem_obs)
    logger.info("  IEM ASOS: %d obs across %d local dates", len(iem_obs), len(iem_by_date))

    # === Step 2: Fetch METAR hourly data ===
    logger.info("Fetching METAR hourly data for %s...", station)
    metar_by_date = fetch_metar_hourly(station)

    # === Step 3: Fetch Kalshi settlements ===
    logger.info("Loading Kalshi settlements...")
    kalshi = get_kalshi_settlements(station)

    # === Step 4: Analyze each date ===
    all_trading_dates = sorted(set(
        list(iem_by_date.keys()) + list(metar_by_date.keys()) + list(kalshi.keys())
    ))

    # Filter to analysis period
    analysis_dates = [d for d in all_trading_dates if start_str <= d <= end_str]
    logger.info("Analyzing %d trading dates...", len(analysis_dates))

    daily_results = []
    days_with_iem = 0
    days_with_metar = 0
    days_with_kalshi = 0
    days_with_all_three = 0

    goldilocks_events = 0           # IEM > METAR AND IEM ≈ Kalshi
    spike_invisible_events = 0       # IEM > METAR (condition 1 met)
    kalshi_rounded_matches = 0       # IEM ≈ Kalshi (but not spike)
    kalshi_exact_matches = 0         # All three match within 0.5°F

    iem_vs_metar_diffs = []
    iem_vs_kalshi_diffs = []

    for date_str in analysis_dates:
        iem_obs_day = iem_by_date.get(date_str, [])
        metar_obs_day = metar_by_date.get(date_str, [])
        kalshi_high = kalshi.get(date_str)

        # Compute max temps
        iem_temps = [o['temp_f'] for o in iem_obs_day]
        metar_temps = [o['temp_f'] for o in metar_obs_day]

        iem_max = max(iem_temps) if iem_temps else None
        metar_max = max(metar_temps) if metar_temps else None

        # Find spike max observation
        if iem_max is not None:
            iem_max_obs = [o for o in iem_obs_day if o['temp_f'] == iem_max]
        else:
            iem_max_obs = []

        if metar_max is not None:
            metar_max_obs = [o for o in metar_obs_day if o['temp_f'] == metar_max]
        else:
            metar_max_obs = []

        # Determine spike timestamp (IEM max timestamp)
        iem_max_ts = iem_max_obs[0]['timestamp_utc'] if iem_max_obs else None
        metar_max_ts = metar_max_obs[0]['timestamp_utc'] if metar_max_obs else None

        # Build result entry
        entry = {
            'date': date_str,
            'iem_max_f': round(iem_max, 1) if iem_max is not None else None,
            'iem_obs_count': len(iem_temps),
            'iem_max_time_utc': iem_max_ts,
            'metar_max_f': round(metar_max, 1) if metar_max is not None else None,
            'metar_obs_count': len(metar_temps),
            'metar_max_time_utc': metar_max_ts,
            'kalshi_high_f': kalshi_high,
        }

        # Differences
        iem_vs_metar = None
        if iem_max is not None and metar_max is not None:
            iem_vs_metar = round(iem_max - metar_max, 1)
            iem_vs_metar_diffs.append(iem_vs_metar)

        iem_vs_kalshi = None
        if iem_max is not None and kalshi_high is not None:
            iem_vs_kalshi = round(kalshi_high - iem_max, 1)
            iem_vs_kalshi_diffs.append(iem_vs_kalshi)

        entry['iem_vs_metar_diff'] = iem_vs_metar
        entry['iem_vs_kalshi_diff'] = iem_vs_kalshi

        # Goldilocks classification
        spike_invisible = (iem_vs_metar is not None and iem_vs_metar >= 1.0)
        kalshi_matches_iem = (iem_vs_kalshi is not None and abs(iem_vs_kalshi) <= 1.0)
        is_goldilocks = spike_invisible and kalshi_matches_iem

        entry['spike_invisible_on_metar'] = spike_invisible
        entry['kalshi_matches_iem'] = kalshi_matches_iem
        entry['is_goldilocks'] = is_goldilocks

        # Exact match: all three within 0.5°F
        if iem_max is not None and metar_max is not None and kalshi_high is not None:
            all_agree = (abs(iem_max - metar_max) <= 0.5 and
                         abs(iem_max - kalshi_high) <= 0.5)
            entry['all_three_agree'] = all_agree
        else:
            entry['all_three_agree'] = None

        daily_results.append(entry)

        # Tally
        if iem_temps:
            days_with_iem += 1
        if metar_temps:
            days_with_metar += 1
        if kalshi_high is not None:
            days_with_kalshi += 1
        if iem_temps and metar_temps and kalshi_high is not None:
            days_with_all_three += 1

        if is_goldilocks:
            goldilocks_events += 1
        if spike_invisible:
            spike_invisible_events += 1
        if kalshi_matches_iem:
            kalshi_rounded_matches += 1
        if entry.get('all_three_agree'):
            kalshi_exact_matches += 1

    # === Step 5: Statistics ===
    def stats_list(vals):
        if not vals:
            return {'mean': 0, 'std': 0, 'max': 0, 'min': 0, 'count': 0}
        n = len(vals)
        mean = sum(vals) / n
        std = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
        return {
            'mean': round(mean, 2),
            'std': round(std, 2),
            'max': round(max(vals), 1),
            'min': round(min(vals), 1),
            'count': n,
        }

    goldilocks_rate = goldilocks_events / max(days_with_all_three, 1)

    summary = {
        'station': station,
        'data_source': 'IEM ASOS (1-min sensor feed) + METAR hourly (local DB) + Kalshi settlements',
        'analysis_period': {
            'start_date': start_str,
            'end_date': end_str,
            'trading_days_in_period': len(analysis_dates),
        },
        'data_coverage': {
            'days_with_iem_asos': days_with_iem,
            'days_with_metar_hourly': days_with_metar,
            'days_with_kalshi': days_with_kalshi,
            'days_with_all_three_sources': days_with_all_three,
            'iem_total_observations': len(iem_obs),
            'iem_avg_obs_per_day': round(len(iem_obs) / max(days_with_iem, 1), 1),
        },
        'goldilocks_analysis': {
            # Core metric: spike invisible on METAR AND matches Kalshi
            'goldilocks_events': goldilocks_events,
            'goldilocks_rate': round(goldilocks_rate, 4),
            # Decomposed conditions
            'spike_invisible_on_metar_events': spike_invisible_events,
            'spike_invisible_rate': round(spike_invisible_events / max(days_with_all_three, 1), 4),
            'kalshi_matches_iem_events': kalshi_rounded_matches,
            'kalshi_match_rate': round(kalshi_rounded_matches / max(days_with_kalshi, 1), 4),
            'all_three_agree_events': kalshi_exact_matches,
            'all_three_agree_rate': round(kalshi_exact_matches / max(days_with_all_three, 1), 4),
            # Temperature differences
            'iem_vs_metar_max_diff': stats_list(iem_vs_metar_diffs),
            'iem_vs_kalshi_diff': stats_list(iem_vs_kalshi_diffs),
        },
        'daily_events': daily_results,
    }

    # === Step 6: Recommendation ===
    if days_with_all_three < 5:
        summary['recommendation'] = {
            'decision': 'INCONCLUSIVE',
            'confidence': 'LOW',
            'rationale': (
                f'Insufficient overlapping data ({days_with_all_three} days with all three sources). '
                f'Need more data to validate.'),
        }
    elif goldilocks_rate >= 0.10:
        summary['recommendation'] = {
            'decision': 'GO',
            'confidence': 'HIGH' if goldilocks_rate >= 0.20 else 'MEDIUM',
            'rationale': (
                f'{goldilocks_events}/{days_with_all_three} days ({goldilocks_rate*100:.0f}%) '
                f'show genuine Goldilocks spikes: IEM ASOS max > METAR hourly max by ≥1°F '
                f'AND the spike matches the Kalshi settlement within 1°F. '
                f'This means the ASOS 1-minute sensor data captures sub-hour temperature '
                f'extremes that both (a) are invisible on the public hourly METAR feed, '
                f'and (b) establish the Kalshi settlement HIGH. '
                f'Mean IEM-minus-METAR diff: '
                f'{stats_list(iem_vs_metar_diffs)["mean"]:+.1f}°F. '
                f'Edge exists at {goldilocks_rate*100:.0f}% rate — actionable.'
            ),
        }
    elif goldilocks_rate >= 0.05:
        summary['recommendation'] = {
            'decision': 'CAUTION',
            'confidence': 'MEDIUM',
            'rationale': (
                f'{goldilocks_events}/{days_with_all_three} days ({goldilocks_rate*100:.1f}%) '
                f'show genuine Goldilocks events. Signal present but modest. '
                f'Recommend extending to 1+ year of data for confidence.'
            ),
        }
    else:
        summary['recommendation'] = {
            'decision': 'NO-GO',
            'confidence': 'HIGH',
            'rationale': (
                f'Only {goldilocks_events}/{days_with_all_three} days '
                f'({goldilocks_rate*100:.1f}%) show genuine Goldilocks events. '
                f'No systematic evidence that 1-minute spikes invisible on hourly METAR '
                f'influence Kalshi settlements at KNYC.'
            ),
        }

    return summary


def format_report(result: dict) -> str:
    """Pretty-print the validation results."""
    lines = []
    lines.append("=" * 70)
    lines.append(f" Goldilocks Validation Report — {result['station']}")
    lines.append(f" {result['data_source']}")
    ap = result['analysis_period']
    lines.append(f" Period: {ap['start_date']} to {ap['end_date']} "
                 f"({ap['trading_days_in_period']} days)")
    lines.append("=" * 70)

    dc = result['data_coverage']
    lines.append(f"\n Data Coverage:")
    lines.append(f"   IEM ASOS obs:      {dc['days_with_iem_asos']} days, "
                 f"{dc['iem_total_observations']} obs ({dc['iem_avg_obs_per_day']}/day avg)")
    lines.append(f"   METAR hourly:      {dc['days_with_metar_hourly']} days")
    lines.append(f"   Kalshi settlements: {dc['days_with_kalshi']} days")
    lines.append(f"   All three sources:  {dc['days_with_all_three_sources']} days")

    ga = result['goldilocks_analysis']
    lines.append(f"\n Goldilocks Analysis:")
    lines.append(f"   Genuine spike events: {ga['goldilocks_events']} / "
                 f"{dc['days_with_all_three_sources']} "
                 f"({ga['goldilocks_rate']*100:.1f}%)")
    lines.append(f"   Spike invisible on METAR: {ga['spike_invisible_on_metar_events']} "
                 f"({ga['spike_invisible_rate']*100:.1f}%)")
    lines.append(f"   Kalshi matches IEM:       {ga['kalshi_matches_iem_events']} "
                 f"({ga['kalshi_match_rate']*100:.1f}%)")
    lines.append(f"   All three agree (≤0.5°F): {ga['all_three_agree_events']} "
                 f"({ga['all_three_agree_rate']*100:.1f}%)")

    ivm = ga['iem_vs_metar_max_diff']
    lines.append(f"\n   IEM max vs METAR max temp diff:")
    lines.append(f"     Mean: {ivm['mean']:+.1f}°F   Max: {ivm['max']:+.1f}°F   "
                 f"Min: {ivm['min']:+.1f}°F   Std: {ivm['std']:.1f}°F   "
                 f"n={ivm['count']}")

    ivk = ga['iem_vs_kalshi_diff']
    lines.append(f"   IEM max vs Kalshi settlement diff:")
    lines.append(f"     Mean: {ivk['mean']:+.1f}°F   Max: {ivk['max']:+.1f}°F   "
                 f"Min: {ivk['min']:+.1f}°F   Std: {ivk['std']:.1f}°F   "
                 f"n={ivk['count']}")

    rec = result.get('recommendation', {})
    lines.append(f"\n Recommendation: {rec.get('decision', 'N/A')}")
    lines.append(f"   Confidence: {rec.get('confidence', 'N/A')}")
    lines.append(f"   Rationale: {rec.get('rationale', '')}")

    lines.append(f"\n Daily events (top 30):")
    header = (
        f"  {'Date':<12} {'IEM':>5} {'METAR':>7} {'ΔI-M':>6} "
        f"{'Kalshi':>7} {'ΔI-K':>6} {'Spike':>6} {'Match':>6} {'Goldi?':>8}"
    )
    lines.append(header)
    lines.append("  " + "-" * len(header.strip()))
    for ev in result.get('daily_events', [])[:30]:
        iem = f"{ev['iem_max_f']:.0f}" if ev['iem_max_f'] else "N/A"
        met = f"{ev['metar_max_f']:.0f}" if ev['metar_max_f'] else "N/A"
        kal = f"{ev['kalshi_high_f']:.0f}" if ev['kalshi_high_f'] else "N/A"
        dim = f"{ev['iem_vs_metar_diff']:+.0f}" if ev['iem_vs_metar_diff'] is not None else "N/A"
        dik = f"{ev['iem_vs_kalshi_diff']:+.0f}" if ev['iem_vs_kalshi_diff'] is not None else "N/A"
        spike = "**" if ev.get('spike_invisible_on_metar') else "no"
        match = "OK" if ev.get('kalshi_matches_iem') else "no"
        goldi = "YES!" if ev.get('is_goldilocks') else "no"
        lines.append(f"  {ev['date']:<12} {iem:>5} {met:>7} {dim:>6} "
                     f"{kal:>7} {dik:>6} {spike:>6} {match:>6} {goldi:>8}")

    n = len(result.get('daily_events', []))
    if n > 30:
        lines.append(f"  ... ({n - 30} more days)")

    lines.append("")
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Goldilocks spike validation (IEM-powered)')
    parser.add_argument('--station', default=DEFAULT_STATION,
                        help=f'Station code (default: {DEFAULT_STATION})')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS,
                        help=f'Days to analyze (default: {DEFAULT_DAYS})')
    parser.add_argument('--output',
                        default=os.path.join(DATA_DIR, 'goldilocks_validation.json'),
                        help='Output JSON path')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose logging')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    result = validate(args.station, args.days)
    print(format_report(result))

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    logger.info("Report saved to %s", args.output)

    decision_rank = {'NO-GO': 0, 'CAUTION': 1, 'GO': 2, 'INCONCLUSIVE': 3}
    return decision_rank.get(
        result.get('recommendation', {}).get('decision', 'NO-GO'), 0)


if __name__ == '__main__':
    sys.exit(main())