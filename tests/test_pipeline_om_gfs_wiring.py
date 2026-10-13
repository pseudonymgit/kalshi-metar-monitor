#!/usr/bin/env python3
"""B2 — second om_gfs wiring point: core/intraday/pipeline._enrich_from_nwp.

Workstream P wired the om_gfs substitution into the SBOX bridge anchor. The
HourlyPipeline NWP enrichment read `model='gfs'` from the raw local GFS block
directly, so with the flag on the intraday feature vectors still carried the raw
local GFS temperature. These tests pin the second wiring point:

  * flag OFF -> _enrich_from_nwp is byte-identical to the raw local GFS path and
               NEVER calls om_gfs (no-op guarantee);
  * flag ON  -> temperature_f comes from the om_gfs bias-corrected state while
               dew/wind/cloud stay from the raw local GFS block;
  * flag ON but om_gfs returns None / raises -> raw local GFS fallback, no raise.

Run: python3 -m pytest tests/test_pipeline_om_gfs_wiring.py -v
"""
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)

import core.intraday.pipeline as P  # noqa: E402
from core.intraday.distribution_adapter import DistributionState  # noqa: E402


class _FakeCur:
    def __init__(self, rows_fetchdate, rows_payload):
        self._fd = rows_fetchdate
        self._payload = rows_payload
        self._which = None

    def execute(self, sql, params=()):
        # Two query shapes: MAX(fetch_date) then the model/variable rows.
        if "MAX(fetch_date)" in sql:
            self._which = "fd"
        else:
            self._which = "payload"

    def fetchone(self):
        return (self._fd,)

    def fetchall(self):
        return list(self._payload)


class _FakeConn:
    def __init__(self, rows_fd, rows_payload):
        self._cur = _FakeCur(rows_fd, rows_payload)

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _raw_gfs_rows():
    # °C, matching the real LONG schema: model gfs carries all features.
    return [
        ("gfs", "temperature_2m_max", 20.0),          # -> 68.0 F
        ("gfs", "dew_point_2m_daily_mean", 10.0),     # -> 50.0 F
        ("gfs", "wind_speed_10m_daily_mean", 18.52),  # -> 10.0 kt
        ("gfs", "wind_direction_10m_daily_mean", 180.0),
        ("gfs", "cloud_cover_daily_mean", 50.0),      # -> 0.5
    ]


def _patch_nwp(monkeypatch, rows=None, fetch_date="2026-10-13"):
    rows = _raw_gfs_rows() if rows is None else rows
    monkeypatch.setattr(
        P, "_get_connection", lambda db: _FakeConn(fetch_date, rows)
    )


def _vectors():
    return [
        P.HourlyVector(station="KNYC", date_str="2026-10-13", hour_utc=6,
                       hour_before_settlement=12),
        P.HourlyVector(station="KNYC", date_str="2026-10-13", hour_utc=12,
                       hour_before_settlement=6),
    ]


def test_flag_off_is_noop_and_never_calls_om_gfs(monkeypatch):
    monkeypatch.delenv("USE_OM_GFS_NWP", raising=False)
    _patch_nwp(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("om_gfs_state_for_day called with flag OFF")

    monkeypatch.setattr(P, "om_gfs_state_for_day", _boom)

    p = P.HourlyPipeline(nwp_db="dummy.db")
    out = p._enrich_from_nwp(_vectors(), "KNYC", "2026-10-13")

    # Raw local GFS Tmax 20C -> 68F, unchanged.
    assert out[0].temperature_f == pytest.approx(68.0)
    assert out[0].dewpoint_f == pytest.approx(50.0)
    assert out[0].wind_speed_kt == pytest.approx(10.0)
    assert out[0].cloud_cover_pct == pytest.approx(0.5)


def test_flag_on_uses_om_gfs_temperature_only(monkeypatch):
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    _patch_nwp(monkeypatch)

    om = DistributionState("KNYC", "2026-10-13", 55.0, 3.0)
    monkeypatch.setattr(P, "om_gfs_state_for_day",
                        lambda st, d, live=True: om)

    p = P.HourlyPipeline(nwp_db="dummy.db")
    out = p._enrich_from_nwp(_vectors(), "KNYC", "2026-10-13")

    # Level axis substituted from om_gfs; non-temperature features untouched.
    assert out[0].temperature_f == pytest.approx(55.0)
    assert out[0].dewpoint_f == pytest.approx(50.0)
    assert out[1].temperature_f == pytest.approx(55.0)


def test_flag_on_missing_om_gfs_falls_back(monkeypatch):
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    _patch_nwp(monkeypatch)
    monkeypatch.setattr(P, "om_gfs_state_for_day",
                        lambda st, d, live=True: None)

    p = P.HourlyPipeline(nwp_db="dummy.db")
    out = p._enrich_from_nwp(_vectors(), "KNYC", "2026-10-13")
    assert out[0].temperature_f == pytest.approx(68.0)  # raw local GFS


def test_flag_on_fetch_error_falls_back_and_never_raises(monkeypatch):
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    _patch_nwp(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(P, "om_gfs_state_for_day", _raise)

    p = P.HourlyPipeline(nwp_db="dummy.db")
    out = p._enrich_from_nwp(_vectors(), "KNYC", "2026-10-13")
    assert out[0].temperature_f == pytest.approx(68.0)
