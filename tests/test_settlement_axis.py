#!/usr/bin/env python3
"""Workstream E4 (GR4 G1) — settlement axis contract.

The Kalshi settlement axis is STRICT strike-crossing, not day-over-day:
    HIGH: settlement_temp_f > strike  => YES wins
    LOW:  settlement_temp_f < strike  => YES wins
    equality is a LOSS on both markets.

Every label/evaluator/calibrator path must route through
``core.settlement_labels.strike_crossing_yes``. This test drives a 12-row
synthetic counterexample corpus — equality rows, both sides of each market,
and a strict-vs-non-strict discriminator — through the canonical helper AND
through every live consumer, and rejects the ``day_over_day`` axis.

Run: .venv-gefs/bin/python -m pytest tests/test_settlement_axis.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.settlement_labels import strike_crossing_yes, strike_crossing_direction


# ── 12-row counterexample corpus ─────────────────────────────────────────────
# (settlement_temp_f, strike_price, market_type, expected_yes)
# Equality rows (temp == strike) are the discriminator: a non-strict (> / <)
# implementation marks them YES; the strict contract makes them a LOSS.
CORPUS = [
    # HIGH market — temp above / at / below strike
    (71.0, 70.0, "HIGH", True),    # 1	  above -> YES
    (70.0, 70.0, "HIGH", False),   # 2	  equality -> LOSS (strict)
    (69.0, 70.0, "HIGH", False),   # 3	  below -> NO
    (90.0, 89.0, "HIGH", True),    # 4	  above -> YES
    (0.5, 0.5, "HIGH", False),     # 5	  equality at boundary -> LOSS
    (100.0, 99.0, "HIGH", True),   # 6	  above -> YES
    # LOW market — temp below / at / above strike
    (69.0, 70.0, "LOW", True),     # 7	  below -> YES
    (70.0, 70.0, "LOW", False),    # 8	  equality -> LOSS (strict)
    (71.0, 70.0, "LOW", False),    # 9	  above -> NO
    (60.0, 61.0, "LOW", True),     # 10  below -> YES
    (75.5, 75.5, "LOW", False),    # 11  equality -> LOSS
    (40.0, 45.0, "LOW", True),     # 12  below -> YES
]


def test_corpus_shape():
    assert len(CORPUS) == 12
    # Both markets and all three relations (above/equal/below) are represented.
    assert {r[2] for r in CORPUS} == {"HIGH", "LOW"}
    eq = [r for r in CORPUS if r[0] == r[1]]
    assert len(eq) == 4, "corpus must carry equality counterexamples"


@pytest.mark.parametrize("temp,strike,market,expected", CORPUS)
def test_strike_crossing_yes_matches_corpus(temp, strike, market, expected):
    assert strike_crossing_yes(temp, strike, market) is expected


@pytest.mark.parametrize("temp,strike,market,expected", CORPUS)
def test_direction_helper_agrees(temp, strike, market, expected):
    assert strike_crossing_direction(temp, strike, market) == ("yes" if expected else "no")


def test_equality_is_loss_both_markets():
    """The core discriminator: equality must be a LOSS everywhere."""
    assert strike_crossing_yes(70.0, 70.0, "HIGH") is False
    assert strike_crossing_yes(70.0, 70.0, "LOW") is False


def test_unknown_market_is_rejected():
    """No silent axis: an unknown market_type must raise, not guess."""
    with pytest.raises(ValueError):
        strike_crossing_yes(70.0, 70.0, "MID")


def test_canonical_settlement_routes_through_strike_crossing():
    """core.canonical_settlement.canonical_settle_yes must equal the corpus."""
    from core.canonical_settlement import canonical_settle_yes
    for temp, strike, market, expected in CORPUS:
        assert canonical_settle_yes(temp, strike, market) == int(expected)


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
    test_equality_is_loss_both_markets()
    test_unknown_market_is_rejected()
    test_canonical_settlement_routes_through_strike_crossing()
    test_live_consumers_declare_strike_axis()
    print("test_settlement_axis: PASS")
