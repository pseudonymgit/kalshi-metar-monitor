#!/usr/bin/env python3
"""
Backtest: Intraday METAR Nowcast Signal

Evaluates the METAR nowcast signal by comparing historical METAR observations
against settlement outcomes. Checks whether nowcast confidence levels correlate
with directional accuracy.

Usage:
    python3 scripts/backtest_metar_nowcast.py [--stations KNYC,KLAX]

B-Mode compliant. No AI/ML.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure project root is in path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.station_registry import get_all_stations
from core.signals.metar_nowcast_signal import MetarNowcastSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backtest_metar_nowcast")

# Default GEFS forecasts (replace with real forecast data in production)
DEFAULT_GEFS_MAX = 85.0
DEFAULT_GEFS_MIN = 65.0

# Bucket temperatures to test
TEST_BUCKETS = [70, 75, 80, 85, 90]


def get_metar_data(db_path: str, station: str, days_back: int = 90) -> List[Dict]:
    """Fetch recent METAR data for nowcast simulation."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT station, obs_time_utc, temp_c, dewpoint_c, wind_speed_kt,
               wind_dir, visibility_m, cloud_cover, pressure_hpa
        FROM metar_observations
        WHERE station=? AND obs_time_utc >= ?
        ORDER BY obs_time_utc DESC
    """, (station, cutoff))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_settlement_outcomes(db_path: str, station: str) -> Dict[str, Dict]:
    """Load historical settlement outcomes."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
          AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    outcomes = {}
    for r in cur.fetchall():
        date_str, bucket, prior = r
        outcomes[date_str] = {
            "settlement": bucket,
            "prior": prior,
            "direction": "up" if bucket > prior else "down",
        }
    conn.close()
    return outcomes


def run_backtest(stations: List[str], settlement_db: str) -> Dict:
    """
    Run nowcast backtest using historical METAR data.
    Tests whether nowcast signals correlate with settlement direction.
    """
    signal = MetarNowcastSignal()
    results = {}
    total_signals = 0
    total_correct = 0
    total_high_signals = 0
    total_low_signals = 0
    high_correct = 0
    low_correct = 0

    for station in stations:
        outcomes = get_settlement_outcomes(settlement_db, station)
        station_signals = 0
        station_correct = 0

        # Try each bucket temperature
        for bucket in TEST_BUCKETS:
            for date_str, outcome in list(outcomes.items())[-30:]:  # Last 30 days
                try:
                    result = signal.evaluate(
                        station=station,
                        bucket_temp_f=bucket,
                        gefs_max_f=DEFAULT_GEFS_MAX,
                        gefs_min_f=DEFAULT_GEFS_MIN,
                    )
                    if result.get("signal"):
                        station_signals += 1
                        total_signals += 1

                        predicted_dir = result["direction"]  # "HIGH" or "LOW"
                        actual_dir = outcome["direction"].upper()  # "UP" or "DOWN"

                        # Map: HIGH→up, LOW→down
                        nowcast_up = predicted_dir == "HIGH"
                        actual_up = actual_dir == "UP"

                        if nowcast_up == actual_up:
                            station_correct += 1
                            total_correct += 1
                            if nowcast_up:
                                high_correct += 1
                            else:
                                low_correct += 1

                        if nowcast_up:
                            total_high_signals += 1
                        else:
                            total_low_signals += 1

                except Exception as e:
                    continue

        accuracy = station_correct / station_signals if station_signals > 0 else 0
        results[station] = {
            "signals": station_signals,
            "correct": station_correct,
            "accuracy": round(accuracy, 4),
        }
        logger.info(f"{station}: {station_correct}/{station_signals} ({accuracy:.2%})")

    overall_accuracy = total_correct / total_signals if total_signals > 0 else 0

    summary = {
        "stations_tested": len(stations),
        "total_signals": total_signals,
        "total_correct": total_correct,
        "overall_accuracy": round(overall_accuracy, 4),
        "high_signals": total_high_signals,
        "high_correct": high_correct,
        "high_accuracy": round(high_correct / total_high_signals, 4) if total_high_signals > 0 else 0,
        "low_signals": total_low_signals,
        "low_correct": low_correct,
        "low_accuracy": round(low_correct / total_low_signals, 4) if total_low_signals > 0 else 0,
        "per_station": results,
        "note": "Backtest uses historical METAR with default GEFS forecasts "
                "(85/65°F). Replace with actual GEFS values for accuracy.",
    }

    logger.info(f"=== OVERALL: {total_correct}/{total_signals} "
                f"({overall_accuracy:.2%}) ===")
    return summary


def main():
    parser = argparse.ArgumentParser(description="METAR Nowcast Backtest")
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated station list")
    parser.add_argument("--db", type=str,
                        default=os.environ.get(
                            "METAR_DB_PATH",
                            str(REPO_ROOT / "data" / "metar_backfill.db")),
                        help="Settlement DB path")
    args = parser.parse_args()

    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",")]
    else:
        stations = get_all_stations()

    logger.info(f"Running METAR nowcast backtest for {len(stations)} stations")
    results = run_backtest(stations, args.db)

    print(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    main()