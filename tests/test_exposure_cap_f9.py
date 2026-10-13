#!/usr/bin/env python3
"""F9 (2026-10-13) — portfolio exposure cap on the SBOX bridge.

The bridge admitted each candidate independently with no per-station cap,
max-concurrent, or daily-exposure check. `exposure_skip_reason` now blocks a new
entry when the live open book already has >=3 rows for the station or >=10 rows
for the station-day.

Run: python3 -m pytest tests/test_exposure_cap_f9.py -v
"""
import importlib.util
import os
import sqlite3
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _book(n_open_per_station, day, station_for_rows, status="open"):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trades (trade_uuid TEXT, station TEXT, trade_date_utc TEXT, status TEXT)")
    for i in range(n_open_per_station):
        c.execute("INSERT INTO trades VALUES (?,?,?,?)",
                  (f"u{i}", station_for_rows, day, status))
    return c


def test_station_cap_blocks_third_open():
    b = _load_bridge()
    c = _book(3, "2026-10-13", "KNYC")
    assert b.exposure_skip_reason(c, "KNYC", "2026-10-13") == "exposure_station_cap_3"
    c.close()


def test_station_cap_allows_below_limit():
    b = _load_bridge()
    c = _book(2, "2026-10-13", "KNYC")
    assert b.exposure_skip_reason(c, "KNYC", "2026-10-13") is None
    c.close()


def test_station_cap_is_station_scoped():
    b = _load_bridge()
    c = _book(3, "2026-10-13", "KNYC")
    # A different station with no open rows is unaffected by KNYC's cap.
    assert b.exposure_skip_reason(c, "KATL", "2026-10-13") is None
    c.close()


def test_day_cap_blocks_eleventh_open():
    b = _load_bridge()
    c = _book(10, "2026-10-13", "KAAA")
    assert b.exposure_skip_reason(c, "KBBB", "2026-10-13") == "exposure_day_cap_10"
    c.close()


def test_closed_rows_do_not_count():
    b = _load_bridge()
    c = _book(5, "2026-10-13", "KNYC", status="closed")
    assert b.exposure_skip_reason(c, "KNYC", "2026-10-13") is None
    c.close()


def test_limits_are_the_documented_values():
    b = _load_bridge()
    assert b.MAX_OPEN_PER_STATION == 3
    assert b.MAX_OPEN_PER_DAY == 10
