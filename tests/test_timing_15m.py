#!/usr/bin/env python3
"""
Tests for the 15-minute entry/exit timing overlay (core/intraday/timing_15m.py).

Drives real station-days from `data/iem_asos_1min.db` through the module and
asserts structural invariants:
  * resampling yields a strict 15-min grid (96 buckets/day, minute == 0/15/30/45)
  * high_so_far is monotone non-decreasing
  * triggers fire ONLY inside trading hours (14:00 <= h < 21:00 UTC)
  * the emitted signal schema is sane
  * the pipeline is deterministic (same input -> same output)
  * the trigger actually fires on real data near the day's high

Read-only on all DBs. No AI/ML. Marked `integration` (reads the live archive).
"""
import sqlite3
from pathlib import Path

import pytest

from core.intraday.timing_15m import (
    BUCKETS_PER_DAY,
    TRADING_HOUR_END_UTC,
    TRADING_HOUR_START_UTC,
    Bucket15m,
    Timing15mParams,
    Timing15mSignal,
    buckets_for_station_day,
    detect_triggers,
    evaluate_bucket,
    latest_signal,
    resample_15m,
)

REPO = Path(__file__).resolve().parents[1]
ASOS_DB = REPO / "data" / "iem_asos_1min.db"

pytestmark = pytest.mark.integration


