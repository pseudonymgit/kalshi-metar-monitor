"""
P0 intraday core tests — HourlyPipeline helpers + HourlyVector (Σ.1d).

Deterministic pure functions: hour→hours-before-settlement mapping, cloud
cover parsing, and vector validity. No DB, no network.
"""

import pytest

from core.intraday.pipeline import (
    SETTLEMENT_HOUR_UTC,
    HourlyVector,
    _cloud_cover_fraction,
    _hour_to_hour_before_settlement,
)


# ── HourlyVector ──────────────────────────────────────────────

def test_vector_is_valid_when_any_field_populated():
    v = HourlyVector(
        station="KATL", date_str="2026-09-07", hour_utc=14,
        hour_before_settlement=4, temperature_f=72.0,
    )
    assert v.is_valid is True


def test_vector_invalid_when_all_fields_none():
    v = HourlyVector(
        station="KATL", date_str="2026-09-07", hour_utc=14,
        hour_before_settlement=4,
    )
    assert v.is_valid is False


# ── Hour → hours-before-settlement ───────────────────────────

def test_hour_before_settlement_basic():
    assert _hour_to_hour_before_settlement(14) == 4  # 18 - 14
    assert _hour_to_hour_before_settlement(18) == 0  # at settlement


def test_hour_before_settlement_floor_at_zero():
    # Hours after settlement should clamp to 0, not go negative
    assert _hour_to_hour_before_settlement(20) == 0
    assert _hour_to_hour_before_settlement(23) == 0


def test_settlement_hour_constant():
    assert SETTLEMENT_HOUR_UTC == 18


# ── Cloud cover parsing ───────────────────────────────────────

def test_cloud_cover_none_when_empty():
    assert _cloud_cover_fraction(None) is None
    assert _cloud_cover_fraction("") is None


def test_cloud_cover_clear_skies():
    assert _cloud_cover_fraction("METAR KATL 141453Z 00000KT 10SM CLR") == 0.0
    assert _cloud_cover_fraction("METAR KATL 141453Z 00000KT 10SM SKC") == 0.0


def test_cloud_cover_overcast():
    assert _cloud_cover_fraction("METAR KATL 141453Z 00000KT 10SM OVC050") == 1.0


def test_cloud_cover_dominant_layer():
    # FEW (0.25) + BKN (0.75) → take the most-cover layer (BKN = 0.75)
    assert _cloud_cover_fraction("METAR KATL 141453Z 00000KT 10SM FEW020 BKN050") == 0.75


def test_cloud_cover_no_cloud_group_returns_none():
    assert _cloud_cover_fraction("METAR KATL 141453Z 00000KT 10SM 27/19 A2992") is None
