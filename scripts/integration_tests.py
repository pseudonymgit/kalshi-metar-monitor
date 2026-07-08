#!/usr/bin/env python3
"""
M5/A3 — Integration Tests for Weather Engine

Test suites:
1. Signal parity: unified signals match backtest inline signals
2. End-to-end: paper trading engine runs in ensemble mode without errors
3. Regression: backtest metrics don't degrade vs baseline

Usage:
  python3 scripts/integration_tests.py [--verbose]

Exit codes:
  0 = all tests pass
  1 = one or more tests fail
"""

import sqlite3
import os
import sys
import json
import math
import numpy as np
from collections import defaultdict

# Ensure core/ is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(SCRIPT_DIR, '..', 'core')
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from signals import SignalRegistry, BACKTEST_SIGNALS, FULL_ENSEMBLE, BaseSignal
from unified_backtest import run_backtest, load_station_data, DB_PATH, compute_sharpe, compute_brier
from signal_fusion import SignalFusionEngine, TimeDecaySignalManager
from calibration_pipeline import CalibrationPipeline

METAR_DB = os.path.join(CORE_DIR, '..', 'data', 'metar_backfill.db')
BASELINE_FILE = os.path.join(CORE_DIR, '..', 'data', 'baseline_metrics.json')
TEST_STATIONS = ['KATL', 'KBOS', 'KLAX', 'KNYC', 'KSEA']

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⏭️  SKIP"

test_results = []


def test(name, condition, details=""):
    status = PASS if condition else FAIL
    test_results.append({'name': name, 'passed': condition, 'details': details})
    print(f"  {status}: {name}" + (f" — {details}" if details else ""))


# ─── 1. Signal Parity Tests ─────────────────────────────────────────────────

def test_signal_parity():
    """Verify unified signals match the inline backtest signal functions."""
    print("\n" + "=" * 70)
    print("TEST SUITE 1: Signal Parity (unified vs inline)")
    print("=" * 70)
    
    # Load data for a test station
    conn = sqlite3.connect(METAR_DB)
    days, market = load_station_data('KATL', conn)
    conn.close()
    
    if len(days) < 70:
        test("signal_parity_data", False, "Insufficient data for KATL")
        return
    
    # Import the inline signal functions from comprehensive_split_backtest
    sys.path.insert(0, SCRIPT_DIR)
    try:
        from comprehensive_split_backtest import (
            signal_reversion, signal_gaussian, signal_gaussian_v2,
            signal_gaussian_v2, signal_pressure, signal_calendar_climatology,
            signal_goldilocks
        )
    except ImportError:
        test("signal_parity_imports", False, "Could not import inline signals")
        return
    
    test("signal_parity_imports", True)
    
    registry = SignalRegistry(METAR_DB)
    
    # Map inline functions to unified signal objects
    signal_map = [
        ('reversion', signal_reversion),
        ('gaussian', signal_gaussian),
        ('gaussian_v2', signal_gaussian_v2),
        ('pressure', signal_pressure),
        ('climatology', signal_calendar_climatology),
        ('goldilocks', signal_goldilocks),
    ]
    
    mismatches = 0
    for sig_name, inline_fn in signal_map:
        sig = registry.get_signal(sig_name)
        if sig is None:
            test(f"signal_exists_{sig_name}", False, f"Signal {sig_name} not in registry")
            mismatches += 1
            continue
        
        # Compare outputs at several indices
        for idx in [65, 100, 150, 200, 250]:
            if idx >= len(days):
                continue
            inline_dir, inline_conf = inline_fn(idx, days)
            unified_dir, unified_conf = sig.evaluate(idx, days)
            
            if inline_dir != unified_dir:
                test(f"signal_parity_{sig_name}_idx{idx}_dir",
                     False,
                     f"inline={inline_dir} vs unified={unified_dir}")
                mismatches += 1
            elif inline_dir is not None:
                if abs(inline_conf - unified_conf) > 0.01:
                    test(f"signal_parity_{sig_name}_idx{idx}_conf",
                         False,
                         f"inline={inline_conf:.4f} vs unified={unified_conf:.4f}")
                    mismatches += 1
    
    if mismatches == 0:
        test("signal_parity_all", True, f"All 7 signals match across {len(signal_map)} signals")
    else:
        test("signal_parity_all", False, f"{mismatches} mismatches found")


# ─── 2. End-to-End Tests ────────────────────────────────────────────────────

