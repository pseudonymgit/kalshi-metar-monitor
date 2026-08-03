#!/usr/bin/env python3
"""
Cascade v1 Research — Beta-binomial Layer 1 Uncertainty-Weighted Cascade.

The Bayesian cascade is the highest-impact architectural fix but depends on CLI
validation. For this B-mode loop, this is a research spike that implements the
Beta-binomial Layer 1 as a standalone test and measures whether the uncertainty-
weighted cascade improves over the point-estimate baseline.

The key hypothesis: propagating uncertainty through the cascade (via Beta
distributions instead of point estimates) improves decision quality by
appropriately discounting low-confidence predictions.

Usage:
    python3 scripts/research_cascade_v1.py [--days 365] [--start 2025-08-03]

Output:
    docs/weather-engine/backtests/cascade_v1_research_20260803.json

Author: Gilfoyle (dispatch Aug 3, 2026, B-mode post-Gray-Room)
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

# ─── Paths ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Import baseline helpers
from scripts.bmode_p1_backtest import (
    kalshi_fee,
    STATIONS,
    INITIAL_BANKROLL,
)

# ─── Constants ───────────────────────────────────────────────────────────────

# Beta distribution: prior parameters
ALPHA_PRIOR = 1.0
BETA_PRIOR = 1.0  # Uniform prior

# Effective sample size: n_eff = n / (1 + (n-1) * rho)
# Using realistic estimates from Gray Room Expert 1 (EL-1):
# At rho = 0.60, n = 31: n_eff = 31 / (1 + 30 * 0.6) = 1.23
# We'll test with rho = 0.60 (realistic) and rho = 0.0 (naive, no correlation)
ESTIMATED_RHO = 0.60  # Average pairwise member correlation

# P&L config (matching baseline)
EDGE_THRESHOLD = 0.02
KELLY_FRACTION = 0.50
ENTRY_PRICE_MIN = 0.15
ENTRY_PRICE_MAX = 0.70
MAX_CONTRACTS = 175
MARKET_PRICE = 0.50

EDGE_TIERS = [
    (0.10, 1.00),
    (0.06, 0.75),
    (0.03, 0.50),
    (0.00, 0.00),
]

# Baseline for comparison
BASELINE = {
    "accuracy": 66.17,
    "pnl": 50165.52,
    "sharpe": 11.36,
    "trades": 2096,
    "source": "calibrated GEFS only (bmode_p1_calib-dir)",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Beta-binomial Layer 1
# ═══════════════════════════════════════════════════════════════════════════════

def compute_effective_n(n_members: int, rho: float = ESTIMATED_RHO) -> float:
    """
    Compute effective sample size accounting for member correlation.
    n_eff = n / (1 + (n-1) * rho)
    """
    if rho <= 0:
        return float(n_members)
    return n_members / (1.0 + (n_members - 1) * rho)


def beta_posterior(
    n_exceed: int, n_total: int, rho: float = ESTIMATED_RHO
) -> Tuple[float, float, float, float]:
    """
    Compute Beta posterior parameters for the exceedance probability.

    Args:
        n_exceed: Number of ensemble members exceeding the boundary
        n_total: Total number of ensemble members
        rho: Average pairwise member correlation

    Returns:
        (alpha, beta, mean, variance)
    """
    n_eff = compute_effective_n(n_total, rho)
    k_eff = n_exceed * (n_eff / n_total)  # Scale exceedance count to effective size

    # Posterior: Beta(alpha_prior + k_eff, beta_prior + n_eff - k_eff)
    alpha = ALPHA_PRIOR + k_eff
    beta = BETA_PRIOR + n_eff - k_eff

    # Mean and variance of the posterior
    mean = alpha / (alpha + beta) if (alpha + beta) > 0 else 0.0
    variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1)) if (alpha + beta) > 1 else 0.0

    return alpha, beta, mean, variance


def point_estimate(n_exceed: int, n_total: int) -> float:
    """Simple point estimate: k/n."""
    return n_exceed / n_total if n_total > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Decision Making: Point Estimate vs Bayesian
# ═══════════════════════════════════════════════════════════════════════════════

def direction_from_probability(p_up: float) -> str:
    """Convert probability of 'up' to direction."""
    if p_up > 0.50:
        return "up"
    elif p_up < 0.50:
        return "down"
    return "flat"


def point_estimate_decision(
    gefs_data: dict, prev_temp_f: float,
) -> Tuple[Optional[str], float]:
    """
    Point-estimate decision: direction from ensemble mean, confidence from fraction.
    This replicates the baseline approach.
    """
    mean_c = gefs_data.get("mean_c")
    if mean_c is None:
        return None, 0.0

    gefs_mean_f = mean_c * 9 / 5 + 32

    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < 0.5:
        return None, 0.0

    pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    if pred_dir == 0:
        return None, 0.0

    member_temps_c = gefs_data.get("member_temps_c")
    if member_temps_c and len(member_temps_c) >= 3:
        member_temps_f = [t * 9 / 5 + 32 for t in member_temps_c]
        n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
        fraction_up = n_up / len(member_temps_f)
        confidence = max(fraction_up, 1.0 - fraction_up)
    else:
        confidence = min(0.99, 0.5 + temp_diff / 20.0)

    pred_direction_str = "up" if pred_dir == 1 else "down"
    return pred_direction_str, confidence


def bayesian_cascade_decision(
    gefs_data: dict, prev_temp_f: float,
    rho: float = ESTIMATED_RHO,
    variance_penalty: float = 1.0,
) -> Tuple[Optional[str], float, dict]:
    """
    Bayesian cascade decision: Beta-binomial posterior, uncertainty-adjusted.

    The variance penalty discounts the edge: effective_edge = edge - c_v * std
    This prevents over-betting when the posterior is wide (high uncertainty).
    """
    mean_c = gefs_data.get("mean_c")
    if mean_c is None:
        return None, 0.0, {}

    gefs_mean_f = mean_c * 9 / 5 + 32

    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < 0.5:
        return None, 0.0, {}

    # Get member exceedances
    member_temps_c = gefs_data.get("member_temps_c")
    n_members = len(member_temps_c) if member_temps_c else 31

    if member_temps_c and len(member_temps_c) >= 3:
        member_temps_f = [t * 9 / 5 + 32 for t in member_temps_c]
        n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
        n_down = n_members - n_up

        # Point-estimate fraction
        fraction_up = n_up / n_members

        # Beta posterior
        # We're computing P(t > prev_temp | ensemble) = P(up)
        alpha_up, beta_up, mean_up, var_up = beta_posterior(
            n_up, n_members, rho
        )
        std_up = math.sqrt(var_up)

        # Direction from posterior mean
        if mean_up > 0.50:
            direction = "up"
            raw_confidence = mean_up
            #
            # # Apply variance penalty: effective confidence = mean - c_v * std
            effective_confidence = max(0.0, mean_up - variance_penalty * std_up)
        elif mean_up < 0.50:
            direction = "down"
            raw_confidence = 1.0 - mean_up
            # For down, variance penalty applies to the complement
            effective_confidence = max(0.0, (1.0 - mean_up) - variance_penalty * std_up)
        else:
            return None, 0.0, {}

        return direction, effective_confidence, {
            "n_members": n_members,
            "n_up": n_up,
            "n_down": n_down,
            "fraction_up": round(fraction_up, 4),
            "alpha": round(alpha_up, 2),
            "beta": round(beta_up, 2),
            "posterior_mean": round(mean_up, 4),
            "posterior_std": round(std_up, 4),
            "effective_n": round(compute_effective_n(n_members, rho), 2),
            "rho": rho,
            "variance_penalty": variance_penalty,
            "raw_confidence": round(raw_confidence, 4),
            "effective_confidence": round(effective_confidence, 4),
        }

    return None, 0.0, {}


# ═══════════════════════════════════════════════════════════════════════════════
# Data Loading
# ═══════════════════════════════════════════════════════════════════════════════

def load_gefs_all() -> Dict[str, Dict[str, dict]]:
    """Load GEFS step=24 forecasts. Returns {station: {target_date: {...}}}."""
    conn = sqlite3.connect(GEFS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT station, target_date, ensemble_mean, n_members, member_values
        FROM gefs_archive WHERE step = 24
    """)
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
    """Load Kalshi settlements. Returns {station: {target_date: temp_f}}."""
    conn = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None and s != "TEST":
            settlements[s][d] = float(t)
    conn.close()
    return {k: dict(v) for k, v in settlements.items()}


