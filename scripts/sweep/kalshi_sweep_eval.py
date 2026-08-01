#!/usr/bin/env python3
"""
kalshi_sweep_eval.py — Re-evaluate sweep against real Kalshi settlements.

This is the main sweep evaluation script. It:
1. Loads Kalshi settlement data as ground truth
2. Loads GEFS ensemble archive
3. Evaluates configs from the existing dual_objective_results.json
4. Uses kalshi_real fee model: ceil(0.07 × C × P × (1-P)) per side
5. Reports P&L-centric metrics (net P&L, daily Sharpe, profit factor, ECE)

Usage:
    python3 scripts/sweep/kalshi_sweep_eval.py
    python3 scripts/sweep/kalshi_sweep_eval.py --quick  # Evaluate top 100 only

B-Mode compliant. No AI/ML.
"""

import json
import math
import os
import sys
import time
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sweep import config as sweep_config
from sweep.data import load_kalshi_settlements, load_gefs_archive
from sweep.config import FEE_TYPE_OPTIONS, SLIPPAGE_BUDGET
from sweep.tiers import apply_edge_penalty, get_tier_info

# ── Constants ──
KALSHI_SETTLEMENTS_DB = sweep_config.DB_PATH
GEFS_DB = sweep_config.GEFS_DB_PATH
SWEEP_DIR = sweep_config.SWEEP_DIR
STATIONS = sweep_config.STATIONS

