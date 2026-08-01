#!/usr/bin/env python3
"""
GEFS Grid Sweep — Targeted parameter grid against Kalshi settlement DB.

Sweeps over:
  - edge_threshold:    0.02, 0.05, 0.08, 0.11, 0.15
  - kelly_fraction:    0.1, 0.25, 0.5
  - entry_price_min:   0.15, 0.25, 0.40
  - entry_price_max:   0.70, 0.75, 0.85
  - max_contracts:     100, 250, 500
  - calibration_method: none, isotonic, beta

Fees: kalshi_real (ceil(0.07 × C × P × (1-P)) per side)
P&L:  Per-contract binary math, DOWN trades use entry_price = 1 - market_price
No stop_loss, trajectory_gate, or position_sizing_model params.

Reports per-station AND aggregate metrics.
No AI/ML — B-Mode compliant.

Usage:
    python3 scripts/sweep/gefs_grid_sweep.py [--quick] [--calibration none]

Output:
    data/sweep/gefs_grid_sweep_results.json
"""

import json
import math
import os
import sys
import time
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from itertools import product
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sweep import config as sweep_config

KALSHI_SETTLEMENTS_DB = sweep_config.DB_PATH
GEFS_DB = sweep_config.GEFS_DB_PATH
SWEEP_DIR = sweep_config.SWEEP_DIR
STATIONS = sweep_config.STATIONS
os.makedirs(SWEEP_DIR, exist_ok=True)


# ── Kalshi REAL Fee Model ──
def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price))


# ── Calibration Models ──
class IsotonicCalibrator:
    """Isotonic regression calibration (piecewise monotonic)."""
    def __init__(self):
        self.boundaries = []
        self.adjustments = []
    
    def fit(self, confidences, outcomes):
        """Simple bin-based isotonic calibration."""
        n_bins = 10
        bins = np.linspace(0.5, 1.0, n_bins + 1)
        self.boundaries = bins
        self.adjustments = np.zeros(n_bins)
        counts = np.zeros(n_bins)
        for c, o in zip(confidences, outcomes):
            idx = min(int((c - 0.5) / 0.5 * n_bins), n_bins - 1)
            idx = max(0, idx)
            self.adjustments[idx] += o
            counts[idx] += 1
        for i in range(n_bins):
            if counts[i] > 0:
                self.adjustments[i] /= counts[i]
        # Enforce monotonicity: PAVA-like forward pass
        for i in range(1, n_bins):
            if counts[i] > 0 and counts[i-1] > 0:
                if self.adjustments[i] < self.adjustments[i-1]:
                    weighted = (self.adjustments[i]*counts[i] + self.adjustments[i-1]*counts[i-1]) / (counts[i] + counts[i-1])
                    self.adjustments[i] = weighted
                    self.adjustments[i-1] = weighted
    
    def predict(self, confidence):
        idx = min(int((confidence - 0.5) / 0.5 * len(self.boundaries)), len(self.boundaries) - 2)
        idx = max(0, idx)
        if idx < len(self.adjustments) and self.adjustments[idx] > 0:
            return min(0.99, self.adjustments[idx])
        return min(0.99, confidence)


class BetaCalibrator:
    """Beta regression calibration using Platt's method."""
    def __init__(self, a=1.0, b=1.0):
        self.a = a
        self.b = b
    
    def fit(self, confidences, outcomes):
        """Simple moment-matching beta calibration."""
        outcomes_arr = np.array(outcomes)
        confidences_arr = np.array(confidences)
        mask = outcomes_arr > 0
        if mask.sum() > 0:
            mean_pos = confidences_arr[mask].mean()
            mean_neg = confidences_arr[~mask].mean() if (~mask).sum() > 0 else 0.5
            var_pos = confidences_arr[mask].var() if mask.sum() > 1 else 0.01
            if var_pos > 0:
                self.a = mean_pos * (mean_pos * (1 - mean_pos) / var_pos - 1)
                self.b = (1 - mean_pos) * (mean_pos * (1 - mean_pos) / var_pos - 1)
            self.a = max(0.1, self.a)
            self.b = max(0.1, self.b)
    
    def predict(self, confidence):
        # Beta CDF
        from scipy.special import betainc  # Use scipy
        try:
            return float(betainc(self.a, self.b, min(0.999, max(0.001, confidence))))
        except:
            return min(0.99, confidence)


