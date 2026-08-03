#!/usr/bin/env python3
"""
A5 — DTW Scale Test (Gating Item)

Full-scale evaluation: all 20 stations, 365 days, walk-forward validation.
All A1-A4 improvements applied:
  - A1: Directional accuracy metric (up/down for HIGH/LOW)
  - A2: Z-score normalization (per-station feature means/std)
  - A3: Per-station matching (same-station only, no climate-zone pooling)
  - A4: Temporal leakage fix (≥5 day gap from query window)

Stop conditions:
  - Directional accuracy <45% → KILL DTW lane
  - Directional accuracy ≥55% → ADVANCE to Phase B

Usage:
    python3 scripts/a5_dtw_scaletest.py [--days 365] [--start 2025-08-03]

Output:
    docs/weather-engine/backtests/a5_dtw_scaletest.json
    stdout summary

Author: Gilfoyle (Phase A, Aug 3 2026)
"""

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 20 stations
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

FEATURE_NAMES = [
    "temp_f", "dewpoint_f", "pressure_mb", "wind_speed_kt",
    "wind_dir_sin", "wind_dir_cos",
]

FEATURE_WEIGHTS = {
    "temp_f": 0.25,
    "dewpoint_f": 0.15,
    "pressure_mb": 0.20,
    "wind_speed_kt": 0.15,
    "wind_dir_sin": 0.125,
    "wind_dir_cos": 0.125,
}
FEATURE_WEIGHTS_ARRAY = np.array([FEATURE_WEIGHTS[f] for f in FEATURE_NAMES])

PHYSICAL_BOUNDS = {
    "temp_f": (-50.0, 130.0),
    "dewpoint_f": (-50.0, 130.0),
    "pressure_mb": (800.0, 1100.0),
    "wind_speed_kt": (0.0, 150.0),
}

# DTW parameters
SEQUENCE_LENGTH = 5       # 5-day sequences
DTW_RADIUS = 5            # Sakoe-Chiba band
MIN_SEPARATION_DAYS = 5   # Temporal leakage fix: no overlapping windows
MAX_ANALOGS_PER_QUERY = 200
MIN_ANALOGS_FOR_QUERY = 3


