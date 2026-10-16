#!/usr/bin/env python3
"""ITEM4 (2026-10-16) — v4 live-book minors.

4.1 grade vs grade_entered disagreed on 5 entered rows (companion `grade`='F'
    while `grade_entered` gated entry). The companion `grade` was written from
    the fusion meta's `grade_from_confidence` re-score (a COMPOSITE-CONFIDENCE
    letter) instead of the edge-ladder gate grade that actually gated entry. The
    booking path now writes the GATE-TIME grade into both.

4.2 `calibrated_at` was NULL on every live row. The booking path now stamps it
    whenever a calibrated_p is written; `trade_calibration` is documented as a
    RETIRED one-off table (the companion IS the live calibration record).

4.3 the gate-ledger `trade_uuid` is the deterministic decision-intent key
    `SBOX-{station}-{day}` (0 direct joins) — documented BY DESIGN.

4.4 the settle `UPDATE positions` matching 0 rows (empty positions) — documented
    BY DESIGN (positions are derived from `trades`).

Run: python3 -m pytest tests/test_item4_minors_2026_10_16.py -v
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


@pytest.mark.unit
def test_4_1_companion_grade_is_the_gate_grade():
    """The companion `grade` must equal the gate grade (`grade_entered`)."""
    b = _load_bridge()
    c = _book_db()
    b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=0.60, market_price=0.30,
        confidence=0.6, strike=75.0, ticker="KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": 0.55, "calibrated_p": 0.60,
               "grade_entered": "B", "grade_cap_usd": 10.0,
               # The OLD re-score that must NOT win:
               "grade": "F"},
        selected_side="yes")
    row = c.execute(
        "SELECT grade, grade_entered FROM trade_forecaster_probs").fetchone()
    assert row[0] == "B", f"companion grade must be the gate grade, got {row[0]}"
    assert row[0] == row[1]
    c.close()


@pytest.mark.unit
def test_4_2_calibrated_at_is_stamped_at_booking():
    b = _load_bridge()
    c = _book_db()
    b.record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=0.60, market_price=0.30,
        confidence=0.6, strike=75.0, ticker="KXHIGHNY-26OCT04-T75",
        size_usd=10.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        probs={"p_fused": 0.55, "calibrated_p": 0.60}, selected_side="yes")
    at = c.execute(
        "SELECT calibrated_at FROM trade_forecaster_probs").fetchone()[0]
    assert at is not None and len(str(at)) > 10, f"calibrated_at not stamped: {at}"
    c.close()


@pytest.mark.unit
def test_4_2_trade_calibration_is_documented_retired():
    """The companion IS the live calibration record; trade_calibration is retired."""
    src = open(os.path.join(_REPO, "scripts",
                            "sbox_intraday_ev_bridge.py")).read()
    assert "RETIRED" in src and "trade_calibration" in src


@pytest.mark.unit
def test_4_3_gate_ledger_intent_key_is_documented():
    b = _load_bridge()
    src = inspect.getsource(b.run)
    assert "SBOX-{st}-{day_str}" in src or 'f"{INSTANCE}-{st}-{day_str}"' in src
    doc = os.path.join(_REPO, "docs", "plans",
                       "GATE-LEDGER-INTENT-KEY-2026-10-16.md")
    assert os.path.exists(doc)


@pytest.mark.unit
def test_4_4_positions_derived_doc_exists():
    doc = os.path.join(_REPO, "docs", "plans", "POSITIONS-DERIVED-2026-10-16.md")
    assert os.path.exists(doc)
    text = open(doc).read()
    assert "positions" in text and "derived" in text.lower()


@pytest.mark.unit
def test_live_trades_284_293_untouched():
    """Record integrity: the 10 day-1 fills must still be the original rows."""
    b = _load_bridge()
    p = os.path.join(_REPO, "data", "paper_trading_sbox.db")
    if not os.path.exists(p):
        pytest.skip("live book not present")
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    ids = [r[0] for r in c.execute(
        "SELECT id FROM trades ORDER BY id")]
    c.close()
    # The book is the 10 day-1 rows 284-293; the fix must not have altered them.
    assert ids == list(range(284, 294)), ids
