#!/usr/bin/env python3
"""
sbox_bankroll_timestop_qdist.py — two independent decisive tests (2026-10-12).

QUERY 1 — BANKROLL-COMPOUNDING SIMULATION (the "$16,769" fix)
------------------------------------------------------------
The resized-book helper (`rebuild_sbox_book_resized.py`) only rescaled each
trade's realized P&L by `position_size * old_per_trade_pnl / 50` — that is a
static rescale, NOT a bankroll simulation. The real question: start at $200 and
run the book's resolved trades in chronological order, sizing each trade off the
CURRENT bankroll via the grading ladder. This script does exactly that.

    bankroll = $200.00
    for each trade in created_at_utc order:
        grade from p_fused:
          p >= 0.9          -> S : min($25, bankroll * 0.125)
          0.8 <= p < 0.9    -> A : min($15, bankroll * 0.075)
          0.7 <= p < 0.8    -> B : min($10, bankroll * 0.05)
          0.6 <= p < 0.7    -> C : min($5,  bankroll * 0.025)
          p < 0.6           -> F : no entry
        net = realized_pnl / position_size_usd * new_position_size
        bankroll += net; stop if bankroll <= 0

Read-only against the live book. In-memory only. No temp DBs.

QUERY 2 — TIME-STOP q DISTRIBUTION (Track A-3 decisive test)
------------------------------------------------------------
Track A-3 found the time-stop rule turns positive only if settlement losers can
be exited at the next window at q >= 0.639 * entry_mid. That artifact stored no
post-entry quotes, so the verdict was "indeterminate". This script goes to the
market-snapshot DB and measures the ACTUAL q/entry_mid for the settlement losers.

DATA-DEPTH CAVEAT (surfaced in the report, not hidden):
The snapshot DB is only ~daily (1 snapshot/day at 04:00Z) for Aug 2026; intraday
snapshots (24/day) start ~2026-09-01 and are reliable from ~09-14. A snapshot at
04:00Z is BEFORE the 14/17/20Z entry, so for August trades the only "next
snapshot" is the FOLLOWING day — i.e. after the market has settled and the mid is
pinned at ~0. Those are post-settlement prices, not time-stop quotes. The script
therefore separates:
  * next_window      : first SAME-DAY snapshot at/after entry+3h (a real
                       time-stop quote, pre-settlement)
  * same_day_late    : first same-day snapshot after entry but < entry+3h
  * cross_day        : only a following-day (post-settlement) snapshot exists
and reports the q distribution for each. The adoptability verdict is based on the
next_window subset.

Usage:
    python3 scripts/sbox_bankroll_timestop_qdist.py            # writes combined JSON
    python3 scripts/sbox_bankroll_timestop_qdist.py --stdout   # print, do not write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SBOX = REPO / "data" / "paper_trading_sbox.db"
TRACKA = REPO / "data" / "trackA_spread_filter.json"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
OUT = REPO / "data" / "bankroll_timestop_combined_2026-10-12.json"

START_BANKROLL = 200.00
BREAKEVEN_Q_FRAC = 0.6391  # Track A-3 break-even, fraction of entry_mid

# ---- Dan's 2026-10-12 grading ladder (contiguous bands) ----
# The task's prose has gaps ([0.79,0.80) and [0.69,0.70)); bands are contiguous
# by intent (S>=.9, A>=.8, B>=.7, C>=.6). Two book trades sit in [0.79,0.80).
GRADE_BANDS = [
    (0.90, "S", 25.0, 0.125),
    (0.80, "A", 15.0, 0.075),
    (0.70, "B", 10.0, 0.050),
    (0.60, "C", 5.0, 0.025),
]


def grade_of(p: float):
    """(grade, cap_usd, bankroll_fraction) or ('F', 0, 0)."""
    for lo, g, cap, frac in GRADE_BANDS:
        if p >= lo:
            return g, cap, frac
    return "F", 0.0, 0.0


# ---------------------------------------------------------------- Query 1 ----
def run_bankroll_sim() -> dict:
    c = sqlite3.connect(f"file:{SBOX}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT id, trade_uuid, station, created_at_utc, p_fused, realized_pnl, "
        "position_size_usd, status, settlement_outcome, market_type "
        "FROM trades ORDER BY created_at_utc"
    ).fetchall()
    c.close()

    bankroll = START_BANKROLL
    peak = bankroll
    max_dd = 0.0
    max_dd_pct = 0.0
    dd_peak = bankroll
    dd_trough = bankroll
    curve = []
    entered = 0
    skipped_f = 0
    skipped_open = 0
    grade_counts = Counter()
    grade_pnl = defaultdict(float)
    bankrupt = False
    bankrupt_at = None

    total_rows = len(rows)
    resolved = [r for r in rows if r[5] is not None]
    for (tid, uuid, st, ts, p, pnl, pos, status, outcome, mtype) in rows:
        if pnl is None:
            skipped_open += 1
            curve.append({"id": tid, "ts": ts, "bankroll": round(bankroll, 2),
                          "action": "open_no_outcome", "p_fused": p})
            continue
        g, cap, frac = grade_of(float(p))
        if g == "F":
            skipped_f += 1
            curve.append({"id": tid, "ts": ts, "bankroll": round(bankroll, 2),
                          "action": "skip_F", "p_fused": p})
            continue
        size = min(cap, bankroll * frac)
        old = float(pos)
        net = (float(pnl) / old * size) if old > 0 else 0.0
        bankroll += net
        entered += 1
        grade_counts[g] += 1
        grade_pnl[g] += net
        peak = max(peak, bankroll)
        if bankroll < peak:
            dd = peak - bankroll
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd / peak
                dd_peak, dd_trough = peak, bankroll
        curve.append({"id": tid, "ts": ts, "bankroll": round(bankroll, 2),
                      "action": "enter", "grade": g, "p_fused": p,
                      "old_size": old, "new_size": round(size, 4),
                      "net_pnl": round(net, 4)})
        if bankroll <= 0:
            bankrupt = True
            bankrupt_at = tid
            break

    return {
        "start_bankroll": START_BANKROLL,
        "final_bankroll": round(bankroll, 2),
        "total_pnl": round(bankroll - START_BANKROLL, 2),
        "return_pct": round(100 * (bankroll - START_BANKROLL) / START_BANKROLL, 2),
        "book_rows_total": total_rows,
        "resolved_trades": len(resolved),
        "open_trades_excluded": skipped_open,
        "trades_entered": entered,
        "trades_skipped_grade_F": skipped_f,
        "entered_by_grade": dict(sorted(grade_counts.items())),
        "pnl_by_grade": {k: round(v, 2) for k, v in sorted(grade_pnl.items())},
        "bankrupt": bankrupt,
        "bankrupt_at_trade_id": bankrupt_at,
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(100 * max_dd_pct, 2),
        "peak_bankroll": round(peak, 2),
        "drawdown_peak": round(dd_peak, 2),
        "drawdown_trough": round(dd_trough, 2),
        "pnl_curve_peak_trough_end": {
            "peak": round(peak, 2),
            "trough": round(min((p["bankroll"] for p in curve), default=START_BANKROLL), 2),
            "end": round(bankroll, 2),
        },
        "curve": curve,
    }


# ---------------------------------------------------------------- Query 2 ----
PREFIX = {
    "KATL": "KXHIGHTATL", "KAUS": "KXHIGHAUS", "KBOS": "KXHIGHTBOS",
    "KDCA": "KXHIGHTDC", "KDEN": "KXHIGHDEN", "KDFW": "KXHIGHTDAL",
    "KHOU": "KXHIGHTHOU", "KLAS": "KXHIGHTLV", "KLAX": "KXHIGHLAX",
    "KMDW": "KXHIGHCHI", "KMIA": "KXHIGHMIA", "KMSP": "KXHIGHTMIN",
    "KMSY": "KXHIGHTNOLA", "KNYC": "KXHIGHNY", "KOKC": "KXHIGHTOKC",
    "KPHL": "KXHIGHPHIL", "KPHX": "KXHIGHTPHX", "KSAT": "KXHIGHTSATX",
    "KSEA": "KXHIGHTSEA", "KSFO": "KXHIGHTSFO",
}
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_MONR = {v: k for k, v in _MON.items()}
WINH = {"14Z": 14, "17Z": 17, "20Z": 20}


def _ticker(st, ld, strike):
    y, mo, d = ld.split("-")
    return f"{PREFIX[st]}-{y[2:]}{_MONR[int(mo)]}{d}-B{float(strike):g}"


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[i], 4)


def run_qdist() -> dict:
    d = json.load(open(TRACKA))
    trades = d["trades"]
    losers = [t for t in trades
              if t["exit_kind"] == "settlement" and t["exit_mid"] == 0]
    c = sqlite3.connect(f"file:{SNAPS}?mode=ro", uri=True)
    cur = c.cursor()

    recs = []
    for t in losers:
        tk = _ticker(t["station"], t["local_date"], t["strike"])
        entry = datetime.fromisoformat(
            f"{t['local_date']}T{WINH[t['window_utc']]:02d}:00:00+00:00")
        target = entry + timedelta(hours=3)
        qrows = cur.execute(
            "SELECT snapshot_time, mid_price, yes_bid, yes_ask FROM market_snapshots "
            "WHERE market_ticker=? ORDER BY snapshot_time", (tk,)).fetchall()
        same_day, after = [], []
        for stime, mid, bid, ask in qrows:
            dt = datetime.fromisoformat(stime)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt <= entry:
                continue
            after.append((dt, mid, bid, ask))
            if dt.date() == entry.date():
                same_day.append((dt, mid, bid, ask))
        cand = [r for r in same_day if r[0] >= target]
        if cand:
            pick, how = min(cand, key=lambda r: r[0]), "next_window"
        elif same_day:
            pick, how = min(same_day, key=lambda r: r[0]), "same_day_late"
        elif after:
            pick, how = min(after, key=lambda r: r[0]), "cross_day"
        else:
            pick, how = None, "no_quote"
        rec = {
            "station": t["station"], "local_date": t["local_date"],
            "window_utc": t["window_utc"], "strike": t["strike"],
            "entry_mid": t["entry_mid"], "ticker": tk, "match_kind": how,
        }
        if pick is not None:
            rec.update(snapshot_time=pick[0].isoformat(), exit_mid=pick[1],
                       exit_bid=pick[2], exit_ask=pick[3],
                       q=round(pick[1] / t["entry_mid"], 6) if t["entry_mid"] else None,
                       has_bid=bool(pick[2] and pick[2] > 0))
        else:
            rec.update(snapshot_time=None, exit_mid=None, exit_bid=None,
                       exit_ask=None, q=None, has_bid=False)
        recs.append(rec)
    c.close()

    def dist(subset):
        qs = sorted(r["q"] for r in subset if r["q"] is not None)
        if not qs:
            return {"n": 0}
        return {
            "n": len(qs),
            "median": round(statistics.median(qs), 4),
            "mean": round(statistics.fmean(qs), 4),
            "p25": _pct(qs, 0.25),
            "p75": _pct(qs, 0.75),
            "min": round(qs[0], 4),
            "max": round(qs[-1], 4),
            "n_zero_mid": sum(1 for r in subset if r["q"] == 0),
            "n_no_quote": sum(1 for r in subset if r["q"] is None),
            "n_zero_bid": sum(1 for r in subset if r["q"] is not None
                              and not r["has_bid"]),
            "n_at_or_above_breakeven": sum(1 for q in qs if q >= BREAKEVEN_Q_FRAC),
        }

    by_kind = defaultdict(list)
    for r in recs:
        by_kind[r["match_kind"]].append(r)
    clean = by_kind["next_window"]
    dist_clean = dist(clean)
    median_clean = dist_clean.get("median")
    adoptable = median_clean is not None and median_clean > 0.64

    return {
        "n_settlement_losers": len(losers),
        "match_kind_counts": dict(sorted(Counter(r["match_kind"] for r in recs).items())),
        "breakeven_q_frac": BREAKEVEN_Q_FRAC,
        "distribution_all_105": dist(recs),
        "distribution_next_window_only": dist_clean,
        "distribution_cross_day_only": dist(by_kind["cross_day"]),
        "median_q_next_window": median_clean,
        "verdict_adoptable": adoptable,
        "data_depth_note": (
            "Aug 2026 snapshots are ~1/day at 04:00Z (pre-entry), so August losers "
            "only have a following-day post-settlement snapshot (mid pinned ~0). "
            "Only Sep losers can carry a true pre-settlement time-stop quote. The "
            "adoptability verdict uses the next_window subset (real next-window "
            "quotes), not the post-settlement cross_day prices."),
        "records": recs,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args(argv)

    q1 = run_bankroll_sim()
    q2 = run_qdist()
    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "query1_bankroll_compound": q1,
        "query2_timestop_qdist": q2,
        "stitch": {
            "timestop_adoptable": q2["verdict_adoptable"],
            "note": ("If the time-stop is adoptable, the bankroll simulation with "
                     "time-stop exits is the final test. See report."),
        },
    }
    print("=== QUERY 1: bankroll compounding ===")
    print(f"  start ${q1['start_bankroll']:.2f} -> final ${q1['final_bankroll']:.2f} "
          f"({q1['return_pct']:+.2f}%)")
    print(f"  resolved={q1['resolved_trades']} entered={q1['trades_entered']} "
          f"skipF={q1['trades_skipped_grade_F']} open={q1['open_trades_excluded']}")
    print(f"  by grade {q1['entered_by_grade']}  pnl {q1['pnl_by_grade']}")
    print(f"  maxDD ${q1['max_drawdown_usd']:.2f} ({q1['max_drawdown_pct']:.2f}%) "
          f"peak ${q1['peak_bankroll']:.2f} bankrupt={q1['bankrupt']}")
    print("=== QUERY 2: time-stop q distribution ===")
    print(f"  losers={q2['n_settlement_losers']} kinds={q2['match_kind_counts']}")
    print(f"  next-window dist: {q2['distribution_next_window_only']}")
    print(f"  all-105 dist: {q2['distribution_all_105']}")
    print(f"  median_q(next_window)={q2['median_q_next_window']} "
          f"adoptable={q2['verdict_adoptable']}")

    if not args.stdout:
        Path(OUT).write_text(json.dumps(out, indent=2))
        print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
