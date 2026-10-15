#!/usr/bin/env python3
"""
Forward settle path — the settlement writer must read CANONICAL truth.

Locks the 2026-10-10 forward fix: `pnl_tracking.process_settlements_for_date`
books from the frozen canonical snapshot (`settlement_snapshot`, Kalshi live API
/ GHCN) — NOT from the epoch `settlement_bucket` (the unreliable settle-time
METAR extreme the ruling flagged).

Contract:
  * a new settlement populates `canonical_settled_f` / `canonical_outcome` /
    `canonical_source` (not just `realized_pnl`);
  * the booked `settled_value` heading forward equals canonical truth;
  * no frozen canonical row for a station-day => the trade stays OPEN (fail
    closed), never booked from the raw/epoch value;
  * immutability: a settlement that would MOVE an existing canonical value
    FAILS LOUD (ImmutableSettlementError) and the trade is not written;
  * re-running a settled date is a no-op (already-closed trades are not
    re-selected; nothing moves).

Run: python3 -m pytest tests/test_forward_settle_canonical.py -v
"""
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core import pnl_tracking  # noqa: E402
from core.canonical_settlement import (  # noqa: E402
    ImmutableSettlementError, ensure_snapshot_table, freeze_snapshot,
)

_DATE = "2026-10-04"
_STATION = "KBOS"
_STRIKE = 61
# The unreliable settle-time read the epoch table carries (partial METAR extreme).
_EPOCH_BUCKET = 57
# Canonical truth (Kalshi live API expiration_value) for KBOS 2026-10-04 HIGH.
_CANONICAL = 63


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
            functionctionality TEXT, functionality TEXT,
            confidence_indicator REAL, forecast_prob REAL,
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
    # An open BUY_YES trade at 0.04 on KBOS HIGH 2026-10-04, strike 61 in notes.
    p.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, trade_type, quantity, trade_price, market_price,"
        " functionality, confidence_indicator, forecast_prob, status, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t-1", _DATE, _STATION, "HIGH", "UP", "buy_yes", 2000, 0.04, 0.04,
         "late_day_momentum", 0.7, 0.7, "open",
         "KXHIGHTBOS-26OCT04-T61 @ 61.0F; inst=SBOX"))
    p.execute(
        "INSERT INTO positions (position_uuid, trade_uuids, quantity)"
        " VALUES ('pos-1', 't-1', 2000)")
    p.commit()
    p.close()

    m = sqlite3.connect(metar_db)
    m.executescript("""
        CREATE TABLE settlement_epochs (
            station TEXT, market_type TEXT, local_trading_date TEXT,
            settlement_bucket REAL, prior_settlement_bucket REAL,
            epoch_status TEXT
        );
    """)
    # Closed epoch carrying the WRONG (partial-METAR) value the ruling flagged.
    m.execute(
        "INSERT INTO settlement_epochs (station, market_type, local_trading_date,"
        " settlement_bucket, prior_settlement_bucket, epoch_status)"
        " VALUES (?,?,?,?,?,?)",
        (_STATION, "HIGH", _DATE, _EPOCH_BUCKET, 60, "closed"))
    # Frozen canonical snapshot: the truth source the writer must read.
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


def _fake_trader(paper_db, metar_db):
    return types.SimpleNamespace(
        paper_db=paper_db, metar_db=metar_db,
        _calibrator=None, _risk_manager=_StubRisk(),
    )


@pytest.fixture(autouse=True)
def _strike_from_notes(monkeypatch):
    """Strike resolution reads the trade's own `notes` (no network)."""
    monkeypatch.setattr(pnl_tracking, "_get_strike_price",
                        lambda station, market_type, date, paper_db, metar_db: _STRIKE)


