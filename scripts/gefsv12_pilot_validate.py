#!/usr/bin/env python3
"""GEFSv12 reforecast pilot validation: Tmax vs GHCN observed daily high.

Read-only. Aligns each reforecast init's forecast-day d (00Z init + (d-1) days,
the collector's 1-based 24h window) to the GHCN calendar day and reports the
correlation of reforecast Tmax against observed GHCN high_f.

Usage: .venv-gefs/bin/python scripts/gefsv12_pilot_validate.py [STATION]
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "gefsv12_reforecast.db"
GHCN = REPO / "data" / "ghcn_settlements.db"


def main():
    station = sys.argv[1] if len(sys.argv) > 1 else "KNYC"
    g = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    h = sqlite3.connect(f"file:{GHCN}?mode=ro", uri=True)
    rows = list(g.execute(
        "SELECT init_date, member, forecast_day, tmax_f FROM gefsv12_reforecast "
        "WHERE station=? AND tmax_f IS NOT NULL ORDER BY init_date, member, forecast_day",
        (station,)))
    pairs = []
    for init, mem, day, tmax in rows:
        d = date.fromisoformat(init) + timedelta(days=day - 1)
        hr = h.execute("SELECT high_f FROM ghcn_settlements WHERE station=? AND date=?",
                       (station, d.isoformat())).fetchone()
        if hr and hr[0] is not None:
            pairs.append((tmax, float(hr[0])))
    n = len(pairs)
    if n < 3:
        print(f"insufficient pairs: {n}"); return
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    mx, my = sum(xs)/n, sum(ys)/n
    cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    vx = sum((x-mx)**2 for x in xs); vy = sum((y-my)**2 for y in ys)
    r = cov/(vx*vy)**0.5 if vx > 0 and vy > 0 else float("nan")
    mae = sum(abs(x-y) for x, y in zip(xs, ys))/n
    bias = sum(x-y for x, y in zip(xs, ys))/n
    rmse = (sum((x-y)**2 for x, y in zip(xs, ys))/n)**0.5
    print(f"{station}: pairs={n}  r={r:.3f}  MAE={mae:.2f}F  bias={bias:+.2f}F  RMSE={rmse:.2f}F")


if __name__ == "__main__":
    main()
