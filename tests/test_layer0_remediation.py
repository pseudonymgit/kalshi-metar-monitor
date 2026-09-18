#!/usr/bin/env python3
"""
Focused QC tests for Layer 0 remediation (POST-SWEEP-COMPLETENESS-FP-2026-08-14).
Authorized scope: S1-S5, S7 + mandatory aggregation/persistence fixes:
  - GAP1: per-signal stats persist through fold aggregation
  - GAP2: Brier/ECE/reliability bins persist and aggregate correctly
  - GAP3: failure counters aggregate across configs x folds
  - GAP4: persistence/trend baselines present
  - GAP5: effective config count/scope documented
  - GAP7: per-station stats persist through fold aggregation
  - GAP8: data_sources provenance block present

Covers:
  L0-AT1: aggregate_per_signal pools fired/total/correct and re-derives accuracy
  L0-AT2: aggregate_per_station pools correct/trades across folds
  L0-AT3: aggregate_calibration_quality weights Brier by eval count + pools ECE bins
  L0-AT4: aggregate_failure_counters sums all counters + _pct_successful
  L0-AT5: _aggregate_failure_accounting sums across configs, excludes _pct_successful
  L0-AT6: compute_baseline_accuracies returns persistence + trend baselines
  L0-AT7: compute_fusion_accuracy emits ece_bins + _n_eval in calibration_quality
  L0-AT8: evaluate_config_on_fold persists per_signal/per_station through fold boundary
  L0-AT9: effective_config_scope present in both single-split and nested results
  L0-AT10: data_sources provenance block present in JSON output (main path)
  L0-AT11: S7 ranking invariance still holds with new fields present
"""

import sys
import os
import copy
import json
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict

from scripts.run_sweep import (
    aggregate_per_signal,
    aggregate_per_station,
    aggregate_calibration_quality,
    aggregate_failure_counters,
    _aggregate_failure_accounting,
    PROXY_PNL_KEYS,
    NESTED_FOLDS,
)
from scripts.ws5_common import compute_baseline_accuracies


def _make_fold_record(acc=0.60, fired=10, total=20, correct=6,
                      brier_raw=0.25, brier_cal=0.20, ece=0.05, n_eval=10,
                      sig_name="gaussian", station="KNYC",
                      sig_exc=1, fus_exc=0):
    """Build a minimal per-fold config result dict."""
    return {
        "accuracy": acc,
        "trades": fired,
        "per_signal": {
            sig_name: {
                "accuracy": correct / fired if fired else 0.0,
                "fire_rate": fired / total if total else 0.0,
                "fired": fired,
                "correct": correct,
                "total": total,
                "n_eval": n_eval,
                "brier_raw": brier_raw,
                "brier_calibrated": brier_cal,
            }
        },
        "per_station": {
            station: {"accuracy": acc, "trades": fired}
        },
        "calibration_quality": {
            "brier_raw": brier_raw,
            "brier_calibrated": brier_cal,
            "brier_improvement": round(brier_raw - brier_cal, 6),
            "ece": ece,
            "ece_bins": [
                [0.0, 0.1, 2, 0.5, 0.05],
                [0.5, 0.6, 8, 0.6, 0.55],
            ],
            "_n_eval": n_eval,
        },
        "failure_counters": {
            "n_signal_exceptions": sig_exc,
            "n_fusion_exceptions": fus_exc,
            "n_dates_skipped_flat": 0,
            "n_dates_skipped_no_metar": 0,
            "n_no_fusion_output": 0,
        },
    }


def test_l0_at1_aggregate_per_signal():
    """GAP1: aggregate_per_signal pools fired/total/correct and re-derives accuracy."""
    fold_records = [
        _make_fold_record(fired=10, total=20, correct=6, sig_name="gaussian"),
        _make_fold_record(fired=5, total=15, correct=3, sig_name="gaussian"),
    ]
    agg = aggregate_per_signal(fold_records)
    assert "gaussian" in agg, f"Missing signal: {agg.keys()}"
    g = agg["gaussian"]
    assert g["fired"] == 15, f"Expected fired=15, got {g['fired']}"
    assert g["total"] == 35, f"Expected total=35, got {g['total']}"
    assert g["correct"] == 9, f"Expected correct=9, got {g['correct']}"
    assert abs(g["accuracy"] - round(9 / 15, 4)) < 1e-9, f"Accuracy {g['accuracy']} != {9/15}"
    assert abs(g["fire_rate"] - round(15 / 35, 4)) < 1e-9, f"Fire rate {g['fire_rate']} != {round(15/35,4)}"
    # Brier weighted by n_eval (10 each)
    assert abs(g["brier_raw"] - 0.25) < 1e-9, f"brier_raw {g['brier_raw']}"
    assert abs(g["brier_calibrated"] - 0.20) < 1e-9, f"brier_cal {g['brier_calibrated']}"
    print(f"  L0-AT1 PASS: per-signal aggregation pools counts, accuracy={g['accuracy']:.4f}, fire_rate={g['fire_rate']:.4f}")


