#!/usr/bin/env python3
"""
P5: Gaussian Signal — Standalone test script.

Tests core/signals/gaussian_signal.py logic against the GEFS baseline.

The Gaussian signal uses a 48-day rolling window z-score:
  - z > 1.0 → predict DOWN (too warm, mean reversion)
  - z < -1.0 → predict UP (too cold, mean reversion)
  - Confidence = abs(z_score)

This version uses settlement temperatures as the climatology source.

Usage:
    python3 scripts/test_gaussian.py  [--output JSON]

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
GAUSSIAN_WINDOW = 48


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


def compute_gaussian_signal(
    station: str,
    target_date: str,
    settlements: Dict[str, Dict[str, float]],
    window: int = GAUSSIAN_WINDOW,
) -> Tuple[Optional[str], float]:
    """
    Compute the 48-day rolling z-score gaussian reversion signal.
    """
    sdates = sorted(settlements.get(station, {}).keys())
    try:
        idx = sdates.index(target_date)
    except ValueError:
        return None, 0.0

    history_dates = sdates[max(0, idx - window):idx]
    if len(history_dates) < 10:
        return None, 0.0

    temps = [settlements[station][d] for d in history_dates if settlements[station].get(d) is not None]
    if len(temps) < 10:
        return None, 0.0

    mean = sum(temps) / len(temps)
    if len(temps) > 1:
        var = sum((t - mean) ** 2 for t in temps) / len(temps)
    else:
        var = 0.01
    std = math.sqrt(var) if var > 0 else 0.01

    current_temp = settlements[station].get(sdates[idx - 1])
    if current_temp is None:
        return None, 0.0

    z = (current_temp - mean) / std

    if z > 1.0:
        return 'down', abs(z)
    elif z < -1.0:
        return 'up', abs(z)
    else:
        return None, 0.0


def run_gaussian_test() -> Dict:
    """Backtest: GEFS baseline with gaussian signal override."""
    print("Loading GEFS archive...")
    gefs = load_gefs_all()
    print(f"  Loaded {sum(len(v) for v in gefs.values())} forecasts")

    print("Loading settlements...")
    settlements = load_settlements()
    print(f"  Loaded {sum(len(v) for v in settlements.values())} settlements")

    all_trades = []
    bankroll = INITIAL_BANKROLL
    gauss_stats = {"fired": 0, "fired_correct": 0, "not_fired": 0}

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

            # ─── GAUSSIAN SIGNAL ───
            gauss_dir, gauss_conf = compute_gaussian_signal(station, target_date, settlements)

            if gauss_dir is not None:
                gauss_stats["fired"] += 1
                pred_dir = 1 if gauss_dir == "up" else -1
                confidence = gauss_conf
            else:
                gauss_stats["not_fired"] += 1
                pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
                if pred_dir == 0:
                    continue
                member_temps_f = [t * 9 / 5 + 32 for t in gefs_data["member_temps_c"]]
                n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
                fraction_up = n_up / len(member_temps_f)
                confidence = max(fraction_up, 1.0 - fraction_up)

            if actual_dir == 0:
                continue

            # Calibration
            pred_dir_str = "up" if pred_dir == 1 else "down"
            cal_table = {}
            try:
                with open(str(REPO_ROOT / "data" / "calibration_curves.json")) as f:
                    cal_table = json.load(f)
            except Exception:
                pass
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
            if gauss_dir is not None and correct:
                gauss_stats["fired_correct"] += 1
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
        "gauss_stats": gauss_stats,
    }

    print(f"\n=== GAUSSIAN SIGNAL RESULTS ===")
    print(f"  Trades: {n}")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  P&L: ${total_pnl:.2f}")
    print(f"  Sharpe: {sharpe:.4f}")
    if gauss_stats["fired"] > 0:
        print(f"  Gaussian fired: {gauss_stats['fired']} ({gauss_stats['fired_correct']}/{gauss_stats['fired']} correct)")

    return result


def main():
    parser = argparse.ArgumentParser(description="P5: Gaussian Signal Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    results = run_gaussian_test()

    print("\n--- Accuracy Improvement Suggestion ---")
    print("P5 Gaussian: Use z-score with a dynamic threshold that adapts to")
    print("seasonal volatility. Summer std deviations are smaller than winter;")
    print("a fixed 1.0 threshold misses summer extremes and over-fires in winter.")
    print("Use z_threshold = 1.0 * (seasonal_std / annual_std) to compensate.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()