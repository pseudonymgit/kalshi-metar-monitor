#!/usr/bin/env python3
"""
P0: Member-Weighted Voting — Standalone test script.

Loads GEFS archive, computes each member's historical directional accuracy per
station, then uses weighted voting (member accuracy as weight) to compute a
weighted ensemble fraction. Applies direction-specific calibration and compares
against the baseline (unweighted ensemble fraction).

Usage:
    python3 scripts/test_member_weights.py  [--output JSON]

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


def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_per_contract = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return fee_per_contract * contracts


def load_gefs_all() -> Dict[str, Dict[str, dict]]:
    """Load all GEFS step=24 forecasts with member values."""
    conn = sqlite3.connect(GEFS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT station, target_date, ensemble_mean, member_values, n_members
        FROM gefs_archive WHERE step = 24
    """)
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


def compute_member_accuracy(
    gefs: Dict[str, Dict[str, dict]],
    settlements: Dict[str, Dict[str, float]],
    station: str,
) -> List[float]:
    """
    Compute each member's historical directional accuracy for a station.
    Returns list of accuracies (one per member, up to 31).
    """
    sdates = sorted(settlements.get(station, {}).keys())
    member_correct = defaultdict(int)
    member_total = defaultdict(int)

    for target_date in sdates:
        if target_date not in gefs.get(station, {}):
            continue
        gefs_data = gefs[station][target_date]
        if gefs_data["member_temps_c"] is None or len(gefs_data["member_temps_c"]) < 3:
            continue

        # Find previous settlement date
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
        actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
        if actual_dir == 0:
            continue

        for mi, mt_c in enumerate(gefs_data["member_temps_c"]):
            mt_f = mt_c * 9 / 5 + 32
            pred_dir = 1 if mt_f > prev_temp_f else (-1 if mt_f < prev_temp_f else 0)
            if pred_dir == 0:
                continue
            member_total[mi] += 1
            if pred_dir == actual_dir:
                member_correct[mi] += 1

    max_member = max(max(member_total.keys(), default=-1) + 1, 31)
    accuracies = []
    for mi in range(max_member):
        if member_total.get(mi, 0) >= 20:
            accuracies.append(member_correct.get(mi, 0) / member_total[mi])
        else:
            accuracies.append(0.55)  # default fallback — slightly above coin flip
    return accuracies


def compute_weighted_ensemble_fraction(
    member_temps_f: List[float],
    prev_temp_f: float,
    member_weights: List[float],
) -> float:
    """Compute weighted fraction of members predicting UP."""
    total_weight = 0.0
    up_weight = 0.0
    for mt_f, w in zip(member_temps_f, member_weights):
        pred_dir = 1 if mt_f > prev_temp_f else (-1 if mt_f < prev_temp_f else 0)
        if pred_dir == 0:
            continue
        total_weight += w
        if pred_dir == 1:
            up_weight += w
    if total_weight <= 0:
        return 0.5
    return up_weight / total_weight