def test_l0_at2_aggregate_per_station():
    """GAP7: aggregate_per_station pools correct/trades across folds."""
    fold_records = [
        _make_fold_record(fired=10, acc=0.60, station="KNYC"),
        _make_fold_record(fired=20, acc=0.50, station="KNYC"),
    ]
    agg = aggregate_per_station(fold_records)
    assert "KNYC" in agg, f"Missing station: {agg.keys()}"
    s = agg["KNYC"]
    assert s["trades"] == 30, f"Expected trades=30, got {s['trades']}"
    # correct = 0.60*10 + 0.50*20 = 6 + 10 = 16
    assert abs(s["accuracy"] - round(16 / 30, 4)) < 1e-9, f"accuracy {s['accuracy']}"
    print(f"  L0-AT2 PASS: per-station aggregation pools trades, accuracy={s['accuracy']:.4f}")


def test_l0_at3_aggregate_calibration_quality():
    """GAP2: aggregate_calibration_quality weights Brier by eval count + pools ECE bins."""
    fold_records = [
        _make_fold_record(n_eval=10, brier_raw=0.25, brier_cal=0.20, ece=0.05),
        _make_fold_record(n_eval=30, brier_raw=0.21, brier_cal=0.18, ece=0.04),
    ]
    agg = aggregate_calibration_quality(fold_records)
    # Weighted: (0.25*10 + 0.21*30)/40 = (2.5+6.3)/40 = 8.8/40 = 0.22
    assert abs(agg["brier_raw"] - 0.22) < 1e-6, f"brier_raw {agg['brier_raw']}"
    assert abs(agg["brier_calibrated"] - 0.185) < 1e-4, f"brier_cal {agg['brier_calibrated']}"
    assert agg["_n_eval"] == 40, f"_n_eval {agg['_n_eval']}"
    # ECE bins: bin0 (0.0-0.1) has 2+2=4 samples; bin5 (0.5-0.6) has 8+8=16
    bins = agg["ece_bins"]
    assert len(bins) == 10, f"Expected 10 bins, got {len(bins)}"
    assert bins[0][2] == 4, f"bin0 count {bins[0][2]}"
    assert bins[5][2] == 16, f"bin5 count {bins[5][2]}"
    assert abs(bins[0][3] - 0.5) < 1e-9, f"bin0 acc {bins[0][3]}"
    assert abs(bins[5][3] - 0.6) < 1e-9, f"bin5 acc {bins[5][3]}"
    print(f"  L0-AT3 PASS: calibration quality weighted (brier_raw={agg['brier_raw']}, ece bins pooled)")


def test_l0_at4_aggregate_failure_counters():
    """GAP3: aggregate_failure_counters sums all counters + _pct_successful."""
    fold_records = [
        _make_fold_record(fired=10, sig_exc=1, fus_exc=0),
        _make_fold_record(fired=20, sig_exc=3, fus_exc=2),
    ]
    agg = aggregate_failure_counters(fold_records)
    assert agg["n_signal_exceptions"] == 4, f"sig_exc {agg['n_signal_exceptions']}"
    assert agg["n_fusion_exceptions"] == 2, f"fus_exc {agg['n_fusion_exceptions']}"
    assert agg["_pct_successful"] > 0, f"_pct_successful {agg['_pct_successful']}"
    print(f"  L0-AT4 PASS: failure counters summed (sig={agg['n_signal_exceptions']}, fus={agg['n_fusion_exceptions']}, pct={agg['_pct_successful']})")


