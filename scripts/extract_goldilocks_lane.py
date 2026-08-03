#!/usr/bin/env python3
"""
Goldilocks Lane Extraction — Backtest the instant_cross_revert detection.

Extracts the temperature event detection from core/metar_monitor.py into a
standalone module (core/lane2_goldilocks.py), implementing the corrected
"instant_cross_revert" detection.

The Goldilocks edge: temperature crosses a Kalshi bucket boundary but does NOT
set a new daily max. The spike is transient — the market settles on daily max,
so the settlement will be below the boundary.

Target: precision > 0.70, recall > 0.50

Usage:
    python3 scripts/extract_goldilocks_lane.py [--days 365] [--stations 20]

Output:
    docs/weather-engine/backtests/goldilocks_backtest_20260803.json

Stop condition:
    If precision < 0.70 or recall < 0.50, PARK and report to Donna.

Author: Gilfoyle (dispatch Aug 3, 2026, B-mode post-Gray-Room)
"""

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ─── Paths ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Import the Goldilocks lane module
sys.path.insert(0, str(REPO_ROOT / "core"))
from lane2_goldilocks import (
    DailyState,
    MetarObservation,
    compute_trend_extrapolation,
    detect_instant_cross_revert,
    evaluate_goldilocks_backtest,
    TRANSIENT_DELTA_THRESHOLD,
    EXCEEDED_THRESHOLD,
)

# 20 canonical stations
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]


# ─── Data Loading ────────────────────────────────────────────────────────────

def load_metar_observations(station: str, start_date: str, end_date: str) -> List[dict]:
    """Load METAR observations for a station in date range."""
    conn = sqlite3.connect(METAR_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp_utc, temp_f, dewpoint_f, wind_speed_kt, pressure_mb
        FROM metar_observations
        WHERE station=? AND date_utc >= ? AND date_utc <= ?
          AND temp_f IS NOT NULL
        ORDER BY timestamp_utc ASC
    """, (station, start_date, end_date))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "timestamp_utc": r[0],
            "temp_f": r[1],
            "dewpoint_f": r[2],
            "wind_speed_kt": r[3],
            "pressure_mb": r[4],
        }
        for r in rows
    ]


def load_kalshi_settlements(station: str) -> Dict[str, float]:
    """Load Kalshi settlement data for a station."""
    conn = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT target_date, kalshi_temp
        FROM kalshi_settlements
        WHERE station=? AND kalshi_temp IS NOT NULL
        ORDER BY target_date
    """, (station,))
    results = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return results


# ─── Backtest Runner ─────────────────────────────────────────────────────────

