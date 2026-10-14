#!/usr/bin/env python3
"""fusion_weight_sweep_2026-10-14.py — best-weighting sweep for the 5-member fusion.

Dan (2026-10-14): "If we're keeping fusion, what's the best weighting? Can we
sweep to find the sweet spot?"

The shipped 5-member fusion is EQUAL-ish (0.25 each over gefs/analog/traj/market
+ om_gfs 0.15) and scores Brier 0.1458 on the honest 2296 station-day cohort,
while the best single member om_gfs(bc,ps) scores 0.1067. The fusion is WEAKER
than om_gfs alone because it averages in weak members (rate_1h 0.36, gefs 0.33,
market 0.62).

This script sweeps the om_gfs vote weight from 0.20 to 0.90 in 0.10 steps on the
SAME honest axis (13:30 pin, T = floor(hsf)+1, strict crossing,
look-ahead-free) and the SAME 2296-row cohort as
`scripts/honest_signal_accuracy_2026-10-14.py`. The remaining weight (1 - w_om)
is split equally among the WIRE'd 4-member (gefs, analog, traj, market).

It also sweeps the two-member om_gfs+market pair (the only two with any
independence) at equal weight and at an optimized weight.

READ-ONLY on every book. No trades writes. Writes ONLY:
  data/fusion_weight_sweep_2026-10-14.json
  artifacts/fusion-weight-sweep/fusion_weight_sweep_2026-10-14.json

usage: fusion_weight_sweep_2026-10-14.py [run]
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util as ilu
import json
import math
import pickle
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    fuse, calibrated_ceiling, DEFAULT_WEIGHTS,
)

# reuse the honest benchmark module (axis + cohort + om_gfs + analog/traj cache)
_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)

ART_DIR = REPO / "artifacts" / "fusion-weight-sweep"
OUT_JSON = REPO / "data" / "fusion_weight_sweep_2026-10-14.json"
OUT_ART_JSON = ART_DIR / "fusion_weight_sweep_2026-10-14.json"

SWEEP = [round(0.20 + 0.10 * i, 2) for i in range(8)]   # 0.20..0.90
PAIR_SWEEP = [round(0.0 + 0.05 * i, 2) for i in range(21)]  # 0.00..1.00
FOUR = ("gefs", "analog", "traj", "market")


def log(m):
    sys.stderr.write(f"[fws] {m}\n")
    sys.stderr.flush()


def build_cohort():
    """Reproduce the honest 2296-row fusion cohort (identical to HSA Part 2/3)."""
    om = HSA.fetch_omgfs(force=False)
    with gzip.open(HSA.GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(HSA.RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    rows = HSA.build_rows(recs, grid, om)

    # om_gfs bias/sigma (per-station & global) over the window
    from collections import defaultdict
    res = defaultdict(list)
    for row in rows.values():
        if row["om_raw"] is not None and row["settled_max"] is not None:
            res[row["station"]].append(row["om_raw"] - row["settled_max"])
    allb = [v for lst in res.values() for v in lst]
    omstats = {
        "bias_global": HSA._mean(allb),
        "sigma_global": HSA._std(allb),
        "bias_st": {st: HSA._mean(v) for st, v in res.items() if len(v) >= 3},
        "sigma_st": {st: HSA._std(v) for st, v in res.items() if len(v) >= 3},
    }

    at = HSA.build_analog_traj(rows, force=False)
    ladders = HSA.load_market_ladders("13:30:00")
    PIN = HSA.PIN_1330

    cohort = []
    for (st, ds), row in rows.items():
        if row["y"] is None or row["strike"] is None or row["hsf_pin"] is None:
            continue
        pg = (HSA.PDS.p_ge(row["gefs"],
                           max(HSA.SIGMA_GEFS_MIN, row["gefs_sd"] or HSA.SIGMA_GEFS_MIN),
                           row["strike"]) if row["gefs"] is not None else None)
        a = at.get((st, ds), {})
        pa = a.get("p_analog")
        pt = a.get("p_traj")
        pm = HSA.market_p_at(ladders.get((st, ds)), row["strike"])
        pog = HSA.signal_p_at(row, PIN, "om_gfs_bc_ps", omstats, ladders)
        if pg is None or pa is None or pt is None:
            continue
        cohort.append({
            "station": st, "date": ds, "y": row["y"], "strike": row["strike"],
            "p_gefs": pg, "p_analog": pa, "p_traj": pt,
            "p_market": pm, "p_om_gfs": pog,
        })
    return cohort


def _brier(ps, ys):
    return HSA.PDS.brier(ps, ys)


def _score(ps, ys):
    d = HSA.PDS.calibration_metrics(ps, ys)
    return {"n": len(ps), "brier": round(_brier(ps, ys), 4),
            "ece": d["ece"], "reliability": d["reliability"],
            "resolution": d["resolution"], "dir_acc": round(HSA.PDS.dir_acc(ps, ys), 4),
            "mean_p": round(sum(ps) / len(ps), 4)}


def fused_core(cohort, w_om, *, weights=None):
    """p_core for a given om_gfs weight; the 4-member split (1-w_om)/4 each.

    Uses the SHIPPED weighted core blend path (use_weights=True) so the sweep is
    the real fuse() blend, not a hand-rolled average. The trajectory modulator
    and calibrated ceiling are reported separately (raw + calibrated columns)
    but the headline is p_core — the exact quantity the honest baseline 0.1458
    is measured on.

    ``weights`` overrides the derived vector (used to reproduce the shipped
    DEFAULT_WEIGHTS baseline exactly).
    """
    if weights is not None:
        w = dict(weights)
    else:
        w = {k: (1.0 - w_om) / len(FOUR) for k in FOUR}
        w["om_gfs"] = w_om
    ps_core, ps_raw, ps_cal, ys = [], [], [], []
    for r in cohort:
        pf, meta = fuse(r["p_gefs"], r["p_analog"], r["p_traj"],
                        use_weights=True, weights=w,
                        p_market=r["p_market"], p_om_gfs=r["p_om_gfs"],
                        use_agreement_boost=False)
        ps_core.append(meta["p_core"])
        # pre-ceiling raw = 0.5 + mult*(p_core-0.5)
        ps_raw.append(min(0.999, max(0.001, 0.5 + meta["traj_mult"] * (meta["p_core"] - 0.5))))
        ps_cal.append(meta["calibrated_p"])
        ys.append(r["y"])
    return w, ps_core, ps_raw, ps_cal, ys


def pair_om_market(cohort, w_om):
    """Fuse ONLY om_gfs + market: w_om on om_gfs, (1-w_om) on market."""
    ps, ys = [], []
    for r in cohort:
        if r["p_om_gfs"] is None or r["p_market"] is None:
            continue
        pm = min(0.999, max(0.001, r["p_market"]))
        pog = min(0.999, max(0.001, r["p_om_gfs"]))
        ps.append(w_om * pog + (1.0 - w_om) * pm)
        ys.append(r["y"])
    return ps, ys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="run", choices=["run"])
    args = ap.parse_args()

    t0 = time.time()
    log("building honest cohort ...")
    cohort = build_cohort()
    log(f"cohort n = {len(cohort)}")
    ys_all = [r["y"] for r in cohort]

    # --- baselines -----------------------------------------------------------
    om_ps, om_ys = [], []
    for r in cohort:
        if r["p_om_gfs"] is not None:
            om_ps.append(r["p_om_gfs"])
            om_ys.append(r["y"])
    om_alone = _score(om_ps, om_ys)

    # shipped equal-ish default (DEFAULT_WEIGHTS, renormalized in fuse()) — the
    # documented 0.1458 p_core reference point
    _, base_core, base_raw, base_cal, base_ys = fused_core(
        cohort, 0.0, weights=DEFAULT_WEIGHTS)
    base_scores = {"p_core": _score(base_core, base_ys),
                   "raw": _score(base_raw, base_ys),
                   "calibrated": _score(base_cal, base_ys)}

    # --- sweep ---------------------------------------------------------------
    log(f"sweeping om_gfs weight {SWEEP} ...")
    rows = []
    for w_om in SWEEP:
        w, pc, pr, pcal, ys = fused_core(cohort, w_om)
        sc_core = _score(pc, ys)
        sc_raw = _score(pr, ys)
        sc_cal = _score(pcal, ys)
        rows.append({
            "weight_om_gfs": w_om,
            "weights_applied": {k: round(v, 4) for k, v in w.items()},
            "fused_Brier_pcore": sc_core["brier"],
            "fused_Brier_raw": sc_raw["brier"],
            "fused_Brier_calibrated": sc_cal["brier"],
            "ECE_pcore": sc_core["ece"],
            "delta_vs_om_gfs_alone": round(sc_core["brier"] - om_alone["brier"], 4),
            "dir_acc_pcore": sc_core["dir_acc"],
            "mean_p_pcore": sc_core["mean_p"],
        })
        log(f"  w_om={w_om:.2f}  core={sc_core['brier']:.4f}  "
            f"raw={sc_raw['brier']:.4f}  cal={sc_cal['brier']:.4f}  "
            f"ECE={sc_core['ece']}")

    best = min(rows, key=lambda r: r["fused_Brier_pcore"])

    # --- paired bootstrap significance (fused vs om_gfs alone) ---------------
    # per-row squared-error difference d_i = SE_fused_i - SE_om_i; a negative
    # mean is fused better. BCa-free percentile CI + P(fused better).
    log("paired bootstrap fused-vs-om_gfs ...")
    se_om = [(r["p_om_gfs"] - r["y"]) ** 2 for r in cohort]

    def _boot(w_om):
        _, pc, _, _, ys = fused_core(cohort, w_om)
        d = [(p - y) ** 2 - s for (p, y), s in zip(zip(pc, ys), se_om)]
        n = len(d)
        rng = random.Random(20261014)
        B = 2000
        means = []
        for _ in range(B):
            acc = 0.0
            for _ in range(n):
                acc += d[rng.randrange(n)]
            means.append(acc / n)
        means.sort()
        return {
            "delta_brier_vs_om_gfs_alone": round(sum(d) / n, 5),
            "ci95": [round(means[int(0.025 * B)], 5), round(means[int(0.975 * B)], 5)],
            "p_fused_better": round(sum(1 for x in means if x < 0) / B, 3),
        }

    for r in rows:
        r.update(_boot(r["weight_om_gfs"]))
    best = min(rows, key=lambda r: r["fused_Brier_pcore"])

    # --- pair om_gfs + market ------------------------------------------------
    log("sweeping om_gfs+market pair ...")
    pair_rows = []
    for w_om in PAIR_SWEEP:
        ps, ys = pair_om_market(cohort, w_om)
        sc = _score(ps, ys)
        pair_rows.append({"weight_om_gfs": w_om, "Brier": sc["brier"],
                          "ECE": sc["ece"], "n": sc["n"],
                          "delta_vs_om_gfs_alone": round(sc["brier"] - om_alone["brier"], 4)})
    pair_equal = next(r for r in pair_rows if abs(r["weight_om_gfs"] - 0.5) < 1e-9)
    pair_best = min(pair_rows, key=lambda r: r["Brier"])

    result = {
        "generated_at_utc": HSA.datetime.now(HSA.timezone.utc).isoformat(),
        "axis": ("strict strike-crossing, T=floor(hsf@13:30)+1, "
                 "crossed = settled_max > T (== is LOSS)"),
        "truth": "canonical GHCN settlement",
        "cohort": {"n": len(cohort), "window": [HSA.MIGRATION_START, HSA.MIGRATION_END],
                   "pin": "13:30 station-local", "look_ahead_free": True},
        "baselines": {
            "om_gfs_bc_ps_alone": om_alone,
            "shipped_default_weights": dict(DEFAULT_WEIGHTS),
            "shipped_default_pcore": base_scores["p_core"],
            "shipped_default_raw": base_scores["raw"],
            "shipped_default_calibrated": base_scores["calibrated"],
        },
        "sweep_5member": rows,
        "best_5member": best,
        "pair_om_gfs_market": {
            "equal_weight_0.5": pair_equal,
            "best": pair_best,
            "sweep": pair_rows,
        },
        "notes": [
            "p_core = shipped weighted core blend BEFORE the trajectory modulator; "
            "it is the exact quantity the honest baseline 0.1458 is measured on.",
            "raw = 0.5 + traj_mult*(p_core-0.5); calibrated = per-station ceiling clamp.",
            "The 4-member split is (1-w_om)/4 on each of gefs, analog, traj, market.",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ART_JSON.write_text(json.dumps(result, indent=2))
    log(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size} bytes), {time.time()-t0:.1f}s")
    print(json.dumps({
        "cohort_n": len(cohort),
        "om_gfs_alone_brier": om_alone["brier"],
        "best_5member": best,
        "pair_equal": pair_equal,
        "pair_best": pair_best,
    }, indent=2))


if __name__ == "__main__":
    main()
