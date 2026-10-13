#!/usr/bin/env python3
"""honest_arsenal_sweep_2026-10-13.py — Stage 3 of the honest arsenal
re-evaluation: the rate_1h window sweep (W=1..6) with every window ENDING at
the 13:30 station-local decision pin (past-window only, NO future bucket), plus
the calibration/ceiling decomposition of the honest point-time signal.

The published AI-NWP sweep (scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py)
scored W=1..6 at EU/UTC grid hours on a full-day label, so its headline 60.2pp
separation at 18Z is a decision-time artifact (18Z is near the local pin for the
eastern stations) and the late-hour separation is inflated because the feature
high-so-far and the label share the same running max.

This script re-anchors the sweep at the STATION-LOCAL pin (mod == 810):
  rate_W = mean(last 4W buckets ending at 810) - mean(prev 4W buckets)
  strike = floor(max bucket mean, mod <= 810) + 1
  crossed = dmax > strike   (dmax = full-day max bucket mean, the outcome)

READ-ONLY. Writes ONLY artifacts/honest-arsenal/sweep_stage3_2026-10-13.json.
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = ilu.spec_from_file_location(
    "pds", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = ilu.module_from_spec(spec)
spec.loader.exec_module(PDS)

import importlib.util as _ilu2
spec2 = _ilu2.spec_from_file_location(
    "cm", REPO / "core" / "intraday" / "multi_forecaster_fusion.py")
# calibrated_ceiling is imported via PDS-adjacent module; load directly
from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    calibrated_ceiling, RESOLUTION_FULL_SKILL, MAX_CEILING, CALIBRATION_FLOOR,
    HOLDOUT_RESOLUTION,
)

RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OUT = REPO / "artifacts" / "honest-arsenal" / "sweep_stage3_2026-10-13.json"

PIN_MOD = 810
BUCKET = 15
WINDOWS_H = [1, 2, 3, 4, 5, 6]
THRESHOLDS = [1.0, 0.5]


def load(fn):
    with gzip.open(fn, "rb") as f:
        return pickle.load(f)


def _mean(xs):
    return sum(xs) / len(xs)


def rate_at_pin(buckets, W):
    """Symmetric contrast ending at the pin: mean(last 4W) - mean(prev 4W).

    Only buckets with mod <= 810 are read (no future bucket)."""
    end = PIN_MOD
    k = 4 * W
    # buckets are on a 15-min grid; require contiguity
    def span(start, stop):
        vals = []
        m = start
        while m < stop:
            if m not in buckets:
                return None
            vals.append(buckets[m])
            m += BUCKET
        return vals
    recent = span(end - (k - 1) * BUCKET, end + BUCKET)
    prior = span(end - (2 * k - 1) * BUCKET, end - (k - 1) * BUCKET)
    if recent is None or prior is None:
        return None
    return _mean(recent) - _mean(prior)


def sep_cell(samples, t):
    up = [s for s in samples if s["rate"] is not None and s["rate"] >= t]
    dn = [s for s in samples if s["rate"] is not None and s["rate"] <= -t]
    if not up or not dn:
        return None
    pu = sum(s["crossed"] for s in up) / len(up)
    pd = sum(s["crossed"] for s in dn) / len(dn)
    return {"p_up": round(pu, 4), "n_up": len(up), "p_down": round(pd, 4),
            "n_down": len(dn), "sep_pp": round(100 * (pu - pd), 2)}


def boot_ci(samples, t, reps, seed):
    by_date = defaultdict(lambda: [0, 0, 0, 0])
    for s in samples:
        if s["rate"] is None:
            continue
        d = by_date[s["ld"]]
        if s["rate"] >= t:
            d[0] += s["crossed"]; d[1] += 1
        elif s["rate"] <= -t:
            d[2] += s["crossed"]; d[3] += 1
    dates = list(by_date)
    if len(dates) < 5:
        return None
    rng = random.Random(seed)
    st = []
    for _ in range(reps):
        su = nu = sd = nd = 0
        for _ in dates:
            d = by_date[rng.choice(dates)]
            su += d[0]; nu += d[1]; sd += d[2]; nd += d[3]
        if nu and nd:
            st.append(su / nu - sd / nd)
    if not st:
        return None
    st.sort()
    return [round(100 * st[int(0.025 * len(st))], 2),
            round(100 * st[int(0.975 * len(st)) - 1], 2)]


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def main():
    recs = load(RECS_PKL)

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "honest pin-anchored rate-window sweep + calibration",
        "anchor": "station-local mod == 810 (13:30 pin); every window ends at the pin",
        "windows_h": WINDOWS_H, "thresholds": THRESHOLDS,
        "by_window": {}, "normalized": {}, "rate_scale": {},
        "published_reference": {
            "note": "from the AI-NWP sweep artifact, 18Z (UTC) peak",
            "W1_pub1h_18Z_sep_pp": 62.58, "W1_symmetric_18Z_sep_pp": 60.22,
            "peak_hour_utc": 18,
        },
    }

    samples_all = {}
    for W in WINDOWS_H:
        rows = []
        for (st, ld), rec in recs.items():
            b = rec["buckets"]
            if not b or PIN_MOD not in b:
                continue
            r = rate_at_pin(b, W)
            if r is None:
                continue
            vals = [b[m] for m in b if m <= PIN_MOD]
            if not vals:
                continue
            hsf = max(vals)
            strike = math.floor(hsf) + 1.0
            dmax = rec["dmax"]
            if dmax is None:
                continue
            rows.append({"st": st, "ld": ld, "rate": r,
                         "crossed": 1 if dmax > strike else 0,
                         "hsf": hsf, "strike": strike})
        samples_all[W] = rows
        out["by_window"][f"W{W}"] = {"n": len(rows),
                                    "base_rate": round(sum(s["crossed"] for s in rows) / len(rows), 4) if rows else None}
        cells = {}
        for t in THRESHOLDS:
            c = sep_cell(rows, t)
            if c is not None:
                c["ci95_pp"] = boot_ci(rows, t, 500, seed=100 * W + int(t * 10))
            cells[f"+/-{t}"] = c
        out["by_window"][f"W{W}"]["cells"] = cells
        # normalized 15/15 split
        srt = sorted(r["rate"] for r in rows)
        if len(srt) >= 50:
            hi = quantile(srt, 0.85); lo = quantile(srt, 0.15)
            up = [r for r in rows if r["rate"] >= hi]
            dn = [r for r in rows if r["rate"] <= lo]
            pu = sum(r["crossed"] for r in up) / len(up)
            pd = sum(r["crossed"] for r in dn) / len(dn)
            out["normalized"][f"W{W}"] = {
                "n_up": len(up), "n_down": len(dn),
                "hi_t": round(hi, 3), "lo_t": round(lo, 3),
                "p_up": round(pu, 4), "p_down": round(pd, 4),
                "sep_pp": round(100 * (pu - pd), 2)}
        # rate scale
        vals = [r["rate"] for r in rows]
        m = _mean(vals)
        sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))
        out["rate_scale"][f"W{W}"] = {"n": len(vals), "mean": round(m, 4),
                                      "sd": round(sd, 4)}

    # ---- calibration / ceiling of the honest point-time signal ------------
    # honest rate_1h P(cross) at the pin (single point), same as Stage 1
    def pit_p(rec, strike):
        b = rec["buckets"]
        if not b or PIN_MOD not in b:
            return None
        vals = [b[x] for x in b if x <= PIN_MOD]
        if not vals or max(vals) <= 0:
            return None
        rate, _ = PDS._rate_and_roll(b, PIN_MOD)
        if rate is None:
            return None
        r3 = [PIN_MOD - 30, PIN_MOD - 15, PIN_MOD]
        roll = (_mean([b[x] for x in r3]) if all(x in b for x in r3) else None)
        d = PDS.deflate(rate, roll)
        if d is None:
            return None
        return PDS.p_ge(max(vals) + d, PDS.SIGMA_RATE, strike)

    ps, ys = [], []
    for (st, ld), rec in recs.items():
        b = rec["buckets"]
        if not b or PIN_MOD not in b:
            continue
        vals = [b[m] for m in b if m <= PIN_MOD]
        if not vals or max(vals) <= 0:
            continue
        strike = math.floor(max(vals)) + 1.0
        p = pit_p(rec, strike)
        dmax = rec["dmax"]
        if p is None or dmax is None:
            continue
        ps.append(p); ys.append(1 if dmax > strike else 0)
    cal = PDS.calibration_metrics(ps, ys)
    honest_cal = {
        "n": len(ps), "brier": round(PDS.brier(ps, ys), 4),
        "reliability": cal["reliability"], "resolution": cal["resolution"],
        "uncertainty": cal["uncertainty"], "ece": cal["ece"], "mce": cal["mce"],
        "ceiling": calibrated_ceiling(cal["resolution"]),
        "calibration_table": cal["calibration_table"],
    }
    out["honest_calibration"] = honest_cal
    out["ceiling_formula"] = {
        "CALIBRATION_FLOOR": CALIBRATION_FLOOR, "MAX_CEILING": MAX_CEILING,
        "RESOLUTION_FULL_SKILL": RESOLUTION_FULL_SKILL,
        "expr": "0.75 + 0.24*min(1, resolution/0.05)"}
    out["holdout_reference"] = {"resolution": HOLDOUT_RESOLUTION,
                                "ceiling": calibrated_ceiling(HOLDOUT_RESOLUTION)}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))

    print("=" * 88)
    print("STAGE 3 — HONEST PIN-ANCHORED RATE-WINDOW SWEEP (window ends at 13:30 pin)")
    print("=" * 88)
    print(f"{'W':>3} {'n':>7} {'sd(rate)':>9} {'sep@+/-1.0':>11} {'ci95':>16} {'norm15 sep':>11}")
    for W in WINDOWS_H:
        bw = out["by_window"][f"W{W}"]
        c = (bw["cells"] or {}).get("+/-1.0")
        nz = out["normalized"].get(f"W{W}", {})
        sep = c["sep_pp"] if c else None
        ci = c["ci95_pp"] if c else None
        print(f"{W:>3} {bw['n']:>7} {out['rate_scale'][f'W{W}']['sd']:>9.3f} "
              f"{str(sep):>11} {str(ci):>16} {str(nz.get('sep_pp')):>11}")
    print(f"\npublished reference: W1 symmetric 18Z sep = 60.22pp, pub-form 62.58pp")
    print("\nhonest calibration of the pin point-time rate_1h P(cross):")
    print(f"  n={honest_cal['n']} brier={honest_cal['brier']} ece={honest_cal['ece']} "
          f"resolution={honest_cal['resolution']} ceiling={honest_cal['ceiling']}")
    print(f"  ceiling formula {out['ceiling_formula']['expr']}; "
          f"holdout ref ceiling={out['holdout_reference']['ceiling']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
