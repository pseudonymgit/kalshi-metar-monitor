#!/usr/bin/env python3
"""F2 (2026-10-13) — p_fused calibration + weighted-fusion unit tests.

Run: python3 -m pytest tests/test_fusion_calibration.py -q
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    fuse, fuse_strike, calibrated_ceiling, component_conf, composite_confidence,
    grade_from_confidence, DEFAULT_WEIGHTS, CALIBRATION_FLOOR,
)


# ── ceiling derivation ───────────────────────────────────────────────────────
def test_ceiling_zero_resolution_is_floor():
    assert calibrated_ceiling(0.0) == CALIBRATION_FLOOR


def test_ceiling_default_is_floor_from_holdout_resolution():
    # holdout resolution 0.0004 << 0.05 -> ceiling is the floor + epsilon.
    ce = calibrated_ceiling()
    assert 0.75 <= ce < 0.77


def test_ceiling_full_skill_reaches_max():
    assert calibrated_ceiling(0.05) == 0.99
    assert calibrated_ceiling(1.0) == 0.99


# ── saturation fix ───────────────────────────────────────────────────────────
def test_no_saturation_on_certain_inputs():
    # GEFS/analog both 1.0, trajectory confirms -> legacy would be 0.99.
    pf, meta = fuse(1.0, 1.0, 1.0)
    assert pf <= meta["ceiling"] + 1e-9
    assert pf < 0.99
    # ...and the raw legacy value would have clipped.
    assert meta["p_core"] >= 0.90


def test_confirm_amplifier_neutralized_in_clip_zone():
    _pf, meta = fuse(0.99, 0.99, 1.0)   # traj confirm, p_core in clip zone
    assert meta["traj_verdict"] == "confirm"
    assert meta["amplifier_neutralized"] is True
    assert meta["traj_mult"] == 1.0


def test_confirm_amplifier_still_applies_below_clip_zone():
    # p_core below 0.90 -> the x1.05 amplifier is preserved (unchanged behavior).
    _pf, meta = fuse(0.70, 0.70, 1.0)
    assert meta["amplifier_neutralized"] is False
    assert meta["traj_mult"] == 1.05


def test_calibrated_p_bounded_by_ceiling_both_sides():
    pf_hi, _ = fuse(1.0, 1.0, 1.0)
    pf_lo, _ = fuse(0.0, 0.0, 0.0)
    assert pf_hi <= 0.99
    assert pf_lo >= 0.01
    # symmetric clamp about 0.5 at the ceiling
    assert abs((1.0 - pf_lo) - pf_hi) < 0.05


def test_ceiling_override_hook():
    pf, meta = fuse(1.0, 1.0, 1.0, ceiling=0.60)
    assert pf <= 0.60 + 1e-9
    assert meta["ceiling"] == 0.60


# ── weighted fusion ──────────────────────────────────────────────────────────
def test_weighted_uses_market_and_defaults_equal():
    _pf, meta = fuse(0.8, 0.8, 0.5, use_weights=True, p_market=0.2)
    assert meta["use_weights"] is True
    assert meta["p_market"] == 0.2
    # equal weights over 4 present components
    assert meta["weights_applied"] == DEFAULT_WEIGHTS
    # core is the equal-weight mean of {0.8, 0.8, 0.5, 0.2}
    assert abs(meta["p_core"] - 0.575) < 1e-3


def test_weighted_without_market_renormalizes():
    _pf, meta = fuse(0.8, 0.8, 0.4, use_weights=True, p_market=None)
    assert meta["p_market"] is None
    # core is the equal-weight mean of {0.8, 0.8, 0.4}
    # p_core is rounded to 4 dp by design
    assert abs(meta["p_core"] - (0.8 + 0.8 + 0.4) / 3) < 1e-3


def test_weighted_custom_weights():
    _pf, meta = fuse(0.9, 0.1, 0.5, use_weights=True,
                     weights={"gefs": 0.7, "analog": 0.1, "traj": 0.2, "market": 0.0})
    # core = 0.7*0.9 + 0.1*0.1 + 0.2*0.5 = 0.74
    assert abs(meta["p_core"] - 0.74) < 1e-3


# ── component confidence / grade ladder ──────────────────────────────────────
def test_component_conf():
    assert component_conf(0.5) == 0.0
    assert component_conf(1.0) == 1.0
    assert abs(component_conf(0.75) - 0.5) < 1e-9
    assert component_conf(None) == 0.0


def test_composite_confidence_equal_weights():
    # all present, all certainty -> 1.0
    assert composite_confidence(1.0, 1.0, 1.0, 1.0, DEFAULT_WEIGHTS) == 1.0
    # all coin flips -> 0.0
    assert composite_confidence(0.5, 0.5, 0.5, 0.5, DEFAULT_WEIGHTS) == 0.0


def test_grade_ladder():
    assert grade_from_confidence(0.95) == "S"
    assert grade_from_confidence(0.90) == "S"
    assert grade_from_confidence(0.80) == "A"
    assert grade_from_confidence(0.70) == "B"
    assert grade_from_confidence(0.60) == "C"
    assert grade_from_confidence(0.59) == "F"
    assert grade_from_confidence(None) == "F"


# ── fuse_strike passes options through ───────────────────────────────────────
def test_fuse_strike_options_forwarded():
    out = fuse_strike(lambda x: 0.9, lambda x: 0.9, lambda x: 1.0, 75.0,
                      use_weights=True, p_market=0.3, ceiling=0.7)
    assert out["ceiling"] == 0.7
    assert out["p_market"] == 0.3
    assert out["p_fused"] <= 0.7 + 1e-9
    assert "calibrated_p" in out and "grade" in out


def test_nan_trajectory_is_unavailable():
    out = fuse_strike(lambda x: 0.8, lambda x: 0.8, lambda x: float("nan"), 75.0)
    assert out["traj_verdict"] == "unavailable"
    assert out["p_trajectory"] is None
