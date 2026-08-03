#!/usr/bin/env python3
"""
P2: Spatial Coherence — Standalone test script.

Rebuilt from Gray Room specification:
  6 regions: Northeast (KNYC, KBOS, KDCA, KPHL), Southeast (KATL, KMIA, KHOU, KMSY),
             Midwest (KMDW, KMSP, KDEN), Southwest (KPHX, KLAS, KLAX, KSFO),
             Texas (KDFW, KAUS, KSAT), Pacific Northwest (KSEA, KOKC)
  Consensus: if ≥75% of stations in a region agree on direction → boost confidence 10%
             if <50% agree → reduce confidence 10%

Usage:
    python3 scripts/test_spatial_coherence.py  [--output JSON]

Do NOT modify gefs_paper_trading_cron.py or bmode_p1_backtest.py.
"""

import argparse
import json
import math
import sqlite3
import struct
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

INITIAL_BANKROLL = 10000.0

# 6 regions per Gray Room spec
REGIONS = {
    "Northeast": ["KNYC", "KBOS", "KDCA", "KPHL"],
    "Southeast": ["KATL", "KMIA", "KHOU", "KMSY"],
    "Midwest":   ["KMDW", "KMSP", "KDEN"],
    "Southwest": ["KPHX", "KLAS", "KLAX", "KSFO"],
    "Texas":     ["KDFW", "KAUS", "KSAT"],
    "Pacific NW": ["KSEA", "KOKC"],
}

# Reverse lookup: station -> region name
STATION_REGION = {}
for rname, rstations in REGIONS.items():
    for s in rstations:
        STATION_REGION[s] = rname


def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_per_contract = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return fee_per_contract * contracts


