#!/usr/bin/env python3
"""
F2 side-inversion functional test (2026-10-07)
=============================================
Pins the F2 fix (commit 17a2fd703): `record_trade` must label
`signal_direction` / `trade_type` from the EV-selected LEG
(`selected_side`: "yes" -> UP/buy_yes, "no" -> DOWN/buy_no), NOT from the
EntryTimingEngine's BUY/SELL default.

Why this matters: before the fix, a NO-leg pick recorded under an engine
BUY signal was written as UP/buy_yes while priced at the NO band
(booked ~= 1 - mid). That produced the KBOS 1fa61868 / KATL d2dc37e8
"inversion" rows the E1 verifier then flagged as MISMATCH. This test proves
the corrected label axis and that the verifier's `_expected_band` accepts the
resulting buy_no booking.

Deterministic, hermetic: uses an in-memory DB seeded with only the columns
`record_trade` touches. Never opens data/*.db.
"""
import os
import sqlite3
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

from scripts.sbox_intraday_ev_bridge import record_trade  # noqa: E402
from scripts.verify_price_provenance import _expected_band  # noqa: E402


def _mem_db():
    """Minimal schema matching the columns record_trade reads/writes."""
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE trades (
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
            price_source TEXT)""")
    c.execute(
        """CREATE TABLE trade_forecaster_probs (
            trade_uuid TEXT NOT NULL UNIQUE, station TEXT NOT NULL,
            trade_date_utc TEXT NOT NULL, strike REAL, side TEXT, ticker TEXT,
            p_gefs REAL, p_analog REAL, p_trajectory REAL, p_fused REAL,
            n_analog INTEGER, n_traj INTEGER, agreement TEXT, edge_mult REAL,
            traj_verdict TEXT, traj_mult REAL, fusion_rule TEXT,
            created_at_utc TEXT NOT NULL)""")
    return c


def _call(conn, engine_side, selected_side, day, price):
    return record_trade(
        conn, "KBOS", "HIGH", engine_side, our_prob=0.60,
        market_price=price, confidence=0.7, strike=75.0,
        ticker="KXHIGHTBOS-26OCT06-T75", size_usd=25.0,
        functionality="intraday_ev_strike", day_str=day,
        selected_side=selected_side)


def _row(conn, day):
    return conn.execute(
        "SELECT signal_direction, trade_type, market_price FROM trades "
        "WHERE trade_date_utc=?", (day,)).fetchone()


# --- the 5 cases -----------------------------------------------------------

def test_engine_buy_selected_yes_is_up_buy_yes():
    c = _mem_db()
    _call(c, "BUY", "yes", "2026-10-06", 0.335)
    d, tt, mp = _row(c, "2026-10-06")
    assert (d, tt) == ("UP", "buy_yes")


def test_engine_buy_selected_no_is_down_buy_no():
    """THE F2 FIX: NO leg under an engine BUY must record DOWN / buy_no."""
    c = _mem_db()
    _call(c, "BUY", "no", "2026-10-06", 0.665)  # NO band ~= 1 - yes_mid
    d, tt, mp = _row(c, "2026-10-06")
    assert (d, tt) == ("DOWN", "buy_no")


def test_engine_sell_selected_no_is_down_buy_no():
    c = _mem_db()
    _call(c, "SELL", "no", "2026-10-07", 0.90)
    d, tt, _ = _row(c, "2026-10-07")
    assert (d, tt) == ("DOWN", "buy_no")


def test_engine_sell_selected_yes_is_up_buy_yes():
    c = _mem_db()
    _call(c, "SELL", "yes", "2026-10-08", 0.10)
    d, tt, _ = _row(c, "2026-10-08")
    assert (d, tt) == ("UP", "buy_yes")


def test_missing_selected_side_falls_back_to_engine_default():
    """Legacy callers (selected_side=None) keep the old BUY/SELL mapping."""
    c = _mem_db()
    _call(c, "BUY", None, "2026-10-09", 0.50)
    d, tt, _ = _row(c, "2026-10-09")
    assert (d, tt) == ("UP", "buy_yes")


# --- verifier coupling ------------------------------------------------------

def test_verifier_band_accepts_buy_no_booking():
    """A correct post-fix NO-leg booking (~= 1 - yes_mid) must MATCH, not MISMATCH."""
    bid, ask, mid = 0.66, 0.68, 0.67          # decision-time YES snapshot
    booked_no = 1.0 - mid                      # what the NO leg actually pays
    lo, hi = _expected_band("buy_no", bid, ask, mid)
    assert lo <= booked_no <= hi, (lo, hi, booked_no)


def test_verifier_band_buy_no_equals_sell_yes():
    bid, ask, mid = 0.66, 0.68, 0.67
    assert _expected_band("buy_no", bid, ask, mid) == \
        _expected_band("sell_yes", bid, ask, mid)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
