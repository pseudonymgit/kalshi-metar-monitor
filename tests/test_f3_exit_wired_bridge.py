#!/usr/bin/env python3
"""F3 wired (verification, 2026-10-13) — the exit pass must reach REAL holdings.

`tests/test_sbox_exit_pass.py` exercises the pass against a throwaway DB. This
file pins the WIRING contract the register flagged as missing: the pass loads
`status='open'` holdings (not this cycle's entries), prices them from the
snapshot DB, calls `evaluate_exits`, and refuses to fabricate a mid-based PnL
for an already-settled row.

Run: python3 -m pytest tests/test_f3_exit_wired_bridge.py -v
"""
import importlib.util
import inspect
import os
import sqlite3
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_exit_pass_calls_evaluate_exits():
    b = _load_bridge()
    assert b.evaluate_exits is not None, "exit_logic.evaluate_exits must be imported"
    src = inspect.getsource(b.run_exit_pass)
    assert "evaluate_exits(" in src, "the pass must actually call evaluate_exits"
    assert "status='open'" in inspect.getsource(b.open_exit_positions), \
        "holdings must be loaded from status='open' rows"


def test_open_exit_positions_shape():
    b = _load_bridge()
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE trades (trade_uuid TEXT, station TEXT, trade_date_utc TEXT,
        signal_direction TEXT, market_price REAL, quantity INTEGER, position_size_usd REAL,
        created_at_utc TEXT, market_ticker TEXT, notes TEXT, status TEXT)""")
    c.execute("""INSERT INTO trades VALUES
        ('u1','KNYC','2026-10-13','UP',0.42,12,5.0,'2026-10-13T18:15:00+00:00',
         'KXHIGHNY-26OCT13-T80','note','open')""")
    pos = b.open_exit_positions(c)[0]
    c.close()
    assert pos["trade_uuid"] == "u1"
    assert pos["direction"] == "UP"
    assert pos["entry_price"] == 0.42
    assert pos["ticker"] == "KXHIGHNY-26OCT13-T80"
    assert pos["entry_hour"] == 18


def test_already_settled_row_is_not_closed(monkeypatch, tmp_path):
    """hours-before-settlement < 0 -> reconciliation, never a fabricated mid PnL.

    Uses a real temp-file DB (the repo's db_write_sandbox fixture rejects
    opening a production DB for writing) so run_exit_pass's open/commit path is
    exercised end to end.
    """
    b = _load_bridge()
    sbox = tmp_path / "sbox.db"
    c = sqlite3.connect(sbox)
    c.execute("""CREATE TABLE trades (trade_uuid TEXT, station TEXT, trade_date_utc TEXT,
        signal_direction TEXT, market_price REAL, quantity INTEGER, position_size_usd REAL,
        created_at_utc TEXT, market_ticker TEXT, notes TEXT, status TEXT, realized_pnl REAL)""")
    c.execute("""INSERT INTO trades VALUES
        ('u1','KNYC','2026-10-12','UP',0.42,12,5.0,'2026-10-12T18:15:00+00:00',
         'KXHIGHNY-26OCT12-T80','note','open',NULL)""")
    c.commit()
    c.close()
    monkeypatch.setattr(b, "SBOX_DB", sbox)
    monkeypatch.setattr(b, "_hours_before_settlement", lambda *a, **k: -3.0)
    monkeypatch.setattr(b, "_exit_live_mid", lambda *a, **k: 0.90)
    out = b.run_exit_pass(dry_run=False)
    assert out["n_closed"] == 0
    assert out["skipped"][0][1] == "already_settled_pending_reconciliation"
    check = sqlite3.connect(sbox)
    assert check.execute("SELECT status FROM trades").fetchone()[0] == "open"
    check.close()


def test_default_rules_safest_first():
    b = _load_bridge()
    assert b._enabled_exit_rules() == {"auto_close_h1"}, \
        "only h-1 auto-close is on by default; the rest are opt-in flags"
