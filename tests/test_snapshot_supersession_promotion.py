#!/usr/bin/env python3
"""
A4 promotion — provisional frozen snapshot rows superseded by finalized truth.

Ruling: `.meta/continuity/weather-engine/governance-provenance-drift-disposition-2026-10-10.md`
(A4: provisional layer + one-time promotion to canonical; settle path must read
canonical truth, never book a provisional proxy).

Contract:
  * resolution order: canonical frozen snapshot row -> finalized
    `kalshi_settlements` row -> fail closed. Never a provisional snapshot row;
  * a frozen provisional snapshot row stays byte-identical; the promotion is an
    additive `settlement_snapshot_supersession` annotation;
  * the settle writer books the FINALIZED value (not the frozen weatherapi proxy)
    and leaves the trade OPEN when neither source exists.

Run: python3 -m pytest tests/test_snapshot_supersession_promotion.py -v
"""
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from core import pnl_tracking  # noqa: E402
from core.canonical_settlement import (  # noqa: E402
    freeze_snapshot, load_canonical_snapshots, resolve_settlement_truth,
)
import promote_snapshot_supersession as pss  # noqa: E402

_DATE = "2026-10-09"
_STATION = "KSFO"
_STRIKE = 70
_PROVISIONAL = 64.94        # frozen weatherapi value (A2-provisional)
_FINALIZED = 71.0           # Kalshi finalized truth


def _make_kalshi_db(tmp_path, station=_STATION, date=_DATE, temp=_FINALIZED):
    db = tmp_path / "kalshi_settlements.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE kalshi_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, series TEXT,
            event_ticker TEXT, target_date TEXT, kalshi_temp REAL,
            markets_found INTEGER, finalized_count INTEGER, source TEXT,
            source_type TEXT, created_at TEXT, updated_at TEXT);
    """)
    c.execute(
        "INSERT INTO kalshi_settlements (station, series, event_ticker,"
        " target_date, kalshi_temp, source, source_type) VALUES (?,?,?,?,?,?,?)",
        (station, "KXHIGHTSFO", "KXHIGHTSFO-26OCT09", date, temp, "live_api",
         "finalized"))
    c.commit()
    c.close()
    return db


def _make_snapshot_db(tmp_path, *, freeze=True, source="weatherapi",
                      temp=_PROVISIONAL):
    db = tmp_path / "metar_backfill.db"
    c = sqlite3.connect(str(db))
    # The settle path reads epochs from the metar DB (`metar.settlement_epochs`).
    c.executescript("""
        CREATE TABLE settlement_epochs (
            station TEXT, market_type TEXT, local_trading_date TEXT,
            settlement_bucket REAL, prior_settlement_bucket REAL,
            epoch_status TEXT);
    """)
    if freeze:
        freeze_snapshot(c, [{
            "station": _STATION, "market_type": "HIGH", "local_trading_date": _DATE,
            "settlement_temp_f": temp, "temp_source": source,
            "strike_price": _STRIKE, "settled_at_utc": "2026-10-10T05:00:00Z",
        }], frozen_at_utc="2026-10-10T00:55:12Z")
    # the unreliable epoch bucket the settle path must NOT read
    c.execute("INSERT INTO settlement_epochs (station, market_type,"
              " local_trading_date, settlement_bucket, epoch_status)"
              " VALUES (?,?,?,?,?)", (_STATION, "HIGH", _DATE, 61, "closed"))
    c.commit()
    c.close()
    return db


# ─── resolution order ───────────────────────────────────────────────────────
def test_finalized_row_resolves_when_snapshot_is_provisional(tmp_path):
    snap = _make_snapshot_db(tmp_path)
    kal = _make_kalshi_db(tmp_path)
    cmap = load_canonical_snapshots(str(snap))
    assert (_STATION, "HIGH", _DATE) not in cmap          # provisional excluded
    rec, reason = resolve_settlement_truth(None, _STATION, "HIGH", _DATE,
                                           canonical_map=cmap, kalshi_db=str(kal))
    assert reason == "finalized_settlements_row"
    assert rec["settlement_temp_f"] == _FINALIZED
    assert rec["temp_source"] == "kalshi_finalized"


def test_canonical_snapshot_beats_finalized_row(tmp_path):
    # A canonical (kalshi_live_api) frozen row + a decoy finalized row: step 1 wins.
    snap = _make_snapshot_db(tmp_path, source="kalshi_live_api",
                             temp=_FINALIZED)
    kal = _make_kalshi_db(tmp_path, temp=999.0)
    cmap = load_canonical_snapshots(str(snap))
    rec, reason = resolve_settlement_truth(None, _STATION, "HIGH", _DATE,
                                           canonical_map=cmap, kalshi_db=str(kal))
    assert reason == "canonical_snapshot"
    assert rec["settlement_temp_f"] == _FINALIZED


def test_fail_closed_when_neither_source_exists(tmp_path):
    snap = _make_snapshot_db(tmp_path)                     # provisional only
    kal = tmp_path / "absent.db"
    cmap = load_canonical_snapshots(str(snap))
    rec, reason = resolve_settlement_truth(None, _STATION, "HIGH", _DATE,
                                           canonical_map=cmap, kalshi_db=str(kal))
    assert rec is None
    assert reason == "no_canonical_truth"


def test_low_market_fails_closed_on_finalized_step(tmp_path):
    kal = _make_kalshi_db(tmp_path)
    rec, reason = resolve_settlement_truth(None, _STATION, "LOW", _DATE,
                                           canonical_map={}, kalshi_db=str(kal))
    assert rec is None and reason == "no_canonical_truth"


# ─── settle writer books finalized, not frozen proxy ────────────────────────
def _make_paper(tmp_path):
    db = tmp_path / "paper.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_uuid TEXT UNIQUE,
            trade_date_utc TEXT, station TEXT, market_type TEXT,
            signal_direction TEXT, trade_type TEXT, quantity INTEGER,
            trade_price REAL, market_price REAL, functionality TEXT,
            confidence_indicator REAL, forecast_prob REAL, settled_value REAL,
            settlement_date_utc TEXT, realized_pnl REAL,
            settlement_return_amount REAL, status TEXT, notes TEXT,
            settlement_confirmed INTEGER DEFAULT 0, settlement_outcome TEXT,
            canonical_settled_f REAL, canonical_outcome TEXT,
            canonical_source TEXT);
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, position_uuid TEXT,
            trade_uuids TEXT, average_cost REAL, quantity REAL,
            realized_pnl REAL DEFAULT 0, total_pnl REAL DEFAULT 0);
    """)
    c.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, trade_type, quantity, trade_price, market_price,"
        " functionality, status, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t-1", _DATE, _STATION, "HIGH", "UP", "buy_yes", 71, 0.7, 0.7,
         "intraday_ev_strike", "open",
         "KXHIGHTSFO-26OCT09-T70 @ 70.0F; inst=SBOX"))
    c.execute("INSERT INTO positions (position_uuid, trade_uuids, quantity)"
              " VALUES ('pos-1','t-1',71)")
    c.commit()
    c.close()
    return db


