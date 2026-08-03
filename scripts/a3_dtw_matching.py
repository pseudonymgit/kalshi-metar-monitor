#!/usr/bin/env python3
"""
A3 — DTW Per-Station Matching

Replaces climate-zone pooling with per-station or similarity-based matching.
The current implementation uses soft station_boost (2.0x same-station, 1.0x same-zone,
0.5x cross-zone) which is effectively a no-op — 98.8% of matches are same-station.

This script implements three matching strategies:
  1. Same-station only: candidates only from the query station's history
  2. Similarity-based: candidates from top-K most similar stations (by climate vector)
  3. Geographic proximity: candidates from N nearest stations by lat/lon

Each strategy is evaluated separately.

Usage:
    python3 scripts/a3_dtw_matching.py [--metar-db data/metar_backfill.db]

Output:
    docs/weather-engine/backtests/a3_station_matching.json
    stdout summary

Author: Gilfoyle (Phase A, Aug 3 2026)
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

# Station coordinates (for geographic proximity matching)
STATION_COORDS = {
    "KATL": (33.6367, -84.4281),
    "KAUS": (30.1974, -97.6664),
    "KBOS": (42.3656, -71.0096),
    "KDCA": (38.8521, -77.0377),
    "KDEN": (39.8561, -104.6737),
    "KDFW": (32.8959, -97.0372),
    "KHOU": (29.9844, -95.3414),
    "KLAS": (36.0801, -115.1522),
    "KLAX": (33.9425, -118.4081),
    "KMDW": (41.7860, -87.7525),
    "KMIA": (25.7932, -80.2906),
    "KMSP": (44.8819, -93.2217),
    "KMSY": (29.9902, -90.2580),
    "KNYC": (40.7407, -73.9809),
    "KOKC": (35.3931, -97.6007),
    "KPHL": (39.8722, -75.2411),
    "KPHX": (33.4484, -112.0701),
    "KSAT": (29.5337, -98.4667),
    "KSEA": (47.4490, -122.3082),
    "KSFO": (37.6190, -122.3748),
}

# Feature weights for DTW distance (expanded to 6 features with sin/cos wind)
FEATURE_WEIGHTS = {
    "temp_f": 0.25,
    "dewpoint_f": 0.15,
    "pressure_mb": 0.20,
    "wind_speed_kt": 0.15,
    "wind_dir_sin": 0.125,
    "wind_dir_cos": 0.125,
}

# Physical bounds for data quality
TEMP_MIN_F = -50.0
TEMP_MAX_F = 130.0
PRESSURE_MIN_MB = 800.0
PRESSURE_MAX_MB = 1100.0
WIND_MAX_KT = 150.0

# Season definitions (for similarity-based matching)
SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "fall", 10: "fall", 11: "fall",
}


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def load_normalization_stats(stats_path: str) -> dict:
    """Load pre-computed z-score statistics from A2 output."""
    with open(stats_path) as f:
        return json.load(f)


def load_metar_corpus(metar_db: str, station: str) -> Dict[str, dict]:
    """
    Load daily METAR data for a single station with physical bounds check.
    Returns {date_str: {feature: value}}
    """
    conn = sqlite3.connect(metar_db)
    cur = conn.cursor()
    
    data = {}
    cur.execute("""
        SELECT date_utc,
               MAX(temp_f) as max_temp,
               AVG(dewpoint_f) as avg_dewpoint,
               AVG(pressure_mb) as avg_pressure,
               AVG(wind_speed_kt) as avg_wind,
               AVG(wind_direction_deg) as avg_wind_dir
        FROM metar_observations
        WHERE station = ?
          AND temp_f IS NOT NULL
          AND dewpoint_f IS NOT NULL
          AND pressure_mb IS NOT NULL
          AND wind_speed_kt IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc
    """, (station,))
    
    for row in cur.fetchall():
        date_str = row[0]
        max_temp = row[1]
        dewpoint = row[2]
        pressure = row[3]
        wind_spd = row[4]
        wind_dir = row[5]
        
        # Physical bounds
        if max_temp is not None and (max_temp < TEMP_MIN_F or max_temp > TEMP_MAX_F):
            continue
        if dewpoint is not None and (dewpoint < TEMP_MIN_F or dewpoint > TEMP_MAX_F):
            continue
        if pressure is not None and (pressure < PRESSURE_MIN_MB or pressure > PRESSURE_MAX_MB):
            continue
        if wind_spd is not None and (wind_spd < 0 or wind_spd > WIND_MAX_KT):
            continue
        
        # Sin/cos wind encoding
        wind_sin = 0.0
        wind_cos = 1.0
        if wind_dir is not None:
            wind_sin = math.sin(math.radians(wind_dir))
            wind_cos = math.cos(math.radians(wind_dir))
        
        data[date_str] = {
            "temp_f": max_temp,
            "dewpoint_f": dewpoint,
            "pressure_mb": pressure,
            "wind_speed_kt": wind_spd,
            "wind_dir_sin": wind_sin,
            "wind_dir_cos": wind_cos,
        }
    
    conn.close()
    return data


def build_climate_vector(station_data: Dict[str, dict]) -> np.ndarray:
    """Build a climate vector for similarity matching (mean of all features)."""
    if not station_data:
        return np.array([])
    
    features = ["temp_f", "dewpoint_f", "pressure_mb", "wind_speed_kt"]
    vector = []
    for feat in features:
        vals = [d[feat] for d in station_data.values() if feat in d]
        if vals:
            vector.append(np.mean(vals))
        else:
            vector.append(0.0)
    return np.array(vector)


def compute_station_similarity(data: Dict[str, Dict[str, dict]]) -> Dict[str, Dict[str, float]]:
    """
    Compute pairwise station similarity based on climate vector cosine similarity.
    Returns {station: {other_station: similarity_score}}
    """
    vectors = {}
    for station in STATIONS:
        v = build_climate_vector(data.get(station, {}))
        if len(v) > 0:
            vectors[station] = v
    
    similarities = {}
    for s1 in vectors:
        similarities[s1] = {}
        for s2 in vectors:
            v1 = vectors[s1]
            v2 = vectors[s2]
            cosine_sim = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)) if np.linalg.norm(v1) > 0 and np.linalg.norm(v2) > 0 else 0
            similarities[s1][s2] = round(float(cosine_sim), 4)
    
    return similarities


def print_top_similar(similarities: Dict[str, Dict[str, float]]) -> None:
    """Print top-5 most similar stations for each station."""
    print(f"\n  TOP-5 SIMILAR STATIONS (by climate vector cosine similarity):")
    print(f"  {'Station':8s}  {'1st':8s}  {'2nd':8s}  {'3rd':8s}  {'4th':8s}  {'5th':8s}")
    print("  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8)
    
    for station in sorted(STATIONS):
        sims = sorted(similarities.get(station, {}).items(), key=lambda x: -x[1])
        # Exclude self
        top5 = [(s, sc) for s, sc in sims if s != station][:5]
        print(f"  {station:8s}  ", end="")
        for s, sc in top5:
            print(f"{s:>8s} ", end="")
        print()


def compute_dtw_distance(seq1: List[np.ndarray], seq2: List[np.ndarray], radius: int = 5) -> float:
    """
    DTW distance with Sakoe-Chiba band.
    Works on z-score normalized feature vectors.
    """
    n, m = len(seq1), len(seq2)
    if n == 0 or m == 0:
        return float('inf')
    
    dtw = np.full((n + 1, m + 1), float('inf'))
    dtw[0, 0] = 0.0
    
    for i in range(1, n + 1):
        j_start = max(1, i - radius)
        j_end = min(m, i + radius)
        for j in range(j_start, j_end + 1):
            # Euclidean distance on z-score normalized vectors
            diff = seq1[i - 1] - seq2[j - 1]
            cost = math.sqrt(np.dot(diff * FEATURE_WEIGHTS_ARRAY, diff))
            dtw[i, j] = cost + min(
                dtw[i - 1, j],
                dtw[i, j - 1],
                dtw[i - 1, j - 1]
            )
    
    return dtw[n, m] / min(n, m)


# Pre-compute weight array for vectorized computation
FEATURE_LIST = ["temp_f", "dewpoint_f", "pressure_mb", "wind_speed_kt", "wind_dir_sin", "wind_dir_cos"]
FEATURE_WEIGHTS_ARRAY = np.array([FEATURE_WEIGHTS[f] for f in FEATURE_LIST])


def dict_to_vector(d: dict, norm_stats: dict, station: str) -> np.ndarray:
    """Convert feature dict to z-score normalized numpy array."""
    vec = np.zeros(len(FEATURE_LIST))
    for i, feat in enumerate(FEATURE_LIST):
        val = d.get(feat, 0.0)
        
        # Get normalization stats for this station
        s = norm_stats.get(station, {}).get(feat, {})
        if not s:
            s = norm_stats.get("_global", {}).get(feat, {"mean": 0.0, "std": 1.0})
        
        mean = s.get("mean", 0.0)
        std = s.get("std", 1.0)
        if std < 0.001:
            std = 1.0
        
        vec[i] = (val - mean) / std
    return vec


def run_station_matching(data: Dict[str, Dict[str, dict]], 
                          similarities: dict,
                          norm_stats: dict,
                          settlements_db: str) -> dict:
    """
    Run DTW matching with per-station strategy for a sample of query dates across all stations.
    Evaluates: same-station only vs similarity-based pool vs geographic proximity.
    """
    # Load settlements
    conn = sqlite3.connect(settlements_db)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements_data = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None:
            settlements_data[s][d] = float(t)
    conn.close()
    
    # Build candidate sequences for each station (5-day sequences)
    # Store as {station: [(end_date, [feature_vectors])]}
    candidates = {}
    for station in STATIONS:
        dates = sorted(data.get(station, {}).keys())
        seqs = []
        for i in range(len(dates) - 4):
            end_date = dates[i + 4]
            seq = []
            valid = True
            for j in range(5):
                d = dates[i + j]
                day_data = data[station].get(d)
                if day_data is None:
                    valid = False
                    break
                seq.append(dict_to_vector(day_data, norm_stats, station))
            if valid and len(seq) == 5:
                seqs.append((end_date, seq))
        candidates[station] = seqs
    
    # Pick query dates: last 60 days for first 5 stations
    results = {}
    test_stations = STATIONS[:5]  # Test on 5 stations for quick evaluation
    
    for query_station in test_stations:
        settlements = settlements_data.get(query_station, {})
        query_dates = sorted(settlements.keys())
        query_dates = [d for d in query_dates if d >= "2026-06-01" and d <= "2026-08-01"]
        
        if len(query_dates) < 5:
            continue
        
        # Sample up to 20 evenly spaced dates
        if len(query_dates) > 20:
            step = len(query_dates) // 20
            query_dates = query_dates[::step][:20]
        
        station_results = []
        
        for query_date in query_dates:
            # Build query sequence
            query_dt = datetime.strptime(query_date, "%Y-%m-%d")
            query_seq = []
            valid = True
            for offset in range(4, -1, -1):
                d = (query_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
                day_data = data.get(query_station, {}).get(d)
                if day_data is None:
                    valid = False
                    break
                query_seq.append(dict_to_vector(day_data, norm_stats, query_station))
            if not valid:
                continue
            
            # Strategy 1: Same-station only
            same_station_matches = []
            for cand_date, cand_seq in candidates.get(query_station, []):
                if cand_date >= query_date:
                    continue
                # Ensure no temporal overlap
                cand_dt = datetime.strptime(cand_date, "%Y-%m-%d")
                if cand_dt > query_dt - timedelta(days=5):
                    continue
                distance = compute_dtw_distance(query_seq, cand_seq, radius=3)
                if distance < float('inf'):
                    same_station_matches.append({
                        "station": query_station,
                        "date": cand_date,
                        "distance": round(distance, 4),
                        "is_same_station": True,
                    })
            same_station_matches.sort(key=lambda x: x["distance"])
            top_same = same_station_matches[:200]
            
            # Strategy 2: Similarity-based (top 5 most similar stations)
            sim_ranking = sorted(
                [(s, similarities.get(query_station, {}).get(s, 0)) for s in STATIONS if s != query_station],
                key=lambda x: -x[1]
            )
            top_similar_stations = [s[0] for s in sim_ranking[:5]]
            
            similar_matches = []
            for cand_station in top_similar_stations:
                for cand_date, cand_seq in candidates.get(cand_station, []):
                    if cand_date >= query_date:
                        continue
                    cand_dt = datetime.strptime(cand_date, "%Y-%m-%d")
                    if cand_dt > query_dt - timedelta(days=5):
                        continue
                    distance = compute_dtw_distance(query_seq, cand_seq, radius=3)
                    if distance < float('inf'):
                        similar_matches.append({
                            "station": cand_station,
                            "date": cand_date,
                            "distance": round(distance, 4),
                            "is_same_station": cand_station == query_station,
                        })
            similar_matches.sort(key=lambda x: x["distance"])
            top_similar = similar_matches[:200]
            
            # Strategy 3: Geographic (N nearest by haversine distance)
            geo_distances = [
                (s, haversine(*STATION_COORDS[query_station], *STATION_COORDS[s]))
                for s in STATIONS if s != query_station
            ]
            geo_distances.sort(key=lambda x: x[1])
            top_geo_stations = [s[0] for s in geo_distances[:5]]
            
            geo_matches = []
            for cand_station in top_geo_stations:
                for cand_date, cand_seq in candidates.get(cand_station, []):
                    if cand_date >= query_date:
                        continue
                    cand_dt = datetime.strptime(cand_date, "%Y-%m-%d")
                    if cand_dt > query_dt - timedelta(days=5):
                        continue
                    distance = compute_dtw_distance(query_seq, cand_seq, radius=3)
                    if distance < float('inf'):
                        geo_matches.append({
                            "station": cand_station,
                            "date": cand_date,
                            "distance": round(distance, 4),
                            "is_same_station": cand_station == query_station,
                        })
            geo_matches.sort(key=lambda x: x["distance"])
            top_geo = geo_matches[:200]
            
            # Compute mode bucket for each strategy
            def get_mode_bucket(matches):
                bucket_counts = defaultdict(int)
                for m in matches:
                    outcome = settlements_data.get(m["station"], {}).get(m["date"])
                    if outcome is not None:
                        if outcome >= 100:
                            bucket = "100+"
                        elif outcome >= 95:
                            bucket = "95-99"
                        elif outcome >= 90:
                            bucket = "90-94"
                        elif outcome >= 85:
                            bucket = "85-89"
                        elif outcome >= 80:
                            bucket = "80-84"
                        elif outcome >= 75:
                            bucket = "75-79"
                        elif outcome >= 70:
                            bucket = "70-74"
                        else:
                            bucket = "below_70"
                        bucket_counts[bucket] += 1
                if bucket_counts:
                    return max(bucket_counts, key=lambda k: bucket_counts[k])
                return None
            
            actual_temp = settlements.get(query_date)
            
            station_results.append({
                "date": query_date,
                "actual_temp": actual_temp,
                "same_station": {
                    "n_matches": len(top_same),
                    "mode_bucket": get_mode_bucket(top_same),
                    "top_distance": top_same[0]["distance"] if top_same else None,
                },
                "similarity_based": {
                    "n_matches": len(top_similar),
                    "mode_bucket": get_mode_bucket(top_similar),
                    "similar_stations": top_similar_stations,
                    "top_distance": top_similar[0]["distance"] if top_similar else None,
                },
                "geographic": {
                    "n_matches": len(top_geo),
                    "mode_bucket": get_mode_bucket(top_geo),
                    "nearby_stations": top_geo_stations,
                    "top_distance": top_geo[0]["distance"] if top_geo else None,
                },
            })
        
        results[query_station] = station_results
    
    return results


def evaluate_results(results: dict) -> dict:
    """Evaluate matching strategies: exact bucket agreement rate."""
    strategies = ["same_station", "similarity_based", "geographic"]
    evaluation = {}
    
    for strat in strategies:
        total = 0
        correct = 0
        station_metrics = {}
        
        for station, q_results in results.items():
            s_correct = 0
            s_total = 0
            for q in q_results:
                actual = q.get("actual_temp")
                mode = q.get(strat, {}).get("mode_bucket")
                if actual is not None and mode:
                    bucket_ranges = {
                        "100+": (100, 200), "95-99": (95, 99),
                        "90-94": (90, 94), "85-89": (85, 89),
                        "80-84": (80, 84), "75-79": (75, 79),
                        "70-74": (70, 74), "below_70": (-100, 69),
                    }
                    low, high = bucket_ranges.get(mode, (0, 200))
                    match = low <= actual <= high
                    
                    if match:
                        s_correct += 1
                    s_total += 1
            
            if s_total > 0:
                station_metrics[station] = {
                    "correct": s_correct,
                    "total": s_total,
                    "accuracy": round(s_correct / s_total, 4),
                }
                correct += s_correct
                total += s_total
        
        evaluation[strat] = {
            "total_queries": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total > 0 else 0,
            "accuracy_pct": round(correct / total * 100, 2) if total > 0 else 0,
            "per_station": station_metrics,
        }
    
    return evaluation


def main():
    parser = argparse.ArgumentParser(
        description="A3 — DTW Per-Station Matching"
    )
    parser.add_argument("--metar-db", type=str,
                        default=str(REPO_ROOT / "data" / "metar_backfill.db"))
    parser.add_argument("--settlements", type=str,
                        default=str(REPO_ROOT / "data" / "kalshi_settlements.db"))
    parser.add_argument("--norm-stats", type=str,
                        default=str(REPO_ROOT / "data" / "dtw_normalization_stats.json"))
    parser.add_argument("--output", type=str,
                        default=str(OUTPUT_DIR / "a3_station_matching.json"))
    args = parser.parse_args()

    print("=" * 72)
    print("  A3 — DTW PER-STATION MATCHING")
    print("=" * 72)

    # Load normalization stats
    norm_stats = load_normalization_stats(args.norm_stats)
    print(f"\n  Normalization stats loaded: {len(norm_stats)} stations")

    # Load METAR data for similarity computation
    print(f"  Loading METAR data for similarity computation...")
    all_data = {}
    for station in STATIONS:
        all_data[station] = load_metar_corpus(args.metar_db, station)
    total_dates = sum(len(d) for d in all_data.values())
    print(f"  Loaded {total_dates:,} station-days across {len(all_data)} stations")

    # Compute station similarities
    print(f"\n  Computing station climate similarities...")
    similarities = compute_station_similarity(all_data)
    print_top_similar(similarities)

    # Run DTW matching with all 3 strategies
    print(f"\n  Running DTW matching with 3 strategies...")
    results = run_station_matching(all_data, similarities, norm_stats, args.settlements)
    
    # Evaluate
    evaluation = evaluate_results(results)
    
    # Print results
    print(f"\n  {'=' * 50}")
    print(f"  MATCHING STRATEGY COMPARISON")
    print(f"  {'=' * 50}")
    
    for strat, metrics in evaluation.items():
        label = strat.replace("_", " ").title()
        print(f"\n  {label}:")
        print(f"    Accuracy: {metrics['accuracy_pct']}% ({metrics['correct']}/{metrics['total_queries']})")
        print(f"    Stations: {len(metrics['per_station'])}")
        for st, sm in sorted(metrics['per_station'].items()):
            acc = sm['accuracy'] * 100
            print(f"      {st}: {acc:.1f}% ({sm['correct']}/{sm['total']})")
    
    # Save
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "stations_tested": STATIONS[:5],
            "strategies": ["same_station", "similarity_based", "geographic"],
            "normalization": "z-score per-station (from A2)",
            "wind_encoding": "sin/cos (circular)",
            "temporal_leakage_fix": "excluded overlapping windows (>=5 day gap)",
            "data_quality": f"filtered temps outside [{TEMP_MIN_F}°F, {TEMP_MAX_F}°F]",
        },
        "station_similarities": similarities,
        "strategy_evaluation": evaluation,
        "per_station_results": results,
    }
    
    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Full results saved to: {output_path}")
    print("  Done.\n")


if __name__ == "__main__":
    main()