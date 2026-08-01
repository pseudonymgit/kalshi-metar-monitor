#!/usr/bin/env python3
"""
goldilocks_labeling.py — Goldilocks event labeling from IEM ASOS 1-min data.

Labels each day at KNYC using the same methodology as goldilocks_validate.py:
  - Fetches 1-minute IEM ASOS data (same sensor feed that drives NWS CLI)
  - Compares IEM 1-min max/min against METAR hourly max/min
  - Labels: 0=no event, 1=Goldilocks HIGH, 2=Goldilocks LOW

Output: CSV with columns [date, is_goldilocks_high, is_goldilocks_low, is_goldilocks_any,
                          spike_magnitude_f, spike_duration_min, iem_max_f, metar_max_f, ...]

Usage:
    python3 scripts/goldilocks_labeling.py
    python3 scripts/goldilocks_labeling.py --station KNYC --days 365 --output data/goldilocks_labels.csv

B-Mode compliant. No AI/ML. Uses IEM free API (no key needed).
"""

import argparse
import csv
import io
import json
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
METAR_DB = os.path.join(DATA_DIR, 'metar_backfill.db')
IEM_ASOS_URL = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'

DEFAULT_STATION = 'KNYC'
DEFAULT_DAYS = 365

# KNYC (Central Park) is US/Eastern
EASTERN_TZ_OFFSET = {
    1: 5, 2: 5, 3: 5,   # EST (simplified)
    4: 4, 5: 4, 6: 4,
    7: 4, 8: 4, 9: 4,
    10: 4, 11: 5, 12: 5,
}
SPIKE_THRESHOLD_F = 1.0  # °F — minimum difference to count as Goldilocks