def load_gefs_all() -> Dict[str, Dict[str, dict]]:
    conn = sqlite3.connect(GEFS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, ensemble_mean, member_values, n_members FROM gefs_archive WHERE step = 24")
    result = defaultdict(dict)
    for r in cur.fetchall():
        n_members = r["n_members"] or 31
        member_values = r["member_values"]
        member_temps_c = None
        if member_values and len(member_values) >= n_members:
            offsets = list(struct.unpack('b' * n_members, member_values[:n_members]))
            member_temps_c = [r["ensemble_mean"] + o * 0.1 for o in offsets]
        result[r["station"]][r["target_date"]] = {
            "mean_c": r["ensemble_mean"],
            "n_members": n_members,
            "member_temps_c": member_temps_c,
        }
    conn.close()
    return {k: dict(v) for k, v in result.items()}


def load_settlements() -> Dict[str, Dict[str, float]]:
    conn = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None and s != "TEST":
            settlements[s][d] = float(t)
    conn.close()
    return {k: dict(v) for k, v in settlements.items()}


def compute_spatial_confidence_modulation(
    station: str,
    target_date: str,
    gefs: Dict[str, Dict[str, dict]],
    prev_temps: Dict[str, float],
) -> float:
    """
    Compute spatial coherence modulation factor for a station on a given date.
    
    Returns a multiplier: 1.10 (boost), 0.90 (penalty), or 1.0 (no change).
    """
    region = STATION_REGION.get(station)
    if region is None:
        return 1.0

    region_stations = REGIONS[region]
    gefs_mean_f = None
    prev_temp_f = None

    # Get this station's GEFS prediction direction
    station_data = gefs.get(station, {}).get(target_date)
    if station_data is None or station_data["member_temps_c"] is None:
        return 1.0

    mean_c = station_data["mean_c"]
    gefs_mean_f = mean_c * 9 / 5 + 32
    prev_temp_f = prev_temps.get(station)
    if prev_temp_f is None:
        return 1.0

    station_pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    if station_pred_dir == 0:
        return 1.0

    # Count direction agreement within the region
    total = 0
    agreeing = 0
    for s in region_stations:
        if s == station:
            total += 1
            agreeing += 1  # station always agrees with itself
            continue
        s_data = gefs.get(s, {}).get(target_date)
        if s_data is None or s_data["member_temps_c"] is None:
            continue
        s_mean_c = s_data["mean_c"]
        s_mean_f = s_mean_c * 9 / 5 + 32
        s_prev = prev_temps.get(s)
        if s_prev is None:
            continue
        s_pred_dir = 1 if s_mean_f > s_prev else (-1 if s_mean_f < s_prev else 0)
        if s_pred_dir == 0:
            continue
        total += 1
        if s_pred_dir == station_pred_dir:
            agreeing += 1

    if total < 2:
        return 1.0

    agreement_pct = agreeing / total

    if agreement_pct >= 0.75:
        return 1.10  # boost 10%
    elif agreement_pct < 0.50:
        return 0.90  # reduce 10%
    else:
        return 1.0


def run_spatial_coherence_test() -> Dict:
    """Full backtest with spatial coherence modulation."""
    print("Loading GEFS archive...")
    gefs = load_gefs_all()
    print(f"  Loaded {sum(len(v) for v in gefs.values())} forecasts")

    print("Loading settlements...")
    settlements = load_settlements()
    print(f"  Loaded {sum(len(v) for v in settlements.values())} settlements")

    # Load calibration curves
    cal_table = {}
    try:
        with open(str(REPO_ROOT / "data" / "calibration_curves.json")) as f:
            cal_table = json.load(f)
    except Exception:
        pass

    def calibrate(station, confidence, direction):
        cal = cal_table.get(station, {})
        dir_cal = cal.get(direction, {})
        dir_bins = dir_cal.get("bins", {})
        confidence_bins = [0.50 + i * 0.05 for i in range(11)]
        bin_label = None
        for i in range(len(confidence_bins) - 1):
            if confidence_bins[i] <= confidence < confidence_bins[i + 1]:
                bin_label = f"{confidence_bins[i]:.2f}-{confidence_bins[i+1]:.2f}"
                break
        if bin_label and bin_label in dir_bins and dir_bins[bin_label].get("win_rate") is not None:
            return dir_bins[bin_label]["win_rate"]
        global_cal = cal_table.get("_global", {})
        global_dir = global_cal.get(direction, {})
        global_bins = global_dir.get("bins", {})
        if bin_label and bin_label in global_bins and global_bins[bin_label].get("win_rate") is not None:
            return global_bins[bin_label]["win_rate"]
        return confidence

    all_trades = []
    bankroll = INITIAL_BANKROLL
    spatial_stats = {"boost": 0, "penalty": 0, "neutral": 0}
    spatial_correct = {"boost": 0, "penalty": 0, "neutral": 0}

    for station in STATIONS:
        if station not in settlements:
            continue
        sdates = sorted(settlements[station].keys())

        for target_date in sdates:
            if target_date not in gefs.get(station, {}):
                continue
            gefs_data = gefs[station][target_date]
            if gefs_data["member_temps_c"] is None or len(gefs_data["member_temps_c"]) < 3:
                continue

            try:
                idx = sdates.index(target_date)
            except ValueError:
                continue
            if idx == 0:
                continue
            prev_date = sdates[idx - 1]
            prev_temp_f = settlements[station].get(prev_date)
            if prev_temp_f is None:
                continue

            actual_temp_f = settlements[station][target_date]
            mean_c = gefs_data["mean_c"]
            gefs_mean_f = mean_c * 9 / 5 + 32

            actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
            pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
            if pred_dir == 0 or actual_dir == 0:
                continue

            temp_diff = abs(gefs_mean_f - prev_temp_f)
            if temp_diff < 0.5:
                continue

            # Baseline confidence
            member_temps_f = [t * 9 / 5 + 32 for t in gefs_data["member_temps_c"]]
            n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
            fraction_up = n_up / len(member_temps_f)
            confidence = max(fraction_up, 1.0 - fraction_up)

            pred_dir_str = "up" if pred_dir == 1 else "down"
            confidence = calibrate(station, confidence, pred_dir_str)

            # ─── SPATIAL COHERENCE MODULATION ───
            # Build prev_temps for all stations on this date
            prev_temps = {}
            for s in STATIONS:
                sdates_s = sorted(settlements.get(s, {}).keys())
                try:
                    s_idx = sdates_s.index(target_date)
                except ValueError:
                    continue
                if s_idx > 0:
                    prev_temps[s] = settlements[s].get(sdates_s[s_idx - 1])

            mod = compute_spatial_confidence_modulation(station, target_date, gefs, prev_temps)

            # Track spatial outcome
            if mod == 1.10:
                spatial_outcome = "boost"
            elif mod == 0.90:
                spatial_outcome = "penalty"
            else:
                spatial_outcome = "neutral"
            spatial_stats[spatial_outcome] += 1

            # Apply spatial modulation to confidence
            if mod == 1.10:
                confidence = min(confidence * 1.10, 0.99)
            elif mod == 0.90:
                confidence = confidence * 0.90

            market_price = 0.50
            if pred_dir == 1:
                entry_price = market_price
                edge = confidence - market_price
            else:
                entry_price = 1.0 - market_price
                edge = confidence - (1.0 - market_price)

            if edge < 0.02:
                continue
            if entry_price < 0.15 or entry_price > 0.70:
                continue

            if edge > 0 and entry_price < 1.0:
                kelly_pct = edge / (1.0 - entry_price)
            else:
                kelly_pct = 0.0

            n_contracts = int(min(175, max(1, kelly_pct * 0.50 * 1000)))
            n_contracts = max(1, n_contracts)

            correct = pred_dir == actual_dir
            gross_pnl = n_contracts * (1.0 if correct else 0.0)
            cost = n_contracts * entry_price

            if cost > bankroll * 0.25:
                if cost > bankroll:
                    continue
                scale = (bankroll * 0.25) / cost
                n_contracts = int(n_contracts * scale)
                cost = n_contracts * entry_price
                gross_pnl = n_contracts * (1.0 if correct else 0.0)

            entry_fee = kalshi_fee(n_contracts, market_price)
            exit_price = 1.0 if correct else 0.0
            exit_fee = kalshi_fee(n_contracts, exit_price)
            total_fees = entry_fee + exit_fee
            net_pnl = gross_pnl - cost - total_fees

            bankroll += net_pnl
            if correct:
                spatial_correct[spatial_outcome] += 1
            all_trades.append({
                "station": station,
                "target_date": target_date,
                "correct": correct,
                "net_pnl": net_pnl,
                "cost": cost,
                "total_fees": total_fees,
                "edge": edge,
                "confidence": confidence,
                "contracts": n_contracts,
                "spatial_mod": mod,
            })

    n = len(all_trades)
    correct = sum(1 for t in all_trades if t["correct"])
    acc = correct / n if n else 0.0
    total_pnl = sum(t["net_pnl"] for t in all_trades)
    total_fees = sum(t["total_fees"] for t in all_trades)

    by_date = defaultdict(list)
    for t in all_trades:
        by_date[t["target_date"]].append(t)
    daily_rets = []
    for d in sorted(by_date.keys()):
        day_pnl = sum(t["net_pnl"] for t in by_date[d])
        daily_rets.append(day_pnl / INITIAL_BANKROLL)
    sharpe = 0.0
    if len(daily_rets) > 2:
        m = np.mean(daily_rets)
        s = np.std(daily_rets, ddof=1)
        sharpe = (m / s * math.sqrt(252)) if s > 0 else 0.0

    result = {
        "trades": n,
        "correct": correct,
        "accuracy": round(acc, 4),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "daily_sharpe": round(sharpe, 4),
        "spatial_stats": spatial_stats,
        "spatial_correct": {k: spatial_correct.get(k, 0) for k in spatial_stats},
    }

    print(f"\n=== SPATIAL COHERENCE RESULTS ===")
    print(f"  Trades: {n}")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  P&L: ${total_pnl:.2f}")
    print(f"  Sharpe: {sharpe:.4f}")
    print(f"  Spatial breakdown: {spatial_stats}")
    for s in ["boost", "penalty", "neutral"]:
        total_s = spatial_stats.get(s, 0)
        correct_s = spatial_correct.get(s, 0)
        if total_s > 0:
            print(f"    {s}: {correct_s}/{total_s} = {correct_s/total_s:.3f}")

    return result


def main():
    parser = argparse.ArgumentParser(description="P2: Spatial Coherence Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    results = run_spatial_coherence_test()

    print("\n--- Accuracy Improvement Suggestion ---")
    print("P2 Spatial Coherence: Use the decompiled continuous modulation formula")
    print("(Phi = 0.6*(1-tanh(|delta|/sigma)) + 0.4*directional_agreement) instead of")
    print("binary 75%/50% thresholds. The continuous formula preserves more information")
    print("and avoids hard cutoffs that lose borderline cases.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()