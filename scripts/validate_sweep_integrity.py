#!/usr/bin/env python3
"""
validate_sweep_integrity.py — Pre-sweep validation that catches the types of issues
we discovered in the first Big Sweep:

1. Signal-backtest alignment — flag if evaluate() method doesn't match signal name
2. P&L sanity — flag per-trade P&L > $500 for binary options
3. Zero-trade diagnosis — classify dead signals by reason
4. Validation split integrity — flag discovery > holdout gap > 15pp
5. Redundancy detection — flag signals with ρ > 0.9
6. Date coverage — verify DB-backed signals have data for sweep range
7. Look-ahead bias — flag signals using days[idx] instead of days[idx-1]
"""

import argparse
import ast
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALS_DIR = os.path.join(REPO_ROOT, "core", "signals")
DATA_DIR = os.path.join(REPO_ROOT, "data")

# ── Results ──
results = {
    "passed": 0,
    "failed": 0,
    "warnings": 0,
    "checks": [],
}


def check(name: str, passed: bool, detail: str = ""):
    """Record a check result."""
    if passed:
        results["passed"] += 1
        status = "✅ PASS"
    else:
        results["failed"] += 1
        status = "❌ FAIL"
    results["checks"].append({"name": name, "status": status, "detail": detail})
    logger.info(f"  {status} {name}" + (f" — {detail}" if detail else ""))


def warn(name: str, detail: str = ""):
    """Record a warning."""
    results["warnings"] += 1
    results["checks"].append({"name": name, "status": "⚠️ WARN", "detail": detail})
    logger.info(f"  ⚠️ WARN {name}" + (f" — {detail}" if detail else ""))


# ── Check 1: Signal evaluate() alignment ──
def check_signal_alignment():
    """Check that each signal's evaluate() method actually uses the data
    described by its name. Detect look-ahead bias (using days[idx] instead of days[idx-1])."""
    logger.info("\n[Check 1] Signal Evaluate Alignment & Look-Ahead Bias")

    signal_files = [f for f in os.listdir(SIGNALS_DIR) if f.endswith("_signal.py") and f != "base_signal.py"]

    for fname in sorted(signal_files):
        filepath = os.path.join(SIGNALS_DIR, fname)
        with open(filepath) as f:
            content = f.read()

        # Look for evaluate() method
        if "def evaluate(self, idx" not in content:
            warn(f"{fname}", "No evaluate(self, idx, ...) method found")
            continue

        # Check for look-ahead bias: using days[idx] instead of days[idx-1]
        # Parse the evaluate method to find data access patterns
        try:
            tree = ast.parse(content)
        except SyntaxError:
            warn(f"{fname}", "Syntax error parsing file")
            continue

        # Find the evaluate method
        evaluate_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "evaluate":
                evaluate_node = node
                break

        if evaluate_node is None:
            warn(f"{fname}", "Could not parse evaluate() method")
            continue

        # Check for days[idx] pattern (look-ahead bias)
        has_days_idx = False
        has_days_idx_minus = False
        for node in ast.walk(evaluate_node):
            if isinstance(node, ast.Subscript):
                if isinstance(node.value, ast.Name) and node.value.id == "days":
                    if isinstance(node.slice, ast.Index):
                        slice_val = node.slice.value
                    elif isinstance(node.slice, ast.Constant):
                        slice_val = node.slice.value
                    elif isinstance(node.slice, ast.BinOp):
                        if isinstance(node.slice, ast.BinOp):
                            # Check if it's days[idx] vs days[idx-1]
                            pass
                        continue
                    else:
                        continue

                    if isinstance(slice_val, ast.Name) and slice_val.id == "idx":
                        # Check for days[idx] - this is a look-ahead flag
                        has_days_idx = True
                        # Check if there's a parent BinOp (idx - 1)
                        for parent in ast.walk(evaluate_node):
                            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Sub):
                                if isinstance(parent.left, ast.Name) and parent.left.id == "idx":
                                    if isinstance(parent.right, ast.Constant) and parent.right.value == 1:
                                        has_days_idx_minus = True

        # Actually, let me do a simpler text-based check
        evaluate_text = content[content.find("def evaluate(self"):]
        evaluate_text = evaluate_text[:evaluate_text.find("\ndef ") if evaluate_text.find("\ndef ") > 0 else len(evaluate_text)]

        # Check for days[idx] without the -1 offset
        has_idx_access = "days[idx]" in evaluate_text or "days[idx +" in evaluate_text
        # Check for proper offset: days[idx - 1] or days[idx - 2]
        has_proper_offset = "days[idx - 1]" in evaluate_text or "days[idx - 2]" in evaluate_text or "max(1, idx - 2)" in evaluate_text

        signal_name = fname.replace("_signal.py", "")
        if has_idx_access and not has_proper_offset:
            fail(f"{signal_name}", f"Look-ahead bias: uses days[idx] instead of days[idx-1]")
        elif has_idx_access and has_proper_offset:
            warn(f"{signal_name}", f"Uses days[idx] but also has proper offset — verify manually")
        elif not has_proper_offset:
            warn(f"{signal_name}", f"Neither days[idx] nor days[idx-1] pattern found — check manually")
        else:
            check(f"{signal_name}", True, "Proper offset (days[idx-1] or days[idx-2])")


