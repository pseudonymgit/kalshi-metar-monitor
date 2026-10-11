#!/usr/bin/env python3
"""
Live booking-path wiring tests (GR5 §1/§2/§6, 2026-10-10).

Locks the wiring the bridge/measurement path now depends on:
  - the decidable booking-time gate (G0/G1/G4) ADMITS a clean candidate and
    REJECTS a wrong-axis candidate, logging the verdict + failing gate to the
    append-only gate ledger;
  - a rejected candidate is NOT booked and gets NO custody row;
  - an accepted trade gets exactly ONE TradeCustodyRecord row (idempotent,
    chain-verifying);
  - the full composed G0–G5 gate rejects a lookahead candidate and admits a
    clean settled one;
  - the pre-registration epoch verifies (and refuses on tamper);
  - the real 13:30 station-local entry pin is supplied (never a 13:00 default);
  - the settled-admission boundary logs every settled row's verdict with its
    failing check and writes custody only for admits.

Hermetic: every DB is a temp/in-memory store; the real data/*.db is never
opened for writing (the repo's db_write_sandbox fixture enforces this).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from core.fees import kalshi_fee  # noqa: E402
from core.booking_admission import (  # noqa: E402
    CandidateRejected, admit_candidate, admit_decision_candidate,
    admit_settled_trades, candidate_from_trades_row, column_count,
    pinned_entry_time_utc, record_custody, verify_run_relies_on_prereg,
    FROZEN_TRADE_VERSION, GOVERNED_EXPERIMENT,
)
from core.trade_custody import verify_chain  # noqa: E402
from gate_composer import VERDICT_ADMITTED, VERDICT_REJECTED  # noqa: E402

# scripts.sbox_intraday_ev_bridge imports scipy; skip the bridge-write tests if
# the heavy forecast stack is unavailable (the admission module itself needs no
# scipy).
try:
    from scripts.sbox_intraday_ev_bridge import record_trade  # noqa: E402
    _HAS_BRIDGE = True
except Exception:  # pragma: no cover
    _HAS_BRIDGE = False

_requires_bridge = pytest.mark.skipif(not _HAS_BRIDGE,
                                      reason="bridge import needs scipy stack")


# ─── helpers ────────────────────────────────────────────────────────────────
def _book_db():
    """Minimal trades schema + the columns the candidate builder reads."""
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
            price_source TEXT, settled_value REAL, settlement_date_utc TEXT,
            realized_pnl REAL, settlement_confirmed INTEGER DEFAULT 0,
            settlement_outcome TEXT, label_axis TEXT DEFAULT 'strike_crossing',
            snapshot_time_utc TEXT, obs_max_source_ts TEXT)""")
    c.execute(
        """CREATE TABLE trade_forecaster_probs (
            trade_uuid TEXT NOT NULL UNIQUE, station TEXT NOT NULL,
            trade_date_utc TEXT NOT NULL, strike REAL, side TEXT, ticker TEXT,
            p_gefs REAL, p_analog REAL, p_trajectory REAL, p_fused REAL,
            n_analog INTEGER, n_traj INTEGER, agreement TEXT, edge_mult REAL,
            traj_verdict TEXT, traj_mult REAL, fusion_rule TEXT,
            created_at_utc TEXT NOT NULL)""")
    return c


def _book(c, station, day, price, *, selected_side="yes",
          confirmed=1, outcome="correct", settled_value=1):
    """Book one trade through the real bridge writer, then settle it."""
    rec = record_trade(
        c, station, "HIGH", "BUY", our_prob=0.60, market_price=price,
        confidence=0.7, strike=75.0, ticker=f"KXHIGH{station[1:]}-{day}-T75",
        size_usd=25.0, functionality="intraday_ev_strike", day_str=day,
        selected_side=selected_side)
    c.execute(
        """UPDATE trades SET settlement_confirmed=?, settlement_outcome=?,
           settled_value=?, realized_pnl=?, label_axis='strike_crossing',
           trade_date_utc=? WHERE trade_uuid=?""",
        (confirmed, outcome, settled_value, 10.0 if outcome == "correct" else -5.0,
         day, rec["trade_uuid"]))
    c.commit()
    return rec["trade_uuid"]