def test_l0_at5_aggregate_failure_accounting():
    """GAP3: _aggregate_failure_accounting sums across configs, excludes _pct_successful."""
    config_records = [
        {"trades": 10, "failure_counters": {"n_signal_exceptions": 1, "n_fusion_exceptions": 0}},
        {"trades": 20, "failure_counters": {"n_signal_exceptions": 2, "n_fusion_exceptions": 1}},
    ]
    agg = _aggregate_failure_accounting(config_records)
    assert agg["n_signal_exceptions"] == 3, f"sig {agg['n_signal_exceptions']}"
    assert agg["n_fusion_exceptions"] == 1, f"fus {agg['n_fusion_exceptions']}"
    assert "_pct_successful" in agg, "missing _pct_successful"
    assert abs(agg["_pct_successful"] - round(30 / 34, 4)) < 1e-9, f"pct {agg['_pct_successful']}"
    print(f"  L0-AT5 PASS: failure accounting summed across configs (pct={agg['_pct_successful']:.4f})")


def test_l0_at6_persistence_baseline():
    """GAP4: compute_baseline_accuracies returns persistence + trend baselines."""
    # Build 10-day synthetic data: strictly rising temps -> persistence predicts up correctly most days
    stations = ["KNYC"]
    settlements = {}
    for stn in stations:
        d = {}
        base = datetime.date(2025, 5, 1)
        for i in range(12):
            ds = (base + datetime.timedelta(days=i)).isoformat()
            d[ds] = 60.0 + i * 1.0
        settlements[stn] = d

    bl = compute_baseline_accuracies(settlements, stations, epsilon=0.5,
                                     holdout_start=None, holdout_end=None, period="eval")
    assert "persistence_accuracy" in bl, f"Missing persistence: {bl.keys()}"
    assert "trend_accuracy" in bl, f"Missing trend: {bl.keys()}"
    assert "persistence_trades" in bl, "Missing persistence_trades"
    assert bl["persistence_trades"] > 0, "No persistence trades computed"
    # Rising series: yesterday's direction is up, today's is up -> persistence should be ~100%
    assert bl["persistence_accuracy"] >= 0.9, f"Persistence should be high on monotonic rise: {bl['persistence_accuracy']}"
    print(f"  L0-AT6 PASS: persistence_accuracy={bl['persistence_accuracy']:.4f} ({bl['persistence_trades']} trades), "
          f"trend_accuracy={bl['trend_accuracy']:.4f} ({bl['trend_trades']} trades)")


def test_l0_at7_ece_bins_in_calibration_quality():
    """GAP2: compute_fusion_accuracy emits ece_bins + _n_eval in calibration_quality."""
    from scripts.ws5_common import compute_fusion_accuracy

    # Minimal synthetic setup exercising the ECE path
    class _MockUpSignal:
        name = "mock_up"
        min_lookback = 0
        def evaluate(self, idx, days):
            return ("up", 0.6)

    class _MockDownSignal:
        name = "mock_down"
        min_lookback = 0
        def evaluate(self, idx, days):
            return ("down", 0.7)

    class _MockRegistry:
        def __init__(self):
            self.signals = {"mock_up": _MockUpSignal(), "mock_down": _MockDownSignal()}
        def get_signal(self, name):
            return self.signals.get(name)

    class _MockFuser:
        calibration_pipeline = type('obj', (object,), {'calibrate': lambda self, sig, stn, raw: raw})()
        def is_calibrated(self):
            return False
        def fuse(self, signal_predictions, station=None):
            return {"direction": "up", "confidence": 0.55}
        def fuse_signals(self, signals, city_code, use_ds_conflict=True):
            return ("up", 0.55)

    import core.signals as sig_mod
    orig_registered = sig_mod.REGISTERED_SIGNAL_NAMES
    orig_degenerate = sig_mod.DEGENERATE_SIGNAL_NAMES
    sig_mod.REGISTERED_SIGNAL_NAMES = ["mock_up", "mock_down"]
    sig_mod.DEGENERATE_SIGNAL_NAMES = []

    stations = ["KNYC"]
    base = datetime.date(2025, 5, 1)
    date_strs = [(base + datetime.timedelta(days=i)).isoformat() for i in range(10)]
    temps = [60.0 + i for i in range(10)]
    settlements = {stn: {ds: t for ds, t in zip(date_strs, temps)} for stn in stations}
    metar_batched = {
        stn: [{"date": ds, "high": t + 5, "low": t - 5, "temp": t,
               "dewpoint": t - 10, "wind_dir": 180.0, "wind_speed": 8.0,
               "pressure": 1013.0} for ds, t in zip(date_strs, temps)]
        for stn in stations
    }

    try:
        result = compute_fusion_accuracy(
            settlements=settlements,
            metar_batched=metar_batched,
            registry=_MockRegistry(),
            fuser=_MockFuser(),
            fusion_method="llop",
            station_list=stations,
            confidence_floor=0.0,
            kelly_fraction=1.0,
            calibration_cutoff_date=None,
            holdout_start=None,
            holdout_end=None,
            period="eval",
            exclude_degenerate=False,
            calibration_method="isotonic",
        )
        cq = result["calibration_quality"]
        assert "ece_bins" in cq, "ece_bins missing from calibration_quality"
        assert len(cq["ece_bins"]) == 10, f"Expected 10 bins, got {len(cq['ece_bins'])}"
        assert "_n_eval" in cq, "_n_eval missing from calibration_quality"
        assert cq["_n_eval"] > 0, f"_n_eval should be > 0, got {cq['_n_eval']}"
        # per-signal stats carry n_eval for downstream weighted aggregation
        for sig, stats in result["per_signal"].items():
            assert "n_eval" in stats, f"n_eval missing for {sig}"
        print(f"  L0-AT7 PASS: ece_bins (10) + _n_eval={cq['_n_eval']} in calibration_quality")
    finally:
        sig_mod.REGISTERED_SIGNAL_NAMES = orig_registered
        sig_mod.DEGENERATE_SIGNAL_NAMES = orig_degenerate


