#!/usr/bin/env python3
"""A4 — evaluators/calibrators must use the strict strike-crossing axis, and the
operator must come from the market's OWN `strike_type`.

Regression for the day-over-day `bucket > prev_bucket` scoring in
core/unified_backtest.py and the calibration consumers, PLUS the 2026-10-14
bug-hunt finding #3: Kalshi daily-high events publish BOTH an "or above" tail
(`strike_type='greater'`, YES iff observed > floor) and an "or below" tail
(`strike_type='less'`, YES iff observed < cap), and the `-T##` ticker suffix is
used for BOTH. Resolving the operator from `market_type` alone INVERTS the label
on every less tail.

Tie rule — VERIFIED AGAINST KALSHI'S OWN API (2026-10-14). The bug-hunt report
asserted Kalshi is inclusive (`>=`). It is NOT: Kalshi's own titles read ">96°"
and "<92°", and every observed tie (observed == bound) settled `result='no'` on
both tails. Both tails are STRICT; equality is a LOSS. That is pinned below.

Run: python3 -m pytest tests/test_strike_crossing_axis.py -v
     python3 tests/test_strike_crossing_axis.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.settlement_labels import (
    strike_crossing_yes, strike_crossing_direction, settle_yes_from_strike_type,
)


def test_strict_equality_is_a_loss():
    """T == strike is a LOSS on both tails (verified vs Kalshi API result='no')."""
    assert strike_crossing_yes(70, 70, "HIGH") is False
    assert strike_crossing_yes(70, 70, "LOW") is False
    assert strike_crossing_yes(71, 70, "HIGH") is True
    assert strike_crossing_yes(69, 70, "LOW") is True
    assert strike_crossing_direction(70, 70, "HIGH") == "no"


def test_operator_comes_from_strike_type_not_the_market_type():
    """A HIGH `less` tail is an 'or below' market — NOT 'observed > strike'.

    Real case: KXHIGHCHI-26OCT09-T71 is strike_type='less' (KMDW high 74).
    A HIGH buy_yes there is a LOSS (74 is not < 71); the legacy market_type
    fallback would wrongly call it a WIN.
    """
    # less tail: YES iff observed < cap
    assert strike_crossing_yes(74, 71, "HIGH", strike_type="less", cap_strike=71) is False
    assert strike_crossing_yes(68, 69, "HIGH", strike_type="less", cap_strike=69) is True
    assert strike_crossing_yes(69, 69, "HIGH", strike_type="less", cap_strike=69) is False
    # greater tail: YES iff observed > floor
    assert strike_crossing_yes(74, 71, "HIGH", strike_type="greater", floor_strike=71) is True
    assert strike_crossing_yes(71, 71, "HIGH", strike_type="greater", floor_strike=71) is False
    # between: floor <= observed <= cap
    assert strike_crossing_yes(70, 70, "HIGH", strike_type="between", floor_strike=69, cap_strike=70) is True
    assert strike_crossing_yes(71, 70, "HIGH", strike_type="between", floor_strike=69, cap_strike=70) is False


def test_settle_yes_from_strike_type_matrix():
    assert settle_yes_from_strike_type(70, "greater", floor_strike=70) is False
    assert settle_yes_from_strike_type(71, "greater", floor_strike=70) is True
    assert settle_yes_from_strike_type(70, "less", cap_strike=70) is False
    assert settle_yes_from_strike_type(69, "less", cap_strike=70) is True
    assert settle_yes_from_strike_type(70, "between", floor_strike=70, cap_strike=72) is True
    assert settle_yes_from_strike_type(73, "between", floor_strike=70, cap_strike=72) is False
    # missing bound for a tail fails loud (no silent operator)
    try:
        settle_yes_from_strike_type(70, "greater")
    except ValueError:
        pass
    else:
        raise AssertionError("greater tail without floor_strike must raise")
    try:
        settle_yes_from_strike_type(70, "bogus", floor_strike=70)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown strike_type must raise")


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
    test_operator_comes_from_strike_type_not_the_market_type()
    test_settle_yes_from_strike_type_matrix()
    test_unified_backtest_uses_strike_axis_not_day_over_day()
    test_unified_backtest_returns_label_axis()
    test_calibration_consumers_rekeyed()
    print("test_strike_crossing_axis: PASS")