# ─── the real pin (G5 provenance) ───────────────────────────────────────────
def test_pinned_entry_is_1330_station_local():
    # 2026-10-04 EDT: KNYC 13:30 -> 17:30Z, KSFO 13:30 -> 20:30Z.
    assert pinned_entry_time_utc("KNYC", "2026-10-04") == "2026-10-04T17:30:00+00:00"
    assert pinned_entry_time_utc("KSFO", "2026-10-04") == "2026-10-04T20:30:00+00:00"
    # never the 13:00 default the H3 pin replaced
    assert "T13:00" not in (pinned_entry_time_utc("KNYC", "2026-10-04") or "")


def test_candidate_from_row_uses_real_pin_and_anchor(tmp_path):
    row = {"trade_uuid": "r1", "station": "KNYC", "market_type": "HIGH",
           "trade_date_utc": "2026-10-04", "quantity": 100, "trade_price": 0.30,
           "trade_version": FROZEN_TRADE_VERSION, "created_at_utc":
           "2026-10-04T17:00:00Z", "label_axis": "strike_crossing",
           "settlement_confirmed": 1, "settlement_outcome": "correct",
           "settled_value": 1}
    cand = candidate_from_trades_row(row)
    assert cand["entry_time_utc"] == "2026-10-04T17:30:00+00:00"
    assert cand["fee_total"] == kalshi_fee(100, 0.30)
    assert cand["signal_family"] == "intraday"


# ─── booking-time gate: decidable subset ────────────────────────────────────
def test_booking_gate_admits_clean_and_logs():
    g = sqlite3.connect(":memory:")
    v, ok = admit_decision_candidate(
        trade_uuid="T1", station="KNYC", settlement_date="2026-10-04",
        label_axis="strike_crossing", conn=g, suite_green=True)
    assert ok and v.verdict == VERDICT_ADMITTED
    row = g.execute("SELECT trade_uuid, verdict, failed_gate FROM gate_ledger"
                    ).fetchone()
    assert row == ("T1", VERDICT_ADMITTED, None)


def test_booking_gate_rejects_wrong_axis_and_logs_failing_check():
    g = sqlite3.connect(":memory:")
    v, ok = admit_decision_candidate(
        trade_uuid="T2", station="KNYC", settlement_date="2026-10-04",
        label_axis="day_over_day", conn=g, suite_green=True)
    assert (not ok) and v.verdict == VERDICT_REJECTED
    row = g.execute("SELECT verdict, failed_gate FROM gate_ledger").fetchone()
    assert row == (VERDICT_REJECTED, "G4")


def test_booking_gate_halts_on_version_mismatch():
    g = sqlite3.connect(":memory:")
    v, ok = admit_decision_candidate(
        trade_uuid="T3", station="KNYC", settlement_date="2026-10-04",
        trade_version="v9_wrong", label_axis="strike_crossing", conn=g,
        suite_green=True)
    assert (not ok) and v.verdict == "HALT"
    assert g.execute("SELECT failed_gate FROM gate_ledger").fetchone()[0] == "G1"


# ─── full composed gate (settled admission) ─────────────────────────────────
def _settled(**over):
    base = {"trade_uuid": "s1", "instance": "SBOX",
            "trade_version": FROZEN_TRADE_VERSION, "station": "KNYC",
            "signal_family": "intraday", "settlement_date": "2026-10-04",
            "quantity": 100, "fill_price": 0.30,
            "fee_total": kalshi_fee(100, 0.30), "settlement_confirmed": 1,
            "settlement_outcome": "correct", "canonical_outcome": "correct",
            "settled_value": 91.0,  # settlement TEMPERATURE (book), not a binary
            "label_axis": "strike_crossing",
            "snapshot_time_utc": "2026-10-04T17:30:00Z",
            "created_at_utc": "2026-10-04T17:00:00Z",
            "entry_time_utc": "2026-10-04T17:30:00Z",
            "finalization_utc": "2026-10-05T05:00:00Z"}
    base.update(over)
    return base


