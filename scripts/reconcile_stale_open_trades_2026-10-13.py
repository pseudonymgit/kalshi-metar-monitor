#!/usr/bin/env python3
"""
reconcile_stale_open_trades_2026-10-13.py — B6 stale-open reconcile (READ-ONLY).

The exit pass found 14 open trades in data/paper_trading_sbox.db whose
settlement date has passed (hours_to_settlement < 0) but which were never
closed: they are stale, entered-but-unreconciled rows.

This tool does NOT write to the trades DB. It:
  1. reads the open rows,
  2. joins the canonical settlement truth
     (data/kalshi_settlements.db  live_api/finalized  == canonical)
     and the provisional source (data/ghcn_settlements.db),
  3. computes the canonical outcome + settlement P&L on the STRICT
     strike-crossing axis (single source: core.canonical_settlement),
  4. EMITS the reconciliation UPDATE it *would* run (prepared, not executed),
  5. writes a JSON artifact for the report.

Settlement convention (from core/pnl_tracking.py, verified against the 68
already-closed rows):
    HIGH: S = 1 iff settlement_temp > strike (strict; equality == not-ITM)
    buy_yes: profit = (S - filled_price) * qty ;  return_amount = S * qty
    buy_no : profit = (filled_price - S) * qty ;  return_amount = (1-S) * qty
    settled_value      = 1.0 if S else 0.0      (contract value, NOT the temp)
    canonical_settled_f / settlement_outcome = canonical temp / 'correct'|'incorrect'
    status = 'closed', settlement_confirmed = 1

Usage:
    .venv-gefs/bin/python scripts/reconcile_stale_open_trades_2026-10-13.py
    .venv-gefs/bin/python scripts/reconcile_stale_open_trades_2026-10-13.py --emit-sql
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SBOX = REPO / "data" / "paper_trading_sbox.db"
KALSHI = REPO / "data" / "kalshi_settlements.db"
GHCN = REPO / "data" / "ghcn_settlements.db"
OUT_DIR = REPO / "artifacts" / "stale-open-reconcile"
OUT_JSON = OUT_DIR / "stale_open_reconcile_2026-10-13.json"

from core.canonical_settlement import canonical_outcome  # noqa: E402


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def strike_from_notes(notes):
    if not notes:
        return None
    m = re.search(r"@\s*(-?\d+(?:\.\d+)?)F", notes)
    return float(m.group(1)) if m else None


def settle_yes(market_type, temp, strike):
    if market_type == "HIGH":
        return 1 if temp > strike else 0
    return 1 if temp < strike else 0


def pnl(trade_type, S, price, qty):
    if trade_type == "buy_yes":
        return (S - price) * qty, S * qty
    if trade_type == "buy_no":
        return (price - S) * qty, (1 - S) * qty
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true")
    a = ap.parse_args()

    p, k, g = ro(SBOX), ro(KALSHI), ro(GHCN)
    p.row_factory = sqlite3.Row
    # Per-station settlement timestamp (UTC) convention, learned from the 68
    # already-closed rows so the prepared UPDATE matches the engine's shape.
    station_sdt = {}
    for st, sdt in p.execute("SELECT DISTINCT station, settlement_date_utc FROM trades "
                             "WHERE status='closed' AND settlement_date_utc IS NOT NULL "
                             "AND settlement_date_utc != ''"):
        station_sdt.setdefault(st, sdt[11:19])  # 'HH:MM:SS'
    open_rows = [dict(r) for r in p.execute(
        """SELECT id, trade_uuid, station, market_ticker, market_type, signal_direction,
                  trade_type, quantity, trade_price, trade_date_utc, created_at_utc,
                  notes, status
             FROM trades WHERE status='open' ORDER BY created_at_utc""")]

    report = {"generated": datetime.now(timezone.utc).isoformat(),
              "source_db": "data/paper_trading_sbox.db (read-only)",
              "n_open": len(open_rows),
              "canonical_sources": ["kalshi_settlements(source='live_api'/finalized)"],
              "provisional_sources": ["ghcn_settlements(high_f, source='ghcn_daily')"],
              "policy": "Settlement P&L per core/pnl_tracking.py; canonical write only from live_api/finalized.",
              "rows": [], "summary": {}}

    n_auto, n_manual = 0, 0
    for r in open_rows:
        strike = strike_from_notes(r["notes"])
        krow = k.execute("SELECT kalshi_temp, source, source_type FROM kalshi_settlements "
                         "WHERE station=? AND target_date=?",
                         (r["station"], r["trade_date_utc"])).fetchone()
        grow = g.execute("SELECT high_f FROM ghcn_settlements WHERE station=? AND date=?",
                         (r["station"], r["trade_date_utc"])).fetchone()
        kalshi = krow[0] if krow else None
        ksrc = f"{krow[1]}/{krow[2]}" if krow else None
        ghcn = grow[0] if grow else None

        rec = {"id": r["id"], "trade_uuid": r["trade_uuid"], "station": r["station"],
               "market_ticker": r["market_ticker"], "market_type": r["market_type"],
               "signal_direction": r["signal_direction"], "trade_type": r["trade_type"],
               "quantity": r["quantity"], "trade_price": r["trade_price"],
               "trade_date_utc": r["trade_date_utc"], "strike_f": strike,
               "kalshi_temp_f": kalshi, "kalshi_source": ksrc, "ghcn_high_f": ghcn}

        if kalshi is None or strike is None:
            rec["disposition"] = "MANUAL"
            rec["reason"] = ("no canonical kalshi settlement for station-day" if kalshi is None
                             else "strike not parseable from notes")
            n_manual += 1
            report["rows"].append(rec)
            continue

        S = settle_yes(r["market_type"], float(kalshi), strike)
        cout = canonical_outcome(r["signal_direction"], r["market_type"], float(kalshi), strike)
        profit, ret = pnl(r["trade_type"], S, float(r["trade_price"]), int(r["quantity"]))
        rec["settle_yes"] = S
        rec["canonical_outcome"] = cout
        rec["realized_pnl"] = round(profit, 4) if profit is not None else None
        rec["settlement_return_amount"] = round(ret, 4) if ret is not None else None
        rec["settled_value"] = 1.0 if S else 0.0
        hhmmss = station_sdt.get(r["station"], "05:00:00")
        rec["settlement_date_utc"] = f"{r['trade_date_utc']}T{hhmmss}Z"
        rec["settlement_date_source"] = ("closed-row convention" if r["station"] in station_sdt
                                         else "default 05:00Z (unverified)")
        rec["disposition"] = "AUTO"
        rec["reason"] = f"canonical kalshi temp {kalshi}F vs strike {strike}F"
        # source-disagreement flag (outcome flips between canonical and provisional)
        if ghcn is not None:
            S_g = settle_yes(r["market_type"], float(ghcn), strike)
            rec["ghcn_settle_yes"] = S_g
            rec["source_disagreement"] = (S_g != S)
        n_auto += 1
        report["rows"].append(rec)

    report["summary"] = {"auto_reconcilable": n_auto, "manual_review": n_manual,
                         "correct": sum(1 for x in report["rows"]
                                        if x.get("canonical_outcome") == "correct"),
                         "incorrect": sum(1 for x in report["rows"]
                                          if x.get("canonical_outcome") == "incorrect")}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    for x in report["rows"]:
        print(f"  id={x['id']} {x['station']} {x['market_ticker']} "
              f"kalshi={x.get('kalshi_temp_f')} strike={x.get('strike_f')} "
              f"-> {x['disposition']} outcome={x.get('canonical_outcome')} "
              f"pnl={x.get('realized_pnl')}"
              + (" [SRC-DISAGREE]" if x.get("source_disagreement") else ""))

    if a.emit_sql:
        print("\n-- PREPARED reconciliation (NOT executed):")
        for x in report["rows"]:
            if x["disposition"] != "AUTO":
                continue
            print(
                f"UPDATE trades SET settled_value={x['settled_value']}, "
                f"settlement_date_utc='{x['settlement_date_utc']}', "
                f"realized_pnl={x['realized_pnl']}, "
                f"settlement_return_amount={x['settlement_return_amount']}, "
                f"settlement_confirmed=1, settlement_outcome='{x['canonical_outcome']}', "
                f"canonical_settled_f={x['kalshi_temp_f']}, "
                f"canonical_source='kalshi_live_api', "
                f"canonical_outcome='{x['canonical_outcome']}', status='closed' "
                f"WHERE id={x['id']} AND status='open';")
        print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
