#!/usr/bin/env python3
"""fix_trades_table_integrity_2026-10-14.py — one-time trades-table integrity
sweep for data/paper_trading_sbox.db (Dan, 2026-10-14: "get every row honest").

Three independent problems, three separate apply modes (each its own commit):

  --problem 1  Settle the stale-open rows that have canonical truth.
               Settlement axis is the repo's SHIPPED rule: STRICT `>`
               (core/settlement_labels.strike_crossing_yes; canonical_settle_yes;
               pnl_tracking.py:129; tests assert equality == LOSS).
               Exact-tie rows are BLOCKED (rule is Dan-gated; see --block-ties).
               Writes: settled_value, realized_pnl (constant-1-contract basis),
               settlement_return_amount, settlement_confirmed=1,
               settlement_outcome, canonical_settled_f,
               canonical_source='kalshi_live_api', canonical_outcome,
               status='closed', settlement_date_utc
               (core/station_time.settlement_time_utc).
  --problem 2  Backfill position_size_usd for all rows from KELLY sizing
               (AF 2026-10-14: kelly_position_size(our_prob, trade_price),
               $200 bankroll / 5% cap / $10 max) on the BOOKED side's calibrated
               probability. Preserves the old size in position_size_usd_old_2026.
  --problem 3  Replace realized_pnl (size fiction) with honest
               constant-1-contract P&L: buy_yes (S-price)*1, buy_no (price-S)*1,
               S from the STRICT axis on canonical_settled_f vs strike.
               Preserves the old P&L in realized_pnl_old_2026. Also caps
               quantity to floor(position_size_usd / trade_price) where sane.

Safety: BACK UP FIRST (the caller does cp). --dry-run prints the plan, writes
nothing. Never touches any table other than `trades` (plus additive columns).
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

from core.station_time import settlement_time_utc  # noqa: E402
from scripts.sbox_intraday_ev_bridge import kelly_position_size  # noqa: E402

BOOK = REPO / "data" / "paper_trading_sbox.db"
KALSHI = REPO / "data" / "kalshi_settlements.db"
OUT = REPO / "artifacts" / "trades-integrity"
CANONICAL_SOURCE = "kalshi_live_api"


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def strike_from_notes(notes):
    if not notes:
        return None
    m = re.search(r"@\s*(-?\d+(?:\.\d+)?)F", notes)
    return float(m.group(1)) if m else None


def settle_yes(market_type, temp, strike):
    """Repo SHIPPED axis: STRICT `>` (equality == not-ITM)."""
    if market_type == "HIGH":
        return 1 if temp > strike else 0
    return 1 if temp < strike else 0


def canonical_outcome(signal_direction, market_type, temp, strike):
    itm = settle_yes(market_type, temp, strike) == 1
    if (market_type or "HIGH").upper() == "HIGH":
        actual = "UP" if itm else "DOWN"
    else:
        actual = "DOWN" if itm else "UP"
    return "correct" if str(signal_direction).upper() == actual else "incorrect"


def honest_pnl(trade_type, S, price):
    """Constant-1-contract P&L."""
    if trade_type == "buy_yes":
        return (S - price) * 1.0
    if trade_type == "buy_no":
        return (price - S) * 1.0
    return None


def ensure_col(conn, table, col, decl):
    cols = {d[1] for d in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.commit()


# ── Problem 1 ────────────────────────────────────────────────────────────────
def problem1(conn, dry_run=False):
    p = ro(BOOK)
    p.row_factory = sqlite3.Row
    k = ro(KALSHI)
    open_rows = [dict(r) for r in p.execute(
        """SELECT id, trade_uuid, station, market_type, signal_direction,
                  trade_type, quantity, trade_price, trade_date_utc, notes, status
             FROM trades WHERE status='open' ORDER BY id""")]
    out = {"settled": [], "blocked_tie": [], "blocked_no_truth": []}
    for r in open_rows:
        strike = strike_from_notes(r["notes"])
        if strike is None:
            out["blocked_no_truth"].append({**r, "reason": "no_strike"})
            continue
        krow = k.execute(
            "SELECT kalshi_temp, source, source_type FROM kalshi_settlements "
            "WHERE station=? AND target_date=? ORDER BY id DESC LIMIT 1",
            (r["station"], r["trade_date_utc"])).fetchone()
        if not krow or krow[0] is None:
            out["blocked_no_truth"].append({**r, "reason": "no_canonical_truth"})
            continue
        temp, src, stype = float(krow[0]), krow[1], krow[2]
        tie = abs(temp - strike) < 1e-9
        if tie:
            out["blocked_tie"].append(
                {**r, "strike": strike, "temp": temp, "source": f"{src}/{stype}"})
            continue
        S = settle_yes(r["market_type"], temp, strike)
        cout = canonical_outcome(r["signal_direction"], r["market_type"], temp, strike)
        pnl = honest_pnl(r["trade_type"], S, r["trade_price"])
        ret = (S if r["trade_type"] == "buy_yes" else (1 - S)) * 1.0
        sdt = settlement_time_utc(r["station"], r["trade_date_utc"])
        sdt_iso = sdt.astimezone(timezone.utc).isoformat() if sdt else None
        rec = {**r, "strike": strike, "temp": temp,
               "source": f"{src}/{stype}", "settle_yes": S,
               "canonical_outcome": cout, "realized_pnl": pnl,
               "settlement_return_amount": ret, "settled_value": float(S),
               "settlement_date_utc": sdt_iso}
        if not dry_run:
            conn.execute(
                """UPDATE trades SET settled_value=?, settlement_date_utc=?,
                       realized_pnl=?, settlement_return_amount=?,
                       settlement_confirmed=1, settlement_outcome=?,
                       canonical_settled_f=?, canonical_source=?,
                       canonical_outcome=?, status='closed'
                   WHERE id=? AND status='open'""",
                (float(S), sdt_iso, pnl, ret, cout, temp, CANONICAL_SOURCE,
                 cout, r["id"]))
        out["settled"].append(rec)
    if not dry_run:
        conn.commit()
    return out


# ── Problem 2 ────────────────────────────────────────────────────────────────
def problem2(conn, dry_run=False):
    ensure_col(conn, "trades", "position_size_usd_old_2026", "REAL")
    c = conn
    # Idempotency guard: only capture the ORIGINAL size the first time. On a
    # re-run (e.g. after the ladder changed), keep the preserved original
    # rather than overwriting it with an already-backfilled value.
    existing_old = {r[0] for r in c.execute(
        "SELECT id FROM trades WHERE position_size_usd_old_2026 IS NOT NULL")}
    calib = {r[0]: r[1] for r in c.execute(
        "SELECT trade_uuid, calibrated_p FROM trade_forecaster_probs "
        "WHERE calibrated_p IS NOT NULL")}
    calib2 = {r[0]: r[1] for r in c.execute(
        "SELECT trade_uuid, calibrated_p FROM trade_calibration "
        "WHERE calibrated_p IS NOT NULL")}
    rows = [dict(r) for r in c.execute(
        "SELECT id, trade_uuid, station, position_size_usd, trade_price, "
        "trade_type, signal_direction "
        "FROM trades ORDER BY id")]
    out = {"changes": [], "unchanged": 0, "no_calibrated_p": []}
    before = {}
    after = {}
    for r in rows:
        cp = calib.get(r["trade_uuid"], calib2.get(r["trade_uuid"]))
        old = r["position_size_usd"]
        before[old] = before.get(old, 0) + 1
        price = float(r["trade_price"] or 0.0)
        if cp is None or not (0.0 < price < 1.0):
            out["no_calibrated_p"].append(r["id"])
            after[old] = after.get(old, 0) + 1
            continue
        # Kelly (AF 2026-10-14): size on the BOOKED side's calibrated prob vs
        # the price paid. cp is P(high>=strike) (YES side).
        leg = "no" if str(r["trade_type"] or "").lower() == "buy_no" else (
            "yes" if str(r["trade_type"] or "").lower() == "buy_yes" else
            ("yes" if str(r["signal_direction"]).upper() == "UP" else "no"))
        our_prob = float(cp) if leg == "yes" else 1.0 - float(cp)
        new = kelly_position_size(our_prob, price)
        after[new] = after.get(new, 0) + 1
        if new == old:
            out["unchanged"] += 1
        else:
            out["changes"].append(
                {"id": r["id"], "station": r["station"], "calibrated_p": cp,
                 "leg": leg, "our_prob": round(our_prob, 4), "price": price,
                 "old": old, "new": new})
        if not dry_run:
            if r["id"] in existing_old:
                c.execute("UPDATE trades SET position_size_usd=? WHERE id=?",
                          (new, r["id"]))
            else:
                c.execute("UPDATE trades SET position_size_usd_old_2026=?, "
                          "position_size_usd=? WHERE id=?", (old, new, r["id"]))
    if not dry_run:
        c.commit()
    out["before_dist"] = {str(k): v for k, v in sorted(before.items())}
    out["after_dist"] = {str(k): v for k, v in sorted(after.items())}
    out["n_changed"] = len(out["changes"])
    return out


# ── Problem 3 ────────────────────────────────────────────────────────────────
def problem3(conn, dry_run=False):
    ensure_col(conn, "trades", "realized_pnl_old_2026", "REAL")
    c = conn
    rows = [dict(r) for r in c.execute(
        """SELECT id, station, market_type, signal_direction, trade_type,
                  quantity, trade_price, canonical_settled_f, settled_value,
                  realized_pnl, notes, status
             FROM trades ORDER BY id""")]
    out = {"rows": [], "total_honest": 0.0, "total_old": 0.0,
           "no_temp": [], "qty_capped": []}
    for r in rows:
        strike = strike_from_notes(r["notes"])
        temp = r["canonical_settled_f"]
        old = r["realized_pnl"]
        if temp is None or strike is None:
            out["no_temp"].append({"id": r["id"], "status": r["status"]})
            continue
        S = settle_yes(r["market_type"], float(temp), strike)
        pnl = honest_pnl(r["trade_type"], S, r["trade_price"])
        out["rows"].append({"id": r["id"], "status": r["status"], "S": S,
                            "old": old, "honest": round(pnl, 4)})
        out["total_honest"] += pnl
        if old is not None:
            out["total_old"] += old
        # quantity: REPORT-ONLY. A cap to the new position_size_usd would zero
        # 8 genuinely-traded rows, and the fee path derives price from
        # abs(trade_cost)/quantity (core/pnl_tracking.py _canonical_fee), so
        # rewriting quantity without trade_cost silently corrupts fee
        # recomputation. Deemed NOT sane -> no write. See the report.
        psize = c.execute("SELECT position_size_usd FROM trades WHERE id=?",
                          (r["id"],)).fetchone()[0]
        if psize and r["trade_price"] and r["trade_price"] > 0:
            cap = int(psize // r["trade_price"])
            if cap != (r["quantity"] or 0):
                out["qty_capped"].append(
                    {"id": r["id"], "old_qty": r["quantity"], "proposed": cap,
                     "psize": psize, "price": r["trade_price"],
                     "applied": False})
        if not dry_run:
            c.execute("UPDATE trades SET realized_pnl_old_2026=?, "
                      "realized_pnl=? WHERE id=?", (old, pnl, r["id"]))
    if not dry_run:
        c.commit()
    out["total_honest"] = round(out["total_honest"], 4)
    out["total_old"] = round(out["total_old"], 4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--problem", type=int, choices=[1, 2, 3], required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(BOOK)
    conn.row_factory = sqlite3.Row
    fn = {1: problem1, 2: problem2, 3: problem3}[a.problem]
    res = fn(conn, dry_run=a.dry_run)
    conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    tag = "dry" if a.dry_run else "applied"
    (OUT / f"problem{a.problem}_{tag}.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
