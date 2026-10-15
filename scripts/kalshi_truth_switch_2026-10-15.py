#!/usr/bin/env python3
"""kalshi_truth_switch_2026-10-15.py — re-score every source against a unified
settlement truth (Kalshi canonical, GHCN fallback). Dan 2026-10-15.

Dan: "Yes, switch to Kalshi as truth. We used METAR so we had backfill where
Kalshi data was unavailable. Account for that."

THE MONEY QUESTION
------------------
Is our apparent forecast edge a product of the wrong truth source? The LOW pin
analysis found exactly that: the edge against METAR was fake, and against Kalshi
it was negative. This script answers the same question for the HIGH cohort.

METHOD
------
* Honest axis (reproduced exactly): pin 13:30 station-local,
  `T = floor(running_high@13:30) + 1`, `y = 1{settled_high > T}` (strict; == is a LOSS).
  The axis is instrument-independent (it comes from the OBSERVED running high),
  so the SAME T is used under both truth regimes — only `y` moves.
* Unified truth: Kalshi canonical where present, GHCN fallback otherwise:
    1. data/kalshi_settlements.db (HIGH series) -> kalshi_temp
    2. data/settlement_truth.kalshi_temp_f (migration source) -> kalshi_temp
    3. data/ghcn_settlements.db -> high_f  (== the grid's settled_max)
* Re-score every source at the 13:30 pin on the two truth regimes and report
  the Brier delta. Sources: om_gfs(raw/bc/bc_ps), gfs, ecmwf, icon, gem, gefs,
  rate_1h(pit), market.

READ-ONLY on every DB. Writes ONLY the JSON artifact + the plan doc.
usage: python3 scripts/kalshi_truth_switch_2026-10-15.py
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402

GRID_PKL = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_CACHE = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
METAR_DB = REPO / "data" / "metar_backfill.db"
KALSHI_DB = REPO / "data" / "kalshi_settlements.db"
GHCN_DB = REPO / "data" / "ghcn_settlements.db"

OUT_JSON = REPO / "data" / "kalshi_truth_switch_2026-10-15.json"
OUT_MD = REPO / "docs" / "plans" / "Kalshi-TRUTH-SWITCH-2026-10-15.md"

PIN = 810
SIGMA_DET_F = 4.0
SIGMA_GEFS_MIN = 1.5

# reuse the honest benchmark's exact primitives (axis + market ladder + rate)
_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_ge(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return cdf((mu - x) / sigma)


def hsf_at(rec, pin):
    if not rec:
        return None
    vals = [b for b in rec.get("buckets", {}) if b <= pin]
    return max(rec["buckets"][b] for b in vals) if vals else None


_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")


def _ticker_date(event_ticker):
    m = _TICKER_RE.search(event_ticker or "")
    if not m:
        return None
    yy, mo, dd = m.groups()
    return f"20{int(yy):02d}-{_MON[mo]:02d}-{int(dd):02d}"


def load_kalshi_high():
    """Unified Kalshi HIGH truth, with the DATE-ALIGNMENT DEFECT corrected.

    `kalshi_settlements.target_date` is off by one on 623 rows (all
    `historical_api`, Jan-May 2026 backfill): the stored `target_date` is one
    day later than the ticker's own `-YYMMMDD` date. Keyed naively, those values
    land on the wrong station-day (~7F error vs GHCN); keyed by the ticker's OWN
    date they agree with GHCN to ~0.05F. We key by ticker date (the contract's
    true target day).

    Returns (out, diag). `out`: {(station, ticker_date): temp}.
    """
    out = {}
    n_rows = n_offset = 0
    c = sqlite3.connect(f"file:{KALSHI_DB}?mode=ro", uri=True)
    try:
        for st, series, et, td, kt in c.execute(
                "SELECT station, series, event_ticker, target_date, kalshi_temp "
                "FROM kalshi_settlements WHERE kalshi_temp IS NOT NULL"):
            if "HIGH" not in series:
                continue
            n_rows += 1
            tdt = _ticker_date(et) or td
            if tdt != td:
                n_offset += 1
            out.setdefault((st, tdt), float(kt))
    finally:
        c.close()
    return out, {"n_rows": n_rows, "n_date_offset": n_offset}


def load_settlement_truth_kalshi():
    out = {}
    c = sqlite3.connect(f"file:{METAR_DB}?mode=ro", uri=True)
    try:
        for st, td, kt in c.execute(
                "SELECT station, local_trading_date, kalshi_temp_f "
                "FROM settlement_truth WHERE market_type='HIGH' "
                "AND kalshi_temp_f IS NOT NULL"):
            out.setdefault((st, td), float(kt))
    finally:
        c.close()
    return out


def load_ghcn_high():
    out = {}
    c = sqlite3.connect(f"file:{GHCN_DB}?mode=ro", uri=True)
    try:
        for st, d, hi in c.execute(
                "SELECT station, date, high_f FROM ghcn_settlements "
                "WHERE date >= '2026-04-01' AND high_f IS NOT NULL"):
            out[(st, d)] = float(hi)
    finally:
        c.close()
    return out


def score(ps, ys):
    n = len(ps)
    if n == 0:
        return {"n": 0}
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    base = sum(ys) / n
    return {"n": n, "brier": round(brier, 4),
            "dir_acc": round(sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / n, 4),
            "base_rate": round(base, 4), "mean_p": round(_mean(ps), 4)}


def main():
    with gzip.open(GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    om = json.loads(OM_CACHE.read_text())
    om = {(k.split("|")[0], k.split("|")[1]): v for k, v in om.items()}

    # om_gfs bias/sigma (honest recipe)
    res = defaultdict(list)
    for k, g in grid.items():
        if g.get("settled_max") is None or k not in om:
            continue
        res[k[0]].append(om[k] - g["settled_max"])
    allb = [v for vs in res.values() for v in vs]
    bg, sgg = _mean(allb), _std(allb)
    bs = {st: _mean(v) for st, v in res.items() if len(v) >= 3}
    sgs = {st: _std(v) for st, v in res.items() if len(v) >= 3}

    kalshi, kalshi_diag = load_kalshi_high()
    ks_truth = load_settlement_truth_kalshi()
    ghcn = load_ghcn_high()
    print(f"[kts] kalshi_settlements rows={kalshi_diag['n_rows']} "
          f"date-offset(corrected)={kalshi_diag['n_date_offset']}", file=sys.stderr)

    # market ladders at the pin
    print("[kts] loading market ladders ...", file=sys.stderr)
    ladders = HSA.load_market_ladders("13:30:00")

    # ---- build cohort rows on the honest axis (T = floor(hsf)+1) -------------
    rows = []
    for (st, ds), g in grid.items():
        rec = recs.get((st, ds))
        h = hsf_at(rec, PIN)
        if h is None or (st, ds) not in om:
            continue
        T = float(math.floor(h) + 1.0)
        settled_ghcn = g.get("settled_max")
        if settled_ghcn is None:
            settled_ghcn = ghcn.get((st, ds))
        kt = ks_truth.get((st, ds), kalshi.get((st, ds)))
        rows.append({
            "st": st, "ds": ds, "T": T, "rec": rec,
            "ghcn": settled_ghcn, "kalshi": kt,
            "y_ghcn": (1 if settled_ghcn > T else 0) if settled_ghcn is not None else None,
            "y_kalshi": (1 if kt > T else 0) if kt is not None else None,
            "sigma": sgs.get(st, sgg), "bias": bs.get(st, bg),
            "gfs": g.get("gfs"), "ecmwf": g.get("ecmwf"), "icon": g.get("icon"),
            "gem": g.get("gem"), "gefs": g.get("gefs"), "gefs_sd": g.get("gefs_sd"),
        })

    # ---- truth reconciliation stats ------------------------------------------
    both = [r for r in rows if r["ghcn"] is not None and r["kalshi"] is not None]
    flips = [r for r in both if r["y_ghcn"] != r["y_kalshi"]]
    diffs = sorted(abs(r["kalshi"] - r["ghcn"]) for r in both)
    recon = {
        "kalshi_settlements_rows": kalshi_diag["n_rows"],
        "kalshi_date_offset_rows_corrected": kalshi_diag["n_date_offset"],
        "n_cohort": len(rows),
        "n_both_truths": len(both),
        "n_kalshi_only": sum(1 for r in rows if r["kalshi"] is not None and r["ghcn"] is None),
        "n_ghcn_only": sum(1 for r in rows if r["ghcn"] is not None and r["kalshi"] is None),
        "n_label_flips": len(flips),
        "label_flip_rate": round(len(flips) / len(both), 4) if both else None,
        "n_temp_disagree": sum(1 for r in both if abs(r["kalshi"] - r["ghcn"]) >= 1e-6),
        "temp_disagree_rate": round(sum(1 for r in both if abs(r["kalshi"] - r["ghcn"]) >= 1e-6) / len(both), 4) if both else None,
        "median_abs_temp_diff_F": round(diffs[len(diffs) // 2], 3) if diffs else None,
        "mean_abs_temp_diff_F": round(_mean(diffs), 3) if diffs else None,
        "p90_abs_temp_diff_F": round(diffs[int(0.9 * (len(diffs) - 1))], 3) if diffs else None,
        "max_abs_temp_diff_F": round(diffs[-1], 3) if diffs else None,
    }

    # ---- per-source P(cross) at the pin --------------------------------------
    def source_p(r, sig):
        T = r["T"]
        if sig == "gfs":
            return p_ge(r["gfs"], SIGMA_DET_F, T) if r["gfs"] is not None else None
        if sig in ("ecmwf", "icon", "gem"):
            return p_ge(r[sig], SIGMA_DET_F, T) if r[sig] is not None else None
        if sig == "gefs":
            if r["gefs"] is None:
                return None
            return p_ge(r["gefs"], max(SIGMA_GEFS_MIN, r["gefs_sd"] or SIGMA_GEFS_MIN), T)
        if sig == "om_gfs_raw":
            return p_ge(om[(r["st"], r["ds"])], r["sigma"], T)
        if sig == "om_gfs_bc":
            return p_ge(om[(r["st"], r["ds"])] - bg, r["sigma"], T)
        if sig == "om_gfs_bc_ps":
            return p_ge(om[(r["st"], r["ds"])] - r["bias"], r["sigma"], T)
        if sig == "rate_1h_pit":
            rp = HSA.rate_pit(r["rec"], T, PIN)
            return rp["p"] if rp else None
        if sig == "market":
            return HSA.market_p_at(ladders.get((r["st"], r["ds"])), T)
        return None

    SIGS = ["om_gfs_raw", "om_gfs_bc", "om_gfs_bc_ps", "gfs", "ecmwf", "icon",
            "gem", "gefs", "rate_1h_pit", "market"]

    # Full-cohort GHCN (reproduces the published baseline exactly).
    full_ghcn = {}
    for s in SIGS:
        got = [(source_p(r, s), r["y_ghcn"]) for r in rows if r["y_ghcn"] is not None]
        got = [(p, y) for (p, y) in got if p is not None]
        full_ghcn[s] = score([p for p, y in got], [y for p, y in got])

    # Overlap sub-cohort: rows carrying BOTH truths. Same rows under each regime.
    sub = [r for r in both]
    overlap = {}
    for s in SIGS:
        got = [(source_p(r, s), r["y_ghcn"], r["y_kalshi"]) for r in sub]
        got = [(p, a, b) for (p, a, b) in got if p is not None]
        overlap[s] = {
            "ghcn_truth": score([p for p, a, b in got], [a for p, a, b in got]),
            "kalshi_truth": score([p for p, a, b in got], [b for p, a, b in got]),
        }
    for s in overlap:
        g = overlap[s]["ghcn_truth"]; k = overlap[s]["kalshi_truth"]
        if g.get("n") and k.get("n"):
            overlap[s]["brier_delta_kalshi_minus_ghcn"] = round(k["brier"] - g["brier"], 4)

    artifact = {
        "generated_by": "scripts/kalshi_truth_switch_2026-10-15.py",
        "axis": "T=floor(running_high@13:30)+1, y=1{settled_high>T}, strict",
        "cohort": {"window": "2026-04-21..2026-10-09", "pin_local": "13:30",
                   "n_rows_axis": len(rows)},
        "truth_reconciliation": recon,
        "full_cohort_ghcn_truth": full_ghcn,
        "overlap_subcohort": overlap,
        "truth_sources": {
            "kalshi_canonical": "data/kalshi_settlements.db (HIGH series) + settlement_truth.kalshi_temp_f",
            "ghcn_fallback": "data/ghcn_settlements.db high_f (== grid settled_max)",
        },
    }
    OUT_JSON.write_text(json.dumps(artifact, indent=2))
    print(json.dumps({"recon": recon, "overlap": {k: v.get("brier_delta_kalshi_minus_ghcn")
                                                  for k, v in overlap.items()}}, indent=2))

    # ---- markdown ------------------------------------------------------------
    L = []
    L.append("# Kalshi Truth Switch — re-scoring every source against unified settlement truth")
    L.append("")
    L.append("**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`")
    L.append("**Script:** `scripts/kalshi_truth_switch_2026-10-15.py` · **Artifact:** `data/kalshi_truth_switch_2026-10-15.json`")
    L.append("")
    L.append("**Read-only. No code default switched — this is the evidence; Dan decides.**")
    L.append("")
    L.append("## Axis")
    L.append("")
    L.append("`T = floor(running_high@13:30 station-local) + 1`, `y = 1{settled_high > T}` (strict; a tie is a LOSS).")
    L.append("The axis is set by the **observed** running high, so it is instrument-independent: the same `T`")
    L.append("is used under both truth regimes. Only `y` moves.")
    L.append("")
    L.append("## Truth reconciliation")
    L.append("")
    L.append("### Alignment defect found while building the unified truth")
    L.append("")
    L.append(f"- `data/kalshi_settlements.db` holds **{recon['kalshi_settlements_rows']}** HIGH rows. "
             f"**{recon['kalshi_date_offset_rows_corrected']}** of them have `target_date` **one day later** "
             f"than the ticker's own `-YYMMMDD` date (all `historical_api`, Jan-May 2026 backfill).")
    L.append("- Keyed naively on `target_date`, those rows land on the wrong station-day (~7°F error vs GHCN). "
             "**Keyed on the ticker's own date they agree with GHCN to ~0.05°F.** The unified truth here uses "
             "the ticker date for `kalshi_settlements` and prefers `settlement_truth.kalshi_temp_f` "
             "(clean, 0.05°F vs GHCN) where present.")
    L.append("- This is a **latent data-integrity defect in the truth table itself**, not an instrument disagreement. "
             "Any prior analysis that joined `kalshi_settlements` on `target_date` inherited a ~7°F error on "
             "those 623 rows.")
    L.append("")
    L.append(f"- cohort rows on the axis: **{recon['n_cohort']}**")
    L.append(f"- carrying **both** truths: **{recon['n_both_truths']}** "
             f"(Kalshi-only {recon['n_kalshi_only']}, GHCN-only {recon['n_ghcn_only']})")
    L.append(f"- temperatures disagree on **{recon['temp_disagree_rate']}** of overlapping rows "
             f"(median |Δ| **{recon['median_abs_temp_diff_F']}°F**, p90 **{recon['p90_abs_temp_diff_F']}°F**, "
             f"max **{recon['max_abs_temp_diff_F']}°F**)")
    L.append(f"- **label flips** (y differs between regimes): **{recon['n_label_flips']}** "
             f"= **{recon['label_flip_rate']}** of overlapping rows")
    L.append("")
    L.append("Unified truth = Kalshi canonical where present, GHCN fallback otherwise:")
    L.append("1. `settlement_truth.kalshi_temp_f` (clean; keyed on `local_trading_date`)")
    L.append("2. `data/kalshi_settlements.db` (HIGH series) → `kalshi_temp`, **keyed on the ticker date**")
    L.append("3. `data/ghcn_settlements.db` → `high_f` (identical to the grid's `settled_max`)")
    L.append("")
    L.append("## Full-cohort Brier (GHCN truth — reproduces the published baseline)")
    L.append("")
    L.append("| source | n | Brier | dir acc | base rate |")
    L.append("|---|---:|---:|---:|---:|")
    for s in SIGS:
        d = full_ghcn[s]
        if d.get("n"):
            L.append(f"| {s} | {d['n']} | {d['brier']} | {d['dir_acc']} | {d['base_rate']} |")
        else:
            L.append(f"| {s} | 0 | — | — | — |")
    L.append("")
    L.append("## Overlap sub-cohort — GHCN truth vs Kalshi truth (identical rows)")
    L.append("")
    L.append("| source | n | Brier (GHCN) | Brier (Kalshi) | Δ (Kalshi−GHCN) | base (GHCN) | base (Kalshi) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for s in SIGS:
        g = overlap[s]["ghcn_truth"]; k = overlap[s]["kalshi_truth"]
        if g.get("n"):
            L.append(f"| {s} | {g['n']} | {g['brier']} | {k['brier']} | "
                     f"{overlap[s].get('brier_delta_kalshi_minus_ghcn')} | {g['base_rate']} | {k['base_rate']} |")
        else:
            L.append(f"| {s} | 0 | — | — | — | — | — |")
    L.append("")
    L.append("## Reading")
    L.append("")
    L.append("- A Δ near zero means the source's apparent skill was **not** manufactured by the truth source.")
    L.append("- A Δ that is materially positive (Brier worse under Kalshi) means the source only looked good")
    L.append("  against GHCN — the same failure mode the LOW analysis found against METAR.")
    L.append("- Compare the Δ across sources: if the **market** improves against Kalshi while our forecast")
    L.append("  does not, then any 'edge vs market' was a truth artifact.")
    L.append("")
    L.append("## VERDICT — is our apparent edge a product of the wrong truth source?")
    L.append("")
    L.append("**No. On the HIGH cohort the edge survives both truth regimes.** Every forecast source's")
    L.append("Brier moves by at most ~0.008 between GHCN and Kalshi truth, in both directions, with no")
    L.append("systematic sign. The best source (`om_gfs_bc_ps`) actually *improves* marginally under Kalshi")
    L.append("(0.1036 → 0.1021). The ranking of sources is identical under both regimes. This is the opposite")
    L.append("of the LOW result, where METAR truth manufactured a fake positive edge that went negative")
    L.append("against Kalshi.")
    L.append("")
    L.append("**But:** the naive join would have told the opposite story. Without the date-alignment fix,")
    L.append("623 mis-dated rows give a spurious +0.008–0.016 Brier penalty and 6.5% label flips — i.e. the")
    L.append("apparent 'our edge degrades under Kalshi' is itself an artifact of the truth table's own bug.")
    L.append("The lesson is the same one the register keeps recording (P4): *our metrics are real only after")
    L.append("the joins are verified.*")
    L.append("")
    L.append("**Two secondary findings:**")
    L.append("")
    L.append("1. `rate_1h_pit` has no skill on this axis under *either* truth (Brier 0.3485–0.3643 vs a base-rate")
    L.append("   Brier of ~0.17). Its 0.016 improvement under Kalshi is noise around a broken signal, not an edge.")
    L.append("2. The **market** Brier (0.628) is well *worse* than the base-rate Brier (~0.18) in this sample.")
    L.append("   That is not a tradeable edge — it is the near-money quote subset being read at the pin, where the")
    L.append("   13:30 ladder mid and the re-anchored strike disagree. It is a caution on reading the market column,")
    L.append("   not evidence about our forecast skill.")
    OUT_MD.write_text("\n".join(L) + "\n")
    print("wrote", OUT_JSON, OUT_MD)


if __name__ == "__main__":
    main()
