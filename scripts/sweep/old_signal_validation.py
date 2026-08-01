#!/usr/bin/env python3
"""
Old 5-Signal Pipeline Validation against Kalshi Settlements.

Validates the legacy 5-signal pipeline:
  - calendar_climatology, gaussian, gaussian_v2, pressure_delta, forecast_disagreement

Reports per-signal accuracy, net P&L with kalshi_real fees,
and comparison to GEFS ensemble fraction.

Usage:
    python3 scripts/sweep/old_signal_validation.py

Output:
    data/sweep/old_signal_validation_results.json
"""

import json
import math
import os
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

import sys
from pathlib import Path

# Add both repo root and scripts dir to path
_script = Path(__file__).resolve()  # scripts/sweep/old_signal_validation.py
_scripts_dir = str(_script.parent.parent)   # scripts/
_repo_root = str(_script.parent.parent.parent)  # repo root
sys.path.insert(0, _repo_root)
sys.path.insert(0, _scripts_dir)

from sweep import config as sweep_config
from core.signals.calendar_climatology_signal import CalendarClimatologySignal
from core.signals.gaussian_signal import GaussianSignal
from core.signals.gaussian_v2_signal import GaussianV2Signal
from core.signals.pressure_delta_signal import PressureDeltaSignal
from core.signals.forecast_disagreement_signal import ForecastDisagreementSignal

STATIONS = sweep_config.STATIONS
WEATHER_DB = os.path.join(sweep_config.BASE_DIR, "data", "weather_data.db")
SETTLEMENTS_DB = sweep_config.DB_PATH

# Fee: kalshi_real (ceil(0.07 × C × P × (1-P)) per side)
def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price))


# ── Load Kalshi Settlements ──
def load_settlements() -> Dict[str, Dict[str, float]]:
    conn = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    rows = cur.fetchall()
    conn.close()
    result = defaultdict(dict)
    for s, d, t in rows:
        if t is not None:
            result[s][d] = float(t)
    return dict(result)