# ── Kalshi REAL Fee Model ──
def kalshi_fee(contracts: int, price: float) -> float:
    """
    Kalshi REAL fee: ceil(0.07 × C × P × (1-P)) per side.
    
    Args:
        contracts: Number of contracts
        price: Market price (0.0 - 1.0)
    
    Returns:
        Fee in dollars for one side of the trade (entry or exit)
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price))


# ── Data Loading ──

def load_all_data() -> dict:
    """Load all Kalshi settlements and GEFS data for the sweep."""
    print("Loading Kalshi settlement data...")
    settlements = load_kalshi_settlements(source_type="finalized")
    n_records = sum(len(dates) for dates in settlements.values())
    print(f"  Loaded {n_records} finalized records across {len(settlements)} stations")
    
    print("Loading GEFS archive data...")
    gefs = load_gefs_archive()
    n_gefs = sum(len(dates) for dates in gefs.values())
    print(f"  Loaded {n_gefs} GEFS records across {len(gefs)} stations")
    
    print("Loading historical API settlement data...")
    hist_settlements = load_kalshi_settlements(source_type="historical_api")
    n_hist = sum(len(dates) for dates in hist_settlements.values())
    print(f"  Loaded {n_hist} historical API records")
    
    return {
        "finalized": settlements,
        "historical_api": hist_settlements,
        "gefs": gefs,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Trading Simulation
# ═══════════════════════════════════════════════════════════════════════════

def simulate_trade(
    config: dict,
    station: str,
    date_str: str,
    gefs_mean_f: float,  # GEFS ensemble mean in °F
    actual_temp_f: float,
    prev_temp_f: Optional[float] = None,
) -> Optional[dict]:
    """
    Simulate a single trade for a given config using Kalshi REAL fee model.
    
    Key fix: DOWN predictions use entry_price = 1 - market_price
    (you buy the "no" contract, which costs 1 - P).
    
    Returns trade result dict or None if no trade signal.
    """
    # Get config parameters
    confidence_floor = config.get("confidence_floor", 0.5)
    edge_threshold = config.get("edge_threshold", 0.05)
    entry_price_min = config.get("entry_price_min", 0.05)
    entry_price_max = config.get("entry_price_max", 0.95)
    kelly_fraction = config.get("kelly_fraction", 0.5)
    max_contracts = config.get("max_contracts", 100)
    position_sizing = config.get("position_sizing_model", "fixed")
    fee_type = config.get("fee_type", "taker_and_slippage")
    slippage_budget = config.get("slippage_budget", SLIPPAGE_BUDGET)
    
    if prev_temp_f is None:
        return None
    
    # Directional prediction
    actual_direction = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
    predicted_direction = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    
    if predicted_direction == 0 or actual_direction == 0:
        return None
    
    # Confidence: how far from the boundary
    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < 0.5:
        return None
    
    confidence = min(0.99, temp_diff / 10.0)
    if confidence < confidence_floor:
        return None
    
    # ═══ Entry Price ═══
    # For UP: buy "yes" contract at price P
    # For DOWN: buy "no" contract at price (1-P)
    if predicted_direction == 1:
        market_price = min(0.95, 0.5 + confidence * 0.4)  # Derived from confidence
        entry_price = market_price  # Cost to buy "yes"
    else:
        market_price = min(0.95, 0.5 + confidence * 0.4)  # Same market price
        entry_price = 1.0 - market_price  # Cost to buy "no" (FIXED)
    
    if entry_price < entry_price_min or entry_price > entry_price_max:
        return None
    
    # ═══ Edge Calculation ═══
    # Gross edge: confidence - entry_price (model_prob - market_price for "yes")
    # For UP: edge = confidence - entry_price (buy "yes" at P, think prob > P)
    # For DOWN: edge = confidence - entry_price (buy "no" at 1-P, think prob > 1-P)
    gross_edge = confidence - entry_price
    
    # Fee per side: ceil(0.07 × C × P × (1-P)) — Kalshi taker formula
    # We use a conservative estimate before position sizing
    # Round-trip fee fraction = 2 * 0.07 * P * (1-P) ≈ 0.035 for P=0.5
    fee_frac_entry = 0.07 * market_price * (1.0 - market_price) if 0 < market_price < 1 else 0.0
    fee_frac_exit = 0.07 * 0.5 * 0.5  # conservative exit fee estimate
    
    if fee_type == "none":
        round_trip_fee_frac = 0.0
    elif fee_type == "taker_only":
        round_trip_fee_frac = fee_frac_entry
    elif fee_type == "taker_and_slippage":
        round_trip_fee_frac = fee_frac_entry + fee_frac_exit + slippage_budget
    elif fee_type == "maker":
        round_trip_fee_frac = 0.0  # Maker fee = $0.00 per E6 retail-sized limit orders
    else:
        round_trip_fee_frac = 0.0
    
    net_edge = gross_edge - round_trip_fee_frac
    
    # ═══ Microstructure Edge Penalty by Liquidity Tier (A5) ═══
    # Apply tier discount before threshold comparison
    tier_info = get_tier_info(station)
    tier_discount = tier_info["discount"]
    tier_adjusted_edge = net_edge * (1.0 - tier_discount)
    
    # Use tier-adjusted edge for threshold check (primary metric)
    if tier_adjusted_edge < edge_threshold:
        return None
    
    edge = net_edge  # Position sizing uses net edge
    
    # Position sizing (only kelly and fixed — removed tiered and stop_loss from sweep)
    if position_sizing == "kelly":
        if edge <= 0:
            return None
        kelly_pct = edge / (1.0 - entry_price) if entry_price < 1.0 else 0
        n_contracts = int(min(max_contracts, max(1, kelly_pct * kelly_fraction * 1000)))
    else:  # fixed
        n_contracts = min(max_contracts, 100)
    
    n_contracts = max(1, n_contracts)
    
    # ═══ P&L Calculation ═══
    correct = predicted_direction == actual_direction
    
    # Gross P&L: $1 per contract if correct, $0 if wrong
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    
    # Entry cost: contracts × entry_price
    cost = n_contracts * entry_price
    
    # ═══ Kalshi REAL Fee Model ═══
    # Entry fee: ceil(0.07 × C × P × (1-P)) where P is the market price
    # Exit fee: same formula, but exit is at $1 (correct) or $0 (wrong)
    entry_fee = kalshi_fee(n_contracts, market_price)
    exit_price = 1.0 if correct else 0.0
    exit_fee = kalshi_fee(n_contracts, exit_price)  # 0 for correct (P=1), 0 for wrong (P=0)
    total_fees = entry_fee + exit_fee
    
    net_pnl = gross_pnl - cost - total_fees
    
    return {
        "station": station,
        "date": date_str,
        "predicted": predicted_direction,
        "actual": actual_direction,
        "confidence": confidence,
        "market_price": market_price,
        "entry_price": entry_price,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "fee_type": fee_type,
        "round_trip_fee_frac": round_trip_fee_frac,
        "contracts": n_contracts,
        "correct": correct,
        "gross_pnl": gross_pnl,
        "cost": cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": total_fees,
        "net_pnl": net_pnl,
    }


def evaluate_config(
    config: dict,
    settlements: Dict[str, Dict[str, float]],
    gefs: Dict[str, Dict[str, dict]],
) -> dict:
    """
    Evaluate a single config across all stations and dates.
    
    Returns aggregate metrics — P&L-centric.
    """
    trades = []
    
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
            gefs_mean_f = gefs_mean_c * 9/5 + 32  # Celsius → Fahrenheit
            
            prev_temp = None
            if i > 0:
                prev_date = dates[i - 1]
                prev_temp = station_settlements.get(prev_date)
            
            trade = simulate_trade(config, station, date_str, gefs_mean_f, actual_temp, prev_temp)
            if trade is not None:
                trades.append(trade)
    
    if len(trades) < 30:
        return {
            "n_trades": 0,
            "accuracy": 0.0,
            "total_pnl": 0.0,
            "sharpe": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_pnl_per_trade": 0.0,
            "win_rate": 0.0,
            "n_daily_returns": 0,
            "total_fees": 0.0,
            "total_cost": 0.0,
        }
    
    # ── P&L-Centric Metrics ──
    correct = sum(1 for t in trades if t["correct"])
    n = len(trades)
    accuracy = correct / n
    
    # Daily P&L aggregation
    daily_pnl = defaultdict(float)
    daily_fees = defaultdict(float)
    daily_costs = defaultdict(float)
    for t in trades:
        daily_pnl[t["date"]] += t["net_pnl"]
        daily_fees[t["date"]] += t["total_fees"]
        daily_costs[t["date"]] += t["cost"]
    
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["total_fees"] for t in trades)
    total_cost = sum(t["cost"] for t in trades)
    avg_pnl = total_pnl / n if n > 0 else 0
    win_rate = correct / n if n > 0 else 0
    
    # Profit factor
    gross_wins = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gross_losses = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
    
    # Daily-return-based Sharpe (annualized) — CORRECTED: percentage returns vs bankroll
    initial_bankroll = 10000.0
    sorted_dates = sorted(daily_pnl.keys())
    daily_return_pcts = []
    bankroll = initial_bankroll
    for date in sorted_dates:
        day_pnl = daily_pnl[date]
        day_return_pct = day_pnl / bankroll if bankroll > 0 else 0.0
        daily_return_pcts.append(day_return_pct)
        bankroll += day_pnl
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
    
    # Calibration ECE (simplified: confidence bins)
    ece = 0.0
    if n >= 50:
        n_bins = 10
        bin_edges = np.linspace(0.5, 1.0, n_bins + 1)
        bin_acc = np.zeros(n_bins)
        bin_conf = np.zeros(n_bins)
        bin_counts = np.zeros(n_bins)
        for t in trades:
            conf = t["confidence"]
            if conf >= 1.0:
                conf = 0.999
            bin_idx = min(int((conf - 0.5) / 0.5 * n_bins), n_bins - 1)
            bin_acc[bin_idx] += 1 if t["correct"] else 0
            bin_conf[bin_idx] += conf
            bin_counts[bin_idx] += 1
        for i in range(n_bins):
            if bin_counts[i] > 0:
                bin_acc[i] /= bin_counts[i]
                bin_conf[i] /= bin_counts[i]
        ece = np.sum(bin_counts * np.abs(bin_acc - bin_conf)) / np.sum(bin_counts)
    
    # Average gross and net edge
    avg_gross_edge = sum(t["gross_edge"] for t in trades) / n if n else 0.0
    avg_net_edge = sum(t["net_edge"] for t in trades) / n if n else 0.0
    
    return {
        "n_trades": n,
        "accuracy": accuracy,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "avg_pnl_per_trade": avg_pnl,
        "win_rate": win_rate,
        "n_daily_returns": n_daily,
        "total_fees": total_fees,
        "total_cost": total_cost,
        "ece": ece,
        "avg_gross_edge": avg_gross_edge,
        "avg_net_edge": avg_net_edge,
    }


# ═══════════════════════════════════════════════════════════════════════════
# LHS Sampling
# ═══════════════════════════════════════════════════════════════════════════

def generate_lhs_samples(n_samples: int) -> List[dict]:
    """
    Generate LHS samples for the sweep parameters.
    
    Only 8 params are active: kelly_fraction, edge_threshold, confidence_floor,
    entry_price_max, entry_price_min, max_contracts, position_sizing_model,
    kelly_fraction.
    
    KILLED params: fee_model (always kalshi_real), stop_loss_kind, 
    stop_loss_pct, trajectory_gate_enabled, ensemble_source (always gefs_only).
    """
    try:
        from scipy.stats import qmc
        sampler = qmc.LatinHypercube(d=6, seed=42)  # 6 continuous params
        samples = sampler.random(n=n_samples)
    except ImportError:
        np.random.seed(42)
        samples = np.random.uniform(0, 1, size=(n_samples, 6))
    
    configs = []
    fee_type_indices = [0, 1, 2, 3]  # none, taker_only, taker_and_slippage, maker
    for i, sample in enumerate(samples):
        # Cycle through fee types deterministically
        ft_idx = fee_type_indices[i % 4]
        fee_type = FEE_TYPE_OPTIONS[ft_idx]
        config = {
            # Fixed params
            "fee_model": "kalshi_real",
            "ensemble_source": "gefs_only",
            "stop_loss_kind": "none",
            "stop_loss_pct": 0.0,
            "trajectory_gate_enabled": False,
            "fee_type": fee_type,
            # Sampled params
            "kelly_fraction": round(0.1 + sample[0] * 3.9, 4),
            "edge_threshold": round(0.001 + sample[1] * 0.2, 4),
            "confidence_floor": round(0.5 + sample[2] * 0.4, 4),
            "entry_price_min": round(0.01 + sample[3] * 0.49, 4),
            "entry_price_max": round(0.5 + sample[4] * 0.45, 4),
            "max_contracts": int(10 + sample[5] * 990),
            "position_sizing_model": "fixed" if sample[5] > 0.5 else "kelly",
        }
        configs.append(config)
    
    return configs


# ═══════════════════════════════════════════════════════════════════════════
# Main Sweep Runner
# ═══════════════════════════════════════════════════════════════════════════

def run_sweep(n_configs: int = 5000, quick: bool = False):
    """Run the full sweep evaluation against Kalshi ground truth."""
    data = load_all_data()
    
    settlements = {}
    for station in STATIONS:
        settlements[station] = {}
        if station in data["finalized"]:
            settlements[station].update(data["finalized"][station])
        if station in data["historical_api"]:
            settlements[station].update(data["historical_api"][station])
    
    gefs = data["gefs"]
    
    print(f"\nGenerating {n_configs} LHS configs...")
    configs = generate_lhs_samples(n_configs)
    
    results = []
    t_start = time.time()
    
    for i, config in enumerate(configs):
        result = evaluate_config(config, settlements, gefs)
        result["config"] = config
        results.append(result)
        
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed
            remaining = (n_configs - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{n_configs}] {elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining")
        
        if quick and i >= 99:
            break
    
    total_time = time.time() - t_start
    print(f"\nSweep complete: {len(results)} configs in {total_time:.1f}s")
    
    return results


def analyze_results(results: List[dict]):
    """Analyze sweep results — P&L-CENTRIC reporting."""
    valid = [r for r in results if r["n_trades"] >= 30]
    
    if not valid:
        print("No valid configs found (min 30 trades)")
        return
    
    print(f"\n{'=' * 72}")
    print(f"  KALSHI SWEEP — P&L-CENTRIC REPORT")
    print(f"  Fee model: ceil(0.07 × C × P × (1-P)) per side")
    print(f"  {len(valid)} valid configs (≥30 trades)")
    print(f"{'=' * 72}")
    
    # ── Net edge vs gross edge analysis ──
    gross_edges = []
    net_edges = []
    for r in valid:
        if r.get("n_trades", 0) > 0:
            gross_edges.append(r.get("avg_gross_edge", 0))
            net_edges.append(r.get("avg_net_edge", 0))
    
    print(f"\n  EDGE ANALYSIS (primary=net_edge, secondary=gross_edge):")
    print(f"    Gross edge:  mean={np.mean(gross_edges):.4f}  median={np.median(gross_edges):.4f}" if gross_edges else "    N/A")
    print(f"    Net edge:    mean={np.mean(net_edges):.4f}  median={np.median(net_edges):.4f}" if net_edges else "    N/A")
    
    # Fee type breakdown
    ft_counts = defaultdict(int)
    for r in valid:
        ft = r.get("config", {}).get("fee_type", "unknown")
        ft_counts[ft] += 1
    print(f"\n  FEE TYPE DISTRIBUTION:")
    for ft, cnt in sorted(ft_counts.items()):
        ft_pnls = [r["total_pnl"] for r in valid if r.get("config", {}).get("fee_type") == ft]
        ft_sharpes = [r["sharpe"] for r in valid if r.get("config", {}).get("fee_type") == ft]
        mean_pnl = np.mean(ft_pnls) if ft_pnls else 0
        mean_s = np.mean(ft_sharpes) if ft_sharpes else 0
        print(f"    {ft:<20}: {cnt:4d} configs  mean PnL=${mean_pnl:+7.0f}  mean Sharpe={mean_s:.4f}")
    
    # ── 1. Net P&L ──
    pnls = [r["total_pnl"] for r in valid]
    best_pnl_idx = int(np.argmax(pnls))
    best_pnl = valid[best_pnl_idx]
    
    print(f"\n  NET P&L:")
    print(f"    Mean:      ${np.mean(pnls):+.0f}")
    print(f"    Median:    ${np.median(pnls):+.0f}")
    print(f"    Max:       ${max(pnls):+.0f}")
    print(f"    Min:       ${min(pnls):+.0f}")
    print(f"    Profitable: {sum(1 for p in pnls if p > 0)}/{len(pnls)} configs")
    
    # ── 2. Daily Sharpe ──
    sharpes = [r["sharpe"] for r in valid]
    print(f"\n  DAILY SHARPE (annualized, 252d):")
    print(f"    Mean:      {np.mean(sharpes):.4f}")
    print(f"    Median:    {np.median(sharpes):.4f}")
    print(f"    Max:       {max(sharpes):.4f}")
    print(f"    Configs ≥ 1.0: {sum(1 for s in sharpes if s >= 1.0)}/{len(sharpes)}")
    print(f"    Configs ≥ 0.5: {sum(1 for s in sharpes if s >= 0.5)}/{len(sharpes)}")
    
    # ── 3. Profit Factor ──
    pfs = [r["profit_factor"] for r in valid if r["profit_factor"] != float('inf')]
    print(f"\n  PROFIT FACTOR:")
    print(f"    Mean:      {np.mean(pfs):.3f}")
    print(f"    Median:    {np.median(pfs):.3f}")
    print(f"    Max:       {max(pfs):.3f}")
    print(f"    Configs ≥ 1.0: {sum(1 for p in pfs if p >= 1.0)}/{len(pfs)}")
    print(f"    Configs ≥ 2.0: {sum(1 for p in pfs if p >= 2.0)}/{len(pfs)}")
    
    # ── 4. Directional Accuracy (diagnostic only) ──
    accs = [r["accuracy"] for r in valid]
    print(f"\n  DIRECTIONAL ACCURACY (diagnostic):")
    print(f"    Mean:      {np.mean(accs)*100:.2f}%")
    print(f"    Median:    {np.median(accs)*100:.2f}%")
    print(f"    Max:       {max(accs)*100:.2f}%")
    print(f"    Configs ≥ 60%: {sum(1 for a in accs if a >= 0.60)}/{len(accs)}")
    
    # ── 5. Calibration ECE ──
    eces = [r["ece"] for r in valid if r["ece"] > 0]
    print(f"\n  CALIBRATION ECE:")
    print(f"    Mean:      {np.mean(eces):.4f}" if eces else "    N/A")
    print(f"    Median:    {np.median(eces):.4f}" if eces else "")
    
    # ── 6. Trade Stats ──
    trades = [r["n_trades"] for r in valid]
    fees = [r["total_fees"] for r in valid]
    costs = [r["total_cost"] for r in valid]
    print(f"\n  TRADE STATS:")
    print(f"    N trades:     mean={np.mean(trades):.0f}  median={np.median(trades):.0f}")
    print(f"    Total fees:   mean=${np.mean(fees):.0f}  median=${np.median(fees):.0f}")
    print(f"    Total cost:   mean=${np.mean(costs):.0f}  median=${np.median(costs):.0f}")
    print(f"    Fee/trade:    mean=${np.mean(fees)/np.mean(trades):.4f}" if np.mean(trades) > 0 else "")
    
    # ── Top 10 by Net P&L ──
    sorted_by_pnl = sorted(valid, key=lambda r: r["total_pnl"], reverse=True)
    print(f"\n  TOP 10 BY NET P&L:")
    print(f"  {'Rank':<5} {'PnL':>10} {'Sharpe':>8} {'PF':>8} {'Acc':>7} {'Trades':>7} {'ECE':>8} {'Config (edge,conf,pos)'}")
    print(f"  {'-'*5} {'-'*10} {'-'*8} {'-'*8} {'-'*7} {'-'*7} {'-'*8} {'-'*30}")
    for i, r in enumerate(sorted_by_pnl[:10]):
        c = r["config"]
        conf_tag = f"{c.get('confidence_floor',0):.2f},{c.get('edge_threshold',0):.3f},{c.get('position_sizing_model','?')[:4]}"
        print(f"  {i+1:<5} {r['total_pnl']:>+10.0f} {r['sharpe']:>8.4f} {r['profit_factor']:>8.3f} {r['accuracy']*100:>6.2f}% {r['n_trades']:>7} {r['ece']:>8.4f} {conf_tag}")
    
    # ── Top 10 by Sharpe ──
    sorted_by_sharpe = sorted(valid, key=lambda r: r["sharpe"], reverse=True)
    print(f"\n  TOP 10 BY SHARPE:")
    for i, r in enumerate(sorted_by_sharpe[:10]):
        c = r["config"]
        print(f"  {i+1:2d}. S={r['sharpe']:.4f}  PnL={r['total_pnl']:>+8.0f}  "
              f"{r['accuracy']*100:.2f}%  T={r['n_trades']:5d}  "
              f"PF={r['profit_factor']:.3f}  conf={c.get('confidence_floor',0):.2f}  "
              f"edge={c.get('edge_threshold',0):.3f}")
    
    # ── Best config ──
    best_pnl_config = sorted_by_pnl[0]
    print(f"\n  BEST CONFIG (NET P&L):")
    print(f"    PnL: ${best_pnl_config['total_pnl']:+.0f}  Sharpe: {best_pnl_config['sharpe']:.4f}")
    print(f"    Gross Edge: {best_pnl_config.get('avg_gross_edge', 0):.4f}  Net Edge: {best_pnl_config.get('avg_net_edge', 0):.4f}")
    print(f"    Fee Type: {best_pnl_config.get('config', {}).get('fee_type', '?')}")
    print(f"    Accuracy: {best_pnl_config['accuracy']*100:.2f}%  Trades: {best_pnl_config['n_trades']}")
    print(f"    Profit Factor: {best_pnl_config['profit_factor']:.3f}  ECE: {best_pnl_config['ece']:.4f}")
    print(f"    Fees: ${best_pnl_config['total_fees']:.0f}  Cost: ${best_pnl_config['total_cost']:.0f}")
    print(f"    Config: {json.dumps(best_pnl_config['config'], indent=6)}")
    
    # ── Save results ──
    output = {
        "metadata": {
            "n_configs": len(results),
            "valid_configs": len(valid),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ground_truth": "kalshi_settlements.db",
            "ensemble_source": "gefs_archive.db",
            "fee_model": "kalshi_real (ceil(0.07 × C × P × (1-P)) per side)",
            "reporting": "P&L-centric (net P&L, daily Sharpe, PF, ECE)",
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
            "config": best_pnl_config["config"],
            **{k: best_pnl_config[k] for k in ["accuracy", "n_trades", "total_pnl", "sharpe", "profit_factor", "max_drawdown", "ece", "total_fees", "total_cost", "win_rate"]}
        },
        "results": [r for r in sorted_by_pnl[:500]],  # Top 500 by PnL
    }
    
    out_path = os.path.join(SWEEP_DIR, "kalshi_sweep_results.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi sweep evaluation")
    parser.add_argument("--n-configs", type=int, default=5000, help="Number of configs")
    parser.add_argument("--quick", action="store_true", help="Quick mode: top 100 only")
    parser.add_argument("--skip-integrity", action="store_true", help="Skip pre-run integrity check")
    args = parser.parse_args()
    
    # Pre-run integrity gate
    if not sweep_config.run_integrity_gate(skip=args.skip_integrity):
        sys.exit(1)
    
    results = run_sweep(n_configs=args.n_configs, quick=args.quick)
    analyze_results(results)