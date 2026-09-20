#!/usr/bin/env python3
"""
Independent QC — R/T-4a NO-side entry rebuild verification.

This is a SEPARATE pass from the build. It does NOT re-derive the build logic; it
inspects the built artifact (`scripts/intraday_entry_stub.py`) by:
  1. Static symbol/schema inspection (does the source actually contain side/bucket/
     trade_type tokens, and does the paper_trades schema carry side/bucket fields?).
  2. A live NO-side entry execution against a REAL `between` rung from
     kalshi_strike_ladder.db (authoritative `result='no'` → NO wins), verifying the
     trade is written with side=NO, trade_type=buy_no, strike_type=between, floor/cap,
     and that `_close_position` persists a non-NULL PnL equal to the realized PnL.

Deterministic, read-only against source + a scratch in-memory/paper DB. No AI in loop.
No data mutation of the production DBs (uses a temp copy of paper_trading.db).
"""

import importlib.util
import inspect
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUB = REPO / 'scripts' / 'intraday_entry_stub.py'
LADDER_DB = REPO / 'data' / 'kalshi_strike_ladder.db'
PAPER_DB = REPO / 'data' / 'paper_trading.db'

# ---------------------------------------------------------------------------
# Load the stub module under test (without executing main()).
# ---------------------------------------------------------------------------
def load_stub():
    spec = importlib.util.spec_from_file_location('intraday_entry_stub_qc', STUB)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['intraday_entry_stub_qc'] = mod
    spec.loader.exec_module(mod)
    return mod


def static_inspection(mod):
    """Verify required symbols/tokens exist in the built source."""
    src = STUB.read_text()
    findings = {}

    # (1) side model tokens
    for tok in ('buy_no', 'sell_no', 'buy_yes', 'sell_yes', 'SIDE_NO', 'SIDE_YES',
                'trade_type', 'side'):
        findings[f'token:{tok}'] = tok in src

    # (2) bucket/strike-ladder tokens
    for tok in ('strike_type', 'strike_value', 'floor_strike', 'cap_strike',
                'between', 'STRIKE_LADDER_DB', '_read_strike_ladder', 'adjacent_bucket'):
        findings[f'token:{tok}'] = tok in src

    # (3) PnL persistence — _close_position must reference realized_pnl and persist
    close_fn = getattr(mod, '_close_position', None)
    findings['fn:_close_position'] = close_fn is not None and callable(close_fn)
    close_src = inspect.getsource(close_fn) if close_fn else ''
    findings['close:realized_pnl'] = 'realized_pnl(' in close_src
    findings['close:no_NULL'] = 'pnl = NULL' not in close_src and 'NULL' not in close_src.split('pnl')[1] if close_src else False

    return findings