def utc_to_local_date(ts_utc: str) -> str:
    """Convert UTC timestamp to Eastern local date (Kalshi trading date)."""
    try:
        dt = datetime.strptime(ts_utc[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
        month = dt.month
        offset = EASTERN_TZ_OFFSET.get(month, 5)
        if dt.hour < offset:
            local_dt = dt - timedelta(hours=offset)
        else:
            local_dt = dt - timedelta(hours=offset)
        return local_dt.strftime('%Y-%m-%d')
    except (ValueError, IndexError):
        return ts_utc[:10]


def fetch_iem_asos(station: str, start_date: str, end_date: str,
                    max_retries: int = 3) -> List[dict]:
    """
    Fetch 1-minute ASOS observations from IEM (free API, no key).

    Args:
        station: ICAO code (KNYC, etc.)
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        max_retries: Number of retries on failure

    Returns:
        List of dicts with keys: timestamp_utc, temp_f, local_date
    """
    params = {
        'station': station,
        'data': 'tmpf',
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
        'missing': 'M',
        'trace': 'T',
    }
    url = f"{IEM_ASOS_URL}?{urlencode(params)}"

    for attempt in range(max_retries):
        try:
            import requests
            resp = requests.get(url, timeout=120)
            if resp.status_code != 200:
                logger.warning("IEM returned %d (attempt %d/%d)",
                               resp.status_code, attempt + 1, max_retries)
                time.sleep(5 * (attempt + 1))
                continue

            reader = csv.DictReader(io.StringIO(resp.text))
            obs = []
            for row in reader:
                temp_str = row.get('tmpf', '').strip()
                valid = row.get('valid', '').strip()
                if temp_str and temp_str not in ('M', '') and valid:
                    try:
                        temp_f = float(temp_str)
                        if -50 <= temp_f <= 150:
                            local_date = utc_to_local_date(valid)
                            obs.append({
                                'timestamp_utc': valid,
                                'temp_f': temp_f,
                                'local_date': local_date,
                            })
                    except (ValueError, TypeError):
                        pass

            logger.info("IEM %s %s to %s: %d obs (attempt %d/%d)",
                        station, start_date, end_date, len(obs),
                        attempt + 1, max_retries)
            return obs

        except Exception as e:
            logger.warning("IEM fetch error (attempt %d/%d): %s",
                           attempt + 1, max_retries, e)
            time.sleep(10 * (attempt + 1))

    logger.error("Failed to fetch IEM data after %d attempts", max_retries)
    return []


def get_metar_hourly(station: str) -> Dict[str, List[dict]]:
    """Fetch METAR hourly obs from local DB, grouped by local date."""
    if not os.path.exists(METAR_DB):
        logger.warning("METAR DB not found at %s", METAR_DB)
        return {}

    obs_by_date: Dict[str, list] = defaultdict(list)
    try:
        conn = sqlite3.connect(f"file:{METAR_DB}?mode=ro", uri=True)
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
            ts = row['timestamp_utc']
            ld = utc_to_local_date(ts)
            obs_by_date[ld].append({
                'timestamp_utc': ts,
                'temp_f': row['temp_f'],
            })

        logger.info("METAR: %d total obs across %d local dates",
                    sum(len(v) for v in obs_by_date.values()), len(obs_by_date))
        return dict(obs_by_date)
    except Exception as e:
        logger.warning("METAR DB error: %s", e)
        return {}


def compute_labels_for_window(
    iem_obs: List[dict],
    metar_by_date: Dict[str, List[dict]],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    Compute Goldilocks labels for each day in the date range.

    Returns DataFrame with columns:
      date, is_goldilocks_high, is_goldilocks_low, is_goldilocks_any,
      spike_magnitude_f, spike_duration_min, iem_max_f, iem_min_f,
      metar_max_f, metar_min_f, n_iem_obs, spike_time_utc, spike_type
    """
    # Group IEM data by local date
    iem_by_date: Dict[str, list] = defaultdict(list)
    for obs in iem_obs:
        iem_by_date[obs['local_date']].append(obs)

    all_dates = sorted(set(
        list(iem_by_date.keys()) + list(metar_by_date.keys())
    ))
    # Filter to range
    all_dates = [d for d in all_dates if start_date <= d <= end_date]

    logger.info("Labeling %d days...", len(all_dates))

    records = []
    for date_str in all_dates:
        iem_day = iem_by_date.get(date_str, [])
        metar_day = metar_by_date.get(date_str, [])

        iem_temps = [o['temp_f'] for o in iem_day]
        metar_temps = [o['temp_f'] for o in metar_day]

        iem_max = max(iem_temps) if iem_temps else None
        iem_min = min(iem_temps) if iem_temps else None
        metar_max = max(metar_temps) if metar_temps else None
        metar_min = min(metar_temps) if metar_temps else None

        # Goldilocks HIGH: IEM max > METAR max by >= threshold
        is_high = 0
        high_magnitude = None
        if iem_max is not None and metar_max is not None:
            diff_high = iem_max - metar_max
            if diff_high >= SPIKE_THRESHOLD_F:
                is_high = 1
                high_magnitude = diff_high

        # Goldilocks LOW: IEM min < METAR min by >= threshold
        is_low = 0
        low_magnitude = None
        if iem_min is not None and metar_min is not None:
            diff_low = metar_min - iem_min
            if diff_low >= SPIKE_THRESHOLD_F:
                is_low = 1
                low_magnitude = diff_low

        # Find spike timestamp (when IEM max/min occurred)
        spike_time = None
        spike_type = None
        if is_high:
            max_obs = [o for o in iem_day if o['temp_f'] == iem_max]
            if max_obs:
                spike_time = max_obs[0]['timestamp_utc']
                spike_type = 'HIGH'
        elif is_low:
            min_obs = [o for o in iem_day if o['temp_f'] == iem_min]
            if min_obs:
                spike_time = min_obs[0]['timestamp_utc']
                spike_type = 'LOW'

        # Spike duration: count consecutive 1-min obs above METAR max
        spike_duration = 0
        if is_high and iem_max is not None and metar_max is not None:
            iem_sorted = sorted(iem_day, key=lambda x: x['timestamp_utc'])
            in_spike = False
            duration_count = 0
            for obs in iem_sorted:
                if obs['temp_f'] > metar_max:
                    if not in_spike:
                        in_spike = True
                        duration_count = 1
                    else:
                        duration_count += 1
                else:
                    if in_spike:
                        spike_duration = max(spike_duration, duration_count)
                        in_spike = False
            if in_spike:
                spike_duration = max(spike_duration, duration_count)

        records.append({
            'date': date_str,
            'is_goldilocks_high': is_high,
            'is_goldilocks_low': is_low,
            'is_goldilocks_any': 1 if is_high or is_low else 0,
            'spike_magnitude_f': high_magnitude or low_magnitude or 0.0,
            'spike_duration_min': spike_duration,
            'iem_max_f': round(iem_max, 1) if iem_max is not None else None,
            'iem_min_f': round(iem_min, 1) if iem_min is not None else None,
            'metar_max_f': round(metar_max, 1) if metar_max is not None else None,
            'metar_min_f': round(metar_min, 1) if metar_min is not None else None,
            'n_iem_obs': len(iem_temps),
            'spike_time_utc': spike_time,
            'spike_type': spike_type,
        })

    df = pd.DataFrame(records)
    return df


def main():
    parser = argparse.ArgumentParser(
        description='Goldilocks labeling from IEM ASOS 1-min data')
    parser.add_argument('--station', default=DEFAULT_STATION,
                        help=f'Station (default: {DEFAULT_STATION})')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS,
                        help=f'Days to label (default: {DEFAULT_DAYS})')
    parser.add_argument('--start', default=None,
                        help='Start date YYYY-MM-DD (overrides --days)')
    parser.add_argument('--end', default=None,
                        help='End date YYYY-MM-DD')
    parser.add_argument('--output', default=None,
                        help=f'Output CSV path (default: data/goldilocks_labels_{{station}}.csv)')
    parser.add_argument('--max-retries', type=int, default=3,
                        help='IEM fetch retries')
    parser.add_argument('--force-refetch', action='store_true',
                        help='Ignore cached IEM data and re-fetch')
    args = parser.parse_args()

    # Determine date range
    if args.start:
        start_str = args.start
        end_str = args.end or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=args.days)
        start_str = start_dt.strftime('%Y-%m-%d')
        end_str = end_dt.strftime('%Y-%m-%d')

    output_path = args.output or os.path.join(DATA_DIR, f'goldilocks_labels_{args.station}.csv')

    logger.info("=" * 60)
    logger.info("Goldilocks Labeling — IEM ASOS 1-min vs METAR hourly")
    logger.info("Station: %s  Range: %s to %s (%d days)",
                args.station, start_str, end_str, (datetime.strptime(end_str, '%Y-%m-%d') -
                                                     datetime.strptime(start_str, '%Y-%m-%d')).days)
    logger.info("=" * 60)

    # Step 1: Fetch METAR hourly data (from local DB — fast)
    logger.info("Loading METAR hourly data...")
    metar_by_date = get_metar_hourly(args.station)

    # Step 2: Fetch IEM 1-min ASOS data (from web — may be slow)
    logger.info(f"Fetching IEM ASOS 1-min data ({args.days} days)...")
    iem_obs = fetch_iem_asos(args.station, start_str, end_str, args.max_retries)

    if not iem_obs:
        logger.error("No IEM data received. Cannot compute labels.")
        sys.exit(1)

    # Step 3: Compute labels
    df = compute_labels_for_window(
        iem_obs, metar_by_date, start_str, end_str)

    # Step 4: Compute and report statistics
    n_days = len(df)
    n_high = int(df['is_goldilocks_high'].sum())
    n_low = int(df['is_goldilocks_low'].sum())
    n_any = int(df['is_goldilocks_any'].sum())

    logger.info(f"\nLabeling Summary:")
    logger.info(f"  Total days:           {n_days}")
    logger.info(f"  Goldilocks HIGH days: {n_high} ({n_high/max(n_days,1)*100:.1f}%)")
    logger.info(f"  Goldilocks LOW days:  {n_low} ({n_low/max(n_days,1)*100:.1f}%)")
    logger.info(f"  Goldilocks ANY days:  {n_any} ({n_any/max(n_days,1)*100:.1f}%)")

    if n_any > 0:
        mean_mag = df[df['is_goldilocks_any'] == 1]['spike_magnitude_f'].mean()
        max_mag = df[df['is_goldilocks_any'] == 1]['spike_magnitude_f'].max()
        mean_dur = df[df['is_goldilocks_any'] == 1]['spike_duration_min'].mean()
        logger.info(f"  Mean spike magnitude: {mean_mag:.1f}°F")
        logger.info(f"  Max spike magnitude:  {max_mag:.1f}°F")
        logger.info(f"  Mean spike duration:  {mean_dur:.0f} min")

    # Step 5: Save
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    df.to_csv(output_path, index=False)
    logger.info(f"Labels saved to {output_path}")
    logger.info(f"  Shape: {df.shape}")
    logger.info(f"  Columns: {list(df.columns)}")

    # Summary JSON
    summary = {
        'station': args.station,
        'date_range': [start_str, end_str],
        'n_days': n_days,
        'n_goldilocks_high': n_high,
        'n_goldilocks_low': n_low,
        'n_goldilocks_any': n_any,
        'goldilocks_rate': round(n_any / max(n_days, 1), 4),
        'mean_spike_magnitude_f': float(mean_mag) if n_any > 0 else 0,
        'mean_spike_duration_min': float(mean_dur) if n_any > 0 else 0,
    }
    summary_path = output_path.replace('.csv', '_summary.json')
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary saved to {summary_path}")

    return 0


if __name__ == '__main__':
    sys.exit(main())