def run_goldilocks_backtest(
    start_date: str,
    days: int,
    bucket_boundaries: List[int] = None,
) -> dict:
    """
    Run the Goldilocks backtest against historical METAR + settlement data.

    For each station-day:
    1. Load METAR observations for the day
    2. Run instant_cross_revert detection on each observation
    3. Run trend extrapolation on each observation
    4. Compare predictions to actual daily max from settlement data
    5. Compute precision and recall
    """
    if bucket_boundaries is None:
        bucket_boundaries = [70, 75, 80, 85, 90, 95, 100]

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = start_dt + timedelta(days=days)
    end_str = end_dt.strftime("%Y-%m-%d")

    print(f"  Stations: {len(STATIONS)}")
    print(f"  Period: {start_date} -> {end_str} ({days} days)")
    print(f"  Bucket boundaries: {bucket_boundaries}")
    print(f"  Transient threshold: {TRANSIENT_DELTA_THRESHOLD}°F")
    print(f"  Exceeded threshold: {EXCEEDED_THRESHOLD}°F")
    print()

    all_results = {
        "instant_cross_revert": {"tp": 0, "fp": 0, "signals": 0},
        "trend_extrapolation": {"tp": 0, "fp": 0, "predictions": 0},
    }
    per_station_stats = {}
    per_boundary_stats = defaultdict(lambda: {"tp": 0, "fp": 0, "signals": 0, "predictions": 0})
    detailed_predictions = []

    for station in STATIONS:
        print(f"  [{station}] Loading data...", end=" ", flush=True)
        observations = load_metar_observations(station, start_date, end_str)
        settlements = load_kalshi_settlements(station)

        if not observations:
            print(f"NO METAR DATA")
            per_station_stats[station] = {"status": "NO_METAR_DATA", "n_obs": 0}
            continue

        print(f"{len(observations)} obs, {len(settlements)} settlements")

        # Group observations by date
        obs_by_date = defaultdict(list)
        for obs in observations:
            try:
                dt = datetime.fromisoformat(obs["timestamp_utc"].replace("Z", "+00:00"))
                date_key = dt.strftime("%Y-%m-%d")
                obs_by_date[date_key].append((dt, obs))
            except Exception:
                continue

        station_predictions = []
        station_tp = {"instant_cross_revert": 0, "trend_extrapolation": 0}
        station_fp = {"instant_cross_revert": 0, "trend_extrapolation": 0}
        station_signals = {"instant_cross_revert": 0, "trend_extrapolation": 0}

        # Process each date
        for date_key in sorted(obs_by_date.keys()):
            if date_key not in settlements:
                continue

            # Check if this date has a HIGH market settlement
            actual_daily_max = settlements[date_key]
            if actual_daily_max is None:
                continue

            day_obs = obs_by_date[date_key]
            if len(day_obs) < 3:
                continue

            # Create daily state
            daily_state = DailyState(station, date_key)

            # Process each observation in sequence
            # IMPORTANT: add_observation() is called BEFORE detection, so
            # detect_instant_cross_revert reads the LAST observation as the
            # current one and snapshots the running max from BEFORE it.
            for i, (dt, obs) in enumerate(day_obs):
                metar_obs = MetarObservation(
                    timestamp=dt,
                    temp_f=obs["temp_f"],
                    dewpoint_f=obs.get("dewpoint_f"),
                    wind_speed_kt=obs.get("wind_speed_kt"),
                    pressure_mb=obs.get("pressure_mb"),
                )

                # MUST add observation before detection (detection reads
                # the last observation and snapshots running_max from before it)
                daily_state.add_observation(metar_obs)

                if i < 1:
                    continue  # Need at least 2 observations for crossing detection

                # Determine the relevant boundary for this observation:
                # the next integer above the running max (before this obs)
                # This is the boundary that matters for the HIGH market.
                prev_obs = daily_state.observations[-2]
                running_max_before = max(o.temp_f for o in daily_state.observations[:-1])
                target_boundary = int(math.floor(running_max_before)) + 1

                # Only check the target boundary and one above/below
                check_boundaries = [target_boundary - 1, target_boundary, target_boundary + 1]
                check_boundaries = [b for b in check_boundaries if b > 0]

                for boundary in check_boundaries:
                    # Detect instant_cross_revert
                    signal = detect_instant_cross_revert(
                        daily_state, metar_obs, boundary
                    )

                    if signal:
                        station_predictions.append(signal)
                        detailed_predictions.append(signal)
                        station_signals["instant_cross_revert"] += 1

                    # Compute trend extrapolation
                    trend = compute_trend_extrapolation(
                        daily_state, metar_obs, boundary
                    )

                    if trend:
                        station_predictions.append(trend)
                        station_signals["trend_extrapolation"] += 1

            # Evaluate predictions for this date
            eval_result = evaluate_goldilocks_backtest(
                station_predictions, actual_daily_max, 0
            )

            # Since evaluate_goldilocks_backtest checks against bucket_boundary,
            # we need to evaluate per boundary
            for boundary in bucket_boundaries:
                boundary_eval = evaluate_goldilocks_backtest(
                    [p for p in station_predictions
                     if p.get("bucket_boundary") == boundary or
                        p.get("signal_type") == "instant_cross_revert"],
                    actual_daily_max, boundary
                )

                icr = boundary_eval["instant_cross_revert"]
                per_boundary_stats[boundary]["tp"] += icr["true_positives"]
                per_boundary_stats[boundary]["fp"] += icr["false_positives"]
                per_boundary_stats[boundary]["signals"] += icr["signals"]

        # Aggregate station stats
        station_eval = evaluate_goldilocks_backtest(
            station_predictions, 0, 0
        )  # Placeholder — will be evaluated per boundary

        # Count per-station
        for sig in station_predictions:
            if sig.get("signal_type") == "instant_cross_revert":
                all_results["instant_cross_revert"]["signals"] += 1
            elif sig.get("signal_type") == "trend_extrapolation":
                all_results["trend_extrapolation"]["predictions"] += 1

        per_station_stats[station] = {
            "n_observations": len(observations),
            "n_dates": len(obs_by_date),
            "n_predictions": len(station_predictions),
            "n_instant_cross_revert": sum(1 for p in station_predictions
                                          if p.get("signal_type") == "instant_cross_revert"),
            "n_trend_extrapolation": sum(1 for p in station_predictions
                                          if p.get("signal_type") == "trend_extrapolation"),
        }

    # Compute final metrics
    def compute_metrics(result_dict: dict) -> dict:
        tp = result_dict.get("tp", 0)
        fp = result_dict.get("fp", 0)
        total = result_dict.get("signals", 0) + result_dict.get("predictions", 0)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / total if total > 0 else 0.0
        return {
            "true_positives": tp,
            "false_positives": fp,
            "total_signals": total,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
        }

    # We need to do proper evaluation. Let me re-run the evaluation properly.
    # Actually, the evaluate_goldilocks_backtest function needs to be called
    # for each date with the correct daily max. Let me refactor to do it properly.

    # ─── Proper Evaluation ───
    # For each prediction, we check if it was correct given the actual daily max.
    total_icr_tp = 0
    total_icr_fp = 0
    total_icr_signals = 0
    total_trend_tp = 0
    total_trend_fp = 0
    total_trend_preds = 0

    for pred in detailed_predictions:
        sig_type = pred.get("signal_type", "")
        station = pred.get("station", "")
        date_key = pred.get("local_date", "")
        boundary = pred.get("bucket_boundary", 0)

        actual_daily_max = load_kalshi_settlements(station).get(date_key)

        if actual_daily_max is None:
            continue

        if sig_type == "instant_cross_revert":
            total_icr_signals += 1
            # Signal predicts: daily max will be BELOW the bucket boundary
            if actual_daily_max < boundary:
                total_icr_tp += 1
            else:
                total_icr_fp += 1
        elif sig_type == "trend_extrapolation":
            total_trend_preds += 1
            direction = pred.get("direction", "")
            if pred.get("prediction_hit"):
                if direction == "up":
                    if actual_daily_max >= boundary:
                        total_trend_tp += 1
                    else:
                        total_trend_fp += 1

    icr_precision = total_icr_tp / (total_icr_tp + total_icr_fp) if (total_icr_tp + total_icr_fp) > 0 else 0.0
    icr_recall = total_icr_tp / total_icr_signals if total_icr_signals > 0 else 0.0
    trend_precision = total_trend_tp / (total_trend_tp + total_trend_fp) if (total_trend_tp + total_trend_fp) > 0 else 0.0
    trend_recall = total_trend_tp / total_trend_preds if total_trend_preds > 0 else 0.0

    print()
    print("  " + "=" * 50)
    print("  RESULTS")
    print("  " + "=" * 50)
    print(f"  Instant Cross Revert:")
    print(f"    Signals: {total_icr_signals}")
    print(f"    TP: {total_icr_tp}  FP: {total_icr_fp}")
    print(f"    Precision: {icr_precision*100:.2f}%")
    print(f"    Recall: {icr_recall*100:.2f}%")
    print()
    print(f"  Trend Extrapolation:")
    print(f"    Predictions: {total_trend_preds}")
    print(f"    TP: {total_trend_tp}  FP: {total_trend_fp}")
    print(f"    Precision: {trend_precision*100:.2f}%")
    print(f"    Recall: {trend_recall*100:.2f}%")

    # Check stop conditions
    print()
    stop_conditions = []
    if icr_precision < 0.70:
        stop_conditions.append(f"Instant Cross Revert precision {icr_precision*100:.2f}% < 70%")
    if icr_recall < 0.50:
        stop_conditions.append(f"Instant Cross Revert recall {icr_recall*100:.2f}% < 50%")

    if stop_conditions:
        print("  ⚠️ STOP CONDITIONS HIT:")
        for sc in stop_conditions:
            print(f"    - {sc}")
        print("  PARK and report to Donna.")
    else:
        print("  ✅ Goldilocks lane meets precision > 0.70 and recall > 0.50 targets")

    # Build output
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "stations": STATIONS,
            "period": {"start": start_date, "end": end_str, "days": days},
            "bucket_boundaries": bucket_boundaries,
            "transient_delta_threshold": TRANSIENT_DELTA_THRESHOLD,
            "exceeded_threshold": EXCEEDED_THRESHOLD,
            "momentum_window": 3,
        },
        "results": {
            "instant_cross_revert": {
                "signals": total_icr_signals,
                "true_positives": total_icr_tp,
                "false_positives": total_icr_fp,
                "precision": round(icr_precision, 4),
                "recall": round(icr_recall, 4),
                "meets_target": icr_precision >= 0.70 and icr_recall >= 0.50,
            },
            "trend_extrapolation": {
                "predictions": total_trend_preds,
                "true_positives": total_trend_tp,
                "false_positives": total_trend_fp,
                "precision": round(trend_precision, 4),
                "recall": round(trend_recall, 4),
            },
        },
        "stop_conditions": stop_conditions if stop_conditions else None,
        "per_station": per_station_stats,
        "viable": len(stop_conditions) == 0,
    }

    output_path = OUTPUT_DIR / "goldilocks_backtest_20260803.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print("  Done.\n")


def main():
    parser = argparse.ArgumentParser(description="Goldilocks Lane Extraction Backtest")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of backtest data (default: 90)")
    parser.add_argument("--start", type=str, default=None,
                        help="Start date (default: 90 days ago)")
    args = parser.parse_args()

    if args.start is None:
        args.start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print("=" * 72)
    print("  GOLDILOCKS LANE — Instant Cross Revert Backtest")
    print("=" * 72)

    run_goldilocks_backtest(args.start, args.days)


if __name__ == "__main__":
    main()