# ── Load METAR Temperature Data per Station ──
def load_metar_data(station: str, conn) -> List[Dict]:
    """Load daily METAR observations for a station as day dicts."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(temp_f) as avg, MAX(dewpoint_f) as dewpoint,
               AVG(pressure_mb) as pressure, AVG(wind_speed_kt) as wind_speed,
               AVG(wind_direction_deg) as wind_dir
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL AND temp_f < 200
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'avg': r[3],
            'dewpoint': r[4], 'pressure': r[5], 'wind_speed': r[6],
            'wind_dir': r[7],
        })
    return days


# ── Trade Simulation ──
def simulate_trade(pred_dir: str, confidence: float, actual_dir: str,
                   entry_price: float = None) -> Optional[dict]:
    if pred_dir is None or actual_dir is None:
        return None
    if actual_dir == 'flat' or pred_dir == 'flat':
        return None
    if confidence <= 0.5:
        return None
    
    pred_up = 1 if pred_dir == 'up' else -1
    actual_up = 1 if actual_dir == 'up' else -1
    if actual_up == 0:
        return None
    
    # Market price from confidence
    market_price = min(0.95, 0.5 + (confidence - 0.5) * 0.8)
    if pred_up == 1:
        entry_price_used = market_price
    else:
        entry_price_used = 1.0 - market_price
    
    # Simple fixed sizing
    contracts = 100
    
    correct = pred_up == actual_up
    gross_pnl = contracts * (1.0 if correct else 0.0)
    cost = contracts * entry_price_used
    
    entry_fee = kalshi_fee(contracts, market_price)
    exit_fee = kalshi_fee(contracts, 1.0 if correct else 0.0)
    total_fees = entry_fee + exit_fee
    
    net_pnl = gross_pnl - cost - total_fees
    
    return {
        'correct': correct, 'net_pnl': net_pnl,
        'pred_dir': pred_dir, 'actual_dir': actual_dir,
        'confidence': confidence, 'entry_price': entry_price_used,
        'contracts': contracts, 'fees': total_fees,
    }


# ── Signal Validation ──
def validate_signal(signal_name: str, signal_obj,
                    settlements: Dict[str, Dict[str, float]],
                    metar_conn) -> dict:
    """Run a signal against all stations and return per-signal metrics."""
    all_trades = []
    per_station_trades = defaultdict(list)
    
    for station in STATIONS:
        days = load_metar_data(station, metar_conn)
        if len(days) < 50:
            continue
        
        station_settlements = settlements.get(station, {})
        
        for idx in range(signal_obj.min_lookback, len(days)):
            date_str = days[idx]['date']
            if date_str not in station_settlements:
                continue
            
            actual_temp = station_settlements[date_str]
            
            # Get actual direction from settlement data
            prev_date = days[idx-1]['date']
            if prev_date in station_settlements:
                prev_temp = station_settlements[prev_date]
                actual_diff = actual_temp - prev_temp
                actual_dir = 'up' if actual_diff > 0 else ('down' if actual_diff < 0 else 'flat')
            else:
                continue
            
            # Evaluate signal
            try:
                pred_dir, conf = signal_obj.evaluate(idx, days)
            except Exception:
                continue
            
            if pred_dir is None:
                continue
            
            trade = simulate_trade(pred_dir, conf, actual_dir)
            if trade:
                all_trades.append(trade)
                per_station_trades[station].append(trade)
    
    # Compute metrics
    agg = compute_signal_metrics(all_trades)
    per_station = {}
    for station in STATIONS:
        m = compute_signal_metrics(per_station_trades.get(station, []), min_trades=3)
        if m['n_trades'] > 0:
            per_station[station] = m
    
    return {**agg, 'per_station': per_station, 'signal_name': signal_name}


def compute_signal_metrics(trades: List[dict], min_trades: int = 10) -> dict:
    if len(trades) < min_trades:
        return {'n_trades': 0, 'accuracy': 0.0, 'total_pnl': 0.0,
                'sharpe': 0.0, 'profit_factor': 0.0, 'total_fees': 0.0}
    
    n = len(trades)
    correct = sum(1 for t in trades if t['correct'])
    accuracy = correct / n
    
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t.get('date', t.get('date_str', ''))[:10]] += t['net_pnl']
    daily_returns = list(daily_pnl.values())
    n_daily = len(daily_returns)
    
    total_pnl = sum(t['net_pnl'] for t in trades)
    total_fees = sum(t['fees'] for t in trades)
    
    gross_wins = sum(t['net_pnl'] for t in trades if t['net_pnl'] > 0)
    gross_losses = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else (999.0 if gross_wins > 0 else 0.0)
    
    if n_daily > 1:
        daily_mean = np.mean(daily_returns)
        daily_std = np.std(daily_returns, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    else:
        sharpe = 0.0
    
    return {
        'n_trades': n, 'accuracy': accuracy, 'total_pnl': total_pnl,
        'sharpe': float(sharpe), 'profit_factor': float(pf),
        'total_fees': total_fees,
    }


def load_gefs_comparison(settlements: Dict[str, Dict[str, float]]) -> dict:
    """Compute GEFS ensemble fraction for comparison."""
    conn = sqlite3.connect(sweep_config.GEFS_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT target_date, station, ensemble_mean
        FROM gefs_archive WHERE step = 24 ORDER BY target_date
    """)
    rows = cur.fetchall()
    conn.close()
    
    all_trades = []
    for target_date, station, mean_c in rows:
        if mean_c is None:
            continue
        if station not in settlements or target_date not in settlements[station]:
            continue
        
        # Find previous date settlement
        station_dates = sorted(settlements[station].keys())
        try:
            idx = station_dates.index(target_date)
        except ValueError:
            continue
        if idx == 0:
            continue
        
        prev_date = station_dates[idx - 1]
        actual_temp = settlements[station][target_date]
        prev_temp = settlements[station][prev_date]
        
        actual_dir = actual_temp - prev_temp
        if actual_dir == 0:
            continue
        
        mean_f = mean_c * 9/5 + 32
        pred_dir = mean_f - prev_temp
        if pred_dir == 0:
            continue
        
        correct = (pred_dir > 0) == (actual_dir > 0)
        # Simulate trade with kalshi_real fees
        market_price = 0.75  # Fixed market price for comparison
        entry_price = market_price if pred_dir > 0 else 1.0 - market_price
        contracts = 100
        gross_pnl = contracts * (1.0 if correct else 0.0)
        cost = contracts * entry_price
        entry_fee = kalshi_fee(contracts, market_price)
        exit_fee = kalshi_fee(contracts, 1.0 if correct else 0.0)
        net_pnl = gross_pnl - cost - entry_fee - exit_fee
        all_trades.append({'correct': correct, 'station': station, 'date': target_date, 'net_pnl': net_pnl, 'fees': entry_fee + exit_fee})
    
    return compute_signal_metrics(all_trades, min_trades=1)


