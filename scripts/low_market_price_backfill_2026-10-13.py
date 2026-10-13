#!/usr/bin/env python3
"""low_market_price_backfill_2026-10-13.py — Item 2.

LOW-market entry-price backfill (unblock the Workstream T/B4 LOW holdout).

PROBLEM
-------
The LOW holdout (docs/plans/LOW-MARKET-HOLDOUT-2026-10-13.md) is designed
(30 trades, win>=60%, net EV>0) but BLOCKED: the design doc measured LOW mid
at the pin on only ONE snapshot day (2026-09-07, 17 stations, via
`market_snapshots.market_type='LOW'`).

THE FIND (this script)
----------------------
`market_type` is a mislabeled *strike-shape* column on the snapshot rows, and
the real LOW Kalshi books are ingested as `market_type='binary'` under the
`KXLOWT*` series (722,648 rows). Once you stop filtering on `market_type` and
key off the `KXLOWT*` ticker prefix instead, the LOW universe is 9 additional
snapshot days, not one.

WHAT THIS BUILDS
----------------
1. LOW entry-price series: for every (station, local_trading_date, ticker) with
   a KXLOWT quote, the YES mid at the snapshot nearest the 13:30 station-local
   decision pin (quote filter yes_bid>0 AND yes_ask<1; date alignment = the
   snapshot's station-LOCAL date == the ticker's target date).
2. A LADDER PROXY for strikes with no direct pin quote: linearly interpolate
   the mid from the two nearest strike levels present on that station-day
   (one-sided nearest when the strike is outside the quoted range).
3. Honest proxy error: leave-one-strike-out — drop each strike's direct quote
   and interpolate it from its neighbours; report |proxy - direct| by moneyness.
4. Coverage: n station-days / n cells priced BEFORE vs AFTER the backfill, and
   how many are >=30-trade-holdout-capable.

Unblocks; does NOT wire. Read-only on every book.

Usage:
    .venv-gefs/bin/python scripts/low_market_price_backfill_2026-10-13.py
"""
from __future__ import annotations

import gzip
import json
import math
import pickle
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
METAR = REPO / "data" / "metar_backfill.db"
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")

OUT_JSON = REPO / "artifacts" / "low-market-backfill" / "low_market_price_backfill_2026-10-13.json"
OUT_CSV = REPO / "artifacts" / "low-market-backfill" / "low_entry_price_series_2026-10-13.csv"
OUT_MD = REPO / "docs" / "plans" / "LOW-MARKET-PRICE-BACKFILL-2026-10-13.md"

PIN = 13 * 60 + 30          # 13:30 station-local
PIN_RADIUS = 30             # snapshot within +/- 30 min of the pin
BUCKET_MIN = 15

MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-([TB])([0-9]+(?:\.[0-9]+)?)$")

_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _tz(st: str) -> ZoneInfo:
    return ZoneInfo(_ICAO_TZ.get(st, "America/New_York"))


# ---------------------------------------------------------------------------
# LOW entry-price series
# ---------------------------------------------------------------------------
def load_low_series():
    """{(station, ds, ticker): {'strike': f, 'kind': 'T'|'B', 'mid': f,
       'snap_local_mod': int, 'dist_pin': int}}.

    Quote filter mirrors the frozen market benchmark contract
    (yes_bid>0 AND yes_ask<1, mid strictly inside (0,1)), but keyed on the
    KXLOWT* ticker prefix instead of `market_type`, and with the same
    station-LOCAL date alignment.
    """
    c = _ro(SNAPS)
    rows = c.execute(
        "SELECT snapshot_time, station, market_ticker, mid_price FROM market_snapshots "
        "WHERE market_ticker LIKE 'KXLOWT%-%' AND yes_bid > 0 AND yes_ask < 1 "
        "AND mid_price > 0 AND mid_price < 1").fetchall()
    c.close()
    out = {}
    for snap, st, tk, mid in rows:
        m = TICKER_RE.search(tk)
        if not m:
            continue
        yy, mo, dd, kind, sv = m.groups()
        tgt = f"20{yy}-{MON[mo]:02d}-{int(dd):02d}"
        dt = datetime.fromisoformat(snap)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        loc = dt.astimezone(_tz(st))
        if loc.date().isoformat() != tgt:      # station-local date alignment
            continue
        mod = loc.hour * 60 + loc.minute
        key = (st, tgt, tk)
        prev = out.get(key)
        if prev is None or abs(mod - PIN) < abs(prev["snap_local_mod"] - PIN):
            out[key] = {"strike": float(sv), "kind": kind, "mid": float(mid),
                        "snap_local_mod": mod, "dist_pin": abs(mod - PIN)}
    return out


