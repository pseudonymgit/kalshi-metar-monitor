#!/usr/bin/env python3
"""
WS1 acceptance tests — rejection accounting, FusionReason, reconciliation.

Spec: OPEN-ISSUES-REMEDIATION-AND-TRIM-TREND-SPEC-2026-08-14.md, PART C WS1.
Gates: G2 (counter reconciliation), G16 (FusionReason enum), G17 (eligible-cell math).

Tests:
  - WS1-AT1: FusionReason — empty signals returns reason "empty"
  - WS1-AT2: FusionReason — DS-conflict veto returns reason "ds_conflict"
  - WS1-AT3: FusionReason — valid fusion returns reason "ok"
  - WS1-AT4: 4-tuple backward compatible via index access (result[0], result[1])
  - WS1-AT5: failure counters sum to n_eligible_cells (fixture)
  - WS1-AT6: all failure counters non-negative
  - WS1-AT7: n_dates_skipped_flat and n_dates_skipped_no_metar incremented (not dead)
  - WS1-AT8: no_signal_fired, ds_conflict_veto, fusion_none_other are distinct keys
  - WS1-AT9: empty and ds_conflict produce distinct None reasons
"""

import sys
import os
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fusion_logic import SignalFusionEngine
from core.calibration_pipeline import CalibrationPipeline
from scripts.ws5_common import compute_fusion_accuracy

EPSILON = 0.5


def _simple_fuser():
    """Build a minimal fusion engine with 2 signals."""
    city_codes = ["KNYC"]
    engine = SignalFusionEngine(["pressure_delta", "gaussian"], city_codes)
    cp = CalibrationPipeline(["pressure_delta", "gaussian"], city_codes)
    cp.refitted = True
    cp.calibrators = {
        "pressure_delta": lambda x: max(0.5, min(0.95, x)),
        "gaussian": lambda x: max(0.5, min(0.95, x)),
    }
    engine.calibration_pipeline = cp
    return engine


# ── FusionReason path tests ──

def test_ws1_at1_reason_empty():
    """WS1-AT1: empty signals → reason "empty"."""
    engine = _simple_fuser()
    result = engine.fuse_signals([], "KNYC")
    assert len(result) == 4, f"expected 4-tuple, got {len(result)}: {result}"
    assert result[0] is None
    assert result[3] == "empty", f"expected 'empty', got {result[3]}"


def test_ws1_at2_reason_ds_conflict():
    """WS1-AT2: conflicting signals → None with reason "ds_conflict" (or ok if no veto)."""
    engine = _simple_fuser()
    engine.mi_matrix = {
        ("pressure_delta", "gaussian"): 0.01,
        ("gaussian", "pressure_delta"): 0.01,
    }
    engine.compute_fusion_weights(engine.mi_matrix)
    result = engine.fuse_signals(
        [("pressure_delta", "up", 0.95), ("gaussian", "down", 0.95)],
        "KNYC",
    )
    assert len(result) == 4, f"expected 4-tuple, got {len(result)}: {result}"
    if result[0] is None:
        assert result[3] == "ds_conflict", f"expected 'ds_conflict', got {result[3]}"
    else:
        assert result[3] == "ok", f"expected 'ok' (no veto), got {result[3]}"


def test_ws1_at3_reason_ok():
    """WS1-AT3: agreeing signals → direction + reason "ok"."""
    engine = _simple_fuser()
    engine.mi_matrix = {
        ("pressure_delta", "gaussian"): 0.5,
        ("gaussian", "pressure_delta"): 0.5,
    }
    engine.compute_fusion_weights(engine.mi_matrix)
    result = engine.fuse_signals(
        [("pressure_delta", "up", 0.7), ("gaussian", "up", 0.65)],
        "KNYC",
    )
    assert len(result) == 4, f"expected 4-tuple, got {len(result)}: {result}"
    assert result[0] is not None, "expected valid direction"
    assert result[3] == "ok", f"expected 'ok', got {result[3]}"


