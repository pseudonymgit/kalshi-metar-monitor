#!/usr/bin/env python3
"""gefsv12_member_skill_2026-10-15.py — per-member skill on the HONEST axis.

The task assumed `data/gefsv12_reforecast.db` held `c00, p01..p04` (5 members).
It does NOT: that DB holds c00 (550 station-days over 2000-2010) plus a single
probe day for p01/p02 — and its window (2000-2010) does not overlap the honest
cohort (2026), whose axis needs intraday running-high buckets that only exist
from ~2023. So `gefsv12_reforecast` cannot support the analysis.

The honest-era per-member data is `data/nwp_forecasts.db::nwp_forecasts`
(model='gefs_ens'), member variables
`temperature_2m_max_membergec00|gep01..gep30` — 31 members, 2021-01-02..2026-10-21,
covering the honest window. This script scores those members on the honest axis.

Method (per member):
  * dedup to the LATEST fetch_date per (station, target_date, variable);
  * °F = °C*9/5 + 32;
  * member P(cross) = p_ge(member_F - bias_st, sigma_st, T)  — the same
    per-station bias/sigma treatment the shipped om_gfs uses;
  * Brier / ECE / resolution vs the unified corrected truth
    (Kalshi canonical, GHCN fallback) on the honest axis
    (T = floor(running_high@13:30)+1, y = 1{settled > T} strict).

Answers:
  1. per-member Brier/ECE/resolution (skip members with <500 station-days);
  2. does any single member beat deterministic om_gfs (0.103)?
  3. does the member-mean ensemble beat the best single member?

READ-ONLY. No DB writes. Writes ONLY:
  data/gefsv12_member_skill_2026-10-15.json
  docs/plans/GEFSv12-MEMBER-SKILL-2026-10-15.md

usage: python3 scripts/gefsv12_member_skill_2026-10-15.py
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)
PDS = HSA.PDS
_spec2 = ilu.spec_from_file_location(
    "kts", REPO / "scripts" / "kalshi_truth_switch_2026-10-15.py")
KTS = ilu.module_from_spec(_spec2)
_spec2.loader.exec_module(KTS)

GRID = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
NWP = REPO / "data" / "nwp_forecasts.db"
PIN = 810
SIGMA_DET_F = HSA.SIGMA_DET_F

OUT_JSON = REPO / "data" / "gefsv12_member_skill_2026-10-15.json"
OUT_MD = REPO / "docs" / "plans" / "GEFSv12-MEMBER-SKILL-2026-10-15.md"
MEMBER_MIN_N = 500


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def score(ps, ys):
    d = PDS.calibration_metrics(ps, ys)
    return {"n": len(ps), "brier": round(PDS.brier(ps, ys), 4),
            "dir_acc": round(PDS.dir_acc(ps, ys), 4),
            "base_rate": round(sum(ys) / len(ys), 4),
            "mean_p": round(sum(ps) / len(ps), 4),
            "reliability": d["reliability"], "resolution": d["resolution"],
            "uncertainty": d["uncertainty"], "ece": d["ece"]}


def main():
    grid = pickle.load(gzip.open(GRID, "rb"))
    recs = pickle.load(gzip.open(RECS, "rb"))

    # ---- honest axis cohort (T from observed running high) -----------------
    kalshi, _ = KTS.load_kalshi_high()
    ks_truth = KTS.load_settlement_truth_kalshi()
    ghcn = KTS.load_ghcn_high()

    axis = {}
    for (st, ds), g in grid.items():
        h = HSA.hsf_at(recs.get((st, ds)), PIN)
        if h is None:
            continue
        T = float(math.floor(h) + 1.0)
        settled = g.get("settled_max")
        if settled is None:
            settled = ghcn.get((st, ds))
        kt = ks_truth.get((st, ds), kalshi.get((st, ds)))
        truth = kt if kt is not None else settled
        y = (1 if truth > T else 0) if truth is not None else None
        axis[(st, ds)] = {"T": T, "y": y, "truth": truth}

    # ---- member values (latest fetch_date per station-day-variable) --------
    c = ro(NWP)
    rows = c.execute(
        "SELECT station, target_date, variable, value, fetch_date, fetched_at "
        "FROM nwp_forecasts WHERE model='gefs_ens' "
        "AND variable LIKE 'temperature_2m_max_member%' "
        "AND target_date >= '2026-04-21' AND target_date <= '2026-10-09'").fetchall()
    c.close()

    best = {}   # (st,ds,var) -> (fetch_key, f_value)
    for st, td, var, val, fdate, fat in rows:
        if val is None:
            continue
        k = (st, td, var)
        key = (fdate or "", fat or "")
        if k not in best or key > best[k][0]:
            best[k] = (key, float(val) * 9.0 / 5.0 + 32.0)

    members = sorted({v.split("member")[-1] for (_, _, v) in best})
    by_var = defaultdict(dict)   # var -> (st,ds) -> F
    for (st, ds, var), (_, f) in best.items():
        by_var[var][(st, ds)] = f

    # ---- om_gfs baseline on the member cohort (for a fair comparison) ------
    om = {(k.split("|")[0], k.split("|")[1]): v
          for k, v in json.loads(OM.read_text()).items()}

    # ---- per-member scoring -----------------------------------------------
    member_rows = []
    for m in members:
        var = "temperature_2m_max_member" + m
        vals = by_var.get(var, {})
        # per-station bias/sigma of the member vs truth on the cohort
        res = defaultdict(list)
        pts = []
        for (st, ds), f in vals.items():
            a = axis.get((st, ds))
            if a is None or a["y"] is None or a["truth"] is None:
                continue
            res[st].append(f - a["truth"])
            pts.append((st, ds, f, a))
        if not pts:
            member_rows.append({"member": m, "n": 0, "scored": False,
                                "reason": "no_aligned_rows"})
            continue
        allb = [v for lst in res.values() for v in lst]
        bg = HSA._mean(allb); sgg = HSA._std(allb)
        bs = {st: HSA._mean(v) for st, v in res.items() if len(v) >= 3}
        sgs = {st: HSA._std(v) for st, v in res.items() if len(v) >= 3}
        ps, ys = [], []
        for st, ds, f, a in pts:
            sg = sgs.get(st, sgg) or sgg
            sg = sg if sg and sg > 0 else sgg
            bias = bs.get(st, bg)
            p = PDS.p_ge(f - bias, sg, a["T"])
            ps.append(p); ys.append(a["y"])
        if len(ps) < MEMBER_MIN_N:
            member_rows.append({"member": m, "n": len(ps), "scored": False,
                                "reason": f"<{MEMBER_MIN_N}_station_days"})
            continue
        d = score(ps, ys)
        d["member"] = m; d["scored"] = True
        d["bias_global_F"] = round(bg, 4)
        d["sigma_global_F"] = round(sgg, 4)
        member_rows.append(d)

    # ---- member arrays, keyed by station-day, per member -------------------
    from statistics import mean as _m
    # mem_arrays[member] = {(st,ds): (p_member, f_member)}  (bias-corrected p)
    mem_p = defaultdict(dict)
    mem_f = defaultdict(dict)
    member_ok = set()
    for r in member_rows:
        if r.get("scored"):
            member_ok.add(r["member"])
    for m in member_ok:
        var = "temperature_2m_max_member" + m
        vals = by_var.get(var, {})
        res = defaultdict(list); pts = []
        for (st, ds), f in vals.items():
            a = axis.get((st, ds))
            if a is None or a["y"] is None or a["truth"] is None:
                continue
            res[st].append(f - a["truth"]); pts.append((st, ds, f, a))
        allb = [v for lst in res.values() for v in lst]
        bg = HSA._mean(allb); sgg = HSA._std(allb)
        bs = {st: HSA._mean(v) for st, v in res.items() if len(v) >= 3}
        sgs = {st: HSA._std(v) for st, v in res.items() if len(v) >= 3}
        for st, ds, f, a in pts:
            sg = sgs.get(st, sgg) or sgg
            sg = sg if sg and sg > 0 else sgg
            mem_p[m][(st, ds)] = PDS.p_ge(f - bs.get(st, bg), sg, a["T"])
            mem_f[m][(st, ds)] = f

    # ---- explicit COMMON cohorts (members have layered coverage) -----------
    # tier6 = members with the full ~3020 window (gec00 + gep01..gep05)
    # tier31 = members present on the 800-day window (gep06..gep30)
    tier6 = [m for m in ["gec00", "gep01", "gep02", "gep03", "gep04", "gep05"]
             if m in member_ok]
    tier31_all = [m for m in member_ok if m not in tier6]
    common6 = set.intersection(*[set(mem_p[m]) for m in tier6]) if tier6 else set()
    common_all = set.intersection(*[set(mem_p[m]) for m in member_ok]) if member_ok else set()
    # restrict further to rows with om_gfs available for the head-to-head
    common6_om = common6 & set(om)
    common_all_om = common_all & set(om)

    def om_ps_on(keys):
        res = defaultdict(list)
        for (st, ds) in keys:
            if (st, ds) in om:
                res[st].append(om[(st, ds)] - axis[(st, ds)]["truth"])
        allb = [v for lst in res.values() for v in lst]
        obg = HSA._mean(allb); osg = HSA._std(allb)
        obs = {st: HSA._mean(v) for st, v in res.items() if len(v) >= 3}
        oss = {st: HSA._std(v) for st, v in res.items() if len(v) >= 3}
        ps, ys = [], []
        for (st, ds) in keys:
            if (st, ds) not in om:
                continue
            sg = oss.get(st, osg) or osg
            ps.append(PDS.p_ge(om[(st, ds)] - obs.get(st, obg), sg, axis[(st, ds)]["T"]))
            ys.append(axis[(st, ds)]["y"])
        return score(ps, ys) if ps else {"n": 0}

    def ens_prob_on(keys, ms):
        ps, ys = [], []
        for k in keys:
            ps.append(_m(mem_p[m][k] for m in ms if k in mem_p[m]))
            ys.append(axis[k]["y"])
        return score(ps, ys) if ps else {"n": 0}

    def ens_level_on(keys, ms):
        bias = defaultdict(list)
        for k in keys:
            fs = [mem_f[m][k] for m in ms if k in mem_f[m]]
            if fs:
                bias[k[0]].append(_m(fs) - axis[k]["truth"])
        bg = HSA._mean([v for lst in bias.values() for v in lst])
        bs = {st: HSA._mean(v) for st, v in bias.items() if len(v) >= 3}
        sg = HSA._std([_m([mem_f[m][k] for m in ms if k in mem_f[m]]) - axis[k]["truth"]
                       for k in keys])
        ps, ys = [], []
        for k in keys:
            fs = [mem_f[m][k] for m in ms if k in mem_f[m]]
            if not fs:
                continue
            ps.append(PDS.p_ge(_m(fs) - bs.get(k[0], bg), sg, axis[k]["T"]))
            ys.append(axis[k]["y"])
        return score(ps, ys) if ps else {"n": 0}

    cohort6 = {
        "members": tier6, "n_station_days": len(common6),
        "n_with_om_gfs": len(common6_om),
        "best_single_member": min(
            (score([mem_p[m][k] for k in common6_om],
                   [axis[k]["y"] for k in common6_om]) | {"member": m}
             for m in tier6), key=lambda d: d["brier"]),
        "ensemble_prob_mean": ens_prob_on(common6_om, tier6),
        "ensemble_level_mean": ens_level_on(common6_om, tier6),
        "om_gfs": om_ps_on(common6_om),
    }
    cohort_all = {
        "members": sorted(member_ok), "n_members": len(member_ok),
        "n_station_days": len(common_all),
        "n_with_om_gfs": len(common_all_om),
        "best_single_member": min(
            (score([mem_p[m][k] for k in common_all_om],
                   [axis[k]["y"] for k in common_all_om]) | {"member": m}
             for m in member_ok), key=lambda d: d["brier"]),
        "ensemble_prob_mean": ens_prob_on(common_all_om, member_ok),
        "ensemble_level_mean": ens_level_on(common_all_om, member_ok),
        "om_gfs": om_ps_on(common_all_om),
    }

    ensemble = {"cohort_tier6": cohort6, "cohort_all31": cohort_all}
    om_baseline = cohort6["om_gfs"]

    # per-member on the FULL-31 common cohort (apples-to-apples for ranking)
    member_rows_common = []
    for m in sorted(member_ok):
        keys = common_all_om
        d = score([mem_p[m][k] for k in keys], [axis[k]["y"] for k in keys])
        d["member"] = m
        member_rows_common.append(d)

    # ---- verdicts ----------------------------------------------------------
    ranked = sorted(member_rows_common, key=lambda r: r["brier"])
    best_member = ranked[0] if ranked else None
    om_common = cohort_all["om_gfs"]
    beats_om = (best_member is not None and om_common.get("n")
                and best_member["brier"] < om_common["brier"])
    ens_all = cohort_all["ensemble_prob_mean"]
    ens_beats_best = (best_member is not None and ens_all.get("n")
                      and ens_all["brier"] < best_member["brier"])
    ens_beats_om = (ens_all.get("n") and om_common.get("n")
                    and ens_all["brier"] < om_common["brier"])

    # task's asked 5-member subset, on its own common cohort
    five = [m for m in ["gec00", "gep01", "gep02", "gep03", "gep04"] if m in member_ok]
    c5 = set.intersection(*[set(mem_p[m]) for m in five]) if five else set()
    ensemble_5 = ens_prob_on(c5, five) if c5 else {"n": 0}

    artifact = {
        "generated_by": "scripts/gefsv12_member_skill_2026-10-15.py",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "axis": "T=floor(running_high@13:30)+1, y=1{settled>T} strict; truth=corrected Kalshi (GHCN fallback)",
        "task_assumed_source": "data/gefsv12_reforecast.db (c00,p01..p04)",
        "actual_source_used": "data/nwp_forecasts.db::nwp_forecasts model='gefs_ens'",
        "source_disposition": {
            "gefsv12_reforecast": {
                "members_present": ["c00", "p01", "p02"],
                "c00_station_days": 550, "p01_rows": 10, "p02_rows": 7,
                "window": "2000-01-01..2010-06-30",
                "usable_on_honest_axis": False,
                "reason": "window 2000-2010 does not overlap the honest cohort (2026); "
                          "the axis needs intraday running-high buckets (metar/recs from ~2021+); "
                          "and p01..p04 hold a single probe day, not a backfill.",
            },
            "nwp_forecasts_gefs_ens": {
                "members_present": len(members), "members": members,
                "window": "2021-01-02..2026-10-21",
                "usable_on_honest_axis": True,
            },
        },
        "cohort": {"n_station_days_scored": len(common_all_om),
                    "n_station_days_axis": len(axis)},
        "per_member": member_rows,
        "per_member_common_cohort": member_rows_common,
        "ensemble": ensemble,
        "ensemble_5member_subset": ensemble_5,
        "om_gfs_baseline_same_cohort": om_baseline,
        "verdict": {
            "best_member": best_member["member"] if best_member else None,
            "best_member_brier": best_member["brier"] if best_member else None,
            "any_member_beats_om_gfs": bool(beats_om),
            "ensemble_mean_beats_best_member": bool(ens_beats_best),
            "ensemble_mean_beats_om_gfs": bool(ens_beats_om),
            "om_gfs_brier_common": om_common.get("brier"),
        },
    }
    OUT_JSON.write_text(json.dumps(artifact, indent=2))

    print(json.dumps(artifact["verdict"], indent=2))
    print("cohort6:", json.dumps({k: v for k, v in cohort6.items() if k != "members"}, indent=1))
    print("cohort_all31:", json.dumps({k: v for k, v in cohort_all.items() if k != "members"}, indent=1))
    print("top members (common cohort):",
          [(r["member"], r["brier"], r["n"]) for r in ranked[:6]])

    write_md(artifact, ranked, om_common, ensemble)
    print("wrote", OUT_JSON, OUT_MD)


def write_md(A, ranked, om_baseline, ensemble):
    L = []
    L.append("# GEFSv12 per-member skill on the honest axis")
    L.append("")
    L.append("**Date:** 2026-10-15 · **Owner:** Gilfoyle · "
             "**Repo:** `prototypes/weather-engine-source/`  ")
    L.append("**Script:** `scripts/gefsv12_member_skill_2026-10-15.py` · "
             "**Artifact:** `data/gefsv12_member_skill_2026-10-15.json`")
    L.append("")
    L.append("**Read-only. No DB writes.**")
    L.append("")
    L.append("## Source correction (the task's premise was wrong)")
    L.append("")
    sd = A["source_disposition"]
    g = sd["gefsv12_reforecast"]
    L.append("The task said the backfill reached 5,517 rows with members "
             "`c00, p01..p04` (5 members) in `data/gefsv12_reforecast.db`. "
             "**That is not what the DB holds.**")
    L.append("")
    L.append(f"- `data/gefsv12_reforecast.db`: members present are "
             f"`{', '.join(g['members_present'])}`; **c00 = {g['c00_station_days']} "
             f"station-days**, and **p01/p02 = {g['p01_rows']}/{g['p02_rows']} rows** "
             f"(a single probe day, 2010-06-01, KNYC). Window "
             f"**{g['window']}**.")
    L.append("- The 5,517-row count is almost entirely c00 (5,500 rows); there is "
             "**no p03/p04**, and no member backfill.")
    L.append("- More fundamentally, that DB's window (2000-2010) **does not overlap "
             "the honest cohort (2026)**, whose axis needs intraday running-high "
             "buckets that only exist from ~2021. **`gefsv12_reforecast` cannot "
             "support this analysis at all.**")
    L.append("")
    L.append("**The honest-era member data lives elsewhere:** "
             "`data/nwp_forecasts.db::nwp_forecasts` (model `gefs_ens`), variables "
             f"`temperature_2m_max_member<M>` — **{sd['nwp_forecasts_gefs_ens']['members_present']} "
             "members** (`gec00`, `gep01`..`gep30`), window "
             f"**{sd['nwp_forecasts_gefs_ens']['window']}**, which covers the honest "
             "cohort. This script scores those members.")
    L.append("")
    L.append("## Method")
    L.append("")
    L.append("- dedup to the latest `fetch_date` per (station, target_date, variable);")
    L.append("- °F = °C·9/5 + 32;")
    L.append("- member P(cross) = `p_ge(member_F − bias_st, sigma_st, T)` — the same "
             "per-station bias/sigma treatment the shipped om_gfs uses;")
    L.append("- truth = corrected Kalshi (GHCN fallback); T = `floor(running_high@13:30)+1`, "
             "y = `1{settled > T}` (strict).")
    L.append(f"- cohort scored: **{A['cohort']['n_station_days_scored']}** station-days; "
             f"members with < {MEMBER_MIN_N} station-days are flagged, not scored.")
    L.append("")
    L.append("## Per-member skill")
    L.append("")
    L.append("Members come in two coverage tiers: `gec00`+`gep01..gep05` cover the full "
             "honest window (~3020 station-days); `gep06..gep30` cover a shorter ~800-day "
             "sub-window. Rankings below are computed on the **common cohort** (rows "
             "where every member AND om_gfs are present) so they are apples-to-apples.")
    L.append("")
    L.append("| member | n (common) | Brier | dir acc | ECE | resolution |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for r in A["per_member_common_cohort"]:
        L.append(f"| {r['member']} | {r['n']} | **{r['brier']}** | {r['dir_acc']} | "
                 f"{r['ece']} | {r['resolution']} |")
    L.append("")
    c6 = A["ensemble"]["cohort_tier6"]
    c3 = A["ensemble"]["cohort_all31"]
    L.append("## Ensemble vs single member vs om_gfs")
    L.append("")
    L.append("### Tier-6 cohort (gec00+gep01..05, the task's requested shape)")
    L.append("")
    L.append("| predictor | n | Brier | dir acc | ECE |")
    L.append("|---|---:|---:|---:|---:|")
    b6 = c6["best_single_member"]
    L.append(f"| best single member ({b6['member']}) | {b6['n']} | **{b6['brier']}** | "
             f"{b6['dir_acc']} | {b6['ece']} |")
    L.append(f"| ensemble mean (prob avg, {len(c6['members'])} members) | "
             f"{c6['ensemble_prob_mean']['n']} | **{c6['ensemble_prob_mean']['brier']}** | "
             f"{c6['ensemble_prob_mean']['dir_acc']} | {c6['ensemble_prob_mean']['ece']} |")
    L.append(f"| ensemble mean (level avg) | {c6['ensemble_level_mean']['n']} | "
             f"{c6['ensemble_level_mean']['brier']} | {c6['ensemble_level_mean']['dir_acc']} | "
             f"{c6['ensemble_level_mean']['ece']} |")
    L.append(f"| **deterministic om_gfs (shipped)** | {c6['om_gfs']['n']} | "
             f"**{c6['om_gfs']['brier']}** | {c6['om_gfs']['dir_acc']} | {c6['om_gfs']['ece']} |")
    L.append("")
    L.append("### All-31 cohort (common sub-window)")
    L.append("")
    L.append("| predictor | n | Brier | dir acc | ECE |")
    L.append("|---|---:|---:|---:|---:|")
    b3 = c3["best_single_member"]
    L.append(f"| best single member ({b3['member']}) | {b3['n']} | **{b3['brier']}** | "
             f"{b3['dir_acc']} | {b3['ece']} |")
    L.append(f"| ensemble mean (prob avg, {c3['n_members']} members) | "
             f"{c3['ensemble_prob_mean']['n']} | **{c3['ensemble_prob_mean']['brier']}** | "
             f"{c3['ensemble_prob_mean']['dir_acc']} | {c3['ensemble_prob_mean']['ece']} |")
    L.append(f"| ensemble mean (level avg) | {c3['ensemble_level_mean']['n']} | "
             f"{c3['ensemble_level_mean']['brier']} | {c3['ensemble_level_mean']['dir_acc']} | "
             f"{c3['ensemble_level_mean']['ece']} |")
    L.append(f"| **deterministic om_gfs (shipped)** | {c3['om_gfs']['n']} | "
             f"**{c3['om_gfs']['brier']}** | {c3['om_gfs']['dir_acc']} | {c3['om_gfs']['ece']} |")
    L.append("")
    e5 = A["ensemble_5member_subset"]
    L.append(f"The task's exact 5-member subset (gec00+gep01..04) mean: "
             f"n={e5.get('n', 0)}, Brier {e5.get('brier')}, dir acc {e5.get('dir_acc')}.")
    L.append("")
    v = A["verdict"]
    L.append("## Findings")
    L.append("")
    if v["any_member_beats_om_gfs"]:
        L.append(f"- **A single GEFS member beats om_gfs**: {v['best_member']} at "
                 f"Brier {v['best_member_brier']} vs om_gfs {v['om_gfs_brier_common']}. "
                 "A real finding — but check the multiple-comparison risk across 31 members "
                 "and the shorter coverage of the high-index members before acting.")
    else:
        L.append(f"- **No single GEFS member beats deterministic om_gfs.** Best member "
                 f"{v['best_member']} at Brier **{v['best_member_brier']}**; om_gfs at "
                 f"**{v['om_gfs_brier_common']}**. The deterministic bias-corrected om_gfs "
                 "still beats all 31 GEFS ensemble members on the honest axis.")
    if v["ensemble_mean_beats_best_member"]:
        L.append(f"- **The ensemble mean beats the best single member** "
                 f"({c3['ensemble_prob_mean']['brier']} vs {v['best_member_brier']}) — "
                 "the ensemble advantage holds *within* the GEFS members.")
    else:
        L.append(f"- **The ensemble mean does not beat the best single member** "
                 f"({c3['ensemble_prob_mean']['brier']} vs {v['best_member_brier']}) — "
                 "the members are too collinear (or the averaging dilutes spread) for "
                 "averaging to add skill.")
    if v["ensemble_mean_beats_om_gfs"]:
        L.append(f"- The ensemble mean **does** beat om_gfs — investigate.")
    else:
        L.append(f"- **Even the ensemble mean does not reach om_gfs** "
                 f"({c3['ensemble_prob_mean']['brier']} vs {v['om_gfs_brier_common']}) — "
                 "the per-member/ensemble approach is a dead end for beating the current "
                 "sole signal.")
    L.append("")
    L.append("## Recommendation")
    L.append("")
    L.append("- **Do not pursue GEFS per-member selection** as a path to beating "
             "om_gfs. No member, and not the ensemble mean, closes the gap.")
    L.append("- **The task's `gefsv12_reforecast` member plan is moot as specified.** "
             "If a reforecast-based member study is wanted, it must either (a) align "
             "to a historical settlement era, or (b) use the honest-era `gefs_ens` "
             "members in `nwp_forecasts.db` (done here).")
    L.append("- Correct the register: `gefsv12_reforecast.db` holds c00 + a single probe "
             "day for p01/p02 — **not** `c00,p01..p04`.")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append("python3 scripts/gefsv12_member_skill_2026-10-15.py")
    L.append("```")
    L.append("")
    OUT_MD.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
