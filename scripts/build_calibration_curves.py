#!/usr/bin/env python3
"""
Build per-station MARKET-TYPE × DIRECTION calibration curves.

For each station, computes FOUR sets of calibration curves:
  - HIGH/up, HIGH/down:  GEFS ensemble (step 24, max temp) vs HIGH settlement epochs
  - LOW/up,  LOW/down:   NWP multi-model min-temp ensemble vs LOW settlement epochs

Each market-type × direction bucket maps ensemble fraction confidence (0.05
increments from 0.50-0.55 to 0.95-1.00) to empirical win rate.

Sources:
  - HIGH: gefs_archive.db  (step 24, 31 members, 2050 dates, 20 stations)
  - LOW:  nwp_forecasts.db (temperature_2m_min from GFS/ECMWF/ICON/GEM, ~94 dates)
  - Settlement epochs: metar_backfill.db (settlement_epochs, HIGH+LOW buckets)

Output: data/calibration_curves.json with per-station HIGH/up, HIGH/down, LOW/up, LOW/down

Usage: python3 scripts/build_calibration_curves.py
"""

import json
import sqlite3
import struct
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
OUTPUT = REPO_ROOT / "data" / "calibration_curves.json"

STATIONS = [
    "KNYC", "KLAX", "KPHX", "KDFW", "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
    "KHOU", "KLAS", "KMDW", "KMIA", "KMSP", "KMSY", "KOKC", "KPHL", "KSAT", "KSEA", "KSFO",
]

BINS = [0.50 + i * 0.05 for i in range(11)]
BIN_LABELS = [f"{BINS[i]:.2f}-{BINS[i+1]:.2f}" for i in range(len(BINS)-1)]


# ── Helpers ──────────────────────────────────────────────────────────────────

def compute_ensemble_fraction(ensemble_mean_c, prev_temp_c, member_temps_c):
    """
    Compute ensemble fraction and direction.
    Returns (fraction_up, predicted_warming, confidence).
    confidence = max(fraction_agreeing, 1 - fraction_agreeing)
    """
    if not member_temps_c or len(member_temps_c) < 2:
        return None, None, None

    predicted_warming = ensemble_mean_c > prev_temp_c
    agreeing = sum(1 for t in member_temps_c
                   if (t > prev_temp_c) == predicted_warming)
    fraction = agreeing / len(member_temps_c)
    fraction_up = sum(1 for t in member_temps_c if t > prev_temp_c) / len(member_temps_c)
    confidence = max(fraction, 1 - fraction)
    return fraction_up, predicted_warming, confidence


def build_direction_curve(cal_data, station_set):
    """
    Build calibration curve for a given direction's data.
    cal_data: {station: [(confidence, correct), ...]}
    Returns {per_station: {s: {...}}, global: {...}}
    """
    result = {}
    global_data = []

    for s in station_set:
        if not cal_data[s]:
            result[s] = {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}
            continue

        bins = {label: {"wins": 0, "total": 0} for label in BIN_LABELS}
        for conf, correct in cal_data[s]:
            for i in range(len(BINS) - 1):
                if BINS[i] <= conf < BINS[i + 1]:
                    bins[BIN_LABELS[i]]["total"] += 1
                    if correct:
                        bins[BIN_LABELS[i]]["wins"] += 1
                    break
            global_data.append((s, conf, correct))

        bin_results = {}
        for label, data in bins.items():
            total = data["total"]
            if total >= 5:
                bin_results[label] = {"win_rate": round(data["wins"] / total, 4), "n": total}
            else:
                bin_results[label] = {"win_rate": None, "n": total}

        total_wins = sum(1 for _, c in cal_data[s] if c)
        total_n = len(cal_data[s])
        result[s] = {
            "bins": bin_results,
            "global": {"n": total_n, "win_rate": round(total_wins / total_n, 4) if total_n > 0 else 0.5},
            "confidence_distribution": {
                "mean": round(sum(c for c, _ in cal_data[s]) / total_n, 4) if total_n > 0 else 0.0,
                "min": round(min(c for c, _ in cal_data[s]), 4) if total_n > 0 else 0.0,
                "max": round(max(c for c, _ in cal_data[s]), 4) if total_n > 0 else 0.0,
            },
        }

    # Global pooled
    global_total = len(global_data)
    global_wins = sum(1 for _, _, c in global_data if c)
    global_bins = {label: {"wins": 0, "total": 0} for label in BIN_LABELS}
    for _, conf, correct in global_data:
        for i in range(len(BINS) - 1):
            if BINS[i] <= conf < BINS[i + 1]:
                global_bins[BIN_LABELS[i]]["total"] += 1
                if correct:
                    global_bins[BIN_LABELS[i]]["wins"] += 1
                break

    global_bin_results = {}
    for label, data in global_bins.items():
        total = data["total"]
        if total >= 5:
            global_bin_results[label] = {"win_rate": round(data["wins"] / total, 4), "n": total}
        else:
            global_bin_results[label] = {"win_rate": None, "n": total}

    return {
        "per_station": result,
        "global": {"bins": global_bin_results, "n": global_total, "win_rate": round(global_wins / global_total, 4) if global_total > 0 else 0.5},
    }


