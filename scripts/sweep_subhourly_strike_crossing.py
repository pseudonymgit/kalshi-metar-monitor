#!/usr/bin/env python3
"""
sweep_subhourly_strike_crossing.py — F2 sweep of the sub-hourly trajectory signal
against REAL strike-crossing outcomes.

WHY THIS EXISTS (F2)
--------------------
`core/signals/subhourly_trajectory_signal.py` is a purely deterministic nowcast
that predicts strike-crossing from the 15-minute METAR/ASOS temperature slope in
the 1-3h before the decision window. It is gated from live trading
(ENABLED_BY_DEFAULT empty) and had NEVER been swept against real strike-crossing
outcomes. This script does that sweep, in isolation, with no live wiring.

TWO SWEEP POPULATIONS
---------------------
1. The 68 settled SBOX paper trades (data/paper_trading_sbox.db). For each trade
   we extract the signal's prediction at the pinned decision instant
   (13:30 station-local -> 17:00-20:00 UTC) and score it against the trade's real
   strike-crossing outcome. We report accuracy, the raw contingency table, a
   standalone P&L, and a comparison to the current fused result (p_fused).
2. The full strike-crossing outcome set for Aug 1 -> now, on the ~9k-pair scale
   of the strike-axis calibration window (data/strike_axis_calibration.json:
   n_training_pairs = 9053). The signal predicts for EVERY station-day, not just
   the traded ones, so we sweep it against the full HIGH/greater ladder
   (data/kalshi_strike_ladder.db) and against the settlement_truth catalog.

CONSTRAINTS (all honored)
-------------------------
* Deterministic only — the signal is arithmetic; this harness adds no ML/AI.
* No lookahead — predictions use only ASOS observations with valid <= T, where
  T is the decision instant. Settlement truth is label-only.
* Read-only — no DB writes, no trade generation, no live-path wiring.
* Axis — strict strike-crossing: cross = settlement_temp_f > strike_price.

Usage:
    .venv-gefs/bin/python scripts/sweep_subhourly_strike_crossing.py
    .venv-gefs/bin/python scripts/sweep_subhourly_strike_crossing.py --out data/f2_subhourly_sweep.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.signals.subhourly_trajectory_signal import (  # noqa: E402
    SubhourlyTrajectorySignal, STATIONS, _parse_utc, _tz_of,
    GAP_LO, GAP_HI, SLOPE_THRESHOLD,
    MIN_OBS_FOR_FIT, WINDOW_BEFORE_HOTD, RES,
)
from core.booking_admission import pinned_entry_time_utc  # noqa: E402

SBOX_DB = REPO / "data" / "paper_trading_sbox.db"
LADDER_DB = REPO / "data" / "kalshi_strike_ladder.db"
TRUTH_DB = REPO / "data" / "metar_backfill.db"
ASOS_DB = REPO / "data" / "iem_asos_1min.db"
AXIS_CAL = REPO / "data" / "strike_axis_calibration.json"

# F2 window: the calibration window the task names (Aug 1 -> now).
WINDOW_START = "2026-08-01"
# Full-history window for the wider strike-crossing check (bounded by ASOS).
FULL_START = "2024-01-01"

MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


# ── data access ────────────────────────────────────────────────────────────
def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _strike_from_ticker(tk: str):
    # Notes look like "... KXHIGHTHOU-26OCT10-T88 @ 88.0F; inst=SBOX" — the T
    # strike is followed by a price suffix, so do NOT anchor to end-of-string.
    m = re.search(r"-T([0-9]+(?:\.[0-9]+)?)", tk or "")
    return float(m.group(1)) if m else None


def load_trades():
    """The 68 settled SBOX trades with real strike-crossing outcomes."""
    c = _ro(SBOX_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY trade_date_utc")]
    c.close()
    out = []
    for r in rows:
        strike = _strike_from_ticker(r.get("notes"))
        settle = r.get("canonical_settled_f")
        if settle is None:
            settle = r.get("settled_value")
        if strike is None or settle is None:
            continue
        out.append({
            "station": r["station"],
            "date": r["trade_date_utc"][:10],
            "market_type": r["market_type"],
            "side": "yes" if (r["signal_direction"] == "UP") else "no",
            "strike": float(strike),
            "settle": float(settle),
            "outcome": 1 if float(settle) > float(strike) else 0,
            "won": 1 if r.get("realized_pnl", 0) and r["realized_pnl"] > 0 else 0,
            "realized_pnl": r.get("realized_pnl"),
            "position_size_usd": r.get("position_size_usd"),
            "trade_price": r.get("trade_price"),
            "p_gefs": r.get("p_gefs"),
            "p_analog": r.get("p_analog"),
            "p_trajectory": r.get("p_trajectory"),
            "p_fused": r.get("p_fused"),
        })
    return out


def load_ladder(start: str):
    """{station: {date: {strike: outcome}}} for HIGH 'greater' strikes."""
    c = _ro(LADDER_DB)
    c.row_factory = sqlite3.Row
    q = ("SELECT station, local_trading_date, strike_value, result FROM strike_ladder "
         "WHERE market_type='HIGH' AND strike_type='greater' "
         "AND result IN ('yes','no')")
    out = defaultdict(lambda: defaultdict(dict))
    n = 0
    for st, d, sv, res in c.execute(q):
        if d < start:
            continue
        out[st][d][float(sv)] = 1 if res == "yes" else 0
        n += 1
    c.close()
    return out, n


def load_truth(start: str):
    """{station: {date: {strike: outcome}}} from settlement_truth (secondary)."""
    c = _ro(TRUTH_DB)
    c.row_factory = sqlite3.Row
    q = ("SELECT station, local_trading_date, strike_price, settlement_temp_f "
         "FROM settlement_truth WHERE strike_type='greater' AND trustworthy=1 "
         "AND strike_price IS NOT NULL AND settlement_temp_f IS NOT NULL")
    out = defaultdict(lambda: defaultdict(dict))
    n = 0
    for st, d, sv, stf in c.execute(q):
        if d < start or st not in STATIONS:
            continue
        out[st][d][float(sv)] = 1 if float(stf) > float(sv) else 0
        n += 1
    c.close()
    return out, n


def load_asos(stations, lo_utc: str, hi_utc: str):
    """{station: {local_date: [(minute_of_day, tmpf), ...]}} for a UTC bound range.

    ASOS `valid` is UTC. We convert to the station's local timezone and key by
    local date, exactly as the signal does. Contemporaneous filtering (valid<=T)
    happens later, at prediction time.
    """
    c = _ro(ASOS_DB)
    out = defaultdict(lambda: defaultdict(list))
    for st in stations:
        q = ("SELECT valid, tmpf FROM observations WHERE station=? AND tmpf IS NOT NULL "
             "AND valid >= ? AND valid <= ? ORDER BY valid")
        for valid, tmpf in c.execute(q, (st, lo_utc, hi_utc)):
            dt = _parse_utc(valid)
            if dt is None:
                continue
            loc = dt.astimezone(_tz_of(st))
            mod = loc.hour * 60 + loc.minute
            out[st][loc.date().isoformat()].append((mod, tmpf))
    c.close()
    for st in out:
        for ld in out[st]:
            out[st][ld].sort()
    return out


# ── scoring helpers ────────────────────────────────────────────────────────
def _metrics(direction_map, outcomes):
    """direction_map: {key: 'cross'|'no-cross'|None}; outcomes: {key: 0|1}."""
    n = len(outcomes)
    correct = sum(1 for k, o in outcomes.items()
                  if direction_map.get(k) in ("cross", "no-cross")
                  and ((direction_map[k] == "cross") == (o == 1)))
    fired = sum(1 for k in outcomes if direction_map.get(k) in ("cross", "no-cross"))
    abstained = sum(1 for k in outcomes if direction_map.get(k) is None)
    return {
        "n_pairs": n,
        "n_fired": fired,
        "n_abstained": abstained,
        "accuracy_on_fired": round(correct / fired, 4) if fired else None,
        "cross_rate": round(sum(outcomes.values()) / n, 4) if n else None,
    }


def _contingency(rows):
    """rows: list of (prediction, outcome). Returns raw 2x2 + derived."""
    tp = sum(1 for p, o in rows if p == "cross" and o == 1)
    fp = sum(1 for p, o in rows if p == "cross" and o == 0)
    fn = sum(1 for p, o in rows if p == "no-cross" and o == 1)
    tn = sum(1 for p, o in rows if p == "no-cross" and o == 0)
    n = tp + fp + fn + tn
    acc = (tp + tn) / n if n else None
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": n,
        "accuracy": round(acc, 4) if acc is not None else None,
        "precision": round(prec, 4) if prec is not None else None,
        "recall": round(rec, 4) if rec is not None else None,
        "cross_base_rate": round((tp + fn) / n, 4) if n else None,
    }


def _wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return [round((c - s) / d, 4), round((c + s) / d, 4)]


# ── analysis 1: the 68 settled trades ──────────────────────────────────────
def analyze_trades(sig, asos, trades):
    rows = []
    for t in trades:
        st, d, strike, settle = t["station"], t["date"], t["strike"], t["settle"]
        asos_day = asos.get(st, {}).get(d)
        if not asos_day or len(asos_day) < MIN_OBS_FOR_FIT:
            rows.append({**t, "prediction": None, "reason": "no_asos"})
            continue
        iso = pinned_entry_time_utc(st, d)
        if iso is None:
            rows.append({**t, "prediction": None, "reason": "no_pin"})
            continue
        t_utc = datetime.fromisoformat(iso)
        loc = t_utc.astimezone(_tz_of(st))
        t_min = loc.hour * 60 + loc.minute  # 13:30 local -> 810
        res = sig.evaluate_nowcast(st, d, t_min, asos_day=asos_day,
                                   strike=strike, settle=settle)
        rows.append({**t, "prediction": res["prediction"],
                     "gap": res["gap"], "slope": round(res["slope"], 5),
                     "cur_max": res["cur_max"],
                     "already_crossed": res["already_crossed"],
                     "confidence": round(res["confidence"], 4),
                     "t_min": t_min, "reason": "ok"})
    return rows


def _price(t, side):
    """Entry price for a side, from the trade's own (executable) price."""
    tp = t.get("trade_price")
    if tp is None:
        return None
    return float(tp) if side == t["side"] else round(1.0 - float(tp), 6)


