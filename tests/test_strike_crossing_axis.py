#!/usr/bin/env python3
"""A4 — evaluators/calibrators must use the strict strike-crossing axis.

Regression for the day-over-day `bucket > prev_bucket` scoring in
core/unified_backtest.py and the calibration consumers. The Kalshi settlement
axis is strict strike-crossing: settlement > strike => win; equality => loss
(core/settlement_labels.py).

Run: python3 -m pytest tests/test_strike_crossing_axis.py -v
     python3 tests/test_strike_crossing_axis.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.settlement_labels import strike_crossing_yes, strike_crossing_direction


def test_strict_equality_is_a_loss():
    """T == strike is a LOSS on both HIGH and LOW markets."""
    assert strike_crossing_yes(70, 70, "HIGH") is False
    assert strike_crossing_yes(70, 70, "LOW") is False
    assert strike_crossing_yes(71, 70, "HIGH") is True
    assert strike_crossing_yes(69, 70, "LOW") is True
    assert strike_crossing_direction(70, 70, "HIGH") == "no"


def test_unified_backtest_uses_strike_axis_not_day_over_day():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "core", "unified_backtest.py")).read()
    assert "LABEL_AXIS = 'strike_crossing'" in src, "unified_backtest axis constant missing"
    # The day-over-day SCORING line must be gone (the loader may still carry the field).
    assert "'up' if actual['settlement_bucket'] > prev_bucket" not in src, \
        "unified_backtest still scores day-over-day (bucket > prev_bucket)"
    assert "strike_ref" in src and "nearest_strike" in src, \
        "unified_backtest does not key off the realized strike ladder"
    assert "'label_axis': label_axis" in src, "unified_backtest lacks label_axis provenance"


def test_unified_backtest_returns_label_axis():
    """run_backtest output must carry label_axis provenance."""
    import core.signal_fusion as sf
    import core.fusion_logic as fl
    sf.SignalFusionEngine = fl.SignalFusionEngine
    sf.TimeDecaySignalManager = fl.TimeDecaySignalManager
    import core.unified_backtest as ub
    res = ub.run_backtest(stations=["KATL"])
    assert "label_axis" in res, "run_backtest result missing label_axis"
    assert res["label_axis"] in ("strike_crossing", "MIXED_OR_NON_STRIKE_CROSSING")


def test_calibration_consumers_rekeyed():
    root = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("core/calibrate_all_signals.py", "scripts/run_calibration_pipeline.py"):
        src = open(os.path.join(root, rel)).read()
        assert "strike_ref" in src, f"{rel} does not use the strike ladder"
        assert "LABEL_AXIS = \"strike_crossing\"" in src, f"{rel} axis constant missing"
    intraday = open(os.path.join(root, "core", "intraday", "calibration.py")).read()
    assert "LABEL_AXIS = \"strike_crossing\"" in intraday, "intraday calibration axis missing"
    assert "Refusing calibration curves" in intraday, "intraday calibration lacks axis rejection"


if __name__ == "__main__":
    test_strict_equality_is_a_loss()
    test_unified_backtest_uses_strike_axis_not_day_over_day()
    test_calibration_consumers_rekeyed()
    test_unified_backtest_returns_label_axis()
    print("test_strike_crossing_axis: PASS")