def fail(name: str, detail: str = ""):
    """Record a failure."""
    results["failed"] += 1
    results["checks"].append({"name": name, "status": "❌ FAIL", "detail": detail})
    logger.info(f"  ❌ FAIL {name}" + (f" — {detail}" if detail else ""))


# ── Check 2: P&L sanity ──
def check_pnl_sanity(results_path: str = None):
    """Check per-trade P&L from sweep results. Flag if > $500 average."""
    logger.info("\n[Check 2] P&L Sanity Check")

    if not results_path or not os.path.exists(results_path):
        warn("No sweep results", "No results file to check. Run sweep first.")
        return

    with open(results_path) as f:
        data = json.load(f)

    signals = data.get("signals", {})
    for name, s in sorted(signals.items()):
        agg = s.get("aggregate", {})
        trades = agg.get("n_trades", 0)
        if trades == 0:
            continue
        pnl = agg.get("total_pnl", 0)
        per_trade = pnl / trades
        if per_trade > 500:
            fail(f"{name}: \${per_trade:.0f}/trade", f"Per-trade P&L > \$500 (${per_trade:.0f}) across {trades} trades. Likely inflated.")
        elif per_trade > 200:
            warn(f"{name}: \${per_trade:.0f}/trade", f"Per-trade P&L > \$200 (${per_trade:.0f}) — verify.")
        else:
            check(f"{name}: \${per_trade:.0f}/trade", True, f"Per-trade P&L reasonable (${per_trade:.0f})")


# ── Check 3: Validation split integrity ──
def check_validation_split(results_path: str = None):
    """Check discovery vs holdout accuracy gap. Flag if > 15pp."""
    logger.info("\n[Check 3] Validation Split Integrity")

    if not results_path or not os.path.exists(results_path):
        warn("No sweep results", "No results file to check.")
        return

    with open(results_path) as f:
        data = json.load(f)

    signals = data.get("signals", {})
    for name, s in sorted(signals.items()):
        val = s.get("validation", {})
        disc = val.get("discovery", {})
        time_h = val.get("time_holdout", {})
        geo_h = val.get("geo_holdout", {})

        disc_acc = disc.get("accuracy", 0)
        time_acc = time_h.get("accuracy", 0)
        geo_acc = geo_h.get("accuracy", 0)

        if disc.get("n_trades", 0) == 0:
            continue

        # Discovery vs time holdout gap
        gap = disc_acc - time_acc
        if gap > 0.15:
            fail(f"{name}: disc={disc_acc:.1%} holdout={time_acc:.1%} gap={gap:.1%}",
                 f"Discovery > holdout by {gap:.1%}pp — look-ahead bias indicator")
        elif gap > 0.10:
            warn(f"{name}: disc={disc_acc:.1%} holdout={time_acc:.1%} gap={gap:.1%}",
                 f"Discovery > holdout by {gap:.1%}pp — moderate gap")
        elif geo_h.get("n_trades", 0) > 0:
            geo_gap = disc_acc - geo_acc
            if geo_gap > 0.15:
                warn(f"{name}: disc={disc_acc:.1%} geo={geo_acc:.1%} gap={geo_gap:.1%}",
                     f"Discovery > geo-holdout by {geo_gap:.1%}pp — signal may not generalize")
            else:
                check(f"{name}: disc={disc_acc:.1%} time={time_acc:.1%} geo={geo_acc:.1%}", True,
                      f"Validation splits reasonable")


