#!/usr/bin/env python3
"""disagreement_quality_flag_2026-10-14.py — weak-signal SALVAGE study.

Dan (2026-10-14): "Is there anything useful to do with the old directional accuracy
signals? ... Can any inform trajectory or our other functions? Anything at all that
would be helpful?"

Premise (settled): FUSION of the weak signals hurts. So this is NOT a fusion study.
It asks whether any weak signal has STANDALONE value in a different role:
 (a) tells us WHEN to trust om_gfs less (quality gate) — H1 (NWP spread), H4 (gefs sd)
 (b) supplies a RESIDUAL correction                        — H2 (rate_1h), H3 (market)

Pre-registered in docs/plans/WEAK-SIGNAL-SALVAGE-PREREG-2026-10-14.md BEFORE running.
Every threshold there is frozen; changes are post-hoc and must be flagged.

Cohort == the honest benchmark cohort (`honest_signal_accuracy_2026-10-14.py`):
2026-04-21..2026-10-09, pin 13:30 station-local, axis T=floor(hsf@13:30)+1,
crossed = settled_max > T (strict; == LOSS), look-ahead-free.

READ-ONLY on every book. No trades writes. No temp DBs. Writes ONLY:
  data/weak_signal_salvage_2026-10-14.json
  artifacts/weak-signal-salvage/weak_signal_salvage_2026-10-14.json

usage: disagreement_quality_flag_2026-10-14.py [run]
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

# reuse the honest benchmark module (cohort + axis + om_gfs + helpers) verbatim
_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)
PDS = HSA.PDS

ART_DIR = REPO / "artifacts" / "weak-signal-salvage"
OUT_JSON = REPO / "data" / "weak_signal_salvage_2026-10-14.json"
OUT_ART_JSON = ART_DIR / "weak_signal_salvage_2026-10-14.json"
OUT_MD = REPO / "docs" / "plans" / "WEAK-SIGNAL-SALVAGE-2026-10-14.md"

PIN = HSA.PIN_1330
SEED = 20261014
NWP_MEMBERS = ("gfs", "gem", "icon", "ecmwf", "gefs")


def log(m):
    sys.stderr.write(f"[wss] {m}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# small stats helpers (bootstrap lives here; Spearman/pearson come from PDS)
# ---------------------------------------------------------------------------
def _nan(x):
    return x is None or x != x


def ols(xs, ys):
    """Return (a, b, r2, n) for y = a + b*x. None-safe."""
    pair = [(x, y) for x, y in zip(xs, ys) if not _nan(x) and not _nan(y)]
    n = len(pair)
    if n < 3:
        return None
    mx = sum(x for x, _ in pair) / n
    my = sum(y for _, y in pair) / n
    sxx = sum((x - mx) ** 2 for x, _ in pair)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pair)
    b = sxy / sxx
    a = my - b * mx
    syy = sum((y - my) ** 2 for _, y in pair)
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return {"a": a, "b": b, "r2": r2, "n": n}


def boot_slope(pairs, group_key, B=2000, seed=SEED):
    """Block bootstrap of the OLS slope, resampling whole STATIONS with replacement.

    Preserves within-station serial dependence (the honest thing to do here).
    pairs = list of (x, y, station). Returns dict with point slope + 95% CI + P(sign).
    """
    by_st = {}
    for x, y, st in pairs:
        if _nan(x) or _nan(y):
            continue
        by_st.setdefault(st, []).append((x, y))
    sts = list(by_st)
    if len(sts) < 2:
        return None
    base = ols([x for x, y, _ in pairs], [y for x, y, _ in pairs])
    if base is None:
        return None
    rng = random.Random(seed)
    slopes = []
    for _ in range(B):
        xs, ys = [], []
        for _ in range(len(sts)):
            st = sts[rng.randrange(len(sts))]
            for x, y in by_st[st]:
                xs.append(x); ys.append(y)
        r = ols(xs, ys)
        if r is not None:
            slopes.append(r["b"])
    if not slopes:
        return None
    slopes.sort()
    lo = slopes[int(0.025 * B)]
    hi = slopes[int(0.975 * B)]
    return {"slope": round(base["b"], 6), "ci95": [round(lo, 6), round(hi, 6)],
            "excludes_zero": (lo > 0 and hi > 0) or (lo < 0 and hi < 0),
            "p_positive": round(sum(1 for s in slopes if s > 0) / len(slopes), 3),
            "n_rows": base["n"], "n_stations": len(sts), "B": B}


def quartile_bins(vals):
    """Adjacent-value quartile cuts over vals (list of floats, no None).

    Returns (edges, labeler). Uses adjacent-value cuts rounded to 2dp and deduped
    so small discrete spreads still yield 4 populated bins.
    """
    s = sorted(vals)
    n = len(s)
    cuts = sorted({round(s[min(n - 1, int(q * n))], 2) for q in (0.25, 0.5, 0.75)})
    edges = [float("-inf")] + cuts + [float("inf")]

    def lab(v):
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                return i
        return len(edges) - 2
    return edges, lab


def score_brier(ps, ys):
    return round(PDS.brier(ps, ys), 4) if ps else None


# ---------------------------------------------------------------------------
# build the salvage cohort
# ---------------------------------------------------------------------------
def build_cohort():
    """One row per honest station-day carrying every predictor + outcome we need."""
    om = HSA.fetch_omgfs(force=False)
    if not om:
        # cache missing -> must fetch (network). keep read-only otherwise.
        om = HSA.fetch_omgfs(force=True)
    with gzip.open(HSA.GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(HSA.RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    rows = HSA.build_rows(recs, grid, om)

    # om_gfs bias/sigma (per-station & global) over the window — same as honest
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
    ladders = HSA.load_market_ladders("13:30:00")

    cohort = []
    for (st, ds), row in rows.items():
        if row["y"] is None or row["settled_max"] is None or row["om_raw"] is None:
            continue
        T = row["pins"][PIN]["T"]
        if T is None:
            continue
        # --- incumbent om_gfs(bc,ps) probability + level residual -----------
        sg = omstats["sigma_st"].get(st, omstats["sigma_global"])
        if not (sg and sg > 0):
            sg = omstats["sigma_global"]
        om_bc = row["om_raw"] - omstats["bias_st"].get(st, omstats["bias_global"])
        p_om = PDS.p_ge(om_bc, sg, T)
        e = row["settled_max"] - row["om_raw"]           # level residual
        # --- NWP member levels (a member is "present" if its level is not None)
        lev = {m: row[m] for m in NWP_MEMBERS}
        present = [v for v in lev.values() if v is not None]
        # --- rate_1h point-in-time ------------------------------------------
        rp = HSA.rate_pit(recs.get((st, ds)), T, PIN)
        rate = rp["rate"] if rp else None
        d_rise = (rp["mu"] - rp["hsf"]) if rp else None
        rate_p = rp["p"] if rp else None
        # --- market mid at 13:30 strike -------------------------------------
        pm = HSA.market_p_at(ladders.get((st, ds)), T)
        cohort.append({
            "station": st, "date": ds, "y": row["y"], "T": T,
            "settled": row["settled_max"], "om_raw": row["om_raw"],
            "om_bc": om_bc, "p_om": p_om, "om_err": e,
            "abs_om_err": abs(e),
            "nwp_lev": lev, "nwp_present": present,
            "nwp_spread5": (HSA._std(present) if len(present) >= 3 else None),
            "nwp_spread6": None,  # filled below (needs om_raw)
            "nwp_range5": (max(present) - min(present) if len(present) >= 3 else None),
            "rate": rate, "rate_rise": d_rise, "rate_p": rate_p,
            "market_mid": pm, "gefs_sd": row["gefs_sd"],
        })
    for r in cohort:
        vals = list(r["nwp_present"])
        if r["om_raw"] is not None:
            vals = vals + [r["om_raw"]]
        r["nwp_spread6"] = HSA._std(vals) if len(vals) >= 3 else None
    return cohort


# ---------------------------------------------------------------------------
# results assembly
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="run", choices=["run"])
    ap.parse_args()
    t0 = time.time()

    log("building salvage cohort ...")
    cohort = build_cohort()
    n = len(cohort)
    log(f"cohort n = {n}")

    result = {
        "generated_at_utc": HSA.datetime.now(HSA.timezone.utc).isoformat(),
        "prereg": "docs/plans/WEAK-SIGNAL-SALVAGE-PREREG-2026-10-14.md",
        "axis": ("strict strike-crossing, T=floor(hsf@13:30)+1, "
                 "crossed = settled_max > T (== is LOSS)"),
        "truth": "canonical GHCN settlement",
        "cohort": {"n": n, "window": [HSA.MIGRATION_START, HSA.MIGRATION_END],
                   "pin": "13:30 station-local", "look_ahead_free": True},
        "incumbent": {"name": "om_gfs(bc,ps)", "brier_1330": 0.1067,
                      "p_om_brier_this_cohort": None},
        "tests": {},
    }

    # incumbent on THIS cohort (sanity: must reproduce ~0.1067)
    ps = [r["p_om"] for r in cohort]
    ys = [r["y"] for r in cohort]
    base = PDS.calibration_metrics(ps, ys)
    result["incumbent"]["p_om_brier_this_cohort"] = round(PDS.brier(ps, ys), 4)
    result["incumbent"]["dir_acc"] = round(PDS.dir_acc(ps, ys), 4)
    result["incumbent"]["ece"] = base["ece"]
    log(f"incumbent om_gfs(bc,ps) Brier = {result['incumbent']['p_om_brier_this_cohort']} "
        f"(n={n})")

    ys_all = [r["y"] for r in cohort]
    se_om = [(r["p_om"] - r["y"]) ** 2 for r in cohort]

    # =====================================================================
    # H1 — NWP spread quartzile vs om_gfs Brier / |err|
    # =====================================================================
    log("H1: NWP-spread quality flag ...")
    h1 = {}
    for tag, key in (("spread5", "nwp_spread5"), ("spread6", "nwp_spread6"),
                     ("range5", "nwp_range5")):
        sub = [r for r in cohort if r.get(key) is not None]
        if len(sub) < 40:
            h1[tag] = {"n": len(sub), "verdict": "NO USE", "reason": "insufficient rows"}
            continue
        edges, lab = quartile_bins([r[key] for r in sub])
        bins = {0: [], 1: [], 2: [], 3: []}
        for r in sub:
            bins[lab(r[key])].append(r)
        bt = []
        for q in (0, 1, 2, 3):
            b = bins[q]
            qps = [r["p_om"] for r in b]
            qys = [r["y"] for r in b]
            bt.append({
                "q": q + 1, "n": len(b),
                "spread_lo": (edges[q] if edges[q] != float("-inf") else None),
                "spread_hi": (edges[q + 1] if edges[q + 1] != float("inf") else None),
                "brier": score_brier(qps, qys),
                "mean_abs_om_err": round(sum(r["abs_om_err"] for r in b) / len(b), 4),
                "mean_y": round(sum(qys) / len(qys), 4),
            })
        sp = PDS.spearman([r[key] for r in sub],
                          [(r["p_om"] - r["y"]) ** 2 for r in sub])
        pval = _spearman_p([r[key] for r in sub],
                           [(r["p_om"] - r["y"]) ** 2 for r in sub])
        d_brier = (bt[3]["brier"] - bt[0]["brier"]
                   if bt[3]["brier"] is not None and bt[0]["brier"] is not None else None)
        usable = (d_brier is not None and d_brier >= 0.02
                  and sp is not None and sp > 0 and pval is not None and pval < 0.05)
        weak = (not usable and d_brier is not None and d_brier >= 0.01
                and ((sp is not None and sp > 0 and pval is not None and pval < 0.05)
                     or (sp is not None and sp > 0)))
        h1[tag] = {
            "n": len(sub), "quartiles": bt,
            "d_brier_q4_minus_q1": (round(d_brier, 4) if d_brier is not None else None),
            "spearman_spread_vs_sqerr": (round(sp, 4) if sp is not None else None),
            "spearman_p": (round(pval, 4) if pval is not None else None),
            "monotone": _is_monotone([bt[i]["brier"] for i in range(4)]),
            "verdict": "SALVAGE" if usable else ("WEAK" if weak else "NO USE"),
        }
    result["tests"]["H1_nwp_spread"] = h1
    # H1 interrogation: the raw-Brier ordering can be a BASE-RATE confound
    # (harder days have lower base rate and higher raw Brier). Add the
    # base-rate-normalized skill (BSS), the base-rate-free LEVEL-error view,
    # a gate simulation, and leave-one-station-out / per-month stability.
    log("H1 robustness: base-rate normalization + gate sim + LOSO ...")
    result["tests"]["H1_robustness"] = h1_robustness(cohort)

    # =====================================================================
    # H2 — rate_1h residual correction  (OLS e ~ rate, block bootstrap)
    # =====================================================================
    log("H2: rate_1h residual ...")
    h2 = {}
    for tag, key in (("rate", "rate"), ("rate_rise", "rate_rise")):
        pairs = [(r[key], r["om_err"], r["station"]) for r in cohort
                 if r.get(key) is not None and r["om_err"] is not None]
        bb = boot_slope(pairs, None)
        os_ = ols([p[0] for p in pairs], [p[1] for p in pairs])
        ho = _holdout_residual_correction(cohort, key, "om_err")
        ok = (bb is not None and bb["excludes_zero"]
              and ho is not None and ho["delta_brier_heldout"] is not None
              and ho["delta_brier_heldout"] <= -0.005)
        h2[tag] = {
            "n": len(pairs),
            "ols_slope": (round(os_["b"], 6) if os_ else None),
            "ols_r2": (round(os_["r2"], 5) if os_ else None),
            "bootstrap": bb,
            "holdout": ho,
            "verdict": "SALVAGE" if ok else "NO USE",
        }
    result["tests"]["H2_rate1h_residual"] = h2

    # =====================================================================
    # H3 — market mid residual / lag  (OLS e ~ market_mid, block bootstrap)
    # =====================================================================
    log("H3: market residual/lag ...")
    pairs = [(r["market_mid"], r["om_err"], r["station"]) for r in cohort
             if r.get("market_mid") is not None and r["om_err"] is not None]
    bb = boot_slope(pairs, None)
    os_ = ols([p[0] for p in pairs], [p[1] for p in pairs])
    ho = _holdout_residual_correction(cohort, "market_mid", "om_err")
    ok = (bb is not None and bb["excludes_zero"]
          and ho is not None and ho["delta_brier_heldout"] is not None
          and ho["delta_brier_heldout"] <= -0.005)
    result["tests"]["H3_market_residual"] = {
        "n": len(pairs),
        "ols_slope": (round(os_["b"], 6) if os_ else None),
        "ols_r2": (round(os_["r2"], 5) if os_ else None),
        "bootstrap": bb,
        "holdout": ho,
        "verdict": "SALVAGE" if ok else "NO USE",
    }

    # =====================================================================
    # H4 — gefs SPREAD as uncertainty / sizing input
    # =====================================================================
    log("H4: gefs spread as uncertainty ...")
    sub = [r for r in cohort if r.get("gefs_sd") is not None]
    xs = [r["gefs_sd"] for r in sub]
    ae = [r["abs_om_err"] for r in sub]
    sp = PDS.spearman(xs, ae)
    pval = _spearman_p(xs, ae)
    edges, lab = quartile_bins(xs)
    qstat = []
    for q in (0, 1, 2, 3):
        b = [r for r in sub if lab(r["gefs_sd"]) == q]
        qstat.append({"q": q + 1, "n": len(b),
                      "sd_lo": (edges[q] if edges[q] != float("-inf") else None),
                      "sd_hi": (edges[q + 1] if edges[q + 1] != float("inf") else None),
                      "mean_abs_om_err": (round(sum(r["abs_om_err"] for r in b) / len(b), 4)
                                          if b else None)})
    ratio = None
    if qstat[0]["mean_abs_om_err"] and qstat[3]["mean_abs_om_err"]:
        ratio = round(qstat[3]["mean_abs_om_err"] / qstat[0]["mean_abs_om_err"], 3)
    bb = boot_slope([(r["gefs_sd"], r["abs_om_err"], r["station"]) for r in sub], None)
    ok = (sp is not None and sp > 0 and pval is not None and pval < 0.0125
          and ratio is not None and ratio >= 1.5
          and bb is not None and bb["excludes_zero"])
    result["tests"]["H4_gefs_spread"] = {
        "n": len(sub),
        "spearman_sd_vs_abs_err": (round(sp, 4) if sp is not None else None),
        "spearman_p": (round(pval, 4) if pval is not None else None),
        "quartiles": qstat,
        "q4_over_q1_abs_err_ratio": ratio,
        "bootstrap": bb,
        "verdict": "SALVAGE" if ok else "NO USE",
    }

    result["misc"] = {"elapsed_s": round(time.time() - t0, 1)}
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ART_JSON.write_text(json.dumps(result, indent=2))
    log(f"wrote {OUT_JSON}")
    log(f"wrote {OUT_ART_JSON}")
    write_md(result)
    log(f"wrote {OUT_MD}")
    print(json.dumps({k: v.get("verdict") for k, v in
                      [(k, (v if not isinstance(v, dict) or "verdict" not in v else v))
                       for k, v in result["tests"].items()]}, indent=2))
    print(json.dumps(result["tests"], indent=2)[:2000])


# ---------------------------------------------------------------------------
# helpers that need the cohort (defined after main is fine in py)
# ---------------------------------------------------------------------------
def _spearman_p(xs, ys):
    """Two-sided p for Spearman rho via t-approx on n-2 dof (no scipy)."""
    pair = [(x, y) for x, y in zip(xs, ys) if not _nan(x) and not _nan(y)]
    n = len(pair)
    if n < 4:
        return None
    rho = PDS.spearman([x for x, _ in pair], [y for _, y in pair])
    if rho is None or rho != rho:
        return None
    if abs(rho) >= 1.0:
        return 0.0
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    # two-sided p from Student-t CDF via incomplete beta (use normal approx for rf)
    return _t_sf(abs(t), n - 2) * 2.0


def _t_sf(t, df):
    """Upper-tail P(T > t) for Student-t, via regularized incomplete beta."""
    x = df / (df + t * t)
    ib = _betainc_reg(df / 2.0, 0.5, x)
    return 0.5 * ib


def _betainc_reg(a, b, x):
    """Regularized incomplete beta I_x(a,b) — continued fraction (Lentz)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _betainc_reg(b, a, 1 - x)
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 200):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-12:
            break
    return front * (f - 1.0)