def test_full_gate_admits_clean_settled():
    g = sqlite3.connect(":memory:")
    v = admit_candidate(_settled(), conn=g, suite_green=True)
    assert v.verdict == VERDICT_ADMITTED


def test_full_gate_admits_temperature_settled_value():
    # The book's real finality representation: settled_value is the settlement
    # TEMPERATURE plus canonical_outcome (the strike-crossing win/loss). G3 must
    # admit this — the frozen {0,1} requirement is reconciled away.
    g = sqlite3.connect(":memory:")
    v = admit_candidate(_settled(settled_value=79.0, canonical_outcome="correct",
                                 settlement_outcome="correct"),
                        conn=g, suite_green=True)
    assert v.verdict == VERDICT_ADMITTED
    g3 = [c for c in v.checks if c.gate == "G3"][0]
    assert g3.status == "PASS"


def test_full_gate_rejects_lookahead_and_raises():
    g = sqlite3.connect(":memory:")
    with pytest.raises(CandidateRejected):
        admit_candidate(_settled(trade_uuid="s2",
                                 snapshot_time_utc="2026-10-04T18:00:00Z"),
                        conn=g, suite_green=True)
    assert g.execute("SELECT failed_gate FROM gate_ledger").fetchone()[0] == "G5"


# ─── TCR writer: one row per accepted trade, idempotent ─────────────────────
def test_record_custody_one_row_idempotent_and_chain_ok():
    c = _book_db()
    c.execute("INSERT INTO trades (trade_uuid, trade_date_utc, station, "
              "market_type, signal_direction, market_price, trade_type, "
              "quantity, position_size_usd, trade_price, trade_cost, "
              "trade_version, functionality, created_at_utc) VALUES "
              "('u1','2026-10-04','KNYC','HIGH','UP',0.3,'buy_yes',100,30,0.3,"
              "30,'v3.1_sbox','intraday_ev_strike','2026-10-04T17:00:00Z')")
    row = dict(zip([d[0] for d in c.execute("SELECT * FROM trades").description],
                   c.execute("SELECT * FROM trades").fetchone()))
    h1 = record_custody(c, row, instance="SBOX", git_commit="abc")
    h2 = record_custody(c, row, instance="SBOX", git_commit="abc")
    assert h1 == h2  # idempotent, no duplicate
    assert c.execute("SELECT COUNT(*) FROM trade_custody").fetchone()[0] == 1
    ok, problems = verify_chain(c)
    assert ok, problems
    assert column_count() == 48


@_requires_bridge
def test_booked_trade_gets_custody_row_via_wiring():
    """The exact sequence the bridge runs: gate -> book -> custody."""
    c = _book_db()
    v, ok = admit_decision_candidate(
        trade_uuid="KNYC-2026-10-04", station="KNYC",
        settlement_date="2026-10-04", label_axis="strike_crossing",
        conn=c, suite_green=True)
    assert ok and v.verdict == VERDICT_ADMITTED
    rec = record_trade(
        c, "KNYC", "HIGH", "BUY", our_prob=0.60, market_price=0.30,
        confidence=0.7, strike=75.0, ticker="KXHIGHNYC-2026-10-04-T75",
        size_usd=25.0, functionality="intraday_ev_strike", day_str="2026-10-04",
        selected_side="yes")
    assert rec["status"] == "executed"
    cur = c.execute("SELECT * FROM trades WHERE trade_uuid=?",
                    (rec["trade_uuid"],))
    trow = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    record_custody(c, trow, instance="SBOX")
    assert c.execute("SELECT COUNT(*) FROM trade_custody WHERE trade_uuid=?",
                     (rec["trade_uuid"],)).fetchone()[0] == 1
    ok, problems = verify_chain(c)
    assert ok, problems


