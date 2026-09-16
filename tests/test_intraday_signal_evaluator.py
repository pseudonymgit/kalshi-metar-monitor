"""
P0 intraday core tests — HourlySignalEvaluator detectors (Σ.1a).

Deterministic detector functions: pressure tendency, temperature trend,
and wind shift. These take an explicit `metar_data` dict (vectors + raw_obs_hour),
so no DB or network is required. Also tests the empirical-confidence lookup
and pool-name mapping logic.
"""

import pytest

from core.intraday.signal_evaluator import (
    HourlySignalEvaluator,
    _detect_pressure_tendency,
    _detect_temp_trend,
    _detect_wind_shift,
)
from core.intraday.pipeline import HourlyVector


def _vector(hour_utc, **kwargs):
    base = dict(
        station="KATL", date_str="2026-09-07", hour_utc=hour_utc,
        hour_before_settlement=18 - hour_utc,
    )
    base.update(kwargs)
    return HourlyVector(**base)


# ── Pressure tendency detector ───────────────────────────────

def test_pressure_tendency_rising_is_down():
    v = _vector(14, pressure_tendency_3h=3.0)
    direction, confidence, feature = _detect_pressure_tendency(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction == "down"
    assert confidence > 0.0
    assert feature == 3.0


def test_pressure_tendency_falling_is_up():
    v = _vector(14, pressure_tendency_3h=-3.0)
    direction, confidence, feature = _detect_pressure_tendency(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction == "up"
    assert confidence > 0.0


def test_pressure_tendency_below_threshold_is_none():
    v = _vector(14, pressure_tendency_3h=1.0)  # < 1.5 threshold
    direction, confidence, _ = _detect_pressure_tendency(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction is None
    assert confidence == 0.0


def test_pressure_tendency_no_data_is_none():
    v = _vector(14)  # pressure_tendency_3h defaults None
    direction, confidence, _ = _detect_pressure_tendency(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction is None


# ── Temperature trend detector ────────────────────────────────

def test_temp_trend_rising_is_up():
    v = _vector(14, temp_trend_3h=4.0)
    direction, confidence, _ = _detect_temp_trend(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction == "up"
    assert confidence > 0.0


def test_temp_trend_falling_is_down():
    v = _vector(14, temp_trend_3h=-4.0)
    direction, confidence, _ = _detect_temp_trend(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction == "down"


def test_temp_trend_below_threshold_is_none():
    v = _vector(14, temp_trend_3h=1.0)  # < 2.0 threshold
    direction, confidence, _ = _detect_temp_trend(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": {}}
    )
    assert direction is None


# ── Wind shift detector ───────────────────────────────────────

def test_wind_shift_requires_min_three_observations():
    v = _vector(14, wind_direction_deg=180.0)
    raw_obs = {14: [
        {"wind_direction_deg": 180.0},
        {"wind_direction_deg": 185.0},
    ]}
    direction, confidence, _ = _detect_wind_shift(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": raw_obs}
    )
    # Fewer than 3 wind obs → None
    assert direction is None


def test_wind_shift_dispersion_detects_direction():
    v = _vector(14, wind_direction_deg=90.0)
    raw_obs = {14: [
        {"wind_direction_deg": 50.0},
        {"wind_direction_deg": 90.0},
        {"wind_direction_deg": 130.0},
    ]}
    # dispersion = 80 (>=45), avg_dir=90 (<180) → "down"
    direction, confidence, _ = _detect_wind_shift(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": raw_obs}
    )
    assert direction == "down"
    assert confidence > 0.0


def test_wind_shift_low_dispersion_is_none():
    v = _vector(14, wind_direction_deg=90.0)
    raw_obs = {14: [
        {"wind_direction_deg": 90.0},
        {"wind_direction_deg": 92.0},
        {"wind_direction_deg": 91.0},
    ]}
    direction, confidence, _ = _detect_wind_shift(
        "KATL", "2026-09-07", 14, {"vectors": [v], "raw_obs_hour": raw_obs}
    )
    assert direction is None


# ── Empirical confidence lookup ───────────────────────────────

def test_empirical_confidence_no_map_returns_none():
    ev = HourlySignalEvaluator(metar_db="", confidence_map_path="")
    # Empty confidence map → _empirical_confidence returns None
    assert ev._empirical_confidence("some_signal", "up", 0.5) is None


def test_empirical_confidence_none_feature_returns_none():
    ev = HourlySignalEvaluator(metar_db="", confidence_map_path="")
    ev._confidence_map = {"sig": {"monotone": True, "buckets": []}}
    assert ev._empirical_confidence("sig", "up", None) is None


def test_empirical_confidence_non_monotone_returns_none():
    ev = HourlySignalEvaluator(metar_db="", confidence_map_path="")
    ev._confidence_map = {"sig": {"monotone": False, "buckets": []}}
    assert ev._empirical_confidence("sig", "up", 0.5) is None


def test_empirical_confidence_lookup_bucket():
    ev = HourlySignalEvaluator(metar_db="", confidence_map_path="")
    ev._confidence_map = {
        "sig": {
            "monotone": True,
            "buckets": [
                {"min": 0.0, "max": 1.0, "p_correct_up": 0.72, "p_correct_down": 0.31},
            ],
        }
    }
    assert ev._empirical_confidence("sig", "up", 0.5) == 0.72
    assert ev._empirical_confidence("sig", "down", 0.5) == 0.31