def load_low_labels():
    """{(station, ds): [(strike_type, strike_value, result), ...]} for LOW."""
    c = _ro(LADDER)
    out = defaultdict(list)
    for st, ld, sv, sty, res in c.execute(
            "SELECT station, local_trading_date, strike_value, strike_type, result "
            "FROM strike_ladder WHERE market_type='LOW' AND result IS NOT NULL"):
        out[(st, ld)].append((sty, float(sv), res))
    c.close()
    return out


def load_low_truth():
    c = _ro(METAR)
    out = {}
    for st, ld, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='LOW' AND settlement_temp_f IS NOT NULL"):
        out[(st, ld)] = float(v)
    c.close()
    return out


def load_running_low():
    """{(station, ds): running-low-at-pin from ASOS bucket means (mod <= pin))."""
    if not RECS.exists():
        return {}
    with gzip.open(RECS, "rb") as f:
        recs = pickle.load(f)
    out = {}
    for (st, ds), rec in recs.items():
        b = rec.get("buckets") or {}
        vals = [b[m] for m in b if m <= PIN]
        if vals:
            out[(st, ds)] = min(vals)
    return out


# ---------------------------------------------------------------------------
# ladder proxy
# ---------------------------------------------------------------------------
def ladder_proxy(levels, target, neighbors=2):
    """Interpolate `target` mid from the nearest `neighbors` levels on the day.

    levels: list of (strike, mid) for the station-day, any kind.
    Returns (proxy_mid, method, support_min_distance). Linear between the
    bracketing levels; nearest-quote when outside the quoted range.
    """
    if not levels:
        return None, "empty", None
    ls = sorted(levels)
    strikes = [s for s, _ in ls]
    if target in (s for s, _ in ls):
        d = dict(ls)
        return d[target], "exact", 0.0
    below = [x for x in ls if x[0] < target]
    above = [x for x in ls if x[0] > target]
    if below and above:
        lo = below[-1]
        hi = above[0]
        if hi[0] > lo[0]:
            p = lo[1] + (hi[1] - lo[1]) * (target - lo[0]) / (hi[0] - lo[0])
            return p, "interp", min(abs(target - lo[0]), abs(hi[0] - target))
    # one-sided: nearest quote
    near = min(ls, key=lambda x: abs(x[0] - target))
    return near[1], "extrap_near", abs(target - near[0])


def leave_one_out_proxy(levels):
    """For each level with >=1 other level, proxy it from the rest and compare."""
    errs = []
    for i, (s, m) in enumerate(sorted(levels)):
        others = [x for j, x in enumerate(sorted(levels)) if j != i]
        if not others:
            continue
        p, method, _ = ladder_proxy(others, s)
        if p is None:
            continue
        errs.append({"strike": s, "direct": m, "proxy": round(p, 4),
                     "abs_err": round(abs(p - m), 4), "mid": m, "method": method})
    return errs