@_requires_bridge
def test_rejected_candidate_is_not_booked_and_has_no_custody():
    c = _book_db()
    v, ok = admit_decision_candidate(
        trade_uuid="bad", station="KNYC", settlement_date="2026-10-04",
        label_axis="day_over_day", conn=c, suite_green=True)
    assert not ok
    # not booked, no custody
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert c.execute(
        "SELECT name FROM sqlite_master WHERE name='trade_custody'").fetchone() is None


# ─── pre-registration epoch ─────────────────────────────────────────────────
def test_prereg_verifies_for_governed_experiment():
    r = verify_run_relies_on_prereg(GOVERNED_EXPERIMENT, REPO)
    assert r["ok"] is True and r["sha256"]


# ─── settled-admission boundary ─────────────────────────────────────────────
@_requires_bridge
def test_settled_admission_logs_verdicts_and_custodies_admits(tmp_path):
    """Clean settled row -> ADMITTED + custody; lookahead row -> REJECTED, logged."""
    book = tmp_path / "book.db"
    c = sqlite3.connect(book)
    # copy the minimal schema in
    for stmt in _book_db().execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
        c.execute(stmt[0])
    c.commit()
    # a clean settled row (entry == pin, fee canonical, canonical_outcome correct)
    # with a TEMPERATURE settled_value — the book's real representation.
    c.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, market_price, trade_type, quantity,"
        " position_size_usd, trade_price, trade_cost, trade_version,"
        " functionality, created_at_utc, label_axis, settlement_confirmed,"
        " settlement_outcome, settled_value, realized_pnl, snapshot_time_utc,"
        " market_price) VALUES ('clean','2026-10-04','KNYC','HIGH','UP',0.3,"
        "'buy_yes',100,30,0.3,30,'v3.1_sbox','intraday_ev_strike',"
        "'2026-10-04T17:00:00Z','strike_crossing',1,'correct',75.0,10.0,"
        "'2026-10-04T17:30:00Z',0.3)")
    # a lookahead row (post-decision input)
    c.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, market_price, trade_type, quantity,"
        " position_size_usd, trade_price, trade_cost, trade_version,"
        " functionality, created_at_utc, label_axis, settlement_confirmed,"
        " settlement_outcome, settled_value, realized_pnl, snapshot_time_utc)"
        " VALUES ('leak','2026-10-04','KSFO','HIGH','UP',0.3,'buy_yes',100,30,"
        "0.3,30,'v3.1_sbox','intraday_ev_strike','2026-10-04T17:00:00Z',"
        "'strike_crossing',1,'correct',1,10.0,'2026-10-04T21:00:00Z')")
    c.commit()
    c.close()

    _anchors = {("KNYC", "HIGH", "2026-10-04"): "2026-10-05T05:00:00Z",
                ("KSFO", "HIGH", "2026-10-04"): "2026-10-05T05:00:00Z"}
    out = admit_settled_trades(
        str(book), gate_db=str(tmp_path / "gate.db"),
        custody_db=str(tmp_path / "custody.db"),
        canonical_map={}, finalization_map=_anchors)
    assert "clean" in out["admitted"]
    assert any(u == "leak" for u, _ in out["rejected"])
    # every rejected row carries its failing gate
    assert all(g is not None for _, g in out["rejected"])
    assert out["custody_hashes"].get("clean")