def test_l0_at8_fold_record_persists_diagnostics():
    """GAP1/GAP7: evaluate_config_on_fold persists per_signal/per_station through fold boundary."""
    from scripts.run_sweep import evaluate_config_on_fold
    import inspect
    # Structural test: the return dict must include the new keys
    src = inspect.getsource(evaluate_config_on_fold)
    for key in ['"per_signal": result.get("per_signal", {})',
                '"per_station": result.get("per_station", {})',
                '"pct_successful"']:
        assert key in src, f"Missing {key} in evaluate_config_on_fold return"
    print(f"  L0-AT8 PASS: evaluate_config_on_fold return includes per_signal/per_station/pct_successful")


def test_l0_at9_effective_config_scope():
    """GAP5: effective_config_scope present in both single-split and nested result shapes."""
    from scripts.run_sweep import run_sweep_single_split, run_sweep_nested
    import inspect
    for fn in (run_sweep_single_split, run_sweep_nested):
        src = inspect.getsource(fn)
        assert "effective_config_scope" in src, f"Missing effective_config_scope in {fn.__name__}"
        assert "n_unique_directional_configs" in src, f"Missing n_unique_directional_configs in {fn.__name__}"
        assert "cosmetic_dimensions" in src, f"Missing cosmetic_dimensions in {fn.__name__}"
    print(f"  L0-AT9 PASS: effective_config_scope present in single-split and nested paths")


def test_l0_at10_data_sources_provenance():
    """GAP8: data_sources provenance block present in JSON output construction."""
    with open(os.path.join(os.path.dirname(__file__), "..", "scripts", "run_sweep.py")) as f:
        content = f.read()
    for token in ['"data_sources"', '"settlements_db"', '"metar_db"',
                  '"n_stations"', '"n_settlement_dates"', '"n_registered_signals"']:
        assert token in content, f"Missing {token} in run_sweep.py JSON construction"
    assert '"failure_accounting"' in content, "Missing failure_accounting in JSON output"
    assert '"effective_config_scope"' in content, "Missing effective_config_scope in JSON output"
    print(f"  L0-AT10 PASS: data_sources + failure_accounting + effective_config_scope in JSON output")