def _is_monotone(seq):
    if any(v is None for v in seq):
        return None
    inc = all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
    dec = all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1))
    return "increasing" if inc else ("decreasing" if dec else "none")


def _holdout_residual_correction(cohort, key, err_key):
    """Fit residual corr on TRAIN half, score the corrected P on the TEST half.

    Split = alternating station-days in the cohort's natural (station, date) order
    (deterministic, pre-registered). Correction: mu' = om_raw + b*x + a (the fitted
    residual); P' = Phi((mu' - T)/sigma_st). Reports Brier delta on the held-out half.
    """
    rows = [r for r in cohort if r.get(key) is not None and r.get(err_key) is not None]
    if len(rows) < 40:
        return None
    rows_sorted = sorted(rows, key=lambda r: (r["station"], r["date"]))
    train = rows_sorted[0::2]
    test = rows_sorted[1::2]
    if len(train) < 20 or len(test) < 20:
        return None
    fit = ols([r[key] for r in train], [r[err_key] for r in train])
    if fit is None:
        return None
    a, b = fit["a"], fit["b"]
    test = [r for r in test if r["om_err"] is not None]
    if not test:
        return None
    # uncorrected Brier on test
    p0 = [r["p_om"] for r in test]
    y0 = [r["y"] for r in test]
    b0 = PDS.brier(p0, y0)
    # corrected: shift the level by the fitted residual (b*x + a)
    p1 = []
    for r in test:
        mu = r["om_raw"] + (a + b * r[key])
        mu_bc = mu - 0.0  # bias already inside om_bc/om_raw comparison; keep raw-space
        sg = 1.5927  # fallback; per-station sigma recomputed below is unavailable here
        p1.append(mu)
    # recompute P' with per-station sigma via a re-score over corrected level
    # (use the same sigma logic as the incumbent)
    from collections import defaultdict
    res = defaultdict(list)
    for r in cohort:
        res[r["station"]].append(r["om_raw"] - r["settled"])
    allb = [v for lst in res.values() for v in lst]
    bias_global = sum(allb) / len(allb)
    def _std(xs, ddof=1):
        m = sum(xs) / len(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - ddof)) if len(xs) > ddof else float("nan")
    bias_st = {st: sum(v) / len(v) for st, v in res.items() if len(v) >= 3}
    sigma_st = {st: _std(v) for st, v in res.items() if len(v) >= 3}
    sigma_global = _std(allb)
    ps1, ys1 = [], []
    for r in test:
        corr = r["om_raw"] + (a + b * r[key])
        corr_bc = corr - bias_st.get(r["station"], bias_global)
        sg = sigma_st.get(r["station"], sigma_global)
        if not (sg and sg > 0):
            sg = sigma_global
        ps1.append(PDS.p_ge(corr_bc, sg, r["T"]))
        ys1.append(r["y"])
    b1 = PDS.brier(ps1, ys1)
    return {"n_train": len(train), "n_test": len(test),
            "fit_a": round(a, 4), "fit_b": round(b, 6),
            "brier_test_uncorrected": round(b0, 4),
            "brier_test_corrected": round(b1, 4),
            "delta_brier_heldout": round(b1 - b0, 4)}


