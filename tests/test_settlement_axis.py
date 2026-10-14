#!/usr/bin/env python3
"""Workstream E4 (GR4 G1) — settlement axis contract.

The Kalshi settlement axis is STRICT strike-crossing, not day-over-day, AND the
operator is set by the market's OWN `strike_type`:

    strike_type='greater': YES iff settlement_temp_f > floor_strike
    strike_type='less':    YES iff settlement_temp_f < cap_strike
    strike_type='between': YES iff floor_strike <= settlement_temp_f <= cap_strike

2026-10-14 bug-hunt finding #3 correction: Kalshi daily-high events publish BOTH
an "or above" tail (`greater`) and an "or below" tail (`less`), and the `-T##`
ticker suffix is used for BOTH. Resolving the operator from `market_type` alone
inverts every `less` tail (the SBOX book's dominant error: 76 rows).

Tie rule — VERIFIED AGAINST KALSHI'S OWN API (2026-10-14): the report's claim
that Kalshi is inclusive (`>=`) is FALSE. Kalshi's titles read ">96°"/"<92°" and
every observed tie settled `result='no'`. Both tails STRICT; equality is a LOSS.

Every label/evaluator/calibrator path must route through
``core.settlement_labels.strike_crossing_yes``. This test drives a counterexample
corpus (equality rows, both tails, both sides, a strict-vs-non-strict
discriminator) through the canonical helper AND through every live consumer, and
rejects the ``day_over_day`` axis.

Run: .venv-gefs/bin/python -m pytest tests/test_settlement_axis.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.settlement_labels import strike_crossing_yes, strike_crossing_direction


# ── counterexample corpus ────────────────────────────────────────────────────
# (settlement_temp_f, strike_price, market_type, strike_type, floor, cap, expected_yes)
# Equality rows (temp == bound) are the discriminator: a non-strict (> / <)
# implementation marks them YES; the strict contract makes them a LOSS.
CORPUS = [
    # greater ("or above") tail — temp above / at / below floor
    (71.0, 70.0, "HIGH", "greater", 70.0, None, True),
    (70.0, 70.0, "HIGH", "greater", 70.0, None, False),   # equality -> LOSS
    (69.0, 70.0, "HIGH", "greater", 70.0, None, False),
    (90.0, 89.0, "HIGH", "greater", 89.0, None, True),
    (0.5, 0.5, "HIGH", "greater", 0.5, None, False),
    # less ("or below") tail — temp below / at / above cap  (the SBOX inversion case)
    (69.0, 70.0, "HIGH", "less", None, 70.0, True),
    (70.0, 70.0, "HIGH", "less", None, 70.0, False),      # equality -> LOSS
    (71.0, 70.0, "HIGH", "less", None, 70.0, False),
    (74.0, 71.0, "HIGH", "less", None, 71.0, False),      # KMDW 26OCT09-T71 real case
    (68.0, 71.0, "HIGH", "less", None, 71.0, True),
    # between range
    (70.0, 70.0, "HIGH", "between", 69.0, 70.0, True),
    (71.0, 70.0, "HIGH", "between", 69.0, 70.0, False),
    (75.5, 70.0, "LOW", "between", 75.0, 76.0, True),
    (40.0, 45.0, "LOW", "less", None, 45.0, True),
]


def test_corpus_shape():
    assert len(CORPUS) == 14
    assert {r[2] for r in CORPUS} == {"HIGH", "LOW"}
    assert {r[3] for r in CORPUS} == {"greater", "less", "between"}
    eq = [r for r in CORPUS if r[1] == r[4] or r[1] == r[5]]
    assert len(eq) >= 4, "corpus must carry equality counterexamples"


@pytest.mark.parametrize("temp,strike,market,stype,floor,cap,expected", CORPUS)
def test_strike_crossing_yes_matches_corpus(temp, strike, market, stype, floor, cap, expected):
    assert strike_crossing_yes(temp, strike, market, strike_type=stype,
                               floor_strike=floor, cap_strike=cap) is expected


@pytest.mark.parametrize("temp,strike,market,stype,floor,cap,expected", CORPUS)
def test_direction_helper_agrees(temp, strike, market, stype, floor, cap, expected):
    assert strike_crossing_direction(temp, strike, market, strike_type=stype,
                                     floor_strike=floor, cap_strike=cap) == (
        "yes" if expected else "no")


def test_equality_is_loss_both_tails():
    """The core discriminator: equality must be a LOSS on both tails."""
    assert strike_crossing_yes(70.0, 70.0, "HIGH", strike_type="greater", floor_strike=70.0) is False
    assert strike_crossing_yes(70.0, 70.0, "HIGH", strike_type="less", cap_strike=70.0) is False


def test_unknown_market_is_rejected():
    """No silent axis: an unknown market_type must raise, not guess."""
    with pytest.raises(ValueError):
        strike_crossing_yes(70.0, 70.0, "MID")


def test_unknown_strike_type_is_rejected():
    with pytest.raises(ValueError):
        strike_crossing_yes(70.0, 70.0, "HIGH", strike_type="neither", floor_strike=70.0)


def test_canonical_settlement_routes_through_strike_crossing():
    """core.canonical_settlement.canonical_settle_yes must equal the corpus."""
    from core.canonical_settlement import canonical_settle_yes
    for temp, strike, market, stype, floor, cap, expected in CORPUS:
        assert canonical_settle_yes(temp, strike, market, strike_type=stype,
                                    floor_strike=floor, cap_strike=cap) == int(expected)


def test_gate_composer_rejects_day_over_day_axis():
    """The pre-ingest gate must REJECT a non-strike-crossing axis."""
    from scripts.gate_composer import gate_g4_axis
    good = {"label_axis": "strike_crossing"}
    assert gate_g4_axis(good).status == "PASS"
    bad = {"label_axis": "day_over_day"}
    assert gate_g4_axis(bad).status != "PASS", "day_over_day axis was not rejected"


def test_live_consumers_declare_strike_axis():
    """No live evaluator/calibrator may score on the day-over-day axis."""
    root = os.path.join(os.path.dirname(__file__), "..")
    sources = {
        "core/unified_backtest.py": 'LABEL_AXIS = \'strike_crossing\'',
        "core/calibrate_all_signals.py": 'LABEL_AXIS = "strike_crossing"',
        "scripts/run_calibration_pipeline.py": 'LABEL_AXIS = "strike_crossing"',
        "core/intraday/calibration.py": 'LABEL_AXIS = "strike_crossing"',
    }
    for rel, needle in sources.items():
        src = open(os.path.join(root, rel)).read()
        assert needle in src, f"{rel} does not declare the strike-crossing axis"


if __name__ == "__main__":
    test_corpus_shape()
    test_equality_is_loss_both_tails()
    test_unknown_market_is_rejected()
    test_unknown_strike_type_is_rejected()
    test_canonical_settlement_routes_through_strike_crossing()
    test_live_consumers_declare_strike_axis()
    print("test_settlement_axis: PASS")
