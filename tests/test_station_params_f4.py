#!/usr/bin/env python3
"""F4 (2026-10-13) — per-station entry/position parameters.

All stations shared one entry window and one cap. `core/station_params` owns a
NEW `station_params` table (live book untouched) seeding all 20 SBOX stations
with regime-differentiated windows (marine T-20h, continental T-18h, convective
T-14h), a $25 cap, and a 0.0 confidence floor. The bridge reads it per station.

Run: python3 -m pytest tests/test_station_params_f4.py -v
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.station_params as sp  # noqa: E402


def test_all_twenty_stations_seeded():
    conn = sqlite3.connect(":memory:")
    sp.ensure_station_params(conn)
    n = conn.execute("SELECT COUNT(*) FROM station_params").fetchone()[0]
    conn.close()
    assert n == 20


def test_defaults_preserve_historical_window():
    p = sp.default_params_for("KNYC")
    assert p["entry_window_start"] == "T-18h"
    assert p["entry_window_end"] == "T-2h"
    assert p["position_cap_usd"] == 25.0
    assert p["confidence_floor"] == 0.0


def test_east_west_regime_heterogeneity_exists():
    starts = {sp.default_params_for(st)["entry_window_start"] for st in sp.STATION_REGIME}
    assert starts == {"T-14h", "T-18h", "T-20h"}, "window distribution must vary by regime"


def test_marine_window_longer_than_convective():
    marine = sp.default_params_for("KLAX")        # marine
    convective = sp.default_params_for("KDFW")    # convective
    assert sp.parse_window_token(marine["entry_window_start"]) > \
        sp.parse_window_token(convective["entry_window_start"])


def test_ensure_is_idempotent_and_preserves_rows():
    conn = sqlite3.connect(":memory:")
    sp.ensure_station_params(conn)
    conn.execute("UPDATE station_params SET position_cap_usd=7.0 WHERE station='KNYC'")
    conn.commit()
    sp.ensure_station_params(conn)  # must not clobber the operator's edit
    cap = conn.execute("SELECT position_cap_usd FROM station_params WHERE station='KNYC'").fetchone()[0]
    conn.close()
    assert cap == 7.0


def test_get_params_parses_window_bounds(tmp_path):
    db = tmp_path / "station_params.db"
    p = sp.get_station_params("KLAX", db_path=db)
    assert p["entry_window_start_hb"] == 20.0
    assert p["entry_window_end_hb"] == 2.0
    assert p["regime"] == "marine"


def test_parse_window_token_rejects_malformed():
    import pytest
    with pytest.raises(ValueError):
        sp.parse_window_token("18h")
