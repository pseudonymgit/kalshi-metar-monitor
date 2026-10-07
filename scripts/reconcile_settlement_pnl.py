#!/usr/bin/env python3
"""
reconcile_settlement_pnl.py — Settlement P&L reconciliation (Sprint G, item G2).

For every settled trade, compare booked `realized_pnl` against the settlement
label on the STRICT strike-crossing axis:

  HIGH: contract pays $1.00 iff settlement_temp > strike   (equality = loss)
  LOW:  contract pays $1.00 iff settlement_temp < strike
  buy_yes: exp = (payout - trade_price) * qty
  buy_no:  exp = ((1 - payout) - (1 - trade_price)) * qty

Strike is parsed from the trade's own executed notes/ticker (`@ <X>F`).
Two settlement-label sources are checked SEPARATELY (they disagree — see below):

  A. epoch  = metar.settlement_epochs.settlement_bucket   (the G2 order's reference)
  B. truth  = kalshi_settlements.kalshi_temp (source=live_api, finalized),
              else ghcn_settlements.high_f

A row "contradicts" when booked P&L sign != expected sign (|Δ|>0.01).
Per the Sprint-G order, closed P&L is NOT retroactively changed — rows are
ANNOTATED (reconciliation_log + a [RECON] note).

Usage:
  python3 scripts/reconcile_settlement_pnl.py            # report only
  python3 scripts/reconcile_settlement_pnl.py --annotate # annotate (no P&L change)
"""
import argparse, json, re, sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAPER = REPO / "data" / "paper_trading_sbox.db"
METAR = REPO / "data" / "metar_backfill.db"
KALSHI = REPO / "data" / "kalshi_settlements.db"
GHCN = REPO / "data" / "ghcn_settlements.db"


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def strike_from_notes(notes):
    if not notes:
        return None
    m = re.search(r"@\s*(-?\d+(?:\.\d+)?)F", notes)
    return float(m.group(1)) if m else None


def payout(mt, settle, strike):
    if mt == "HIGH":
        return 1.0 if settle > strike else 0.0
    return 1.0 if settle < strike else 0.0


