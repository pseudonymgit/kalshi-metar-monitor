#!/usr/bin/env python3
"""
dual_grade_prototype.py — grade each settled trade on two independent axes.

Axis 1 — FINANCIAL (execution cost):
    spread / entry_mid, using entry bid/ask/mid from
    `data/kalshi_market_snapshots.db` (nearest snapshot <= entry_time).
        F  > 0.25
        B  0.15 .. 0.25
        A  0.10 .. 0.15
        S  < 0.05
    NOTE: the spec's A band (0.10-0.15) leaves 0.05-0.10 unspecified. The band is
    completed to A (0.05-0.15) so the scale stays monotone and total; the count of
    rows that land in the completed sub-band is reported as `fin_completed_band`.

Axis 2 — METEOROLOGICAL (signal separation at the decision window):
    separation (pp) of the station's UTC decision window, from
    `data/sweep_intraday_rate1h_crossing.json` (`sep_pp`, ±0.5 °F threshold).
    The sweep publishes per-WINDOW separation, not per-station, so every trade is
    graded on its window's separation.
        F  < 5 pp
        C  5 .. 15 pp
        B  15 .. 30 pp
        A  30 .. 50 pp
        S  > 50 pp
    Downgrade one letter if the GEFS-analog gap |p_gefs - p_analog| > 0.10.

Composite = MIN(financial, meteorological) on the quality scale F<C<B<A<S.
    (met=A + fin=F -> composite F -> no-go.)

Output: data/dual_grade.json
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
SNAP = REPO / "data" / "kalshi_market_snapshots.db"
SWEEP = REPO / "data" / "sweep_intraday_rate1h_crossing.json"
OUT = REPO / "data" / "dual_grade.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
GEFS_GAP_DOWNGRADE = 0.10
FIN_COMPLETED_BAND = (0.05, 0.10)   # spec gap, completed to A
RANK = {"F": 1, "C": 2, "B": 3, "A": 4, "S": 5}
POORER = {"F": "F", "C": "F", "B": "C", "A": "B", "S": "A"}  # one-letter downgrade


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def nearest_window(hour: int) -> int:
    c = [g for g in GRID_HOURS if g <= hour]
    return c[-1] if c else GRID_HOURS[0]


def fin_grade(ratio: float) -> str:
    if ratio > 0.25:
        return "F"
    if ratio >= 0.15:
        return "B"
    if ratio >= 0.10:
        return "A"
    if ratio < 0.05:
        return "S"
    return "A"  # completed 0.05-0.10 band


def met_grade(sep_pp: float) -> str:
    if sep_pp < 5:
        return "F"
    if sep_pp < 15:
        return "C"
    if sep_pp < 30:
        return "B"
    if sep_pp <= 50:
        return "A"
    return "S"


def main() -> None:
    sweep = json.loads(SWEEP.read_text())
    win_sep = {
        h: sweep["windows"][f"{h:02d}Z"]["thresholds"]["\u00b10.5"]["separation"]["sep_pp"]
        for h in GRID_HOURS
        if f"{h:02d}Z" in sweep["windows"]
    }

    book = _ro(BOOK)
    trades = [dict(r) for r in book.execute("SELECT * FROM trades WHERE status='closed'")]
    book.close()
    snap = _ro(SNAP)

    graded = []
    fin_counts, met_counts, comp_counts = {}, {}, {}
    completed_band = 0
    downgrades = 0

    for r in trades:
        et = r.get("created_at_utc")
        ts = datetime.fromisoformat(et)
        w = nearest_window(ts.hour)

        row = snap.execute(
            "SELECT snapshot_time, yes_bid, yes_ask, mid_price FROM market_snapshots "
            "WHERE market_ticker=? AND snapshot_time<=? ORDER BY snapshot_time DESC LIMIT 1",
            (r.get("market_ticker"), et),
        ).fetchone()
        if not row or not row["mid_price"]:
            continue
        mid = float(row["mid_price"])
        bid = float(row["yes_bid"]) if row["yes_bid"] is not None else None
        ask = float(row["yes_ask"]) if row["yes_ask"] is not None else None
        spread = (ask - bid) if (ask is not None and bid is not None) else None
        if spread is None:
            continue
        ratio = spread / mid
        if FIN_COMPLETED_BAND[0] <= ratio < FIN_COMPLETED_BAND[1]:
            completed_band += 1
        fg = fin_grade(ratio)

        sep = win_sep.get(w)
        mg0 = met_grade(sep) if sep is not None else None
        gap = None
        if r.get("p_gefs") is not None and r.get("p_analog") is not None:
            gap = abs(r["p_gefs"] - r["p_analog"])
        mg = mg0
        downgraded = False
        if mg0 is not None and gap is not None and gap > GEFS_GAP_DOWNGRADE:
            mg = POORER[mg0]
            downgraded = True
            downgrades += 1

        comp = None
        if fg and mg:
            comp = fg if RANK[fg] <= RANK[mg] else mg

        fin_counts[fg] = fin_counts.get(fg, 0) + 1
        met_counts[mg] = met_counts.get(mg, 0) + 1
        comp_counts[comp] = comp_counts.get(comp, 0) + 1

        graded.append({
            "trade_uuid": r.get("trade_uuid"),
            "station": r.get("station"),
            "trade_date_utc": r.get("trade_date_utc"),
            "window_utc": f"{w:02d}Z",
            "settlement_outcome": r.get("settlement_outcome"),
            "realized_pnl": r.get("realized_pnl"),
            "financial": {
                "entry_mid": mid, "entry_bid": bid, "entry_ask": ask,
                "spread": round(spread, 4),
                "spread_over_mid": round(ratio, 4),
                "grade": fg,
                "completed_band": FIN_COMPLETED_BAND[0] <= ratio < FIN_COMPLETED_BAND[1],
            },
            "meteorological": {
                "window_sep_pp": sep,
                "gefs_analog_gap": round(gap, 4) if gap is not None else None,
                "grade_before_downgrade": mg0,
                "downgraded": downgraded,
                "grade": mg,
            },
            "composite": comp,
            "no_go": comp == "F",
        })

    snap.close()

    example = [g for g in graded if g["meteorological"]["grade"] == "A"
               and g["financial"]["grade"] == "F"]

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_graded": len(graded),
        "axes": {
            "financial": "spread/entry_mid; F>0.25 B .15-.25 A .10-.15 S<.05 "
                         "(0.05-0.10 completed to A)",
            "meteorological": "window sep_pp; F<5 C 5-15 B 15-30 A 30-50 S>50; "
                              "downgrade one letter if GEFS-analog gap > 0.10",
            "composite": "MIN of the two axes on scale F<C<B<A<S",
        },
        "window_sep_pp": win_sep,
        "grade_distributions": {
            "financial": fin_counts,
            "meteorological": met_counts,
            "composite": comp_counts,
        },
        "fin_completed_band_count": completed_band,
        "gefs_downgrade_count": downgrades,
        "no_go_count": sum(1 for g in graded if g["no_go"]),
        "met_A_fin_F_examples": [g["trade_uuid"] for g in example],
        "graded": graded,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"dual grade: {len(graded)} trades")
    print(f"  financial  {fin_counts}")
    print(f"  meteo      {met_counts}")
    print(f"  composite  {comp_counts}")
    print(f"  completed 0.05-0.10 band -> A: {completed_band}")
    print(f"  gefs downgrades: {downgrades}")
    print(f"  no-go (composite F): {result['no_go_count']}")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
