#!/usr/bin/env python3
"""ITEM 1 (2026-10-16) — the booked prob is DERIVED from the companion, both sides.

Cold-eyes BLOCK re-verify. After the b431fcfea re-sync, ids 287/288 still
disagreed with their companion because the re-sync wrote the CALLER-supplied
``our_prob`` rather than the companion's own calibrated value:

    id=287 KMSP buy_no  booked 0.6914 vs 1-cal 0.6976  (Δ0.0062)
    id=288 KMSY buy_yes booked 0.9481 vs cal   0.9534  (Δ0.0053)

Root cause: the companion ``trade_forecaster_probs.calibrated_p`` is the single
source of the calibrated probability, but the booked column was written from a
separate caller argument. A 15:15 companion re-stamp therefore moved
``calibrated_p`` while the booked column kept its 14:15 value.

Fix: ONE canonical writer (`_sync_booked_prob`) that DERIVES the booked prob from
the companion's YES-side ``calibrated_p`` transformed by the row's OWN booked leg
(YES: ``calibrated_p``; NO: ``1 - calibrated_p``), and writes the SAME value to
``forecast_prob``/``analytical_prob``. Both the insert and both enrich branches
route through it.

Invariant (both sides): ``abs(booked - (cal if YES else 1-cal)) < 1e-6``.

Run: python3 -m pytest tests/test_item1_booked_prob_invariant_2026_10_16.py -v
"""
import importlib.util
import inspect
import os
import sqlite3
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

TOL = 1e-6


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge",
        os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _book_db():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_uuid TEXT NOT NULL UNIQUE, trade_date_utc TEXT NOT NULL,
            station TEXT NOT NULL, market_type TEXT NOT NULL,
            signal_direction TEXT NOT NULL, forecast_prob REAL,
            market_price REAL NOT NULL, trade_type TEXT NOT NULL,
            quantity INTEGER NOT NULL, position_size_usd REAL NOT NULL,
            trade_price REAL NOT NULL, trade_cost REAL NOT NULL,
            trade_version TEXT NOT NULL, functionality TEXT NOT NULL,
            status TEXT DEFAULT 'open', notes TEXT, implied_prob REAL,
            analytical_prob REAL, calibration_error REAL,
            confidence_indicator REAL, created_at_utc TEXT NOT NULL,
            p_gefs REAL, p_analog REAL, p_trajectory REAL, p_fused REAL,
            price_source TEXT, market_ticker TEXT, lane TEXT)""")
    return c


def _book(b, c, *, our_prob, cal, side="yes", market_price=0.30, ticker=None):
    return b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=our_prob, market_price=market_price,
        confidence=0.6, strike=75.0, ticker=ticker or "KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": cal, "calibrated_p": cal, "grade_entered": "B"},
        selected_side=side)


def _delta(c, tuuid):
    """|booked - (cal if YES else 1-cal)| for a booked row."""
    row = c.execute(
        "SELECT t.trade_type, t.forecast_prob, f.calibrated_p "
        "FROM trades t LEFT JOIN trade_forecaster_probs f "
        "ON f.trade_uuid = t.trade_uuid WHERE t.trade_uuid=?", (tuuid,)).fetchone()
    tt, booked, cal = row
    leg = "yes" if "yes" in (tt or "").lower() else "no"
    derived = cal if leg == "yes" else 1.0 - cal
    return abs(booked - derived)


# ── the invariant, BOTH sides ────────────────────────────────────────────────
@pytest.mark.unit
def test_yes_side_booked_equals_calibrated_p():
    b = _load_bridge()
    c = _book_db()
    r = _book(b, c, our_prob=0.42, cal=0.42, side="yes")
    assert r["status"] == "executed"
    booked, cal = c.execute(
        "SELECT t.forecast_prob, f.calibrated_p FROM trades t "
        "JOIN trade_forecaster_probs f ON f.trade_uuid=t.trade_uuid "
        "WHERE t.trade_uuid=?", (r["trade_uuid"],)).fetchone()
    assert booked == pytest.approx(cal, abs=TOL)
    assert _delta(c, r["trade_uuid"]) < TOL


@pytest.mark.unit
def test_no_side_booked_equals_one_minus_calibrated_p():
    b = _load_bridge()
    c = _book_db()
    r = _book(b, c, our_prob=0.70, cal=0.30, side="no")
    assert r["status"] == "executed"
    booked, cal = c.execute(
        "SELECT t.forecast_prob, f.calibrated_p FROM trades t "
        "JOIN trade_forecaster_probs f ON f.trade_uuid=t.trade_uuid "
        "WHERE t.trade_uuid=?", (r["trade_uuid"],)).fetchone()
    assert booked == pytest.approx(1.0 - cal, abs=TOL)
    assert _delta(c, r["trade_uuid"]) < TOL


@pytest.mark.unit
def test_restamp_resyncs_both_sides():
    """A companion re-stamp on each side must re-derive the booked prob."""
    b = _load_bridge()
    for side, p0, cal0, cal1 in (("yes", 0.40, 0.40, 0.55),
                                 ("no", 0.60, 0.40, 0.45)):
        c = _book_db()
        r = _book(b, c, our_prob=p0, cal=cal0, side=side)
        tuuid = r["trade_uuid"]
        # Re-stamp the companion with cal1; the booked prob must follow.
        _book(b, c, our_prob=cal1 if side == "yes" else 1 - cal1,
              cal=cal1, side=side)
        assert _delta(c, tuuid) < TOL, (side, _delta(c, tuuid))


@pytest.mark.unit
def test_writer_ignores_a_raw_inconsistent_our_prob():
    """The companion is the authority: a caller passing a raw/inconsistent
    `our_prob` cannot make the booked prob diverge from the calibrated source."""
    b = _load_bridge()
    c = _book_db()
    # our_prob (0.99) is NOT the side transform of calibrated_p (0.30) for a NO.
    r = _book(b, c, our_prob=0.99, cal=0.30, side="no")
    assert _delta(c, r["trade_uuid"]) < TOL, \
        "booked prob must be DERIVED from companion calibrated_p, not the caller"


@pytest.mark.unit
def test_no_injection_path_bypasses_the_single_writer():
    """Source contract: both enrich branches AND the insert route through
    `_sync_booked_prob`, and no other SQL writes trades.forecast_prob."""
    b = _load_bridge()
    src = inspect.getsource(b.record_trade)
    assert src.count("_sync_booked_prob(") >= 3, \
        "insert + both enrich branches must canonicalise the booked prob"
    full = open(os.path.join(_REPO, "scripts",
                             "sbox_intraday_ev_bridge.py")).read()
    # Exactly one UPDATE writer of the booked columns.
    assert full.count("UPDATE trades SET forecast_prob=") == 1
