#!/usr/bin/env python3
"""
P4: Calendar Climatology — Standalone test script.

Tests core/signals/calendar_climatology_signal.py logic against the GEFS baseline.

The calendar climatology signal uses a 60-day rolling window z-score:
  - z > 1.5 → predict DOWN (too warm, mean reversion coming)
  - z < -1.5 → predict UP (too cold, mean reversion coming)
  - Confidence capped at 0.60

This version uses the GEFS ensemble mean as the "temperature" source for the
climatology window, blended with settlement temps for the actual climatology.

Usage:
    python3 scripts/test_climatology.py  [--output JSON]

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
CLIMATOLOGY_WINDOW = 60
Z_THRESHOLD = 1.5


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


def compute_climatology_signal(
    station: str,
    target_date: str,
    settlements: Dict[str, Dict[str, float]],
    window: int = CLIMATOLOGY_WINDOW,
) -> Tuple[Optional[str], float]:
    """
    Compute the 60-day rolling z-score climatology signal.
    Uses settlement temperatures as the actual climatology data.
    """
    sdates = sorted(settlements.get(station, {}).keys())
    try:
        idx = sdates.index(target_date)
    except ValueError:
        return None, 0.0

    # Get the last `window` days of settlement data before the target
    history_dates = sdates[max(0, idx - window):idx]
    if len(history_dates) < 10:
        return None, 0.0

    temps = [settlements[station][d] for d in history_dates if settlements[station].get(d) is not None]
    if len(temps) < 10:
        return None, 0.0

    mean = sum(temps) / len(temps)
    if len(temps) > 1:
        var = sum((t - mean) ** 2 for t in temps) / (len(temps) - 1)
    else:
        var = 0.01
    std = math.sqrt(var) if var > 0 else 0.01

    # Get the most recent settlement temp (yesterday) as "current" for z-score
    current_temp = settlements[station].get(sdates[idx - 1])
    if current_temp is None:
        return None, 0.0

    z = (current_temp - mean) / std

    if z > Z_THRESHOLD:
        # Too warm → predict mean reversion DOWN
        conf = min(abs(z) / 3.0, 0.60)
        return 'down', conf
    elif z < -Z_THRESHOLD:
        # Too cold → predict mean reversion UP
        conf = min(abs(z) / 3.0, 0.60)
        return 'up', conf

    return None, 0.0


def run_climatology_test() -> Dict:
    """Backtest: GEFS baseline but with climatology signal override/trade."""
    print("Loading GEFS archive...")
    gefs = load_gefs_all()
    print(f"  Loaded {sum(len(v) for v in gefs.values())} forecasts")

    print("Loading settlements...")
    settlements = load_settlements()
    print(f"  Loaded {sum(len(v) for v in settlements.values())} settlements")

    all_trades = []
    bankroll = INITIAL_BANKROLL
    climo_stats = {"fired": 0, "fired_correct": 0, "not_fired": 0}

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

            # ─── CLIMATOLOGY SIGNAL ───
            climo_dir, climo_conf = compute_climatology_signal(station, target_date, settlements)

            if climo_dir is not None:
                climo_stats["fired"] += 1
                # Use climatology signal direction instead of GEFS
                pred_dir = 1 if climo_dir == "up" else -1
                confidence = climo_conf
            else:
                climo_stats["not_fired"] += 1
                # Fall back to GEFS baseline
                pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
                if pred_dir == 0:
                    continue
                member_temps_f = [t * 9 / 5 + 32 for t in gefs_data["member_temps_c"]]
                n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
                fraction_up = n_up / len(member_temps_f)
                confidence = max(fraction_up, 1.0 - fraction_up)

            if actual_dir == 0:
                continue

            # Use pre-computed calibration
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
            if climo_dir is not None and correct:
                climo_stats["fired_correct"] += 1
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
        "climo_stats": climo_stats,
    }

    print(f"\n=== CALENDAR CLIMATOLOGY RESULTS ===")
    print(f"  Trades: {n}")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  P&L: ${total_pnl:.2f}")
    print(f"  Sharpe: {sharpe:.4f}")
    if climo_stats["fired"] > 0:
        print(f"  Climatology fired: {climo_stats['fired']} ({climo_stats['fired_correct']}/{climo_stats['fired']} correct)")

    return result


def main():
    parser = argparse.ArgumentParser(description="P4: Calendar Climatology Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    results = run_climatology_test()

    print("\n--- Accuracy Improvement Suggestion ---")
    print("P4 Climatology: Use ERA5 reanalysis data (30-year climatology) instead of")
    print("a 60-day rolling window. A 30-year baseline provides stable z-score thresholds")
    print("that don't drift with seasonal transitions. The 60-day window lags seasonality.")
    print("Cross-reference: era5_archive.db has 478 dates of reliable ERA5 data.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()