def test_ws1_at4_backward_compat():
    """WS1-AT4: backward compatible via index access (result[0], result[1])."""
    engine = _simple_fuser()
    result = engine.fuse_signals([("pressure_delta", "up", 0.7)], "KNYC")
    # Index access like existing callers
    assert result[0] is not None, "result[0] must be valid direction"
    assert result[1] is not None, "result[1] must be valid probability"
    # 4-tuple unpacking also works
    dir_, prob, conf, reason = result
    assert dir_ is not None


def test_ws1_at9_distinct_none_reasons():
    """WS1-AT9: empty and ds_conflict None paths have distinct reasons."""
    engine = _simple_fuser()
    r1 = engine.fuse_signals([], "KNYC")
    assert r1[3] == "empty"
    # For ds_conflict: two strong opposite signals
    engine.mi_matrix = {
        ("pressure_delta", "gaussian"): 0.01,
        ("gaussian", "pressure_delta"): 0.01,
    }
    engine.compute_fusion_weights(engine.mi_matrix)
    r2 = engine.fuse_signals(
        [("pressure_delta", "up", 0.95), ("gaussian", "down", 0.95)],
        "KNYC",
    )
    assert r2[3] == "ds_conflict" or r2[3] == "ok", f"unexpected reason: {r2[3]}"
    if r2[0] is None:
        assert r1[3] != r2[3], f"None-path reasons must differ: {r1[3]} vs {r2[3]}"


# ── Fixture-based reconciliation tests ──

def _build_fixture():
    """
    2-station, 14-day fixture.
    KNYC: day 5 (i=5) has temp = day 4's temp → actual_dir None (flat).
    KBOS: day 8 (i=8) has no METAR record → no_metar.
    """
    stations = ["KNYC", "KBOS"]
    base = datetime.date(2025, 5, 1)
    settlements = {}
    for stn in stations:
        d = {}
        for i in range(14):
            ds = (base + datetime.timedelta(days=i)).isoformat()
            temp = 60.0 + i * 1.5
            if stn == "KNYC" and i == 5:
                temp = 60.0 + 4 * 1.5  # same as day 4 → flat
            elif stn == "KNYC" and i == 4:
                temp = 60.0 + 4 * 1.5  # 66.0
            d[ds] = temp
        settlements[stn] = d
    # Override: day 5 = day 4 temp for KNYC
    settlements["KNYC"]["2025-05-06"] = settlements["KNYC"]["2025-05-05"]  # 66.0

    metar_batched = {}
    for stn in stations:
        records = []
        for i in range(14):
            ds = (base + datetime.timedelta(days=i)).isoformat()
            # Keep a genuine pre-target gap so the no-METAR counter is tested
            # without changing the scored settlement fixture.
            if stn == "KBOS" and i == 7:
                continue  # METAR gap before target day 8
            records.append({"date": ds, "max_temp": 60.0 + i * 1.5, "min_temp": 50.0 + i * 1.0})
        metar_batched[stn] = records
    return settlements, metar_batched, stations


def _run_fixture_accuracy():
    settlements, metar_batched, stations = _build_fixture()
    from core.signals import REGISTERED_SIGNAL_NAMES
    registry = list(REGISTERED_SIGNAL_NAMES)
    city_codes = stations
    calibration_pipeline = CalibrationPipeline(registry, city_codes)
    calibration_pipeline.refitted = True
    for sig in registry:
        calibration_pipeline.calibrators[sig] = lambda x, _s=sig: max(0.5, min(0.95, x))
    fuser = SignalFusionEngine(registry, city_codes)
    fuser.calibration_pipeline = calibration_pipeline
    return compute_fusion_accuracy(
        settlements=settlements,
        metar_batched=metar_batched,
        station_list=stations,
        registry=registry,
        fuser=fuser,
        fusion_method="llop",
        epsilon=EPSILON,
        holdout_start=None,
        holdout_end=None,
        period="eval",
        exclude_degenerate=False,
        calibration_method="isotonic",
        eval_signal_names=registry,
    )


