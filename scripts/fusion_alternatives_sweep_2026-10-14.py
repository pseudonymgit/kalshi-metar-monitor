#!/usr/bin/env python3
"""fusion_alternatives_sweep_2026-10-14.py — three structurally different
signal-combination approaches vs om_gfs alone, on the honest 2296 cohort.

Dan (2026-10-14): "Run the 3 fusion alternatives sweep."

===============================================================================
PRE-REGISTRATION — WRITTEN BEFORE RUNNING (2026-10-14)
===============================================================================
Axis      : strict strike-crossing, T = floor(hsf@13:30)+1,
            y = settled_max > T  (== is LOSS).  Truth = canonical GHCN.
            LOOK-AHEAD-FREE (no bucket with mod > 13:30 used).
Cohort    : the exact honest 2296-row fusion cohort (2026-04-21..2026-10-09),
            identical to scripts/fusion_weight_sweep_2026-10-14.py.
Sources   : gefs, analog, traj, market, om_gfs(bc, per-station)  [the 5 members]
Split     : random 50/50 FIT/TEST, seed 20261014 (fixed here; not tuned).
Baselines : computed on the SAME TEST rows:
            (B0) om_gfs(bc,ps) alone            — the honest best single member
            (B1) om_gfs@0.80 5-member fusion    — the current best sweep point
                                                 (docs/plans/FUSION-WEIGHT-SWEEP)

An alternative "BEATS om_gfs alone" iff ALL THREE hold on TEST:
  (i)   DeltaBrier(TEST) = Brier(alt) - Brier(om_gfs alone) < 0
  (ii)  paired-bootstrap P(alt better than om_gfs alone) >= 0.95
        (2000 resamples of per-row squared-error differences, seed 20261014)
  (iii) ECE(alt) <= ECE(om_gfs alone) + 0.02   (no calibration regression)

Alternative A — Inverse-variance weighting.
  w_i ∝ 1 / Brier_i^2 , Brier_i = source i's solo Brier on FIT. Normalize over
  sources present in each row. Weak sources get near-zero weight automatically.

Alternative B — Bayesian model averaging (BMA).
  w_i ∝ P(data | source i) = Π_FIT  p_i^y (1-p_i)^(1-y)  (equal priors),
  computed in log-space (clip p to [0.01,0.99]). Posterior = normalized.
  More principled than A; needs source-level likelihoods (this is the honest
  reading of "the likelihood that it is the true forecast given the outcomes").

Alternative C — Stacking (L1 logistic meta-model).
  Features = the 5 source probabilities (missing -> 0.5 imputation, noted).
  LogisticRegression(penalty='l1', solver='liblinear', C=1.0), fit on FIT,
  scored on TEST. L1 zeroes useless sources automatically.

If NONE beats om_gfs alone -> the honest recommendation is to DROP the ensemble
and ship om_gfs(bc, per-station) alone.

READ-ONLY on every book. No trades writes. Writes ONLY:
  data/fusion_alternatives_sweep_2026-10-14.json
  artifacts/fusion-alternatives/fusion_alternatives_sweep_2026-10-14.json

usage: fusion_alternatives_sweep_2026-10-14.py [run]
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
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.multi_forecaster_fusion import fuse, DEFAULT_WEIGHTS  # noqa: E402

# reuse the honest benchmark module (axis + cohort + om_gfs + analog/traj cache)
_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)

ART_DIR = REPO / "artifacts" / "fusion-alternatives"
OUT_JSON = REPO / "data" / "fusion_alternatives_sweep_2026-10-14.json"
OUT_ART_JSON = ART_DIR / "fusion_alternatives_sweep_2026-10-14.json"

SEED = 20261014
SOURCES = ("gefs", "analog", "traj", "market", "om_gfs")
FEAT = {"gefs": "p_gefs", "analog": "p_analog", "traj": "p_traj",
        "market": "p_market", "om_gfs": "p_om_gfs"}
BOOT_B = 2000
OMGFS_080 = 0.80


def log(m):
    sys.stderr.write(f"[fas] {m}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# cohort (identical to fusion_weight_sweep_2026-10-14.py::build_cohort)
# ---------------------------------------------------------------------------
def build_cohort():
    om = HSA.fetch_omgfs(force=False)
    with gzip.open(HSA.GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(HSA.RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    rows = HSA.build_rows(recs, grid, om)

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


# ---------------------------------------------------------------------------
# scoring (reuse the shared diversity-study metric definitions)
# ---------------------------------------------------------------------------
def _sc(ps, ys):
    d = HSA.PDS.calibration_metrics(ps, ys)
    return {"n": len(ps), "brier": round(HSA.PDS.brier(ps, ys), 5),
            "ece": d["ece"], "reliability": d["reliability"],
            "resolution": d["resolution"],
            "dir_acc": round(HSA.PDS.dir_acc(ps, ys), 4),
            "base_rate": round(sum(ys) / len(ys), 4),
            "mean_p": round(sum(ps) / len(ps), 4)}


def _clip(v, lo=0.01, hi=0.99):
    return min(hi, max(lo, float(v)))


def weight_renorm_fuse(row, w):
    """Σ w_i p_i / Σ w_i over sources PRESENT in the row."""
    num = den = 0.0
    for s in SOURCES:
        p = row[FEAT[s]]
        if p is None:
            continue
        wi = max(0.0, w.get(s, 0.0))
        if wi <= 0:
            continue
        num += wi * _clip(p)
        den += wi
    return (num / den) if den > 0 else None


def paired_boot(diffs, seed=SEED, B=BOOT_B):
    n = len(diffs)
    rng = random.Random(seed)
    means = []
    for _ in range(B):
        acc = 0.0
        for _ in range(n):
            acc += diffs[rng.randrange(n)]
        means.append(acc / n)
    means.sort()
    return {
        "delta_brier": round(sum(diffs) / n, 5),
        "ci95": [round(means[int(0.025 * B)], 5), round(means[int(0.975 * B)], 5)],
        "p_alt_better": round(sum(1 for x in means if x < 0) / B, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="run", choices=["run"])
    args = ap.parse_args()
    t0 = time.time()

    log("building honest 2296 cohort ...")
    cohort = build_cohort()
    n = len(cohort)
    log(f"cohort n = {n}")
    n_market = sum(1 for r in cohort if r["p_market"] is not None)
    log(f"market present on {n_market}/{n} rows")

    # ---- fixed 50/50 split (seed) ------------------------------------------
    idx = list(range(n))
    random.Random(SEED).shuffle(idx)
    half = n // 2
    fit_idx = set(idx[:half])
    test_idx = [i for i in idx[half:]]
    fit = [cohort[i] for i in range(n) if i in fit_idx]
    test = [cohort[i] for i in test_idx]
    log(f"FIT n={len(fit)}  TEST n={len(test)}")

    # ---- TEST baselines ----------------------------------------------------
    om_test_ps = [_clip(r["p_om_gfs"]) if r["p_om_gfs"] is not None else None
                  for r in test]
    om_rows = [(p, r["y"]) for p, r in zip(om_test_ps, test) if p is not None]
    om_alone_test = _sc([p for p, _ in om_rows], [y for _, y in om_rows])

    # om_gfs@0.80 fusion on TEST (reuse the shipped weighted blend)
    def fused080(rows):
        w = {k: (1.0 - OMGFS_080) / 4.0 for k in ("gefs", "analog", "traj", "market")}
        w["om_gfs"] = OMGFS_080
        ps, ys = [], []
        for r in rows:
            _, meta = fuse(r["p_gefs"], r["p_analog"], r["p_traj"],
                           use_weights=True, weights=w,
                           p_market=r["p_market"], p_om_gfs=r["p_om_gfs"],
                           use_agreement_boost=False)
            ps.append(meta["p_core"])
            ys.append(r["y"])
        return ps, ys

    ps080, ys080 = fused080(test)
    base080_test = _sc(ps080, ys080)

    # ---- ALTERNATIVE A — inverse-variance weighting ------------------------
    log("Alt A: inverse-variance ...")
    solo_fit = {}
    for s in SOURCES:
        ps = [_clip(r[FEAT[s]]) for r in fit if r[FEAT[s]] is not None]
        ys = [r["y"] for r in fit if r[FEAT[s]] is not None]
        solo_fit[s] = {"n": len(ps), "brier": HSA.PDS.brier(ps, ys)} if ps else {"n": 0}
    # weights ∝ 1/Brier^2 over sources with a solo Brier
    rawA = {}
    for s in SOURCES:
        b = solo_fit[s].get("brier")
        rawA[s] = (1.0 / (b * b)) if b and b > 0 else 0.0
    denA = sum(rawA.values())
    wA = {s: (rawA[s] / denA) for s in SOURCES} if denA > 0 else {s: 0.0 for s in SOURCES}
    # sensitivity: a sharper inverse-variance exponent (w ~ 1/Brier^4)
    rawA4 = {s: (1.0 / (solo_fit[s]["brier"] ** 4))
             if solo_fit[s].get("brier") else 0.0 for s in SOURCES}
    denA4 = sum(rawA4.values())
    wA4 = ({s: (rawA4[s] / denA4) for s in SOURCES} if denA4 > 0
           else {s: 0.0 for s in SOURCES})
    a4_rows = [(weight_renorm_fuse(r, wA4), r["y"], r) for r in test]
    a4_rows = [(p, y, r) for p, y, r in a4_rows if p is not None]
    altA4 = _sc([p for p, _, _ in a4_rows], [y for _, y, _ in a4_rows])
    a_rows = [(weight_renorm_fuse(r, wA), r["y"], r) for r in test]
    a_rows = [(p, y, r) for p, y, r in a_rows if p is not None]
    altA = _sc([p for p, _, _ in a_rows], [y for _, y, _ in a_rows])
    se_omA = [(_clip(r["p_om_gfs"]) - y) ** 2 for p, y, r in a_rows
              if r["p_om_gfs"] is not None]
    pA = [p for p, _, _ in a_rows]
    yA = [y for _, y, _ in a_rows]
    omA = [_clip(r["p_om_gfs"]) for _, _, r in a_rows]
    bootA = paired_boot([(pf - y) ** 2 - (_clip(pm) - y) ** 2
                         for pf, y, pm in zip(pA, yA, omA)]) if all(
                             r["p_om_gfs"] is not None for _, _, r in a_rows) else None

    # ---- ALTERNATIVE B — Bayesian model averaging --------------------------
    log("Alt B: BMA (source log-likelihood) ...")

    def _bma_weights(rows):
        """Posterior over sources = softmax of PER-ROW MEAN log-likelihood.

        The mean (not the sum) is essential: sources are present on different
        row counts (market on ~284/2296), and a summed NLL rewards the source
        evaluated on the fewest rows. Equal priors.
        """
        ll = {}
        for s in SOURCES:
            acc, got = 0.0, 0
            for r in rows:
                if r[FEAT[s]] is None:
                    continue
                p = _clip(r[FEAT[s]])
                acc += math.log(p if r["y"] == 1 else (1.0 - p))
                got += 1
            ll[s] = {"nll_mean": (acc / got) if got else float("-inf"),
                     "nll_sum": acc, "n": got}
        mx = max(v["nll_mean"] for v in ll.values())
        raw = {s: math.exp(ll[s]["nll_mean"] - mx) for s in SOURCES}
        den = sum(raw.values())
        w = {s: (raw[s] / den) for s in SOURCES} if den > 0 else {s: 0.0 for s in SOURCES}
        return ll, w

    ll, wB = _bma_weights(fit)
    # common-support variant: weights fit on rows where ALL 5 sources present
    fit_cs = [r for r in fit if all(r[FEAT[s]] is not None for s in SOURCES)]
    _, wB_cs = _bma_weights(fit_cs) if fit_cs else ({}, wB)
    cs_rows = [(weight_renorm_fuse(r, wB_cs), r["y"], r) for r in test]
    cs_rows = [(p, y, r) for p, y, r in cs_rows if p is not None]
    altB_cs = _sc([p for p, _, _ in cs_rows], [y for _, y, _ in cs_rows])
    b_rows = [(weight_renorm_fuse(r, wB), r["y"], r) for r in test]
    b_rows = [(p, y, r) for p, y, r in b_rows if p is not None]
    altB = _sc([p for p, _, _ in b_rows], [y for _, y, _ in b_rows])
    allB_om = all(r["p_om_gfs"] is not None for _, _, r in b_rows)
    bootB = paired_boot([(p - y) ** 2 - (_clip(r["p_om_gfs"]) - y) ** 2
                         for p, y, r in b_rows]) if allB_om else None

    # ---- ALTERNATIVE C — stacking (L1 logistic) ----------------------------
    log("Alt C: L1 logistic stacking ...")
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    def feat_vec(r):
        return [0.5 if r[FEAT[s]] is None else _clip(r[FEAT[s]]) for s in SOURCES]

    Xfit = np.array([feat_vec(r) for r in fit])
    yfit = np.array([r["y"] for r in fit])
    Xte = np.array([feat_vec(r) for r in test])
    yte = np.array([r["y"] for r in test])
    # logit-space features improve the linear model; probabilities are on (0,1)
    eps = 1e-6
    Xfit_l = np.log(np.clip(Xfit, eps, 1 - eps) / (1 - np.clip(Xfit, eps, 1 - eps)))
    Xte_l = np.log(np.clip(Xte, eps, 1 - eps) / (1 - np.clip(Xte, eps, 1 - eps)))
    sc = StandardScaler().fit(Xfit_l)
    clf = LogisticRegression(penalty="l1", solver="liblinear", C=1.0,
                             max_iter=2000, random_state=SEED)
    clf.fit(sc.transform(Xfit_l), yfit)
    pC = clf.predict_proba(sc.transform(Xte_l))[:, 1]
    altC = _sc(list(map(float, pC)), list(map(int, yte)))
    bootC = paired_boot([(float(p) - int(y)) ** 2 - (_clip(r["p_om_gfs"]) - int(y)) ** 2
                         for p, y, r in zip(pC, yte, test)
                         if r["p_om_gfs"] is not None])
    coef = {s: round(float(c), 5) for s, c in zip(SOURCES, clf.coef_[0])}
    zeroed = [s for s, c in coef.items() if abs(c) < 1e-9]

    # ---- verdicts ----------------------------------------------------------
    def verdict(alt, boot):
        beats = (alt["brier"] < om_alone_test["brier"]
                 and boot is not None and boot["p_alt_better"] >= 0.95
                 and alt["ece"] <= om_alone_test["ece"] + 0.02)
        return {"beats_om_gfs_alone": bool(beats),
                "delta_brier_vs_om_gfs_alone": (round(alt["brier"] - om_alone_test["brier"], 5)),
                "delta_brier_vs_om_gfs_080": (round(alt["brier"] - base080_test["brier"], 5)),
                "meets_ece_criterion": bool(alt["ece"] <= om_alone_test["ece"] + 0.02),
                "boot": boot}

    result = {
        "generated_at_utc": HSA.datetime.now(HSA.timezone.utc).isoformat(),
        "preregistration": {
            "axis": "strict crossing, T=floor(hsf@13:30)+1, y=settled_max>T (== LOSS)",
            "truth": "canonical GHCN settlement",
            "cohort_n": n, "window": [HSA.MIGRATION_START, HSA.MIGRATION_END],
            "look_ahead_free": True, "split": "random 50/50 FIT/TEST",
            "seed": SEED, "boot_resamples": BOOT_B,
            "beat_criterion": [
                "DeltaBrier(TEST) < 0",
                "paired-bootstrap P(alt better) >= 0.95",
                "ECE(alt) <= ECE(om_gfs alone) + 0.02",
            ],
        },
        "sources": list(SOURCES),
        "market_present_rows": {"cohort": n_market, "of": n},
        "fit_n": len(fit), "test_n": len(test),
        "baselines_test": {
            "om_gfs_bc_ps_alone": om_alone_test,
            "om_gfs_080_5member": base080_test,
        },
        "alternative_A_inverse_variance": {
            "solo_Brier_on_FIT": {s: round(solo_fit[s]["brier"], 5)
                                  for s in SOURCES if solo_fit[s].get("brier") is not None},
            "weights": {s: round(wA[s], 5) for s in SOURCES},
            "test": altA, **verdict(altA, bootA),
            "sensitivity_exponent4": {
                "weights": {s: round(wA4[s], 5) for s in SOURCES},
                "test": altA4,
                "delta_brier_vs_om_gfs_alone": round(
                    altA4["brier"] - om_alone_test["brier"], 5),
                "delta_brier_vs_om_gfs_080": round(
                    altA4["brier"] - base080_test["brier"], 5),
            },
        },
        "alternative_B_bma": {
            "source_mean_loglik_on_FIT": {
                s: {"nll_mean": round(ll[s]["nll_mean"], 4), "n": ll[s]["n"]}
                for s in SOURCES},
            "weights": {s: round(wB[s], 5) for s in SOURCES},
            "test": altB, **verdict(altB, bootB),
            "common_support_variant": {
                "fit_rows_all5": len(fit_cs),
                "weights": {s: round(wB_cs[s], 5) for s in SOURCES},
                "test": altB_cs,
                "delta_brier_vs_om_gfs_alone": round(
                    altB_cs["brier"] - om_alone_test["brier"], 5),
            },
        },
        "alternative_C_stacking": {
            "model": "LogisticRegression(penalty=l1, liblinear, C=1.0) on logit(features)",
            "coefficients": coef, "zeroed_sources": zeroed,
            "missing_imputation": "0.5 for absent source probs (market absent on most rows)",
            "test": altC, **verdict(altC, bootC),
        },
        "notes": [
            "FIT/TEST split fixed at seed 20261014; no tuning on TEST.",
            "om_gfs alone baseline recomputed on the SAME TEST rows as each alt.",
            "Alt A weights are 1/Brier^2 of each source's solo FIT Brier.",
            "Alt B weights are the normalized source likelihood (equal priors).",
            "Alt C imputes absent source probs at 0.5 (market absent on ~88% of rows).",
        ],
    }

    beats = [k for k in ("alternative_A_inverse_variance", "alternative_B_bma",
                         "alternative_C_stacking") if result[k]["beats_om_gfs_alone"]]
    result["summary"] = {
        "any_beats_om_gfs_alone": bool(beats),
        "beating_alternatives": beats,
        "om_gfs_alone_brier_test": om_alone_test["brier"],
        "om_gfs_080_brier_test": base080_test["brier"],
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ART_JSON.write_text(json.dumps(result, indent=2))
    log(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size} bytes), {time.time()-t0:.1f}s")
    print(json.dumps(result["summary"], indent=2))
    for k in ("alternative_A_inverse_variance", "alternative_B_bma",
              "alternative_C_stacking"):
        r = result[k]
        print(f"{k:36s} brier={r['test']['brier']:.4f} ece={r['test']['ece']:.4f} "
              f"dir={r['test']['dir_acc']:.4f} beats={r['beats_om_gfs_alone']} "
              f"d_om={r['delta_brier_vs_om_gfs_alone']:+.5f} "
              f"d_080={r['delta_brier_vs_om_gfs_080']:+.5f}")


if __name__ == "__main__":
    main()