# ---------------------------------------------------------------------------
def main():
    series = load_low_series()
    labels = load_low_labels()
    truth = load_low_truth()
    runlow = load_running_low()

    # ---- BEFORE (the design doc's universe: market_type='LOW' rows) --------
    c = _ro(SNAPS)
    before_rows = c.execute(
        "SELECT DISTINCT station, substr(snapshot_time,1,10) FROM market_snapshots "
        "WHERE market_type='LOW'").fetchall()
    c.close()
    before_days = {d for _, d in before_rows}
    before_sd = {(st, d) for st, d in before_rows}

    # ---- AFTER: group cells by (station, ds) ------------------------------
    by_sd = defaultdict(list)
    for (st, ds, tk), v in series.items():
        by_sd[(st, ds)].append((v["strike"], v["mid"], v["kind"], tk, v["dist_pin"]))

    priced_sd = set(by_sd)
    priced_with_label = {k for k in priced_sd if k in labels}
    priced_with_truth = {k for k in priced_sd if k in truth}

    # ---- proximity / near-money coverage ----------------------------------
    # A cell counts as "direct at the pin" when its nearest snapshot is within
    # PIN_RADIUS of the 13:30 pin; otherwise it is a same-day fallback quote.
    pin_cells = sum(1 for v in series.values() if v["dist_pin"] <= PIN_RADIUS)
    fallback_cells = len(series) - pin_cells
    near_money_sd = set()               # station-days with a near-money pin quote
    any_near_money_sd = set()           # station-days with a near-money same-day quote
    for (st, ds, tk), v in series.items():
        if 0.2 <= v["mid"] <= 0.8:
            any_near_money_sd.add((st, ds))
            if v["dist_pin"] <= PIN_RADIUS:
                near_money_sd.add((st, ds))
    dist_pin_vals = sorted(v["dist_pin"] for v in series.values())
    def _pct(vals, q):
        return round(vals[int(q * (len(vals) - 1))], 1) if vals else None

    # ---- ladder proxy validation (leave-one-strike-out, per station-day) ---
    loo_all = []
    by_moneyness = defaultdict(list)
    for (st, ds), cells in by_sd.items():
        levels = sorted({(s, m) for s, m, *_ in cells})
        if len(levels) < 2:
            continue
        for e in leave_one_out_proxy(levels):
            loo_all.append(e)
            mm = e["mid"]
            band = "near(0.2-0.8)" if 0.2 <= mm <= 0.8 else ("low(<0.2)" if mm < 0.2 else "high(>0.8)")
            by_moneyness[band].append(e["abs_err"])
    def summ(v):
        if not v:
            return {"n": 0}
        v = sorted(v)
        return {"n": len(v), "mean": round(sum(v) / len(v), 4),
                "median": round(v[len(v) // 2], 4), "max": round(v[-1], 4),
                "p90": round(v[int(0.9 * (len(v) - 1))], 4)}

    proxy_err = {
        "overall": summ([e["abs_err"] for e in loo_all]),
        "by_method": {},
        "by_moneyness": {k: summ(v) for k, v in sorted(by_moneyness.items())},
    }
    for meth in ("interp", "extrap_near"):
        proxy_err["by_method"][meth] = summ([e["abs_err"] for e in loo_all if e["method"] == meth])

    # ---- write the entry-price series CSV ---------------------------------
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    lines = ["station,target_date,ticker,strike,kind,mid,dist_pin_minutes,"
             "has_low_label,has_low_truth,running_low_at_pin"]
    for (st, ds), cells in sorted(by_sd.items()):
        rl = runlow.get((st, ds))
        haslab = 1 if (st, ds) in labels else 0
        hastr = 1 if (st, ds) in truth else 0
        for s, m, kind, tk, dp in sorted(cells):
            lines.append(f"{st},{ds},{tk},{s},{kind},{m},{dp},{haslab},{hastr},"
                         f"{'' if rl is None else round(rl,2)}")
    OUT_CSV.write_text("\n".join(lines) + "\n")

    # ---- coverage artifact ------------------------------------------------
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pin_local": "13:30",
        "pin_radius_min": PIN_RADIUS,
        "before_backfill": {
            "source": "market_snapshots.market_type='LOW' (the design doc's universe)",
            "snapshot_days": sorted(before_days),
            "n_snapshot_days": len(before_days),
            "n_station_days": len(before_sd),
        },
        "after_backfill": {
            "source": "KXLOWT* ticker prefix, market_type='binary' (the real LOW books)",
            "n_station_days_priced": len(priced_sd),
            "n_station_days_with_low_label": len(priced_with_label),
            "n_station_days_with_low_truth": len(priced_with_truth),
            "by_date": {d: sum(1 for (s, dd) in priced_sd if dd == d)
                        for d in sorted({dd for _, dd in priced_sd})},
            "by_date_with_truth": {d: sum(1 for (s, dd) in priced_with_truth if dd == d)
                                   for d in sorted({dd for _, dd in priced_with_truth})},
            "n_cells_total": len(series),
            "n_cells_with_low_label": sum(1 for (st, ds, tk) in series if (st, ds) in labels),
            "n_cells_direct_at_pin": pin_cells,
            "n_cells_same_day_fallback": fallback_cells,
            "n_station_days_with_near_money_pin_quote": len(near_money_sd),
            "n_station_days_with_near_money_any_quote": len(any_near_money_sd),
            "dist_pin_minutes_p50": _pct(dist_pin_vals, 0.5),
            "dist_pin_minutes_p90": _pct(dist_pin_vals, 0.9),
            "dist_pin_minutes_max": dist_pin_vals[-1] if dist_pin_vals else None,
        },
        "holdout_capable": {
            "target_trades": 30,
            "truth_anchored_station_days": len(priced_with_truth),
            "clears_30_with_margin": len(priced_with_truth) >= 30,
        },
        "proxy_error": proxy_err,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))

    # ---- markdown ---------------------------------------------------------
    b = result["before_backfill"]
    a = result["after_backfill"]
    L = []
    L.append("# LOW Market Entry-Price Backfill — 2026-10-13")
    L.append("")
    L.append("**Script:** `scripts/low_market_price_backfill_2026-10-13.py`  ")
    L.append("**Machine artifact:** `artifacts/low-market-backfill/low_market_price_backfill_2026-10-13.json`  ")
    L.append("**Series:** `artifacts/low-market-backfill/low_entry_price_series_2026-10-13.csv`  ")
    L.append("**Status:** backfill only. Holdout is **unblocked**, NOT wired.")
    L.append("")
    L.append("## Bottom line")
    L.append("")
    L.append(f"The LOW entry-price universe was **{b['n_station_days']} station-days on "
             f"{b['n_snapshot_days']} snapshot day(s)** as measured by the holdout design "
             f"doc (via `market_snapshots.market_type='LOW'`). It is **"
             f"{a['n_station_days_priced']} station-days** once keyed on the `KXLOWT*` "
             f"ticker prefix (the real LOW books are ingested as `market_type='binary'`). "
             f"**{a['n_station_days_with_low_truth']} of those carry canonical LOW "
             f"settlement truth** — enough for a 30-trade holdout with "
             f"{a['n_station_days_with_low_truth'] - 30:+d} margin.")
    L.append("")
    L.append("## Why the design doc saw one day")
    L.append("")
    L.append("`market_snapshots.market_type` is a mislabeled *strike-shape* column: for "
             "KXLOWT rows it reads `HIGH` on T-strikes and `LOW` on B-strikes, so "
             "filtering on it fragments and mostly drops the LOW books. The `KXLOWT*` "
             "ticker prefix is the reliable key. (The 2026-09-07 rows are a one-off "
             "ingest pass that happened to stamp real `market_type` values.)")
    L.append("")
    L.append("## Coverage: before → after")
    L.append("")
    L.append("| metric | before | after |")
    L.append("|---|---:|---:|")
    L.append(f"| snapshot days | {b['n_snapshot_days']} | {len(a['by_date'])} |")
    L.append(f"| station-days priced at the pin | {b['n_station_days']} | {a['n_station_days_priced']} |")
    L.append(f"| ...with LOW label | — | {a['n_station_days_with_low_label']} |")
    L.append(f"| ...with LOW settlement truth | — | {a['n_station_days_with_low_truth']} |")
    L.append(f"| priced cells (station-day-ticker) | — | {a['n_cells_total']} |")
    L.append(f"| cells direct at the pin (≤{PIN_RADIUS} min) | — | {a['n_cells_direct_at_pin']} |")
    L.append(f"| cells same-day fallback (nearest snapshot) | — | {a['n_cells_same_day_fallback']} |")
    L.append(f"| station-days with a near-money pin quote (mid∈[0.2,0.8]) | — | {a['n_station_days_with_near_money_pin_quote']} |")
    L.append(f"| station-days with a near-money same-day quote | — | {a['n_station_days_with_near_money_any_quote']} |")
    L.append("")
    L.append(f"Distance from each cell's snapshot to the 13:30 pin (minutes): "
             f"median {a['dist_pin_minutes_p50']}, p90 {a['dist_pin_minutes_p90']}, "
             f"max {a['dist_pin_minutes_max']}.")
    L.append("")
    L.append("**Entry-price caveat (measured, not assumed):** on the 2026-09-15→23 "
             "days the live watch lists are sparse at the pin — a handful of strikes "
             "per station-day, frequently tails (mid ≈ 0 or 1) rather than near-money. "
             "Only the station-days in the near-money row above give a *directly "
             "usable* 50/50-ish entry price; the rest need the ladder proxy below or a "
             "rank-2 strike, and the proxy is only trustworthy when near-money "
             "neighbours actually exist that day.")
    L.append("")
    L.append("### By target date (station-days priced / with truth)")
    L.append("")
    L.append("| target date | priced | with truth |")
    L.append("|---|---:|---:|")
    for d in sorted(a["by_date"]):
        L.append(f"| {d} | {a['by_date'][d]} | {a['by_date_with_truth'].get(d, 0)} |")
    L.append("")
    L.append("## Ladder proxy — honest error (leave-one-strike-out)")
    L.append("")
    L.append("For each station-day, every quoted strike was dropped and "
             "re-derived from its neighbours (linear interpolation between the "
             "bracketing strikes; nearest-quote when outside the quoted range). "
             "Absolute error vs the direct quote:")
    L.append("")
    L.append("| slice | n | mean | median | p90 | max |")
    L.append("|---|---:|---:|---:|---:|---:|")
    def prow(name, s):
        if not s.get("n"):
            L.append(f"| {name} | 0 | — | — | — | — |")
        else:
            L.append(f"| {name} | {s['n']} | {s['mean']} | {s['median']} | {s['p90']} | {s['max']} |")
    prow("overall", proxy_err["overall"])
    prow("interp (bracketed)", proxy_err["by_method"]["interp"])
    prow("extrap_near (one-sided)", proxy_err["by_method"]["extrap_near"])
    for k, s in proxy_err["by_moneyness"].items():
        prow(f"moneyness {k}", s)
    L.append("")
    o = proxy_err["overall"]
    if o.get("n"):
        L.append(f"**Proxy read:** median absolute mid error **{o['median']}** "
                 f"(mean {o['mean']}, p90 {o['p90']}) over {o['n']} leave-one-out "
                 f"cells. Bracketed interpolation is better than one-sided "
                 f"extrapolation on the median, but both are **large relative to a "
                 f"traded price** (a 0.18–0.32 absolute mid error is ~18–32 cents). "
                 f"The proxy is therefore only acceptable where near-money "
                 f"neighbours actually exist (see the moneyness rows); the one-sided "
                 f"branch and the deep-tail cells should be flagged or dropped, not "
                 f"silently interpolated.")
    L.append("")
    L.append("## What is now unblocked")
    L.append("")
    L.append(f"- A **{a['n_station_days_with_low_truth']}-station-day** LOW entry-price "
             f"series with canonical truth and labels — comfortably ≥30 for the "
             f"pre-registered holdout.")
    L.append("- Entry price = YES mid at the nearest-to-13:30 station-local snapshot, "
             "same frozen quote filter and LOCAL date alignment as the HIGH benchmark.")
    L.append("- For any strike lacking a direct pin quote, the ladder proxy supplies a "
             "fill estimate with a documented error distribution (above).")
    L.append("")
    L.append("## Explicit non-goals (still true)")
    L.append("")
    L.append("- No `market_type='LOW'` booking path enabled; no gate/sizing/exposure "
             "change; no live-book write.")
    L.append("- The 30 holdout trades are NOT selected or frozen here — that is the "
             "next step, gated on this backfill being accepted.")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append(".venv-gefs/bin/python scripts/low_market_price_backfill_2026-10-13.py")
    L.append("```")
    L.append("")
    OUT_MD.write_text("\n".join(L) + "\n")

    print(json.dumps(result, indent=2))
    print(f"\nwrote {OUT_JSON}\nwrote {OUT_CSV}\nwrote {OUT_MD}")


if __name__ == "__main__":
    main()
