#!/usr/bin/env python3
"""
P3: Frontal Detector — Standalone test script.

Tests core/frontal_detector.py + core/signals/frontal_detector_signal.py logic
against the GEFS baseline. The daily frontal detector uses METAR daily proxies
(pressure change, wind shift, temp gradient, temp trend) — but for this sweep we
rebuild the frontal detection from GEFS daily temperature data since GEFS is the
signal source of record.

Frontal conditions (from frontal_detector_signal.py):
  A. Pressure change > 1.5 mb day-over-day
  B. Wind direction shift > 45°
  C. Temp gradient proxy > 3.34°C day-over-day
  D. Temp trend > 3.0°F day-over-day

We reconstruct A & D from settlement temps (temp trend is observable); the 
frontal signal fires when rapid day-over-day temperature change indicates a
front passed, and we trade the direction implied by the front type:
  - warm front (temps rising) → bias UP
  - cold front (temps falling) → bias DOWN
  - high day-over-day temp change ≥ 2 conditions → override GEFS direction

Usage:
    python3 scripts/test_frontal.py  [--output JSON]

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
TEMP_TREND_THRESHOLD_F = 3.0  # °F day-over-day change signals front passage
TEMP_CHANGE_MIN_F = 2.0       # minimum temp change for frontal override signal


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


def frontal_signal(
    station: str,
    target_date: str,
    settlements: Dict[str, Dict[str, float]],
) -> Tuple[Optional[str], float]:
    """
    Detect frontal passage from day-over-day temperature change.

    Returns (direction, confidence):
      - ('up', conf) when a warm front (strong warming) recently passed
      - ('down', conf) when a cold front (strong cooling) recently passed
      - (None, 0.0) when no frontal signal
    """
    sdates = sorted(settlements.get(station, {}).keys())
    try:
        idx = sdates.index(target_date)
    except ValueError:
        return None, 0.0
    if idx < 1:
        return None, 0.0

    # Recent temp change: compare the last two days before target
    prev1 = settlements[station].get(sdates[idx - 1])
    prev2 = settlements[station].get(sdates[idx - 2]) if idx >= 2 else None

    if prev1 is None:
        return None, 0.0

    if prev2 is not None:
        change_f = prev1 - prev2  # day-over-day change in °F
    else:
        change_f = 0.0

    if abs(change_f) < TEMP_CHANGE_MIN_F:
        return None, 0.0

    # Strong warming → warm front → temps likely to continue up (or at least
    # the front passage is complete → momentum)
    if change_f > TEMP_CHANGE_MIN_F:
        confidence = min(0.60, 0.40 + abs(change_f) / 20.0)
        return 'up', confidence
    elif change_f < -TEMP_CHANGE_MIN_F:
        confidence = min(0.60, 0.40 + abs(change_f) / 20.0)
        return 'down', confidence

    return None, 0.0


def run_frontal_test() -> Dict:
    """Backtest: GEFS baseline signal, frontal override when frontal signal fires."""
    print("Loading GEFS archive...")
    gefs = load_gefs_all()
    print(f"  Loaded {sum(len(v) for v in gefs.values())} forecasts")

    print("Loading settlements...")
    settlements = load_settlements()
    print(f"  Loaded {sum(len(v) for v in settlements.values())} settlements")

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
    frontal_stats = {"fired": 0, "fired_correct": 0, "not_fired": 0}

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

            member_temps_f = [t * 9 / 5 + 32 for t in gefs_data["member_temps_c"]]
            n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
            fraction_up = n_up / len(member_temps_f)
            confidence = max(fraction_up, 1.0 - fraction_up)

            pred_dir_str = "up" if pred_dir == 1 else "down"

            # ─── FRONTAL SIGNAL OVERRIDE ───
            front_dir, front_conf = frontal_signal(station, target_date, settlements)

            if front_dir is not None:
                frontal_stats["fired"] += 1
                # Frontal signal overrides GEFS direction
                pred_dir_str = front_dir
                pred_dir = 1 if front_dir == "up" else -1
                confidence = max(confidence, front_conf)
            else:
                frontal_stats["not_fired"] += 1

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
            if frontal_stats["fired"] >= 0:
                pass
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

    # Track frontal-fired accuracy properly
    frontal_fired_trades = [t for t in all_trades
                            if (lambda s, d: frontal_signal(s, d, settlements) is not None)(
                                t["station"], t["target_date"])]

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
        "frontal_stats": {
            "fired": frontal_stats["fired"],
            "fired_trade_accuracy": (round(sum(1 for t in frontal_fired_trades if t["correct"]) / len(frontal_fired_trades), 4)
                                     if frontal_fired_trades else None),
            "fired_trades": len(frontal_fired_trades),
        },
    }

    print(f"\n=== FRONTAL DETECTOR RESULTS ===")
    print(f"  Trades: {n}")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  P&L: ${total_pnl:.2f}")
    print(f"  Sharpe: {sharpe:.4f}")
    print(f"  Frontal fired (pre-filter): {frontal_stats['fired']}")
    if frontal_fired_trades:
        fa = sum(1 for t in frontal_fired_trades if t["correct"]) / len(frontal_fired_trades)
        print(f"  Frontal-fired trade accuracy: {fa:.4f} ({len(frontal_fired_trades)} trades)")

    return result


def main():
    parser = argparse.ArgumentParser(description="P3: Frontal Detector Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    results = run_frontal_test()

    print("\n--- Accuracy Improvement Suggestion ---")
    print("P3 Frontal: Use GEFS member dispersion to detect front passages — a")
    print("large day-over-day ensemble_mean shift combined with HIGH member spread")
    print("indicates a front transition day where directional certainty is low.")
    print("Gate on that: skip trades when |mean shift| > 6°F AND spread > 8°F.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()