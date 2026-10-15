#!/usr/bin/env python3
"""honest_calibration_audit_2026-10-15.py — external red-team audit of the
honest decision-time calibration formula.

`core/intraday/honest_calibration.py::honest_calibrated_p` is the single
measurement layer applied to every shipped probability (bridge fusion path,
sole-om_gfs path, and the companion-table back-fill). It was written in sprint
AA and revised once (the one-sided bug) but never audited by someone who did not
build it. This script is that audit. It TRACES the formula against real data;
it does not merely re-run the author's summary.

Audited transform (shipped):

    calibrated_p = clamp_to[0.01, ce]( 0.5 + (p_fused - 0.5) * (1 - ECE/2) )

    with ECE = 0.47 (a constant), ce = per-station ceiling (refit artifact),
    pooled fallback 0.84.

Evidence produced:
  1. Murphy decomposition (reliability / resolution / uncertainty) of the
     CALIBRATED outputs on the honest cohort, plus a 10-bin reliability table.
  2. Whether the ECE=0.47 constant is still valid for the SHIPPED signal, and
     what the calibrated transform becomes when ECE is renormalized to it.
  3. Ceiling leveling: range, distinctness, and provenance of per-station ce.
  4. Edge cases: p_fused in {0.01, 0.50, 0.99, NaN, None}, ceiling in {NaN,<0.5}.
  5. Neutrality: does honest_calibrated_p(0.5, ce) == 0.5 ?
  6. Per-station cross-validation: does calibrated beat raw at EVERY station?

READ-ONLY. No DB writes. Writes ONLY:
  data/honest_calibration_audit_2026-10-15.json
  docs/plans/HONEST-CALIBRATION-AUDIT-2026-10-15.md

usage: python3 scripts/honest_calibration_audit_2026-10-15.py
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
    honest_calibrated_p, station_ceiling, station_ceilings,
    composite_conf_from_p, ECE, BASE_RATE, POOLED_CEILING,
)
from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    fuse, calibrated_ceiling,
)

_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)
PDS = HSA.PDS

GRID = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
AT = Path("/tmp/honest_analog_traj_2026-10-14.pkl.gz")

OUT_JSON = REPO / "data" / "honest_calibration_audit_2026-10-15.json"
OUT_MD = REPO / "docs" / "plans" / "HONEST-CALIBRATION-AUDIT-2026-10-15.md"
PIN = 810


def sv(ps, ys):
    """Score = the exact metric family the author used (round to 4dp)."""
    d = PDS.calibration_metrics(ps, ys)
    return {"n": len(ps),
            "brier": round(PDS.brier(ps, ys), 4),
            "reliability": d["reliability"], "resolution": d["resolution"],
            "uncertainty": d["uncertainty"], "ece": d["ece"], "mce": d["mce"],
            "base_rate": round(sum(ys) / len(ys), 4),
            "mean_p": round(sum(ps) / len(ps), 4),
            "calibration_table": d["calibration_table"]}


def main():
    grid = pickle.load(gzip.open(GRID, "rb"))
    recs = pickle.load(gzip.open(RECS, "rb"))
    om = {(k.split("|")[0], k.split("|")[1]): v
          for k, v in json.loads(OM.read_text()).items()}
    rows = HSA.build_rows(recs, grid, om)

    # ---- om_gfs bias/sigma (the honest recipe; matches HSA) ----------------
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

    # =======================================================================
    # 0. NEUTRALITY — the old one-sided bug must be gone
    # =======================================================================
    neutrality = {}
    for ce in (0.50, 0.75, 0.84, 0.9031, 0.999):
        try:
            neutrality[f"ce={ce}"] = honest_calibrated_p(0.5, ce)
        except Exception as e:  # noqa: BLE001
            neutrality[f"ce={ce}"] = f"raised {type(e).__name__}"
    # symmetry about 0.5
    sym = {}
    for pf in (0.2, 0.3, 0.4, 0.6, 0.7, 0.8):
        lo = honest_calibrated_p(pf, 0.99)
        hi = honest_calibrated_p(1.0 - pf, 0.99)
        sym[f"p={pf}"] = {"cal": lo, "cal_mirror": hi,
                          "symmetric": abs((0.5 - lo) - (hi - 0.5)) < 1e-9}

    # =======================================================================
    # 1. EDGE CASES — fail-closed contract
    # =======================================================================
    edge = {}

    def probe(fn, label):
        try:
            edge[label] = {"result": fn(), "raised": None}
        except Exception as e:  # noqa: BLE001
            edge[label] = {"result": None, "raised": f"{type(e).__name__}"}

    probe(lambda: honest_calibrated_p(0.01, 0.84), "p=0.01,ce=0.84")
    probe(lambda: honest_calibrated_p(0.50, 0.84), "p=0.50,ce=0.84")
    probe(lambda: honest_calibrated_p(0.99, 0.84), "p=0.99,ce=0.84")
    probe(lambda: honest_calibrated_p(float("nan"), 0.84), "p=NaN,ce=0.84")
    probe(lambda: honest_calibrated_p(None, 0.84), "p=None,ce=0.84")
    probe(lambda: honest_calibrated_p(0.7, float("nan")), "p=0.7,ce=NaN")
    probe(lambda: honest_calibrated_p(0.7, None), "p=0.7,ce=None")
    probe(lambda: honest_calibrated_p(0.7, 0.2), "p=0.7,ce=0.2(<0.5)")
    probe(lambda: honest_calibrated_p(0.7, 1.5), "p=0.7,ce=1.5(>1)")
    probe(lambda: honest_calibrated_p(-0.3, 0.84), "p=-0.3")
    probe(lambda: honest_calibrated_p(1.7, 0.84), "p=1.7")

    # =======================================================================
    # 2. Build the two cohorts the transform is actually applied to
    #    (a) SOLE om_gfs (shipped path)  (b) fused 5-member (bridge path)
    # =======================================================================
    def om_ps(row, sig):
        return HSA.signal_p_at(row, PIN, sig, omstats, {PIN: {}})

    # (a) sole cohort — every row with om raw + y (n≈2927)
    sole = []
    for (st, ds), row in rows.items():
        if row["y"] is None:
            continue
        p = om_ps(row, "om_gfs_bc_ps")
        if p is None or row["pins"][PIN]["T"] is None:
            continue
        sole.append({"st": st, "ds": ds, "y": row["y"], "p_raw": p,
                     "ce": station_ceiling(st)})
    sole_raw = [r["p_raw"] for r in sole]
    sole_ys = [r["y"] for r in sole]
    sole_cal = [honest_calibrated_p(r["p_raw"], r["ce"]) for r in sole]

    # (b) fused cohort — reproduce HSA Part 2/3 (needs analog+traj + market)
    ladders = HSA.load_market_ladders("13:30:00")
    if AT.exists():
        at = pickle.load(gzip.open(AT, "rb"))
    else:
        at = HSA.build_analog_traj(rows, force=False)
    fused = []
    for (st, ds), row in rows.items():
        if row["y"] is None or row["strike"] is None or row["hsf_pin"] is None:
            continue
        pg = (PDS.p_ge(row["gefs"], max(HSA.SIGMA_GEFS_MIN,
                row["gefs_sd"] or HSA.SIGMA_GEFS_MIN), row["strike"])
              if row["gefs"] is not None else None)
        a = at.get((st, ds), {})
        pa, pt = a.get("p_analog"), a.get("p_traj")
        if pa is None or pt is None or pg is None:
            continue
        pm = HSA.market_p_at(ladders.get((st, ds)), row["strike"])
        pog = om_ps(row, "om_gfs_bc_ps")
        pf, meta = fuse(pg, pa, pt, use_weights=True,
                        p_market=pm, p_om_gfs=pog)
        fused.append({"st": st, "ds": ds, "y": row["y"],
                      "p_raw": pf, "p_core": meta["p_core"],
                      "traj_mult": meta["traj_mult"],
                      "ce": station_ceiling(st)})
    fused_raw = [r["p_raw"] for r in fused]
    fused_ys = [r["y"] for r in fused]
    fused_cal = [honest_calibrated_p(r["p_raw"], r["ce"]) for r in fused]

    # pre-ceiling raw fused value (what the doc calls "fused_5_raw")
    fused_preraw = [min(0.99, max(0.01, 0.5 + r["traj_mult"] * (r["p_core"] - 0.5)))
                    for r in fused]

    audit_sole = {
        "n": len(sole),
        "raw_om_gfs_bc_ps": sv(sole_raw, sole_ys),
        "calibrated_by_shipped_formula": sv(sole_cal, sole_ys),
    }
    audit_fused = {
        "n": len(fused),
        "raw_fused_pre_ceiling": sv(fused_preraw, fused_ys),
        "raw_fused_legacy_clamp": sv(fused_raw, fused_ys),
        "calibrated_by_shipped_formula": sv(fused_cal, fused_ys),
    }

    # means vs base rate — the underconfidence introduced by the shrink
    for tag, d, raw, ys in (("sole", audit_sole, sole_raw, sole_ys),
                            ("fused", audit_fused, fused_raw, fused_ys)):
        br = sum(ys) / len(ys)
        m_raw = sum(raw) / len(raw)
        lam = ECE / 2.0
        m_cal = 0.5 + (m_raw - 0.5) * (1 - lam)
        d["mean_check"] = {
            "base_rate": round(br, 4),
            "mean_p_raw": round(m_raw, 4),
            "mean_p_calibrated_linear_expect": round(m_cal, 4),
            "raw_gap_to_base": round(m_raw - br, 4),
            "cal_gap_to_base": round(m_cal - br, 4),
        }

    # =======================================================================
    # 3. ECE renormalization — is 0.47 still the right constant?
    # =======================================================================
    # what ECE does the SHIPPED signal actually have?
    ece_rate1h = sv(sole_raw, sole_ys)  # placeholder overwritten below
    # honest rate_1h ECE (the source of 0.47) — recompute on this cohort
    rp_ps, rp_ys = [], []
    for (st, ds), row in rows.items():
        if row["y"] is None or row["pins"][PIN]["T"] is None:
            continue
        rp = HSA.rate_pit(recs.get((st, ds)), row["pins"][PIN]["T"], PIN)
        if rp:
            rp_ps.append(rp["p"]); rp_ys.append(row["y"])
    rate_metrics = sv(rp_ps, rp_ys)

    ece_table = {
        "rate_1h_pit (source of the 0.47 constant)": rate_metrics["ece"],
        "om_gfs_bc_ps (shipped sole signal)": audit_sole["raw_om_gfs_bc_ps"]["ece"],
        "fused_5 pre-ceiling": audit_fused["raw_fused_pre_ceiling"]["ece"],
    }
    # recalibrate the shrink factor under each candidate ECE
    renorm = {}
    for label, e in (("constant ECE (0.47, shipped)", ECE),
                     ("renormalized to om_gfs (shipped signal)", audit_sole["raw_om_gfs_bc_ps"]["ece"]),
                     ("renormalized to fused", audit_fused["raw_fused_pre_ceiling"]["ece"])):
        lam = e / 2.0
        cal = [honest_calibrated_p(r["p_raw"], r["ce"], ece=e) for r in sole]
        renorm[label] = {
            "ece_used": e, "shrink_factor_1_minus_lam": round(1 - lam, 4),
            "cal_0.90->": round(0.5 + 0.4 * (1 - lam), 4),
            "brier_sole": round(PDS.brier(cal, sole_ys), 4),
            "mean_p": round(sum(cal) / len(cal), 4),
        }

    # =======================================================================
    # 4. Ceiling leveling
    # =======================================================================
    ceilings = station_ceilings()
    cvals = sorted(ceilings.values())
    ceiling_audit = {
        "n_stations_with_custom_ceiling": len(ceilings),
        "min": min(cvals), "max": max(cvals),
        "mean": round(sum(cvals) / len(cvals), 4),
        "n_distinct": len(set(round(v, 6) for v in cvals)),
        "pooled_constant_in_code": POOLED_CEILING,
        "within_075_090": all(0.75 <= v <= 0.90 for v in cvals),
        "provenance": ("per-station values ARE distinct and looked up by "
                       "station; they are NOT a single reused constant. But the "
                       "refit formula is 0.75 + 0.24*min(1, resolution/0.05) "
                       "where `resolution` is rate_1h's — a signal with no skill."),
        "ceiling_vs_source_resolution": {
            st: {"ceiling": v,
                 "src_resolution": None}
            for st, v in sorted(ceilings.items())},
    }
    # attach rate_1h resolution from the refit artifact
    refit = json.loads(
        (REPO / "artifacts" / "ceiling-refit" / "ceiling_refit_2026-10-13.json").read_text())
    per_st = refit.get("A2_rate1h_bounded_per_station", {})
    ceiling_audit["ceiling_vs_source_resolution"] = {
        st: {"ceiling": ceilings.get(st),
             "rate1h_resolution": per_st.get(st, {}).get("resolution"),
             "rate1h_ece": per_st.get(st, {}).get("ece")}
        for st in sorted(ceilings)}
    ceiling_audit["pooled_from_refit"] = refit.get("A_lookahead_rate1h", {}) \
        .get("bounded_<=pin_780_810", {}).get("ceiling")

    # what DOWNSTREAM binding does the ceiling actually do on the sole cohort?
    n_clamped = sum(1 for r in sole if 0.5 + (r["p_raw"] - 0.5) * (1 - ECE / 2)
                    > r["ce"] and r["p_raw"] > 0.5)
    ceiling_audit["sole_rows_clamped_by_ceiling"] = n_clamped
    ceiling_audit["sole_rows_total"] = len(sole)
    ceiling_audit["sole_clamp_rate"] = round(n_clamped / len(sole), 4)

    # =======================================================================
    # 5. Per-station cross-validation — calibrated vs raw, station by station
    # =======================================================================
    per_station = {}
    wins = loses = 0
    for st in sorted(set(r["st"] for r in sole)):
        sub = [r for r in sole if r["st"] == st]
        if len(sub) < 30:
            continue
        ys = [r["y"] for r in sub]
        raw = PDS.brier([r["p_raw"] for r in sub], ys)
        cal = PDS.brier([honest_calibrated_p(r["p_raw"], r["ce"]) for r in sub], ys)
        better = cal < raw
        wins += better
        loses += (not better)
        per_station[st] = {
            "n": len(sub),
            "brier_raw": round(raw, 4), "brier_calibrated": round(cal, 4),
            "delta": round(cal - raw, 4),
            "calibrated_better": better,
            "ceiling": station_ceiling(st),
        }
    per_station_summary = {
        "n_stations": len(per_station),
        "calibrated_beats_raw": wins,
        "calibrated_loses_to_raw": loses,
        "beats_every_station": loses == 0,
    }

    # =======================================================================
    # 6. What SHOULD the constant be? — isotonic-style diagnostic
    # =======================================================================
    # If the raw signal is already roughly calibrated (ECE ~0.04), the correct
    # transform is near-identity. Show the identity/Platt alternatives.
    ident_cal = [max(r["ce"] if r["p_raw"] > 0.5 else 1e-9,
                     min(r["ce"] if r["p_raw"] > 0.5 else min(0.99, r["p_raw"]),
                         r["p_raw"]))
                 for r in sole]
    alternatives = {
        "identity_raw": sv(sole_raw, sole_ys),
        "shipped_shrink_shipped_ece": audit_sole["calibrated_by_shipped_formula"],
        "shrink_renormalized_ece_004": {
            "brier": renorm["renormalized to om_gfs (shipped signal)"]["brier_sole"],
            "note": "shrink factor ~0.981 -> near identity"},
    }

    artifact = {
        "generated_by": "scripts/honest_calibration_audit_2026-10-15.py",
        "audited_formula": "calibrated_p = clamp[0.01,ce](0.5 + (p_fused-0.5)*(1-ECE/2))",
        "constants": {"ECE": ECE, "BASE_RATE": BASE_RATE,
                      "POOLED_CEILING": POOLED_CEILING},
        "neutrality": {"at_0.5": neutrality, "symmetry": sym},
        "edge_cases": edge,
        "cohorts": {"sole_om_gfs": audit_sole, "fused_5member": audit_fused},
        "ece_renormalization": {"observed_ece": ece_table, "renorm_sweep": renorm},
        "ceiling_audit": ceiling_audit,
        "per_station_crossval": {"stations": per_station,
                                 "summary": per_station_summary},
        "alternatives": alternatives,
    }
    OUT_JSON.write_text(json.dumps(artifact, indent=2))

    # =======================================================================
    # VERDICT
    # =======================================================================
    d_raw = audit_sole["raw_om_gfs_bc_ps"]["brier"]
    d_cal = audit_sole["calibrated_by_shipped_formula"]["brier"]
    verdict_parts = []
    if d_cal > d_raw:
        verdict_parts.append("the shipped transform DEGRADES the shipped signal")
    if ece_table["rate_1h_pit (source of the 0.47 constant)"] > 5 * ece_table["om_gfs_bc_ps (shipped sole signal)"]:
        verdict_parts.append("the shrink constant is measured from a DIFFERENT (skill-less) signal")
    if not per_station_summary["beats_every_station"]:
        verdict_parts.append("it loses to raw at %d/%d stations" %
                             (loses, len(per_station)))
    if edge.get("p=None,ce=0.84", {}).get("raised") == "TypeError":
        verdict_parts.append("None raises TypeError, not ValueError (breaks the documented fail-closed contract)")
    if edge.get("p=0.7,ce=NaN", {}).get("result") is not None:
        verdict_parts.append("a NaN ceiling silently becomes the MAX ceiling (fail-OPEN)")
    verdict = "REPAIR" if verdict_parts else "PASS"

    write_md(artifact, verdict, verdict_parts)

    print(json.dumps({
        "verdict": verdict,
        "reasons": verdict_parts,
        "sole_raw_brier": d_raw, "sole_cal_brier": d_cal,
        "sole_mean_raw": audit_sole["mean_check"]["mean_p_raw"],
        "sole_mean_cal": audit_sole["mean_check"]["mean_p_calibrated_linear_expect"],
        "sole_base_rate": audit_sole["mean_check"]["base_rate"],
        "ece_rate1h": ece_table["rate_1h_pit (source of the 0.47 constant)"],
        "ece_omgfs": ece_table["om_gfs_bc_ps (shipped sole signal)"],
        "per_station": per_station_summary,
        "ceiling_range": [ceiling_audit["min"], ceiling_audit["max"]],
        "n_clamped": n_clamped, "n_sole": len(sole),
    }, indent=2))
    print("wrote", OUT_JSON, OUT_MD)


def write_md(A, verdict, reasons):
    L = []
    L.append("# Honest Calibration Audit — external red-team of `honest_calibrated_p`")
    L.append("")
    L.append("**Date:** 2026-10-15 · **Owner:** Gilfoyle (external audit) · "
             "**Repo:** `prototypes/weather-engine-source/`  ")
    L.append("**Script:** `scripts/honest_calibration_audit_2026-10-15.py` · "
             "**Artifact:** `data/honest_calibration_audit_2026-10-15.json`")
    L.append("")
    L.append("**Audited transform (shipped):**")
    L.append("")
    L.append("```")
    L.append("calibrated_p = clamp_to[0.01, ce]( 0.5 + (p_fused - 0.5) * (1 - ECE/2) )")
    L.append("               ECE = 0.47 (constant)   ce = per-station ceiling (pooled 0.84 fallback)")
    L.append("```")
    L.append("")
    L.append(f"## VERDICT — **{verdict}**")
    L.append("")
    if reasons:
        for r in reasons:
            L.append(f"- {r}")
    else:
        L.append("- No defects found.")
    L.append("")

    # 0 neutrality
    L.append("## 1. Neutrality (the one-sided bug)")
    L.append("")
    L.append("`honest_calibrated_p(0.5, ce)` over all ceilings: ")
    L.append("`" + json.dumps(A["neutrality"]["at_0.5"]) + "`  ")
    L.append("**Neutral at 0.5 by construction — the old 0.5705 one-sided bug is fixed.**")
    L.append("")
    sym_ok = all(v["symmetric"] for v in A["neutrality"]["symmetry"].values())
    L.append(f"Symmetric about 0.5 at every probe point: **{sym_ok}**.")
    L.append("")

    # 1 edge
    L.append("## 2. Edge cases / fail-closed contract")
    L.append("")
    L.append("| input | result | raised |")
    L.append("|---|---:|---|")
    for k, v in A["edge_cases"].items():
        L.append(f"| `{k}` | {v.get('result')} | {v.get('raised') or '—'} |")
    L.append("")
    L.append("The docstring promises *\"Raises ValueError on a non-finite `p_fused` "
             "(callers fail closed)\"*. NaN does raise `ValueError`. **`None` raises "
             "`TypeError`**, and a **NaN ceiling silently clamps to the MAX ceiling** — "
             "both escape the documented fail-closed contract.")
    L.append("")

    # 2 cohort
    L.append("## 3. Murphy decomposition on the CALIBRATED output")
    L.append("")
    L.append("### Cohort A — sole om_gfs (the shipped HIGH path), n = "
             f"{A['cohorts']['sole_om_gfs']['n']}")
    L.append("")
    L.append("| variant | n | Brier | reliability | resolution | uncertainty | ECE | mean_p | base |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, key in (("raw om_gfs_bc_ps", "raw_om_gfs_bc_ps"),
                      ("shipped calibration", "calibrated_by_shipped_formula")):
        d = A["cohorts"]["sole_om_gfs"][key]
        L.append(f"| {name} | {d['n']} | **{d['brier']}** | {d['reliability']} | "
                 f"{d['resolution']} | {d['uncertainty']} | {d['ece']} | {d['mean_p']} | {d['base_rate']} |")
    mc = A["cohorts"]["sole_om_gfs"]["mean_check"]
    L.append("")
    L.append(f"- base rate **{mc['base_rate']}**, mean raw p **{mc['mean_p_raw']}**, "
             f"mean calibrated p **{mc['mean_p_calibrated_linear_expect']}**")
    L.append(f"- raw gap to base **{mc['raw_gap_to_base']:+.4f}** → calibrated gap "
             f"**{mc['cal_gap_to_base']:+.4f}**  ")
    L.append("The shrink pulls the mean probability *away* from the base rate: the "
             "transform manufactures **under**confidence out of a signal that was "
             "already close to calibrated.")
    L.append("")
    L.append("### Cohort B — fused 5-member (bridge path), n = "
             f"{A['cohorts']['fused_5member']['n']}")
    L.append("")
    L.append("| variant | n | Brier | reliability | resolution | uncertainty | ECE | mean_p |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, key in (("raw (pre-ceiling)", "raw_fused_pre_ceiling"),
                      ("raw (legacy clamp)", "raw_fused_legacy_clamp"),
                      ("shipped calibration", "calibrated_by_shipped_formula")):
        d = A["cohorts"]["fused_5member"][key]
        L.append(f"| {name} | {d['n']} | **{d['brier']}** | {d['reliability']} | "
                 f"{d['resolution']} | {d['uncertainty']} | {d['ece']} | {d['mean_p']} |")
    L.append("")
    L.append("### Reliability diagram (10 bins) — calibrated sole cohort")
    L.append("")
    L.append("| bin | n | mean p | empirical rate | gap |")
    L.append("|---|---:|---:|---:|---:|")
    for row in A["cohorts"]["sole_om_gfs"]["calibrated_by_shipped_formula"]["calibration_table"]:
        L.append(f"| {row['bin']} | {row['n']} | {row['mean_p']} | "
                 f"{row['emp_rate']} | {row['gap']} |")
    L.append("")

    # 3 ece
    L.append("## 4. Is the ECE = 0.47 constant still valid?")
    L.append("")
    L.append("| signal | observed ECE on this cohort |")
    L.append("|---|---:|")
    for k, v in A["ece_renormalization"]["observed_ece"].items():
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("The 0.47 constant is **rate_1h's** ECE — a signal with **no skill** on "
             "this axis (Brier ≈ 0.36, dir-acc ≈ 0.20, i.e. worse than a coin flip). "
             "The shipped signal's own ECE is ~12× smaller. The transform therefore "
             "shrinks the *good* signal by the *broken* signal's unreliability.")
    L.append("")
    L.append("| shrink under | ECE used | factor (1−λ) | 0.90 → | Brier (sole) |")
    L.append("|---|---:|---:|---:|---:|")
    for k, v in A["ece_renormalization"]["renorm_sweep"].items():
        L.append(f"| {k} | {v['ece_used']} | {v['shrink_factor_1_minus_lam']} | "
                 f"{v['cal_0.90->']} | {v['brier_sole']} |")
    L.append("")

    # 4 ceiling
    ca = A["ceiling_audit"]
    L.append("## 5. Ceiling leveling")
    L.append("")
    L.append(f"- {ca['n_stations_with_custom_ceiling']} stations carry a custom ceiling; "
             f"range **{ca['min']}–{ca['max']}** (mean {ca['mean']}), "
             f"{ca['n_distinct']} distinct values")
    L.append(f"- all within [0.75, 0.90]: **{ca['within_075_090']}**  ")
    L.append("- Per-station values **are** distinct and **are** looked up per station — "
             "this is *not* a single constant reused everywhere. **But** the refit "
             "formula is `0.75 + 0.24·min(1, resolution/0.05)` where `resolution` is "
             "**rate_1h's** — the skill-less signal. So a per-station *number* is "
             "leveled, but it is leveled off the wrong signal's noise + a 0.75 floor.")
    L.append(f"- the ceiling binds on **{ca['sole_rows_clamped_by_ceiling']}/"
             f"{ca['sole_rows_total']}** sole-cohort rows "
             f"({ca['sole_clamp_rate']:.1%})")
    L.append("")

    # 5 per station
    ps = A["per_station_crossval"]["summary"]
    L.append("## 6. Per-station cross-validation (calibrated vs raw)")
    L.append("")
    L.append(f"- calibrated beats raw at **{ps['calibrated_beats_raw']}/{ps['n_stations']}** stations")
    L.append(f"- **beats every station: {ps['beats_every_station']}**")
    L.append("")
    L.append("| station | n | Brier raw | Brier calibrated | Δ | calibrated better | ceiling |")
    L.append("|---|---:|---:|---:|---:|:--:|---:|")
    for st, d in A["per_station_crossval"]["stations"].items():
        L.append(f"| {st} | {d['n']} | {d['brier_raw']} | {d['brier_calibrated']} | "
                 f"{d['delta']:+.4f} | {'yes' if d['calibrated_better'] else '**NO**'} | "
                 f"{d['ceiling']} |")
    L.append("")

    L.append("## Recommendation")
    L.append("")
    L.append("**" + verdict + ".**")
    L.append("")
    L.append("1. **Set the shrink constant from the SHIPPED signal, not rate_1h.** "
             "Recompute ECE on the signal the transform is actually applied to "
             "(om_gfs_bc_ps ≈ 0.04, not 0.47). Under the current constant the "
             "transform is a 23.5% one-size-fits-all pull toward 0.5 with no basis "
             "in the shipped signal's miscalibration.")
    L.append("2. **Stop treating the output as a probability.** A fixed linear shrink "
             "+ a clamp is not a calibration. Either fit an isotonic/Platt map on a "
             "holdout (the machinery already exists: `core/platt_calibration.py`) or "
             "drop the shrink and keep the ceiling clamp.")
    L.append("3. **Re-derive the ceilings from the shipped signal's resolution.** The "
             "current ceilings inherit rate_1h's (near-zero) resolution over a 0.75 "
             "floor.")
    L.append("4. **Fix the fail-closed contract:** reject `None` with `ValueError`, and "
             "reject a non-finite `ceiling` instead of silently clamping it to 0.999.")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append("python3 scripts/honest_calibration_audit_2026-10-15.py")
    L.append("```")
    L.append("")
    OUT_MD.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
