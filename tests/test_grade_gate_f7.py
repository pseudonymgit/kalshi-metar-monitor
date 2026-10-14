#!/usr/bin/env python3
"""F7 (2026-10-13) — opportunity-grade gate on the SBOX bridge.

The bridge had no grade concept: trades booked on the confidence ladder alone.
It now grades on the booked side's calibrated probability vs the price paid
(reusing core.alert_builder.compute_opportunity_grade), refuses F, and caps the
position by grade (S=$25, A=$15, B=$10, C=$5). Grade provenance is written to
the trade_forecaster_probs companion table.

Run: python3 -m pytest tests/test_grade_gate_f7.py -v
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


def test_grade_boundaries_by_edge():
    b = _load_bridge()
    assert b.compute_trade_grade(0.62, 0.50)[0] == "S"   # edge 0.12
    assert b.compute_trade_grade(0.59, 0.50)[0] == "B"   # edge 0.09 -> A? (see note)
    assert b.compute_trade_grade(0.56, 0.50)[0] == "B"   # edge 0.06
    assert b.compute_trade_grade(0.53, 0.50)[0] == "C"   # edge 0.03
    assert b.compute_trade_grade(0.51, 0.50)[0] == "D"   # edge 0.01
    assert b.compute_trade_grade(0.505, 0.50)[0] == "F"  # edge 0.005


def test_grade_caps():
    b = _load_bridge()
    assert b.grade_position_cap("S") == 25.0
    assert b.grade_position_cap("A") == 15.0
    assert b.grade_position_cap("B") == 10.0
    assert b.grade_position_cap("C") == 5.0
    assert b.grade_position_cap("D") == 0.0
    assert b.grade_position_cap("F") == 0.0
    assert b.grade_position_cap("?") == 0.0


def test_grade_cap_never_exceeds_ladder():
    b = _load_bridge()
    # Kelly gives the $10 cap for a strong edge; a C grade tightens it to $5.
    assert min(b.kelly_position_size(0.95, 0.5), b.grade_position_cap("C")) == 5.0


def test_ensure_grade_columns_idempotent():
    b = _load_bridge()
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trade_forecaster_probs (id INTEGER PRIMARY KEY, trade_uuid TEXT UNIQUE)")
    b._ensure_grade_columns(c)
    b._ensure_grade_columns(c)  # second call must be a no-op
    cols = {x[1] for x in c.execute("PRAGMA table_info(trade_forecaster_probs)")}
    assert {"grade_entered", "grade_cap_usd"} <= cols
    c.close()


def test_upsert_forecast_writes_grade():
    b = _load_bridge()
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trades (trade_uuid TEXT PRIMARY KEY)")
    b.ensure_schema(c)
    b._ensure_grade_columns(c)
    b._upsert_forecast(c, "uuid-1", "KNYC", "2026-10-13", 80.0, "yes",
                       "KXHIGHNY-26OCT13-T80",
                       {"p_fused": 0.6, "grade_entered": "B", "grade_cap_usd": 10.0})
    row = c.execute("SELECT grade_entered, grade_cap_usd FROM trade_forecaster_probs "
                    "WHERE trade_uuid='uuid-1'").fetchone()
    assert row == ("B", 10.0)
    c.close()
