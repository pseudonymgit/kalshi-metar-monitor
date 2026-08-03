#!/usr/bin/env python3
"""
P1: Trajectory Confirmation Gate — Standalone test script.

Rebuilt from Gray Room spec (since .pyc depends on core.trajectory modules
that don't have source files).

The trajectory gate checks multi-day temperature trend:
  - If GEFS predicts UP but the 5-day trajectory is DOWN → override (veto)
  - If GEFS predicts DOWN but the 5-day trajectory is UP → override (veto)
  - If trajectory matches GEFS → scale up position by 1.5x
  - If trajectory is flat/neutral → keep normal position

Trajectory is computed from the last 3-5 days of settlement data (linear
regression slope of daily temps).

Usage:
    python3 scripts/test_trajectory_gate.py  [--output JSON]

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
TRAJECTORY_WINDOW = 5  # days of history to compute trajectory


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


def compute_trajectory_direction(
    station: str,
    target_date: str,
    settlements: Dict[str, Dict[str, float]],
    window: int = TRAJECTORY_WINDOW,
) -> Tuple[Optional[str], float]:
    """
    Compute the multi-day temperature trajectory from settlement data.
    
    Uses linear regression on the last `window` days of actual temperatures.
    
    Returns:
        (direction, slope_f_per_day):
            direction = 'up' if slope > 0.5°F/day, 'down' if slope < -0.5°F/day, 'flat' otherwise
            slope = linear regression slope in °F/day
    """
    sdates = sorted(settlements.get(station, {}).keys())
    try:
        idx = sdates.index(target_date)
    except ValueError:
        return None, 0.0

    # Get the last `window` days before the target date
    history_dates = sdates[max(0, idx - window):idx]
    if len(history_dates) < 3:
        return None, 0.0

    temps = []
    days = []
    for i, d in enumerate(history_dates):
        temp = settlements[station].get(d)
        if temp is not None:
            temps.append(temp)
            days.append(i)

    if len(temps) < 3:
        return None, 0.0

    # Linear regression slope
    x = np.array(days, dtype=float)
    y = np.array(temps, dtype=float)
    if np.std(x) == 0:
        return "flat", 0.0
    slope = np.polyfit(x, y, 1)[0]  # °F/day

    # Threshold: 0.5°F/day sustained trajectory
    MIN_TRAJECTORY_SLOPE = 0.5

    if slope > MIN_TRAJECTORY_SLOPE:
        return "up", round(slope, 2)
    elif slope < -MIN_TRAJECTORY_SLOPE:
        return "down", round(slope, 2)
    else:
        return "flat", round(slope, 2)


def run_trajectory_gate_test() -> Dict:
    """Full backtest with trajectory confirmation gate."""
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
    gate_stats = {"confirmed": 0, "neutral": 0, "contradicted": 0, "unavailable": 0}
    gate_correct = {"confirmed": 0, "neutral": 0, "contradicted": 0}

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

            # Baseline confidence from ensemble fraction
            member_temps_f = [t * 9 / 5 + 32 for t in gefs_data["member_temps_c"]]
            n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
            fraction_up = n_up / len(member_temps_f)
            confidence = max(fraction_up, 1.0 - fraction_up)

            # Direction-specific calibration
            pred_dir_str = "up" if pred_dir == 1 else "down"
            confidence = calibrate(station, confidence, pred_dir_str)

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

            # ─── TRAJECTORY CONFIRMATION GATE ───
            traj_dir, traj_slope = compute_trajectory_direction(station, target_date, settlements)

            if traj_dir is None:
                gate_outcome = "unavailable"
                scale_factor = 1.0
            elif traj_dir == "flat":
                gate_outcome = "neutral"
                scale_factor = 1.0
            elif traj_dir == pred_dir_str:
                gate_outcome = "confirmed"
                scale_factor = 1.5  # scale up position
            else:
                gate_outcome = "contradicted"
                scale_factor = 0.0  # veto

            gate_stats[gate_outcome] += 1

            if gate_outcome == "contradicted":
                continue  # Skip the trade entirely

            # ─── Kelly sizing (with trajectory scale factor) ───
            if edge > 0 and entry_price < 1.0:
                kelly_pct = edge / (1.0 - entry_price)
            else:
                kelly_pct = 0.0

            n_contracts = int(min(175, max(1, kelly_pct * 0.50 * scale_factor * 1000)))
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
            if gate_outcome not in gate_correct:
                gate_correct[gate_outcome] = 0
            gate_correct[gate_outcome] += 1 if correct else 0
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
                "gate_outcome": gate_outcome,
                "traj_slope": traj_slope,
            })

    # Stats
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
        "gate_stats": gate_stats,
        "gate_correct": gate_correct,
    }

    print(f"\n=== TRAJECTORY GATE RESULTS ===")
    print(f"  Trades: {n}")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  P&L: ${total_pnl:.2f}")
    print(f"  Sharpe: {sharpe:.4f}")
    print(f"  Gate breakdown: {gate_stats}")
    for g in ["confirmed", "neutral", "contradicted"]:
        if gate_stats[g] > 0:
            c = gate_correct.get(g, 0)
            print(f"    {g}: {c}/{gate_stats[g]} = {c/gate_stats[g]:.3f} accuracy")

    return result


def main():
    parser = argparse.ArgumentParser(description="P1: Trajectory Confirmation Gate Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    results = run_trajectory_gate_test()

    print("\n--- Accuracy Improvement Suggestion ---")
    print("P1 Trajectory Gate: Use a shorter window (3 days) for fast-moving systems")
    print("and longer window (5 days) for synoptic patterns. The adaptive window")
    print("captures cold fronts (fast) and heat waves (slow) more accurately.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()