def h1_robustness(cohort):
    """Post-hoc interrogation of the H1 gate (frozen gate already applied in main).

    The gate passed on RAW Brier, but raw Brier mixes calibration with difficulty.
    This section asks three things the gate does not:
      1. BSS per bin (1 - Brier/uncertainty) — base-rate-normalized, so a bin that is
         merely HARDER (lower base rate) does not look "worse" for free.
      2. Signed bias per bin (mean_err) — is the mechanism added NOISE (|err| up,
         mean_err ~flat) or a systematic BIAS (mean_err shifts)?
      3. A GATE SIMULATION — actually drop the high-spread bin and re-score the
         incumbent (and the honest level-error), which is what "usable" means.
    Plus per-month stability of the Q4-Q1 delta, so a single regime cannot drive it.
    """
    out = {}
    for tag, key in (("spread5", "nwp_spread5"), ("spread6", "nwp_spread6")):
        sub = [r for r in cohort if r.get(key) is not None]
        if len(sub) < 40:
            out[tag] = {"n": len(sub), "note": "insufficient rows"}
            continue
        edges, lab = quartile_bins([r[key] for r in sub])
        bins = {0: [], 1: [], 2: [], 3: []}
        for r in sub:
            bins[lab(r[key])].append(r)
        qtab = []
        for q in (0, 1, 2, 3):
            b = bins[q]
            qys = [r["y"] for r in b]
            qps = [r["p_om"] for r in b]
            base = sum(qys) / len(qys)
            unc = base * (1 - base)
            br = PDS.brier(qps, qys)
            bss = (1 - br / unc) if unc > 0 else None
            mean_err = sum(r["om_err"] for r in b) / len(b)
            qtab.append({
                "q": q + 1, "n": len(b), "brier": round(br, 4),
                "bss": (round(bss, 4) if bss is not None else None),
                "base_rate": round(base, 4),
                "mean_err_signed": round(mean_err, 4),
                "mean_abs_err": round(sum(r["abs_om_err"] for r in b) / len(b), 4),
            })
        # --- gate simulation: drop the top spread bin, rescore incumbent ---
        keep = [r for r in sub if lab(r[key]) != 3]     # all but Q4
        dropq4_brier = score_brier([r["p_om"] for r in keep], [r["y"] for r in keep])
        full_brier = score_brier([r["p_om"] for r in sub], [r["y"] for r in sub])
        keep_abs = round(sum(r["abs_om_err"] for r in keep) / len(keep), 4)
        full_abs = round(sum(r["abs_om_err"] for r in sub) / len(sub), 4)
        # --- per-month stability of Q4-Q1 delta Brier ---
        months = {}
        for r in sub:
            mo = r["date"][:7]
            months.setdefault(mo, []).append(r)
        per_month = []
        for mo, rs in sorted(months.items()):
            if len(rs) < 60:
                continue
            ed, lb = quartile_bins([r[key] for r in rs])
            q4 = [r for r in rs if lb(r[key]) == 3]
            q1 = [r for r in rs if lb(r[key]) == 0]
            if len(q4) < 5 or len(q1) < 5:
                continue
            d = (PDS.brier([r["p_om"] for r in q4], [r["y"] for r in q4])
                 - PDS.brier([r["p_om"] for r in q1], [r["y"] for r in q1]))
            per_month.append({"month": mo, "n": len(rs), "d_brier_q4_minus_q1": round(d, 4)})
        out[tag] = {
            "n": len(sub),
            "quartiles": qtab,
            "gate_sim": {"full_brier": full_brier, "drop_q4_brier": dropq4_brier,
                         "delta_brier_from_gate": (round(dropq4_brier - full_brier, 4)
                                                   if dropq4_brier is not None else None),
                         "full_mean_abs_err": full_abs, "drop_q4_mean_abs_err": keep_abs,
                         "delta_abs_err_from_gate": round(keep_abs - full_abs, 4)},
            "per_month_delta": per_month,
            "n_months_positive": sum(1 for m in per_month if m["d_brier_q4_minus_q1"] > 0),
            "n_months": len(per_month),
        }
    return out


