#!/usr/bin/env python3
"""
P7: Multi-Model Fusion — Standalone test script.

Loads GFS, ECMWF, ICON, GEM data from nwp_forecasts.db, computes the ensemble
mean across models, and tests directional accuracy vs GEFS-only.

Note: The NWP DB stores forecast values in °C for temperature_2m_max and
temperature_2m_min (per Open-Meteo convention). Models have limited coverage:
  ecmwf: ~83 dates, gfs: ~93, icon: ~77, gem: ~79, era5: ~478, gefs_ens: ~2035.

This test computes:
  1. Per-model directional accuracy (against settlement)
  2. Multi-model mean ensemble directional accuracy
  3. A backtest using multi-model ensemble mean as the signal source

Usage:
    python3 scripts/test_multi_model.py  [--output JSON]

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

NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

INITIAL_BANKROLL = 10000.0

MODELS = ["gfs", "ecmwf", "icon", "gem"]
MIN_MODELS = 2  # minimum models to form a multi-model consensus
MIN_DATES = 100  # stop condition: if total overlap dates < this, note and skip

# Model MAE weights from core/multi_model_ensemble.py (inverse-error weighting)
MODEL_WEIGHTS = {"ecmwf": 2.1, "gfs": 2.4, "icon": 2.6, "gem": 2.8}


def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_per_contract = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return fee_per_contract * contracts


def load_nwp_forecasts() -> Dict[str, Dict[str, Dict[str, float]]]:
    """
    Load NWP temperature forecasts.

    Returns {station: {target_date: {model: temp_c}}} using temperature_2m_max
    if available (falling back to temperature_2m_min).
    """
    conn = sqlite3.connect(NWP_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Prefer max temp
    cur.execute("""
        SELECT station, target_date, model, value
        FROM nwp_forecasts
        WHERE variable = 'temperature_2m_max'
        ORDER BY target_date
    """)
    result = defaultdict(lambda: defaultdict(dict))
    for r in cur.fetchall():
        if r["model"] not in MODELS + ["gefs_ens", "era5"]:
            continue
        if r["model"] in MODELS:
            result[r["station"]][r["target_date"]][r["model"]] = float(r["value"])
        elif r["model"] == "gefs_ens":
            # GEFS ensemble in nwp db is stored in member columns; skip individual
            pass

    conn.close()
    return {k: dict(v) for k, v in result.items()}


def load_gefs_archive() -> Dict[str, Dict[str, dict]]:
    """Load GEFS ensemble means for comparison."""
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


def multi_model_mean(nwp: Dict[str, Dict[str, float]], weight_by_skill: bool = False) -> Optional[float]:
    """Compute the multi-model ensemble mean temperature (°C)."""
    if not nwp:
        return None
    if weight_by_skill:
        total_w = 0.0
        weighted = 0.0
        for model, temp_c in nwp.items():
            w = 1.0 / MODEL_WEIGHTS.get(model, 2.5)
            weighted += w * temp_c
            total_w += w
        return weighted / total_w if total_w > 0 else None
    return sum(nwp.values()) / len(nwp)


def run_multi_model_test() -> Dict:
    """Backtest comparing multi-model fusion vs GEFS-only."""
    print("Loading NWP forecasts...")
    nwp = load_nwp_forecasts()
    n_dates = set()
    for st_data in nwp.values():
        n_dates.update(st_data.keys())
    print(f"  Loaded NWP for {len(nwp)} stations, {len(n_dates)} unique dates")

    print("Loading GEFS archive...")
    gefs = load_gefs_archive()

    print("Loading settlements...")
    settlements = load_settlements()

    # Count overlap between multi-model dates and settlements
    overlap_dates = set()
    for station, st_data in nwp.items():
        for d in st_data:
            if station in settlements and d in settlements[station]:
                overlap_dates.add(d)
    print(f"  Overlap dates (multi-model ∩ settlements): {len(overlap_dates)}")

    if len(overlap_dates) < MIN_DATES:
        print(f"  ⚠ STOP CONDITION: multi-model data insufficient (<{MIN_DATES} dates). Noting and skipping.")
        return {
            "trades": 0,
            "accuracy": None,
            "total_pnl": 0.0,
            "daily_sharpe": None,
            "skipped": True,
            "reason": f"Only {len(overlap_dates)} overlap dates (<{MIN_DATES}) — NWP archive too shallow",
            "n_dates_available": len(overlap_dates),
        }

    # ── Phase 1: Directional accuracy comparison ──
    print("\n── Phase 1: Directional accuracy comparison ──")
    model_accuracy = {}
    multi_accuracy = {"hits": 0, "total": 0}
    gefs_accuracy = {"hits": 0, "total": 0}

    for station in STATIONS:
        if station not in settlements or station not in nwp:
            continue
        sdates = sorted(settlements[station].keys())

        # Per-model accuracy
        for model in MODELS:
            mhits = 0
            mtotal = 0
            for target_date in sdates:
                if station not in nwp or target_date not in nwp[station]:
                    continue
                if model not in nwp[station][target_date]:
                    continue
                try:
                    idx = sdates.index(target_date)
                except ValueError:
                    continue
                if idx == 0:
                    continue
                prev_temp_f = settlements[station].get(sdates[idx - 1])
                if prev_temp_f is None:
                    continue
                actual_temp_f = settlements[station][target_date]
                actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
                temp_c = nwp[station][target_date][model]
                temp_f = temp_c * 9 / 5 + 32
                pred_dir = 1 if temp_f > prev_temp_f else (-1 if temp_f < prev_temp_f else 0)
                if pred_dir == 0 or actual_dir == 0:
                    continue
                mtotal += 1
                if pred_dir == actual_dir:
                    mhits += 1
            if mtotal >= 20:
                model_accuracy[model] = {"hits": mhits, "total": mtotal,
                                         "accuracy": round(mhits / mtotal, 4)}

        # Multi-model + GEFS comparison on the SAME overlap dates
        for target_date in sdates:
            if station not in nwp or target_date not in nwp[station]:
                continue
            nwp_st = nwp[station][target_date]
            available = [m for m in MODELS if m in nwp_st]
            if len(available) < MIN_MODELS:
                continue
            try:
                idx = sdates.index(target_date)
            except ValueError:
                continue
            if idx == 0:
                continue
            prev_temp_f = settlements[station].get(sdates[idx - 1])
            if prev_temp_f is None:
                continue
            actual_temp_f = settlements[station][target_date]
            actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
            if actual_dir == 0:
                continue

            # Multi-model mean
            mm_mean_c = multi_model_mean(nwp_st)
            if mm_mean_c is not None:
                mm_mean_f = mm_mean_c * 9 / 5 + 32
                mm_pred = 1 if mm_mean_f > prev_temp_f else (-1 if mm_mean_f < prev_temp_f else 0)
                if mm_pred != 0:
                    multi_accuracy["total"] += 1
                    if mm_pred == actual_dir:
                        multi_accuracy["hits"] += 1

            # GEFS on same date (if available)
            if station in gefs and target_date in gefs[station] and gefs[station][target_date]["mean_c"] is not None:
                gefs_mean_f = gefs[station][target_date]["mean_c"] * 9 / 5 + 32
                g_pred = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
                if g_pred != 0:
                    gefs_accuracy["total"] += 1
                    if g_pred == actual_dir:
                        gefs_accuracy["hits"] += 1

    print(f"  Per-model accuracy (≥20 samples):")
    for model, stats in sorted(model_accuracy.items()):
        print(f"    {model}: {stats['accuracy']:.4f} ({stats['hits']}/{stats['total']})")
    if multi_accuracy["total"] > 0:
        print(f"  Multi-model mean: {multi_accuracy['hits']/multi_accuracy['total']:.4f} ({multi_accuracy['hits']}/{multi_accuracy['total']})")
    if gefs_accuracy["total"] > 0:
        print(f"  GEFS (same dates): {gefs_accuracy['hits']/gefs_accuracy['total']:.4f} ({gefs_accuracy['hits']}/{gefs_accuracy['total']})")

    # ── Phase 2: Full backtest with multi-model as signal source ──
    print("\n── Phase 2: Backtest with multi-model signal source ──")
    all_trades = []
    bankroll = INITIAL_BANKROLL

    for station in STATIONS:
        if station not in settlements or station not in nwp:
            continue
        sdates = sorted(settlements[station].keys())

        for target_date in sdates:
            if station not in nwp or target_date not in nwp[station]:
                continue
            nwp_st = nwp[station][target_date]
            available = [m for m in MODELS if m in nwp_st]
            if len(available) < MIN_MODELS:
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
            actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)

            mm_mean_c = multi_model_mean(nwp_st)
            if mm_mean_c is None:
                continue
            mm_mean_f = mm_mean_c * 9 / 5 + 32

            pred_dir = 1 if mm_mean_f > prev_temp_f else (-1 if mm_mean_f < prev_temp_f else 0)
            if pred_dir == 0 or actual_dir == 0:
                continue

            temp_diff = abs(mm_mean_f - prev_temp_f)
            if temp_diff < 0.5:
                continue

            # Confidence from model agreement
            model_dirs = []
            for model in available:
                model_f = nwp_st[model] * 9 / 5 + 32
                d = 1 if model_f > prev_temp_f else (-1 if model_f < prev_temp_f else 0)
                if d != 0:
                    model_dirs.append(d)
            if model_dirs:
                agree_up = sum(1 for d in model_dirs if d == 1)
                fraction_up = agree_up / len(model_dirs)
                confidence = max(fraction_up, 1.0 - fraction_up)
            else:
                confidence = min(0.99, 0.5 + temp_diff / 20.0)

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
                "models": available,
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
        "skipped": False,
        "model_accuracy": model_accuracy,
        "multi_model_directional": {
            "hits": multi_accuracy["hits"],
            "total": multi_accuracy["total"],
            "accuracy": round(multi_accuracy["hits"] / multi_accuracy["total"], 4) if multi_accuracy["total"] else None,
        },
        "gefs_same_dates_directional": {
            "hits": gefs_accuracy["hits"],
            "total": gefs_accuracy["total"],
            "accuracy": round(gefs_accuracy["hits"] / gefs_accuracy["total"], 4) if gefs_accuracy["total"] else None,
        },
        "n_overlap_dates": len(overlap_dates),
    }

    print(f"\n=== MULTI-MODEL FUSION RESULTS ===")
    print(f"  Trades: {n}")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  P&L: ${total_pnl:.2f}")
    print(f"  Sharpe: {sharpe:.4f}")

    return result


def main():
    parser = argparse.ArgumentParser(description="P7: Multi-Model Fusion Test")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    results = run_multi_model_test()

    print("\n--- Accuracy Improvement Suggestion ---")
    print("P7 Multi-Model: The NWP DB currently has ~80-90 dates per model — too shallow")
    print("for a full backtest. Wire the daily Open-Meteo collector (nwp_collect.py)")
    print("to accumulate 365+ days of GFS/ECMWF/ICON/GEM before fusing. Also weight")
    print("by per-model rolling MAE (ECMWF > GFS) rather than equal weighting.\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {args.output}")

    return results


if __name__ == "__main__":
    main()