def schema_inspection(tmp_db: Path):
    """Verify the paper_trades schema carries side/bucket/settle columns after a write."""
    mod = load_stub()
    # Trigger the migration by writing a trade to a temp DB copy.
    import shutil
    if PAPER_DB.exists():
        shutil.copy(PAPER_DB, tmp_db)
    ok = mod._write_paper_trade(
        station='KATL', date_str='2026-07-19', hour_utc=12, hour_before=6,
        direction='SELL', contracts=1, entry_price=0.80, our_prob=0.90,
        market_price=0.80, edge_pp=10.0, side='NO', trade_type='buy_no',
        strike_type='between', strike_value=96.5, floor_strike=96.0, cap_strike=97.0,
        db_path=tmp_db,
    )
    con = sqlite3.connect(str(tmp_db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(paper_trades)")}
    con.close()
    return {
        'write_ok': ok,
        'col:side': 'side' in cols,
        'col:trade_type': 'trade_type' in cols,
        'col:strike_type': 'strike_type' in cols,
        'col:strike_value': 'strike_value' in cols,
        'col:floor_strike': 'floor_strike' in cols,
        'col:cap_strike': 'cap_strike' in cols,
        'col:settle_value': 'settle_value' in cols,
    }


def no_side_live_verification(mod, tmp_db: Path):
    """
    Verify a REAL NO-side trade end-to-end against a real `between` rung.

    Steps:
      1. Pick a real between rung with result='no' from kalshi_strike_ladder.db.
      2. Build the NO-side entry spec (our model says YES prob low -> NO prob high).
      3. Execute buy_no via execute_no_side_entries.
      4. Confirm the written row: side=NO, trade_type=buy_no, strike_type=between,
         floor/cap match the real rung.
      5. Close via _close_position with the authoritative settle_value (1.0 since
         result='no' means the NO side won), and assert pnl is non-NULL and equals
         realized_pnl(buy_no, entry, 1.0, contracts).
    """
    con = sqlite3.connect(f'file:{LADDER_DB}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    rung = con.execute(
        "SELECT * FROM strike_ladder WHERE strike_type='between' AND result='no' "
        "LIMIT 1").fetchone()
    con.close()
    assert rung is not None, 'no real between/no rung found'

    bucket = dict(rung)
    # Our model predicts YES is unlikely (low yes prob) -> NO is favored.
    our_yes_prob = 0.10
    market_yes_price = 0.20  # market yes price; NO market = 0.80

    spec = {
        'station': bucket['station'],
        'bucket': bucket,
        'our_yes_prob': our_yes_prob,
        'market_yes_price': market_yes_price,
        'hour_before': 6,
    }
    date_str = bucket['local_trading_date']

    res = mod.execute_no_side_entries([spec], hour_utc=12, date_str=date_str, db_path=tmp_db)
    assert res['executed_trades'], f'no trade executed: {res}'
    trade = res['executed_trades'][0]

    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM paper_trades WHERE trade_type='buy_no' AND station=? "
        "ORDER BY created_at_utc DESC LIMIT 1", (bucket['station'],)).fetchone()
    assert row is not None, 'no buy_no row written'
    d = dict(row)
    con.close()

    # Assert provenance matches the real rung.
    checks = {
        'side': d.get('side') == 'NO',
        'trade_type': d.get('trade_type') == 'buy_no',
        'strike_type': d.get('strike_type') == 'between',
        'floor_strike': abs((d.get('floor_strike') or 0) - (bucket['floor_strike'] or 0)) < 1e-6,
        'cap_strike': abs((d.get('cap_strike') or 0) - (bucket['cap_strike'] or 0)) < 1e-6,
        'entry_is_no_price': abs(d.get('entry_price') - 0.80) < 1e-6,
    }

    # Close with authoritative settle_value=1.0 (NO won because result='no').
    settle_value = 1.0  # NO side wins when result == 'no'
    closed = mod._close_position(d['id'], 'closed_settled', settle_value, db_path=tmp_db)
    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    row2 = con.execute("SELECT pnl, settle_value, status FROM paper_trades WHERE id=?",
                       (d['id'],)).fetchone()
    con.close()

    expected_pnl = mod.realized_pnl('buy_no', d['entry_price'], 1.0, d['contracts'])
    checks['closed'] = closed
    checks['pnl_non_null'] = row2['pnl'] is not None
    checks['pnl_correct'] = abs((row2['pnl'] or 0) - expected_pnl) < 1e-6
    checks['status_settled'] = row2['status'] == 'closed_settled'
    checks['settle_value_persisted'] = abs((row2['settle_value'] or 0) - 1.0) < 1e-6

    return {
        'rung': {'station': bucket['station'], 'date': date_str,
                 'strike_type': bucket['strike_type'],
                 'floor': bucket['floor_strike'], 'cap': bucket['cap_strike'],
                 'result': bucket['result'],
                 'settlement_value_dollars': bucket['settlement_value_dollars']},
        'checks': checks,
        'expected_pnl': expected_pnl,
        'actual_pnl': row2['pnl'],
    }


def main():
    mod = load_stub()

    print('=' * 70)
    print('INDEPENDENT QC — R/T-4a NO-side entry rebuild')
    print('=' * 70)

    print('\n[1] Static symbol inspection')
    static = static_inspection(mod)
    for k, v in static.items():
        print(f'    {k}: {v}')
    static_pass = all(static.values())

    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / 'paper_trading.db'

        print('\n[2] Schema inspection (side/bucket columns)')
        schema = schema_inspection(tmp_db)
        for k, v in schema.items():
            print(f'    {k}: {v}')
        schema_pass = all(v for k, v in schema.items() if k != 'write_ok')

        print('\n[3] Live NO-side verification against a real `between` rung')
        live = no_side_live_verification(mod, tmp_db)
        print(f"    rung: {live['rung']}")
        for k, v in live['checks'].items():
            print(f'    {k}: {v}')
        live_pass = all(live['checks'].values())
        print(f"    expected_pnl={live['expected_pnl']} actual_pnl={live['actual_pnl']}")

    print('\n' + '=' * 70)
    overall = static_pass and schema_pass and live_pass
    print(f'STATIC PASS: {static_pass}')
    print(f'SCHEMA PASS: {schema_pass}')
    print(f'LIVE NO-SIDE PASS: {live_pass}')
    print(f'OVERALL VERDICT: {"PASS" if overall else "FAIL"}')
    print('=' * 70)
    return 0 if overall else 1


if __name__ == '__main__':
    sys.exit(main())