# ═══════════════════════════════════════════════════════════════════════════════
# Trade Calculation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_trade(
    pred_direction: str,
    confidence: float,
    actual_dir_int: int,
    market_price: float = MARKET_PRICE,
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


# ═══════════════════════════════════════════════════════════════════════════════
# Backtest
# ═══════════════════════════════════════════════════════════════════════════════

def run_cascade_backtest(
    mode: str,
    start_date: str,
    days: int,
    rho: float = ESTIMATED_RHO,
    variance_penalty: float = 1.0,
    initial_bankroll: float = INITIAL_BANKROLL,
) -> dict:
    """
    Run a backtest with the specified cascade mode.

    mode: 'point_estimate' or 'bayesian'
    """
    print(f"  Loading GEFS data...")
    gefs_all = load_gefs_all()
    print(f"  Loaded {sum(len(v) for v in gefs_all.values())} GEFS forecasts")

    print(f"  Loading settlement data...")
    settlements = load_settlements()
    print(f"  Loaded {sum(len(v) for v in settlements.values())} settlement records")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = start_dt + timedelta(days=days)

    trades = []
    bankroll = initial_bankroll
    daily_returns = defaultdict(float)
    bayesian_debug = []
    n_total = 0
    n_correct = 0

    for station in STATIONS:
        station_dates = sorted(settlements.get(station, {}).keys())
        if len(station_dates) < 2:
            continue

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

            gefs_for_date = gefs_all.get(station, {}).get(target_date)
            if not gefs_for_date:
                continue

            n_total += 1

            if mode == 'point_estimate':
                pred_dir, confidence = point_estimate_decision(gefs_for_date, prev_temp)
                debug_info = {}
            else:
                pred_dir, confidence, debug_info = bayesian_cascade_decision(
                    gefs_for_date, prev_temp, rho, variance_penalty
                )

            if pred_dir is None:
                continue

            trade = compute_trade(pred_dir, confidence, actual_dir_int)
            if trade is None:
                continue

            trade["station"] = station
            trade["target_date"] = target_date
            if debug_info:
                trade["bayesian"] = debug_info

            net_pnl = trade["net_pnl"]
            trade["bankroll_after"] = round(bankroll + net_pnl, 2)
            bankroll += net_pnl
            daily_returns[target_date] += net_pnl
            trades.append(trade)

            if trade.get("correct"):
                n_correct += 1

            if mode == 'bayesian' and debug_info:
                bayesian_debug.append({
                    "station": station,
                    "date": target_date,
                    "direction": pred_dir,
                    "actual": "up" if actual_dir_int == 1 else "down",
                    "correct": trade.get("correct"),
                    "posterior_mean": debug_info.get("posterior_mean"),
                    "posterior_std": debug_info.get("posterior_std"),
                    "effective_n": debug_info.get("effective_n"),
                    "confidence": confidence,
                })

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

    accuracy_pct = accuracy * 100

    print(f"    Trades: {total}, Correct: {correct}, Acc: {accuracy_pct:.2f}%")
    print(f"    P&L: ${total_pnl:+.2f}, Fees: ${total_fees:.2f}, Sharpe: {sharpe:.2f}")

    return {
        "trades": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "accuracy_pct": round(accuracy_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "final_bankroll": round(bankroll, 2),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": round(pf, 4),
        "config": {
            "mode": mode,
            "rho": rho,
            "variance_penalty": variance_penalty,
            "n_eff_formula": f"n / (1 + (n-1) * {rho})",
        },
        "bayesian_debug": bayesian_debug[:50],  # First 50 for inspection
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Cascade v1 Beta-binomial Research")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=str, default="2025-08-03")
    parser.add_argument("--rho", type=float, default=ESTIMATED_RHO,
                        help="Pairwise member correlation (default: 0.60)")
    parser.add_argument("--variance-penalty", type=float, default=1.0,
                        help="Variance penalty coefficient (default: 1.0)")
    args = parser.parse_args()

    end_date = (datetime.strptime(args.start, "%Y-%m-%d") + timedelta(days=args.days - 1)).strftime("%Y-%m-%d")

    print("=" * 72)
    print("  CASCADE V1 RESEARCH — Beta-binomial Layer 1")
    print("=" * 72)
    print(f"  Period: {args.start} -> {end_date} ({args.days} days)")
    print(f"  Baseline: {BASELINE['accuracy']:.1f}% acc, ${BASELINE['pnl']:.2f} P&L")
    print(f"  Rho: {args.rho} (effective n = n / (1 + (n-1)*{args.rho}))")
    print(f"  Variance penalty: {args.variance_penalty}")
    print()

    # Run point-estimate (baseline replica)
    print("  [1/3] Point-estimate (baseline replica)...")
    pe_result = run_cascade_backtest(
        "point_estimate", args.start, args.days, args.rho, args.variance_penalty
    )

    print()
    print("  [2/3] Bayesian cascade (with uncertainty weighting)...")
    bayes_result = run_cascade_backtest(
        "bayesian", args.start, args.days, args.rho, args.variance_penalty
    )

    print()
    print("  [3/3] Bayesian cascade (rho=0.0, naive overconfident)...")
    naive_result = run_cascade_backtest(
        "bayesian", args.start, args.days, 0.0, args.variance_penalty
    )

    # Compare results
    print()
    print("=" * 72)
    print("  COMPARISON")
    print("=" * 72)

    pe_acc = pe_result["accuracy_pct"]
    bayes_acc = bayes_result["accuracy_pct"]
    naive_acc = naive_result["accuracy_pct"]

    print(f"  {'Method':<30} {'Acc%':>8} {'P&L':>12} {'Sharpe':>8} {'Trades':>8}")
    print(f"  {'-'*66}")
    print(f"  {'Point-estimate (baseline)':<30} {pe_acc:>7.2f}% "
          f"${pe_result['total_pnl']:>+10.2f} {pe_result['sharpe']:>8.2f} "
          f"{pe_result['trades']:>8}")
    print(f"  {'Bayesian (rho=0.60)':<30} {bayes_acc:>7.2f}% "
          f"${bayes_result['total_pnl']:>+10.2f} {bayes_result['sharpe']:>8.2f} "
          f"{bayes_result['trades']:>8}")
    print(f"  {'Bayesian (rho=0.0, naive)':<30} {naive_acc:>7.2f}% "
          f"${naive_result['total_pnl']:>+10.2f} {naive_result['sharpe']:>8.2f} "
          f"{naive_result['trades']:>8}")

    bayes_vs_pe = bayes_acc - pe_acc
    naive_vs_pe = naive_acc - pe_acc

    print(f"\n  Bayesian vs Point-estimate: {bayes_vs_pe:+.2f}pp")
    print(f"  Naive (rho=0.0) vs Point-estimate: {naive_vs_pe:+.2f}pp")

    # Assessment
    print()
    print("  ASSESSMENT:")
    if bayes_acc > pe_acc:
        print(f"  ✅ Bayesian cascade improves over point-estimate (+{bayes_vs_pe:.2f}pp)")
        print(f"     The uncertainty-weighted posterior adds value by appropriately")
        print(f"     discounting confidence when ensemble spread is high.")
    else:
        print(f"  ⚠️ Bayesian cascade does NOT improve over point-estimate ({bayes_vs_pe:+.2f}pp)")
        if naive_acc > bayes_acc:
            print(f"     But naive (rho=0.0) Bayesian is worse: {naive_vs_pe:+.2f}pp vs baseline")
            print(f"     This suggests the correlation adjustment is directionally correct.")

    # Build output
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": {"start": args.start, "end": end_date, "days": args.days},
        "config": {
            "prior": {"alpha": ALPHA_PRIOR, "beta": BETA_PRIOR},
            "model": "Beta-binomial Layer 1",
            "variance_penalty": args.variance_penalty,
            "edge_threshold": EDGE_THRESHOLD,
            "kelly_fraction": KELLY_FRACTION,
        },
        "baseline": BASELINE,
        "results": {
            "point_estimate": pe_result,
            "bayesian_rho_0_60": bayes_result,
            "bayesian_rho_0_0_naive": naive_result,
        },
        "comparison": {
            "bayesian_vs_point_estimate_pp": round(bayes_vs_pe, 2),
            "naive_vs_point_estimate_pp": round(naive_vs_pe, 2),
            "improves_over_baseline": bayes_acc > pe_acc,
        },
        "recommendation": (
            "The Bayesian Beta-binomial Layer 1 with correlation-adjusted effective "
            "sample size "
            + ("improves" if bayes_acc > pe_acc else "does not improve")
            + " over the point-estimate baseline. "
            + (
                "The uncertainty-weighted posterior appropriately discounts low-confidence "
                "predictions, reducing over-betting on high-spread ensemble forecasts."
                if bayes_acc > pe_acc
                else "The variance penalty may be too aggressive. Further tuning of the "
                     "variance_penalty coefficient and rho parameter is needed."
            )
        ),
    }

    output_path = OUTPUT_DIR / "cascade_v1_research_20260803.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print("  Done.\n")


if __name__ == "__main__":
    main()