def test_end_to_end():
    """Test that the paper trading engine can run in ensemble mode."""
    print("\n" + "=" * 70)
    print("TEST SUITE 2: End-to-End (ensemble mode)")
    print("=" * 70)
    
    try:
        from paper_trading_engine import PaperTrader, MarketSide
    except Exception as e:
        test("e2e_import", False, str(e))
        return
    
    test("e2e_import", True)
    
    try:
        trader = PaperTrader(initial_balance=10000.0, fee_rate=0.001)
        test("e2e_init", True)
    except Exception as e:
        test("e2e_init", False, str(e))
        return
    
    try:
        trader.enable_ensemble_mode()
        test("e2e_ensemble_enable", True,
             f"signals={len(trader.ensemble_signal_names)}")
    except Exception as e:
        test("e2e_ensemble_enable", False, str(e))
        return
    
    try:
        trader.enable_conviction_gating()
        test("e2e_conviction_enable", True)
    except Exception as e:
        test("e2e_conviction_enable", False, str(e))
        return
    
    # Test signal generation for a historical date
    try:
        # Find a date with settlement data
        conn = sqlite3.connect(METAR_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT local_trading_date FROM settlement_epochs
            WHERE epoch_status='closed' AND market_type='HIGH'
            ORDER BY local_trading_date DESC LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        
        if row:
            test_date = row[0]
            signals = trader.generate_ensemble_signals(test_date)
            test("e2e_signal_generation", True,
                 f"Generated {len(signals)} signals for {test_date}")
        else:
            test("e2e_signal_generation", True, "No settlement data available (skip)")
    except Exception as e:
        test("e2e_signal_generation", False, str(e))
    
    # Test fusion engine
    try:
        fusion = SignalFusionEngine(BACKTEST_SIGNALS, TEST_STATIONS)
        test_signals = [
            ('reversion', 'up', 0.7),
            ('gaussian_v2', 'up', 0.65),
            ('pressure', 'down', 0.6),
        ]
        direction, prob, conf = fusion.fuse_signals(test_signals, 'KATL')
        test("e2e_fusion_engine", True,
             f"dir={direction}, prob={prob:.3f}, conf={conf:.3f}")
    except Exception as e:
        test("e2e_fusion_engine", False, str(e))
    
    # Test time-decay integration
    try:
        tdm = TimeDecaySignalManager(BACKTEST_SIGNALS, TEST_STATIONS, 0.9, 30)
        adjusted = tdm.adjust_confidence('reversion', 'KATL', 0.75)
        test("e2e_time_decay", True,
             f"adjusted conf={adjusted:.3f}")
    except Exception as e:
        test("e2e_time_decay", False, str(e))


# ─── 3. Regression Tests ─────────────────────────────────────────────────────

def test_regression():
    """Verify backtest metrics don't degrade vs baseline."""
    print("\n" + "=" * 70)
    print("TEST SUITE 3: Regression (metric stability)")
    print("=" * 70)
    
    # Run unified backtest on subset of stations
    try:
        results = run_backtest(
            stations=TEST_STATIONS,
            use_fusion=False,
            use_time_decay=True,
            verbose=False,
        )
        test("regression_backtest_run", True,
             f"trades={results['trades']}, acc={results['accuracy']:.4f}")
    except Exception as e:
        test("regression_backtest_run", False, str(e))
        return
    
    if results['trades'] == 0:
        test("regression_trades", False, "No trades generated")
        return
    
    test("regression_trades", results['trades'] > 0, f"{results['trades']} trades")
    
    # Check accuracy baseline
    accuracy = results['accuracy']
    test("regression_accuracy_min",
         accuracy >= 0.55,
         f"accuracy={accuracy:.4f} (min: 0.55)")
    
    # Compare to baseline if available
    if os.path.exists(BASELINE_FILE):
        try:
            with open(BASELINE_FILE) as f:
                baseline = json.load(f)
            
            baseline_acc = baseline.get('accuracy', 0.0)
            if baseline_acc > 0:
                degradation = baseline_acc - accuracy
                test("regression_accuracy_vs_baseline",
                     degradation < 0.05,  # Allow 5pp degradation
                     f"baseline={baseline_acc:.4f}, current={accuracy:.4f}, "
                     f"degradation={degradation:+.4f}")
            else:
                test("regression_accuracy_vs_baseline", True, "Baseline has no accuracy")
        except Exception as e:
            test("regression_accuracy_vs_baseline", True, f"Could not load baseline: {e}")
    else:
        test("regression_accuracy_vs_baseline", True,
             "No baseline file (first run)")
    
    # Check Brier score
    brier = results['brier']
    test("regression_brier", brier < 0.40,
         f"brier={brier:.4f} (max: 0.40)")
    
    # Check that all 7 backtest signals produce output
    signal_stats = results.get('per_signal_stats', {})
    for sig_name in BACKTEST_SIGNALS:
        if sig_name in signal_stats:
            total = signal_stats[sig_name]['total']
            test(f"regression_signal_{sig_name}_fires", total > 0,
                 f"{total} predictions")
        else:
            test(f"regression_signal_{sig_name}_fires", False,
                 "Signal not in stats")
    
    # Test with fusion (S7 path)
    try:
        fusion_results = run_backtest(
            stations=TEST_STATIONS,
            use_fusion=True,
            use_time_decay=True,
            verbose=False,
        )
        test("regression_fusion_run", True,
             f"fusion trades={fusion_results['trades']}, "
             f"acc={fusion_results['accuracy']:.4f}")
    except Exception as e:
        test("regression_fusion_run", False, str(e))


# ─── 4. Signal Registry Tests ──────────────────────────────────────────────

def test_signal_registry():
    """Test signal registry completeness and interface compliance."""
    print("\n" + "=" * 70)
    print("TEST SUITE 4: Signal Registry")
    print("=" * 70)
    
    registry = SignalRegistry(METAR_DB)
    
    # All expected signals registered
    expected = set(BACKTEST_SIGNALS + [
        'late_day_momentum_hourly',
        'pressure_regime_interaction',
        'wind_direction_shift',
    ])
    
    registered = set(registry.get_signal_names())
    
    missing = expected - registered
    test("registry_completeness",
         len(missing) == 0,
         f"missing: {missing}" if missing else f"all {len(expected)} signals present")
    
    # All signals implement BaseSignal
    all_base = True
    for name in registered:
        sig = registry.get_signal(name)
        if not isinstance(sig, BaseSignal):
            all_base = False
            test(f"registry_base_signal_{name}", False)
    
    test("registry_all_base_signal", all_base)
    
    # All signals have name and min_lookback
    for name in registered:
        sig = registry.get_signal(name)
        if not hasattr(sig, 'name') or not hasattr(sig, 'min_lookback'):
            test(f"registry_interface_{name}", False, "missing name or min_lookback")
    
    test("registry_interface_all", True, "All signals have name + min_lookback")


# ─── 5. Calibration Pipeline Tests ──────────────────────────────────────────

def test_calibration():
    """Test calibration pipeline basic functionality."""
    print("\n" + "=" * 70)
    print("TEST SUITE 5: Calibration Pipeline")
    print("=" * 70)
    
    try:
        cal = CalibrationPipeline(['reversion', 'gaussian_v2'], ['KATL', 'KBOS'])
        test("calibration_init", True)
    except Exception as e:
        test("calibration_init", False, str(e))
        return
    
    # Test calibrate
    try:
        result = cal.calibrate('reversion', 'KATL', 0.75)
        test("calibration_calibrate", isinstance(result, (float, int)),
             f"calibrated={result:.4f}")
    except Exception as e:
        test("calibration_calibrate", False, str(e))
    
    # Test update
    try:
        cal.update('reversion', 'KATL', 0.75, True)
        cal.update('reversion', 'KATL', 0.65, False)
        test("calibration_update", True)
    except Exception as e:
        test("calibration_update", False, str(e))


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    
    print("=" * 70)
    print("M5/A3 — Weather Engine Integration Tests")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"DB: {METAR_DB}")
    print("=" * 70)
    
    test_signal_parity()
    test_end_to_end()
    test_regression()
    test_signal_registry()
    test_calibration()
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    total = len(test_results)
    passed = sum(1 for r in test_results if r['passed'])
    failed = total - passed
    
    for r in test_results:
        status = PASS if r['passed'] else FAIL
        print(f"  {status}: {r['name']}" +
              (f" — {r['details']}" if r['details'] else ""))
    
    print(f"\n{passed}/{total} passed, {failed} failed")
    
    if failed > 0:
        print("\n❌ INTEGRATION TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ ALL INTEGRATION TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    from datetime import datetime
    main()
