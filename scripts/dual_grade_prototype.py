#!/usr/bin/env python3
"""
dual_grade_prototype.py — dual-axis trade grading prototype.

Two INDEPENDENT confidence axes; the composite is the WORST of the two, so a
trade strong on one axis can never mask weakness on the other.

  Axis 1 — FINANCIAL confidence (can we afford the round trip?)
    * spread_ratio = (yes_ask - yes_bid) / entry_mid, from the snapshot
      nearest (<=) the entry instant in data/kalshi_market_snapshots.db
    * bankroll depth  = position_size_usd / current bankroll
    axis grade = worst(spread grade, depth grade)

  Axis 2 — METEOROLOGICAL confidence (does the weather support it?)
    * rate_1h separation (pp) at the station's entry window, from the ASOS
      1-min archive (mirrors scripts/sweep_intraday_rate1h_crossing.py)
    * regional class (East/West) from longitude as a prior/fallback
    * downgrade one step when |p_gefs - p_analog| > 0.10

READ-ONLY. Does not modify any DB.
Output: data/dual_grade_results.json
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
SNAP = REPO / "data" / "kalshi_market_snapshots.db"
ASOS = REPO / "data" / "iem_asos_1min.db"
STATION_META = REPO / "data" / "station_metadata.json"
OUT = REPO / "data" / "dual_grade_results.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
MOM_LOOKBACK = 4
SEP_THRESHOLD = 1.0
MIN_CELL = 10                 # min up/down samples for a station-window cell
DOWNGRADE_GAP = 0.10

ORDER = ["F", "C", "B", "A", "S"]         # worst -> best
DOWN = {"F": "F", "C": "F", "B": "C", "A": "B", "S": "A"}

_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def worst(a, b):
    return a if ORDER.index(a) <= ORDER.index(b) else b


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def nearest_grid_hour(h):
    c = [g for g in GRID_HOURS if g <= h]
    return c[-1] if c else GRID_HOURS[0]


# ---------- regional class ----------
def region_of(station, meta):
    lon = meta.get(station, {}).get("longitude")
    if lon is None:
        return "East"
    return "West" if lon < -100.0 else "East"


# ---------- rate_1h separation ----------
def build_separations():
    """Return (per_cell, per_station, per_region) separations in pp at +/-1.0."""
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
            prior = means[i - MOM_LOOKBACK:i]
            rate = means[i] - sum(prior) / len(prior)
            hs = max(means[: i + 1])
            strike = math.floor(hs) + 1.0
            crossed = 1 if dmax > strike else 0
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
            rows = [s for s in samples if s[0] == st and s[2] == h]
            sep, nu, nd = sep_of(rows)
            per_cell[(st, h)] = {"sep_pp": sep, "n_up": nu, "n_down": nd}

    per_station = {}
    for st in {s[0] for s in samples}:
        sep, nu, nd = sep_of([s for s in samples if s[0] == st])
        per_station[st] = {"sep_pp": sep, "n_up": nu, "n_down": nd}

    region_vals = defaultdict(list)
    for st, v in per_station.items():
        region_vals[st[1] == "K"].append(None)  # placeholder never used
    return per_cell, per_station, samples


def grade_financial(spread_ratio, depth_frac):
    if spread_ratio is None:
        sg = "F"
    elif spread_ratio > 0.25:
        sg = "F"
    elif spread_ratio >= 0.15:
        sg = "C"
    elif spread_ratio >= 0.10:
        sg = "B"
    elif spread_ratio >= 0.05:
        sg = "A"
    else:
        sg = "S"
    dg = "F" if depth_frac > 0.10 else "S"
    return worst(sg, dg), sg, dg


def grade_met(sep_pp, gefs_gap):
    if sep_pp is None:
        g = "F"
    elif sep_pp < 5:
        g = "F"
    elif sep_pp < 15:
        g = "C"
    elif sep_pp < 30:
        g = "B"
    elif sep_pp <= 50:
        g = "A"
    else:
        g = "S"
    downgraded = False
    if gefs_gap is not None and gefs_gap > DOWNGRADE_GAP:
        g = DOWN[g]
        downgraded = True
    return g, downgraded


def main():
    meta = json.loads(STATION_META.read_text())
    meta = {k: v for k, v in meta.items() if isinstance(v, dict)}
    bank = _ro(BOOK).execute(
        "SELECT closing_balance FROM daily_balances ORDER BY date_utc DESC LIMIT 1"
    ).fetchone()[0]
    bank = float(bank)

    per_cell, per_station, samples = build_separations()
    # regional prior = median station sep within the region
    regions = defaultdict(list)
    for st, v in per_station.items():
        regions[region_of(st, meta)].append(v["sep_pp"])
    region_prior = {r: sorted(v)[len(v) // 2] for r, v in regions.items() if v}

    book = _ro(BOOK)
    trades = [dict(r) for r in book.execute(
        "SELECT * FROM trades WHERE settlement_confirmed=1 AND canonical_outcome IS NOT NULL "
        "ORDER BY trade_date_utc, id"
    )]
    snp = _ro(SNAP)

    results = []
    n_skip = n_reduce = n_enter = 0
    for t in trades:
        st = t["station"]
        entry_iso = t["created_at_utc"]
        ts = datetime.fromisoformat(entry_iso)
        win = nearest_grid_hour(ts.hour)
        ld = ts.astimezone(ZoneInfo(_TZ.get(st, "America/New_York"))).date().isoformat()

        # --- Axis 1 ---
        row = snp.execute(
            "SELECT snapshot_time, yes_bid, yes_ask, mid_price FROM market_snapshots "
            "WHERE market_ticker=? AND snapshot_time<=? ORDER BY snapshot_time DESC LIMIT 1",
            (t["market_ticker"], entry_iso),
        ).fetchone()
        spread_ratio = None
        if row and row["mid_price"]:
            mid = float(row["mid_price"])
            if mid > 0:
                spread_ratio = (float(row["yes_ask"]) - float(row["yes_bid"])) / mid
        pos = t["position_size_usd"] or 0.0
        depth_frac = pos / bank if bank else 0.0
        fin, sg, dg = grade_financial(spread_ratio, depth_frac)

        # --- Axis 2 ---
        cell = per_cell.get((st, win))
        sep_src = None
        if cell and cell["n_up"] >= MIN_CELL and cell["n_down"] >= MIN_CELL:
            sep_pp = cell["sep_pp"]; sep_src = f"station-window {win:02d}Z"
        elif per_station.get(st) and per_station[st]["n_up"] >= MIN_CELL and per_station[st]["n_down"] >= MIN_CELL:
            sep_pp = per_station[st]["sep_pp"]; sep_src = "station-all-windows"
        else:
            sep_pp = region_prior.get(region_of(st, meta)); sep_src = "region-prior"
        gefs_gap = None
        if t["p_gefs"] is not None and t["p_analog"] is not None:
            gefs_gap = abs(float(t["p_gefs"]) - float(t["p_analog"]))
        met, downgraded = grade_met(sep_pp, gefs_gap)

        composite = worst(fin, met)
        if composite == "F":
            gate = "skip"
        elif composite == "C":
            gate = "reduce"
        else:
            gate = "enter"
        if gate == "skip":
            n_skip += 1
        elif gate == "reduce":
            n_reduce += 1
        else:
            n_enter += 1

        results.append({
            "trade_uuid": t["trade_uuid"],
            "station": st,
            "date": t["trade_date_utc"],
            "entry_window": f"{win:02d}Z",
            "region": region_of(st, meta),
            "spread_ratio": round(spread_ratio, 4) if spread_ratio is not None else None,
            "depth_frac": round(depth_frac, 4),
            "fin_spread_grade": sg,
            "fin_depth_grade": dg,
            "fin_grade": fin,
            "rate1h_sep_pp": sep_pp,
            "sep_source": sep_src,
            "gefs_analog_gap": round(gefs_gap, 4) if gefs_gap is not None else None,
            "met_downgraded": downgraded,
            "met_grade": met,
            "composite": composite,
            "gate": gate,
            "actual_outcome": t["canonical_outcome"],
        })
    snp.close()

    # grade distribution
    from collections import Counter
    comp_dist = Counter(r["composite"] for r in results)
    fin_dist = Counter(r["fin_grade"] for r in results)
    met_dist = Counter(r["met_grade"] for r in results)
    # how the gate lines up with realised outcomes
    enter_ok = sum(1 for r in results if r["gate"] == "enter" and r["actual_outcome"] == "correct")
    enter_n = sum(1 for r in results if r["gate"] == "enter")
    skip_bad = sum(1 for r in results if r["gate"] == "skip" and r["actual_outcome"] == "incorrect")
    skip_n = sum(1 for r in results if r["gate"] == "skip")

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "bankroll_usd": round(bank, 2),
        "settled_trades": len(results),
        "region_prior_sep_pp": region_prior,
        "axes": {
            "financial": {"F": "spread>0.25 or depth>0.10", "C": "0.15-0.25",
                          "B": "0.10-0.15", "A": "0.05-0.10", "S": "<0.05 & depth<=0.10"},
            "meteorological": {"F": "sep<5pp", "C": "5-15", "B": "15-30",
                               "A": "30-50", "S": ">50", "downgrade": "|p_gefs-p_analog|>0.10"},
        },
        "composite_distribution": dict(comp_dist),
        "fin_distribution": dict(fin_dist),
        "met_distribution": dict(met_dist),
        "gate": {"enter": n_enter, "reduce": n_reduce, "skip": n_skip},
        "gate_quality": {
            "entered_that_were_correct": f"{enter_ok}/{enter_n}",
            "skipped_that_were_incorrect": f"{skip_bad}/{skip_n}",
        },
        "per_trade": results,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"settled trades: {len(results)} | bankroll ${bank:.2f}")
    print(f"composite dist: {dict(comp_dist)}")
    print(f"gate: enter={n_enter} reduce={n_reduce} skip={n_skip}")
    print(f"enter&correct {enter_ok}/{enter_n} | skip&incorrect {skip_bad}/{skip_n}")
    print(f"{'trade':38} {'st':5} {'win':4} {'Fin':3} {'Met':3} {'Comp':4} {'gate':6} {'outcome'}")
    for r in results:
        print(f"{r['trade_uuid'][:36]:38} {r['station']:5} {r['entry_window']:4} "
              f"{r['fin_grade']:3} {r['met_grade']:3} {r['composite']:4} {r['gate']:6} {r['actual_outcome']}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
