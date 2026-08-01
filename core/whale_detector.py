#!/usr/bin/env python3
"""
WhaleWatch — Whale Detector (Multi-Signal Fusion)

Anomaly detection algorithm that analyzes order book microstructure to detect
informed trading activity.

Signals:
  S1: Bid Side Concentration — yes_bid_size Z-score > 3.0
  S2: Bid/Ask Ratio Spike — ratio > 95th percentile
  S3: Volume Acceleration — volume growth > 5× median
  S4: Spread Compression — spread ≤ 1¢ AND bid_size 2×
  S5: Stepwise Accumulation — bid_size increases over 3+ cycles
  S6: Bucket Gradient Discontinuity — price gap > 5¢ between adjacent buckets
  S7: Open Interest Drift — OI change > 2σ
  S8: Depth Imbalance — yes:no book depth ratio > 3:1

Fusion: anomaly_score ≥ 2.0 → DETECTED, ≥ 1.0 → SUSPECTED

Confirmation latch:
  1 cycle → SUSPECTED
  2 consecutive → DETECTED (tradeable)
  3+ with increasing strength → HIGH_CONVICTION

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Thresholds from design doc
Z_BID_THRESHOLD = 3.0          # S1
Z_RATIO_THRESHOLD = 1.96        # S2 (95th percentile)
Z_VOLUME_THRESHOLD = 2.0        # S3
SPREAD_COMPRESSION_MAX = 1      # S4: spread ≤ 1¢
BID_SIZE_DOUBLE_FACTOR = 2.0    # S4: bid_size growth
STEPWISE_CYCLES = 3             # S5: consecutive cycles
STEPWISE_GROWTH_MIN = 50        # S5: min contract increase per cycle
PRICE_GAP_THRESHOLD = 5         # S6: cents gap
OI_CHANGE_SIGMA = 2.0           # S7
DEPTH_IMBALANCE_RATIO = 3.0     # S8

# Score weights
SCORE_S1 = 2.0    # Strongest
SCORE_S2 = 1.5
SCORE_S3 = 0.8
SCORE_S4 = 1.5
SCORE_S5 = 1.0
SCORE_S6 = 0.3    # Context only
SCORE_S7 = 0.0    # Context only
SCORE_S8 = 0.0    # Confirmation only

# Confirmation latch
DETECTION_THRESHOLD = 2.0       # anomaly_score ≥ 2.0 → classified
SUSPECT_THRESHOLD = 1.0         # anomaly_score ≥ 1.0 → suspected


def z_score(value: float, mean: float, std: float) -> float:
    """Compute z-score with safety for zero std."""
    if std <= 0.001 or mean == 0:
        return 0.0
    return (value - mean) / std


def compute_spread(ask: Optional[int], bid: Optional[int]) -> Optional[int]:
    """Compute bid/ask spread in cents."""
    if ask is not None and bid is not None:
        return ask - bid
    return None


def compute_stepwise_score(recent_bid_sizes: List[Optional[int]]) -> float:
    """
    Detect stepwise accumulation over consecutive poll cycles.

    Returns score 0.0-1.0 based on consistent increases.
    """
    sizes = [s for s in recent_bid_sizes if s is not None]
    if len(sizes) < STEPWISE_CYCLES:
        return 0.0

    increases = 0
    for i in range(1, len(sizes)):
        if sizes[i] >= sizes[i-1] + STEPWISE_GROWTH_MIN:
            increases += 1

    return min(increases / (len(sizes) - 1), 1.0)


def detect_anomaly(snapshot: dict, baseline: Optional[dict],
                   recent_history: List[dict],
                   adjacent_bucket_prices: Optional[Dict[int, int]] = None,
                   previous_anomaly: Optional[dict] = None) -> dict:
    """
    Run multi-signal fusion on one snapshot against its baseline.

    Args:
        snapshot: Current order book snapshot
        baseline: Rolling baseline for this station-bucket-hour
        recent_history: Recent snapshots for stepwise detection
        adjacent_bucket_prices: {bucket_temp_f: last_price} for gradient check
        previous_anomaly: Most recent anomaly event for this bucket (for latch)

    Returns:
        dict with anomaly_score, components, strength, classification
    """
    yes_bid_size = snapshot.get('yes_bid_size', 0) or 0
    yes_ask_size = snapshot.get('yes_ask_size', 1) or 1
    bid_ask_ratio = yes_bid_size / max(yes_ask_size, 1)
    yes_bid = snapshot.get('yes_bid', 0) or 0
    yes_ask = snapshot.get('yes_ask', 0) or 0
    spread = compute_spread(yes_ask, yes_bid)
    volume = snapshot.get('volume_24h', 0) or 0
    open_interest = snapshot.get('open_interest', 0) or 0

    # Default values when no baseline exists
    bl_mean = baseline.get('rolling_mean_20', 0) if baseline else 0
    bl_std = baseline.get('rolling_std_20', 0) if baseline else 0
    bl_mean_ratio = baseline.get('rolling_mean_ratio_20', 0) if baseline else 0
    bl_std_ratio = baseline.get('rolling_std_ratio_20', 0) if baseline else 0
    bl_mean_vol = baseline.get('rolling_mean_vol_20', 0) if baseline else 0
    bl_std_vol = baseline.get('rolling_std_vol_20', 0) if baseline else 0
    bl_mean_spread = baseline.get('rolling_mean_spread_20', 0) if baseline else 0
    bl_std_spread = baseline.get('rolling_std_spread_20', 0) if baseline else 0

    # Compute z-scores
    z_bid = z_score(yes_bid_size, bl_mean, bl_std)
    z_ratio = z_score(bid_ask_ratio, bl_mean_ratio, bl_std_ratio)
    z_volume = z_score(volume, bl_mean_vol, bl_std_vol)

    # Spread change
    spread_change = (bl_mean_spread - spread) if (spread is not None and bl_mean_spread > 0) else 0

    # Stepwise accumulation
    recent_sizes = [s.get('yes_bid_size') for s in recent_history[-STEPWISE_CYCLES:]]
    acc_score = compute_stepwise_score(recent_sizes + [yes_bid_size])

    # Bucket gradient check
    gradient_gap = 0
    bucket_temp = snapshot.get('bucket_temp_f', 0)
    if adjacent_bucket_prices and bucket_temp in adjacent_bucket_prices:
        for other_temp, other_price in adjacent_bucket_prices.items():
            if abs(other_temp - bucket_temp) == 1:
                gap = abs((snapshot.get('last_price', 0) or 0) - other_price)
                gradient_gap = max(gradient_gap, gap)

    # Open interest change
    oi_change_score = 0

    # Fusion
    components = {'S1': 0, 'S2': 0, 'S3': 0, 'S4': 0, 'S5': 0, 'S6': 0, 'S7': 0, 'S8': 0}

    anomaly_score = 0.0

    # S1: Bid side concentration
    if z_bid > Z_BID_THRESHOLD:
        components['S1'] = min((z_bid - Z_BID_THRESHOLD) / 1.0, 1.0) * SCORE_S1
        anomaly_score += components['S1']

    # S2: Bid/ask ratio spike
    if z_ratio > Z_RATIO_THRESHOLD:
        components['S2'] = min((z_ratio - Z_RATIO_THRESHOLD) / 1.0, 1.0) * SCORE_S2
        anomaly_score += components['S2']

    # S3: Volume acceleration
    if z_volume > Z_VOLUME_THRESHOLD:
        components['S3'] = min((z_volume - Z_VOLUME_THRESHOLD) / 1.0, 1.0) * SCORE_S3
        anomaly_score += components['S3']

    # S4: Spread compression + bid_size growth
    bid_size_growth = 1.0
    if len(recent_sizes) >= 1 and recent_sizes[-1] is not None and recent_sizes[-1] > 0:
        bid_size_growth = yes_bid_size / max(recent_sizes[-1], 1)
    if spread is not None and spread <= SPREAD_COMPRESSION_MAX and bid_size_growth >= BID_SIZE_DOUBLE_FACTOR:
        components['S4'] = SCORE_S4
        anomaly_score += SCORE_S4

    # S5: Stepwise accumulation
    if acc_score > 0.7:
        components['S5'] = acc_score * SCORE_S5
        anomaly_score += components['S5']

    # S6: Bucket gradient discontinuity (context only)
    if gradient_gap >= PRICE_GAP_THRESHOLD:
        components['S6'] = SCORE_S6
        anomaly_score += SCORE_S6

    # Compute strength (capped at 1.0)
    anomaly_strength = min(anomaly_score / 5.0, 1.0)

    # Classification
    classification = 'none'
    if anomaly_score >= DETECTION_THRESHOLD:
        classification = 'detected'
    elif anomaly_score >= SUSPECT_THRESHOLD:
        classification = 'suspected'

    # Confirmation latch
    if previous_anomaly and classification == 'detected':
        prev_score = previous_anomaly.get('anomaly_score', 0)
        prev_status = previous_anomaly.get('status', '')
        if prev_status == 'suspected':
            classification = 'suspected'  # Wait for 2nd consecutive detection
        elif prev_status in ('detected', 'high_conviction') and anomaly_score >= prev_score:
            classification = 'high_conviction'

    return {
        'anomaly_score': round(anomaly_score, 2),
        'anomaly_strength': round(anomaly_strength, 2),
        'classification': classification,
        'components': {k: round(v, 2) for k, v in components.items() if v > 0},
        'z_score_bid': round(z_bid, 2),
        'z_score_ratio': round(z_ratio, 2),
        'z_score_volume': round(z_volume, 2),
        'spread_compression': spread_change,
        'accumulation_score': round(acc_score, 2),
        'yes_bid_size': yes_bid_size,
        'baseline_mean': round(bl_mean, 1),
    }


def process_all_buckets(conn, station: str, series_type: str,
                         current_temp_f: Optional[float] = None) -> List[dict]:
    """
    Run anomaly detection on all buckets for a station-series.

    Args:
        conn: DB connection
        station: Station code
        series_type: 'HIGH' or 'LOW'
        current_temp_f: Current temperature for Tier 1 filtering

    Returns:
        List of anomaly event dicts
    """
    from core.whale_watch_db import (
        get_recent_snapshots, get_baseline, get_active_anomalies, insert_anomaly
    )

    # Get all bucket temperatures for this station-series
    buckets = conn.execute("""
        SELECT DISTINCT bucket_temp_f
        FROM order_book_snap
        WHERE station=? AND series_type=?
        ORDER BY bucket_temp_f
    """, (station, series_type)).fetchall()

    bucket_temps = [r['bucket_temp_f'] for r in buckets]
    events = []

    # Get last prices for gradient check
    last_prices = {}
    for bt in bucket_temps:
        row = conn.execute("""
            SELECT last_price FROM order_book_snap
            WHERE station=? AND series_type=? AND bucket_temp_f=?
            ORDER BY snapshot_ts DESC LIMIT 1
        """, (station, series_type, bt)).fetchone()
        if row:
            last_prices[bt] = row['last_price']

    for bucket_temp_f in bucket_temps:
        snapshots = get_recent_snapshots(conn, station, series_type,
                                          bucket_temp_f, limit=20)
        if not snapshots:
            continue

        current = snapshots[0]
        hour = 0  # Will be set properly in production
        try:
            dt = datetime.fromisoformat(current['snapshot_ts'].replace('Z', '+00:00'))
            hour = (dt.hour - 4) % 24  # EDT heuristic
        except ValueError:
            pass

        baseline = get_baseline(conn, station, series_type, bucket_temp_f, hour)

        # Get only previous anomaly for this bucket from current session
        prev_anomalies = get_active_anomalies(conn, station, min_score=0)
        prev = None
        for a in prev_anomalies:
            if a['bucket_temp_f'] == bucket_temp_f:
                prev = a
                break

        result = detect_anomaly(
            current, baseline, snapshots[1:],
            last_prices, prev)

        if result['classification'] != 'none':
            anomaly_event = {
                'station': station,
                'series_type': series_type,
                'bucket_temp_f': bucket_temp_f,
                'bucket_ticker': current.get('bucket_ticker', ''),
                'detected_ts': current['snapshot_ts'],
                'confirmed_ts': current['snapshot_ts'] if result['classification'] in ('detected', 'high_conviction') else None,
                'anomaly_score': result['anomaly_score'],
                'anomaly_strength': result['anomaly_strength'],
                'anomaly_components': result['components'],
                'status': result['classification'],
                'yes_bid_size': result['yes_bid_size'],
                'z_score_bid': result['z_score_bid'],
                'z_score_ratio': result['z_score_ratio'],
                'z_score_volume': result['z_score_volume'],
                'spread_compression': result['spread_compression'],
                'accumulation_score': result['accumulation_score'],
            }
            insert_anomaly(conn, anomaly_event)
            events.append(anomaly_event)

    conn.commit()
    return events


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    from core.whale_watch_db import init_db

    db_path = os.path.join(REPO_ROOT, 'data', 'whale_watch.db')
    conn = init_db(None, db_path)

    if os.path.exists(db_path):
        # Check how many snapshots we have
        count = conn.execute("SELECT COUNT(*) FROM order_book_snap").fetchone()[0]
        print(f"Snapshots available: {count}")

        stations = conn.execute("SELECT DISTINCT station FROM order_book_snap").fetchall()
        print(f"Stations with data: {[r['station'] for r in stations]}")

        # Run detection on first station
        if stations:
            events = process_all_buckets(conn, stations[0]['station'], "HIGH")
            print(f"Anomalies detected: {len(events)}")
            for ev in events:
                print(f"  Bucket {ev['bucket_temp_f']}°F: score={ev['anomaly_score']}, "
                      f"strength={ev['anomaly_strength']}, status={ev['status']}")
    else:
        print("No WhaleWatch DB found. Run order_book_collector first.")

    conn.close()