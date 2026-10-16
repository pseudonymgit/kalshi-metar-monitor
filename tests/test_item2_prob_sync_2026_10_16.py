#!/usr/bin/env python3
"""ITEM2 (2026-10-16) — a companion re-stamp must re-sync the BOOKED prob.

Verified defect: the fast-path enrich at `record_trade` refreshed `p_*` and the
`trade_forecaster_probs` companion row but never re-wrote the booked
`forecast_prob`/`analytical_prob`. Observed on ids 285/287/288: the companion was
re-stamped at 15:15 while the trade row kept the 14:15 values — the scoring/
calibrating paths read a stale probability, so booked prob != companion
calibrated_p.

Fix: on every enrich (fast-path hit AND duplicate-intent no-op) re-sync the
booked `forecast_prob`/`analytical_prob`/`calibration_error` from the SAME
calibrated value used at booking, in the SAME transaction as the companion
update, so a re-stamp is atomic on both sides.

Run: python3 -m pytest tests/test_item2_prob_sync_2026_10_16.py -v
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


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


def _book(b, c, *, our_prob=0.60, cal=0.60):
    return b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=our_prob, market_price=0.30,
        confidence=0.6, strike=75.0, ticker="KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": 0.55, "agreement": "sole", "calibrated_p": cal,
               "grade_entered": "B", "grade_cap_usd": 10.0},
        selected_side="yes")


@pytest.mark.unit
def test_enrich_resyncs_booked_prob():
    """A second call (enrich path) with a new calibrated value must update the
    booked prob to match the companion — atomically."""
    b = _load_bridge()
    c = _book_db()
    r1 = _book(b, c, our_prob=0.55, cal=0.55)
    assert r1["status"] == "executed"
    tuuid = r1["trade_uuid"]

    # Re-stamp with a NEW calibrated value (companion would move to 0.70).
    r2 = b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=0.70, market_price=0.30,
        confidence=0.4, strike=75.0, ticker="KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": 0.66, "calibrated_p": 0.70,
               "grade_entered": "B", "grade_cap_usd": 10.0},
        selected_side="yes")
    assert r2["status"] == "enriched"

    booked = c.execute(
        "SELECT forecast_prob, analytical_prob FROM trades WHERE trade_uuid=?",
        (tuuid,)).fetchone()
    comp = c.execute(
        "SELECT calibrated_p FROM trade_forecaster_probs WHERE trade_uuid=?",
        (tuuid,)).fetchone()
    assert booked[0] == 0.70, f"booked forecast_prob stale: {booked[0]}"
    assert booked[1] == 0.70, f"booked analytical_prob stale: {booked[1]}"
    assert booked[0] == comp[0], "booked prob must equal companion calibrated_p"


@pytest.mark.unit
def test_enrich_resyncs_calibration_error():
    b = _load_bridge()
    c = _book_db()
    r = _book(b, c, our_prob=0.55, cal=0.55)
    tuuid = r["trade_uuid"]
    b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=0.70, market_price=0.30,
        confidence=0.4, strike=75.0, ticker="KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": 0.66, "calibrated_p": 0.70}, selected_side="yes")
    err = c.execute(
        "SELECT calibration_error FROM trades WHERE trade_uuid=?",
        (tuuid,)).fetchone()[0]
    assert abs(err - (0.70 - 0.30)) < 1e-9, err


@pytest.mark.unit
def test_duplicate_intent_noop_resyncs_booked_prob():
    """The DB-refused duplicate-intent branch must also re-sync the booked prob."""
    b = _load_bridge()
    c = _book_db()
    r1 = _book(b, c, our_prob=0.55, cal=0.55)
    tuuid = r1["trade_uuid"]
    # Pin the decision instant so the second intent collides on the unique index.
    c.execute("UPDATE trades SET created_at_utc=? WHERE trade_uuid=?",
              ("2026-10-04T17:00:00Z", tuuid))
    c.commit()
    b._ensure_trade_uniqueness(c)

    r2 = b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=0.80, market_price=0.30,
        confidence=0.6, strike=75.0, ticker="KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": 0.75, "calibrated_p": 0.80}, selected_side="yes")
    assert r2["status"] in ("duplicate_intent_noop", "enriched"), r2["status"]
    booked = c.execute(
        "SELECT forecast_prob FROM trades WHERE trade_uuid=?", (tuuid,)).fetchone()
    comp = c.execute(
        "SELECT calibrated_p FROM trade_forecaster_probs WHERE trade_uuid=?",
        (tuuid,)).fetchone()
    assert booked[0] == comp[0], (booked[0], comp[0])


@pytest.mark.unit
def test_sync_helper_is_wired_in_both_branches():
    b = _load_bridge()
    import inspect
    src = inspect.getsource(b.record_trade)
    assert src.count("_sync_booked_prob(") >= 2, \
        "both enrich branches must re-sync the booked prob"
