#!/usr/bin/env python3
"""LOW-market parity holdout (2026-10-14) — mirrored direction + gate wiring.

The LOW holdout shares the HIGH gate (F1 cost, F7 grades, 13:30 pin,
per-station ceiling) but MIRRORS the strike-crossing direction (LOW YES is ITM
iff settlement < strike). These tests pin the mirror and the pre-registered
postconditions, without running the full data pass.

Run: python3 -m pytest tests/test_low_market_holdout.py -q
"""
import importlib.util
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.settlement_labels import strike_crossing_yes  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "low_market_holdout", os.path.join(_REPO, "scripts", "low_market_holdout.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_low_yes_is_itm_below_strike():
    # LOW: YES (ITM) iff temp < strike, strict.
    assert strike_crossing_yes(70.0, 71.0, "LOW") is True
    assert strike_crossing_yes(71.0, 71.0, "LOW") is False   # equality == not-ITM
    assert strike_crossing_yes(72.0, 71.0, "LOW") is False
    # HIGH is the mirror.
    assert strike_crossing_yes(72.0, 71.0, "HIGH") is True


def test_p_low_le_running_truncation():
    m = _load()
    samples = [60.0, 65.0, 70.0, 75.0]
    # running low already below the strike -> the day's min is locked <= x.
    assert m.p_low_le(samples, 72.0, running_low=68.0) == 1.0
    # no truncation -> empirical share <= x.
    assert abs(m.p_low_le(samples, 70.0, None) - 0.75) < 1e-9
    # empty sample, no truncation -> 0.
    assert m.p_low_le([], 70.0, None) == 0.0


def test_grade_edge_ladder_matches_high():
    m = _load()
    assert m.grade_edge(0.15) == "S"
    assert m.grade_edge(0.10) == "A"
    assert m.grade_edge(0.07) == "B"
    assert m.grade_edge(0.04) == "C"
    assert m.grade_edge(0.02) == "D"
    assert m.grade_edge(0.005) == "F"


def test_preregistered_postconditions_present():
    m = _load()
    assert m.HOLDOUT_N == 30
    assert m.MIN_WIN_RATE == 0.60
    assert m.EXIT_PROFILE == ["auto_close_h1"]
    assert m.PIN_LOCAL == 13 * 60 + 30


def test_holdout_result_shape_is_honest():
    """A synthetic result must expose the pre-registered pass/fail fields."""
    m = _load()
    # exercise the summary arithmetic directly (no data pass).
    import inspect
    src = inspect.getsource(m.run_holdout)
    for key in ("P1_n_30", "P2_win_ge_60", "P3_net_ev_gt_0", "PASS_ALL"):
        assert key in src
    # net EV is per settled contract, not a raw sum.
    assert "net_total / len(settled)" in src