def test_l0_at11_s7_invariant_with_new_fields():
    """S7 regression: ranking invariant holds with new diagnostic fields present."""
    from scripts.run_sweep import assert_s7_ranking_invariant
    configs = [
        {"params": {"fusion": "llop", "calibration": "isotonic", "confidence_floor": 0.5, "kelly_fraction": 0.25},
         "accuracy": 0.64, "net_pnl": 100.0, "net_pnl_per_trade": 0.02, "sharpe": 1.2,
         "profit_factor": 1.5, "kelly_adjusted_pnl": 50.0, "win_rate": 0.55,
         "max_drawdown": 0.1, "per_signal": {}, "per_station": {},
         "calibration_quality": {}, "failure_counters": {}},
        {"params": {"fusion": "uwc", "calibration": "isotonic", "confidence_floor": 0.55, "kelly_fraction": 0.5},
         "accuracy": 0.58, "net_pnl": 200.0, "net_pnl_per_trade": 0.04, "sharpe": 2.0,
         "profit_factor": 1.8, "kelly_adjusted_pnl": 100.0, "win_rate": 0.60,
         "max_drawdown": 0.05, "per_signal": {}, "per_station": {},
         "calibration_quality": {}, "failure_counters": {}},
    ]
    configs.sort(key=lambda x: x["accuracy"], reverse=True)
    rank_before = [tuple(sorted(c["params"].items())) for c in configs]
    assert_s7_ranking_invariant(configs)
    rank_after = [tuple(sorted(c["params"].items())) for c in configs]
    assert rank_before == rank_after, "Ranking changed with new fields present"
    print(f"  L0-AT11 PASS: S7 ranking invariant holds with new diagnostic fields")


def test_l0_at12_stations_immutable():
    """STATIONS is immutable tuple (prevents hidden mutation by importers)."""
    from scripts.sweep.config import STATIONS
    assert isinstance(STATIONS, tuple), f"STATIONS should be tuple, got {type(STATIONS).__name__}"
    assert len(STATIONS) == 20, f"Expected 20 stations, got {len(STATIONS)}"
    # Verify KDAL not present (removed as KDFW duplicate)
    assert "KDAL" not in STATIONS, "KDAL should not be in canonical STATIONS"
    print(f"  L0-AT12 PASS: STATIONS is tuple({len(STATIONS)}) — immutable")


def test_l0_at13_fee_canonical():
    """compute_pnl_metrics uses canonical kalshi_fee(1, confidence), price-dependent."""
    from scripts.ws5_common import compute_pnl_metrics
    # Fee at conf=0.50: kalshi_fee(1, 0.50) = $0.02  -> P&L = $1 - $0.02 = $0.98
    pnl_50 = compute_pnl_metrics([{"correct": True, "confidence": 0.50}])
    assert abs(pnl_50["net_pnl"] - 0.98) < 1e-9, (
        f"Expected $0.98 at conf=0.50, got ${pnl_50['net_pnl']}"
    )
    # Fee at conf=0.86: kalshi_fee(1, 0.86) = $0.01  -> P&L = $1 - $0.01 = $0.99
    pnl_86 = compute_pnl_metrics([{"correct": True, "confidence": 0.86}])
    assert abs(pnl_86["net_pnl"] - 0.99) < 1e-9, (
        f"Expected $0.99 at conf=0.86, got ${pnl_86['net_pnl']}"
    )
    # Fee at conf=0.50 loss: P&L = -$1 - $0.02 = -$1.02
    pnl_loss = compute_pnl_metrics([{"correct": False, "confidence": 0.50}])
    assert abs(pnl_loss["net_pnl"] - (-1.02)) < 1e-9, (
        f"Expected -$1.02 at conf=0.50 loss, got ${pnl_loss['net_pnl']}"
    )
    # Fee varies with confidence (price-dependent)
    assert pnl_50["net_pnl"] != pnl_86["net_pnl"], (
        "Fee should vary with confidence: conf=0.50 and conf=0.86 should differ"
    )
    # Disclaimer references canonical fee
    disc = pnl_50["_disclaimer"]
    assert "kalshi_fee" in disc.lower() or "canonical" in disc.lower(), (
        f"Disclaimer should reference canonical fee: {disc[:60]}"
    )
    print(f"  L0-AT13 PASS: canonical fee price-dependent "
          f"(conf=0.50: ${pnl_50['net_pnl']}, conf=0.86: ${pnl_86['net_pnl']})")


