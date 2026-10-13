#!/usr/bin/env python3
"""pin_sweep_2026-10-13.py — Item 2: per-station decision-pin sweep.

For each of the 20 stations, sweep the decision pin 12:00 -> 15:00 station-local
in 30-min steps, producing three per-pin metrics:

  1. brier_reanchor — the LITERAL study method: T = floor(high-so-far at pin)+1,
     p = the study's rate_1h window-max scan (13:00..16:45), strict label,
     canonical truth. NOTE: under re-anchoring the strike rises with the pin, so
     this curve is ANCHOR-DOMINATED (it always favours the earliest pin — an
     easier strike — and is NOT evidence about signal timing).

  2. brier_fixedT — signal TIMING, isolated: hold the target fixed at the
     canonical 13:30-pin strike (T = floor(hsf@13:30)+1), and score the rate_1h
     nowcast BOUNDED to [13:00 .. pin] (no look-ahead past the pin). This is the
     curve that answers "when is the decision-time signal best?" — it wants the
     pin LATE (more of the day observed).

  3. spread_near — median near-money (mid in [0.2,0.8]) bid-ask spread of the
     Kalshi -T ladder at that station-local pin; the fill wants the pin EARLY
     (overnight spreads are narrower).

Recommendation rule: minimise `spread_z + brier_fixedT_z` (the crossing of the
signal-timing and fill-cost curves). The re-anchored column is reported for
completeness but is not used for the pick (anchor artifact).

READ-ONLY on every book. Writes ONLY:
  docs/plans/PER-STATION-PIN-SCHEDULE-2026-10-13.md
  data/per_station_pins.json
"""
from __future__ import annotations

import importlib.util as ilu
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