def exp_pnl(tt, pay, tp, qty):
    if tt == "buy_yes":
        return (pay - tp) * qty
    if tt == "buy_no":
        return ((1 - pay) - (1 - tp)) * qty
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotate", action="store_true")
    a = ap.parse_args()

    p, m, k, g = ro(PAPER), ro(METAR), ro(KALSHI), ro(GHCN)
    trades = p.execute("""SELECT id,trade_uuid,trade_date_utc,station,market_type,
                          signal_direction,trade_type,quantity,trade_price,
                          settled_value,realized_pnl,status,notes
                          FROM trades ORDER BY id""").fetchall()

    rows = []
    for (tid, uuid, d, st, mt, sd, tt, qty, tp, sv, pnl, status, notes) in trades:
        strike = strike_from_notes(notes)
        ep = m.execute("SELECT settlement_bucket FROM settlement_epochs "
                       "WHERE station=? AND market_type=? AND local_trading_date=?",
                       (st, mt, d)).fetchone()
        bucket = ep[0] if ep else None
        ks = k.execute("SELECT kalshi_temp FROM kalshi_settlements WHERE station=? AND target_date=?",
                       (st, d)).fetchone()
        kalshi = ks[0] if ks else None
        gh = g.execute("SELECT high_f FROM ghcn_settlements WHERE station=? AND date=?", (st, d)).fetchone()
        ghcn = gh[0] if gh else None
        truth = kalshi if kalshi is not None else (round(ghcn) if ghcn is not None else None)
        truth_src = "kalshi" if kalshi is not None else ("ghcn" if ghcn is not None else None)

        def mk(settle):
            if settle is None or strike is None or status != "closed":
                return None
            return exp_pnl(tt, payout(mt, settle, strike), tp, qty)

        exp_e, exp_t = mk(bucket), mk(truth)
        ce = (exp_e is not None and pnl is not None and (exp_e > 0) != (pnl > 0) and abs(exp_e - pnl) > 0.01)
        ct = (exp_t is not None and pnl is not None and (exp_t > 0) != (pnl > 0) and abs(exp_t - pnl) > 0.01)
        rows.append(dict(id=tid, uuid=uuid, date=d, station=st, market_type=mt, trade_type=tt,
                         status=status, strike=strike, trade_price=tp, qty=qty,
                         booked_pnl=pnl, epoch_bucket=bucket, kalshi_temp=kalshi, ghcn_high=ghcn,
                         truth=truth, truth_src=truth_src, settled_value=sv,
                         exp_epoch=None if exp_e is None else round(exp_e, 3),
                         exp_truth=None if exp_t is None else round(exp_t, 3),
                         contrad_epoch=ce, contrad_truth=ct))

    closed = [r for r in rows if r["status"] == "closed"]
    clean = [r for r in rows if r["status"] != "flagged"]
    ce_rows = [r for r in clean if r["contrad_epoch"]]
    ct_rows = [r for r in clean if r["contrad_truth"]]
    either = {r["id"] for r in ce_rows} | {r["id"] for r in ct_rows}

    # epoch-vs-truth label disagreement (independent of P&L)
    label_disagree = [r for r in closed if r["truth"] is not None and r["epoch_bucket"] is not None
                      and abs(r["truth"] - r["epoch_bucket"]) >= 1]
    # did the trade's own settled_value follow epoch or truth?
    sv_vs_epoch = sum(1 for r in closed if r["settled_value"] is not None and r["epoch_bucket"] is not None
                      and abs(r["settled_value"] - r["epoch_bucket"]) < 0.5)
    sv_vs_truth = sum(1 for r in closed if r["settled_value"] is not None and r["truth"] is not None
                      and abs(r["settled_value"] - r["truth"]) < 0.5)
    sv_n = sum(1 for r in closed if r["settled_value"] is not None and r["truth"] is not None)

    out = {
        "settled_trades": len(closed), "clean_trades": len(clean),
        "check_A_epoch_contradictions_in_clean": len(ce_rows),
        "check_A_ids": sorted(r["id"] for r in ce_rows),
        "check_A_pnl_delta_if_corrected": round(sum(r["exp_epoch"] - r["booked_pnl"] for r in ce_rows), 3),
        "check_B_truth_contradictions_in_clean": len(ct_rows),
        "check_B_ids": sorted(r["id"] for r in ct_rows),
        "check_B_pnl_delta_if_corrected": round(sum(r["exp_truth"] - r["booked_pnl"] for r in ct_rows), 3),
        "rows_contradicting_either": sorted(either),
        "epoch_vs_truth_label_disagreements": len(label_disagree),
        "trade_settled_value_matches_epoch": f"{sv_vs_epoch}/{len(closed)}",
        "trade_settled_value_matches_truth": f"{sv_vs_truth}/{sv_n}",
        "root_cause": ("settlement_epochs was re-derived 2026-10-07T04:00Z, AFTER these trades "
                       "settled (10-03..10-06), so the current epoch bucket no longer equals the "
                       "trade's own settled_value. Separately, the epoch disagrees with independent "
                       "Kalshi/GHCN truth on many 10-02..10-05 station-days, and Kalshi truth is not "
                       "yet published for 10-03..10-06. Trade settled_value tracks the epoch, not truth."),
        "action": "ANNOTATE only; closed P&L NOT changed.",
    }
    print(json.dumps(out, indent=2))

    if a.annotate:
        w = sqlite3.connect(PAPER)
        now = datetime.now(timezone.utc).isoformat()
        w.execute("""CREATE TABLE IF NOT EXISTS reconciliation_log(
            id INTEGER PRIMARY KEY, trade_uuid TEXT, check_name TEXT, verdict TEXT,
            detail TEXT, evidence TEXT, created_at_utc TEXT)""")
        n = 0
        for r in rows:
            if r["status"] != "closed" or not (r["contrad_epoch"] or r["contrad_truth"]):
                continue
            w.execute("DELETE FROM reconciliation_log WHERE trade_uuid=? AND check_name=?",
                      (r["uuid"], "settlement_pnl_contradiction"))
            w.execute("""INSERT INTO reconciliation_log
                (trade_uuid,check_name,verdict,detail,evidence,created_at_utc) VALUES (?,?,?,?,?,?)""",
                (r["uuid"], "settlement_pnl_contradiction", "stale_epoch_recompute",
                 json.dumps({k: r[k] for k in ("id", "station", "date", "strike", "epoch_bucket",
                            "kalshi_temp", "ghcn_high", "booked_pnl", "exp_epoch", "exp_truth")}, default=str),
                 f"settled_value={r['settled_value']} epoch={r['epoch_bucket']} truth={r['truth']}({r['truth_src']})", now))
            tag = ("[RECON 2026-10-07] settlement label unreliable: booked P&L matches trade "
                   "settled_value, but the epoch was re-derived after settlement and disagrees "
                   "with independent truth; closed P&L left unchanged by policy.")
            cur = w.execute("SELECT notes FROM trades WHERE trade_uuid=?", (r["uuid"],)).fetchone()
            notes = cur[0] if cur else ""
            if tag not in (notes or ""):
                w.execute("UPDATE trades SET notes=? WHERE trade_uuid=?",
                          ((notes + " " + tag) if notes else tag, r["uuid"]))
            n += 1
        w.commit(); w.close()
        print(f"\nannotated {n} rows (reconciliation_log + notes; P&L unchanged)")


if __name__ == "__main__":
    main()
