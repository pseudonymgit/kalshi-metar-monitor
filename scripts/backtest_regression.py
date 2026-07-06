#!/usr/bin/env python3
"""
Backtest-as-CI Regression Gate

Runs full backtest and compares metrics against stored baselines.
Fails (exit 1) if any metric degrades beyond threshold.

Usage:
    python scripts/backtest_regression.py [--threshold-accuracy 2] [--threshold-sharpe 0.05]

Thresholds:
    - accuracy: maximum percentage point degradation (default: 2pp)
    - sharpe: maximum Sharpe ratio degradation (default: 0.05)
    - brier: maximum Brier score increase (default: 0.02)
    - ece: maximum ECE increase (default: 0.05)
    - trade_count: maximum percentage decrease (default: 10%)

Exit codes:
    0: All metrics within thresholds
    1: One or more metrics degraded beyond threshold
    2: Error running backtest or loading baselines
"""

import sqlite3
import os
import sys
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# Add core to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, 'core'))

# Import backtest modules
try:
    from ensemble_v8_fdr_validation import run_full_backtest
except ImportError:
    # Try alternative backtest script
    try:
        from split_backtest_current import run_backtest
    except ImportError:
        print("ERROR: No backtest module found")
        sys.exit(2)

METAR_DB = os.path.join(REPO_ROOT, "data", "metar_backfill.db")
BASELINES_FILE = os.path.join(REPO_ROOT, "data", "backtest_baselines.json")


# Default baselines (from Phase 1 results)
DEFAULT_BASELINES = {
    "accuracy": 0.6526,
    "sharpe": 0.30,
    "brier": 0.21,
    "ece": 0.08,
    "trade_count": 16000,
    "stations": 16,
}