# ── Real-data fixtures ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def asos_conn():
    if not ASOS_DB.exists():
        pytest.skip(f"ASOS archive not present: {ASOS_DB}")
    c = sqlite3.connect(f"file:{ASOS_DB}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def real_station_day(asos_conn):
    """Pick a real full station-day (KNYC) directly from the archive."""
    row = asos_conn.execute(
        "SELECT station, substr(valid,1,10) AS d, COUNT(*) AS n "
        "FROM observations "
        "WHERE station='KNYC' AND substr(valid,1,10)='2026-06-15' "
        "AND tmpf IS NOT NULL GROUP BY station, d"
    ).fetchone()
    if row is None or row[2] < 1000:
        pytest.skip("KNYC 2026-06-15 not fully populated in ASOS archive")
    return row[0], row[1], row[2]


# ── Synthetic helpers ─────────────────────────────────────────────────────────

def _synth_rows(date="2026-01-02", start_f=50.0, peak_f=70.0, peak_hour=18):
    """1-min rows: linear ramp to `peak_f` at peak_hour, then flat/decline."""
    rows = []
    for minute in range(0, 24 * 60):
        h, m = divmod(minute, 60)
        if h <= peak_hour:
            frac = h / peak_hour
            t = start_f + (peak_f - start_f) * frac
        else:
            # cooling tail so fade triggers are reachable
            t = peak_f - 0.5 * (h - peak_hour)
        rows.append((f"{date} {h:02d}:{m:02d}", t))
    return rows


def _mk_bucket(index, hour, tmpf_mean, rate_1h=0.0):
    return Bucket15m(
        index=index, start_utc=f"2026-01-02 {hour:02d}:00",
        hour_utc=hour, tmpf_mean=tmpf_mean, high_so_far=tmpf_mean,
        rate_15m=rate_1h, rate_1h=rate_1h, vol_15m=0.5,
    )


# ── Resampling invariants ─────────────────────────────────────────────────────

def test_resample_strict_15m_grid_synthetic():
    bk = resample_15m(_synth_rows())
    assert len(bk) == BUCKETS_PER_DAY
    assert [b.index for b in bk] == list(range(BUCKETS_PER_DAY))
    for b in bk:
        minute = int(b.start_utc[14:16])
        assert minute in (0, 15, 30, 45), f"non-15m bucket: {b.start_utc}"
        assert b.hour_utc == (b.index * 15) // 60


def test_high_so_far_monotone_synthetic():
    bk = resample_15m(_synth_rows())
    highs = [b.high_so_far for b in bk]
    assert all(highs[i] >= highs[i - 1] for i in range(1, len(highs)))


def test_bucket_mean_is_mean_of_minutes():
    rows = [("2026-01-02 00:00", 10.0), ("2026-01-02 00:01", 20.0),
            ("2026-01-02 00:02", 30.0)]
    bk = resample_15m(rows)
    assert len(bk) == 1
    assert bk[0].tmpf_mean == pytest.approx(20.0)


# ── Real-data invariants ──────────────────────────────────────────────────────

def test_real_station_day_buckets(real_station_day, asos_conn):
    station, date, _n = real_station_day
    bk = buckets_for_station_day(station, date, conn=asos_conn)
    assert len(bk) == BUCKETS_PER_DAY
    assert [b.index for b in bk] == list(range(BUCKETS_PER_DAY))
    highs = [b.high_so_far for b in bk]
    assert all(highs[i] >= highs[i - 1] for i in range(1, len(highs)))


def test_real_temperatures_are_fahrenheit_sane(real_station_day, asos_conn):
    """Guard the units bug: ASOS tmpf is native F; values must be plausible F."""
    station, date, _n = real_station_day
    bk = buckets_for_station_day(station, date, conn=asos_conn)
    means = [b.tmpf_mean for b in bk]
    assert -60.0 <= min(means) <= 130.0, f"implausible temps: {min(means)}"
    assert -60.0 <= max(means) <= 130.0, f"implausible temps: {max(means)}"
    assert max(means) < 140.0, "temperatures look C->F double-converted"


def test_trigger_fires_on_real_day(real_station_day, asos_conn):
    """A strike just above the observed day-high must fire approach_up."""
    station, date, _n = real_station_day
    bk = buckets_for_station_day(station, date, conn=asos_conn)
    day_high = max(b.tmpf_mean for b in bk)
    strike = round(day_high) + 1.0  # just above the realized high
    sigs = detect_triggers(bk, strike, station)
    assert sigs, f"no trigger fired on {station} {date} (high={day_high:.1f}, strike={strike})"
    assert any(s.trigger == "approach_up" for s in sigs)


def test_signals_only_inside_trading_hours(real_station_day, asos_conn):
    station, date, _n = real_station_day
    bk = buckets_for_station_day(station, date, conn=asos_conn)
    day_high = max(b.tmpf_mean for b in bk)
    for strike in (round(day_high) - 1, round(day_high), round(day_high) + 1):
        for s in detect_triggers(bk, strike, station):
            hour = int(s.timestamp[11:13])
            assert TRADING_HOUR_START_UTC <= hour < TRADING_HOUR_END_UTC, (
                f"trigger at {s.timestamp} outside trading hours"
            )


def test_signal_schema_sane(real_station_day, asos_conn):
    station, date, _n = real_station_day
    bk = buckets_for_station_day(station, date, conn=asos_conn)
    day_high = max(b.tmpf_mean for b in bk)
    sigs = detect_triggers(bk, round(day_high) + 1.0, station)
    assert sigs
    for s in sigs:
        assert isinstance(s, Timing15mSignal)
        assert s.station == station
        assert s.trigger in ("approach_up", "fade_down")
        assert s.direction in ("UP", "DOWN")
        assert 0.0 <= s.confidence <= 1.0
        d = s.as_dict()
        assert set(d) == {"station", "timestamp", "trigger", "direction",
                          "confidence", "high_so_far", "strike", "gap",
                          "rate_1h", "vol_15m"}


# ── Gate logic (synthetic, exact) ─────────────────────────────────────────────

def test_outside_trading_hours_never_fires():
    p = Timing15mParams()
    for hour in (0, 6, 13, 21, 23):
        b = _mk_bucket(index=hour * 4, hour=hour, tmpf_mean=70.0, rate_1h=10.0)
        assert evaluate_bucket(b, strike=70.0, station="KXYZ", params=p) is None


def test_approach_up_requires_proximity_and_momentum():
    p = Timing15mParams(proximity_F=1.5, mom_thr_F=0.5)
    # near strike + strong climb -> fires UP
    b = _mk_bucket(index=56, hour=14, tmpf_mean=70.0, rate_1h=1.0)
    up = evaluate_bucket(b, strike=70.5, station="KXYZ", params=p)
    assert up is not None and up.trigger == "approach_up" and up.direction == "UP"
    # near strike but falling -> no approach_up
    b2 = _mk_bucket(index=57, hour=14, tmpf_mean=70.0, rate_1h=-1.0)
    assert evaluate_bucket(b2, strike=70.5, station="KXYZ", params=p) is None


def test_fade_down_fires_when_strike_far_and_high_falling():
    p = Timing15mParams(proximity_F=1.5, mom_thr_F=0.5)
    b = _mk_bucket(index=60, hour=15, tmpf_mean=70.0, rate_1h=-1.5)
    sig = evaluate_bucket(b, strike=75.0, station="KXYZ", params=p)
    assert sig is not None and sig.trigger == "fade_down" and sig.direction == "DOWN"


def test_latest_signal_returns_last_bucket_trigger(real_station_day, asos_conn):
    station, date, _n = real_station_day
    bk = buckets_for_station_day(station, date, conn=asos_conn)
    day_high = max(b.tmpf_mean for b in bk)
    sigs = detect_triggers(bk, round(day_high) + 1.0, station)
    latest = latest_signal(bk, round(day_high) + 1.0, station)
    if sigs:
        assert latest is not None
        assert latest.timestamp == sigs[-1].timestamp


# ── Determinism ───────────────────────────────────────────────────────────────

def test_deterministic(real_station_day, asos_conn):
    station, date, _n = real_station_day
    a = buckets_for_station_day(station, date, conn=asos_conn)
    b = buckets_for_station_day(station, date, conn=asos_conn)
    assert [x.tmpf_mean for x in a] == [x.tmpf_mean for x in b]
    strike = round(max(x.tmpf_mean for x in a)) + 1.0
    da = [s.as_dict() for s in detect_triggers(a, strike, station)]
    db = [s.as_dict() for s in detect_triggers(b, strike, station)]
    assert da == db