def run_member_weighted_test(
    gefs: Dict[str, Dict[str, dict]],
    settlements: Dict[str, Dict[str, float]],
    precompute_member_accuracy: bool = True,
) -> Dict:
    """
    Full backtest using member-weighted voting.
    """
    # Precompute member accuracies per station
    member_weights_cache = {}
    for station in STATIONS:
        if precompute_member_accuracy:
            accs = compute_member_accuracy(gefs, settlements, station)
            # Use accuracy as weight (higher = more weight)
            # Normalize: weight = max(0.1, accuracy - 0.5) to avoid negative weights
            weights = [max(0.05, a - 0.45) for a in accs]
            # Smooth with a small constant to avoid zero weights
            member_weights_cache[station] = weights
        else:
            member_weights_cache[station] = [1.0] * 31  # uniform

    all_trades = []
    bankroll = INITIAL_BANKROLL

    for station in STATIONS:
        if station not in settlements:
            continue
        sdates = sorted(settlements[station].keys())
        weights = member_weights_cache.get(station, [1.0] * 31)

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

            # Weighted ensemble fraction
            member_temps_f = [t * 9 / 5 + 32 for t in gefs_data["member_temps_c"]]
            fraction_up = compute_weighted_ensemble_fraction(member_temps_f, prev_temp_f, weights)
            confidence = max(fraction_up, 1.0 - fraction_up)

            # Apply direction-specific calibration from the baseline
            pred_dir_str = "up" if pred_dir == 1 else "down"
            # Use the existing calibration curves
            try:
                with open(str(REPO_ROOT / "data" / "calibration_curves.json")) as f:
                    cal_table = json.load(f)
                cal = cal_table.get(station, {})
                dir_cal = cal.get(pred_dir_str, {})
                dir_bins = dir_cal.get("bins", {})
                confidence_bins = [0.50 + i * 0.05 for i in range(11)]
                bin_label = None
                for i in range(len(confidence_bins) - 1):
                    if confidence_bins[i] <= confidence < confidence_bins[i + 1]:
                        bin_label = f"{confidence_bins[i]:.2f}-{confidence_bins[i+1]:.2f}"
                        break
                if bin_label and bin_label in dir_bins and dir_bins[bin_label].get("win_rate") is not None:
                    confidence = dir_bins[bin_label]["win_rate"]
                else:
                    global_cal = cal_table.get("_global", {})
                    global_dir = global_cal.get(pred_dir_str, {})
                    global_bins = global_dir.get("bins", {})
                    if bin_label and bin_label in global_bins and global_bins[bin_label].get("win_rate") is not None:
                        confidence = global_bins[bin_label]["win_rate"]
            except Exception:
                pass

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

            # Kelly sizing
            if edge > 0 and entry_price < 1.0:
                kelly_pct = edge / (1.0 - entry_price)
            else:
                kelly_pct = 0.0

            n_contracts = int(min(175, max(1, kelly_pct * 0.50 * 1000)))
            n_contracts = max(1, n_contracts)

            correct = pred_dir == actual_dir
            gross_pnl = n_contracts * (1.0 if correct else 0.0)
            cost = n_contracts * entry_price

            # Per-trade bankroll cap (25%)
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
            })

    # Compute stats
    n = len(all_trades)
    correct = sum(1 for t in all_trades if t["correct"])
    acc = correct / n if n else 0.0
    total_pnl = sum(t["net_pnl"] for t in all_trades)
    total_fees = sum(t["total_fees"] for t in all_trades)

    # Daily returns for Sharpe
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

    return {
        "trades": n,
        "correct": correct,
        "accuracy": round(acc, 4),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "daily_sharpe": round(sharpe, 4),
    }


def main():
    parser = argparse.ArgumentParser(description="P0: Member-Weighted Voting Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    print("Loading GEFS archive...")
    gefs = load_gefs_all()
    print(f"  Loaded {sum(len(v) for v in gefs.values())} forecasts across {len(gefs)} stations")

    print("Loading settlements...")
    settlements = load_settlements()
    print(f"  Loaded {sum(len(v) for v in settlements.values())} settlements across {len(settlements)} stations")

    print("\nComputing per-member directional accuracy (this may take a minute)...")
    # Precompute member accuracies for all stations
    member_accs = {}
    for station in STATIONS:
        accs = compute_member_accuracy(gefs, settlements, station)
        member_accs[station] = accs
        n_active = sum(1 for a in accs if a != 0.55)
        mean_acc = np.mean([a for a in accs if a != 0.55]) if n_active > 0 else 0
        print(f"  {station}: {n_active} active members, mean accuracy = {mean_acc:.3f}")

    print("\nRunning member-weighted backtest...")
    results = run_member_weighted_test(gefs, settlements)

    print(f"\n=== RESULTS ===")
    print(f"  Trades: {results['trades']}")
    print(f"  Correct: {results['correct']}")
    print(f"  Accuracy: {results['accuracy']:.4f}")
    print(f"  P&L: ${results['total_pnl']:.2f}")
    print(f"  Fees: ${results['total_fees']:.2f}")
    print(f"  Sharpe: {results['daily_sharpe']:.4f}")

    # Accuracy improvement suggestion
    print("\n--- Accuracy Improvement Suggestion ---")
    print("P0 Member-Weighted: Use a rolling-window accuracy (last 180 days) instead of")
    print("full-archive accuracy. Members' skill changes seasonally — a GEFS member that")
    print("performs well in summer may be weaker in winter. Rolling weights capture this drift.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()