class NoCalibrator:
    def fit(self, confidences, outcomes):
        pass
    def predict(self, confidence):
        return min(0.99, confidence)


# ── Data Loading ──
def load_kalshi_settlements(source_type: str = None) -> Dict[str, Dict[str, float]]:
    conn = sqlite3.connect(KALSHI_SETTLEMENTS_DB)
    cur = conn.cursor()
    query = "SELECT station, target_date, kalshi_temp FROM kalshi_settlements"
    params = []
    filters = []
    if source_type:
        filters.append("source_type = ?")
        params.append(source_type)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY target_date"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    result = defaultdict(dict)
    for row in rows:
        station, date_str, temp = row
        if temp is not None:
            result[station][date_str] = temp
    return dict(result)


def load_gefs_archive() -> Dict[str, Dict[str, dict]]:
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT target_date, station, ensemble_mean, n_members, step
        FROM gefs_archive WHERE step = 24 ORDER BY target_date
    """)
    rows = cur.fetchall()
    conn.close()
    result = defaultdict(dict)
    for row in rows:
        date_str, station, mean_, n_mem, step = row
        if mean_ is not None:
            result[station][date_str] = {
                "mean": mean_,
                "n_members": n_mem or 0,
            }
    return dict(result)


# ── Trading Simulation ──
def simulate_trade(
    edge_threshold: float,
    kelly_fraction: float,
    entry_price_min: float,
    entry_price_max: float,
    max_contracts: int,
    calibrator,
    station: str,
    date_str: str,
    gefs_mean_f: float,
    actual_temp_f: float,
    prev_temp_f: Optional[float],
) -> Optional[dict]:
    if prev_temp_f is None:
        return None
    
    actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
    pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    
    if pred_dir == 0 or actual_dir == 0:
        return None
    
    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < 0.5:
        return None
    
    raw_conf = min(0.99, temp_diff / 10.0)
    confidence = calibrator.predict(raw_conf)
    
    # Market price derived from confidence
    market_price = min(0.95, 0.5 + confidence * 0.4)
    
    if pred_dir == 1:
        entry_price = market_price
    else:
        entry_price = 1.0 - market_price
    
    if entry_price < entry_price_min or entry_price > entry_price_max:
        return None
    
    edge = confidence - entry_price
    if edge < edge_threshold:
        return None
    
    # Kelly sizing
    if edge > 0 and entry_price < 1.0:
        kelly_pct = edge / (1.0 - entry_price)
    else:
        kelly_pct = 0
    
    n_contracts = int(min(max_contracts, max(1, kelly_pct * kelly_fraction * 1000)))
    n_contracts = max(1, n_contracts)
    
    correct = pred_dir == actual_dir
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    cost = n_contracts * entry_price
    
    entry_fee = kalshi_fee(n_contracts, market_price)
    exit_price = 1.0 if correct else 0.0
    exit_fee = kalshi_fee(n_contracts, exit_price)
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - cost - total_fees
    
    return {
        "station": station, "date": date_str,
        "predicted": pred_dir, "actual": actual_dir,
        "confidence": confidence, "raw_confidence": raw_conf,
        "edge": edge, "contracts": n_contracts,
        "correct": correct, "net_pnl": net_pnl,
        "entry_price": entry_price, "market_price": market_price,
        "entry_fee": entry_fee, "exit_fee": exit_fee,
        "total_fees": total_fees, "cost": cost,
    }


def compute_metrics(trades: List[dict], n_trades_min: int = 10) -> dict:
    if len(trades) < n_trades_min:
        return {
            "n_trades": 0, "accuracy": 0.0, "total_pnl": 0.0,
            "sharpe": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0,
            "avg_pnl": 0.0, "win_rate": 0.0, "total_fees": 0.0,
            "ece": 0.0, "n_daily": 0,
        }
    
    n = len(trades)
    correct = sum(1 for t in trades if t["correct"])
    accuracy = correct / n
    
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t["date"]] += t["net_pnl"]
    
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["total_fees"] for t in trades)
    avg_pnl = total_pnl / n if n > 0 else 0
    win_rate = correct / n
    
    # Profit factor
    gross_wins = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gross_losses = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (float('inf') if gross_wins > 0 else 0.0)
    
    # Daily Sharpe (annualized) — CORRECTED: percentage returns vs bankroll
    # Track cumulative P&L to compute running bankroll
    # Start with initial_bankroll = max absolute P&L or $10K
    initial_bankroll = 10000.0
    
    # Sort dates to process chronologically
    sorted_dates = sorted(daily_pnl.keys())
    daily_return_pcts = []
    bankroll = initial_bankroll
    
    for date in sorted_dates:
        day_pnl = daily_pnl[date]
        if bankroll > 0:
            day_return_pct = day_pnl / bankroll
        else:
            day_return_pct = 0.0
        daily_return_pcts.append(day_return_pct)
        bankroll += day_pnl  # Update bankroll for next day
    
    n_daily = len(daily_return_pcts)
    
    if n_daily > 1:
        daily_mean = np.mean(daily_return_pcts)
        daily_std = np.std(daily_return_pcts, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    else:
        sharpe = 0.0
    
    # Max drawdown (from percentage returns)
    cumulative_return = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in daily_return_pcts:
        cumulative_return += r
        peak = max(peak, cumulative_return)
        dd = (peak - cumulative_return) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    # Calibration ECE
    ece = 0.0
    if n >= 30:
        n_bins = 10
        bin_acc = np.zeros(n_bins)
        bin_conf = np.zeros(n_bins)
        bin_counts = np.zeros(n_bins)
        for t in trades:
            conf = t["raw_confidence"]
            if conf >= 1.0:
                conf = 0.999
            bin_idx = min(int((conf - 0.5) / 0.5 * n_bins), n_bins - 1)
            bin_idx = max(0, bin_idx)
            bin_acc[bin_idx] += 1 if t["correct"] else 0
            bin_conf[bin_idx] += conf
            bin_counts[bin_idx] += 1
        for i in range(n_bins):
            if bin_counts[i] > 0:
                bin_acc[i] /= bin_counts[i]
                bin_conf[i] /= bin_counts[i]
        ece = float(np.sum(bin_counts * np.abs(bin_acc - bin_conf)) / np.sum(bin_counts)) if bin_counts.sum() > 0 else 0.0
    
    return {
        "n_trades": n, "accuracy": accuracy, "total_pnl": total_pnl,
        "sharpe": float(sharpe), "profit_factor": float(profit_factor) if profit_factor != float('inf') else 999.0,
        "max_drawdown": float(max_dd), "avg_pnl": avg_pnl,
        "win_rate": win_rate, "total_fees": total_fees, "ece": ece, "n_daily": n_daily,
    }


def run_grid_sweep(
    settlements: Dict[str, Dict[str, float]],
    gefs: Dict[str, Dict[str, dict]],
    calibration_method: str = "none",
    quick: bool = False,
) -> List[dict]:
    """Run a grid sweep over all parameter combinations."""
    
    # Parameter grid
    edge_thresholds = [0.02, 0.05, 0.08, 0.11, 0.15]
    kelly_fractions = [0.1, 0.25, 0.5]
    entry_price_mins = [0.15, 0.25, 0.40]
    entry_price_maxs = [0.70, 0.75, 0.85]
    max_contractss = [100, 250, 500]
    
    if quick:
        edge_thresholds = [0.02, 0.08, 0.15]
        kelly_fractions = [0.1, 0.5]
        entry_price_mins = [0.15, 0.40]
        entry_price_maxs = [0.70, 0.85]
        max_contractss = [100, 500]
    
    total_configs = len(edge_thresholds) * len(kelly_fractions) * len(entry_price_mins) * len(entry_price_maxs) * len(max_contractss)
    print(f"Grid: {len(edge_thresholds)}×{len(kelly_fractions)}×{len(entry_price_mins)}×{len(entry_price_maxs)}×{len(max_contractss)} = {total_configs} configs")
    print(f"Calibration: {calibration_method}")
    print(f"Stations: {len(STATIONS)}")
    print(f"Fee: kalshi_real (ceil(0.07 × C × P × (1-P)) per side)")
    print()
    
    results = []
    t0 = time.time()
    config_idx = 0
    
    for et, kf, ep_min, ep_max, mc in product(
        edge_thresholds, kelly_fractions, entry_price_mins, entry_price_maxs, max_contractss
    ):
        config_idx += 1
        
        # Create calibrator
        if calibration_method == "isotonic":
            calibrator = IsotonicCalibrator()
        elif calibration_method == "beta":
            calibrator = BetaCalibrator()
        else:
            calibrator = NoCalibrator()
        
        # Run all trades
        all_trades = []
        per_station_trades = defaultdict(list)
        
        for station in STATIONS:
            station_settlements = settlements.get(station, {})
            station_gefs = gefs.get(station, {})
            dates = sorted(set(station_settlements.keys()) & set(station_gefs.keys()))
            
            for i, date_str in enumerate(dates):
                actual_temp = station_settlements[date_str]
                gefs_data = station_gefs[date_str]
                gefs_mean_c = gefs_data["mean"]
                if gefs_mean_c is None:
                    continue
                gefs_mean_f = gefs_mean_c * 9/5 + 32
                
                prev_temp = None
                if i > 0:
                    prev_temp = station_settlements.get(dates[i - 1])
                
                trade = simulate_trade(
                    et, kf, ep_min, ep_max, mc, calibrator,
                    station, date_str, gefs_mean_f, actual_temp, prev_temp,
                )
                if trade:
                    all_trades.append(trade)
                    per_station_trades[station].append(trade)
        
        # Train calibrator on all trades
        if len(all_trades) >= 50:
            confidences = [t["raw_confidence"] for t in all_trades]
            outcomes = [1 if t["correct"] else 0 for t in all_trades]
            calibrator.fit(confidences, outcomes)
            
            # Re-run with calibrated confidence
            all_trades = []
            per_station_trades = defaultdict(list)
            for station in STATIONS:
                station_settlements = settlements.get(station, {})
                station_gefs = gefs.get(station, {})
                dates = sorted(set(station_settlements.keys()) & set(station_gefs.keys()))
                
                for i, date_str in enumerate(dates):
                    actual_temp = station_settlements[date_str]
                    gefs_data = station_gefs[date_str]
                    if gefs_data["mean"] is None:
                        continue
                    gefs_mean_f = gefs_data["mean"] * 9/5 + 32
                    prev_temp = station_settlements.get(dates[i - 1]) if i > 0 else None
                    
                    trade = simulate_trade(
                        et, kf, ep_min, ep_max, mc, calibrator,
                        station, date_str, gefs_mean_f, actual_temp, prev_temp,
                    )
                    if trade:
                        all_trades.append(trade)
                        per_station_trades[station].append(trade)
        
        # Aggregate metrics
        aggregate = compute_metrics(all_trades)
        
        # Per-station metrics
        station_metrics = {}
        for station in STATIONS:
            sm = compute_metrics(per_station_trades.get(station, []), n_trades_min=5)
            if sm["n_trades"] > 0:
                station_metrics[station] = sm
        
        result = {
            "config": {
                "edge_threshold": et,
                "kelly_fraction": kf,
                "entry_price_min": ep_min,
                "entry_price_max": ep_max,
                "max_contracts": mc,
                "calibration_method": calibration_method,
                "fee_model": "kalshi_real",
            },
            **aggregate,
            "n_stations_active": len(station_metrics),
            "station_metrics": station_metrics,
        }
        results.append(result)
        
        if config_idx % 50 == 0 or config_idx == total_configs:
            elapsed = time.time() - t0
            rate = config_idx / elapsed
            remaining = (total_configs - config_idx) / rate if rate > 0 else 0
            print(f"  [{config_idx}/{total_configs}] {elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining")
    
    print(f"\nSweep complete: {len(results)} configs in {time.time()-t0:.1f}s")
    return results


def analyze_results(results: List[dict]):
    """P&L-centric analysis with per-station breakdown."""
    valid = [r for r in results if r["n_trades"] >= 30]
    if not valid:
        print("No valid configs (≥30 trades)")
        return
    
    print(f"\n{'='*80}")
    print(f"  GEFS GRID SWEEP — P&L-CENTRIC REPORT")
    print(f"  Fee: kalshi_real | {len(valid)} valid configs (≥30 trades)")
    print(f"{'='*80}")
    
    # Aggregate metrics
    pnls = [r["total_pnl"] for r in valid]
    sharpes = [r["sharpe"] for r in valid]
    pfs = [r["profit_factor"] for r in valid if r["profit_factor"] < 999]
    accs = [r["accuracy"] for r in valid]
    eces = [r["ece"] for r in valid if r["ece"] > 0]
    trades_n = [r["n_trades"] for r in valid]
    
    print(f"\n  ┌─ AGGREGATE METRICS ──────────────────────────────┐")
    print(f"  │ NET P&L:     mean=${np.mean(pnls):>+8.0f}  median=${np.median(pnls):>+8.0f}  max=${max(pnls):>+8.0f}")
    print(f"  │   Profitable: {sum(1 for p in pnls if p > 0)}/{len(pnls)} configs")
    print(f"  │ DAILY SHARPE: mean={np.mean(sharpes):.4f}  max={max(sharpes):.4f}  ≥1.0: {sum(1 for s in sharpes if s >= 1.0)}/{len(sharpes)}")
    print(f"  │ PROFIT FACTOR: mean={np.mean(pfs):.3f}  max={max(pfs):.3f}")
    print(f"  │ DIR ACCURACY: mean={np.mean(accs)*100:.2f}%  max={max(accs)*100:.2f}%")
    print(f"  │ ECE:          mean={np.mean(eces):.4f}{' (N/A)' if not eces else ''}")
    print(f"  │ N TRADES:     mean={np.mean(trades_n):.0f}  median={np.median(trades_n):.0f}")
    print(f"  └────────────────────────────────────────────────────┘")
    
    # Per-station breakdown from best config
    sorted_by_pnl = sorted(valid, key=lambda r: r["total_pnl"], reverse=True)
    best = sorted_by_pnl[0]
    print(f"\n  ┌─ PER-STATION METRICS (BEST CONFIG) ──────────────────┐")
    print(f"  │ Config: edge={best['config']['edge_threshold']}  kelly={best['config']['kelly_fraction']}  "
          f"ep_min={best['config']['entry_price_min']}  ep_max={best['config']['entry_price_max']}  "
          f"mc={best['config']['max_contracts']}  cal={best['config']['calibration_method']}")
    print(f"  │ Aggregate: PnL=${best['total_pnl']:>+8.0f}  Sharpe={best['sharpe']:.4f}  "
          f"Acc={best['accuracy']*100:.2f}%  Trades={best['n_trades']}  PF={best['profit_factor']:.2f}")
    print(f"  │ {'Station':<8} {'PnL':>10} {'Acc':>7} {'Sharpe':>8} {'PF':>6} {'Trades':>7} {'Fees':>8} {'ECE':>6}")
    print(f"  │ {'-'*8} {'-'*10} {'-'*7} {'-'*8} {'-'*6} {'-'*7} {'-'*8} {'-'*6}")
    for station in STATIONS:
        sm = best.get("station_metrics", {}).get(station, {})
        if sm.get("n_trades", 0) > 0:
            print(f"  │ {station:<8} {sm['total_pnl']:>+10.0f} {sm['accuracy']*100:>6.2f}% {sm['sharpe']:>8.4f} {sm['profit_factor']:>6.2f} {sm['n_trades']:>7} {sm['total_fees']:>8.0f} {sm['ece']:>6.4f}")
        else:
            print(f"  │ {station:<8} {'—':>10} {'—':>7} {'—':>8} {'—':>6} {'—':>7} {'—':>8} {'—':>6}")
    print(f"  └────────────────────────────────────────────────────────┘")
    
    # Top 10 by PnL
    print(f"\n  TOP 10 BY NET P&L:")
    print(f"  {'Rank':<5} {'PnL':>10} {'Sharpe':>8} {'PF':>7} {'Acc':>7} {'Trades':>7} {'ECE':>7} {'Stations':>9} {'Config'}")
    print(f"  {'-'*5} {'-'*10} {'-'*8} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*9} {'-'*40}")
    for i, r in enumerate(sorted_by_pnl[:10]):
        c = r["config"]
        conf_tag = f"e={c['edge_threshold']},k={c['kelly_fraction']},pmin={c['entry_price_min']},pmax={c['entry_price_max']},mc={c['max_contracts']},cal={c['calibration_method']}"
        print(f"  {i+1:<5} {r['total_pnl']:>+10.0f} {r['sharpe']:>8.4f} {r['profit_factor']:>7.2f} {r['accuracy']*100:>6.2f}% {r['n_trades']:>7} {r['ece']:>7.4f} {r['n_stations_active']:>9} {conf_tag}")
    
    # Top 10 by Sharpe
    sorted_by_sharpe = sorted(valid, key=lambda r: r["sharpe"], reverse=True)
    print(f"\n  TOP 10 BY SHARPE:")
    for i, r in enumerate(sorted_by_sharpe[:10]):
        c = r["config"]
        print(f"  {i+1:2d}. S={r['sharpe']:.4f}  PnL={r['total_pnl']:>+8.0f}  {r['accuracy']*100:.2f}%  T={r['n_trades']:5d}  "
              f"PF={r['profit_factor']:.3f}  stns={r['n_stations_active']}  e={c['edge_threshold']}  k={c['kelly_fraction']}  cal={c['calibration_method']}")
    
    # Best config details
    print(f"\n  BEST CONFIG (NET P&L):")
    print(f"    PnL: ${best['total_pnl']:+.0f}  Sharpe: {best['sharpe']:.4f}  PF: {best['profit_factor']:.3f}")
    print(f"    Accuracy: {best['accuracy']*100:.2f}%  Trades: {best['n_trades']}  ECE: {best['ece']:.4f}")
    print(f"    Fees: ${best['total_fees']:.0f}  Active stations: {best['n_stations_active']}")
    print(f"    Config: {json.dumps(best['config'])}")
    
    # Save
    output = {
        "metadata": {
            "n_configs": len(results),
            "valid_configs": len(valid),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ground_truth": "kalshi_settlements.db",
            "ensemble_source": "gefs_archive.db",
            "fee_model": "kalshi_real (ceil(0.07 × C × P × (1-P)) per side)",
            "sweep_type": "grid",
            "calibration_swept": True if len(set(r['config']['calibration_method'] for r in results)) > 1 else False,
        },
        "summary": {
            "mean_net_pnl": float(np.mean(pnls)),
            "median_net_pnl": float(np.median(pnls)),
            "max_net_pnl": float(max(pnls)),
            "profitable_configs": int(sum(1 for p in pnls if p > 0)),
            "mean_sharpe": float(np.mean(sharpes)),
            "max_sharpe": float(max(sharpes)),
            "configs_sharpe_ge_1": int(sum(1 for s in sharpes if s >= 1.0)),
            "mean_accuracy": float(np.mean(accs)),
            "max_accuracy": float(max(accs)),
        },
        "best_pnl": {
            "config": best["config"],
            **{k: best[k] for k in ["accuracy", "n_trades", "total_pnl", "sharpe", "profit_factor", "max_drawdown", "ece", "total_fees", "n_stations_active"]},
            "station_metrics": {
                k: v for k, v in best.get("station_metrics", {}).items()
            }
        },
        "results": [r for r in sorted_by_pnl[:200]],
    }
    
    out_path = os.path.join(SWEEP_DIR, "gefs_grid_sweep_results.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GEFS Grid Sweep")
    parser.add_argument("--quick", action="store_true", help="Reduced grid")
    parser.add_argument("--calibration", default="none", choices=["none", "isotonic", "beta"],
                        help="Calibration method")
    parser.add_argument("--skip-integrity", action="store_true",
                        help="Skip pre-run integrity check")
    args = parser.parse_args()
    
    # Pre-run integrity gate
    if not sweep_config.run_integrity_gate(skip=args.skip_integrity):
        sys.exit(1)
    
    print("Loading data...")
    print("  Kalshi settlements (finalized)...")
    finalized = load_kalshi_settlements(source_type="finalized")
    n_fin = sum(len(d) for d in finalized.values())
    print(f"    {n_fin} records across {len(finalized)} stations")
    
    print("  Kalshi settlements (historical_api)...")
    hist = load_kalshi_settlements(source_type="historical_api")
    n_hist = sum(len(d) for d in hist.values())
    print(f"    {n_hist} records across {len(hist)} stations")
    
    settlements = {}
    for station in STATIONS:
        settlements[station] = {}
        if station in finalized:
            settlements[station].update(finalized[station])
        if station in hist:
            settlements[station].update(hist[station])
    
    print(f"  Total: {sum(len(d) for d in settlements.values())} records")
    
    print("  GEFS archive...")
    gefs = load_gefs_archive()
    n_gefs = sum(len(d) for d in gefs.values())
    print(f"    {n_gefs} records across {len(gefs)} stations")
    print()
    
    results = run_grid_sweep(settlements, gefs, calibration_method=args.calibration, quick=args.quick)
    analyze_results(results)