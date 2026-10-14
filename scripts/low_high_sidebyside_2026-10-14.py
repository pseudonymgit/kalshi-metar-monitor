#!/usr/bin/env python3
"""low_high_sidebyside_2026-10-14.py — definitive LOW vs HIGH comparison.

Dan (2026-10-14): "Give me end to end side by side HIGH vs LOW, expert derived."
Dan disagrees with the earlier "keep the same pin" finding, so this doc-based
comparison is backed by TWO concrete analyses:

  1. om_gfs Tmin Brier @13:30 pin on the LOW backfill cohort
     (178 station-days with canonical LOW truth, 84 with ASOS recs), with the
     HIGH om_gfs Tmax anchor recomputed on the honest 2296 cohort.
  2. Cooling-rate signal (rlow_1h) — the exact mirror of HIGH rate_1h on FALLING
     temps (running-low). Sweep pins 18:00..23:00 local, report Brier per pin,
     plus the running-low LEVEL-ONLY arm that isolates whether cooling adds
     discrimination.

Reuses the EXISTING machinery verbatim (no re-implementation):
  * LOW : scripts/low_trajectory_signal.py (recs, cooling_nowcast, sweep_pin,
          om_tmin_arm, level_metrics, cohorts A/B, truth loaders)
  * HIGH: scripts/honest_signal_accuracy_2026-10-14.py (om_gfs Tmax axis/cohort)
          + scripts/fusion_weight_sweep_2026-10-14.py (fusion-cohort om_gfs)

READ-ONLY on every book. No trades writes. Writes ONLY:
  data/low_high_sidebyside_2026-10-14.json
  artifacts/low-market-lane/low_high_sidebyside_2026-10-14.json

usage: low_high_sidebyside_2026-10-14.py [run]
"""
from __future__ import annotations

import argparse
import importlib.util as ilu
import json
import math
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load(name, rel):
    spec = ilu.spec_from_file_location(name, REPO / rel)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOW = _load("lowtraj", "scripts/low_trajectory_signal.py")
HSA = _load("hsa", "scripts/honest_signal_accuracy_2026-10-14.py")
FWS = _load("fws", "scripts/fusion_weight_sweep_2026-10-14.py")

ART_DIR = REPO / "artifacts" / "low-market-lane"
OUT_JSON = REPO / "data" / "low_high_sidebyside_2026-10-14.json"
OUT_ART_JSON = ART_DIR / "low_high_sidebyside_2026-10-14.json"

EVENING_PINS = [1080, 1110, 1140, 1170, 1200, 1230, 1260, 1290, 1320, 1350, 1380]


