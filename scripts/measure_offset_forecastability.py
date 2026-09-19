#!/usr/bin/env python3
"""
P0-H follow-up: is the ~4F warm-offset FORECASTABLE ahead of settlement,
or only post-hoc observable?

Deterministic, read-only, script-only, contemporaneous-only (no lookahead).

offset = strike_price (kalshi_temp) - settlement_temp (ghcn high_f)

Key design constraint: the offset's *features* must be available BEFORE the
settlement date (contemporaneous). The offset's *label* is only known after
settlement. We test whether a model trained on pre-settlement features can
predict the offset that will be realized.

Strict walk-forward / expanding-window evaluation to guarantee no lookahead.
"""

import sqlite3
import json
import math
from collections import defaultdict

import numpy as np

DB_SETTLE = "data/kalshi_settlements.db"
DB_GHCN = "data/ghcn_settlements.db"


def load_data():
    c = sqlite3.connect(DB_SETTLE)
    c.row_factory = sqlite3.Row
    rows = list(c.execute(
        "SELECT station, series, target_date, kalshi_temp FROM kalshi_settlements "
        "WHERE series LIKE 'KXHIGH%' ORDER BY target_date, station"
    ).fetchall())
    c.close()

    g = sqlite3.connect(DB_GHCN)
    g.row_factory = sqlite3.Row
    ghcn = {}
    for r in g.execute("SELECT station, date, high_f FROM ghcn_settlements").fetchall():
        ghcn[(r["station"], r["date"])] = r["high_f"]
    g.close()

    recs = []
    for r in rows:
        st = r["station"]
        d = r["target_date"]
        strike = r["kalshi_temp"]
        if (st, d) not in ghcn:
            continue
        real = ghcn[(st, d)]
        if strike is None or real is None:
            continue
        # skip TEST / non-physical
        if st == "TEST":
            continue
        # offset in F
        offset = strike - real
        recs.append({
            "station": st,
            "date": d,
            "strike": strike,
            "realized": real,
            "offset": offset,
        })

    recs.sort(key=lambda x: (x["date"], x["station"]))
    return recs


def main():
    recs = load_data()
    print(f"Loaded {len(recs)} HIGH settlement records (strike & realized matched)")

    if not recs:
        print("NO DATA. Abort.")
        return

    # ---- Overall offset stats ----
    offs = np.array([r["offset"] for r in recs])
    print("\n===== OFFSET DISTRIBUTION (overall) =====")
    print(f"  n={len(offs)}")
    print(f"  mean offset = {offs.mean():+.2f} F")
    print(f"  median offset = {np.median(offs):+.2f} F")
    print(f"  std = {offs.std():.2f} F")
    print(f"  p25/p75 = {np.percentile(offs,25):+.2f} / {np.percentile(offs,75):+.2f} F")
    print(f"  frac offset>0 (warm) = {(offs>0).mean():.3f}")
    print(f"  frac offset>2F = {(offs>2).mean():.3f}")
    print(f"  frac offset>4F = {(offs>4).mean():.3f}")

    # ---- By year (is it growing? established: 2023 -2F -> 2026 -4F) ----
    print("\n===== OFFSET BY YEAR =====")
    byyear = defaultdict(list)
    for r in recs:
        byyear[r["date"][:4]].append(r["offset"])
    for y in sorted(byyear):
        a = np.array(byyear[y])
        print(f"  {y}: n={len(a):5d}  mean={a.mean():+6.2f}F  median={np.median(a):+6.2f}F")

    # ---- By station ----
    print("\n===== OFFSET BY STATION (mean, n) =====")
    bystat = defaultdict(list)
    for r in recs:
        bystat[r["station"]].append(r["offset"])
    for s in sorted(bystat, key=lambda x: -np.mean(bystat[x])):
        a = np.array(bystat[s])
        print(f"  {s:5s}: n={len(a):5d}  mean={a.mean():+6.2f}F  std={a.std():5.2f}")

    # ---- By month (seasonality) ----
    print("\n===== OFFSET BY MONTH =====")
    bymon = defaultdict(list)
    for r in recs:
        bymon[int(r["date"][5:7])].append(r["offset"])
    for m in sorted(bymon):
        a = np.array(bymon[m])
        print(f"  month {m:02d}: n={len(a):5d}  mean={a.mean():+6.2f}F")

    # save a compact flat file for downstream deterministic reuse
    out = [{"station": r["station"], "date": r["date"], "offset": round(r["offset"], 3),
            "strike": r["strike"], "realized": r["realized"]} for r in recs]
    with open("reports/offset_dataset.json", "w") as f:
        json.dump(out, f)
    print(f"\nWrote reports/offset_dataset.json ({len(out)} rows)")


if __name__ == "__main__":
    main()