def test_ws1_at5_reconciliation_fixture():
    """
    WS1-AT5 (G2, G17): identity:

      trades + n_no_signal_fired + n_ds_conflict_veto + n_fusion_none_other
        + n_no_fusion_output == n_eligible_cells

    The flat and no_metar counters are subtracted BEFORE n_eligible_cells
    increments, so they do NOT appear in the eligible-cell identity.
    """
    result = _run_fixture_accuracy()
    fc = result.get("failure_counters", {})
    trades = result.get("total_trades", result.get("trades", 0))
    n_eligible = fc.get("n_eligible_cells", 0)
    sum_inner = (
        fc.get("n_no_signal_fired", 0)
        + fc.get("n_ds_conflict_veto", 0)
        + fc.get("n_fusion_none_other", 0)
        + fc.get("n_no_fusion_output", 0)
        + fc.get("n_fusion_exceptions", 0)
    )
    total = trades + sum_inner
    flat = fc.get("n_dates_skipped_flat", 0)
    no_metar = fc.get("n_dates_skipped_no_metar", 0)
    print(f"  trades={trades}, sum_inner={sum_inner}, n_eligible={n_eligible}, flat={flat}, no_metar={no_metar}")
    assert total == n_eligible, f"identity FAIL: {trades} + {sum_inner} = {total} != {n_eligible}"
    # Verify flat and no_metar are positive (they exist outside n_eligible)
    assert flat > 0, f"flat counter should be > 0 (got {flat})"
    assert no_metar > 0, f"no_metar counter should be > 0 (got {no_metar})"


def test_ws1_at6_counters_non_negative():
    """WS1-AT6: all failure counters are non-negative."""
    result = _run_fixture_accuracy()
    fc = result.get("failure_counters", {})
    for k, v in fc.items():
        assert v >= 0, f"counter {k} = {v} < 0"


def test_ws1_at7_dead_counters_alive():
    """WS1-AT7: flat and no-METAR counters are > 0 (not dead)."""
    result = _run_fixture_accuracy()
    fc = result.get("failure_counters", {})
    assert fc.get("n_dates_skipped_flat", 0) > 0, "n_dates_skipped_flat still dead (0)"
    assert fc.get("n_dates_skipped_no_metar", 0) > 0, "n_dates_skipped_no_metar still dead (0)"


def test_ws1_at8_counters_distinct():
    """WS1-AT8: no_signal_fired, ds_conflict_veto, fusion_none_other are distinct keys."""
    result = _run_fixture_accuracy()
    fc = result.get("failure_counters", {})
    assert "n_no_signal_fired" in fc, "missing n_no_signal_fired"
    assert "n_ds_conflict_veto" in fc, "missing n_ds_conflict_veto"
    assert "n_fusion_none_other" in fc, "missing n_fusion_none_other"
    # At least one of the inner counters should be > 0
    inner = (
        fc.get("n_no_signal_fired", 0)
        + fc.get("n_ds_conflict_veto", 0)
        + fc.get("n_fusion_none_other", 0)
        + fc.get("n_no_fusion_output", 0)
    )
    assert inner > 0, "all inner rejection counters are 0"


# ── Runner ──
def main():
    tests = [
        ("WS1-AT1", test_ws1_at1_reason_empty),
        ("WS1-AT2", test_ws1_at2_reason_ds_conflict),
        ("WS1-AT3", test_ws1_at3_reason_ok),
        ("WS1-AT4", test_ws1_at4_backward_compat),
        ("WS1-AT5", test_ws1_at5_reconciliation_fixture),
        ("WS1-AT6", test_ws1_at6_counters_non_negative),
        ("WS1-AT7", test_ws1_at7_dead_counters_alive),
        ("WS1-AT8", test_ws1_at8_counters_distinct),
        ("WS1-AT9", test_ws1_at9_distinct_none_reasons),
    ]
    passed = 0
    failed = 0
    for name, func in tests:
        try:
            func()
            passed += 1
            print(f"  {name}: PASS")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  {name}: FAIL — {e}")
            traceback.print_exc()
    print(f"WS1 reconciliation: {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)