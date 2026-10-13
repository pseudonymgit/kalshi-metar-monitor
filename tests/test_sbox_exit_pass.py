#!/usr/bin/env python3
"""F3 (2026-10-13) — production exit pass over real open SBOX holdings.

Before F3 `core/intraday/exit_logic.evaluate_exits` had no production caller:
intraday_runner built `open_positions` from this cycle's entries only, so no
exit rule ever saw a real holding. These tests exercise the new pass against a
throwaway SBOX-shaped DB (no repo DB is touched):

  * an open holding inside the h-1 window is closed and records exit P&L;
  * outside the window with only h-1 enabled, nothing closes;
  * an already-settled row (hours-before-settlement < 0) is NOT closed at a mid
    (no fabricated P&L) — it is flagged for settlement reconciliation;
  * take-profit/stop-loss stay off unless their env flag is set.

Run: python3 -m pytest tests/test_sbox_exit_pass.py -v
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.station_time import settlement_time_utc  # noqa: E402


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    mod = _load_bridge()
    sbox = tmp_path / "sbox.db"
    snaps = tmp_path / "snaps.db"
    _make_sbox(sbox)
    _make_snaps(snaps)
    monkeypatch.setattr(mod, "SBOX_DB", sbox)
    monkeypatch.setattr(mod, "SNAPS", snaps)
    for flag in ("WE_SBOX_EXIT_TAKE_PROFIT", "WE_SBOX_EXIT_STOP_LOSS",
                 "WE_SBOX_EXIT_SIGNAL_FLIP"):
        monkeypatch.delenv(flag, raising=False)
    return mod


def _make_sbox(path):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE trades (
        trade_uuid TEXT PRIMARY KEY, station TEXT, trade_date_utc TEXT,
        signal_direction TEXT, market_price REAL, quantity INTEGER,
        position_size_usd REAL, created_at_utc TEXT, market_ticker TEXT,
        notes TEXT, status TEXT DEFAULT 'open', realized_pnl REAL)""")
    c.execute("""INSERT INTO trades (trade_uuid, station, trade_date_utc,
        signal_direction, market_price, quantity, position_size_usd,
        created_at_utc, market_ticker, status)
        VALUES ('T1','KNYC','2026-10-13','UP',0.50,10,5.0,
                '2026-10-13T12:00:00+00:00','KXHIGHNY-26OCT13-T80','open')""")
    c.commit()
    c.close()


def _make_snaps(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE market_snapshots (market_ticker TEXT, mid_price REAL, snapshot_time TEXT)")
    c.execute("INSERT INTO market_snapshots VALUES ('KXHIGHNY-26OCT13-T80', 0.92, '2026-10-13T00:00:00+00:00')")
    c.commit()
    c.close()


def test_exit_pass_closes_holding_in_h1_window(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_hours_before_settlement", lambda *a, **k: 0.5)
    out = bridge.run_exit_pass(dry_run=False)
    assert out["n_closed"] == 1
    assert out["closed"][0]["reason"] == "auto_close_h1"
    assert out["closed"][0]["station"] == "KNYC"
    # realized_pnl recorded = size * (mid 0.92 - entry 0.50) = 10 * 0.42
    assert abs(out["closed"][0]["pnl"] - 4.2) < 1e-6
    c = sqlite3.connect(bridge.SBOX_DB)
    row = c.execute("SELECT status, realized_pnl FROM trades WHERE trade_uuid='T1'").fetchone()
    c.close()
    assert row[0] == "closed"
    assert abs(row[1] - 4.2) < 1e-6


def test_exit_pass_dry_run_writes_nothing(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_hours_before_settlement", lambda *a, **k: 0.5)
    out = bridge.run_exit_pass(dry_run=True)
    assert out["n_closed"] == 1
    c = sqlite3.connect(bridge.SBOX_DB)
    status = c.execute("SELECT status FROM trades WHERE trade_uuid='T1'").fetchone()[0]
    c.close()
    assert status == "open", "dry-run must not mutate the book"


def test_exit_pass_no_close_outside_h1_window(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_hours_before_settlement", lambda *a, **k: 5.0)
    out = bridge.run_exit_pass(dry_run=True)
    assert out["n_closed"] == 0
    assert out["skipped"][0][1] == "no_exit_rule_triggered"


def test_exit_pass_refuses_already_settled_row(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_hours_before_settlement", lambda *a, **k: -3.0)
    out = bridge.run_exit_pass(dry_run=False)
    assert out["n_closed"] == 0
    assert out["skipped"][0][1] == "already_settled_pending_reconciliation"
    c = sqlite3.connect(bridge.SBOX_DB)
    status = c.execute("SELECT status FROM trades WHERE trade_uuid='T1'").fetchone()[0]
    c.close()
    assert status == "open", "settled rows must not be closed at a fabricated mid"


def test_take_profit_opt_in(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_hours_before_settlement", lambda *a, **k: 5.0)
    monkeypatch.setenv("WE_SBOX_EXIT_TAKE_PROFIT", "1")
    out = bridge.run_exit_pass(dry_run=True)
    assert out["n_closed"] == 1
    assert out["closed"][0]["reason"] == "take_profit"


def test_default_rules_only_h1(bridge):
    assert bridge._enabled_exit_rules() == {"auto_close_h1"}


def test_enabled_rules_respects_flags(bridge, monkeypatch):
    monkeypatch.setenv("WE_SBOX_EXIT_STOP_LOSS", "1")
    assert bridge._enabled_exit_rules() == {"auto_close_h1", "stop_loss"}


def test_hours_before_settlement_uses_station_tz():
    from datetime import datetime, timezone
    settle = settlement_time_utc("KNYC", "2026-10-13")
    assert settle is not None
    # Midnight local (EDT = UTC-4) next day -> 04:00Z on 2026-10-14.
    assert settle == datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
    mod = _load_bridge()
    hb = mod._hours_before_settlement(
        "KNYC", "2026-10-13", datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc))
    assert abs(hb - 2.0) < 1e-9
