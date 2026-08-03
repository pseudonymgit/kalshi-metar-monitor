#!/usr/bin/env python3
"""
FUSED ENSEMBLE TEST — GEFS + Gaussian + Forecast Disagreement

Tests weighted-voting fusion of three signals against the calibrated GEFS baseline.

Usage:
    python3 scripts/test_fused_ensemble.py [--days 365] [--start 2025-08-03]

Output:
    docs/weather-engine/backtests/fused_ensemble_20260803.json

Author: Donna -> Gilfoyle (dispatch Aug 3, 2026)
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

# --- Paths --------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- Import baseline backtest helpers ---------------------------------------

from scripts.bmode_p1_backtest import (
    kalshi_fee,
    calibrate_confidence,
    load_gefs_all,
    load_settlements,
    STATIONS,
    INITIAL_BANKROLL,
)

# === Historical Accuracy Weights =============================================

# From signal_sweep_20260803.json:
#   GEFS calibrated = 66.17% (baseline)
#   Gaussian (48-day z-score) = 64.05% accuracy, $91,893 P&L
#   Forecast Disagreement (7-day) = 64.55% accuracy

GEFS_WEIGHT = 0.6617
GAUSSIAN_WEIGHT = 0.6405
DISAGREEMENT_WEIGHT = 0.6455


# === Historical METAR Data Access =============================================

def load_metar_daily(station: str) -> List[dict]:
    """Load daily METAR observations for a station sorted by date ascending."""
    conn = sqlite3.connect(METAR_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(temp_f) as temp, AVG(dewpoint_f) as dewpoint,
               AVG(wind_direction_deg) as wind_dir,
               AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'temp': r[3],
            'dewpoint': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
        })
    conn.close()
    return days


def get_metar_history_for_date(
    metar_days: List[dict], target_date: str, lookback: int
) -> Tuple[Optional[int], Optional[List[dict]]]:
    """Find target_date in metar_days and return (idx, window before idx)."""
    target_idx = None
    for i, d in enumerate(metar_days):
        if d['date'] == target_date:
            target_idx = i
            break
    if target_idx is None:
        return None, None
    start = target_idx - lookback
    if start < 0:
        return None, None
    return target_idx, metar_days[start:target_idx]


# === Gaussian Signal (48-day z-score reversion) ===============================

GAUSS_WINDOW = 48
GAUSS_THRESHOLD = 1.0

def gaussian_signal(
    metar_days: List[dict], target_date: str
) -> Tuple[Optional[str], float]:
    """
    Gaussian z-score reversion signal.
    Uses 48-day rolling high-temp window.
    z > +1.0 -> predict DOWN (too hot, regression expected)
    z < -1.0 -> predict UP (too cold, regression expected)
    Confidence = min(1.0, |z_score| / 3.0)
    """
    target_idx, window = get_metar_history_for_date(metar_days, target_date, GAUSS_WINDOW)
    if window is None or len(window) < GAUSS_WINDOW:
        return None, 0.0

    highs = [d.get('high') for d in window if d.get('high') is not None]
    if len(highs) < GAUSS_WINDOW:
        return None, 0.0

    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01

    # Use the day before target for current temp (same as signal implementation)
    current = metar_days[target_idx - 1].get('high') if target_idx > 0 else None
    if current is None:
        return None, 0.0

    z_score = (current - mean) / std if std > 0 else 0

    if z_score > GAUSS_THRESHOLD:
        return 'down', min(1.0, abs(z_score) / 3.0)
    elif z_score < -GAUSS_THRESHOLD:
        return 'up', min(1.0, abs(z_score) / 3.0)
    else:
        return None, 0.0


# === Forecast Disagreement Signal (7-day comparison) ==========================

DISAGREEMENT_WINDOW = 7
DISAGREEMENT_THRESHOLD = 5.0  # deg F

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def forecast_disagreement_signal(
    metar_days: List[dict], target_date: str
) -> Tuple[Optional[str], float]:
    """
    Forecast disagreement: compare yesterday's high vs 7-day mean.
    If yesterday was hotter -> predict DOWN (revert to mean)
    If yesterday was colder -> predict UP
    """
    target_idx, window = get_metar_history_for_date(metar_days, target_date, DISAGREEMENT_WINDOW + 1)
    if window is None or len(window) < DISAGREEMENT_WINDOW + 1:
        return None, 0.0

    yesterday = window[-1]
    yesterday_high = yesterday.get('high')
    if yesterday_high is None:
        return None, 0.0

    week = window[:-1]
    week_highs = [d.get('high') for d in week if d.get('high') is not None]
    if len(week_highs) < 3:
        return None, 0.0

    weekly_mean = sum(week_highs) / len(week_highs)
    disagreement = yesterday_high - weekly_mean
    abs_disagreement = abs(disagreement)

    if abs_disagreement < DISAGREEMENT_THRESHOLD:
        return None, 0.0

    direction = 'down' if disagreement > 0 else 'up'
    confidence = _sigmoid((abs_disagreement - DISAGREEMENT_THRESHOLD) / 3.0)
    return direction, confidence


# === GEFS Signal (from archive) ===============================================

def gefs_signal(
    gefs_data: dict,
    prev_temp_f: float,
    station: str,
    target_date: str,
) -> Tuple[Optional[str], float]:
    """
    GEFS ensemble signal: direction from mean vs prev temp,
    confidence from ensemble fraction, calibrated.
    """
    mean_c = gefs_data.get("mean_c")
    if mean_c is None:
        return None, 0.0

    gefs_mean_f = mean_c * 9 / 5 + 32

    if prev_temp_f is None:
        return None, 0.0

    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < 0.5:
        return None, 0.0

    pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    if pred_dir == 0:
        return None, 0.0

    member_temps_c = gefs_data.get("member_temps_c")  # Already in Celsius
    if member_temps_c and len(member_temps_c) >= 3:
        member_temps_f = [t * 9 / 5 + 32 for t in member_temps_c]
        n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
        fraction_up = n_up / len(member_temps_f)
        raw_confidence = max(fraction_up, 1.0 - fraction_up)
    else:
        raw_confidence = min(0.99, 0.5 + temp_diff / 20.0)

    pred_direction_str = "up" if pred_dir == 1 else "down"
    confidence = calibrate_confidence(station, raw_confidence, pred_direction_str)

    return pred_direction_str, confidence


# === Weighted Voting Fusion ===================================================

def fused_weighted_voting(
    signals: List[Tuple[Optional[str], float, float]],
) -> Tuple[Optional[str], float]:
    """
    Fuse signals via weighted voting.
    
    signals: list of (direction, confidence, weight)
    Returns (fused_direction, fused_confidence).
    Fused confidence = weighted avg of confidences for the winning direction,
    then calibrated.
    """
    up_weight = 0.0
    down_weight = 0.0
    up_conf_sum = 0.0
    down_conf_sum = 0.0

    for direction, confidence, weight in signals:
        if direction is None or confidence is None:
            continue
        if direction == 'up':
            up_weight += weight
            up_conf_sum += confidence * weight
        elif direction == 'down':
            down_weight += weight
            down_conf_sum += confidence * weight

    if up_weight == 0 and down_weight == 0:
        return None, 0.0

    if up_weight > down_weight:
        fused_direction = 'up'
        fused_confidence = up_conf_sum / up_weight if up_weight > 0 else 0.0
    elif down_weight > up_weight:
        fused_direction = 'down'
        fused_confidence = down_conf_sum / down_weight if down_weight > 0 else 0.0
    else:
        up_avg = up_conf_sum / up_weight if up_weight > 0 else 0.0
        down_avg = down_conf_sum / down_weight if down_weight > 0 else 0.0
        if up_avg >= down_avg:
            fused_direction = 'up'
            fused_confidence = up_avg
        else:
            fused_direction = 'down'
            fused_confidence = down_avg

    # Calibrate the fused confidence
    fused_confidence = calibrate_confidence(None, fused_confidence, fused_direction)
    return fused_direction, fused_confidence


# === P&L Calculation (matches baseline model) =================================

EDGE_THRESHOLD = 0.02
KELLY_FRACTION = 0.50
ENTRY_PRICE_MIN = 0.15
ENTRY_PRICE_MAX = 0.70
MAX_CONTRACTS = 175

EDGE_TIERS = [
    (0.10, 1.00),
    (0.06, 0.75),
    (0.03, 0.50),
    (0.00, 0.00),
]

def compute_trade(
    pred_direction: str,
    confidence: float,
    actual_dir_int: int,
    market_price: float = 0.50,
) -> Optional[dict]:
    """Compute trade params matching baseline model."""
    pred_dir_int = 1 if pred_direction == 'up' else -1

    if pred_dir_int == 1:
        entry_price = market_price
        edge = confidence - market_price
    else:
        entry_price = 1.0 - market_price
        edge = confidence - (1.0 - market_price)

    if entry_price < ENTRY_PRICE_MIN or entry_price > ENTRY_PRICE_MAX:
        return None
    if edge < EDGE_THRESHOLD:
        return None

    tier_mult = 1.0
    for thresh, mult in EDGE_TIERS:
        if edge >= thresh:
            tier_mult = mult
            break
    if tier_mult <= 0.0:
        return None

    kelly_pct = edge / (1.0 - entry_price) if edge > 0 and entry_price < 1.0 else 0
    n_contracts = int(min(MAX_CONTRACTS, max(1, kelly_pct * KELLY_FRACTION * tier_mult * 1000)))
    n_contracts = max(1, n_contracts)

    correct = (pred_dir_int == actual_dir_int)
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    cost = n_contracts * entry_price

    entry_fee = kalshi_fee(n_contracts, market_price)
    exit_price = 1.0 if correct else 0.0
    exit_fee = kalshi_fee(n_contracts, exit_price)
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - cost - total_fees

    return {
        "correct": correct,
        "contracts": n_contracts,
        "entry_price": round(entry_price, 4),
        "edge": round(edge, 4),
        "confidence": round(confidence, 4),
        "gross_pnl": gross_pnl,
        "cost": cost,
        "total_fees": total_fees,
        "net_pnl": round(net_pnl, 2),
    }


# === Full Backtest per Config ===============================================

def run_fused_backtest(
    config_signals: List[str],
    start_date: str,
    days: int,
    initial_bankroll: float = INITIAL_BANKROLL,
) -> dict:
    """Run a fused backtest with the specified signal combination."""
    print(f"\n  Loading GEFS data...")
    gefs_all = load_gefs_all()
    gefs_count = sum(len(v) for v in gefs_all.values())
    print(f"  Loaded {gefs_count} GEFS forecasts")

    print(f"  Loading settlement data...")
    settlements = load_settlements()
    settle_count = sum(len(v) for v in settlements.values())
    print(f"  Loaded {settle_count} settlement records")

    print(f"  Loading METAR history for signals...")
    metar_cache = {}
    for station in STATIONS:
        metar_cache[station] = load_metar_daily(station)
    print(f"  Loaded METAR for {len(metar_cache)} stations")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = start_dt + timedelta(days=days)

    trades = []
    bankroll = initial_bankroll
    daily_returns = defaultdict(float)
    total_signals_fired = {"gefs": 0, "gaussian": 0, "disagreement": 0}

    for station in STATIONS:
        station_dates = sorted(settlements.get(station, {}).keys())
        if len(station_dates) < 2:
            continue
        metar_days = metar_cache.get(station, [])

        for i in range(1, len(station_dates)):
            target_date = station_dates[i]
            prev_date = station_dates[i - 1]

            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            if target_dt < start_dt or target_dt >= end_dt:
                continue

            actual_temp = settlements[station][target_date]
            prev_temp = settlements[station][prev_date]
            if actual_temp is None or prev_temp is None:
                continue

            actual_diff = actual_temp - prev_temp
            if actual_diff == 0:
                continue
            actual_dir_int = 1 if actual_diff > 0 else -1

            # 1. GEFS signal
            gefs_dir = None
            gefs_conf = 0.0
            gefs_for_date = gefs_all.get(station, {}).get(target_date)
            if gefs_for_date:
                gd, gc = gefs_signal(gefs_for_date, prev_temp, station, target_date)
                if gd is not None:
                    gefs_dir = gd
                    gefs_conf = gc
                    total_signals_fired["gefs"] += 1

            # 2. Gaussian signal
            gauss_dir = None
            gauss_conf = 0.0
            if metar_days and len(metar_days) > GAUSS_WINDOW + 1:
                gzd, gzc = gaussian_signal(metar_days, target_date)
                if gzd is not None:
                    gauss_dir = gzd
                    gauss_conf = gzc
                    total_signals_fired["gaussian"] += 1

            # 3. Forecast disagreement signal
            disag_dir = None
            disag_conf = 0.0
            if metar_days and len(metar_days) > DISAGREEMENT_WINDOW + 2:
                dd, dc = forecast_disagreement_signal(metar_days, target_date)
                if dd is not None:
                    disag_dir = dd
                    disag_conf = dc
                    total_signals_fired["disagreement"] += 1

            # Build signal list for this config
            signal_list = []
            if 'gefs' in config_signals and gefs_dir is not None:
                signal_list.append((gefs_dir, gefs_conf, GEFS_WEIGHT))
            if 'gaussian' in config_signals and gauss_dir is not None:
                signal_list.append((gauss_dir, gauss_conf, GAUSSIAN_WEIGHT))
            if 'disagreement' in config_signals and disag_dir is not None:
                signal_list.append((disag_dir, disag_conf, DISAGREEMENT_WEIGHT))

            if not signal_list:
                continue

            # Fuse
            fused_dir, fused_conf = fused_weighted_voting(signal_list)
            if fused_dir is None:
                continue

            # Trade
            trade = compute_trade(fused_dir, fused_conf, actual_dir_int)
            if trade is None:
                continue

            trade["station"] = station
            trade["target_date"] = target_date
            trade["prev_date"] = prev_date
            trade["pred_direction"] = fused_dir
            trade["actual_direction"] = "up" if actual_dir_int == 1 else "down"
            if gefs_dir:
                trade["gefs_details"] = {"dir": gefs_dir, "conf": round(gefs_conf, 4)}
            if gauss_dir:
                trade["gauss_details"] = {"dir": gauss_dir, "conf": round(gauss_conf, 4)}
            if disag_dir:
                trade["disag_details"] = {"dir": disag_dir, "conf": round(disag_conf, 4)}

            net_pnl = trade["net_pnl"]
            trade["bankroll_after"] = round(bankroll + net_pnl, 2)
            bankroll += net_pnl
            daily_returns[target_date] += net_pnl
            trades.append(trade)

    # Compute stats
    total = len(trades)
    correct = sum(1 for t in trades if t.get("correct"))
    accuracy = correct / total if total > 0 else 0.0
    total_pnl = sum(t.get("net_pnl", 0) for t in trades)
    total_fees = sum(t.get("total_fees", 0) for t in trades)

    daily_pct_returns = []
    cumulative = initial_bankroll
    for d in sorted(daily_returns.keys()):
        day_pnl = daily_returns[d]
        day_pct = day_pnl / cumulative if cumulative > 0 else 0.0
        daily_pct_returns.append(day_pct)
        cumulative += day_pnl

    if len(daily_pct_returns) > 1:
        daily_mean = np.mean(daily_pct_returns)
        daily_std = np.std(daily_pct_returns, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    else:
        sharpe = 0.0

    cum_val = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in daily_pct_returns:
        cum_val += r
        peak = max(peak, cum_val)
        dd = (peak - cum_val) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    wins = sum(t.get("net_pnl", 0) for t in trades if t.get("net_pnl", 0) > 0)
    losses = abs(sum(t.get("net_pnl", 0) for t in trades if t.get("net_pnl", 0) < 0))
    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)

    sig_str = "+".join(config_signals)
    print(f"    [{sig_str}] Trades: {total}, Correct: {correct}, Acc: {accuracy*100:.2f}%")
    print(f"    P&L: ${total_pnl:+.2f}, Fees: ${total_fees:.2f}, Sharpe: {sharpe:.2f}")
    print(f"    Signals fired: {dict(total_signals_fired)}")

    return {
        "trades": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "final_bankroll": round(bankroll, 2),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": round(pf, 4),
        "signal_counts": dict(total_signals_fired),
    }


# === Main =====================================================================

BASELINE = {
    "accuracy": 66.17,
    "pnl": 50165.52,
    "sharpe": 11.36,
    "trades": 2096,
    "source": "calibrated GEFS only (bmode_p1_calib-dir)",
}

def main():
    parser = argparse.ArgumentParser(description="Fused Ensemble Test")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=str, default="2025-08-03")
    parser.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    args = parser.parse_args()

    end_date = (datetime.strptime(args.start, "%Y-%m-%d") + timedelta(days=args.days - 1)).strftime("%Y-%m-%d")

    print("=" * 72)
    print("  FUSED ENSEMBLE TEST - GEFS + Gaussian + Forecast Disagreement")
    print("=" * 72)
    print(f"  Period: {args.start} -> {end_date} ({args.days} days)")
    print(f"  Baseline: {BASELINE['accuracy']:.1f}% acc, ${BASELINE['pnl']:.2f} P&L, "
          f"Sharpe {BASELINE['sharpe']:.2f}, {BASELINE['trades']} trades")
    print(f"  Weights: GEFS={GEFS_WEIGHT:.4f}, Gauss={GAUSSIAN_WEIGHT:.4f}, "
          f"Disag={DISAGREEMENT_WEIGHT:.4f}")
    print(f"  P&L config: edge={EDGE_THRESHOLD}, kelly={KELLY_FRACTION}, "
          f"price=[{ENTRY_PRICE_MIN},{ENTRY_PRICE_MAX}], max_c={MAX_CONTRACTS}")

    configs = [
        ("gefs_gaussian_disagreement", ["gefs", "gaussian", "disagreement"],
         "GEFS + Gaussian + Disagreement"),
        ("gefs_gaussian", ["gefs", "gaussian"],
         "GEFS + Gaussian only"),
        ("gefs_disagreement", ["gefs", "disagreement"],
         "GEFS + Disagreement only"),
    ]

    results = {}
    for cfg_key, cfg_signals, cfg_label in configs:
        print(f"\n{'~' * 72}")
        print(f"  CONFIG: {cfg_label}")
        print(f"{'~' * 72}")
        results[cfg_key] = run_fused_backtest(cfg_signals, args.start, args.days, args.bankroll)

    # Summary table
    print(f"\n{'=' * 100}")
    print("  FUSED ENSEMBLE - RESULTS SUMMARY")
    print(f"{'=' * 100}")
    print(f"  {'Config':<36} {'Trades':>7} {'Acc%':>7} {'P&L':>12} {'Sharpe':>8} "
          f"{'MaxDD%':>7} {'PF':>7} {'vsBase':>9}")
    print(f"  {'-' * 84}")

    best_acc = -1.0
    best_key = None
    for cfg_key, _, cfg_label in configs:
        r = results[cfg_key]
        acc_pct = r["accuracy"] * 100
        vs_base = acc_pct - BASELINE["accuracy"]
        vs_str = f"{vs_base:+.2f}pp"
        print(f"  {cfg_label:<36} {r['trades']:>7} {acc_pct:>6.1f}% "
              f"${r['total_pnl']:>+11.2f} {r['sharpe']:>8.2f} {r['max_drawdown_pct']:>6.2f}% "
              f"{r['profit_factor']:>7.2f} {vs_str:>9}")
        if r["accuracy"] > best_acc:
            best_acc = r["accuracy"]
            best_key = cfg_key

    print(f"\n{'=' * 72}")
    print("  RECOMMENDATION")
    print(f"{'=' * 72}")

    best_result = results[best_key]
    best_acc_pct = best_result["accuracy"] * 100
    best_vs_base = best_acc_pct - BASELINE["accuracy"]

    if best_acc_pct > BASELINE["accuracy"]:
        print(f"  OK FUSED ENSEMBLE BEATS BASELINE")
        print(f"     Best: {best_key} ({best_acc_pct:.2f}%, +{best_vs_base:.2f}pp)")
        print(f"     P&L: ${best_result['total_pnl']:.2f} vs ${BASELINE['pnl']:.2f}")
        print(f"     Recommendation: WIRE the {best_key} fused ensemble")
    else:
        print(f"  X FUSED ENSEMBLE DOES NOT BEAT BASELINE")
        print(f"     Best: {best_key} ({best_acc_pct:.2f}%, delta={best_vs_base:+.2f}pp)")
        closest_key = None
        closest_delta = 999.0
        for cfg_key, _, _ in configs:
            delta = abs(results[cfg_key]["accuracy"] * 100 - BASELINE["accuracy"])
            if delta < closest_delta:
                closest_delta = delta
                closest_key = cfg_key
        cr = results[closest_key]
        print(f"     Closest: {closest_key} ({cr['accuracy']*100:.2f}%, delta={closest_delta:.2f}pp)")
        print(f"     Recommendation: DO NOT WIRE - stick with GEFS only")

    # Build output JSON
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": {"start": args.start, "end": end_date, "days": args.days},
        "config": {
            "edge_threshold": EDGE_THRESHOLD,
            "kelly_fraction": KELLY_FRACTION,
            "entry_price_min": ENTRY_PRICE_MIN,
            "entry_price_max": ENTRY_PRICE_MAX,
            "max_contracts": MAX_CONTRACTS,
            "market_price": 0.50,
            "signal_weights": {
                "gefs": GEFS_WEIGHT,
                "gaussian": GAUSSIAN_WEIGHT,
                "disagreement": DISAGREEMENT_WEIGHT,
            },
            "fusion_method": "weighted voting + direction-specific calibration",
        },
        "baseline": BASELINE,
        "configurations": {},
        "recommendation": (
            f"WIRE {best_key}" if best_acc_pct > BASELINE["accuracy"]
            else f"DO NOT WIRE - stick with GEFS only"
        ),
    }
    for cfg_key, _, _ in configs:
        r = results[cfg_key]
        acc_pct = r["accuracy"] * 100
        vs_base = acc_pct - BASELINE["accuracy"]
        output["configurations"][cfg_key] = {
            "accuracy": round(acc_pct, 2),
            "pnl": r["total_pnl"],
            "sharpe": r["sharpe"],
            "trades": r["trades"],
            "vs_baseline": f"{vs_base:+.2f}pp",
        }

    output_path = OUTPUT_DIR / "fused_ensemble_20260803.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print("  Done.")


if __name__ == "__main__":
    main()