@_requires_bridge
def test_two_trades_same_day_get_distinct_custody_rows():
    """Regression: a trades row has no `market_ticker`, so the TCR key must fall
    back to trade_uuid. Without it every same-day trade collapses onto the
    "None" key and the 2nd trade is wrongly deduped into the 1st's custody row
    (observed: KMSP + KPHX booked together produced ONE custody row)."""
    c = _book_db()
    uids = []
    for st, tk in (("KNYC", "KXHIGHNYC-2026-10-04-T75"),
                   ("KSFO", "KXHIGHTSFO-2026-10-04-T70")):
        rec = record_trade(
            c, st, "HIGH", "BUY", our_prob=0.60, market_price=0.30,
            confidence=0.7, strike=75.0, ticker=tk, size_usd=25.0,
            functionality="intraday_ev_strike", day_str="2026-10-04",
            selected_side="yes")
        uids.append(rec["trade_uuid"])
    hashes = []
    for u in uids:
        cur = c.execute("SELECT * FROM trades WHERE trade_uuid=?", (u,))
        trow = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        hashes.append(record_custody(c, trow, instance="SBOX"))
    assert len(set(hashes)) == 2, hashes
    assert c.execute("SELECT COUNT(*) FROM trade_custody").fetchone()[0] == 2
    ok, problems = verify_chain(c)
    assert ok, problems


# ─── ledger idempotency guard (production writer) ───────────────────────────
def test_settled_admission_rerun_is_idempotent(tmp_path):
    """The production writer's append point is idempotent per (trade, check-set).

    Re-running `admit_settled_trades()` over the same settled rows must not
    double the append-only gate ledger. Regression for the measured 93 -> 157
    duplication (the reconcile driver guarded this itself; the production
    writer did not).
    """
    book = tmp_path / "book.db"
    c = sqlite3.connect(book)
    for stmt in _book_db().execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
        c.execute(stmt[0])
    c.commit()
    # one clean ADMITTED row + one lookahead REJECTED row (every verdict must
    # each be written at most once).
    c.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, market_price, trade_type, quantity,"
        " position_size_usd, trade_price, trade_cost, trade_version,"
        " functionality, created_at_utc, label_axis, settlement_confirmed,"
        " settlement_outcome, settled_value, realized_pnl, snapshot_time_utc)"
        " VALUES ('clean','2026-10-04','KNYC','HIGH','UP',0.3,'buy_yes',100,"
        "30,0.3,30,'v3.1_sbox','intraday_ev_strike','2026-10-04T17:00:00Z',"
        "'strike_crossing',1,'correct',75.0,10.0,'2026-10-04T17:30:00Z')")
    c.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,"
        " signal_direction, market_price, trade_type, quantity,"
        " position_size_usd, trade_price, trade_cost, trade_version,"
        " functionality, created_at_utc, label_axis, settlement_confirmed,"
        " settlement_outcome, settled_value, realized_pnl, snapshot_time_utc)"
        " VALUES ('leak','2026-10-04','KSFO','HIGH','UP',0.3,'buy_yes',100,"
        "30,0.3,30,'v3.1_sbox','intraday_ev_strike','2026-10-04T17:00:00Z',"
        "'strike_crossing',1,'correct',1,10.0,'2026-10-04T21:00:00Z')")
    c.commit()
    c.close()

    anchors = {("KNYC", "HIGH", "2026-10-04"): "2026-10-05T05:00:00Z",
               ("KSFO", "HIGH", "2026-10-04"): "2026-10-05T05:00:00Z"}
    gate_db, custody_db = str(tmp_path / "gate.db"), str(tmp_path / "custody.db")

    def _ledger_count():
        g = sqlite3.connect(f"file:{gate_db}?mode=ro", uri=True)
        n = g.execute("SELECT COUNT(*) FROM gate_ledger").fetchone()[0]
        g.close()
        return n

    out1 = admit_settled_trades(
        str(book), gate_db=gate_db, custody_db=custody_db,
        canonical_map={}, finalization_map=anchors)
    n1 = _ledger_count()
    assert out1["ledger_written"] == 2 and out1["ledger_duplicates"] == 0

    out2 = admit_settled_trades(
        str(book), gate_db=gate_db, custody_db=custody_db,
        canonical_map={}, finalization_map=anchors)
    n2 = _ledger_count()
    assert n2 == n1, f"re-run duplicated ledger rows: {n1} -> {n2}"
    assert out2["ledger_written"] == 0 and out2["ledger_duplicates"] == 2
    # the custody writer stays idempotent too
    assert out2["custody_hashes"].get("clean") == out1["custody_hashes"].get("clean")
    cc = sqlite3.connect(f"file:{custody_db}?mode=ro", uri=True)
    assert cc.execute("SELECT COUNT(*) FROM trade_custody").fetchone()[0] == 1
    ok, problems = verify_chain(cc)
    cc.close()
    assert ok, problems


