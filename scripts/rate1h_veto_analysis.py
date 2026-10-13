#!/usr/bin/env python3
"""
rate1h_veto_analysis.py — evaluate a rate_1h pre-trade veto for Track B.

The veto: "do not enter this station-day if the rate_1h separation at the
entry window is below the station's median." Uses the rate_1h separation
reconstructed from the ASOS 1-min archive exactly as
scripts/sweep_intraday_rate1h_crossing.py does (the published JSON stores only
window-level separation, no per-station detail).

Reads the 68 usable rows (settlement_confirmed=1, canonical_outcome NOT NULL).
READ-ONLY. No DB writes.
Output: data/rate1h_veto_results.json
"""
from __future__ import annotations

import json
import math
import statistics
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
ASOS = REPO / "data" / "iem_asos_1min.db"
OUT = REPO / "data" / "rate1h_veto_results.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
MOM_LOOKBACK = 4
SEP_THRESHOLD = 1.0
LOW_PP = 5.0
HIGH_PP = 30.0
MIN_CELL = 10

_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def nearest_grid_hour(h):
    c = [g for g in GRID_HOURS if g <= h]
    return c[-1] if c else GRID_HOURS[0]


def build_separations():
    con = sqlite3.connect(f"file:{ASOS}?mode=ro", uri=True)
    cur = con.execute(
        "SELECT station, substr(valid,1,10), "
        "(cast(substr(valid,12,2) as integer)*60 + (cast(substr(valid,15,2) as integer)/15)*15), "
        "avg(tmpf) FROM observations WHERE tmpf IS NOT NULL GROUP BY station, 2, 3"
    )
    bm = {}
    for st, d, mod, m in cur:
        try:
            base = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dt = base + timedelta(minutes=mod)
        loc = dt.astimezone(ZoneInfo(_TZ.get(st, "America/New_York")))
        bm[(st, loc.date().isoformat(), mod)] = m
    con.close()

    by_sd = defaultdict(dict)
    for (st, ld, mod), m in bm.items():
        by_sd[(st, ld)][mod] = m

    samples = []
    for (st, ld), mods in by_sd.items():
        ordered = sorted(mods.items())
        minutes = [m for m, _ in ordered]
        means = [v for _, v in ordered]
        dmax = max(means)
        for h in GRID_HOURS:
            dec = h * 60
            if dec not in mods:
                continue
            i = minutes.index(dec)
            if i < MOM_LOOKBACK:
                continue
            rate = means[i] - sum(means[i - MOM_LOOKBACK:i]) / MOM_LOOKBACK
            hs = max(means[: i + 1])
            crossed = 1 if dmax > math.floor(hs) + 1.0 else 0
            samples.append((st, ld, h, rate, crossed))

    def sep_of(rows):
        up = [s for s in rows if s[3] >= SEP_THRESHOLD]
        dn = [s for s in rows if s[3] <= -SEP_THRESHOLD]
        pu = sum(s[4] for s in up) / len(up) if up else 0.0
        pd = sum(s[4] for s in dn) / len(dn) if dn else 0.0
        return round(100 * (pu - pd), 2), len(up), len(dn)

    per_cell = {}
    for st in {s[0] for s in samples}:
        for h in GRID_HOURS:
            sep, nu, nd = sep_of([s for s in samples if s[0] == st and s[2] == h])
            per_cell[(st, h)] = {"sep_pp": sep, "n_up": nu, "n_down": nd}

    per_station = {}
    for st in {s[0] for s in samples}:
        sep, nu, nd = sep_of([s for s in samples if s[0] == st])
        # median of the station's per-window separations = the veto baseline
        win_seps = [per_cell[(st, h)]["sep_pp"] for h in GRID_HOURS
                    if (st, h) in per_cell
                    and per_cell[(st, h)]["n_up"] >= MIN_CELL
                    and per_cell[(st, h)]["n_down"] >= MIN_CELL]
        per_station[st] = {
            "sep_pp": sep, "n_up": nu, "n_down": nd,
            "median_window_sep_pp": round(statistics.median(win_seps), 2) if win_seps else None,
            "n_windows_used": len(win_seps),
        }
    return per_cell, per_station


def zone_of(sep_pp):
    if sep_pp is None:
        return "unknown"
    if sep_pp < LOW_PP:
        return "low(<5pp)"
    if sep_pp <= HIGH_PP:
        return "mid(5-30pp)"
    return "high(>30pp)"