def trade_selection_policies(trade_rows):
    """Compare 'fused as-is' vs subhourly-driven selection on the 68."""
    def pnl(side, outcome, price, stake):
        if price is None or price <= 0:
            return 0.0
        settle = 1.0 if ((side == "yes") == (outcome == 1)) else 0.0
        return (settle - price) / price * stake

    out = {}

    # A) fused as-is: the 68 executed trades, real booked P&L.
    a_pnl = sum(r["realized_pnl"] or 0.0 for r in trade_rows)
    a_win = sum(r["won"] for r in trade_rows
                if r.get("realized_pnl") is not None)
    a_n = sum(1 for r in trade_rows if r.get("realized_pnl") is not None)
    out["A_fused_as_is"] = {
        "n": a_n, "wins": a_win,
        "win_rate": round(a_win / a_n, 4) if a_n else None,
        "total_pnl": round(a_pnl, 2),
        "note": "the 68 settled trades exactly as booked",
    }

    # B) subhourly alone: fire the subhourly direction on every traded station-day.
    b_pnl, b_win, b_n, b_skip = 0.0, 0, 0, 0
    for r in trade_rows:
        p = r.get("prediction")
        if p is None:
            b_skip += 1
            continue
        side = "yes" if p == "cross" else "no"
        px = _price(r, side)
        stake = r.get("position_size_usd") or 10.0
        b_n += 1
        b_pnl += pnl(side, r["outcome"], px, stake)
        if r["outcome"] == (1 if side == "yes" else 0):
            b_win += 1
    out["B_subhourly_alone"] = {
        "n": b_n, "skipped_abstain": b_skip, "wins": b_win,
        "win_rate": round(b_win / b_n, 4) if b_n else None,
        "total_pnl": round(b_pnl, 2),
        "note": "subhourly direction traded at the trade's own entry price; "
                "abstains on the 'already crossed' regime",
    }

    # C) fused PASSES only when subhourly AGREES with the booked side.
    c_pnl, c_win, c_n, c_veto = 0.0, 0, 0, 0
    for r in trade_rows:
        p = r.get("prediction")
        agree = (p == "cross" and r["side"] == "yes") or \
                (p == "no-cross" and r["side"] == "no")
        if not agree:
            c_veto += 1
            continue
        c_n += 1
        c_pnl += (r["realized_pnl"] or 0.0)
        if r["won"]:
            c_win += 1
    out["C_fused_x_subhourly_agrees"] = {
        "n": c_n, "vetoed": c_veto, "wins": c_win,
        "win_rate": round(c_win / c_n, 4) if c_n else None,
        "total_pnl": round(c_pnl, 2),
        "note": "keep the booked trade only when subhourly agrees; else skip",
    }

    # D) fused VETO: drop booked trades where subhourly disagrees (independent gate).
    d_pnl, d_win, d_n, d_drop = 0.0, 0, 0, 0
    for r in trade_rows:
        p = r.get("prediction")
        disagree = (p == "cross" and r["side"] == "no") or \
                   (p == "no-cross" and r["side"] == "yes")
        if disagree:
            d_drop += 1
            continue
        d_n += 1
        d_pnl += (r["realized_pnl"] or 0.0)
        if r["won"]:
            d_win += 1
    out["D_fused_minus_subhourly_disagrees"] = {
        "n": d_n, "dropped": d_drop, "wins": d_win,
        "win_rate": round(d_win / d_n, 4) if d_n else None,
        "total_pnl": round(d_pnl, 2),
        "note": "veto only the disagreements; abstains are kept",
    }
    return out


