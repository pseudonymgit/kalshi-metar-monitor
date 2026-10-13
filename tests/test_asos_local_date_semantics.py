#!/usr/bin/env python3
"""Regression test: ASOS local-date bucketing must equal true station-local time.

Locks the 2026-10-13 finding that the `tot // 1440` pattern in
`scripts/predictor_diversity_study_2026-10-13.py::load_asos` (and the sibling in
`scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py`) is CORRECT.

Claim under test: for a UTC observation timestamp, building the station-local
minute-of-day as `tot = hh*60 + mm + off//60` and taking
`local_date = utc_date + timedelta(days=tot // 1440)`,
`local_mod = tot % 1440` reproduces exactly what `utc_dt.astimezone(tz)` yields.
`tot` is negative pre-dawn for US stations; Python's floor `//` and Euclidean
`%` make this an exact split, unlike C-style truncation.

Also asserts the counterexample: the om-gfs loader's `ldate = utc_date`
("no cross-day carry") is NOT equivalent, and diverges for local-evening rows.

Hermetic: pure datetime math + stdlib; touches no database.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core.station_time import station_timezone_name

BUCKET_MIN = 15

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]

# days around DST transitions and a year boundary
DAYS = ["2026-03-07", "2026-03-08", "2026-03-09",
        "2026-10-31", "2026-11-01", "2026-11-02",
        "2026-12-31", "2026-01-01", "2026-06-15"]


def _loader_local(utc_dt, tz):
    """Replicate the loader's date/mod derivation for one UTC timestamp."""
    ds = utc_dt.strftime("%Y-%m-%d")
    hh, mm = utc_dt.hour, utc_dt.minute
    off = int(utc_dt.astimezone(tz).utcoffset().total_seconds())
    tot = hh * 60 + mm + off // 60
    ldate = (date.fromisoformat(ds) + timedelta(days=tot // 1440)).isoformat()
    lmod = (tot % 1440) // BUCKET_MIN * BUCKET_MIN
    return ldate, lmod


@pytest.mark.parametrize("st", STATIONS)
def test_loader_local_date_equals_astimezone(st):
    """Every UTC minute of the sample days maps to the true station-local date/mod."""
    tz = ZoneInfo(station_timezone_name(st))
    for ds in DAYS:
        base = datetime.fromisoformat(f"{ds}T00:00").replace(tzinfo=timezone.utc)
        for minute in range(0, 24 * 60, 7):  # every 7 min
            utc_dt = base + timedelta(minutes=minute)
            ldate, lmod = _loader_local(utc_dt, tz)
            tloc = utc_dt.astimezone(tz)
            assert ldate == tloc.date().isoformat(), (st, utc_dt)
            assert lmod == (tloc.hour * 60 + tloc.minute) // BUCKET_MIN * BUCKET_MIN


def test_negative_minute_of_day_is_previous_local_day():
    """A pre-dawn UTC hour lands on the PREVIOUS local date, as astimezone does."""
    tz = ZoneInfo("America/New_York")  # EST = UTC-5 in winter
    utc_dt = datetime(2026, 1, 15, 3, 30, tzinfo=timezone.utc)  # 22:30 local Jan 14
    ldate, lmod = _loader_local(utc_dt, tz)
    assert ldate == "2026-01-14"
    assert lmod == (22 * 60 + 30) // BUCKET_MIN * BUCKET_MIN
    assert utc_dt.astimezone(tz).date().isoformat() == ldate


def test_no_carry_variant_is_not_equivalent():
    """The `ldate = utc_date` shortcut diverges for local-evening rows.

    This documents why the previous om-gfs 'fix' was itself wrong: folding a
    UTC day's local-evening rows (the local daily max) into that UTC date
    mis-attributes the day. The post-pin rows are the affected ones.
    """
    tz = ZoneInfo("America/Los_Angeles")  # UTC-8/-7
    # 2026-08-01 23:00 UTC = 16:00 PDT on 2026-08-01 (local-minute 960 > 810)
    utc_dt = datetime(2026, 8, 1, 23, 0, tzinfo=timezone.utc)
    correct_date, correct_mod = _loader_local(utc_dt, tz)
    no_carry_date = utc_dt.strftime("%Y-%m-%d")
    assert correct_date == "2026-08-01"
    assert correct_mod == 960
    # same here; use a row whose LOCAL date is the NEXT UTC day to force a diff
    utc_dt2 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)  # 23:00 local Jul 31
    correct_date2, _ = _loader_local(utc_dt2, tz)
    assert correct_date2 == "2026-07-31"
    assert utc_dt2.strftime("%Y-%m-%d") == "2026-08-01"  # no-carry would be wrong
