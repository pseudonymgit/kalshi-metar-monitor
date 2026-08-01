#!/usr/bin/env python3
"""
WhaleWatch — Order Book Baseline Computation

Per-station, per-bucket, per-hour-of-day rolling baseline for order book
metrics (bid_size, ask_size, bid/ask ratio, spread, volume).

Computes:
  - Short-term rolling (20-period = 10 min at 30s polling)
  - Diurnal baseline per hour-of-day
  - Long window (288 periods = 1 trading day)

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# KNYC (Central Park) is US/Eastern. EST = UTC-5, EDT = UTC-4.
STATION_TIMEZONE_OFFSET = {
    s: -5 for s in [
        "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
        "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
        "KPHX", "KSAT", "KSEA", "KSFO",
    ]
}


def utc_to_local_hour(utc_ts: str, station: str) -> int:
    """Convert UTC ISO timestamp to local hour (0-23) for a station."""
    try:
        dt = datetime.fromisoformat(utc_ts.replace('Z', '+00:00'))
        # Simplified: US stations use EST/EDT
        month = dt.month
        offset = -4 if 4 <= month <= 10 else -5  # EDT/EST heuristic
        local_hour = (dt.hour + offset) % 24
        return local_hour
    except (ValueError, AttributeError):
        return 0


def compute_baseline_from_snapshots(snapshots: List[dict], station: str,
                                     series_type: str, bucket_temp_f: int) -> dict:
    """
    Compute rolling baseline metrics from recent snapshot history.

    Returns dict with rolling mean/std for key metrics, indexed by hour_of_day.
    """
    from collections import defaultdict

    # Group by hour of day
    by_hour: Dict[int, List[dict]] = defaultdict(list)
    for snap in snapshots:
        hour = utc_to_local_hour(snap['snapshot_ts'], station)
        by_hour[hour].append(snap)

    baselines = {}
    for hour, snaps in by_hour.items():
        if len(snaps) < 3:
            continue

        bid_sizes = [s.get('yes_bid_size', 0) or 0 for s in snaps]
        ask_sizes = [s.get('yes_ask_size', 1) or 1 for s in snaps]
        ratios = [s.get('bid_ask_ratio', 0) or 0 for s in snaps]
        volumes = [s.get('volume_24h', 0) or 0 for s in snaps]
        spreads = [s.get('spread_cents', 0) or 0 for s in snaps]

        baselines[hour] = {
            'rolling_mean_20': sum(bid_sizes) / len(bid_sizes),
            'rolling_std_20': std(bid_sizes),
            'rolling_mean_ratio_20': sum(ratios) / len(ratios),
            'rolling_std_ratio_20': std(ratios),
            'rolling_mean_vol_20': sum(volumes) / len(volumes),
            'rolling_std_vol_20': std(volumes),
            'rolling_mean_spread_20': sum(spreads) / len(spreads),
            'rolling_std_spread_20': std(spreads),
            'diurnal_mean': sum(bid_sizes) / len(bid_sizes),
            'diurnal_variance': variance(bid_sizes),
            'sample_count': len(snaps),
        }

    return baselines


def std(values: List[float]) -> float:
    """Compute sample standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def variance(values: List[float]) -> float:
    """Compute sample variance."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def update_station_baseline(conn, station: str, series_type: str,
                             bucket_temp_f: int, snapshots: List[dict]) -> int:
    """
    Update all hour-of-day baselines for a station-bucket from snapshot history.

    Returns count of hours updated.
    """
    from core.whale_watch_db import upsert_baseline

    baselines = compute_baseline_from_snapshots(
        snapshots, station, series_type, bucket_temp_f)

    updated = 0
    for hour, bl in baselines.items():
        bl['station'] = station
        bl['series_type'] = series_type
        bl['bucket_temp_f'] = bucket_temp_f
        bl['hour_of_day'] = hour
        upsert_baseline(conn, bl)
        updated += 1

    return updated


def refresh_all_baselines(conn) -> int:
    """
    Refresh baselines for all station-bucket-hour combinations
    that have sufficient data in order_book_snap.

    Returns count of baselines updated.
    """
    from core.whale_watch_db import get_recent_snapshots

    # Get distinct station-series-bucket combinations
    rows = conn.execute("""
        SELECT DISTINCT station, series_type, bucket_temp_f
        FROM order_book_snap
    """).fetchall()

    total = 0
    for row in rows:
        snapshots = get_recent_snapshots(
            conn, row['station'], row['series_type'], row['bucket_temp_f'], limit=100)
        if len(snapshots) >= 3:
            n = update_station_baseline(
                conn, row['station'], row['series_type'],
                row['bucket_temp_f'], snapshots)
            total += n

    conn.commit()
    return total


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    from core.whale_watch_db import init_db, get_db

    db_path = os.path.join(REPO_ROOT, 'data', 'whale_watch.db')
    conn = init_db(None, db_path)

    if os.path.exists(db_path):
        n = refresh_all_baselines(conn)
        print(f"Refreshed {n} baselines")
    else:
        print("No WhaleWatch DB found. Run order_book_collector first.")

    conn.close()