# ── analysis 2: full strike-crossing sweep ─────────────────────────────────
def sweep_full(sig, asos, outcome_sets, label, scan_all_minutes=True):
    """One pass per (station,date). Fan the day's nowcast out to every strike.

    scan_all_minutes=True  -> a pair fires if ANY window-before-HOTD minute
                              predicts cross (mirrors iter_nowcast_events()).
    scan_all_minutes=False -> single evaluation at the 13:30-local pin.
    """
    dir_map = {}
    outcomes = {}
    per_day = {}
    for st in sorted(asos):
        tz = _tz_of(st)
        for d in sorted(asos[st]):
            strikes = outcome_sets.get(st, {}).get(d)
            if not strikes:
                continue
            asos_day = asos[st][d]
            if len(asos_day) < MIN_OBS_FOR_FIT:
                continue
            if scan_all_minutes:
                hi_mod = max(asos_day, key=lambda x: x[1])[0]
                t_lo = max(0, hi_mod - WINDOW_BEFORE_HOTD[1])
                t_hi = max(0, hi_mod - WINDOW_BEFORE_HOTD[0])
                minutes = range(t_lo, t_hi + 1, RES)
            else:
                minutes = [810]  # 13:30 local
            for sv, o in strikes.items():
                key = (st, d, sv)
                outcomes[key] = o
                fired_cross = False
                any_pred = False
                for t in minutes:
                    r = sig.evaluate_nowcast(st, d, t, asos_day=asos_day,
                                             strike=sv, settle=None)
                    if r["prediction"] is None:
                        continue
                    any_pred = True
                    if r["prediction"] == "cross":
                        fired_cross = True
                        break
                if fired_cross:
                    dir_map[key] = "cross"
                elif any_pred:
                    dir_map[key] = "no-cross"
                else:
                    dir_map[key] = None
    m = _metrics(dir_map, outcomes)
    m["label"] = label
    rows = [(dir_map[k], o) for k, o in outcomes.items()
            if dir_map[k] in ("cross", "no-cross")]
    m["contingency"] = _contingency(rows)
    if m["n_fired"]:
        k = m["contingency"]["tp"] + m["contingency"]["tn"]
        m["accuracy_wilson_95"] = _wilson(k, m["n_fired"])
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "data" / "f2_subhourly_sweep.json"))
    ap.add_argument("--start", default=WINDOW_START)
    ap.add_argument("--full-start", default=FULL_START)
    a = ap.parse_args()

    sig = SubhourlyTrajectorySignal(str(TRUTH_DB))

    trades = load_trades()
    ladder, n_ladder = load_ladder(a.start)
    ladder_full, n_ladder_full = load_ladder(a.full_start)
    truth, n_truth = load_truth(a.start)

    # ASOS load bounds: widen the UTC window to cover local-date boundaries.
    lo = (datetime.fromisoformat(a.full_start) - timedelta(days=2)).isoformat()
    hi = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    stations = set(STATIONS)
    for st in list(ladder) + list(ladder_full):
        stations.add(st)
    for t in trades:
        stations.add(t["station"])

    print(f"[F2] loading ASOS for {len(stations)} stations, {lo}..{hi} ...")
    asos = load_asos(sorted(stations), lo, hi)
    print(f"[F2] asos station-days: {sum(len(v) for v in asos.values())}")

    # analysis 1: the 68 trades
    trade_rows = analyze_trades(sig, asos, trades)
    fired = [(r["prediction"], r["outcome"]) for r in trade_rows
             if r["prediction"] in ("cross", "no-cross")]
    trade_cont = _contingency(fired)
    n_fired = len(fired)
    n_correct = sum(1 for p, o in fired if (p == "cross") == (o == 1))
    trade_acc = round(n_correct / n_fired, 4) if n_fired else None
    abstain = sum(1 for r in trade_rows if r["prediction"] is None)
    # agreement of subhourly direction with the booked +GEFS fused side
    agree = sum(1 for r in trade_rows
                if r["prediction"] in ("cross", "no-cross")
                and ((r["prediction"] == "cross") == (r["side"] == "yes")))

    # fused baseline on the same 68
    fused_n = len(trade_rows)
    fused_win = sum(r["won"] for r in trade_rows)
    fused_pnl = sum((r["realized_pnl"] or 0.0) for r in trade_rows)

    analysis1 = {
        "n_trades": fused_n,
        "subhourly_fired": n_fired,
        "subhourly_abstained": abstain,
        "subhourly_accuracy": trade_acc,
        "subhourly_accuracy_wilson_95": _wilson(n_correct, n_fired) if n_fired else None,
        "contingency": trade_cont,
        "agreement_with_booked_side": agree,
        "agreement_rate": round(agree / n_fired, 4) if n_fired else None,
        "fused_as_is": {
            "n": fused_n, "wins": fused_win,
            "win_rate": round(fused_win / fused_n, 4) if fused_n else None,
            "total_pnl": round(fused_pnl, 2),
        },
        "selection_policies": trade_selection_policies(trade_rows),
        "per_trade": trade_rows,
    }

    # analysis 2: full window sweeps
    print("[F2] sweeping full window against Kalshi ladder ...")
    full_ladder = sweep_full(sig, asos, ladder, f"ladder_HIGH_greater {a.start}+", True)
    full_ladder_pin = sweep_full(sig, asos, ladder, f"ladder_HIGH_greater {a.start}+ @13:30", False)
    full_truth = sweep_full(sig, asos, truth, f"settlement_truth_greater {a.start}+", True)
    print("[F2] sweeping wider history (>=%s) ..." % a.full_start)
    full_hist = sweep_full(sig, asos, ladder_full, f"ladder_HIGH_greater {a.full_start}+", True)

    analysis2 = {
        "ladder_pairs_window": n_ladder,
        "ladder_pairs_full": n_ladder_full,
        "truth_pairs_window": n_truth,
        "calibration_window_reference": {
            "source": "data/strike_axis_calibration.json",
            "n_training_pairs": 9053,
            "eval_window_start": "2026-08-01",
        },
        "sweeps": {
            "ladder_window": full_ladder,
            "ladder_window_pinned_1330": full_ladder_pin,
            "settlement_truth_window": full_truth,
            "ladder_full_history": full_hist,
        },
    }

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "signal": "subhourly_trajectory",
        "cell": {"gap_lo": GAP_LO, "gap_hi": GAP_HI, "slope_threshold": SLOPE_THRESHOLD},
        "axis": "strict strike-crossing (settle > strike)",
        "deterministic": True, "no_lookahead": True, "scoring_only": True,
        "window": a.start, "full_start": a.full_start,
        "analysis_1_traded": analysis1,
        "analysis_2_full_sweep": analysis2,
        "notes": [
            "Predictions use only ASOS observations with valid<=T (13:30 station-local pin).",
            "Subhourly 'cross' == the traded UP/yes side on a greater market.",
            "Analysis 2 fans each station-day's nowcast across every ladder strike; a",
            "pair fires 'cross' if any pre-HOTD minute is in the anticipatory cell.",
            "No trades emitted; no live/paper/fusion wiring (Dan-gated).",
        ],
    }
    Path(a.out).write_text(json.dumps(result, indent=2, default=str))
    print(f"[F2] wrote {a.out}")

    # console summary
    print("\n=== TRADED (68) ===")
    print(json.dumps({k: v for k, v in analysis1.items() if k != "per_trade"}, indent=2))
    print("\n=== FULL SWEEPS ===")
    for k, v in analysis2["sweeps"].items():
        print(k, {kk: v[kk] for kk in
                  ("n_pairs", "n_fired", "n_abstained", "accuracy_on_fired", "cross_rate")})
        print("   contingency:", v["contingency"], "wilson:", v.get("accuracy_wilson_95"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
