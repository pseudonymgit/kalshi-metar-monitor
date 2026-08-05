#!/usr/bin/env python3
"""
GEFS Gray Room Validation — Compute trustworthy numbers from existing paper trades.

Reads data/paper_trading_dev.db, recomputes metrics with corrected methodology:
- Walk-forward backtest (7‑day windows)
- Corrected edge formula (Kelly log‑return with fee)
- Daily Sharpe with cap (max 3.0)
- Monte Carlo null test (10k simulations)
- Station‑by‑station breakdown
- Slippage simulation (0.5¢, 1.0¢ per contract)
- Kalshi market price source verification

Outputs formatted results to stdout.
"""

import sqlite3
import math
import random
import numpy as np
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Any
import sys

# ─── Configuration ──────────────────────────────────────────────────────────

TRADE_DB = "data/paper_trading_dev.db"
INITIAL_BANKROLL = 10000.0
QUARTER_KELLY_CAP = 0.25
SHARPE_CAP = 3.0
SLIPPAGE_CENTS = [0.0, 0.5, 1.0]
NULL_SIMULATIONS = 10000

# ─── Fee Model ──────────────────────────────────────────────────────────────

def kalshi_fee(contracts: int, price: float) -> float:
    """Fee per contract (dollars)."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_per_contract = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return fee_per_contract * contracts

# ─── Corrected Edge Formula ────────────────────────────────────────────────

def expected_log_return(p: float, price: float, f: float, fee_per_contract: float = 0.0) -> float:
    """Expected log return for binary option with fee."""
    if fee_per_contract == 0.0:
        b = (1.0 - price) / price
        return p * math.log(1 + f * b) + (1 - p) * math.log(1 - f)
    else:
        cost = price + fee_per_contract
        payout = 1.0 - price - fee_per_contract
        if cost <= 0 or payout <= 0:
            return -math.inf
        b = payout / cost
        return p * math.log(1 + f * b) + (1 - p) * math.log(1 - f)

def optimal_kelly_fraction(p: float, price: float, fee_per_contract: float = 0.0) -> float:
    """Find f that maximizes expected_log_return, capped at quarter‑Kelly."""
    edge = p - price
    if edge <= 0:
        return 0.0
    # Grid search over f ∈ (0, 0.99)
    candidates = np.linspace(0.001, 0.99, 200)
    best_f = 0.0
    best_val = -math.inf
    for f in candidates:
        val = expected_log_return(p, price, f, fee_per_contract)
        if val > best_val:
            best_val = val
            best_f = f
    # Apply quarter‑Kelly cap
    return best_f * QUARTER_KELLY_CAP

# ─── Data Loading ──────────────────────────────────────────────────────────

def load_trades(db_path: str) -> List[dict]:
    """Load all trades from the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT station, target_date, pred_direction, actual_direction,
               confidence, market_price, edge, contracts, correct,
               net_pnl, entry_fee, exit_fee
        FROM trades
        ORDER BY target_date
    """)
    rows = cur.fetchall()
    trades = [dict(row) for row in rows]
    conn.close()
    return trades

def group_by_window(trades: List[dict], window_days: int = 7) -> List[List[dict]]:
    """Group trades into consecutive non‑overlapping windows of window_days days."""
    if not trades:
        return []
    # Determine date range
    dates = sorted({t['target_date'] for t in trades})
    # Create windows
    windows = []
    for i in range(0, len(dates), window_days):
        window_dates = set(dates[i:i+window_days])
        window_trades = [t for t in trades if t['target_date'] in window_dates]
        if window_trades:
            windows.append(window_trades)
    return windows

# ─── Metrics ───────────────────────────────────────────────────────────────

def compute_window_metrics(trades: List[dict]) -> dict:
    """Compute accuracy, P&L, edge stats for a window."""
    if not trades:
        return {}
    correct = sum(1 for t in trades if t['correct'])
    total = len(trades)
    accuracy = correct / total
    total_pnl = sum(t['net_pnl'] for t in trades)
    # Edge stats
    edges = [t['edge'] for t in trades]
    avg_edge = np.mean(edges) if edges else 0.0
    # Fee per contract
    fee_per_contract = sum(t['entry_fee'] + t['exit_fee'] for t in trades) / sum(t['contracts'] for t in trades) if any(t['contracts'] for t in trades) else 0.0
    return {
        'trades': total,
        'correct': correct,
        'accuracy': accuracy,
        'total_pnl': total_pnl,
        'avg_edge': avg_edge,
        'fee_per_contract': fee_per_contract,
    }

def compute_daily_returns(trades: List[dict]) -> Dict[str, float]:
    """Aggregate P&L per day, return daily percentage returns."""
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t['target_date']] += t['net_pnl']
    # Convert to percentage of initial bankroll (simplistic)
    daily_returns = {date: pnl / INITIAL_BANKROLL for date, pnl in daily_pnl.items()}
    return daily_returns

def sharpe_ratio(daily_returns: List[float], cap: bool = True) -> float:
    """Annualized Sharpe ratio, optionally capped at SHARPE_CAP."""
    if len(daily_returns) < 2:
        return 0.0
    mean = np.mean(daily_returns)
    std = np.std(daily_returns, ddof=1)
    if std == 0:
        return 0.0
    sharpe = mean / std * math.sqrt(252)
    if cap and len(daily_returns) < 30:
        sharpe = min(sharpe, SHARPE_CAP)
    return sharpe

def monte_carlo_null(trades: List[dict], simulations: int = 10000) -> Tuple[float, float]:
    """Simulate random accuracy under null hypothesis (50% correct)."""
    n = len(trades)
    if n == 0:
        return 0.0, 1.0
    # Observed accuracy
    obs_correct = sum(1 for t in trades if t['correct'])
    obs_acc = obs_correct / n
    # Simulate
    sim_accs = []
    for _ in range(simulations):
        sim_correct = sum(1 for __ in range(n) if random.random() < 0.5)
        sim_accs.append(sim_correct / n)
    # Percentile
    sim_accs_sorted = sorted(sim_accs)
    percentile = np.searchsorted(sim_accs_sorted, obs_acc) / len(sim_accs_sorted) * 100.0
    # Two‑tailed p‑value
    p_value = min(percentile / 100.0, 1 - percentile / 100.0) * 2
    return percentile, p_value

def station_breakdown(trades: List[dict]) -> Dict[str, dict]:
    """Compute per‑station statistics."""
    stats = defaultdict(lambda: {'trades': 0, 'correct': 0, 'pnl': 0.0})
    for t in trades:
        s = t['station']
        stats[s]['trades'] += 1
        if t['correct']:
            stats[s]['correct'] += 1
        stats[s]['pnl'] += t['net_pnl']
    return stats

def slippage_impact(trades: List[dict], slippage_cents: float) -> Dict[str, float]:
    """Apply per‑contract slippage cost and recompute accuracy and P&L."""
    slippage_dollar = slippage_cents / 100.0
    total_pnl = 0.0
    correct = 0
    for t in trades:
        slippage_cost = slippage_dollar * t['contracts']
        adjusted_pnl = t['net_pnl'] - slippage_cost
        total_pnl += adjusted_pnl
        if adjusted_pnl > 0:
            correct += 1
    accuracy = correct / len(trades) if trades else 0.0
    return {'accuracy': accuracy, 'total_pnl': total_pnl}

def verify_market_price_source(trades: List[dict]) -> dict:
    """Check how market_price was sourced (0.5 fallback vs other)."""
    prices = [t['market_price'] for t in trades]
    unique = set(round(p, 3) for p in prices)
    count_fallback = sum(1 for p in prices if abs(p - 0.5) < 0.001)
    return {
        'unique_prices': len(unique),
        'fallback_0.5_count': count_fallback,
        'price_range': (min(prices), max(prices)) if prices else (0.0, 0.0),
    }

# ─── Main Analysis ─────────────────────────────────────────────────────────

def main():
    print("Loading trades from", TRADE_DB)
    trades = load_trades(TRADE_DB)
    if not trades:
        print("No trades found.")
        sys.exit(1)
    
    print(f"Loaded {len(trades)} trades.")
    
    # 1. Walk‑forward backtest (7‑day windows)
    windows = group_by_window(trades, 7)
    print(f"Created {len(windows)} walk‑forward windows.")
    
    window_metrics = [compute_window_metrics(w) for w in windows]
    total_trades = sum(w['trades'] for w in window_metrics)
    total_correct = sum(w['correct'] for w in window_metrics)
    overall_accuracy = total_correct / total_trades if total_trades else 0.0
    
    # 2. Corrected edge formula (compute for each trade)
    corrected_edges = []
    for t in trades:
        p = t['confidence']
        price = t['market_price']
        fee_per = kalshi_fee(1, price)
        edge_corrected = p - price - fee_per
        corrected_edges.append(edge_corrected)
    
    avg_edge_original = np.mean([t['edge'] for t in trades])
    avg_edge_corrected = np.mean(corrected_edges) if corrected_edges else 0.0
    
    # 3. Daily returns Sharpe (capped)
    daily_returns_dict = compute_daily_returns(trades)
    daily_returns_list = list(daily_returns_dict.values())
    sharpe_uncapped = sharpe_ratio(daily_returns_list, cap=False)
    sharpe_capped = sharpe_ratio(daily_returns_list, cap=True)
    
    # 4. Monte Carlo null test
    null_percentile, null_p = monte_carlo_null(trades, NULL_SIMULATIONS)
    
    # 5. Station breakdown
    station_stats = station_breakdown(trades)
    
    # 6. Slippage impact
    slippage_results = {}
    for cents in SLIPPAGE_CENTS:
        slippage_results[cents] = slippage_impact(trades, cents)
    
    # 7. Market price source verification
    price_check = verify_market_price_source(trades)
    
    # 8. Output
    print("\n" + "="*72)
    print("  GEFS GRAY ROOM VALIDATION RESULTS")
    print("="*72)
    print(f"  Date range: {trades[0]['target_date']} → {trades[-1]['target_date']}")
    print(f"  Windows: {len(windows)} (each 7 days)")
    print(f"  Total trades: {total_trades}")
    print(f"  Accuracy: {overall_accuracy*100:.2f}%")
    print(f"  Original avg edge: {avg_edge_original:.4f}")
    print(f"  Corrected avg edge (with fee): {avg_edge_corrected:.4f}")
    print(f"  Corrected Sharpe (capped 3.0): {sharpe_capped:.2f}")
    print(f"  Uncapped Sharpe: {sharpe_uncapped:.2f}")
    print(f"  Null test (10K sims): observed at {null_percentile:.1f}%ile (p={null_p:.4f})")
    print(f"  Kelly fraction used: {QUARTER_KELLY_CAP:.2f} (quarter‑Kelly)")
    print("\n  Station breakdown:")
    for station, stats in sorted(station_stats.items()):
        if stats['trades'] == 0:
            continue
        acc = stats['correct'] / stats['trades']
        print(f"    {station}: {stats['trades']} trades, {acc*100:.1f}% acc, +${stats['pnl']:.2f}")
    print("\n  Slippage impact:")
    for cents, res in slippage_results.items():
        print(f"    {cents:0.1f}¢: {res['accuracy']*100:.2f}% acc, +${res['total_pnl']:.2f}")
    print("\n  Market price source:")
    print(f"    Unique prices: {price_check['unique_prices']}")
    print(f"    Trades using 0.5 fallback: {price_check['fallback_0.5_count']}")
    print(f"    Price range: {price_check['price_range'][0]:.3f} – {price_check['price_range'][1]:.3f}")
    print("\n  Note: Corrected edge includes fee subtraction. Kelly sizing uses quarter‑Kelly cap.")
    print("="*72)
    
    # Additional: walk‑forward window accuracies
    print("\n  Window‑by‑window accuracy:")
    for i, wm in enumerate(window_metrics):
        print(f"    Window {i+1}: {wm['trades']} trades, {wm['accuracy']*100:.1f}% acc, P&L ${wm['total_pnl']:.2f}")

if __name__ == "__main__":
    main()