#!/usr/bin/env python3
"""honest_arsenal_rescore_2026-10-13.py — Stage 1 of the honest arsenal
re-evaluation.

The published predictor-diversity study scored `rate_1h` as the WINDOW-MAX
P(cross) over 13:00..16:45 (`RATE_MAX_MOD = 16:45`), i.e. it reads buckets
AFTER the 13:30 station-local decision pin. That is look-ahead. This script
re-derives rate_1h as a SINGLE POINT at the pin (local mod == 810, no mod > 810)
and re-scores EVERY source on the identical strike-crossing axis
(T = floor(hsf@13:30)+1, strict label settled_max > T, canonical truth) over the
same cohort window 2026-04-21..2026-10-09.

READ-ONLY. Writes ONLY artifacts/honest-arsenal/rescore_stage1_2026-10-13.json.
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

spec = ilu.spec_from_file_location(
    "pds", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = ilu.module_from_spec(spec)
spec.loader.exec_module(PDS)

GRID_PKL = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OUT = REPO / "artifacts" / "honest-arsenal" / "rescore_stage1_2026-10-13.json"

PIN_MOD = 810  # 13:30 station-local, minutes-of-day
SOURCES = ["ecmwf", "gefs", "gfs", "icon", "gem", "rate_1h", "rate_1h_pit", "market"]


def load(fn):
    with gzip.open(fn, "rb") as f:
        return pickle.load(f)


def rate_pit(rec, strike):
    """rate_1h nowcast as a SINGLE POINT at the 13:30 pin (mod == 810 only).

    hsf = max bucket mean with mod <= 810; rate/roll use ONLY buckets with
    mod <= 810 (mod+15 = 825 would be a future bucket, so roll is the
    3-bucket centred form truncated at the pin: we use _rate_and_roll(mod=810)
    which reads need_roll=[780,795,810,825] -> 825 is future. To stay strictly
    honest we null the roll when 825 is absent and fall back to the undeflated
    rate; a sensitivity variant that drops the roll entirely is also reported.
    """
    b = rec["buckets"] if rec else None
    if not b:
        return None
    if PIN_MOD not in b:
        return None
    vals = [b[x] for x in b if x <= PIN_MOD]
    if not vals:
        return None
    hsf = max(vals)
    if hsf <= 0:
        return None
    rate, roll = PDS._rate_and_roll(b, PIN_MOD)
    if rate is None:
        return None
    # honest roll: only buckets <= pin
    r3 = [PIN_MOD - 30, PIN_MOD - 15, PIN_MOD]
    roll_pt = (sum(b[x] for x in r3) / 3.0) if all(x in b for x in r3) else None
    d_pt = PDS.deflate(rate, roll_pt)
    d_noroll = min(max(rate, 0.0), PDS.RATE_CAP)
    if d_pt is None:
        return None
    p_pt = PDS.p_ge(hsf + d_pt, PDS.SIGMA_RATE, strike)
    p_noroll = PDS.p_ge(hsf + d_noroll, PDS.SIGMA_RATE, strike)
    return {"mu_pit": hsf + d_pt, "p_pit": p_pt, "p_pit_noroll": p_noroll,
            "rate": rate, "roll_pt": roll_pt, "hsf": hsf}


def score(ps, ys):
    d = PDS.calibration_metrics(ps, ys)
    return {
        "n": len(ps),
        "brier": round(PDS.brier(ps, ys), 4),
        "dir_acc": round(PDS.dir_acc(ps, ys), 4),
        "base_rate": round(sum(ys) / len(ys), 4),
        "mean_p": round(sum(ps) / len(ps), 4),
        "reliability": d["reliability"], "resolution": d["resolution"],
        "uncertainty": d["uncertainty"], "ece": d["ece"], "mce": d["mce"],
        "calibration_table": d["calibration_table"],
    }


def main():
    grid = load(GRID_PKL)
    recs = load(RECS_PKL)

    # attach honest point-in-time rate_1h columns onto the shared grid rows
    n_pit = 0
    for (st, ds), row in grid.items():
        rec = recs.get((st, ds))
        rp = rate_pit(rec, row["strike"])
        if rp is None:
            row["rate_1h_pit"] = row["rate_1h_pit_noroll"] = None
            row["rate_1h_pit_mu"] = None
        else:
            row["rate_1h_pit"] = rp["p_pit"]
            row["rate_1h_pit_noroll"] = rp["p_pit_noroll"]
            row["rate_1h_pit_mu"] = rp["mu_pit"]
            n_pit += 1
    # map rate_1h_pit -> p-column name for the generic scorer
    col = {"ecmwf": "ecmwf_p", "gefs": "gefs_p", "gfs": "gfs_p",
           "icon": "icon_p", "gem": "gem_p", "rate_1h": "rate_1h_p",
           "rate_1h_pit": "rate_1h_pit", "market": "market_p"}

    per_source = {}
    for s in SOURCES:
        pk = col[s]
        rows = [r for r in grid.values()
                if r.get(pk) is not None and r.get("y") is not None]
        if not rows:
            per_source[s] = {"n": 0}
            continue
        ps = [r[pk] for r in rows]
        ys = [r["y"] for r in rows]
        dates = sorted(r["date"] for r in rows)
        d = score(ps, ys)
        d["date_min"], d["date_max"] = dates[0], dates[-1]
        per_source[s] = d

    # common-row head-to-head (all eight sources present)
    keys = [col[s] for s in SOURCES]
    common = [r for r in grid.values()
              if r.get("y") is not None and all(r.get(k) is not None for k in keys)]
    common_scores = {}
    if len(common) >= 10:
        ys = [r["y"] for r in common]
        for s in SOURCES:
            common_scores[s] = score([r[col[s]] for r in common], ys)
        common_scores["_n"] = len(common)

    # honest ranking by Brier (per-source coverage)
    rank = sorted([s for s in SOURCES if per_source[s].get("brier") is not None],
                  key=lambda s: per_source[s]["brier"])

    # ---- pairwise P(cross) correlation (all rows where BOTH present) --------
    pcorr = {}
    for i, a in enumerate(SOURCES):
        for b in SOURCES[i + 1:]:
            rows = [r for r in grid.values()
                    if r.get(col[a]) is not None and r.get(col[b]) is not None]
            if len(rows) < 3:
                pcorr[f"{a}|{b}"] = {"n": len(rows), "pearson": None}
                continue
            rp = PDS.pearson([r[col[a]] for r in rows], [r[col[b]] for r in rows])
            pcorr[f"{a}|{b}"] = {"n": len(rows),
                                 "pearson": None if rp != rp else round(rp, 4)}

    # ---- level error (fcerr): honest rate_1h mu vs each NWP level ----------
    def level_err_rows(other_key):
        out = []
        for r in grid.values():
            if r.get("rate_1h_pit_mu") is None or r.get("settled_max") is None:
                continue
            if r.get(other_key) is None:
                continue
            out.append((r["station"],
                        r["rate_1h_pit_mu"] - r["settled_max"],
                        r[other_key] - r["settled_max"]))
        return out

    def demean(rows):
        from collections import defaultdict
        by = defaultdict(list)
        for st, a, b in rows:
            by[st].append((a, b))
        oa, ob = [], []
        for st, lst in by.items():
            ma = sum(a for a, _ in lst) / len(lst)
            mb = sum(b for _, b in lst) / len(lst)
            for a, b in lst:
                oa.append(a - ma)
                ob.append(b - mb)
        return oa, ob

    fcerr = {}
    for other in ["gfs", "gefs", "icon", "gem", "ecmwf"]:
        rows = level_err_rows(other)
        if len(rows) < 5:
            fcerr[other] = {"n": len(rows)}
            continue
        pooled = round(PDS.pearson([a for _, a, _ in rows],
                                   [b for _, _, b in rows]), 4)
        da, db = demean(rows)
        within = round(PDS.pearson(da, db), 4) if len(da) > 2 else None
        fcerr[other] = {"n": len(rows), "level_pooled": pooled,
                        "level_within_station_demeaned": within}

    # ---- market orthogonality vs the honest NWP field ----------------------
    nwp_keys = ["ecmwf_p", "gefs_p", "gfs_p", "icon_p", "gem_p"]
    market_rows = [r for r in grid.values()
                   if r.get("market_p") is not None and r.get("y") is not None]
    mkt_r = {}
    if market_rows:
        for k in nwp_keys + ["rate_1h_p", "rate_1h_pit"]:
            sub = [r for r in market_rows if r.get(k) is not None]
            if len(sub) >= 5:
                mkt_r[k] = {"n": len(sub),
                            "pearson": round(PDS.pearson([r["market_p"] for r in sub],
                                                         [r[k] for r in sub]), 4)}
        # mean of the honest NWP field on market rows
        allp = [r for r in market_rows
                if all(r.get(k) is not None for k in nwp_keys)]
        if len(allp) >= 5:
            field = [(sum(r[k] for k in nwp_keys) / len(nwp_keys)) for r in allp]
            mkt_r["nwp_field_mean"] = {
                "n": len(allp),
                "pearson": round(PDS.pearson([r["market_p"] for r in allp],
                                             field), 4)}

    res = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "honest point-in-time re-score",
        "why": ("published diversity scoring took window-MAX P(cross) over "
                "13:00..16:45 (RATE_MAX_MOD=16:45) past the 13:30 pin; look-ahead"),
        "pin_mod_local": PIN_MOD,
        "rate_1h_pit_definition": ("single point at local mod==810; hsf=max bucket "
                                   "mean mod<=810; roll from [780,795,810] only"),
        "strike_rule": "T = floor(hsf@13:30)+1 (climatology fallback)",
        "label_rule": "crossed = settled_max > T (strict; == is a LOSS)",
        "cohort_window": [PDS.MIGRATION_START, PDS.MIGRATION_END],
        "cohort_grid_rows": len(grid),
        "rate_1h_pit_rows": n_pit,
        "per_source": per_source,
        "honest_ranking_by_brier": rank,
        "common_row_head_to_head": common_scores,
        "pcross_correlation": pcorr,
        "fcerr_honest_rate_vs_nwp": fcerr,
        "market_orthogonality": mkt_r,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2))

    print("=" * 96)
    print("STAGE 1 — HONEST POINT-IN-TIME RE-SCORE (rate_1h bounded to <=13:30 pin)")
    print("=" * 96)
    print(f"{'source':14} {'n':>6} {'Brier':>8} {'ECE':>7} {'resol':>7} "
          f"{'reliab':>7} {'base':>6} {'mean_p':>7}  range")
    for s in SOURCES:
        r = per_source[s]
        if r.get("n", 0) == 0:
            print(f"{s:14} {'0':>6}   (no rows)")
            continue
        print(f"{s:14} {r['n']:>6} {r['brier']:>8.4f} {r['ece']:>7.4f} "
              f"{r['resolution']:>7.4f} {r['reliability']:>7.4f} "
              f"{r['base_rate']:>6.3f} {r['mean_p']:>7.3f}  "
              f"{r['date_min']}..{r['date_max']}")
    print(f"\nhonest ranking by Brier: {' > '.join(rank)}")
    if common_scores.get("_n"):
        print(f"\ncommon-row head-to-head (n={common_scores['_n']}):")
        for s in SOURCES:
            print(f"  {s:14} brier={common_scores[s]['brier']:.4f} "
                  f"dir={common_scores[s]['dir_acc']:.4f}")
    print("\npairwise P(cross) Pearson (honest rate vs):")
    for k in sorted(pcorr):
        if "rate_1h_pit" in k or "market" in k:
            print(f"  {k:26} n={pcorr[k]['n']:>5} r={pcorr[k]['pearson']}")
    print("\nfcerr (honest rate_1h level error vs NWP level error):")
    for k, v in fcerr.items():
        print(f"  vs {k:6} n={v.get('n'):>5} pooled={v.get('level_pooled')} "
              f"within={v.get('level_within_station_demeaned')}")
    print("\nmarket orthogonality (market mid vs source P(cross)):")
    for k, v in mkt_r.items():
        print(f"  {k:16} n={v['n']:>5} r={v['pearson']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