def main():
    print("=" * 72)
    print("  Old 5-Signal Pipeline Validation")
    print("  Fee: kalshi_real (ceil(0.07 × C × P × (1-P)) per side)")
    print(f"  Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)
    
    print("\nLoading Kalshi settlements...")
    settlements = load_settlements()
    print(f"  {sum(len(v) for v in settlements.values())} records across {len(settlements)} stations")
    
    print("\nConnecting to METAR weather database...")
    if not os.path.exists(WEATHER_DB):
        print("  ERROR: weather_data.db not found!")
        sys.exit(1)
    metar_conn = sqlite3.connect(WEATHER_DB)
    
    # Initialize signals
    signals = {
        'calendar_climatology': CalendarClimatologySignal(),
        'gaussian': GaussianSignal(),
        'gaussian_v2': GaussianV2Signal(),
        'pressure_delta': PressureDeltaSignal(),
        'forecast_disagreement': ForecastDisagreementSignal(),
    }
    
    # Validate each signal
    results = {}
    for name, signal_obj in signals.items():
        print(f"\n  Evaluating {name}...", end=' ', flush=True)
        try:
            result = validate_signal(name, signal_obj, settlements, metar_conn)
            results[name] = result
            print(f"{result['n_trades']} trades, {result['accuracy']*100:.2f}% acc, "
                  f"PnL=${result['total_pnl']:+.0f}, Sharpe={result['sharpe']:.4f}")
        except Exception as e:
            print(f"ERROR: {e}")
            results[name] = {'n_trades': 0, 'accuracy': 0.0, 'total_pnl': 0.0,
                            'sharpe': 0.0, 'profit_factor': 0.0, 'error': str(e)}
    
    # GEFS comparison
    print("\n  GEFS ensemble fraction (directional)...", end=' ', flush=True)
    gefs_metrics = load_gefs_comparison(settlements)
    print(f"{gefs_metrics['n_trades']} trades, {gefs_metrics['accuracy']*100:.2f}% acc, "
          f"PnL=${gefs_metrics['total_pnl']:+.0f}, Sharpe={gefs_metrics['sharpe']:.4f}")
    
    metar_conn.close()
    
    # ── REPORT ──
    print(f"\n{'='*72}")
    print(f"  SIGNAL VALIDATION REPORT")
    print(f"{'='*72}")
    print(f"  {'Signal':<28} {'Trades':>7} {'Acc':>7} {'PnL':>10} {'Sharpe':>8} {'PF':>7}")
    print(f"  {'-'*28} {'-'*7} {'-'*7} {'-'*10} {'-'*8} {'-'*7}")
    
    for name, r in results.items():
        print(f"  {name:<28} {r['n_trades']:>7} {r['accuracy']*100:>6.2f}% "
              f"{r['total_pnl']:>+10.0f} {r['sharpe']:>8.4f} {r.get('profit_factor',0.0):>7.2f}")
    
    print(f"  {'GEFS ensemble (directional)':<28} {gefs_metrics['n_trades']:>7} "
          f"{gefs_metrics['accuracy']*100:>6.2f}% "
          f"{gefs_metrics['total_pnl']:>+10.0f} {gefs_metrics['sharpe']:>8.4f} "
          f"{gefs_metrics.get('profit_factor',0.0):>7.2f}")
    
    print(f"\n  Per-station breakdown for best signal:")
    best_signal = max(results.items(), key=lambda x: x[1].get('total_pnl', -9999))
    best_name, best_result = best_signal
    print(f"  Best: {best_name}")
    print(f"  {'Station':<8} {'Trades':>7} {'Acc':>7} {'PnL':>10} {'Sharpe':>8} {'Fees':>8}")
    print(f"  {'-'*8} {'-'*7} {'-'*7} {'-'*10} {'-'*8} {'-'*8}")
    for station in STATIONS:
        sm = best_result.get('per_station', {}).get(station, {})
        if sm.get('n_trades', 0) > 0:
            print(f"  {station:<8} {sm['n_trades']:>7} {sm['accuracy']*100:>6.2f}% "
                  f"{sm['total_pnl']:>+10.0f} {sm['sharpe']:>8.4f} {sm['total_fees']:>8.0f}")
    
    # Save results
    output = {
        'metadata': {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'signals_tested': list(signals.keys()),
            'fee_model': 'kalshi_real',
            'ground_truth': 'kalshi_settlements.db',
            'data_source': 'weather_data.db (metar_observations)',
        },
        'signal_results': {
            k: {kk: vv for kk, vv in v.items() if kk != 'per_station'}
            for k, v in results.items()
        },
        'gefs_comparison': gefs_metrics,
        'best_signal': best_name,
        'best_signal_details': {
            'name': best_name,
            'metrics': {k: v for k, v in best_result.items() if k != 'per_station'},
            'per_station': {
                k: v for k, v in best_result.get('per_station', {}).items()
            },
        },
    }
    
    out_path = os.path.join(sweep_config.SWEEP_DIR, 'old_signal_validation_results.json')
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved to: {out_path}")
    print(f"{'='*72}")


if __name__ == '__main__':
    main()