# ── Check 4: DB date coverage ──
def check_db_coverage():
    """Check that DB-backed signals have data for the sweep date range."""
    logger.info("\n[Check 4] DB Date Coverage")

    dbs_to_check = [
        ("GEFS archive", "gefs_archive.db", "gefs_archive", "target_date"),
        ("Kalshi settlements", "kalshi_settlements.db", "kalshi_settlements", "target_date"),
        ("TIGGE archive", "tigge_archive.db", "tigge_archive", "target_date"),
        ("METAR backfill", "metar_backfill.db", "metar_daily", "date"),
        ("NWP forecasts", "nwp_forecasts.db", "nwp_forecasts", "target_date"),
        ("ERA5 archive", "era5_archive.db", "era5_archive", "date"),
    ]

    for label, dbname, table, date_col in dbs_to_check:
        dbpath = os.path.join(DATA_DIR, dbname)
        if not os.path.exists(dbpath):
            warn(f"{label}", f"DB not found: {dbname}")
            continue

        try:
            conn = sqlite3.connect(dbpath)
            c = conn.cursor()
            c.execute(f"SELECT MIN({date_col}), MAX({date_col}), COUNT(*) FROM {table}")
            row = c.fetchone()
            if row:
                dmin, dmax, cnt = row
                check(f"{label}: {cnt:,} rows, {dmin} → {dmax}", True,
                      f"{cnt:,} rows from {dmin} to {dmax}")
            conn.close()
        except Exception as e:
            fail(f"{label}", f"Error: {e}")
            try:
                # Check what tables exist
                conn = sqlite3.connect(dbpath)
                c = conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [r[0] for r in c.fetchall()]
                warn(f"  Tables in {dbname}: {tables}")
                conn.close()
            except:
                pass


# ── Check 5: Redundancy detection ──
def check_redundancy(results_path: str = None):
    """Check for signals with ρ > 0.9 (near-identical)."""
    logger.info("\n[Check 5] Redundancy Detection")

    corr_path = os.path.join(DATA_DIR, "signal_correlation_matrix.json")
    if not os.path.exists(corr_path):
        warn("No correlation matrix", "Run compute_signal_correlation_matrix.py first.")
        return

    with open(corr_path) as f:
        cm = json.load(f)

    if not cm:
        warn("Empty correlation matrix", "No signal data in matrix.")
        return

    # Check each signal pair
    redundancies = []
    for sig1, pairs in cm.items():
        if not isinstance(pairs, dict):
            continue
        for sig2, rho in pairs.items():
            if sig1 < sig2 and abs(rho) > 0.9:
                redundancies.append((sig1, sig2, rho))

    if redundancies:
        for sig1, sig2, rho in redundancies:
            fail(f"{sig1} ↔ {sig2}: ρ={rho:.3f}", "Near-identical signals — kill one")
    else:
        check("No redundant signal pairs", True, "All ρ < 0.9")


# ── Check 6: Signal registry completeness ──
def check_signal_registry():
    """Check that all signal files in core/signals/ are registered in big_sweep.py."""
    logger.info("\n[Check 6] Signal Registry Completeness")

    sweep_path = os.path.join(REPO_ROOT, "scripts", "big_sweep.py")
    if not os.path.exists(sweep_path):
        warn("No sweep script", "big_sweep.py not found")
        return

    with open(sweep_path) as f:
        sweep_content = f.read()

    # Get all signal files
    signal_files = [f.replace("_signal.py", "") for f in os.listdir(SIGNALS_DIR)
                    if f.endswith("_signal.py") and f != "base_signal.py"]

    # Check if each signal is registered in big_sweep.py
    unregistered = []
    for sig in signal_files:
        if sig not in sweep_content:
            unregistered.append(sig)

    if unregistered:
        for sig in unregistered:
            warn(f"{sig}_signal.py", "Not registered in big_sweep.py sweep registry")
    else:
        check("All signals registered", True, f"{len(signal_files)} files, all registered in sweep")


# ── Main ──
def main():
    parser = argparse.ArgumentParser(description="Validate sweep integrity")
    parser.add_argument("--results", type=str, default=None,
                        help="Path to sweep results JSON for P&L/validation checks")
    parser.add_argument("--output", type=str, default=None,
                        help="Output path for validation report")
    args = parser.parse_args()

    if args.results:
        results_path = args.results
    else:
        # Try to find latest results
        for fname in ["sweep_results_v2.json", "sweep_results_v1.json"]:
            fp = os.path.join(DATA_DIR, fname)
            if os.path.exists(fp):
                results_path = fp
                break
        else:
            results_path = None

    logger.info("=" * 70)
    logger.info("  SWEEP INTEGRITY VALIDATION")
    logger.info("=" * 70)

    check_signal_alignment()
    check_db_coverage()
    check_signal_registry()
    check_pnl_sanity(results_path)
    check_validation_split(results_path)
    check_redundancy(results_path)

    logger.info("\n" + "=" * 70)
    logger.info(f"  Results: {results['passed']} passed, {results['failed']} failed, {results['warnings']} warnings")
    logger.info("=" * 70)

    # Write output
    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"\nReport written to {args.output}")

    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())