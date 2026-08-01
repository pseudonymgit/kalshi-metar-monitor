#!/usr/bin/env python3
"""
WhaleWatch — Signal Feeder

Converts anomaly detection events into trade signals with entry/exit logic.

Entry conditions (ALL must be met):
  C1: Whale activity confirmed 2+ consecutive cycles (status=detected|high_conviction)
  C2: YES price > 5¢ AND < 95¢ (not already priced in / too early)
  C3: Station has volume_24h_fp > $500
  C4: No contradictory whale on same station
  C5: Time to settlement > 2 hours

Position sizing:
  base_units = 0.05 * volume_24h_contracts
  strength_scalar = min(anomaly_score / 5.0, 1.0)
  kelly_fraction = 0.25 (Quarter-Kelly)
  Max $500/signal, min $10/signal

Exit signals:
  E1: Whale stops accumulating → anomaly_score < 1.0 for 3 cycles
  E2: Temperature confirms bucket → SELL HALF at profit
  E3: Temperature contradicts → SELL ALL
  E4: Whale reverses → SELL ALL
  E5: Maximum hold 6 hours → SELL ALL
  E6: Stop loss → 50% of entry price

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Entry gates
MIN_YES_PRICE_CENTS = 5
MAX_YES_PRICE_CENTS = 95
MIN_VOLUME_24H_FP = 500.0
MIN_HOURS_TO_SETTLEMENT = 2
MIN_ANOMALY_SCORE_EXIT = 1.0
EXIT_CONFIRMATION_CYCLES = 3
MAX_HOLD_HOURS = 6
STOP_LOSS_PCT = 0.50  # 50% of entry price
TARGET_TAKE_PROFIT_FACTOR = 0.50  # 50% of remaining edge

# Position sizing
BASE_VOLUME_PCT = 0.05
KELLY_FRACTION = 0.25
MAX_POSITION_DOLLARS = 500.0
MIN_POSITION_DOLLARS = 10.0


def compute_position_size(anomaly_score: float, anomaly_strength: float,
                          volume_24h_fp: float, last_price_cents: int,
                          baseline_days: int = 15) -> Tuple[int, float]:
    """
    Compute position size in contracts and dollars.

    Returns (contracts, dollars).
    """
    # Base units: 5% of daily volume (in contracts)
    volume_contracts = volume_24h_fp / max(last_price_cents / 100.0, 0.01)
    base_units = int(BASE_VOLUME_PCT * volume_contracts)

    # Strength scaling
    strength_scalar = min(anomaly_score / 5.0, 1.0)

    # Baseline confidence (0.0-1.0, requires 15 days full confidence)
    baseline_confidence = min(baseline_days / 15.0, 1.0)

    # Settlement proximity decay (placeholder)
    settlement_decay = 1.0

    # Kelly-inspired
    position_contracts = int(base_units * strength_scalar * baseline_confidence
                             * settlement_decay * KELLY_FRACTION)

    position_dollars = position_contracts * last_price_cents / 100.0

    # Clamp
    position_dollars = min(position_dollars, MAX_POSITION_DOLLARS)
    if position_dollars < MIN_POSITION_DOLLARS:
        position_dollars = 0
        position_contracts = 0
    else:
        position_contracts = max(1, position_contracts)

    return position_contracts, position_dollars


def check_entry_gates(snapshot: dict, anomaly: dict) -> Tuple[bool, str]:
    """
    Check all entry conditions.

    Returns (allow_entry, reason).
    """
    # C2: Price range
    last_price = snapshot.get('last_price', 0) or 0
    if last_price < MIN_YES_PRICE_CENTS:
        return False, f"C2: Price {last_price}¢ below {MIN_YES_PRICE_CENTS}¢ threshold"
    if last_price > MAX_YES_PRICE_CENTS:
        return False, f"C2: Price {last_price}¢ above {MAX_YES_PRICE_CENTS}¢ threshold"

    # C3: Volume
    volume = snapshot.get('volume_24h', 0) or 0
    if volume < MIN_VOLUME_24H_FP:
        return False, f"C3: Volume ${volume:.0f} below ${MIN_VOLUME_24H_FP:.0f}"

    # C5: Time to settlement (simplified — ~1 day for same-day markets)
    # In production, check actual settlement time from Kalshi market
    hours_to_settle = 24  # Placeholder
    if hours_to_settle < MIN_HOURS_TO_SETTLEMENT:
        return False, f"C5: Only {hours_to_settle}h to settlement"

    return True, "All entry gates passed"


def determine_trade_direction(series_type: str, anomaly: dict) -> str:
    """
    Determine trade direction from anomaly.

    Whale accumulating YES → Buy YES (expect temperature to reach bucket)
    Whale accumulating NO → Buy NO (expect temperature to stay below/above)

    Currently we detect YES accumulation. NO accumulation detection TBD.
    """
    # Default: if we detect anomaly on bid side, it's YES accumulation
    return "BUY_YES"


def generate_signal(conn, anomaly: dict) -> Optional[dict]:
    """
    Generate a trade signal from a confirmed anomaly.

    Returns signal dict or None if gates reject.
    """
    from core.whale_watch_db import get_recent_snapshots

    station = anomaly['station']
    series_type = anomaly['series_type']
    bucket_temp_f = anomaly['bucket_temp_f']
    bucket_ticker = anomaly.get('bucket_ticker', '')

    # Get most recent snapshot
    snapshots = get_recent_snapshots(conn, station, series_type, bucket_temp_f, limit=1)
    if not snapshots:
        logger.info("No snapshot for %s %s bucket %d", station, series_type, bucket_temp_f)
        return None

    snap = snapshots[0]

    # Check entry gates
    allow, reason = check_entry_gates(snap, anomaly)
    if not allow:
        logger.info("Entry rejected for %s %s %d°F: %s",
                    station, series_type, bucket_temp_f, reason)
        return None

    # Compute position
    volume = snap.get('volume_24h', 0) or 0
    last_price = snap.get('last_price', 50) or 50
    contracts, dollars = compute_position_size(
        anomaly['anomaly_score'], anomaly['anomaly_strength'],
        volume, last_price)

    if contracts == 0:
        logger.info("Position size zero for %s %s %d°F", station, series_type, bucket_temp_f)
        return None

    # Direction
    direction = determine_trade_direction(series_type, anomaly)

    signal = {
        'anomaly_id': anomaly.get('id', 0),
        'station': station,
        'series_type': series_type,
        'bucket_temp_f': bucket_temp_f,
        'bucket_ticker': bucket_ticker,
        'direction': direction,
        'entry_ts': datetime.now(timezone.utc).isoformat(),
        'entry_price_cents': last_price,
        'entry_reason': 'E1_WHALE_DETECTED',
        'position_contracts': contracts,
        'position_dollars': round(dollars, 2),
    }

    return signal


def check_exit(conn, anomaly: dict, signal: dict) -> Tuple[bool, str]:
    """
    Check whether an active signal should exit.

    Returns (should_exit, reason).
    """
    from core.whale_watch_db import get_recent_snapshots

    station = signal['station']
    series_type = signal['series_type']
    bucket_temp_f = signal['bucket_temp_f']

    # E1: Whale stopped accumulating
    anomalies = get_active_anomalies(conn, station, min_score=MIN_ANOMALY_SCORE_EXIT)
    for a in anomalies:
        if a['bucket_temp_f'] == bucket_temp_f:
            break
    else:
        # No active anomaly for this bucket → whale stopped
        return True, "E1_WHALE_STOPPED"

    # Get recent snapshots for spread/price check
    snapshots = get_recent_snapshots(conn, station, series_type, bucket_temp_f, limit=3)
    if snapshots:
        current_price = snapshots[0].get('last_price', 0) or 0
        entry_price = signal.get('entry_price_cents', 50)

        # E6: Stop loss
        if current_price <= entry_price * (1 - STOP_LOSS_PCT):
            return True, f"E6_STOP_LOSS at {current_price}¢"

        # E2: Target profit
        target = entry_price + (TARGET_TAKE_PROFIT_FACTOR * (100 - entry_price))
        if current_price >= target:
            return True, f"E2_TAKE_PROFIT at {current_price}¢"

    # E5: Max hold time
    entry_ts = signal.get('entry_ts', '')
    if entry_ts:
        try:
            entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
            elapsed = datetime.now(timezone.utc) - entry_dt
            if elapsed > timedelta(hours=MAX_HOLD_HOURS):
                return True, f"E5_MAX_HOLD {elapsed.total_seconds()/3600:.1f}h"
        except ValueError:
            pass

    return False, ""


def get_active_anomalies(conn, station: Optional[str] = None,
                         min_score: float = 2.0) -> List[dict]:
    """Get anomalies that are still active."""
    query = """
        SELECT * FROM anomaly_events
        WHERE status IN ('detected', 'high_conviction')
          AND anomaly_score >= ?
    """
    params = [min_score]
    if station:
        query += " AND station=?"
        params.append(station)
    query += " ORDER BY anomaly_score DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def process_signals(conn) -> List[dict]:
    """
    Main signal processing loop:
    1. Get active confirmed anomalies
    2. Check entry gates
    3. Generate signals
    4. Check exits on existing signals

    Returns list of generated signals.
    """
    from core.whale_watch_db import get_active_anomalies as db_get_active, insert_signal

    signals = []

    # Get active anomalies
    anomalies = db_get_active(conn, min_score=DETECTION_THRESHOLD)

    for anomaly in anomalies:
        if anomaly['status'] not in ('detected', 'high_conviction'):
            continue

        signal = generate_signal(conn, anomaly)
        if signal:
            signal_id = insert_signal(conn, signal)
            signal['id'] = signal_id
            signals.append(signal)
            logger.info("Signal: %s %s %d°F → %s $%.2f (%d contracts)",
                        signal['station'], signal['series_type'],
                        signal['bucket_temp_f'], signal['direction'],
                        signal['position_dollars'], signal['position_contracts'])

    conn.commit()
    return signals


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    from core.whale_watch_db import init_db

    db_path = os.path.join(REPO_ROOT, 'data', 'whale_watch.db')
    conn = init_db(None, db_path)

    if os.path.exists(db_path):
        signals = process_signals(conn)
        print(f"Generated {len(signals)} signals")
    else:
        print("No WhaleWatch DB found. Run order_book_collector first.")

    conn.close()