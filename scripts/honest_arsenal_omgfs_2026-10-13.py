#!/usr/bin/env python3
"""honest_arsenal_omgfs_2026-10-13.py — Stage 2 of the honest arsenal
re-evaluation: re-run the om_gfs strike-crossing validation under the HONEST
point-in-time rate_1h baseline (single point at the 13:30 pin, no future mod).

The published om_gfs test (scripts/om_gfs_strike_crossing_test_2026-10-13.py)
compared om_gfs(bias-corrected) against the LOOK-AHEAD rate_1h baseline
(Brier 0.0921 / 0.1241 window-max). This script re-runs the same head-to-head,
fcerr, blend and level-substitution tests with rate_1h re-derived honestly.

READ-ONLY. Writes ONLY artifacts/honest-arsenal/omgfs_stage2_2026-10-13.json.
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

spec = ilu.spec_from_file_location(
    "pds", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = ilu.module_from_spec(spec)
spec.loader.exec_module(PDS)

GRID_PKL = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_CACHE = REPO / "data" / "_omgfs_cache.json"
OUT = REPO / "artifacts" / "honest-arsenal" / "omgfs_stage2_2026-10-13.json"

PIN_MOD = 810
COLLINEAR_FLAG = 0.85


def load(fn):
    with gzip.open(fn, "rb") as f:
        return pickle.load(f)


def c2f(c):
    return c * 9.0 / 5.0 + 32.0


def mean(xs):
    return sum(xs) / len(xs)


def std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def rate_pit(rec, strike):
    """Honest single-point rate_1h at the pin (identical to Stage 1)."""
    b = rec["buckets"] if rec else None
    if not b or PIN_MOD not in b:
        return None
    vals = [b[x] for x in b if x <= PIN_MOD]
    if not vals:
        return None
    hsf = max(vals)
    if hsf <= 0:
        return None
    rate, _ = PDS._rate_and_roll(b, PIN_MOD)
    if rate is None:
        return None
    r3 = [PIN_MOD - 30, PIN_MOD - 15, PIN_MOD]
    roll_pt = (sum(b[x] for x in r3) / 3.0) if all(x in b for x in r3) else None
    d = PDS.deflate(rate, roll_pt)
    if d is None:
        return None
    return {"mu": hsf + d, "p": PDS.p_ge(hsf + d, PDS.SIGMA_RATE, strike)}


def score(name, ps, ys):
    d = PDS.calibration_metrics(ps, ys)
    return {"name": name, "n": len(ps), "brier": round(PDS.brier(ps, ys), 4),
            "dir_acc": round(PDS.dir_acc(ps, ys), 4),
            "base_rate": round(sum(ys) / len(ys), 4),
            "mean_p": round(sum(ps) / len(ps), 4),
            "reliability": d["reliability"], "resolution": d["resolution"],
            "ece": d["ece"], "mce": d["mce"]}


def main():
    grid = load(GRID_PKL)
    recs = load(RECS_PKL)
    om = {(k.split("|")[0], k.split("|")[1]): v
          for k, v in json.loads(OM_CACHE.read_text()).items()}

    # rebuild an honest row set: strike from hsf@pin, honest rate, om_gfs, truth
    rows = []
    for (st, ds), row in grid.items():
        rec = recs.get((st, ds))
        # strike is guaranteed present on the grid; recompute hsf from rec
        rp = rate_pit(rec, row["strike"])
        rn_old = PDS.rate_nowcast(rec, row["strike"])
        rows.append({
            "station": st, "date": ds, "strike": row["strike"],
            "settled_max": row["settled_max"], "y": row["y"],
            "om_gfs_raw": om.get((st, ds)),
            "rate_1h_pit": rp["p"] if rp else None,
            "rate_1h_pit_mu": rp["mu"] if rp else None,
            "rate_1h_old_p": row.get("rate_1h_p") if row.get("rate_1h_p") is not None
                             else (rn_old["p"] if rn_old else None),
            "rate_1h_old_mu": row.get("rate_1h") if row.get("rate_1h") is not None
                              else (rn_old["mu"] if rn_old else None),
            "gfs_raw": row.get("gfs"),
        })

    # ---- per-station bias + sigma for om_gfs (same recipe as the test) -----
    res = defaultdict(list)
    for r in rows:
        if r["om_gfs_raw"] is not None and r["settled_max"] is not None:
            res[r["station"]].append(r["om_gfs_raw"] - r["settled_max"])
    global_bias = mean([v for lst in res.values() for v in lst])
    global_sigma = std([v for lst in res.values() for v in lst])
    bias_st = {st: mean(v) for st, v in res.items() if len(v) >= 3}
    sigma_st = {st: std(v) for st, v in res.items() if len(v) >= 3}

    for r in rows:
        st = r["station"]
        omr = r["om_gfs_raw"]
        if omr is None or r["y"] is None:
            r["om_gfs_p_raw"] = r["om_gfs_p_bc"] = r["om_gfs_p_bc_ps"] = None
            continue
        sg = sigma_st.get(st, global_sigma)
        if not (sg > 0):
            sg = global_sigma
        r["om_gfs_p_raw"] = PDS.p_ge(omr, sg, r["strike"])
        r["om_gfs_p_bc"] = PDS.p_ge(omr - global_bias, sg, r["strike"])
        bs = bias_st.get(st, global_bias)
        r["om_gfs_p_bc_ps"] = PDS.p_ge(omr - bs, sg, r["strike"])

    # ---- common rows: om_gfs(bc) + honest rate_1h + truth ------------------
    crows = [r for r in rows
             if r["om_gfs_p_bc"] is not None and r["rate_1h_pit"] is not None
             and r["y"] is not None]
    ys = [r["y"] for r in crows]
    scores = {
        "rate_1h_pit_honest": score("rate_1h_pit", [r["rate_1h_pit"] for r in crows], ys),
        "om_gfs_raw": score("om_gfs_raw", [r["om_gfs_p_raw"] for r in crows], ys),
        "om_gfs_bc": score("om_gfs_bc", [r["om_gfs_p_bc"] for r in crows], ys),
        "om_gfs_bc_per_station_bias": score(
            "om_gfs_bc_ps", [r["om_gfs_p_bc_ps"] for r in crows], ys),
    }
    # old (look-ahead) rate_1h baseline on the SAME common rows for reference
    orows = [r for r in crows if r["rate_1h_old_p"] is not None]
    scores["rate_1h_windowmax_REFERENCE"] = score(
        "rate_1h_windowmax", [r["rate_1h_old_p"] for r in orows],
        [r["y"] for r in orows])

    # ---- independence: fcerr honest rate mu vs om_gfs level ----------------
    byst = defaultdict(list)
    for r in rows:
        if r["rate_1h_pit_mu"] is not None and r["om_gfs_raw"] is not None \
                and r["settled_max"] is not None:
            byst[r["station"]].append(
                (r["rate_1h_pit_mu"] - r["settled_max"],
                 r["om_gfs_raw"] - r["settled_max"]))
    lv_err = [pair for lst in byst.values() for pair in lst]
    fcerr_pooled = round(PDS.pearson([a for a, _ in lv_err],
                                     [b for _, b in lv_err]), 4) if len(lv_err) > 2 else None
    # within-station demeaned
    da, db = [], []
    for st, lst in byst.items():
        ma = mean([a for a, _ in lst]); mb = mean([b for _, b in lst])
        for a, b in lst:
            da.append(a - ma); db.append(b - mb)
    fcerr_within = round(PDS.pearson(da, db), 4) if len(da) > 2 else None
    pcross_corr = round(PDS.pearson([r["rate_1h_pit"] for r in crows],
                                    [r["om_gfs_p_bc"] for r in crows]), 4)
    fcerr_max = max([v for v in (fcerr_pooled, fcerr_within, pcross_corr)
                     if v is not None], key=abs) if crows else None

    b_om = scores["om_gfs_bc"]["brier"]
    b_rate = scores["rate_1h_pit_honest"]["brier"]
    crit_a = b_om <= b_rate
    crit_b = fcerr_max is not None and abs(fcerr_max) < COLLINEAR_FLAG
    if crit_a and crit_b:
        verdict = "WIRE_AS_SECOND_MEMBER"
    elif not crit_a and not crit_b:
        verdict = "DO_NOT_WIRE"
    elif not crit_a:
        verdict = "DO_NOT_WIRE__worse_brier"
    else:  # better brier but collinear
        verdict = "REPLACE_LOCAL_GFS_ONLY_NO_DIVERSITY_GAIN"

    # ---- level MAE (unchanged axis) ----------------------------------------
    def mae(pairs):
        v = [abs(a - b) for a, b in pairs if a is not None and b is not None]
        return round(mean(v), 4) if v else None
    level_mae = {
        "om_gfs_raw": mae([(r["om_gfs_raw"], r["settled_max"]) for r in rows]),
        "om_gfs_bc_global": mae([(r["om_gfs_raw"] - global_bias, r["settled_max"])
                                 for r in rows if r["om_gfs_raw"] is not None]),
        "gfs_raw": mae([(r["gfs_raw"], r["settled_max"]) for r in rows]),
        "rate_1h_pit_mu_honest": mae([(r["rate_1h_pit_mu"], r["settled_max"]) for r in rows]),
    }

    # ---- honest blend test --------------------------------------------------
    blend = {"common_n": len(crows), "rate_1h_pit_alone": b_rate,
             "om_gfs_bc_alone": b_om, "grid": {}}
    best = None
    for w in (0.0, 0.1, 0.2, 0.25, 0.3, 0.5, 0.7, 1.0):
        ps = [(1 - w) * r["rate_1h_pit"] + w * r["om_gfs_p_bc"] for r in crows]
        b = round(PDS.brier(ps, ys), 4)
        blend["grid"][f"w_om={w}"] = {"brier": b,
                                      "dir_acc": round(PDS.dir_acc(ps, ys), 4)}
        if best is None or b < best[1]:
            best = (w, b)
    blend["best_weight_on_om"] = best[0]
    blend["best_blend_brier"] = best[1]
    blend["best_blend_minus_honest_rate"] = round(best[1] - b_rate, 4)

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "om_gfs re-validation under honest point-in-time rate_1h baseline",
        "pin_mod_local": PIN_MOD,
        "common_rows": len(crows),
        "global_bias_F": round(global_bias, 4),
        "global_sigma_F": round(global_sigma, 4),
        "scores": scores,
        "fcerr": {"level_pooled": fcerr_pooled,
                  "level_within_station_demeaned": fcerr_within,
                  "pcross_pooled": pcross_corr,
                  "max_abs": fcerr_max,
                  "flag_threshold": COLLINEAR_FLAG,
                  "collinear": (abs(fcerr_max) >= COLLINEAR_FLAG) if fcerr_max is not None else None},
        "level_mae": level_mae,
        "blend_test": blend,
        "decision": {
            "criterion_a_brier_le_honest_rate": crit_a,
            "criterion_b_fcerr_lt_0.85": crit_b,
            "om_gfs_bc_brier": b_om,
            "honest_rate_1h_brier": b_rate,
            "published_rate_1h_brier_reference": 0.0921,
            "verdict": verdict,
            "verdicts": {
                "WIRE_AS_SECOND_MEMBER": "Brier <= honest rate_1h AND fcerr < 0.85 -> wire",
                "REPLACE_LOCAL_GFS_ONLY_NO_DIVERSITY_GAIN": "better/equal Brier but collinear",
                "DO_NOT_WIRE": "worse Brier -> do not wire",
            },
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))

    print("=" * 92)
    print("STAGE 2 — om_gfs re-validation under HONEST point-in-time rate_1h")
    print("=" * 92)
    print(f"common rows: {len(crows)}  global bias={global_bias:.4f}F sigma={global_sigma:.4f}F")
    print(f"{'source':30} {'n':>6} {'Brier':>8} {'dir_acc':>8} {'ECE':>7}")
    for k in ("rate_1h_pit_honest", "rate_1h_windowmax_REFERENCE", "om_gfs_raw",
              "om_gfs_bc", "om_gfs_bc_per_station_bias"):
        r = scores[k]
        print(f"{r['name']:30} {r['n']:>6} {r['brier']:>8.4f} "
              f"{r['dir_acc']:>8.4f} {r['ece']:>7.4f}")
    print(f"\nfcerr honest rate vs om_gfs: pooled={fcerr_pooled} within={fcerr_within} "
          f"pcross={pcross_corr} max|r|={fcerr_max} collinear={out['fcerr']['collinear']}")
    print(f"level MAE: {level_mae}")
    print(f"blend: best w_om={best[0]} brier={best[1]} (honest rate alone {b_rate})")
    print(f"\ncriterion (a) Brier(om_gfs bc)<=honest rate: {crit_a} ({b_om} vs {b_rate})")
    print(f"criterion (b) fcerr<0.85: {crit_b}")
    print(f"VERDICT: {verdict}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