# ── HIGH Calibration (GEFS step 24 ensemble) ─────────────────────────────────

def build_high_calibration():
    """Build HIGH market type calibration from GEFS step 24 data."""
    gdb = sqlite3.connect(GEFS_DB)
    gcur = gdb.execute("""
        SELECT station, target_date, ensemble_mean, member_values, n_members
        FROM gefs_archive
        WHERE step = 24
        ORDER BY target_date
    """)

    # Pre-load all gefs data to sort and get prev dates
    all_rows = list(gcur.fetchall())
    gdb.close()

    # Group by station
    station_data = defaultdict(list)
    for s, d, mean_c, blob, n in all_rows:
        if s not in STATIONS or mean_c is None:
            continue
        station_data[s].append((d, mean_c, blob, n))

    cal_up = defaultdict(list)
    cal_down = defaultdict(list)
    dir_counts = {"up": {"correct": 0, "total": 0}, "down": {"correct": 0, "total": 0}}
    station_n = {s: 0 for s in STATIONS}

    mdb = sqlite3.connect(METAR_DB)
    mcur = mdb.cursor()

    for s in STATIONS:
        rows = sorted(station_data.get(s, []), key=lambda x: x[0])

        # Load HIGH settlement epochs for this station (sorted)
        mcur.execute("""
            SELECT local_trading_date, settlement_bucket
            FROM settlement_epochs
            WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
            ORDER BY local_trading_date
        """, (s,))
        epoch_rows = mcur.fetchall()
        epoch_map = {r[0]: r[1] for r in epoch_rows}
        epoch_dates = sorted(epoch_map.keys())

        for target_date, mean_c, blob, n_members in rows:
            if target_date not in epoch_map:
                continue

            idx = epoch_dates.index(target_date)
            if idx == 0:
                continue
            prev_date = epoch_dates[idx - 1]

            prev_bucket = epoch_map[prev_date]
            cur_bucket = epoch_map[target_date]

            # Convert buckets to Fahrenheit-like values
            prev_temp_f = float(prev_bucket)
            cur_temp_f = float(cur_bucket)
            prev_temp_c = (prev_temp_f - 32) * 5.0 / 9.0
            cur_temp_c = (cur_temp_f - 32) * 5.0 / 9.0

            n_members = n_members or 31
            if blob and len(blob) >= n_members:
                offsets = list(struct.unpack('b' * n_members, blob[:n_members]))
                member_temps_c = [mean_c + o * 0.1 for o in offsets]
            else:
                continue

            fraction_up, predicted_warming, confidence = compute_ensemble_fraction(
                mean_c, prev_temp_c, member_temps_c
            )
            if confidence is None or confidence <= 0.50:
                continue

            actual_warming = cur_temp_c > prev_temp_c
            correct = predicted_warming == actual_warming
            direction = "up" if predicted_warming else "down"

            if direction == "up":
                cal_up[s].append((confidence, correct))
                dir_counts["up"]["total"] += 1
                if correct:
                    dir_counts["up"]["correct"] += 1
            else:
                cal_down[s].append((confidence, correct))
                dir_counts["down"]["total"] += 1
                if correct:
                    dir_counts["down"]["correct"] += 1

            station_n[s] += 1

    mdb.close()

    up_curves = build_direction_curve(cal_up, STATIONS)
    down_curves = build_direction_curve(cal_down, STATIONS)

    return {
        "up": up_curves,
        "down": down_curves,
    }, dir_counts, station_n