def main():
    per_cell, per_station = build_separations()

    book = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    book.row_factory = sqlite3.Row
    trades = [dict(r) for r in book.execute(
        "SELECT * FROM trades WHERE settlement_confirmed=1 AND canonical_outcome IS NOT NULL "
        "ORDER BY trade_date_utc, id"
    )]
    book.close()

    rows = []
    def seps_for(st, win):
        cell = per_cell.get((st, win))
        if cell and cell["n_up"] >= MIN_CELL and cell["n_down"] >= MIN_CELL:
            return cell["sep_pp"], f"station-window {win:02d}Z"
        ps = per_station.get(st)
        if ps and ps["n_up"] >= MIN_CELL and ps["n_down"] >= MIN_CELL:
            return ps["sep_pp"], "station-all-windows"
        return None, "unavailable"

    contingency = defaultdict(lambda: {"correct": 0, "incorrect": 0})
    helped = []      # low sep & incorrect
    fine = []        # high sep & correct
    missed = []      # high sep & incorrect
    veto_saved = []  # blocked by median veto & incorrect
    veto_missed = [] # blocked by median veto & correct

    for t in trades:
        st = t["station"]
        ts = datetime.fromisoformat(t["created_at_utc"])
        win = nearest_grid_hour(ts.hour)
        sep_pp, src = seps_for(st, win)
        z = zone_of(sep_pp)
        median_sep = per_station.get(st, {}).get("median_window_sep_pp")
        veto_blocked = (sep_pp is not None and median_sep is not None and sep_pp < median_sep)
        outcome = t["canonical_outcome"]
        contingency[z][outcome] += 1

        entry = {
            "trade_uuid": t["trade_uuid"], "station": st, "date": t["trade_date_utc"],
            "window": f"{win:02d}Z", "sep_pp": sep_pp, "sep_source": src, "zone": z,
            "station_median_sep_pp": median_sep, "veto_blocked": veto_blocked,
            "outcome": outcome,
        }
        rows.append(entry)

        if sep_pp is not None and sep_pp < LOW_PP and outcome == "incorrect":
            helped.append(entry)
        if sep_pp is not None and sep_pp > HIGH_PP and outcome == "correct":
            fine.append(entry)
        if sep_pp is not None and sep_pp > HIGH_PP and outcome == "incorrect":
            missed.append(entry)
        if veto_blocked and outcome == "incorrect":
            veto_saved.append(entry)
        if veto_blocked and outcome == "correct":
            veto_missed.append(entry)

    actual_winners = sum(1 for r in rows if r["outcome"] == "correct")
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "usable_trades": len(rows),
        "actual_book": {"correct": actual_winners, "incorrect": len(rows) - actual_winners},
        "veto_rule": ("do not enter this station-day if the rate_1h separation at the "
                      "entry window is below the station's median window separation"),
        "zone_thresholds_pp": {"low": LOW_PP, "high": HIGH_PP},
        "contingency_zone_x_outcome": {z: dict(v) for z, v in contingency.items()},
        "flags": {
            "veto_would_have_helped (sep<5pp & incorrect)": len(helped),
            "veto_would_have_been_fine (sep>30pp & correct)": len(fine),
            "veto_would_have_missed (sep>30pp & incorrect)": len(missed),
        },
        "median_veto": {
            "blocked_total": len(veto_saved) + len(veto_missed),
            "would_have_saved_losers": len(veto_saved),
            "would_have_blocked_winners": len(veto_missed),
            "net_pnl_proxy": f"saved {len(veto_saved)} losers vs {len(veto_missed)} winners blocked",
            "blocked_winners": [r["trade_uuid"] for r in veto_missed],
        },
        "station_medians": {st: v["median_window_sep_pp"] for st, v in per_station.items()},
        "per_trade": rows,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"usable trades: {len(rows)} | actual correct {actual_winners}/{len(rows)}")
    print("contingency (zone x outcome):")
    for z in ("low(<5pp)", "mid(5-30pp)", "high(>30pp)", "unknown"):
        if z in contingency:
            c = contingency[z]
            print(f"  {z:12} correct={c['correct']:3} incorrect={c['incorrect']:3}")
    print(f"veto helped (sep<5 & incorrect): {len(helped)}")
    print(f"veto fine   (sep>30 & correct): {len(fine)}")
    print(f"veto missed (sep>30 & incorrect): {len(missed)}")
    print(f"median veto: blocked {len(veto_saved)+len(veto_missed)} "
          f"({len(veto_saved)} losers saved, {len(veto_missed)} winners blocked)")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
