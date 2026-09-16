"""
P0 intraday core tests — ContinuousRecalibrationLoop (Σ.1c).

Deterministic Bayesian belief updating + calibration performance metrics.
No network, no DB. Uses a throwaway state path to avoid touching production
state.
"""

import os
import tempfile

import pytest

from core.intraday.recalibration_loop import (
    CONSISTENCY_BONUS,
    ContinuousRecalibrationLoop,
)


@pytest.fixture
def loop(tmp_path):
    """A loop with isolated state file (no production state touched)."""
    return ContinuousRecalibrationLoop(state_path=str(tmp_path / "state.json"))


# ── Belief update ─────────────────────────────────────────────

def test_update_belief_weighted_average(loop):
    new_prob, new_weight = loop.update_belief(
        station="KATL", hour_before_settlement=4,
        prior_belief=0.60, prior_weight=3.0,
        new_probability=0.70, confidence=0.8,
    )
    # Weighted average should land between prior and new, closer to heavier side
    assert 0.60 < new_prob < 0.70
    assert new_weight > 3.0  # weight accumulates


def test_update_belief_clamped_to_unit_interval(loop):
    new_prob, _ = loop.update_belief(
        station="KATL", hour_before_settlement=4,
        prior_belief=0.0, prior_weight=1.0,
        new_probability=1.5, confidence=0.5,
    )
    assert 0.0 <= new_prob <= 1.0


def test_consistency_bonus_increases_weight(loop):
    # Consistent (both >= 0.5) should weigh more than inconsistent
    _, w_consistent = loop.update_belief(
        station="A", hour_before_settlement=4,
        prior_belief=0.60, prior_weight=3.0,
        new_probability=0.65, confidence=0.5,
    )
    _, w_inconsistent = loop.update_belief(
        station="B", hour_before_settlement=4,
        prior_belief=0.60, prior_weight=3.0,
        new_probability=0.40, confidence=0.5,
    )
    assert w_consistent > w_inconsistent


def test_weight_capped_at_100(loop):
    _, w = loop.update_belief(
        station="KATL", hour_before_settlement=1,
        prior_belief=0.60, prior_weight=99.9,
        new_probability=0.65, confidence=1.0,
    )
    assert w <= 100.0


# ── Belief retrieval ──────────────────────────────────────────

def test_get_belief_none_when_empty(loop):
    assert loop.get_belief("KATL", 4) is None


def test_get_combined_belief_initializes_on_first_call(loop):
    prob = loop.get_combined_belief("KATL", 4, current_probability=0.62, confidence=0.7)
    assert 0.0 <= prob <= 1.0
    # Now a prior exists
    assert loop.get_belief("KATL", 4) is not None


# ── Settlement + performance ──────────────────────────────────

def test_performance_empty_returns_no_data(loop):
    perf = loop.get_performance("KATL", 4)
    assert perf["n"] == 0
    assert "error" in perf


def test_performance_accuracy_and_brier(loop):
    loop.record_settlement("KATL", "2026-09-01", 4, predicted_probability=0.9, outcome=1.0)
    loop.record_settlement("KATL", "2026-09-02", 4, predicted_probability=0.1, outcome=0.0)
    perf = loop.get_performance("KATL", 4)
    assert perf["n"] == 2
    # Both correct directionally → accuracy 1.0
    assert perf["accuracy"] == 1.0
    # Perfect calibration → brier near 0.01
    assert perf["brier"] < 0.02


def test_get_all_performance_requires_min_samples(loop):
    # One entry is below the n>=10 threshold → excluded from the hb dict.
    # The station key is still present but its hb dict is empty.
    loop.record_settlement("KATL", "2026-09-01", 4, predicted_probability=0.6, outcome=1.0)
    all_perf = loop.get_all_performance()
    # Either the station is absent, or present with an empty (filtered) hb dict.
    assert all_perf.get("KATL", {}) == {}


# ── State management ─────────────────────────────────────────

def test_prune_stale_removes_old_cells(loop):
    loop.update_belief(
        station="KATL", hour_before_settlement=4,
        prior_belief=0.6, prior_weight=3.0,
        new_probability=0.65, confidence=0.5,
    )
    # Force the cell to look stale by manipulating updated timestamp
    loop.belief_state["KATL"]["4"]["updated"] = 0  # epoch 0 = very stale
    pruned = loop.prune_stale(max_stale_hours=24)
    assert pruned >= 1
    assert loop.get_belief("KATL", 4) is None


def test_to_json_dict_roundtrips(loop):
    loop.update_belief(
        station="KATL", hour_before_settlement=4,
        prior_belief=0.6, prior_weight=3.0,
        new_probability=0.65, confidence=0.5,
    )
    d = loop.to_json_dict()
    assert "belief_state" in d
    assert "KATL" in d["belief_state"]