class _StubRisk:
    def update_after_trade(self, result):
        return {}


def _trader(paper_db, metar_db):
    return types.SimpleNamespace(paper_db=str(paper_db), metar_db=str(metar_db),
                                 _calibrator=None, _risk_manager=_StubRisk())


@pytest.fixture(autouse=True)
def _strike(monkeypatch):
    monkeypatch.setattr(pnl_tracking, "_get_strike_price",
                        lambda *a, **k: _STRIKE)


def test_settle_books_finalized_not_frozen_proxy(tmp_path, monkeypatch):
    snap = _make_snapshot_db(tmp_path)
    kal = _make_kalshi_db(tmp_path)
    paper = _make_paper(tmp_path)
    import core.canonical_settlement as cs
    monkeypatch.setattr(cs, "KALSHI_SETTLEMENTS_DB", str(kal))

    pnl_tracking.process_settlements_for_date(_trader(paper, snap), _DATE)
    c = sqlite3.connect(str(paper))
    c.row_factory = sqlite3.Row
    t = dict(c.execute("SELECT * FROM trades WHERE trade_uuid='t-1'").fetchone())
    c.close()
    assert t["status"] == "closed"
    assert t["canonical_settled_f"] == _FINALIZED
    assert t["canonical_source"] == "kalshi_finalized"
    assert t["settled_value"] == _FINALIZED                 # not 64.94, not 61
    assert t["canonical_outcome"] == "correct"              # 71 > 70 -> ITM
    assert t["realized_pnl"] == pytest.approx((1.0 - 0.7) * 71)


def test_settle_leaves_open_when_no_truth(tmp_path, monkeypatch):
    snap = _make_snapshot_db(tmp_path)                       # provisional only
    paper = _make_paper(tmp_path)
    import core.canonical_settlement as cs
    monkeypatch.setattr(cs, "KALSHI_SETTLEMENTS_DB",
                        str(tmp_path / "absent.db"))

    pnl_tracking.process_settlements_for_date(_trader(paper, snap), _DATE)
    c = sqlite3.connect(str(paper))
    t = c.execute("SELECT status, canonical_settled_f FROM trades"
                  " WHERE trade_uuid='t-1'").fetchone()
    c.close()
    assert t[0] == "open"
    assert t[1] is None


# ─── supersession annotation ────────────────────────────────────────────────
def test_promotion_annotates_without_moving_frozen_rows(tmp_path):
    snap = _make_snapshot_db(tmp_path)
    kal = _make_kalshi_db(tmp_path)
    before = pss.frozen_checksum(pss.load_snapshot(snap))
    rc = pss.run(execute=True, snapshot_db=snap, kalshi_db=kal)
    assert rc == 0
    after = pss.frozen_checksum(pss.load_snapshot(snap))
    assert before == after                                  # frozen untouched

    c = sqlite3.connect(str(snap))
    rows = c.execute("SELECT * FROM settlement_snapshot_supersession").fetchall()
    cols = [d[0] for d in c.execute(
        "SELECT * FROM settlement_snapshot_supersession").description]
    c.close()
    assert len(rows) == 1
    rec = dict(zip(cols, rows[0]))
    assert rec["annotation"] == "superseded_by_canonical"
    assert rec["frozen_temp_f"] == _PROVISIONAL
    assert rec["canonical_temp_f"] == _FINALIZED
    assert rec["canonical_source"] == "kalshi_finalized"


def test_promotion_is_idempotent_and_skips_canonical_rows(tmp_path):
    snap = _make_snapshot_db(tmp_path)
    kal = _make_kalshi_db(tmp_path)
    pss.run(execute=True, snapshot_db=snap, kalshi_db=kal)
    pss.run(execute=True, snapshot_db=snap, kalshi_db=kal)
    c = sqlite3.connect(str(snap))
    n = c.execute("SELECT COUNT(*) FROM settlement_snapshot_supersession").fetchone()[0]
    c.close()
    assert n == 1                                            # INSERT OR REPLACE
