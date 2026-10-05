"""Tests for the sub-hourly trajectory nowcast signal.

Ported with core/signals/subhourly_trajectory_signal.py from temp-sprint-branch
(original commit f7a653861 by Dan <dan@openclaw.ai>).

These tests exercise the PURE, DB-free surface: slope math, confidence,
downsampling, time parsing, and the cross/no-cross decision rule via the
``asos_day`` injection path. The DB-backed held-out OOS reproduction lives in
``scripts/test_subhourly_trajectory_signal.py`` (read-only, not collected by
pytest). No production DB is opened here.
"""
import math

import pytest

from core.signals.subhourly_trajectory_signal import (
    SubhourlyTrajectorySignal,
    GAP_LO,
    GAP_HI,
    SLOPE_THRESHOLD,
    RES,
    MIN_OBS_FOR_FIT,
    STATIONS,
    _parse_utc,
    _tz_of,
)


def _ramp(step=0.1, minutes=range(0, 240, 5), base=70.0):
    """Build an observation window as a linear ramp: base + step*minute."""
    return [(m, base + step * m) for m in minutes]


class TestSubhourlyTrajectorySignal:
    """Test suite for SubhourlyTrajectorySignal."""

    def test_signal_can_be_imported(self):
        sig = SubhourlyTrajectorySignal()
        assert sig is not None
        assert sig.name == "subhourly_trajectory"

    def test_min_lookback_is_consistent(self):
        sig = SubhourlyTrajectorySignal()
        mlb = sig.min_lookback
        assert isinstance(mlb, int)
        assert mlb >= 0

    def test_daily_interface_is_inert(self):
        """Sub-hourly signal uses evaluate_nowcast; evaluate() abstains."""
        sig = SubhourlyTrajectorySignal()
        result = sig.evaluate(0, [])
        assert result == (None, 0.0)
        assert isinstance(result, tuple) and len(result) == 2

    def test_constants_are_frozen(self):
        assert GAP_LO == 1.0
        assert GAP_HI == 3.0
        assert SLOPE_THRESHOLD == 0.05
        assert RES == 5
        assert MIN_OBS_FOR_FIT == 4
        assert len(STATIONS) == 20

    # ── slope math ──
    def test_compute_slope_linear_ramp(self):
        sig = SubhourlyTrajectorySignal()
        assert sig.compute_slope(_ramp(step=0.1)) == pytest.approx(0.1, abs=1e-9)
        assert sig.compute_slope(_ramp(step=-0.05)) == pytest.approx(-0.05, abs=1e-9)

    @pytest.mark.parametrize("window", [[], [(0, 60.0)]])
    def test_compute_slope_degenerate_returns_zero(self, window):
        sig = SubhourlyTrajectorySignal()
        assert sig.compute_slope(window) == 0.0

    def test_compute_slope_flat_returns_zero(self):
        sig = SubhourlyTrajectorySignal()
        assert sig.compute_slope(_ramp(step=0.0)) == 0.0

    # ── confidence ──
    @pytest.mark.parametrize("slope,expected", [
        (-1.0, 0.0),
        (0.0, 0.0),
        (0.075, 0.5),
        (0.15, 1.0),
        (1.0, 1.0),
    ])
    def test_confidence_for_slope(self, slope, expected):
        sig = SubhourlyTrajectorySignal()
        assert sig._confidence_for_slope(slope) == pytest.approx(expected, abs=1e-9)

    def test_confidence_never_nan_and_bounded(self):
        sig = SubhourlyTrajectorySignal()
        for s in [-5.0, -1.0, 0.0, 0.03, 0.3, 10.0]:
            c = sig._confidence_for_slope(s)
            assert not math.isnan(c)
            assert 0.0 <= c <= 1.0

    # ── helpers ──
    def test_downsample_reduces_resolution(self):
        sig = SubhourlyTrajectorySignal()
        obs = [(m, float(m)) for m in range(0, 25)]  # 1-min steps
        ds = sig._downsample(obs, 5)
        assert len(ds) < len(obs)
        assert all(b[0] - a[0] >= 5 for a, b in zip(ds, ds[1:]))

    def test_downsample_identity_when_step_le_1(self):
        sig = SubhourlyTrajectorySignal()
        obs = [(m, float(m)) for m in range(0, 10)]
        assert sig._downsample(obs, 1) == obs

    def test_hotd_minute(self):
        sig = SubhourlyTrajectorySignal()
        assert sig._hotd_minute([(0, 60.0), (120, 80.0), (240, 70.0)]) == 120
        assert sig._hotd_minute([]) is None

    def test_parse_utc_variants(self):
        assert _parse_utc("2026-09-19T18:00:00Z") is not None
        assert _parse_utc("2026-09-19T18:00:00+00:00") is not None
        assert _parse_utc("") is None
        assert _parse_utc(None) is None
        assert _parse_utc("not-a-date") is None

    def test_tz_of_known_station(self):
        assert _tz_of("KNYC").key == "America/New_York"
        # Unknown ICAO falls back to Eastern, never raises.
        assert _tz_of("KZZZ").key == "America/New_York"

    # ── decision rule (asos_day injected; no DB) ──
    def test_predicts_cross_when_in_anticipatory_cell(self):
        sig = SubhourlyTrajectorySignal()
        asos = _ramp(step=0.1)  # +0.1 F/min from 70
        # t=180: cur_max = 70 + 0.1*180 = 88; strike 90 -> gap 2.0 in [1,3), slope 0.1
        r = sig.evaluate_nowcast("KNYC", "2026-09-19", 180,
                                 asos_day=asos, strike=90.0, settle=91.0)
        assert r["prediction"] == "cross"
        assert GAP_LO <= r["gap"] < GAP_HI
        assert r["slope"] >= SLOPE_THRESHOLD
        assert r["already_crossed"] is False
        assert r["outcome"] == 1

    def test_abstains_when_already_crossed(self):
        sig = SubhourlyTrajectorySignal()
        asos = _ramp(step=0.1)
        # cur_max 88 > strike 80 -> already happened -> abstain
        r = sig.evaluate_nowcast("KNYC", "2026-09-19", 180,
                                 asos_day=asos, strike=80.0, settle=91.0)
        assert r["prediction"] is None
        assert r["already_crossed"] is True

    def test_no_cross_when_gap_outside_band(self):
        sig = SubhourlyTrajectorySignal()
        asos = _ramp(step=0.1)
        # gap 7.0 > GAP_HI -> no-cross
        r = sig.evaluate_nowcast("KNYC", "2026-09-19", 180,
                                 asos_day=asos, strike=95.0, settle=91.0)
        assert r["prediction"] == "no-cross"

    def test_no_cross_when_slope_below_threshold(self):
        sig = SubhourlyTrajectorySignal()
        asos = _ramp(step=0.01)  # +0.01 F/min, below 0.05 threshold
        r = sig.evaluate_nowcast("KNYC", "2026-09-19", 180,
                                 asos_day=asos, strike=73.0, settle=74.0)
        # cur_max = 70 + 0.01*180 = 71.8; gap = 1.2 in [1,3) but slope too low
        assert GAP_LO <= r["gap"] < GAP_HI
        assert r["slope"] < SLOPE_THRESHOLD
        assert r["prediction"] == "no-cross"

    def test_abstains_on_insufficient_observations(self):
        sig = SubhourlyTrajectorySignal()
        r = sig.evaluate_nowcast("KNYC", "2026-09-19", 5,
                                 asos_day=[(0, 60.0)], strike=90.0, settle=91.0)
        assert r["prediction"] is None
        assert r["confidence"] == 0.0

    def test_cross_prediction_axis_is_strict(self):
        """Outcome label must be settlement_temp_f > strike_price (strict)."""
        sig = SubhourlyTrajectorySignal()
        asos = _ramp(step=0.1)
        # settle exactly equals strike -> loss (strict >)
        r_eq = sig.evaluate_nowcast("KNYC", "2026-09-19", 180,
                                    asos_day=asos, strike=91.0, settle=91.0)
        assert r_eq["outcome"] == 0
        # settle strictly greater than strike -> win
        r_win = sig.evaluate_nowcast("KNYC", "2026-09-19", 180,
                                     asos_day=asos, strike=89.0, settle=89.5)
        assert r_win["outcome"] == 1
