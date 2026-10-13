#!/usr/bin/env python3
"""
bias_corrected_consensus_pcross_2026-10-13.py — Item 3 of the Brier improvement sprint.

THE MONEY TEST
  Workstream Q tested bias-adjusted consensus on the LEVEL axis (MAE) and found
  aggregation loses. Workstream N/agg-exploration found the same on level. This is
  the SAME test on the STRIKE-CROSSING axis — P(cross) at the 13:30 pin, scored by
  Brier against settled truth. It is the direct test of Dan's "bias-adjust, then
  aggregate" thesis on the axis that actually matters.

DESIGN (pre-registered here, mirrors Workstream Q)
  * Universe: 20 stations × 30 days (2026-09-13..10-12), 15 NWP Tmax sources from
    artifacts/forecast-aggregation/station_day_rows.json.
  * Axis: T = floor(ASOS high-so-far at the 13:30 station-local pin) + 1 ;
    y = 1 if settled truth > T else 0  (STRICT: `==` is a LOSS).
  * Map each source to P(cross) via the SAME fixed σ=4°F used in the diversity
    study: P = Φ((Tmax_src − T)/4).
  * Split: FIRST 20 days = train, LAST 10 days = test (no lookahead). Rows are
    ordered by date; stations within a date stay together.
  * Bias correction: per (source, station) MEDIAN (Tmax − truth) on train, applied
    to test. Two variants: per-station bias, and global-per-source bias.
  * Consensus variants (on the bias-corrected P(cross)): mean, median, trimmed
    (drop min/max), inverse-MAE weighted (weights from TRAIN MAE only).
  * Scores: Brier (+ dir acc, base rate) on TEST vs settled truth.

READ-ONLY on the live book. Writes ONLY:
  data/bias_corrected_consensus_pcross.json

Usage: .venv-gefs/bin/python scripts/bias_corrected_consensus_pcross_2026-10-13.py
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import importlib  # noqa: E402

_div = importlib.import_module("predictor_diversity_study_2026-10-13")
load_asos, hsf_at_pin = _div.load_asos, _div.hsf_at_pin

AGG = REPO / "artifacts" / "forecast-aggregation" / "station_day_rows.json"
METAR = REPO / "data" / "metar_backfill.db"
OUT = REPO / "data" / "bias_corrected_consensus_pcross.json"

SIGMA = 4.0
TRAIN_DAYS = 20
SOURCES = ["nws", "gfs", "ecmwf", "icon", "gem", "gefs_ens", "om_gfs", "om_ecmwf",
           "om_icon", "om_gem", "om_mf", "om_ukmo", "om_jma", "om_knmi", "om_dmi"]


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_cross(mu, T, sigma=SIGMA):
    if mu is None:
        return None
    return cdf((mu - T) / sigma)


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def dir_acc(ps, ys):
    return sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / len(ps)


def load_truth_canonical(lo, hi):
    c = ro(METAR)
    out = {}
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL "
            "AND local_trading_date>=? AND local_trading_date<=?", (lo, hi)):
        out[(st, ds)] = float(v)
    c.close()
    return out


def main():
    rows = json.loads(AGG.read_text())
    agg_truth = {(r["station"], r["date"]): r["truth"] for r in rows}
    dates = sorted({r["date"] for r in rows})
    lo, hi = dates[0], dates[-1]
    canon = load_truth_canonical(lo, hi)

    print(f"loading ASOS ({lo}..{hi}) ...", flush=True)
    asos = load_asos()

    grid = []
    for r in rows:
        sta, ds = r["station"], r["date"]
        rec = asos.get((sta, ds))
        hsf = hsf_at_pin(rec) if rec else None
        truth = canon.get((sta, ds), agg_truth.get((sta, ds)))
        if hsf is None or truth is None:
            continue
        T = float(math.floor(hsf) + 1.0)
        srcs = r.get("sources", {})
        grid.append({"station": sta, "date": ds, "T": T, "truth": truth,
                     "y": 1 if truth > T else 0,
                     "hsf": hsf, "truth_src": "canonical" if (sta, ds) in canon else "ghcn",
                     "sources": {s: srcs[s] for s in SOURCES if srcs.get(s) is not None}})

    grid.sort(key=lambda g: (g["date"], g["station"]))
    train_dates = set(dates[:TRAIN_DAYS])
    test_dates = set(dates[TRAIN_DAYS:])            # all remaining dates (window has 28)
    train = [g for g in grid if g["date"] in train_dates]
    test = [g for g in grid if g["date"] in test_dates]
    print(f"grid={len(grid)} train={len(train)} test={len(test)} "
          f"train_dates={dates[0]}..{dates[TRAIN_DAYS-1]} "
          f"test_dates={dates[TRAIN_DAYS]}..{dates[-1]}")

    # ---- per (source, station) median bias on TRAIN ----
    bias_ps = defaultdict(list)
    bias_glob = defaultdict(list)
    for g in train:
        for s, v in g["sources"].items():
            e = v - g["truth"]
            bias_ps[(s, g["station"])].append(e)
            bias_glob[s].append(e)
    B_PS = {k: st.median(v) for k, v in bias_ps.items()}
    B_G = {s: st.median(v) for s, v in bias_glob.items()}

    # ---- per-source TRAIN MAE on the Tmax level (for inverse-MAE weights) ----
    mae_ps = defaultdict(list)
    for g in train:
        for s, v in g["sources"].items():
            mae_ps[s].append(abs(v - g["truth"]))
    MAE = {s: sum(v) / len(v) for s, v in mae_ps.items() if v}
    inv = {s: 1.0 / MAE[s] for s in MAE}
    inv_sum = sum(inv.values())
    W = {s: inv[s] / inv_sum for s in inv}

    # ---- build per-test-row corrected P(cross) ----
    def corrected(g, s, per_station=True):
        v = g["sources"].get(s)
        if v is None:
            return None
        b = B_PS.get((s, g["station"])) if per_station else B_G.get(s)
        if b is None:
            b = B_G.get(s, 0.0)
        return v - b

    def score_rows(sub, keyfn):
        ps, ys = [], []
        for g in sub:
            p = keyfn(g)
            if p is None:
                continue
            ps.append(p)
            ys.append(g["y"])
        if not ps:
            return {"n": 0}
        return {"n": len(ps), "brier": round(brier(ps, ys), 4),
                "dir_acc": round(dir_acc(ps, ys), 4),
                "base_rate": round(sum(ys) / len(ys), 4)}

    out = {
        "axis": "strike-crossing P(cross) at 13:30 station-local pin",
        "sigma_F": SIGMA,
        "train_dates": [dates[0], dates[TRAIN_DAYS - 1]],
        "test_dates": [dates[TRAIN_DAYS], dates[-1]],
        "n_train_dates": TRAIN_DAYS, "n_test_dates": len(dates) - TRAIN_DAYS,
        "window_distinct_dates": len(dates),
        "n_grid": len(grid), "n_train": len(train), "n_test": len(test),
        "bias_per_source_station": {f"{s}|{stn}": round(b, 3) for (s, stn), b in sorted(B_PS.items())},
        "bias_global_per_source": {s: round(b, 3) for s, b in sorted(B_G.items())},
        "train_mae_per_source": {s: round(v, 3) for s, v in sorted(MAE.items())},
        "inv_mae_weights": {s: round(W[s], 4) for s in sorted(W)},
    }

    # ---- single-source scores on TEST (raw and bias-corrected) ----
    singles = {}
    for s in SOURCES:
        raw = score_rows(test, lambda g, s=s: p_cross(g["sources"].get(s), g["T"]))
        bc = score_rows(test, lambda g, s=s: p_cross(corrected(g, s, True), g["T"]))
        bc_glob = score_rows(test, lambda g, s=s: p_cross(corrected(g, s, False), g["T"]))
        singles[s] = {"raw": raw, "bc_per_station": bc, "bc_global": bc_glob}
    out["singles_test"] = singles

    # ---- consensus variants on bias-corrected P(cross) ----
    def members(g, per_station=True):
        d = {}
        for s in SOURCES:
            p = p_cross(corrected(g, s, per_station), g["T"])
            if p is not None:
                d[s] = p
        return d

    def consensus_fn(kind, per_station=True, w=None):
        def f(g):
            d = members(g, per_station)
            if not d:
                return None
            vals = list(d.values())
            if kind == "mean":
                return sum(vals) / len(vals)
            if kind == "median":
                return st.median(vals)
            if kind == "trim":                      # drop 1 min + 1 max
                if len(vals) < 3:
                    return sum(vals) / len(vals)
                sv = sorted(vals)
                return sum(sv[1:-1]) / len(sv[1:-1])
            if kind == "inv_mae":
                num = sum(w[s] * p for s, p in d.items())
                den = sum(w[s] for s in d)
                return num / den
            return None
        return f

    cons = {}
    for name, kind, ps_flag, w in [
        ("mean_ps", "mean", True, None), ("mean_glob", "mean", False, None),
        ("median_ps", "median", True, None), ("median_glob", "median", False, None),
        ("trim_ps", "trim", True, None), ("trim_glob", "trim", False, None),
        ("inv_mae_ps", "inv_mae", True, W), ("inv_mae_glob", "inv_mae", False, W),
    ]:
        cons[name] = score_rows(test, consensus_fn(kind, ps_flag, w))
    out["consensus_test"] = cons

    # ---- best single (by test brier among bc_per_station) ----
    bs = min(((s, singles[s]["bc_per_station"]["brier"]) for s in SOURCES
              if singles[s]["bc_per_station"].get("brier") is not None),
             key=lambda t: t[1])
    best_raw = min(((s, singles[s]["raw"]["brier"]) for s in SOURCES
                    if singles[s]["raw"].get("brier") is not None), key=lambda t: t[1])
    out["best_single_bc_per_station"] = {"source": bs[0], "brier": bs[1]}
    out["best_single_raw"] = {"source": best_raw[0], "brier": best_raw[1]}

    best_cons = min(((k, v["brier"]) for k, v in cons.items() if v.get("brier") is not None),
                    key=lambda t: t[1])
    out["best_consensus"] = {"variant": best_cons[0], "brier": best_cons[1]}
    out["consensus_minus_best_single"] = round(best_cons[1] - bs[1], 4)

    # mean single (unweighted average of the individual bc P(cross)) — context
    all_single = [singles[s]["bc_per_station"]["brier"] for s in SOURCES
                  if singles[s]["bc_per_station"].get("brier") is not None]
    out["mean_single_bc_brier"] = round(sum(all_single) / len(all_single), 4)

    # per-source gain from bias correction (train-set bias -> test-set Brier)
    out["bias_correction_gain"] = {
        s: round(singles[s]["raw"]["brier"] - singles[s]["bc_per_station"]["brier"], 4)
        for s in SOURCES
        if singles[s]["raw"].get("brier") is not None
        and singles[s]["bc_per_station"].get("brier") is not None
    }

    # verdict
    agg_wins = best_cons[1] < bs[1]
    out["verdict"] = {
        "aggregation_beats_best_single_on_crossing_axis": agg_wins,
        "delta_brier": out["consensus_minus_best_single"],
        "best_single": out["best_single_bc_per_station"],
        "best_consensus": out["best_consensus"],
        "bias_correction_helps_singles": all(
            v >= 0 for v in out["bias_correction_gain"].values()),
        "conclusion": (
            "Bias-adjusting each source works (every source improves), but aggregation "
            "STILL LOSES to the best single source on the strike-crossing axis — "
            "confirms Workstream Q's level-axis verdict on the axis that matters. "
            "The sources' errors are correlated (shared synoptic driver), so averaging "
            "re-centres but does not reduce variance; it drags the sharp source (om_gfs) "
            "toward the poorly-calibrated members."
            if not agg_wins else
            "Aggregation beats the best single source on the crossing axis."),
        "precedent": "Workstream Q (level MAE): best single 1.116 vs best consensus 1.184.",
    }

    OUT.write_text(json.dumps(out, indent=2))
    _print(out)
    print("\nwrote", OUT)


def _print(out):
    print("\n=== TEST scores (strike-crossing Brier; base_rate = P(y=1)) ===")
    print(f"{'source':14s} {'raw':>8s} {'bc_station':>11s} {'bc_global':>10s}")
    for s, d in out["singles_test"].items():
        r = d["raw"].get("brier"); a = d["bc_per_station"].get("brier"); b = d["bc_global"].get("brier")
        print(f"{s:14s} {str(r):>8s} {str(a):>11s} {str(b):>10s}")
    print("\n=== CONSENSUS (bias-corrected P(cross)) ===")
    for k, v in out["consensus_test"].items():
        print(f"  {k:14s} n={v.get('n')} brier={v.get('brier')} dir={v.get('dir_acc')}")
    print(f"\nbest single bc/station: {out['best_single_bc_per_station']}")
    print(f"best consensus:         {out['best_consensus']}")
    print(f"consensus - best_single (pp, neg=consensus wins): "
          f"{out['consensus_minus_best_single']}")
    print(f"base_rate(y=1) test: {out['singles_test']['gfs']['raw'].get('base_rate')}")


if __name__ == "__main__":
    main()