def load_normalization_stats(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def load_metar_station(db_path: str, station: str) -> dict:
    """Load daily METAR for one station with physical bounds."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    data = {}
    cur.execute("""
        SELECT date_utc, MAX(temp_f), AVG(dewpoint_f), AVG(pressure_mb),
               AVG(wind_speed_kt), AVG(wind_direction_deg)
        FROM metar_observations
        WHERE station = ?
          AND temp_f IS NOT NULL AND dewpoint_f IS NOT NULL
          AND pressure_mb IS NOT NULL AND wind_speed_kt IS NOT NULL
        GROUP BY date_utc
    """, (station,))
    for row in cur.fetchall():
        date_str = row[0]
        vals = {"temp_f": row[1], "dewpoint_f": row[2], "pressure_mb": row[3],
                "wind_speed_kt": row[4], "wind_dir_deg": row[5]}
        ok = True
        for k, (lo, hi) in PHYSICAL_BOUNDS.items():
            if k == "wind_dir_deg":
                continue
            if vals[k] is None or vals[k] < lo or vals[k] > hi:
                ok = False
                break
        if ok:
            wd = vals.pop("wind_dir_deg")
            data[date_str] = vals
            data[date_str]["wind_dir_sin"] = math.sin(math.radians(wd)) if wd is not None else 0.0
            data[date_str]["wind_dir_cos"] = math.cos(math.radians(wd)) if wd is not None else 1.0
    conn.close()
    return data


def to_normalized_vector(features: dict, norm_stats: dict, station: str) -> np.ndarray:
    """Convert feature dict to z-score normalized array."""
    vec = np.zeros(len(FEATURE_NAMES))
    for i, feat in enumerate(FEATURE_NAMES):
        val = features.get(feat, 0.0)
        s = norm_stats.get(station, {}).get(feat, norm_stats.get("_global", {}).get(feat, {"mean": 0.0, "std": 1.0}))
        mean = s.get("mean", 0.0)
        std = s.get("std", 1.0)
        vec[i] = (val - mean) / std if std > 0.001 else 0.0
    return vec


def compute_dtw(seq1, seq2, radius=DTW_RADIUS):
    """DTW distance with Sakoe-Chiba band on z-score normalized vectors."""
    n, m = len(seq1), len(seq2)
    if n == 0 or m == 0:
        return float('inf')
    dtw = np.full((n + 1, m + 1), float('inf'))
    dtw[0, 0] = 0.0
    for i in range(1, n + 1):
        j_start = max(1, i - radius)
        j_end = min(m, i + radius)
        for j in range(j_start, j_end + 1):
            diff = seq1[i - 1] - seq2[j - 1]
            cost = math.sqrt(np.dot(diff * FEATURE_WEIGHTS_ARRAY, diff))
            dtw[i, j] = cost + min(dtw[i-1, j], dtw[i, j-1], dtw[i-1, j-1])
    return dtw[n, m] / min(n, m)


def build_candidates(station_data, norm_stats, station):
    """Build 5-day normalized sequences for a station."""
    dates = sorted(station_data.keys())
    seqs = []
    for i in range(len(dates) - SEQUENCE_LENGTH + 1):
        end_date = dates[i + SEQUENCE_LENGTH - 1]
        seq = []
        valid = True
        for j in range(SEQUENCE_LENGTH):
            d = dates[i + j]
            features = station_data[d]
            vec = to_normalized_vector(features, norm_stats, station)
            seq.append(vec)
        seqs.append((end_date, seq))
    return seqs


def query_dtw(query_date, query_vecs, candidates, min_sep=MIN_SEPARATION_DAYS):
    """Find top N DTW analogs for a query."""
    query_dt = datetime.strptime(query_date, "%Y-%m-%d")
    matches = []
    for cand_date, cand_vecs in candidates:
        cand_dt = datetime.strptime(cand_date, "%Y-%m-%d")
        # Temporal leakage fix
        if (query_dt - cand_dt).days < min_sep:
            continue
        distance = compute_dtw(query_vecs, cand_vecs)
        if distance < float('inf'):
            matches.append((cand_date, distance))
    matches.sort(key=lambda x: x[1])
    return matches[:MAX_ANALOGS_PER_QUERY]


def run_scaletest(metar_db, settlements_db, norm_stats_path, start_date, days):
    """Run full A5 scale test across all stations."""
    norm_stats = load_normalization_stats(norm_stats_path)

    # Load settlements
    conn = sqlite3.connect(settlements_db)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    all_settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None and s in STATIONS:
            all_settlements[s][d] = float(t)
    conn.close()

    # Determine query period
    end_dt = datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=days)
    end_date = end_dt.strftime("%Y-%m-%d")

    print(f"\n  Period: {start_date} → {end_date} ({days} days)")
    print(f"  Stations: {len(STATIONS)}")
    print(f"  Features: {len(FEATURE_NAMES)} (z-score normalized)")
    print(f"  Sequence: {SEQUENCE_LENGTH}-day, DTW radius={DTW_RADIUS}")
    print(f"  Temporal gap: {MIN_SEPARATION_DAYS}d (leakage fix)")

    per_station_results = {}
    total_queries = 0
    total_directional = 0
    total_directional_correct = 0

    t0 = time.time()

    for idx, station in enumerate(STATIONS):
        st = time.time()
        print(f"\n  [{idx+1}/{len(STATIONS)}] {station}:", end=" ", flush=True)

        # Load METAR data
        station_data = load_metar_station(metar_db, station)
        if len(station_data) < MIN_SEPARATION_DAYS + SEQUENCE_LENGTH + 2:
            print(f"SKIP (only {len(station_data)} days)")
            continue

        # Build candidate sequences
        candidates = build_candidates(station_data, norm_stats, station)
        print(f"{len(candidates)} candidates |", end=" ", flush=True)

        # Get settlement dates for query period
        station_settlements = all_settlements.get(station, {})
        query_dates = sorted([
            d for d in station_settlements.keys()
            if start_date <= d <= end_date
        ])

        if len(query_dates) < 3:
            print("SKIP (no settlements)")
            continue

        print(f"{len(query_dates)} queries", end=" ", flush=True)

        # Build date->prev temp lookup
        all_dates = sorted(station_settlements.keys())
        date_to_prev = {}
        for i, d in enumerate(all_dates):
            if i > 0:
                date_to_prev[d] = station_settlements[all_dates[i - 1]]

        station_query_results = []
        station_dir_correct = 0
        station_dir_total = 0

        for query_date in query_dates:
            # Build query 5-day sequence
            query_dt = datetime.strptime(query_date, "%Y-%m-%d")
            query_vecs = []
            valid = True
            for offset in range(SEQUENCE_LENGTH - 1, -1, -1):
                d = (query_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
                features = station_data.get(d)
                if features is None:
                    valid = False
                    break
                query_vecs.append(to_normalized_vector(features, norm_stats, station))
            if not valid or len(query_vecs) != SEQUENCE_LENGTH:
                continue

            # Find top DTW analogs
            top_matches = query_dtw(query_date, query_vecs, candidates)

            if len(top_matches) < MIN_ANALOGS_FOR_QUERY:
                continue

            # Get analog settlement temperatures
            analog_temps = []
            for cand_date, _ in top_matches:
                t = station_settlements.get(cand_date)
                if t is not None:
                    analog_temps.append(t)

            if len(analog_temps) < MIN_ANALOGS_FOR_QUERY:
                continue

            # Actual temp and direction
            actual_temp = station_settlements[query_date]
            prev_temp = date_to_prev.get(query_date)
            if prev_temp is None:
                continue

            # Directional prediction: median of analog temps vs prev
            median_analog = np.median(analog_temps)
            
            # Count analogs predicting UP, DOWN, FLAT
            up_ct = sum(1 for t in analog_temps if t > prev_temp + 0.5)
            dn_ct = sum(1 for t in analog_temps if t < prev_temp - 0.5)
            fl_ct = len(analog_temps) - up_ct - dn_ct

            if up_ct >= dn_ct and up_ct >= fl_ct:
                pred_dir = "UP"
            elif dn_ct >= up_ct and dn_ct >= fl_ct:
                pred_dir = "DOWN"
            else:
                pred_dir = "FLAT"

            actual_diff = actual_temp - prev_temp
            if actual_diff > 0.5:
                actual_dir = "UP"
            elif actual_diff < -0.5:
                actual_dir = "DOWN"
            else:
                actual_dir = "FLAT"

            correct = pred_dir == actual_dir
            if actual_dir != "FLAT":
                station_dir_total += 1
                if correct:
                    station_dir_correct += 1

            station_query_results.append({
                "date": query_date,
                "actual_temp": actual_temp,
                "prev_temp": prev_temp,
                "actual_dir": actual_dir,
                "pred_dir": pred_dir,
                "correct": correct,
                "n_analogs": len(analog_temps),
                "median_analog_temp": round(float(median_analog), 1),
                "up_analogs": up_ct,
                "down_analogs": dn_ct,
                "flat_analogs": fl_ct,
                "top_distances": [round(d, 3) for _, d in top_matches[:5]],
            })

        station_acc = station_dir_correct / station_dir_total if station_dir_total > 0 else 0.0
        per_station_results[station] = {
            "n_queries": len(station_query_results),
            "directional_n": station_dir_total,
            "directional_correct": station_dir_correct,
            "directional_accuracy": round(station_acc, 4),
            "candidates": len(candidates),
            "time_sec": round(time.time() - st, 1),
        }
        total_queries += len(station_query_results)
        total_directional += station_dir_total
        total_directional_correct += station_dir_correct

        print(f" | acc={station_acc*100:.1f}% ({station_dir_correct}/{station_dir_total}) | "
              f"{time.time()-st:.1f}s")

    dt = time.time() - t0
    overall_acc = total_directional_correct / total_directional if total_directional > 0 else 0.0

    # Agresti-Coull CI
    z = 1.96
    n_tilde = total_directional + z**2
    p_tilde = (total_directional_correct + z**2 / 2) / n_tilde
    se = math.sqrt(p_tilde * (1 - p_tilde) / n_tilde)

    print(f"\n{'=' * 72}")
    print(f"  A5 — DTW SCALE TEST RESULTS")
    print(f"{'=' * 72}")
    print(f"  Period: {start_date} → {end_date} ({days} days)")
    print(f"  Stations: {len(STATIONS)}")
    print(f"  Total queries: {total_queries}")
    print(f"  Directional queries: {total_directional}")
    print(f"  Directional accuracy: {overall_acc*100:.2f}%")
    print(f"    95% CI: [{p_tilde - z*se:.4f}, {p_tilde + z*se:.4f}]")
    print(f"  Correct: {total_directional_correct}/{total_directional}")
    print(f"  Total time: {dt:.1f}s ({dt/60:.1f}m)")
    print()

    # Stop condition
    if overall_acc < 0.45:
        print(f"  ❌ ACCURACY {overall_acc*100:.1f}% < 45% THRESHOLD")
        print(f"  → KILL DTW lane")
        verdict = "KILL"
    elif overall_acc >= 0.55:
        print(f"  ✅ ACCURACY {overall_acc*100:.1f}% ≥ 55% THRESHOLD")
        print(f"  → ADVANCE to Phase B")
        verdict = "ADVANCE"
    else:
        print(f"  ⚠ ACCURACY {overall_acc*100:.1f}% BETWEEN THRESHOLDS")
        print(f"  → Needs further evaluation")
        verdict = "NEEDS_EVALUATION"

    print()
    print(f"  Per-station:")
    print(f"  {'Station':8s}  {'Queries':>8s}  {'Dir N':>6s}  {'Correct':>7s}  {'Accuracy':>9s}  {'Candidates':>10s}  {'Time':>6s}")
    print("  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 6 + "  " + "-" * 7 + "  " + "-" * 9 + "  " + "-" * 10 + "  " + "-" * 6)
    for s in sorted(per_station_results.keys()):
        r = per_station_results[s]
        acc = r["directional_accuracy"] * 100
        print(f"  {s:8s}  {r['n_queries']:>8d}  {r['directional_n']:>6d}  "
              f"{r['directional_correct']:>7d}  {acc:>7.1f}%  "
              f"{r['candidates']:>10d}  {r['time_sec']:>5.1f}s")

    # Save
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "period": {"start": start_date, "end": end_date, "days": days},
            "stations": STATIONS,
            "sequence_length": SEQUENCE_LENGTH,
            "dtw_radius": DTW_RADIUS,
            "min_separation_days": MIN_SEPARATION_DAYS,
            "features": FEATURE_NAMES,
            "feature_weights": FEATURE_WEIGHTS,
            "applied_fixes": [
                "A1: directional accuracy (up/down for HIGH/LOW)",
                "A2: z-score normalization (per-station)",
                "A3: same-station matching (no climate-zone pooling)",
                "A4: temporal leakage fix (>=5 day gap)",
                "wind_dir: sin/cos encoding (circular fix)",
                "data_quality: physical bounds filter",
            ],
        },
        "overall": {
            "total_queries": total_queries,
            "directional_n": total_directional,
            "directional_correct": total_directional_correct,
            "directional_accuracy": round(overall_acc, 4),
            "ci_95": [round(p_tilde - z*se, 4), round(p_tilde + z*se, 4)],
            "verdict": verdict,
            "runtime_sec": round(dt, 1),
        },
        "per_station": per_station_results,
    }

    output_path = OUTPUT_DIR / "a5_dtw_scaletest.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Full results saved to: {output_path}")
    print("  Done.\n")

    return output


def main():
    parser = argparse.ArgumentParser(description="A5 — DTW Scale Test")
    parser.add_argument("--days", type=int, default=365,
                        help="Days to test (default: 365)")
    parser.add_argument("--start", type=str, default=None,
                        help="Start date YYYY-MM-DD (default: 365 days ago)")
    parser.add_argument("--metar-db", type=str,
                        default=str(REPO_ROOT / "data" / "metar_backfill.db"))
    parser.add_argument("--settlements", type=str,
                        default=str(REPO_ROOT / "data" / "kalshi_settlements.db"))
    parser.add_argument("--norm-stats", type=str,
                        default=str(REPO_ROOT / "data" / "dtw_normalization_stats.json"))
    args = parser.parse_args()

    if args.start is None:
        args.start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print("=" * 72)
    print("  A5 — DTW SCALE TEST (Gating Item)")
    print("=" * 72)
    print()
    print("  Applied fixes:")
    print("    A1: Directional accuracy metric")
    print("    A2: Z-score normalization (per-station)")
    print("    A3: Per-station matching (no climate-zone pooling)")
    print("    A4: Temporal leakage fix")
    print("    Wind: sin/cos encoding (circular fix)")
    print("    Data: Physical bounds filter")

    run_scaletest(args.metar_db, args.settlements, args.norm_stats, args.start, args.days)


if __name__ == "__main__":
    main()