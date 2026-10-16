#!/usr/bin/env python3
"""ITEM 3 (2026-10-16) — an unresolved strike axis must LEAVE THE TRADE OPEN.

Cold-eyes BLOCK: `core/pnl_tracking.py` fell back to a `market_type` guess
(HIGH -> '>', LOW -> '<') when `_resolve_strike_axis_row()` returned no axis and
then SETTLED the trade. The ticker suffix `-T##` is ambiguous between the "or
above" and "or below" tails (the code at the axis-resolution site documents
this), so the guess can INVERT the payout — a HIGH "or below" market settled with
'>' pays the losing side on every body.

Fix: no axis -> loud skip, trade stays OPEN, no settlement write.

Run: python3 -m pytest tests/test_item3_settle_axis_failclosed_2026_10_16.py -v
"""
import inspect
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core import pnl_tracking  # noqa: E402
from core.canonical_settlement import freeze_snapshot  # noqa: E402

_DATE = "2026-10-04"
_STATION = "KBOS"
_STRIKE = 61
_CANONICAL = 63  # 63 > 61 -> the 'greater' YES leg would be ITM


def _make_dbs(tmp_path):
    paper_db = str(tmp_path / "paper.db")
    metar_db = str(tmp_path / "metar.db")

    p = sqlite3.connect(paper_db)
    p.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_uuid TEXT UNIQUE, trade_date_utc TEXT, station TEXT,
            market_type TEXT, signal_direction TEXT, trade_type TEXT,
            quantity INTEGER, trade_price REAL, market_price REAL,
            functionality TEXT, confidence_indicator REAL, forecast_prob REAL,
            settled_value REAL, settlement_date_utc TEXT, realized_pnl REAL,
            settlement_return_amount REAL, status TEXT, notes TEXT,
            settlement_confirmed INTEGER DEFAULT 0, settlement_outcome TEXT,
            canonical_settled_f REAL, canonical_outcome TEXT, canonical_source TEXT
        );
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, position_uuid TEXT,
            trade_uuids TEXT, average_cost REAL, quantity REAL,
            realized_pnl REAL DEFAULT 0, total_pnl REAL DEFAULT 0
        );
    """)
    p.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, trade_type, quantity, trade_price, market_price,"
        " functionality, confidence_indicator, forecast_prob, status, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t-1", _DATE, _STATION, "HIGH", "UP", "buy_yes", 100, 0.04, 0.04,
         "late_day_momentum", 0.7, 0.7, "open",
         "KXHIGHTBOS-26OCT04-T61 @ 61.0F; inst=SBOX"))
    p.execute("INSERT INTO positions (position_uuid, trade_uuids, quantity)"
              " VALUES ('pos-1', 't-1', 100)")
    p.commit()
    p.close()

    m = sqlite3.connect(metar_db)
    m.executescript("""
        CREATE TABLE settlement_epochs (
            station TEXT, market_type TEXT, local_trading_date TEXT,
            settlement_bucket REAL, prior_settlement_bucket REAL,
            epoch_status TEXT);
    """)
    m.execute(
        "INSERT INTO settlement_epochs (station, market_type, local_trading_date,"
        " settlement_bucket, prior_settlement_bucket, epoch_status)"
        " VALUES (?,?,?,?,?,?)", (_STATION, "HIGH", _DATE, 61, 60, "closed"))
    freeze_snapshot(m, [{
        "station": _STATION, "market_type": "HIGH", "local_trading_date": _DATE,
        "settlement_temp_f": _CANONICAL, "temp_source": "kalshi_live_api",
        "strike_price": _STRIKE, "settled_at_utc": "2026-10-05T05:00:00Z",
    }], frozen_at_utc="2026-10-05T05:00:00Z")
    m.commit()
    m.close()
    return paper_db, metar_db


class _StubRisk:
    def update_after_trade(self, result):
        return {}


def _trader(paper_db, metar_db):
    return types.SimpleNamespace(paper_db=paper_db, metar_db=metar_db,
                                 _calibrator=None, _risk_manager=_StubRisk())


def _trade(paper_db):
    c = sqlite3.connect(paper_db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM trades WHERE trade_uuid='t-1'").fetchone()
    c.close()
    return dict(row)


@pytest.mark.unit
def test_unresolved_axis_leaves_trade_open(tmp_path, monkeypatch):
    paper_db, metar_db = _make_dbs(tmp_path)
    monkeypatch.setattr(pnl_tracking, "_get_strike_price",
                        lambda *a, **k: _STRIKE)
    # No axis resolvable (the axis DB has no such ticker in this fixture).
    monkeypatch.setattr(pnl_tracking, "_resolve_strike_axis_row",
                        lambda ticker, **k: None)

    pnl_tracking.process_settlements_for_date(_trader(paper_db, metar_db), _DATE)

    t = _trade(paper_db)
    assert t["status"] == "open", "unresolved axis must NOT settle the trade"
    assert t["realized_pnl"] is None
    assert t["settled_value"] is None
    assert t["canonical_outcome"] is None


@pytest.mark.unit
def test_market_type_fallback_is_gone_from_source():
    """No `market_type`-guessed operator may remain on the settle- payout path."""
    src = inspect.getsource(pnl_tracking.process_settlements_for_date)
    assert 'elif market_type == "HIGH":\n                settlement_contract_value' not in src
    assert 'elif market_type == "LOW":\n                settlement_contract_value' not in src
    # The loud skip reason is present instead.
    assert "strike axis unresolved" in src


@pytest.mark.unit
def test_resolved_axis_still_settles(tmp_path, monkeypatch):
    """Control: with a resolved 'greater' axis the trade settles as before."""
    paper_db, metar_db = _make_dbs(tmp_path)
    monkeypatch.setattr(pnl_tracking, "_get_strike_price",
                        lambda *a, **k: _STRIKE)
    monkeypatch.setattr(pnl_tracking, "_resolve_strike_axis_row",
                        lambda ticker, **k: {"strike_type": "greater",
                                             "floor_strike": _STRIKE,
                                             "cap_strike": None})

    pnl_tracking.process_settlements_for_date(_trader(paper_db, metar_db), _DATE)

    t = _trade(paper_db)
    assert t["status"] == "closed"
    assert t["canonical_settled_f"] == _CANONICAL
    assert t["realized_pnl"] == pytest.approx((1.0 - 0.04) * 100)
