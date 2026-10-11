#!/usr/bin/env python3
"""
SBOX trade uniqueness (Sprint C / E4-b, 2026-10-11).

The SBOX `trades` DDL carried only `trade_uuid UNIQUE`, so a duplicate decision
intent (double-down, cron re-run, partial re-invoke) silently multi-booked: each
new row drew a fresh uuid4. This locks the row-identity uniqueness constraint
`(station, market_ticker, created_at_utc)` on the booking path.

A second identical insert must be refused (idempotent no-op), not a second row.

Hermetic: in-memory / temp DBs only; the real data/*.db is never written (the
repo's db_write_sandbox fixture enforces this).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

try:
    from scripts.sbox_intraday_ev_bridge import (
        record_trade, _ensure_trade_uniqueness, _covered_ticker,
        UNIQUE_INTENT_INDEX,
    )
    _HAS_BRIDGE = True
except Exception:  # pragma: no cover
    _HAS_BRIDGE = False

_requires_bridge = pytest.mark.skipif(not _HAS_BRIDGE,
                                      reason="bridge import needs scipy stack")


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


def _book(c, *, day="2026-10-04", station="KNYC",
          ticker="KXHIGHNY-26OCT04-T75", functionality="intraday_ev_strike"):
    return record_trade(
        c, station, "HIGH", "BUY", our_prob=0.60, market_price=0.30,
        confidence=0.7, strike=75.0, ticker=ticker, size_usd=25.0,
        functionality=functionality, day_str=day, selected_side="yes")


def test_covered_ticker_regex():
    assert _covered_ticker(
        "multi-forecaster EV-strike KXHIGHNY-26OCT04-T75 @ 75F; inst=SBOX"
    ) == "KXHIGHNY-26OCT04-T75"
    assert _covered_ticker("nothing here") is None
    assert _covered_ticker(None) is None


@_requires_bridge
def test_schema_adds_uniqueness_index():
    c = _book_db()
    _ensure_trade_uniqueness(c)
    idx = c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (UNIQUE_INTENT_INDEX,)).fetchone()
    assert idx is not None
    assert "market_ticker" in {d[1] for d in c.execute("PRAGMA table_info(trades)")}
    # idempotent: re-running does not raise
    _ensure_trade_uniqueness(c)


@_requires_bridge
def test_second_identical_intent_is_refused():
    """The core E4-b proof: a double-book of the SAME decision intent inserts
    exactly ONE row (the 2nd is an idempotent no-op), not two."""
    c = _book_db()
    r1 = _book(c)
    assert r1["status"] == "executed"
    # force the exact same decision instant to simulate a re-run/double-down
    c.execute("UPDATE trades SET created_at_utc=? WHERE trade_uuid=?",
              ("2026-10-04T17:00:00Z", r1["trade_uuid"]))
    c.commit()

    # same station/ticker/decision instant -> the DB must refuse a twin.
    try:
        c.execute(
            "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
            " signal_direction, market_price, trade_type, quantity,"
            " position_size_usd, trade_price, trade_cost, trade_version,"
            " functionality, created_at_utc, market_ticker)"
            " VALUES ('twin','2026-10-04','KNYC','HIGH','UP',0.3,'buy_yes',50,"
            "25,0.3,15,'v3.1_sbox','intraday_ev_strike',"
            "'2026-10-04T17:00:00Z','KXHIGHNY-26OCT04-T75')")
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "duplicate decision intent was not refused by the DB"
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


@_requires_bridge
def test_record_trade_duplicate_is_idempotent_noop():
    """record_trade returns the existing uuid and does not add a row when the
    decision instant collides (the exclusion backstop, not the fast path)."""
    c = _book_db()
    r1 = _book(c)
    c.execute("UPDATE trades SET created_at_utc=? WHERE trade_uuid=?",
              ("2026-10-04T17:00:00Z", r1["trade_uuid"]))
    c.commit()

    # Patch the writer's wall-clock so the 2nd call reproduces the same instant.
    import scripts.sbox_intraday_ev_bridge as B
    orig = B.datetime

    class _DT(orig):
        @classmethod
        def now(cls, tz=None):
            return orig(2026, 10, 4, 17, 0, 0, tzinfo=tz)

    B.datetime = _DT
    try:
        r2 = _book(c)
    finally:
        B.datetime = orig
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert r2["trade_uuid"] == r1["trade_uuid"]


@_requires_bridge
def test_distinct_intents_still_book():
    """Different decision identities (different stations) are NOT collapsed."""
    c = _book_db()
    _book(c, station="KNYC", ticker="KXHIGHNY-26OCT04-T75")
    _book(c, station="KSFO", ticker="KXHIGHTSFO-26OCT04-T70")
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