def log(m):
    sys.stderr.write(f"[lhs] {m}\n")
    sys.stderr.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="run", choices=["run"])
    args = ap.parse_args()
    t0 = time.time()

    log("loading ASOS recs + LOW truths ...")
    recs = LOW.load_recs()
    ghcn = LOW.load_ghcn_lows()
    low_truth, low_sds = LOW.load_low_backfill_truth()

    keys_A = sorted(k for k in low_sds if k in recs and k in low_truth)
    keys_B = []
    for k in recs:
        if k in ghcn and recs[k].get("buckets"):
            b = recs[k]["buckets"]
            if b and min(b) == 0 and max(b) >= 1350:
                keys_B.append(k)
    keys_B = sorted(keys_B)
    log(f"cohort A={len(keys_A)} (of {len(low_sds)} truth station-days)  B={len(keys_B)}")

    # =====================================================================
    # ANALYSIS 1 — om_gfs Tmin @13:30 pin (LOW backfill cohort)
    # =====================================================================
    log("analysis 1: om_gfs Tmin @13:30 ...")
    om_t = LOW.fetch_omgfs_tmin(force=False)
    om_pairs = [(om_t[k] - low_truth[k]) for k in keys_A if k in om_t]
    om_bias = LOW._mean(om_pairs) if om_pairs else 0.0
    om_sigma = LOW._std(om_pairs) if len(om_pairs) > 1 else 3.27
    # per-station bias/sigma (refit on cohort A)
    from collections import defaultdict
    res = defaultdict(list)
    for k in keys_A:
        if k in om_t:
            res[k[0]].append(om_t[k] - low_truth[k])
    bias_st = {st: LOW._mean(v) for st, v in res.items() if len(v) >= 3}
    sigma_st = {st: (LOW._std(v) or om_sigma) for st, v in res.items() if len(v) >= 3}

    def om_tmin_arm_st(keys, pin_mod, use_st=True):
        """om_gfs Tmin level signal with per-station bias (mirror of om_tmin_arm)."""
        ps, ys = [], []
        for (st, ds) in keys:
            rec = recs.get((st, ds))
            tmin = low_truth.get((st, ds))
            mu = om_t.get((st, ds))
            if rec is None or tmin is None or mu is None:
                continue
            b = rec.get("buckets", {})
            vals = [b[x] for x in b if x <= pin_mod]
            if not vals:
                continue
            T = math.floor(min(vals)) - 0.5
            bcorr = (bias_st.get(st, om_bias) if use_st else om_bias)
            sg = (sigma_st.get(st, om_sigma) if use_st else om_sigma)
            ps.append(LOW.p_low_le(mu - bcorr, sg, T))
            ys.append(1 if tmin <= T else 0)
        return LOW._score(ps, ys) if ps else {"n": 0}

    tmin_1330 = {
        "pin": "13:30",
        "per_station_bc": om_tmin_arm_st(keys_A, 810, use_st=True),
        "global_bc": om_tmin_arm_st(keys_A, 810, use_st=False),
        "raw": LOW.om_tmin_arm(recs, low_truth, keys_A, om_t, bias=0.0,
                               sigma=om_sigma, pin_mod=810),
    }
    log(f"  om_gfs Tmin @13:30 global-bc: {tmin_1330['global_bc']}")

    # per-pin context (global-bc, cohort A) to show the plateau/decline
    per_pin_tmin = {LOW.PIN_LABEL.get(p, str(p)): om_tmin_arm_st(keys_A, p, use_st=False)
                    for p in LOW.CONTEXT_PINS + LOW.SWEEP_PINS}

    # HIGH anchor — om_gfs Tmax @13:30 (honest cohorts)
    log("analysis 1: HIGH om_gfs Tmax anchor ...")
    # (a) fusion cohort (n=2296) → the 0.1096 figure
    try:
        fcohort = FWS.build_cohort()
        om_ps = [r["p_om_gfs"] for r in fcohort if r["p_om_gfs"] is not None]
        om_ys = [r["y"] for r in fcohort if r["p_om_gfs"] is not None]
        high_fusion_cohort = HSA.PDS.calibration_metrics(om_ps, om_ys)
        high_fusion_cohort = {
            "n": len(om_ps),
            "brier": round(HSA.PDS.brier(om_ps, om_ys), 4),
            "ece": high_fusion_cohort["ece"],
            "dir_acc": round(HSA.PDS.dir_acc(om_ps, om_ys), 4),
            "base_rate": round(sum(om_ys) / len(om_ys), 4),
        }
    except Exception as e:  # noqa: BLE001
        high_fusion_cohort = {"error": str(e)}
    log(f"  HIGH om_gfs Tmax (fusion cohort): {high_fusion_cohort}")

    # =====================================================================
    # ANALYSIS 2 — cooling-rate (rlow_1h) sweep 18:00..23:00
    # =====================================================================
    log("analysis 2: cooling-rate sweep 18:00..23:00 ...")
    cooling = {"cohort_A_low_backfill": {}, "cohort_B_asos_ghcn_full": {}}
    for name, keys, truth in (("cohort_A_low_backfill", keys_A, low_truth),
                              ("cohort_B_asos_ghcn_full", keys_B, ghcn)):
        for p in EVENING_PINS:
            lab = LOW.PIN_LABEL[p]
            cool = LOW.sweep_pin(recs, truth, keys, p, offset=1.0, use_cooling=True)
            lvl = LOW.sweep_pin(recs, truth, keys, p, offset=1.0, use_cooling=False)
            for arm in (cool, lvl):
                b = arm.get("base_rate")
                arm["bss"] = (round(1 - arm["brier"] / (b * (1 - b)), 4)
                              if b and 0 < b < 1 and arm.get("n") else None)
            cooling[name][lab] = {"pin_mod": p, "cooling": cool, "level_only": lvl}
        log(f"  {name}: swept {len(EVENING_PINS)} pins")

    # custody of the HIGH rate_1h anchor (from the honest register number)
    high_rate_anchor = {"honest_rate_1h_brier_1330": 0.3617,
                        "source": "docs/plans/HONEST-SIGNAL-ACCURACY-2026-10-14.md"}

    result = {
        "generated_at_utc": HSA.datetime.now(HSA.timezone.utc).isoformat(),
        "axis_low": ("T=floor(rlow@pin)-0.5; y = settled_min <= T; "
                     "p = Phi((T-mu_low)/sigma)"),
        "axis_high": ("T=floor(hsf@13:30)+1; y = settled_max > T (== LOSS); "
                      "truth = canonical GHCN"),
        "cohorts": {
            "low_backfill_truth_station_days": len(low_sds),
            "low_A_with_asos_recs": len(keys_A),
            "low_B_asos_ghcn_full": len(keys_B),
        },
        "analysis_1_om_gfs_tmin_1330": {
            "cohort_A_n_truth": len(low_sds),
            "cohort_A_n_scored": len(om_pairs),
            "bias_global": round(om_bias, 4),
            "sigma_global": round(om_sigma, 4),
            "level_mae_global_bc": round(
                sum(abs(om_t[k] - (low_truth[k] - om_bias)) for k in keys_A if k in om_t)
                / max(1, len(om_pairs)), 4),
            "level_mae_raw": round(
                sum(abs(om_t[k] - low_truth[k]) for k in keys_A if k in om_t)
                / max(1, len(om_pairs)), 4),
            "at_1330": tmin_1330,
            "per_pin_global_bc": per_pin_tmin,
            "high_tmax_anchor_fusion_cohort": high_fusion_cohort,
            "high_tmax_anchor_full_cohort": {
                "n": 2296, "brier": 0.1067,
                "source": "docs/plans/FUSION-WEIGHT-SWEEP-2026-10-14.md"},
        },
        "analysis_2_cooling_rate_sweep": cooling,
        "high_rate_1h_anchor": high_rate_anchor,
        "notes": [
            "Cohort A = the requested LOW backfill subset (178 truth station-days, "
            "84 with ASOS recs + om_gfs Tmin). Cohort B = the ASOS∩GHCN-low "
            "full-coverage panel (sensitivity: 12,629 days).",
            "cohort A per-pin n ~52-84 → pin ORDERING on A is noisy; B is the "
            "stable read for what moves with the pin.",
            "om_gfs Tmin @13:30 uses a per-station bias/sigma refit where a station "
            "has >=3 observations, else the global bias/sigma.",
        ],
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ART_JSON.write_text(json.dumps(result, indent=2))
    log(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size} bytes), {time.time()-t0:.1f}s")

    # console summary
    a1 = result["analysis_1_om_gfs_tmin_1330"]
    print(json.dumps({
        "low_om_tmin_1330_per_station_bc": a1["at_1330"]["per_station_bc"],
        "low_om_tmin_1330_global_bc": a1["at_1330"]["global_bc"],
        "low_om_tmin_1330_raw": a1["at_1330"]["raw"],
        "high_om_tmax_fusion_cohort": a1["high_tmax_anchor_fusion_cohort"],
        "high_om_tmax_full_cohort": a1["high_tmax_anchor_full_cohort"],
    }, indent=2))
    print("\ncooling sweep (cohort A):")
    for lab, d in result["analysis_2_cooling_rate_sweep"]["cohort_A_low_backfill"].items():
        c, l = d["cooling"], d["level_only"]
        print(f"  {lab:>6} cooling n={c.get('n')} brier={c.get('brier')} bss={c.get('bss')} "
              f"| level_only brier={l.get('brier')} bss={l.get('bss')}")
    print("\ncooling sweep (cohort B):")
    for lab, d in result["analysis_2_cooling_rate_sweep"]["cohort_B_asos_ghcn_full"].items():
        c, l = d["cooling"], d["level_only"]
        print(f"  {lab:>6} cooling n={c.get('n')} brier={c.get('brier')} bss={c.get('bss')} "
              f"| level_only brier={l.get('brier')} bss={l.get('bss')}")


if __name__ == "__main__":
    main()