def _trade(paper_db, uuid="t-1"):
    c = sqlite3.connect(paper_db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM trades WHERE trade_uuid=?", (uuid,)).fetchone()
    c.close()
    return dict(row)


def test_new_settlement_populates_canonical_truth(tmp_path):
    paper_db, metar_db = _make_dbs(tmp_path)
    pnl_tracking.process_settlements_for_date(_fake_trader(paper_db, metar_db), _DATE)

    t = _trade(paper_db)
    # Canonical truth is populated, not just realized_pnl.
    assert t["canonical_settled_f"] == _CANONICAL
    assert t["canonical_source"] == "kalshi_live_api"
    assert t["canonical_outcome"] == "correct"      # UP, 63 > 61 -> ITM
    assert t["settlement_confirmed"] == 1
    assert t["settlement_outcome"] == "correct"
    assert t["status"] == "closed"
    # Booked value going forward is canonical truth — NOT the epoch bucket (57).
    assert t["settled_value"] == _CANONICAL
    assert t["settled_value"] != _EPOCH_BUCKET
    # realized_pnl is CONTRACT-NOTIONAL (Dan ruling 2026-10-15): (1.0 - 0.04) * 2000.
    # Size-scaled: the settle writer scores (payout - price) * quantity, matching
    # the shipped qty-scaled SBOX book (PWW-BUG-HUNT F7).
    assert t["realized_pnl"] == pytest.approx((1.0 - 0.04) * 2000)
    assert t["realized_pnl"] == pytest.approx(1920.0)


def test_epoch_bucket_would_have_flipped_the_outcome(tmp_path):
    paper_db, metar_db = _make_dbs(tmp_path)
    pnl_tracking.process_settlements_for_date(_fake_trader(paper_db, metar_db), _DATE)
    t = _trade(paper_db)
    # With the bad epoch value (57 <= strike 61) the trade would have LOST the
    # canonical outcome. Reading canonical truth fixes it.
    assert t["canonical_settled_f"] > _STRIKE > _EPOCH_BUCKET
    assert t["realized_pnl"] > 0


def test_no_canonical_row_leaves_trade_open(tmp_path, monkeypatch):
    paper_db, metar_db = _make_dbs(tmp_path)
    # Wipe the frozen snapshot -> no canonical truth for the station-day.
    m = sqlite3.connect(metar_db)
    m.execute("DROP TRIGGER trg_settlement_snapshot_no_delete")
    m.execute("DELETE FROM settlement_snapshot")
    m.commit()
    m.close()
    # A4 (2026-10-11): the settle path now also falls through to a finalized
    # `kalshi_settlements` row. Point that source at an absent file so this test
    # still exercises the true fail-closed contract (neither snapshot nor
    # finalized truth -> leave OPEN).
    import core.canonical_settlement as cs
    monkeypatch.setattr(cs, "KALSHI_SETTLEMENTS_DB", str(tmp_path / "absent.db"))

    pnl_tracking.process_settlements_for_date(_fake_trader(paper_db, metar_db), _DATE)
    t = _trade(paper_db)
    assert t["status"] == "open"                    # fail closed: never booked raw
    assert t["canonical_settled_f"] is None
    assert t["realized_pnl"] is None
    assert t["settled_value"] is None


def test_moving_existing_canonical_value_fails_loud(tmp_path):
    paper_db, metar_db = _make_dbs(tmp_path)
    # Pre-seed a DIFFERENT canonical value on the open trade (simulates a prior
    # fetch). A settlement must not move it — the invariant fails loud.
    m = sqlite3.connect(paper_db)
    m.execute("UPDATE trades SET canonical_settled_f=? WHERE trade_uuid='t-1'",
              (60.0,))
    m.commit()
    m.close()

    with pytest.raises(ImmutableSettlementError):
        pnl_tracking.process_settlements_for_date(_fake_trader(paper_db, metar_db), _DATE)
    t = _trade(paper_db)
    assert t["status"] == "open"                    # trade not written
    assert t["canonical_settled_f"] == 60.0         # value not moved


def test_rerun_settled_date_is_a_noop(tmp_path):
    paper_db, metar_db = _make_dbs(tmp_path)
    trader = _fake_trader(paper_db, metar_db)
    pnl_tracking.process_settlements_for_date(trader, _DATE)
    before = _trade(paper_db)

    # Re-run the same (now settled) date: closed trades are not re-selected.
    pnl_tracking.process_settlements_for_date(trader, _DATE)
    after = _trade(paper_db)

    for k in ("status", "canonical_settled_f", "canonical_outcome",
              "canonical_source", "settled_value", "realized_pnl",
              "settlement_confirmed", "settlement_outcome"):
        assert before[k] == after[k], f"{k} moved on re-run"


def test_snapshot_table_is_immutable(tmp_path):
    _, metar_db = _make_dbs(tmp_path)
    m = sqlite3.connect(metar_db)
    with pytest.raises(sqlite3.IntegrityError):
        m.execute("UPDATE settlement_snapshot SET settlement_temp_f=99")
    m.close()