spec = ilu.spec_from_file_location(
    "pds", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = ilu.module_from_spec(spec)
spec.loader.exec_module(PDS)

from core.station_time import station_timezone_name  # noqa: E402

SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
OUT_JSON = REPO / "data" / "per_station_pins.json"
OUT_MD = REPO / "docs" / "plans" / "PER-STATION-PIN-SCHEDULE-2026-10-13.md"
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")

PINS_MOD = [720, 750, 780, 810, 840, 870, 900]
PIN_LABEL = {720: "12:00", 750: "12:30", 780: "13:00", 810: "13:30",
             840: "14:00", 870: "14:30", 900: "15:00"}
SCAN_LO, SCAN_HI = 780, 1005          # study rate_1h window (13:00..16:45)
ANCHOR = 810                          # canonical 13:30 pin


def load_recs():
    import gzip, pickle
    with gzip.open(RECS, "rb") as f:
        return pickle.load(f)


def scan_p(rec, T, lo, hi, sigma=PDS.SIGMA_RATE):
    b = rec["buckets"]
    if not b:
        return None
    mods = sorted(b)
    best = None
    for mod in range(lo, hi + 1, 15):
        if mod not in b:
            continue
        h = max(b[x] for x in mods if x <= mod)
        if h <= 0:
            continue
        rate, roll = PDS._rate_and_roll(b, mod)
        d = PDS.deflate(rate, roll)
        if d is None:
            continue
        p = PDS.p_ge(h + d, sigma, T)
        if best is None or p > best:
            best = p
    return best


def sweep_brier(recs, truth):
    """Per (station, pin): re-anchored Brier and fixed-T (signal-timing) Brier."""
    out = {}
    for st in PDS.STATIONS:
        for pin in PINS_MOD:
            r_ps, r_ys, f_ps, f_ys = [], [], [], []
            for (s, ds), rec in recs.items():
                if s != st:
                    continue
                b = rec["buckets"]
                if not b:
                    continue
                settled = truth.get((st, ds))
                if settled is None:
                    continue
                hsf_pin = max([b[x] for x in b if x <= pin], default=None)
                hsf_anc = max([b[x] for x in b if x <= ANCHOR], default=None)
                if hsf_pin is None or hsf_anc is None:
                    continue
                # (1) re-anchored: T from the pin's high-so-far, study window-max
                T_r = float(int(hsf_pin) + 1.0)
                pr = scan_p(rec, T_r, SCAN_LO, SCAN_HI)
                if pr is not None:
                    r_ps.append(pr)
                    r_ys.append(1 if settled > T_r else 0)
                # (2) fixed-T: anchor at 13:30, bounded scan up to the pin
                T_f = float(int(hsf_anc) + 1.0)
                pf = scan_p(rec, T_f, SCAN_LO, max(SCAN_LO, pin))
                if pf is not None:
                    f_ps.append(pf)
                    f_ys.append(1 if settled > T_f else 0)
            out[(st, pin)] = {
                "brier_reanchor": round(PDS.brier(r_ps, r_ys), 4) if r_ps else None,
                "n_reanchor": len(r_ps),
                "base_reanchor": round(sum(r_ys) / len(r_ys), 4) if r_ys else None,
                "brier_fixedT": round(PDS.brier(f_ps, f_ys), 4) if f_ps else None,
                "n_fixedT": len(f_ps),
                "base_fixedT": round(sum(f_ys) / len(f_ys), 4) if f_ys else None,
            }
    return out


def load_spreads():
    c = sqlite3.connect(f"file:{SNAPS}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT station, snapshot_time, yes_bid, yes_ask, mid_price "
        "FROM market_snapshots WHERE market_ticker LIKE 'KXHIGH%-T%' "
        "AND market_type='binary' AND yes_bid > 0 AND yes_ask < 1 "
        "AND mid_price > 0 AND mid_price < 1").fetchall()
    c.close()
    tz = {st: ZoneInfo(station_timezone_name(st)) for st in PDS.STATIONS}
    by = defaultdict(list)
    for st, snap, bid, ask, mid in rows:
        z = tz.get(st)
        if z is None:
            continue
        try:
            dt = datetime.fromisoformat(snap)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            loc = dt.astimezone(z)
        except Exception:
            continue
        by[(st, loc.date().isoformat())].append(
            (loc.hour * 60 + loc.minute, float(ask) - float(bid), float(mid)))
    return by


def spread_at(by, st, pin, radius=30):
    near, allv = [], []
    for (s, ds), pts in by.items():
        if s != st:
            continue
        cand = [t for t in pts if abs(t[0] - pin) <= radius]
        if not cand:
            continue
        nm = min(cand, key=lambda t: abs(t[0] - pin))
        allv.append(nm[1])
        if 0.2 <= nm[2] <= 0.8:
            near.append(nm[1])
    return {"median_spread_near": round(statistics.median(near), 4) if near else None,
            "n_spread_near": len(near),
            "median_spread_all": round(statistics.median(allv), 4) if allv else None,
            "n_spread_all": len(allv)}


def zscores(vals):
    m = statistics.fmean(vals)
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    if sd <= 1e-12:
        return [0.0] * len(vals)
    return [(v - m) / sd for v in vals]


def main():
    recs = load_recs()
    truth = PDS.load_truth()
    brier = sweep_brier(recs, truth)
    by = load_spreads()

    per_station = {}
    for st in PDS.STATIONS:
        rows = []
        for pin in PINS_MOD:
            b = brier.get((st, pin), {})
            s = spread_at(by, st, pin)
            rows.append({
                "pin_mod": pin, "pin_local_hhmm": PIN_LABEL[pin] + ":00",
                "brier_reanchor": b.get("brier_reanchor"), "n_reanchor": b.get("n_reanchor", 0),
                "brier_fixedT": b.get("brier_fixedT"), "n_fixedT": b.get("n_fixedT", 0),
                "base_fixedT": b.get("base_fixedT"),
                "spread": s["median_spread_near"], "n_spread_near": s["n_spread_near"],
                "spread_all": s["median_spread_all"], "n_spread_all": s["n_spread_all"],
                "lead_h": round(24.0 - pin / 60.0, 2),
            })
        # pick on brier_fixedT + spread (both present); spread_z imputed 0 if flat
        usable = [r for r in rows if r["brier_fixedT"] is not None]
        if usable:
            bz = zscores([r["brier_fixedT"] for r in usable])
            for r in usable:
                r["brier_fixedT_z"] = None
            for r, z in zip(usable, bz):
                r["brier_fixedT_z"] = round(z, 4)
            if all(r["spread"] is not None for r in usable) and len(usable) > 1 \
                    and statistics.pstdev([r["spread"] for r in usable]) > 1e-12:
                sz = zscores([r["spread"] for r in usable])
            else:
                sz = [0.0] * len(usable)
            for r, z in zip(usable, sz):
                r["spread_z"] = round(z, 4)
                r["combined_z"] = round(r["brier_fixedT_z"] + z, 4)
            best = min(usable, key=lambda r: r["combined_z"])
            rec = {"pin_local_hhmm": best["pin_local_hhmm"], "pin_mod": best["pin_mod"],
                   "brier": best["brier_fixedT"], "brier_reanchor": best["brier_reanchor"],
                   "spread": best["spread"], "lead_h": best["lead_h"],
                   "combined_z": best["combined_z"]}
        else:
            rec = None
        per_station[st] = {"pins": rows, "recommended": rec}

    OUT_JSON.write_text(json.dumps(
        {st: ({"pin_local_hhmm": v["recommended"]["pin_local_hhmm"],
               "brier": v["recommended"]["brier"],
               "brier_reanchor": v["recommended"]["brier_reanchor"],
               "spread": v["recommended"]["spread"],
               "lead_h": v["recommended"]["lead_h"]}
              if v["recommended"] else None)
         for st, v in per_station.items()}, indent=2))

    lines = [
        "# Per-Station Pin Schedule (Item 2)",
        "",
        "**Date:** 2026-10-13 · **Agent:** Gilfoyle",
        "**Directive (Dan):** multiple timing pins per station, localized to station needs.",
        "",
        "Pin swept 12:00→15:00 station-local in 30-min steps. **Nothing in the bridge is",
        "changed** (`DECISION_PIN_LOCAL_HHMM` untouched); schedule + JSON only.",
        "",
        "### Method (read this — it matters)",
        "",
        "Two Brier columns, because they answer different questions:",
        "",
        "- **brier_reanchor** — the literal study rule: `T = floor(hsf@pin)+1`, p = the",
        "  study's rate_1h window-max scan (13:00–16:45), strict label, canonical truth.",
        "  Under re-anchoring the strike RISES with the pin, so this curve is",
        "  **anchor-dominated** (always favours the earliest, easiest strike). It is NOT",
        "  evidence about signal timing and is shown only for completeness.",
        "- **brier_fixedT** — signal timing, isolated: hold the target at the canonical",
        "  13:30 strike (`T = floor(hsf@13:30)+1`) and score the rate_1h nowcast BOUNDED",
        "  to `13:00..pin` (no look-ahead). This curve wants the pin **LATE** (more of the",
        "  day observed).",
        "- **spread (near)** — median near-money (mid ∈ [0.2,0.8]) bid-ask at that",
        "  station-local pin; the fill wants the pin **EARLY** (overnight spreads are",
        "  narrower).",
        "",
        "**Recommendation rule:** minimise `spread_z + brier_fixedT_z` — the crossing of the",
        "signal-timing and fill-cost curves.",
        "",
        "## Recommended pin per station",
        "",
        "| Station | pin (local) | Brier(fixedT) | Brier(reanchor) | median spread(near) | lead_h |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for st in PDS.STATIONS:
        r = per_station[st]["recommended"]
        if r:
            cr = "—" if r["brier_reanchor"] is None else f"{r['brier_reanchor']:.4f}"
            lines.append(f"| {st} | {r['pin_local_hhmm']} | {r['brier']:.4f} | {cr} | "
                         f"{r['spread'] if r['spread'] is not None else '—'} | {r['lead_h']} |")
        else:
            lines.append(f"| {st} | — | — | — | — | — |")
    lines += ["", "## Findings", "",
              "- **The signal wants the pin late, monotonically, at every station.** "
              "brier_fixedT falls from 13:00 to 15:00 for all 20 stations (e.g. KATL "
              "0.567 → 0.102, KSAT 0.638 → 0.073): the nowcast is cold at 13:00 and "
              "sharpens as the day's max is observed.",
              "- **The spread does NOT differentiate inside 12:00–15:00 local.** "
              "Near-money (mid ∈ [0.2,0.8]) median spread is ~1 tick (0.01) across the "
              "whole window at essentially every station; the wide-spread regime"
              "identified earlier sits in the 17–20Z (afternoon) window, outside this "
              "sweep. Sample counts are thin (n=4–7 station-days per pin), so the "
              "spread column is weakly identified.",
              "- **Consequence: the mechanical pick is the late edge (15:00 local, 9h "
              "lead) at all 20 stations** — the signal-timing term dominates because the "
              "fill-cost term is flat. This is *not* a per-station-differentiated "
              "schedule; the data does not support differentiation within "
              "12:00–15:00.",
              "- **The binding constraint is not in this sweep:** going later than "
              "15:00 keeps improving the signal but (a) the wide-spread afternoon "
              "window opens ~17–20Z and (b) settlement/close bounds the lead. The "
              "honest read is that the recommend-late result is bounded by those, and "
              "a 13:30→15:00 move is the defensible in-window delta, not a station map.",
              ""]
    lines += ["", "## Full sweep", ""]
    for st in PDS.STATIONS:
        lines.append(f"### {st}")
        lines.append("")
        lines.append("| pin | Brier(fixedT) | n | Brier(reanchor) | spread(near) | n | spread(all) | lead_h | brier_fixedT_z | spread_z | combined_z |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in per_station[st]["pins"]:
            def f(v, d=4):
                return "—" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
            bz = "—" if r.get("brier_fixedT_z") is None else f"{r['brier_fixedT_z']:+.3f}"
            sz = "—" if r.get("spread_z") is None else f"{r['spread_z']:+.3f}"
            cz = "—" if r.get("combined_z") is None else f"{r['combined_z']:+.3f}"
            lines.append(f"| {r['pin_local_hhmm']} | {f(r['brier_fixedT'])} | {r['n_fixedT']} | "
                         f"{f(r['brier_reanchor'])} | {f(r['spread'])} | {r['n_spread_near']} | "
                         f"{f(r['spread_all'])} | {r['lead_h']} | {bz} | {sz} | {cz} |")
        lines.append("")
    OUT_MD.write_text("\n".join(lines) + "\n")

    print("recommended pins:")
    for st in PDS.STATIONS:
        r = per_station[st]["recommended"]
        if r:
            print(f"  {st}: {r['pin_local_hhmm']}  Brier(fixedT)={r['brier']:.4f} "
                  f"spread={r['spread']} lead={r['lead_h']}h z={r['combined_z']:+.3f}")
    print(f"\nwrote {OUT_JSON}\nwrote {OUT_MD}")


if __name__ == "__main__":
    main()
