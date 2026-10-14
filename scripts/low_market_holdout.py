#!/usr/bin/env python3
"""low_market_holdout.py — LOW-market parity holdout (2026-10-14).

Implements the design in docs/plans/LOW-MARKET-HOLDOUT-2026-10-13.md: a
30-trade LOW holdout under the SAME gate as HIGH, with a simpler exit
(auto_close_h1 only). NOT wired to the live bridge — this is the script + test
that produces the pre-registered measurement.

Parity with HIGH
----------------
  * Same EV-strike selector (`core.strike_selection_ev.select_strike`).
  * Same fee model + execution cost (F1) and grade ladder (F7: S/A/B/C/D, F=no entry).
  * Same 13:30 station-local decision pin.
  * Same per-station calibrated ceiling for the fusion.
  * Exit profile: {"auto_close_h1"} only (no TP/SL/flip).

LOW-specific modelling
----------------------
  * Direction is MIRRORED: LOW YES is ITM iff settlement < strike. The
    probability is P(low <= X), built from GHCN daily lows with the SAME analog
    machinery as the HIGH CDF (high_so_far -> running-low-at-pin truncation).
  * Side mapping is direction-aware per market_type: a signal that the low is
    BELOW the strike buys YES; otherwise NO.

Pre-registered postconditions (frozen before evaluation):
  P1 exactly 30 trades · P2 win rate >= 60% · P3 net EV > 0 per contract.

Read-only on every book. No live-book write. Usage:
    .venv/bin/python scripts/low_market_holdout.py --json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.strike_selection_ev import select_strike, kalshi_fee  # noqa: E402
from core.intraday.honest_calibration import (  # noqa: E402
    honest_calibrated_p, station_ceiling,
)
from core.settlement_labels import strike_crossing_yes  # noqa: E402

CSV = REPO / "artifacts" / "low-market-backfill" / "low_entry_price_series_2026-10-13.csv"
GHCN = REPO / "data" / "ghcn_settlements.db"
METAR = REPO / "data" / "metar_backfill.db"
OUT = REPO / "artifacts" / "low-market-backfill" / "low_holdout_2026-10-14.json"

PIN_LOCAL = 13 * 60 + 30        # 13:30 station-local decision pin
HOLDOUT_N = 30
MIN_WIN_RATE = 0.60
EXEC_COST = 0.0205              # F1 fallback execution cost (half-spread+slippage)
MIN_EDGE = 0.03
EXIT_PROFILE = ["auto_close_h1"]


# ── data ─────────────────────────────────────────────────────────────────────
def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_low_quotes() -> Dict[Tuple[str, str], List[dict]]:
    """{(station, date): [quote,...]} keyed off the KXLOWT backfill series."""
    out: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    with open(CSV) as f:
        for r in csv.DictReader(f):
            out[(r["station"], r["target_date"])].append({
                "ticker": r["ticker"], "strike": float(r["strike"]),
                "mid": float(r["mid"]), "kind": r["kind"],
                "dist_pin": int(r["dist_pin_minutes"]),
                "running_low": (float(r["running_low_at_pin"])
                                if r["running_low_at_pin"] else None),
                "has_truth": r["has_low_truth"] == "1",
            })
    return out


def load_low_truth() -> Dict[Tuple[str, str], float]:
    """{(station, date): canonical LOW settlement temp F} (trustworthy GHCN)."""
    c = _ro(METAR)
    try:
        rows = c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='LOW' AND settlement_temp_f IS NOT NULL "
            "AND (trustworthy IS NULL OR trustworthy=1)").fetchall()
    finally:
        c.close()
    return {(r[0], r[1]): float(r[2]) for r in rows}


def load_ghcn_lows(station: str) -> Tuple[List[str], List[Optional[float]]]:
    c = _ro(GHCN)
    try:
        rows = c.execute(
            "SELECT date, low_f FROM ghcn_settlements WHERE station=? ORDER BY date",
            (station,)).fetchall()
    finally:
        c.close()
    return [r[0][:10] for r in rows], [r[1] for r in rows]


# ── mirrored LOW analog CDF ──────────────────────────────────────────────────
def low_analog_pdf(station: str, target_date: str, running_low: Optional[float],
                   top_k: int = 200, exclude_days: int = 10) -> Optional[List[float]]:
    """Analog daily-lows strictly before the target (calendar-excluded).

    Mirrors `core.intraday.analog_cdf` but over GHCN `low_f`. Returns the analog
    sample list, or None when the target is absent from the archive.
    """
    dates, lows = load_ghcn_lows(station)
    try:
        tgt = dates.index(target_date)
    except ValueError:
        return None
    day = dates[tgt]
    doy_t = _doy(day)
    # simple seasonal + level-neighbour scan on the low series
    cands = []
    for i in range(tgt - exclude_days - 1, -1, -1):
        if lows[i] is None:
            continue
        dd = abs(doy_t - _doy(dates[i]))
        dd = min(dd, 366 - dd)
        cands.append((dd, i))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], -t[1]))
    idx = [i for _, i in cands[:top_k]]
    return [float(lows[i]) for i in idx if lows[i] is not None]


def _doy(ds: str) -> int:
    return date_ordinal_doy(ds)


def date_ordinal_doy(ds: str) -> int:
    from datetime import datetime
    return datetime.strptime(ds[:10], "%Y-%m-%d").timetuple().tm_yday


def p_low_le(samples: List[float], x: float, running_low: Optional[float]) -> float:
    """Empirical P(low <= x) with running-low truncation.

    Once the day's running low is already below the strike, the low is locked
    in the money for the DOWN side; the conditional CDF respects that.
    """
    n = len(samples)
    if n == 0:
        return 0.0 if (running_low is None or x >= running_low) else 1.0
    if running_low is not None and x >= running_low:
        # running low is already <= x -> the day's min is <= x with certainty
        return 1.0
    cnt = sum(1 for v in samples if v <= x)
    return cnt / n


# ── gate (HIGH parity) ───────────────────────────────────────────────────────
GRADE_EDGE = {"S": 0.12, "A": 0.09, "B": 0.06, "C": 0.03, "D": 0.01}


def grade_edge(edge: float) -> str:
    for g in ("S", "A", "B", "C", "D"):
        if edge >= GRADE_EDGE[g]:
            return g
    return "F"


def evaluate_station_day(station: str, target_date: str,
                         quotes: List[dict],
                         truth: Optional[float]) -> Optional[dict]:
    """Apply the gate to one LOW station-day; return the selected trade or None."""
    running_low = next((q["running_low"] for q in quotes if q["running_low"] is not None), None)
    samples = low_analog_pdf(station, target_date, running_low)
    if not samples:
        return None
    # Build a strike grid in Kalshi's {strike: yes_price} shape.
    ladder = []
    for q in quotes:
        if q["kind"] != "T":       # only the T (greater) books map to P(low<=X) compares
            continue
        p = p_low_le(samples, q["strike"], running_low)
        ladder.append({"strike": q["strike"], "yes_price": q["mid"],
                       "no_price": round(1.0 - q["mid"], 6), "ticker": q["ticker"],
                       "_p": p})
    if not ladder:
        return None

    def p_fn(x: float) -> float:
        row = next((s for s in ladder if abs(s["strike"] - x) < 1e-9), None)
        return row["_p"] if row else 0.0

    picked = select_strike(0.0, 1.0, ladder, min_edge=MIN_EDGE, p_fn=p_fn,
                           cost_model=_Cost(EXEC_COST), fail_closed=False)
    if not picked or picked.get("ev", 0) <= 0:
        return None
    strike = picked["strike"]
    row = next(s for s in ladder if abs(s["strike"] - strike) < 1e-9)
    p_low = row["_p"]
    side_yes = picked["side"] == "yes"
    # The trade's probability for the booked side (mirrored for NO).
    our_prob = p_low if side_yes else 1.0 - p_low
    mkt = row["yes_price"] if side_yes else row["no_price"]
    # Honest calibration at the per-station ceiling.
    cal = honest_calibrated_p(our_prob, station_ceiling(station))
    marked_edge = cal - mkt - EXEC_COST - kalshi_fee(mkt, cal)
    grade = grade_edge(marked_edge)
    if grade == "F":
        return None
    # Outcome: LOW YES = low < strike (strict); NO = not.
    won = None
    if truth is not None:
        yes_wins = strike_crossing_yes(truth, strike, "LOW")
        won = yes_wins if side_yes else (not yes_wins)
    return {"station": station, "date": target_date, "strike": strike,
            "side": "yes" if side_yes else "no", "ticker": row["ticker"],
            "p_low": round(p_low, 4), "calibrated_p": cal, "market_price": mkt,
            "our_prob": round(our_prob, 4), "grade": grade,
            "marked_edge": round(marked_edge, 4),
            "ev": picked["ev"], "running_low": running_low,
            "settlement_low": truth, "won": won}


class _Cost:
    """Minimal F1 cost model: flat half-spread + slippage per contract."""
    def __init__(self, rate: float):
        self.rate = rate
        self.slippage = 0.0
    def get_dynamic_spread(self, ticker: str) -> float:
        return self.rate * 2.0


# ── holdout assembly ─────────────────────────────────────────────────────────
def run_holdout(limit: int = HOLDOUT_N) -> dict:
    quotes = load_low_quotes()
    truth = load_low_truth()
    # Frozen selection: first N qualifying station-days by ascending date.
    rows = []
    for (st, ds) in sorted(quotes, key=lambda k: (k[1], k[0])):
        qs = quotes[(st, ds)]
        t = truth.get((st, ds))
        ev = evaluate_station_day(st, ds, qs, t)
        if ev is not None:
            rows.append(ev)
    selected = rows[:limit]
    settled = [r for r in selected if r["won"] is not None]
    wins = sum(1 for r in settled if r["won"])
    win_rate = (wins / len(settled)) if settled else None
    # Net P&L over the selected book (settlement P&L, auto-close profile),
    # summed and expressed per settled contract.
    net_total = 0.0
    for r in settled:
        mkt = r["market_price"]
        payoff = 1.0 if r["won"] else 0.0
        net_total += payoff - mkt - kalshi_fee(mkt, r["calibrated_p"]) * 2
    net_ev = (net_total / len(settled)) if settled else 0.0
    p1 = len(selected) == HOLDOUT_N
    p2 = win_rate is not None and win_rate >= MIN_WIN_RATE
    p3 = net_ev > 0
    return {
        "market_type": "LOW",
        "gate": "HIGH-parity (F1 cost, F7 grades, 13:30 pin, per-station ceiling)",
        "exit_profile": EXIT_PROFILE,
        "pin_local": "13:30",
        "holdout_n_target": HOLDOUT_N,
        "n_qualifying": len(rows),
        "n_selected": len(selected),
        "n_settled": len(settled),
        "wins": wins,
        "win_rate": (round(win_rate, 4) if win_rate is not None else None),
        "net_ev_per_contract": round(net_ev, 4),
        "net_pnl_total": round(net_total, 4),
        "pass": {"P1_n_30": p1, "P2_win_ge_60": p2, "P3_net_ev_gt_0": p3},
        "PASS_ALL": bool(p1 and p2 and p3),
        "trades": selected,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=HOLDOUT_N)
    args = ap.parse_args()
    result = run_holdout(limit=args.limit)
    print(json.dumps(result, indent=2) if args.json else
          f"LOW holdout: {result['n_selected']} selected / {result['n_settled']} settled, "
          f"win_rate={result['win_rate']}, net_ev={result['net_ev_per_contract']}, "
          f"PASS={result['PASS_ALL']}")
    if args.json:
        OUT.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