def load_baselines():
    """Load baselines from file or use defaults."""
    if os.path.exists(BASELINES_FILE):
        try:
            with open(BASELINES_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load baselines from {BASELINES_FILE}: {e}")
            print("Using defaults...")
    
    return DEFAULT_BASELINES.copy()


def save_baselines(baselines: Dict):
    """Save baselines to file."""
    os.makedirs(os.path.dirname(BASELINES_FILE), exist_ok=True)
    with open(BASELINES_FILE, 'w') as f:
        json.dump(baselines, f, indent=2)
    print(f"Baselines saved to {BASELINES_FILE}")


def compute_metrics(results: List[Tuple]) -> Dict:
    """
    Compute accuracy, Sharpe, Brier, ECE, and trade count from backtest results.
    
    Args:
        results: List of (predicted, actual, confidence) tuples
        
    Returns:
        Dict with metrics
    """
    if not results:
        return {
            "accuracy": 0.0,
            "sharpe": 0.0,
            "brier": 0.0,
            "ece": 0.0,
            "trade_count": 0,
        }
    
    # Accuracy
    correct = sum(1 for pred, actual, conf in results if pred == actual)
    accuracy = correct / len(results)
    
    # Sharpe ratio (simplified)
    returns = []
    for pred, actual, conf in results:
        # Simplified return: +2*conf if correct, -2*conf if wrong
        # Fee: 0.05 (5%)
        fee = 0.05 * conf
        if pred == actual:
            returns.append(2 * conf - fee)
        else:
            returns.append(-2 * conf - fee)
    
    if len(returns) >= 2:
        mean_ret = sum(returns) / len(returns)
        var_ret = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(var_ret) if var_ret > 0 else 0.001
        sharpe = mean_ret / std_ret if std_ret > 0 else 0.0
    else:
        sharpe = 0.0
    
    # Brier score
    brier_sum = 0.0
    for pred, actual, conf in results:
        prob_up = conf if pred == 'up' else 1.0 - conf
        outcome = 1.0 if actual == 'up' else 0.0
        brier_sum += (prob_up - outcome) ** 2
    brier = brier_sum / len(results)
    
    # ECE (Expected Calibration Error) - simplified
    n_bins = 10
    bins = [[] for _ in range(n_bins)]
    for pred, actual, conf in results:
        prob_up = conf if pred == 'up' else 1.0 - conf
        bin_idx = min(int(prob_up * n_bins), n_bins - 1)
        bins[bin_idx].append((prob_up, 1.0 if actual == 'up' else 0.0))
    
    ece_sum = 0.0
    total = len(results)
    for bin_data in bins:
        if len(bin_data) > 0:
            avg_conf = sum(p for p, _ in bin_data) / len(bin_data)
            avg_acc = sum(a for _, a in bin_data) / len(bin_data)
            ece_sum += abs(avg_conf - avg_acc) * len(bin_data)
    ece = ece_sum / total if total > 0 else 0.0
    
    return {
        "accuracy": accuracy,
        "sharpe": sharpe,
        "brier": brier,
        "ece": ece,
        "trade_count": len(results),
    }


def compare_metrics(current: Dict, baseline: Dict, thresholds: Dict) -> List[Dict]:
    """
    Compare current metrics to baselines and identify degradations.
    
    Args:
        current: Current metrics
        baseline: Baseline metrics
        thresholds: Degradation thresholds
        
    Returns:
        List of degradations (empty if all within thresholds)
    """
    degradations = []
    
    # Accuracy: degradation = baseline - current (negative is good)
    accuracy_change = current["accuracy"] - baseline["accuracy"]
    if accuracy_change < -thresholds["accuracy"]:
        degradations.append({
            "metric": "accuracy",
            "baseline": baseline["accuracy"],
            "current": current["accuracy"],
            "change": accuracy_change,
            "threshold": -thresholds["accuracy"],
            "status": "DEGRADED",
            "description": f"Accuracy dropped from {baseline['accuracy']:.4f} to {current['accuracy']:.4f} (-{abs(accuracy_change):.2f}pp)"
        })
    
    # Sharpe: degradation = baseline - current (negative is good)
    sharpe_change = current["sharpe"] - baseline["sharpe"]
    if sharpe_change < -thresholds["sharpe"]:
        degradations.append({
            "metric": "sharpe",
            "baseline": baseline["sharpe"],
            "current": current["sharpe"],
            "change": sharpe_change,
            "threshold": -thresholds["sharpe"],
            "status": "DEGRADED",
            "description": f"Sharpe ratio dropped from {baseline['sharpe']:.3f} to {current['sharpe']:.3f} (-{abs(sharpe_change):.3f})"
        })
    
    # Brier: degradation = current - baseline (positive is bad)
    brier_change = current["brier"] - baseline["brier"]
    if brier_change > thresholds["brier"]:
        degradations.append({
            "metric": "brier",
            "baseline": baseline["brier"],
            "current": current["brier"],
            "change": brier_change,
            "threshold": thresholds["brier"],
            "status": "DEGRADED",
            "description": f"Brier score increased from {baseline['brier']:.4f} to {current['brier']:.4f} (+{brier_change:.4f})"
        })
    
    # ECE: degradation = current - baseline (positive is bad)
    ece_change = current["ece"] - baseline["ece"]
    if ece_change > thresholds["ece"]:
        degradations.append({
            "metric": "ece",
            "baseline": baseline["ece"],
            "current": current["ece"],
            "change": ece_change,
            "threshold": thresholds["ece"],
            "status": "DEGRADED",
            "description": f"ECE increased from {baseline['ece']:.4f} to {current['ece']:.4f} (+{ece_change:.4f})"
        })
    
    # Trade count: degradation = baseline - current (negative is good, but we check decrease)
    trade_change = current["trade_count"] - baseline["trade_count"]
    if trade_change < -thresholds["trade_count"] * baseline["trade_count"] / 100:
        degradations.append({
            "metric": "trade_count",
            "baseline": baseline["trade_count"],
            "current": current["trade_count"],
            "change": trade_change,
            "threshold": -thresholds["trade_count"],
            "status": "DEGRADED",
            "description": f"Trade count dropped from {baseline['trade_count']} to {current['trade_count']} (-{abs(trade_change)} trades)"
        })
    
    return degradations


def run_backtest_comparison(baseline: Dict, thresholds: Dict) -> Tuple[Dict, List[Dict]]:
    """
    Run backtest and compare against baselines.
    
    Args:
        baseline: Baseline metrics
        thresholds: Degradation thresholds
        
    Returns:
        Tuple of (current_metrics, degradations)
    """
    print("=" * 80)
    print("BACKTEST REGRESSION GATE")
    print("=" * 80)
    
    print(f"\nRunning backtest on {METAR_DB}...")
    
    # Run backtest (using ensemble_v8 or split_backtest_current)
    try:
        from ensemble_v8_fdr_validation import run_full_backtest
        results = run_full_backtest()
    except ImportError:
        try:
            from split_backtest_current import run_backtest
            results = run_backtest()
        except Exception as e:
            print(f"ERROR: Could not run backtest: {e}")
            sys.exit(2)
    
    print(f"Backtest complete: {len(results)} trades")
    
    # Compute current metrics
    current = compute_metrics(results)
    
    # Compare to baselines
    degradations = compare_metrics(current, baseline, thresholds)
    
    return current, degradations


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Backtest regression gate")
    parser.add_argument("--threshold-accuracy", type=float, default=2.0,
                       help="Maximum accuracy degradation (pp)")
    parser.add_argument("--threshold-sharpe", type=float, default=0.05,
                       help="Maximum Sharpe ratio degradation")
    parser.add_argument("--threshold-brier", type=float, default=0.02,
                       help="Maximum Brier score increase")
    parser.add_argument("--threshold-ece", type=float, default=0.05,
                       help="Maximum ECE increase")
    parser.add_argument("--threshold-trade-count", type=float, default=10.0,
                       help="Maximum trade count decrease (%)")
    parser.add_argument("--update-baselines", action="store_true",
                       help="Update baselines with current results")
    parser.add_argument("--output", type=str, default="reports/backtest_regression_report.md",
                       help="Output report path")
    
    args = parser.parse_args()
    
    # Load baselines
    baseline = load_baselines()
    print(f"Loaded baselines:")
    for k, v in baseline.items():
        print(f"  {k}: {v}")
    
    # Run comparison
    current, degradations = run_backtest_comparison(baseline, {
        "accuracy": args.threshold_accuracy / 100,  # Convert pp to fraction
        "sharpe": args.threshold_sharpe,
        "brier": args.threshold_brier,
        "ece": args.threshold_ece,
        "trade_count": args.threshold_trade_count,
    })
    
    # Generate report
    report = []
    report.append("# Backtest Regression Report")
    report.append(f"\n**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report.append("\n## Baselines")
    for k, v in baseline.items():
        report.append(f"- {k}: {v}")
    
    report.append("\n## Current Results")
    for k, v in current.items():
        report.append(f"- {k}: {v:.4f}" if isinstance(v, float) else f"- {k}: {v}")
    
    if degradations:
        report.append("\n## Degradations (FAIL)")
        for deg in degradations:
            report.append(f"\n### {deg['metric'].upper()}")
            report.append(f"- Status: {deg['status']}")
            report.append(f"- {deg['description']}")
            report.append(f"- Threshold: {deg['threshold']}")
    else:
        report.append("\n## Results: PASS ✅")
        report.append("\nAll metrics are within acceptable thresholds.")
    
    report_str = "\n".join(report)
    
    # Write report
    output_path = args.output
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(report_str)
    print(f"\nReport written to {output_path}")
    
    # Update baselines if requested
    if args.update_baselines:
        print("\nUpdating baselines...")
        save_baselines(current)
    
    # Exit with appropriate code
    if degradations:
        print(f"\n{'='*80}")
        print("REGRESSION DETECTED - FAIL")
        print(f"{'='*80}")
        for deg in degradations:
            print(f"  {deg['metric']}: {deg['description']}")
        sys.exit(1)
    else:
        print(f"\n{'='*80}")
        print("ALL METRICS WITHIN THRESHOLDS - PASS")
        print(f"{'='*80}")
        sys.exit(0)


if __name__ == "__main__":
    main()