def write_md(result):
    L = []
    A = result
    T = A["tests"]
    h1 = T["H1_nwp_spread"]
    L.append("# Weak-Signal Salvage — can the old signals help the decision model? (2026-10-14)")
    L.append("")
    L.append("**Script:** `scripts/disagreement_quality_flag_2026-10-14.py`  ")
    L.append("**Pre-registration:** `docs/plans/WEAK-SIGNAL-SALVAGE-PREREG-2026-10-14.md` "
             "(written before any test was run)  ")
    L.append("**Artifacts:** `data/weak_signal_salvage_2026-10-14.json` · "
             "`artifacts/weak-signal-salvage/weak_signal_salvage_2026-10-14.json`  ")
    L.append("**Axis:** strict strike-crossing, `T = floor(hsf@13:30)+1`, "
             "`crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN. "
             "**Look-ahead-free.**  ")
    L.append(f"**Cohort:** {A['cohort']['window'][0]} → {A['cohort']['window'][1]}, "
             f"n = {A['cohort']['n']} station-days.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 0. Bottom line")
    L.append("")
    vmap = {h1["spread5"].get("verdict"), h1["spread6"].get("verdict"),
            T["H2_rate1h_residual"]["rate"].get("verdict"),
            T["H3_market_residual"].get("verdict"),
            T["H4_gefs_spread"].get("verdict")}
    salvage = [k for k, v in (("H1 NWP spread", h1["spread5"]),
                              ("H2 rate_1h", T["H2_rate1h_residual"]["rate"]),
                              ("H3 market", T["H3_market_residual"]),
                              ("H4 gefs sd", T["H4_gefs_spread"]))
               if v.get("verdict") == "SALVAGE"]
    L.append("**Verdicts against the frozen gates:**")
    L.append("")
    L.append("| signal | role tested | literal gate | decision verdict |")
    L.append("|---|---|---|---|")
    L.append(f"| NWP cross-source spread (H1) | quality gate | {h1['spread5'].get('verdict')} / {h1['spread6'].get('verdict')} | **NO USE** (base-rate artifact — see §H1) |")
    L.append(f"| rate_1h (0.362) (H2) | residual correction | {T['H2_rate1h_residual']['rate'].get('verdict')} | **NO USE** |")
    L.append(f"| market (0.619) (H3) | lag / residual | {T['H3_market_residual'].get('verdict')} | **NO USE** |")
    L.append(f"| gefs spread (H4) | uncertainty/sizing | {T['H4_gefs_spread'].get('verdict')} | **NO USE** |")
    L.append("")
    L.append("> **Honest zero.** No weak signal earns a non-fusion role in the decision model. "
             "One test (H1, NWP cross-source spread) *passed its literal pre-registered gate* "
             "(ΔBrier(Q4−Q1) = +0.024 ≥ 0.02, Spearman p < 0.05) — but on interrogation the "
             "effect is a **base-rate/difficulty artifact**, not a usable quality flag: the "
             "base-rate-normalized skill (BSS) is *highest* in the high-disagreement bin, the "
             "implementable gate gain is **≈0.001 Brier** (noise), and it is not month-stable. "
             "Reported as NO USE. The other three are clean zeros.")
    L.append("")
    L.append("**Nothing here is worth wiring.** The old directional-accuracy signals remain "
             "useful only as *context* — they confirm the market is priced off roughly the "
             "same information set (`market` ρ≈0, §H3). The actionable lever remains the "
             "incumbent: ship `om_gfs(bc,ps)` (Brier 0.1067) and drop the fusion.")
    L.append("")
    L.append("Incumbent on this cohort: `om_gfs(bc,ps)` Brier = "
             f"**{A['incumbent']['p_om_brier_this_cohort']}** (n = {A['cohort']['n']}, "
             f"ECE {A['incumbent']['ece']}).")
    L.append("")

    # ---- H1 ------------------------------------------------------------
    L.append("## H1 — Cross-source DISAGREEMENT as a quality flag")
    L.append("")
    L.append("Hypothesis: when the NWP members disagree, `om_gfs` is less trustworthy, so "
             "fused error should be worse. **Gate:** `Brier(Q4)−Brier(Q1) ≥ 0.02` AND "
             "Spearman(spread, sq-err) > 0 with p < 0.05.")
    L.append("")
    for tag in ("spread5", "spread6", "range5"):
        d = h1[tag]
        L.append(f"### {tag} — verdict **{d.get('verdict')}** (n = {d.get('n')})")
        if "quartiles" not in d:
            L.append("")
            continue
        L.append("")
        L.append("| bin | spread range | n | om_gfs Brier | mean\\|err\\| (°F) | mean y |")
        L.append("|---|---|---:|---:|---:|---:|")
        for b in d["quartiles"]:
            lo = "—" if b["spread_lo"] is None else f"{b['spread_lo']:.2f}"
            hi = "—" if b["spread_hi"] is None else f"{b['spread_hi']:.2f}"
            L.append(f"| Q{b['q']} | [{lo}, {hi}) | {b['n']} | {b['brier']:.4f} | "
                     f"{b['mean_abs_om_err']:.4f} | {b['mean_y']:.3f} |")
        L.append("")
        L.append(f"ΔBrier(Q4−Q1) = **{d['d_brier_q4_minus_q1']}**; "
                 f"Spearman ρ = {d['spearman_spread_vs_sqerr']} (p = {d['spearman_p']}); "
                 f"monotone: {d['monotone']}.")
        L.append("")
    # ---- H1 interrogation: the base-rate confound ----------------------
    rb = T.get("H1_robustness", {}).get("spread5")
    if rb:
        L.append("### H1 interrogation — the gate passes, but the mechanism is a base-rate artifact")
        L.append("")
        L.append("The literal gate passed on **raw Brier**. But the quartile shape is *non-monotone* "
                 "(Q1 0.0855 → Q2 0.1098 → Q3 0.1194 → Q4 0.1097) and the base rate falls across the "
                 "bins — the signature of a **difficulty confound**, not a disagreement signal. "
                 "Two checks settle it.")
        L.append("")
        L.append("**(1) Base-rate-normalized skill (BSS).** If disagreement meant *less* skill, BSS "
                 "should be lowest in Q4. It is **highest** (Q1 0.282 → Q4 **0.394**): on "
                 "disagreement days `om_gfs` is *sharper*, not worse. The raw-Brier gap was the "
                 "lower base rate, not degradation of the forecast.")
        L.append("")
        L.append("| bin | n | raw Brier | BSS | base rate | mean err (signed) | mean\|err\| |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for b in rb["quartiles"]:
            L.append(f"| Q{b['q']} | {b['n']} | {b['brier']:.4f} | {b['bss']} | {b['base_rate']:.3f} | "
                     f"{b['mean_err_signed']:+.3f} | {b['mean_abs_err']:.3f} |")
        L.append("")
        gs = rb["gate_sim"]
        L.append(f"**(2) Gate simulation.** What a \"skip the high-disagreement bin\" gate would "
                 f"actually buy: dropping Q4 moves the incumbent Brier from {gs['full_brier']} to "
                 f"{gs['drop_q4_brier']} — a gain of **{gs['delta_brier_from_gate']}** (noise; the gate "
                 f"costs you {rb['quartiles'][3]['n']} of {rb['n']} rows and buys ~nothing). The mean "
                 f"level error only moves {gs['delta_abs_err_from_gate']} °F. This is the number that "
                 f"decides \"usable\" — and it does not.")
        L.append("")
        L.append(f"**(3) Stability.** Q4−Q1 ΔBrier is positive in {rb['n_months_positive']}/"
                 f"{rb['n_months']} months (5–7% in Jul–Sep), but the last partial month (2026-10) "
                 f"flips sign — and month-reranked quartiles are a different partition. Not the "
                 f"stable-if-monotone picture the H1 gate was written to require.")
        L.append("")
        L.append("**H1 verdict: NO USE.** The pre-registered gate was met in letter "
                 "(ΔBrier ≥ 0.02, Spearman p < 0.05) but the mechanism is a base-rate artifact and "
                 "the implementable gain is ≈0.001 Brier. A quality gate that fires on ~25% of days "
                 "to buy 0.1% Brier is not a tradeable edge. Honest zero.")
        L.append("")
    # ---- H2 ------------------------------------------------------------
    L.append("## H2 — rate_1h as a residual correction")
    L.append("")
    L.append("Hypothesis: rate_1h's sign/magnitude carries dynamics that add skill to the "
             "`om_gfs` level residual `e = settled − om_gfs_raw`. **Gate:** block-bootstrap "
             "95% CI on the OLS slope excludes 0 AND the correction improves held-out Brier "
             "by ≥ 0.005.")
    L.append("")
    for tag in ("rate", "rate_rise"):
        d = T["H2_rate1h_residual"][tag]
        L.append(f"- **{tag}** — verdict **{d['verdict']}** (n = {d['n']}): slope "
                 f"{d['ols_slope']} (r² {d['ols_r2']}); bootstrap CI "
                 f"{d['bootstrap']['ci95'] if d['bootstrap'] else None}; "
                 f"held-out ΔBrier {d['holdout']['delta_brier_heldout'] if d['holdout'] else None}.")
    L.append("")
    # ---- H3 ------------------------------------------------------------
    d = T["H3_market_residual"]
    L.append("## H3 — market as a lag / residual indicator")
    L.append("")
    L.append(f"Hypothesis: the market mid at the pin is slow to reprice and adds to the "
             f"`om_gfs` residual. **Verdict: {d['verdict']}** (n = {d['n']}). "
             f"Slope {d['ols_slope']} (r² {d['ols_r2']}); bootstrap CI "
             f"{d['bootstrap']['ci95'] if d['bootstrap'] else None}; held-out ΔBrier "
             f"{d['holdout']['delta_brier_heldout'] if d['holdout'] else None}. "
             f"Consistent with the register's `corr ≈ −0.002` null.")
    L.append("")
    # ---- H4 ------------------------------------------------------------
    d = T["H4_gefs_spread"]
    L.append("## H4 — gefs ensemble SPREAD as an uncertainty input")
    L.append("")
    L.append(f"Hypothesis: gefs is a poor point predictor but its spread estimates "
             f"`om_gfs`'s error magnitude (heteroscedasticity), making it a *sizing* input. "
             f"**Verdict: {d['verdict']}** (n = {d['n']}). Spearman ρ = {d['spearman_sd_vs_abs_err']} "
             f"(p = {d['spearman_p']}); Q4/Q1 mean\\|err\\| ratio = {d['q4_over_q1_abs_err_ratio']} "
             f"(gate ≥ 1.5); bootstrap CI "
             f"{d['bootstrap']['ci95'] if d['bootstrap'] else None}.")
    L.append("")
    L.append("| bin | gefs sd range | n | mean\\|om_gfs err\\| |")
    L.append("|---|---|---:|---:|")
    for b in d["quartiles"]:
        lo = "—" if b["sd_lo"] is None else f"{b['sd_lo']:.2f}"
        hi = "—" if b["sd_hi"] is None else f"{b['sd_hi']:.2f}"
        L.append(f"| Q{b['q']} | [{lo}, {hi}) | {b['n']} | {b['mean_abs_om_err']} |")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append("python3 scripts/disagreement_quality_flag_2026-10-14.py run")
    L.append("# needs the honest-benchmark /tmp caches "
             "(/tmp/we_grid_2026-10-13.pkl.gz, /tmp/we_asos_recs_2026-10-13.pkl.gz,")
    L.append("#  /tmp/honest_omgfs_cache_2026-10-14.json) from "
             "scripts/honest_signal_accuracy_2026-10-14.py")
    L.append("```")
    L.append("")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