# ── LOW Calibration (NWP multi-model min-temp ensemble) ──────────────────────

def build_low_calibration():
    """
    Build LOW market type calibration from NWP temperature_2m_min forecasts.

    Uses GFS/ECMWF/ICON/GEM min-temp forecasts as "members" to compute
    ensemble fraction (confidence). Actual LOW temps from daily_stats.min_temp_f.
    """
    ndb = sqlite3.connect(NWP_DB)
    ncur = ndb.cursor()

    # Fetch all NWP temperature_2m_min data
    ncur.execute("""
        SELECT station, target_date, model, value
        FROM nwp_forecasts
        WHERE variable = 'temperature_2m_min'
          AND model IN ('gfs', 'ecmwf', 'icon', 'gem')
          AND value IS NOT NULL
        ORDER BY target_date
    """)
    nwp_rows = ncur.fetchall()
    ndb.close()

    # Group by (station, target_date) → {model: temp_c}
    forecast_map = defaultdict(dict)
    for s, d, model, val in nwp_rows:
        if s not in STATIONS:
            continue
        forecast_map[(s, d)][model] = float(val)

    # Load daily min temps from metar_backfill (actual low temps)
    mdb = sqlite3.connect(METAR_DB)
    mcur = mdb.cursor()
    mcur.execute("""
        SELECT station, date_local, min_temp_f
        FROM daily_stats
        WHERE min_temp_f IS NOT NULL
        ORDER BY date_local
    """)
    actual_rows = mcur.fetchall()

    actual_map = defaultdict(dict)  # {station: {date: min_temp_f}}
    for s, d, t in actual_rows:
        if s in STATIONS:
            actual_map[s][d] = float(t)

    # Load LOW settlement epochs for direction
    mcur.execute("""
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs
        WHERE market_type='LOW' AND epoch_status='closed'
        ORDER BY local_trading_date
    """)
    low_epochs_raw = mcur.fetchall()
    epoch_map = defaultdict(dict)
    for d, bucket in low_epochs_raw:
        epoch_map[d.split('-')[0]][d] = bucket  # won't filter by station here

    # Actually just do station-specific epoch queries
    mdb.close()

    cal_up = defaultdict(list)
    cal_down = defaultdict(list)
    dir_counts = {"up": {"correct": 0, "total": 0}, "down": {"correct": 0, "total": 0}}
    station_n = {s: 0 for s in STATIONS}

    mdb2 = sqlite3.connect(METAR_DB)
    mcur2 = mdb2.cursor()

    for s in STATIONS:
        mcur2.execute("""
            SELECT local_trading_date, settlement_bucket
            FROM settlement_epochs
            WHERE station=? AND market_type='LOW' AND epoch_status='closed'
            ORDER BY local_trading_date
        """, (s,))
        low_epochs = [(r[0], r[1]) for r in mcur2.fetchall()]
        epoch_dates = [r[0] for r in low_epochs]
        epoch_buckets = {r[0]: r[1] for r in low_epochs}

        # Get unique target dates for this station
        station_forecast_dates = sorted(set(
            d for (st, d) in forecast_map if st == s
        ))

        for target_date in station_forecast_dates:
            if target_date not in epoch_buckets:
                continue

            idx = epoch_dates.index(target_date)
            if idx == 0:
                continue
            prev_date = epoch_dates[idx - 1]

            prev_bucket = epoch_buckets[prev_date]
            cur_bucket = epoch_buckets[target_date]

            prev_temp_f = float(prev_bucket)
            cur_temp_f = float(cur_bucket)

            # Get NWP forecasts for this station+date (min temp in Celsius)
            models = forecast_map.get((s, target_date), {})
            # Filter to at least 2 models
            if len(models) < 2:
                continue

            temps_c = list(models.values())
            mean_c = sum(temps_c) / len(temps_c)
            prev_temp_c = (prev_temp_f - 32) * 5.0 / 9.0

            # "Members": treat each model forecast as an ensemble member
            predicted_warming = mean_c > prev_temp_c
            agreeing = sum(1 for t in temps_c if (t > prev_temp_c) == predicted_warming)
            fraction = agreeing / len(temps_c)
            confidence = max(fraction, 1 - fraction)

            if confidence <= 0.50:
                continue

            cur_temp_c = (cur_temp_f - 32) * 5.0 / 9.0
            actual_warming = cur_temp_c > prev_temp_c
            correct = predicted_warming == actual_warming
            direction = "up" if predicted_warming else "down"

            if direction == "up":
                cal_up[s].append((confidence, correct))
                dir_counts["up"]["total"] += 1
                if correct:
                    dir_counts["up"]["correct"] += 1
            else:
                cal_down[s].append((confidence, correct))
                dir_counts["down"]["total"] += 1
                if correct:
                    dir_counts["down"]["correct"] += 1

            station_n[s] += 1

    mdb2.close()

    up_curves = build_direction_curve(cal_up, STATIONS)
    down_curves = build_direction_curve(cal_down, STATIONS)

    return {
        "up": up_curves,
        "down": down_curves,
    }, dir_counts, station_n


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("BUILDING CALIBRATION CURVES — Market-Type × Direction")
    print("=" * 70)
    print()

    # ── HIGH calibration ──
    print("▶ Building HIGH calibration curves (GEFS step 24 ensemble)...")
    high_result, high_dir_counts, high_station_n = build_high_calibration()
    print(f"  HIGH total samples: {high_dir_counts['up']['total'] + high_dir_counts['down']['total']}")
    print(f"  HIGH UP:   {high_dir_counts['up']['correct']}/{high_dir_counts['up']['total']} = "
          f"{high_dir_counts['up']['correct']/max(high_dir_counts['up']['total'],1)*100:.1f}%")
    print(f"  HIGH DOWN: {high_dir_counts['down']['correct']}/{high_dir_counts['down']['total']} = "
          f"{high_dir_counts['down']['correct']/max(high_dir_counts['down']['total'],1)*100:.1f}%")

    # ── LOW calibration ──
    print()
    print("▶ Building LOW calibration curves (NWP multi-model min-temp)...")
    low_result, low_dir_counts, low_station_n = build_low_calibration()
    print(f"  LOW total samples: {low_dir_counts['up']['total'] + low_dir_counts['down']['total']}")
    print(f"  LOW UP:   {low_dir_counts['up']['correct']}/{low_dir_counts['up']['total']} = "
          f"{low_dir_counts['up']['correct']/max(low_dir_counts['up']['total'],1)*100:.1f}%")
    print(f"  LOW DOWN: {low_dir_counts['down']['correct']}/{low_dir_counts['down']['total']} = "
          f"{low_dir_counts['down']['correct']/max(low_dir_counts['down']['total'],1)*100:.1f}%")

    # ── Assemble output ──
    calibration = {}
    for s in STATIONS:
        calibration[s] = {
            "HIGH": {
                "up": high_result["up"]["per_station"].get(s, {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}),
                "down": high_result["down"]["per_station"].get(s, {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}),
            },
            "LOW": {
                "up": low_result["up"]["per_station"].get(s, {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}),
                "down": low_result["down"]["per_station"].get(s, {"bins": {}, "global": {"n": 0, "win_rate": 0.5}}),
            },
        }

    calibration["_global"] = {
        "HIGH": {"up": high_result["up"]["global"], "down": high_result["down"]["global"]},
        "LOW": {"up": low_result["up"]["global"], "down": low_result["down"]["global"]},
    }

    with open(OUTPUT, "w") as f:
        json.dump(calibration, f, indent=2)

    # ── Report ──
    print()
    print(f"Calibration curves written to {OUTPUT}")

    total_h = high_dir_counts["up"]["total"] + high_dir_counts["down"]["total"]
    total_l = low_dir_counts["up"]["total"] + low_dir_counts["down"]["total"]
    print(f"  HIGH total: {total_h} samples across {sum(1 for s in STATIONS if high_station_n[s] > 0)}/{len(STATIONS)} stations")
    print(f"  LOW total:  {total_l} samples across {sum(1 for s in STATIONS if low_station_n[s] > 0)}/{len(STATIONS)} stations")
    print(f"  Grand total: {total_h + total_l} samples")
    print(f"  Expected minimum curves: {len(STATIONS)} stations × 4 = {len(STATIONS) * 4}")

    print()
    print("═══ STATION CALIBRATION SUMMARY (HIGH) ═══")
    print(f"  {'Station':<8} {'UpSmpl':>8} {'UpWR':>8} {'DnSmpl':>8} {'DnWR':>8}")
    print("  " + "-" * 42)
    for s in STATIONS:
        hu = high_result["up"]["per_station"].get(s, {"global": {"n": 0, "win_rate": 0}})
        hd = high_result["down"]["per_station"].get(s, {"global": {"n": 0, "win_rate": 0}})
        un = hu["global"]["n"]
        uw = hu["global"]["win_rate"]
        dn = hd["global"]["n"]
        dw = hd["global"]["win_rate"]
        print(f"  {s:<8} {un:>8} {uw*100:>7.1f}% {dn:>8} {dw*100:>7.1f}%")

    print()
    print("═══ STATION CALIBRATION SUMMARY (LOW) ═══")
    print(f"  {'Station':<8} {'UpSmpl':>8} {'UpWR':>8} {'DnSmpl':>8} {'DnWR':>8}")
    print("  " + "-" * 42)
    for s in STATIONS:
        lu = low_result["up"]["per_station"].get(s, {"global": {"n": 0, "win_rate": 0}})
        ld = low_result["down"]["per_station"].get(s, {"global": {"n": 0, "win_rate": 0}})
        un = lu["global"]["n"]
        uw = lu["global"]["win_rate"]
        dn = ld["global"]["n"]
        dw = ld["global"]["win_rate"]
        print(f"  {s:<8} {un:>8} {uw*100:>7.1f}% {dn:>8} {dw*100:>7.1f}%")

    # Data sparsity
    print()
    print("═══ DATA SPARSITY ═══")
    sparse = []
    for s in STATIONS:
        for mt, result in [("HIGH", high_result), ("LOW", low_result)]:
            for direction in ["up", "down"]:
                d = result[direction]["per_station"].get(s, {"global": {"n": 0}})
                n = d["global"]["n"]
                if n < 5:
                    sparse.append((s, mt, direction, n))
    if sparse:
        print(f"  ⚠ {len(sparse)} station/market/direction combos with <5 samples:")
        for s, mt, dr, n in sparse:
            print(f"    {s} {mt} {dr}: {n} samples")
    else:
        print("  ✓ All combos have ≥5 samples")

    print()
    print("═══ NOTE ═══")
    print("LOW calibration uses NWP multi-model (GFS/ECMWF/ICON/GEM) min-temp")
    print("forecasts as ensemble substitutes — data spans ~94 dates (2026-05 to 2026-08).")
    print("HIGH calibration uses full GEFS archive (2021-01 to 2026-08, 2050 dates).")


if __name__ == "__main__":
    main()