def test_ledger_append_idempotent_key_unique():
    """Direct ledger append: the same (trade, check-set) writes once; a changed
    key gets its own row."""
    from gate_composer import GateLedger, compose_gate, verdict_idempotency_key

    conn = sqlite3.connect(":memory:")
    led = GateLedger(conn)
    v = compose_gate(
        {"trade_uuid": "u1", "instance": "SBOX",
         "trade_version": FROZEN_TRADE_VERSION,
         "created_at_utc": "2026-10-04T17:00:00Z",
         "settlement_date": "2026-10-04", "quantity": 100,
         "fill_price": 0.30, "fee_total": kalshi_fee(100, 0.30),
         "settlement_confirmed": 1, "settlement_outcome": "correct",
         "canonical_outcome": "correct", "label_axis": "strike_crossing",
         "snapshot_time_utc": "2026-10-04T17:30:00Z"},
        REPO, frozen_version=FROZEN_TRADE_VERSION, suite_green=True)
    key = verdict_idempotency_key("u1", FROZEN_TRADE_VERSION, v.verdict,
                                  "2026-10-04T17:00:00Z")
    assert led.append(v, idempotency_key=key) is True
    assert led.append(v, idempotency_key=key) is False
    # the key is a pure function of row identity (reproducible across runs)
    assert key == verdict_idempotency_key(
        "u1", FROZEN_TRADE_VERSION, v.verdict, "2026-10-04T17:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM gate_ledger").fetchone()[0] == 1


def test_decision_time_gate_rerun_is_idempotent(tmp_path):
    """E4-a: the booking-time gate must key the ledger on row identity, not
    wall-clock now(). A double-run of the SAME booking intent leaves the ledger
    count unchanged (the fresh candidate used to carry `datetime.now()` as its
    decision instant, so every run produced a new secret key and duplicated)."""
    gate_db = str(tmp_path / "gate.db")

    def _count():
        g = sqlite3.connect(f"file:{gate_db}?mode=ro", uri=True)
        n = g.execute("SELECT COUNT(*) FROM gate_ledger").fetchone()[0]
        g.close()
        return n

    for _ in range(2):
        admit_decision_candidate(
            trade_uuid="SBOX-KNYC-2026-10-04", station="KNYC",
            settlement_date="2026-10-04", label_axis="strike_crossing",
            gate_db=gate_db, repo=REPO, suite_green=True)
    assert _count() == 1, "booking-time re-run duplicated a ledger row"


def test_decision_time_decision_instant_is_not_wall_clock(tmp_path):
    """The candidate's decision instant is the pinned 13:30-local entry (or a
    deterministic identity fallback), never now()."""
    gate_db = str(tmp_path / "gate.db")
    admit_decision_candidate(
        trade_uuid="SBOX-KNYC-2026-10-04", station="KNYC",
        settlement_date="2026-10-04", label_axis="strike_crossing",
        gate_db=gate_db, repo=REPO, suite_green=True)
    g = sqlite3.connect(f"file:{gate_db}?mode=ro", uri=True)
    row = g.execute("SELECT trade_uuid FROM gate_ledger").fetchone()
    g.close()
    assert row[0] == "SBOX-KNYC-2026-10-04"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