def test_l0_at14_fee_price_dependent():
    """Fee is price-dependent (known kalshi_fee values), not flat."""
    from scripts.ws5_common import compute_pnl_metrics
    # Known kalshi_fee(1, confidence) values:
    #   conf=0.50 -> $0.02, conf=0.70 -> $0.02, conf=0.86 -> $0.01, conf=0.95 -> $0.01
    # Expected P&L = $1.00 - kalshi_fee(1, conf) for wins
    known = {0.50: 0.98, 0.70: 0.98, 0.86: 0.99, 0.95: 0.99}
    for conf, expected_pnl in known.items():
        pnl = compute_pnl_metrics([{"correct": True, "confidence": conf}])
        assert abs(pnl["net_pnl"] - expected_pnl) < 1e-9, (
            f"At conf={conf}: expected ${expected_pnl}, got ${pnl['net_pnl']}"
        )
    # Verify slippage adds $0.002
    pnl_base = compute_pnl_metrics([{"correct": True, "confidence": 0.50}], fee_model="taker_only")
    pnl_slp = compute_pnl_metrics([{"correct": True, "confidence": 0.50}], fee_model="taker_and_slippage")
    diff = pnl_base["net_pnl"] - pnl_slp["net_pnl"]
    assert abs(diff - 0.002) < 1e-9, f"Slippage should add $0.002, got ${diff}"
    # Disclaimer references canonical fee
    disc = pnl_base["_disclaimer"]
    assert "canonical" in disc.lower(), f"Disclaimer should reference canonical fee: {disc[:60]}"
    # Verify NOT flat: conf=0.50 and conf=0.86 produce different P&L
    assert known[0.50] != known[0.86], "Fee should vary with confidence"
    print(f"  L0-AT14 PASS: price-dependent fee verified at 4 conf levels, "
          f"slippage=${diff}, non-flat confirmed")


def test_l0_at15_calibration_cutoff_default():
    """--calibration-cutoff CLI default is 2026-04-30 (refreeze recommendation)."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    # Read the default from run_sweep.py source
    with open(os.path.join(os.path.dirname(__file__), "..", "scripts", "run_sweep.py")) as f:
        content = f.read()
    assert "default=\"2026-04-30\"" in content, (
        "Default --calibration-cutoff should be 2026-04-30"
    )
    assert "2024-12-31" not in content.split("default=")[0] + content.split("default=")[-1], (
        "Stale 2024-12-31 default should not remain"
    )
    # Verify NESTED_FOLDS per-fold cutoffs are unchanged
    from scripts.run_sweep import NESTED_FOLDS
    expected_cutoffs = ["2025-04-30", "2025-08-31", "2025-11-30", "2026-02-28"]
    for fd in NESTED_FOLDS:
        assert fd["calibration_cutoff"] in expected_cutoffs, (
            f"Unexpected fold cutoff {fd['calibration_cutoff']}"
        )
    print(f"  L0-AT15 PASS: CLI default changed to 2026-04-30, fold cutoffs unchanged")


if __name__ == "__main__":
    print("=" * 60)
    print("LAYER 0 REMEDIATION — FOCUSED QC TESTS")
    print("=" * 60)
    tests = [
        ("L0-AT1", test_l0_at1_aggregate_per_signal),
        ("L0-AT2", test_l0_at2_aggregate_per_station),
        ("L0-AT3", test_l0_at3_aggregate_calibration_quality),
        ("L0-AT4", test_l0_at4_aggregate_failure_counters),
        ("L0-AT5", test_l0_at5_aggregate_failure_accounting),
        ("L0-AT6", test_l0_at6_persistence_baseline),
        ("L0-AT7", test_l0_at7_ece_bins_in_calibration_quality),
        ("L0-AT8", test_l0_at8_fold_record_persists_diagnostics),
        ("L0-AT9", test_l0_at9_effective_config_scope),
        ("L0-AT10", test_l0_at10_data_sources_provenance),
        ("L0-AT11", test_l0_at11_s7_invariant_with_new_fields),
        ("L0-AT12", test_l0_at12_stations_immutable),
        ("L0-AT13", test_l0_at13_fee_canonical),
        ("L0-AT14", test_l0_at14_fee_price_dependent),
        ("L0-AT15", test_l0_at15_calibration_cutoff_default),
    ]
    passed = 0
    failed = 0
    for name, func in tests:
        try:
            func()
            passed += 1
        except Exception as e:
            print(f"  {name} FAIL: {e}")
            failed += 1
            import traceback
            traceback.print_exc()
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(tests)} total")
    print(f"{'=' * 60}")
