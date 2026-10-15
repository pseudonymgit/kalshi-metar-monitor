#!/usr/bin/env python3
"""verify_calibration_fix_2026-10-15.py — INDEPENDENT recomputation of the
honest-cohort raw-vs-calibrated Brier, plus a head-to-head of calibration
variants, to decide the ITEM-1 repair.

This does NOT re-run the audit's summary numbers. It rebuilds the sole-om_gfs
cohort from the same frozen caches (grid/ASOS/om_gfs), recomputes the om_gfs
bias/sigma recipe itself, and scores EVERY variant with its own Brier on a
5-fold, date-ordered cross-validation (so a fitted map cannot peek at its test
rows). The decision is whatever the out-of-fold numbers support.

Cohort: sole om_gfs (bias-corrected, per-station) P(high >= T) at the 13:30 pin
— the SHIPPED HIGH path (Workstream AF). n ~= 2927.

Variants:
  raw                      p_raw
  ceiling_only             min(p_raw, ceiling)              (clamp, no shrink)
  shrink_ece_047           the SHIPPED formula, ECE=0.47
  shrink_ece_00376         the same shape, ECE=om_gfs's own
  identity_ece_0           the same shape, ECE=0.0 (== clamp + 0.01 floor)
  platt_cv                 logistic fit, 5-fold out-of-fold
  isotonic_cv              PAVA isotonic fit, 5-fold out-of-fold

READ-ONLY. Writes only artifacts/calibration-fix/verify_calibration_fix_2026-10-15.json

usage: python3 scripts/verify_calibration_fix_2026-10-15.py
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.honest_calibration import (  # noqa: E402
    honest_calibrated_p, station_ceiling, ECE, POOLED_CEILING,
)
from core._pava import _pava_isotonic_fit  # noqa: E402

_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)
PDS = HSA.PDS

GRID = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
PIN = 810

OUT = REPO / "artifacts" / "calibration-fix" / "verify_calibration_fix_2026-10-15.json"


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def ece(ps, ys, n_bins=10):
    n = len(ps)
    bins = defaultdict(list)
    for p, y in zip(ps, ys):
        bins[min(n_bins - 1, max(0, int(p * n_bins)))].append((p, y))
    out = 0.0
    for b in bins.values():
        pk = sum(p for p, _ in b) / len(b)
        ok = sum(y for _, y in b) / len(b)
        out += len(b) / n * abs(pk - ok)
    return out


def reliability(ps, ys, n_bins=10):
    n = len(ps)
    bins = defaultdict(list)
    for p, y in zip(ps, ys):
        bins[min(n_bins - 1, max(0, int(p * n_bins)))].append((p, y))
    rel = 0.0
    for b in bins.values():
        pk = sum(p for p, _ in b) / len(b)
        ok = sum(y for _, y in b) / len(b)
        rel += len(b) / n * (pk - ok) ** 2
    return rel


def fit_platt(xs, ys):
    """2-param logistic MLE via scipy (same family as core/platt_calibration)."""
    import numpy as np
    from scipy.optimize import minimize
    x = np.clip(np.asarray(xs, dtype=float), 1e-6, 1 - 1e-6)
    y = np.asarray(ys, dtype=float)
    z = np.log(x / (1 - x))

    def nll(theta):
        a, b = theta
        w = a * z + b
        # stable log-likelihood
        ll = y * np.logaddexp(0, -w) + (1 - y) * np.logaddexp(0, w)
        return ll.sum()

    r = minimize(nll, np.array([1.0, 0.0]), method="Nelder-Mead",
                 options={"xatol": 1e-6, "fatol": 1e-9, "maxiter": 2000})
    return tuple(r.x)


def apply_platt(theta, x):
    a, b = theta
    z = math.log(max(1e-6, min(1 - 1e-6, x)) / (1 - max(1e-6, min(1 - 1e-6, x))))
    return 1.0 / (1.0 + math.exp(-(a * z + b)))


def fit_isotonic(xs, ys):
    import numpy as np
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    xk, yk = _pava_isotonic_fit(x, y)
    return np.asarray(xk, dtype=float), np.asarray(yk, dtype=float)


def apply_isotonic(xk, yk, x):
    import numpy as np
    if len(xk) == 0:
        return 0.5
    i = int(np.searchsorted(xk, x, side="right")) - 1
    i = max(0, min(len(yk) - 1, i))
    return float(yk[i])


def main():
    grid = pickle.load(gzip.open(GRID, "rb"))
    recs = pickle.load(gzip.open(RECS, "rb"))
    om = {(k.split("|")[0], k.split("|")[1]): v
          for k, v in json.loads(OM.read_text()).items()}
    rows = HSA.build_rows(recs, grid, om)

    # ---- om_gfs bias/sigma (own recompute, matches HSA recipe) ------------
    res = defaultdict(list)
    for r in rows.values():
        if r["om_raw"] is not None and r["settled_max"] is not None:
            res[r["station"]].append(r["om_raw"] - r["settled_max"])
    allb = [v for lst in res.values() for v in lst]
    bg, sgg = HSA._mean(allb), HSA._std(allb)
    bs = {st: HSA._mean(v) for st, v in res.items() if len(v) >= 3}
    sgs = {st: HSA._std(v) for st, v in res.items() if len(v) >= 3}
    omstats = {"bias_global": bg, "sigma_global": sgg,
               "bias_st": bs, "sigma_st": sgs}

    # ---- sole cohort ------------------------------------------------------
    cohort = []
    for (st, ds), row in rows.items():
        if row["y"] is None:
            continue
        p = HSA.signal_p_at(row, PIN, "om_gfs_bc_ps", omstats, {PIN: {}})
        if p is None or row["pins"][PIN]["T"] is None:
            continue
        cohort.append({"st": st, "ds": ds, "y": int(row["y"]),
                       "p_raw": float(p), "ce": station_ceiling(st)})
    cohort.sort(key=lambda r: (r["ds"], r["st"]))
    xs = [r["p_raw"] for r in cohort]
    ys = [r["y"] for r in cohort]
    ces = [r["ce"] for r in cohort]

    out = {
        "generated_by": "scripts/verify_calibration_fix_2026-10-15.py",
        "cohort": {"n": len(cohort), "base_rate": round(sum(ys) / len(ys), 4)},
        "raw_metrics": {
            "brier": round(brier(xs, ys), 4),
            "ece": round(ece(xs, ys), 4),
            "reliability": round(reliability(xs, ys), 4),
        },
        "constants": {"ECE_shipped": ECE, "POOLED_CEILING": POOLED_CEILING},
    }

    # ECE of the shipped (raw) signal — the value the shrink SHOULD use
    om_ece = ece(xs, ys)
    out["om_gfs_own_ece"] = round(om_ece, 4)

    # ---- point-estimate variants (no fitting on target) -------------------
    def clamp(v, ce):
        return round(min(ce, max(0.01, v)), 4)

    variants = {
        "raw": xs,
        "ceiling_only": [clamp(x, ce) for x, ce in zip(xs, ces)],
        "shrink_ece_047": [honest_calibrated_p(x, ce) for x, ce in zip(xs, ces)],
        "shrink_ece_omgfs": [honest_calibrated_p(x, ce, ece=om_ece)
                             for x, ce in zip(xs, ces)],
        "identity_ece_0": [honest_calibrated_p(x, ce, ece=0.0)
                           for x, ce in zip(xs, ces)],
    }
    out["variants_point"] = {
        k: {"brier": round(brier(v, ys), 4), "ece": round(ece(v, ys), 4),
            "reliability": round(reliability(v, ys), 4),
            "mean_p": round(sum(v) / len(v), 4)}
        for k, v in variants.items()
    }

    # ---- 5-fold, date-ordered CV for FITTED maps --------------------------
    dates = sorted(set(r["ds"] for r in cohort))
    k = 5
    fold_of_date = {d: i % k for i, d in enumerate(dates)}
    platt_oof = [None] * len(cohort)
    iso_oof = [None] * len(cohort)
    for f in range(k):
        tr = [i for i, r in enumerate(cohort) if fold_of_date[r["ds"]] != f]
        te = [i for i, r in enumerate(cohort) if fold_of_date[r["ds"]] == f]
        if not tr or not te:
            continue
        xtr = [cohort[i]["p_raw"] for i in tr]
        ytr = [cohort[i]["y"] for i in tr]
        theta = fit_platt(xtr, ytr)
        xk, yk = fit_isotonic(xtr, ytr)
        for i in te:
            platt_oof[i] = apply_platt(theta, cohort[i]["p_raw"])
            iso_oof[i] = apply_isotonic(xk, yk, cohort[i]["p_raw"])
    platt_oof = [p if p is not None else 0.5 for p in platt_oof]
    iso_oof = [p if p is not None else 0.5 for p in iso_oof]
    out["variants_cv"] = {
        "platt_cv": {"brier": round(brier(platt_oof, ys), 4),
                     "ece": round(ece(platt_oof, ys), 4),
                     "mean_p": round(sum(platt_oof) / len(platt_oof), 4)},
        "isotonic_cv": {"brier": round(brier(iso_oof, ys), 4),
                        "ece": round(ece(iso_oof, ys), 4),
                        "mean_p": round(sum(iso_oof) / len(iso_oof), 4)},
    }

    # ---- per-station for the winner candidates ---------------------------
    def per_station(vals):
        st_rows = defaultdict(list)
        for r, v in zip(cohort, vals):
            st_rows[r["st"]].append((v, r["y"]))
        wins = 0
        n = 0
        detail = {}
        for st, lst in sorted(st_rows.items()):
            if len(lst) < 30:
                continue
            n += 1
            bv = brier([p for p, _ in lst], [y for _, y in lst])
            br = brier([cohort[i]["p_raw"] for i, r in enumerate(cohort)
                        if r["st"] == st], [r["y"] for r in cohort if r["st"] == st])
            better = bv < br
            wins += better
            detail[st] = {"n": len(lst), "brier": round(bv, 4),
                          "raw": round(br, 4), "delta": round(bv - br, 4),
                          "better_than_raw": better}
        return {"n_stations": n, "beats_raw": wins,
                "beats_every": wins == n, "detail": detail}

    out["per_station"] = {
        "ceiling_only": per_station(variants["ceiling_only"]),
        "identity_ece_0": per_station(variants["identity_ece_0"]),
        "shrink_ece_omgfs": per_station(variants["shrink_ece_omgfs"]),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))

    print(json.dumps({
        "n": out["cohort"]["n"],
        "base_rate": out["cohort"]["base_rate"],
        "raw_brier": out["raw_metrics"]["brier"],
        "raw_ece": out["raw_metrics"]["ece"],
        "raw_reliability": out["raw_metrics"]["reliability"],
        "om_gfs_own_ece": out["om_gfs_own_ece"],
        "point_variants": {k: v["brier"] for k, v in out["variants_point"].items()},
        "cv_variants": {k: v["brier"] for k, v in out["variants_cv"].items()},
        "ceiling_only_beats_every": out["per_station"]["ceiling_only"]["beats_every"],
        "identity_beats_every": out["per_station"]["identity_ece_0"]["beats_every"],
    }, indent=2))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
