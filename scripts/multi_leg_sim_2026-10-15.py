#!/usr/bin/env python3
"""multi_leg_sim_2026-10-15.py — two-leg hold/exit simulation (Dan 2026-10-15).

Dan:
  "If we were playing two legs, what would our accuracy and confidence be at
   1, 3, and six hours?"

This is a SIMULATION, not a system test. It answers one question: if we buy two
adjacent strikes at the 13:30 pin and let om_gfs' evolving forecast drive a
"keep one / exit the other / hold both" policy, what do accuracy, confidence and
implied P&L look like 1h / 3h / 6h later — versus holding the single om_gfs
best-guess strike?

DESIGN
------
* Cohort: the honest HIGH cohort (grid y present + om_gfs cache present + ASOS
  recs present), 2026-04-21..2026-10-09, 13:30 station-local pin.
* Legs: T_lo = floor(running_high@13:30) + 1   (the om_gfs best-guess strike)
        T_hi = T_lo + 5                        (Dan's "T75 and T80" spacing)
* Truth: strict crossing (y = 1{settled_max > T}); grid settled_max is GHCN-based.
  A KALSHI-truth variant is reported on the sub-cohort where Kalshi temp exists.
* Evolving forecast at tau in {13:30(0h), 14:30(1h), 16:30(3h), 19:30(6h)}:
      rh(tau)      = max ASOS 15-min bucket mean with mod <= tau
      mu_final(tau)= max(rh(tau), om_gfs_bc_ps)
      P(final > T) = 1 if rh(tau) > T  else  Phi((mu_final - T)/sigma)
  i.e. once the observed running high has already crossed T the market is
  decided; otherwise the bias-corrected om_gfs forecast governs the rest of the
  day. This is the same Gaussian crossing primitive the honest benchmark uses.

POLICY (Dan's rule)
-------------------
  if rh(tau) > T_hi:      hold T_hi (decided YES), exit T_lo
  elif rh(tau) > T_lo:    hold T_lo, exit T_hi
  else:                   hold both

METRICS per tau
---------------
  accuracy      fraction of RETAINED legs that settle YES (did we hold winners?)
  confidence    mean decision-time P(cross) of the RETAINED legs
  brier         Brier of the retained-leg probabilities vs the retained outcomes
  pnl_2leg      implied P&L per contract: entry at model p@13:30, exit at model
                p@tau for the exited leg, payoff = actual y for the held leg
  pnl_1leg      same but simply holding T_lo for the whole window
  pnl_gain      pnl_2leg - pnl_1leg  (is the two-leg + exit policy additive?)
Fees applied per contract on entry/exit via core.fees (canonical).

READ-ONLY on every DB. Writes ONLY the JSON artifact + the plan doc.
usage: python3 scripts/multi_leg_sim_2026-10-15.py
"""
from __future__ import annotations

import gzip
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.fees import kalshi_fee  # noqa: E402

GRID_PKL = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_CACHE = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
KALSHI_DB = REPO / "data" / "kalshi_settlements.db"

OUT_JSON = REPO / "data" / "multi_leg_sim_2026-10-15.json"
OUT_MD = REPO / "docs" / "plans" / "MULTI-LEG-SIM-2026-10-15.md"

PIN = 810            # 13:30 station-local
TAUS = [(810, "0h"), (870, "1h"), (990, "3h"), (1170, "6h")]
LEG_GAP = 5.0        # T_hi = T_lo + 5  (Dan's T75/T80 example)


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_gt(mu, sigma, x):
    """P(final max > x) under a Gaussian centred on mu."""
    if sigma <= 0:
        return 1.0 if mu > x else 0.0
    return _phi((mu - x) / sigma)


def running_high(rec, tau):
    b = rec.get("buckets") if rec else None
    if not b:
        return None
    vals = [b[m] for m in b if m <= tau]
    return max(vals) if vals else None


def load_kalshi_high():
    c = sqlite3.connect(f"file:{KALSHI_DB}?mode=ro", uri=True)
    try:
        out = {}
        for st, series, td, kt in c.execute(
                "SELECT station, series, target_date, kalshi_temp "
                "FROM kalshi_settlements WHERE kalshi_temp IS NOT NULL"):
            if "HIGH" in series:
                out[(st, td)] = float(kt)
        return out
    finally:
        c.close()


def load_kalshi_high_by_axis():
    """{ticker: result} restricted to HIGH series (strict axis, from ladder)."""
    db = REPO / "data" / "kalshi_strike_ladder.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {(r[0], r[1]): r[2] for r in c.execute(
            "SELECT ticker, result, station FROM strike_ladder "
            "WHERE market_type='HIGH' AND result IN ('yes','no')")}
    finally:
        c.close()


def main():
    with gzip.open(GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    om = json.loads(OM_CACHE.read_text())
    om = {(k.split("|")[0], k.split("|")[1]): v for k, v in om.items()}

    # ---- per-station om_gfs bias/sigma (same recipe as the honest benchmark) --
    res = defaultdict(list)
    for k, g in grid.items():
        if g.get("settled_max") is None or k not in om:
            continue
        res[k[0]].append(om[k] - g["settled_max"])
    allb = [v for vs in res.values() for v in vs]
    bias_global = _mean(allb)
    sigma_global = _std(allb)
    bias_st = {st: _mean(v) for st, v in res.items() if len(v) >= 3}
    sigma_st = {st: _std(v) for st, v in res.items() if len(v) >= 3}

    kalshi = load_kalshi_high()

    # ---- build the cohort rows ----------------------------------------------
    rows = []
    for (st, ds), g in grid.items():
        if g.get("y") is None or g.get("strike") is None:
            continue
        if (st, ds) not in om:
            continue
        rec = recs.get((st, ds))
        if not rec or not rec.get("buckets"):
            continue
        rh_pin = running_high(rec, PIN)
        if rh_pin is None:
            continue
        T_lo = float(math.floor(rh_pin) + 1.0)
        T_hi = T_lo + LEG_GAP
        settled = g["settled_max"]
        if settled is None:
            continue
        y_lo = 1 if settled > T_lo else 0
        y_hi = 1 if settled > T_hi else 0
        kt = kalshi.get((st, ds))
        ky_lo = (1 if kt > T_lo else 0) if kt is not None else None
        ky_hi = (1 if kt > T_hi else 0) if kt is not None else None
        rows.append({
            "st": st, "ds": ds, "rec": rec, "T_lo": T_lo, "T_hi": T_hi,
            "y_lo": y_lo, "y_hi": y_hi, "ky_lo": ky_lo, "ky_hi": ky_hi,
            "om": om[(st, ds)],
            "sig": sigma_st.get(st, sigma_global),
            "bias": bias_st.get(st, bias_global),
        })

    # ---- simulate ------------------------------------------------------------
    def sim(sigma_mode):
        """sigma_mode: 'fixed' (honest-benchmark sigma) or 'decay' (shrinks with
        the fraction of the peak window still ahead)."""
        out = {}
        for tau, label in TAUS:
            acc_ret, conf_ret, brier_p, brier_y = [], [], [], []
            pnl2, pnl1 = [], []
            n_hold_both = n_hold_lo = n_hold_hi = 0
            # 13:30 entry probability for each leg (the model's own price proxy)
            for r in rows:
                mu13 = max(running_high(r["rec"], PIN), r["om"] - r["bias"])
                r["p_lo_entry"] = p_gt(mu13, r["sig"], r["T_lo"])
                r["p_hi_entry"] = p_gt(mu13, r["sig"], r["T_hi"])
            for r in rows:
                tau_r = tau
                rh = running_high(r["rec"], tau_r)
                if rh is None:
                    continue
                if sigma_mode == "decay":
                    frac = max((1440 - tau_r) / (1440 - PIN), 0.05)
                    sg = r["sig"] * math.sqrt(frac)
                else:
                    sg = r["sig"]
                mu = max(rh, r["om"] - r["bias"])
                p_lo = 1.0 if rh > r["T_lo"] else p_gt(mu, sg, r["T_lo"])
                p_hi = 1.0 if rh > r["T_hi"] else p_gt(mu, sg, r["T_hi"])

                if rh > r["T_hi"]:
                    hold = [(r["p_hi_entry"], p_hi, r["y_hi"], r["T_hi"])]
                    exited = [(r["p_lo_entry"], p_lo, r["y_lo"])]
                    n_hold_hi += 1
                elif rh > r["T_lo"]:
                    hold = [(r["p_lo_entry"], p_lo, r["y_lo"], r["T_lo"])]
                    exited = [(r["p_hi_entry"], p_hi, r["y_hi"])]
                    n_hold_lo += 1
                else:
                    hold = [(r["p_lo_entry"], p_lo, r["y_lo"], r["T_lo"]),
                            (r["p_hi_entry"], p_hi, r["y_hi"], r["T_hi"])]
                    exited = []
                    n_hold_both += 1

                # accuracy + confidence on retained legs
                for (pe, pc, y, _T) in hold:
                    acc_ret.append(y)
                    conf_ret.append(pc)
                    brier_p.append(pc)
                    brier_y.append(y)

                # ---- implied P&L (per contract, dollars), fees included ------
                # two-leg policy
                pl = 0.0
                for (pe, pc, y, _T) in hold:
                    pl += (y - pe) - kalshi_fee(1, pe) - kalshi_fee(1, max(1e-6, min(1 - 1e-6, y)))
                for (pe, pc, _y) in exited:
                    pl += (pc - pe) - kalshi_fee(1, pe) - kalshi_fee(1, max(1e-6, min(1 - 1e-6, pc)))
                pnl2.append(pl)

                # single-leg baseline: hold T_lo the whole time
                pe = r["p_lo_entry"]
                pnl1.append((r["y_lo"] - pe) - kalshi_fee(1, pe)
                            - kalshi_fee(1, max(1e-6, min(1 - 1e-6, r["y_lo"]))))

            n = len(acc_ret)
            out[label] = {
                "tau_mod": tau,
                "n_rows": len(pnl2),
                "n_retained_legs": n,
                "policy_split": {"hold_both": n_hold_both, "hold_lo": n_hold_lo,
                                 "hold_hi": n_hold_hi},
                "accuracy": round(_mean(acc_ret), 4) if acc_ret else None,
                "confidence": round(_mean(conf_ret), 4) if conf_ret else None,
                "brier": round(_mean([(p - y) ** 2 for p, y in zip(brier_p, brier_y)]), 4)
                if brier_p else None,
                "pnl_2leg_mean": round(_mean(pnl2), 4) if pnl2 else None,
                "pnl_1leg_mean": round(_mean(pnl1), 4) if pnl1 else None,
                "pnl_gain": round(_mean(pnl2) - _mean(pnl1), 4) if pnl2 else None,
                "sigma_mode": sigma_mode,
            }
        return out

    fixed = sim("fixed")
    decay = sim("decay")

    # ---- Kalshi-truth variant (fixed sigma), on the overlap sub-cohort ------
    kals = {}
    for tau, label in TAUS:
        acc_ret, conf_ret, bp, by = [], [], [], []
        n = 0
        for r in rows:
            if r["ky_lo"] is None:
                continue
            rh = running_high(r["rec"], tau)
            if rh is None:
                continue
            n += 1
            mu = max(rh, r["om"] - r["bias"])
            p_lo = 1.0 if rh > r["T_lo"] else p_gt(mu, r["sig"], r["T_lo"])
            p_hi = 1.0 if rh > r["T_hi"] else p_gt(mu, r["sig"], r["T_hi"])
            if rh > r["T_hi"]:
                hold = [(p_hi, r["ky_hi"])]
            elif rh > r["T_lo"]:
                hold = [(p_lo, r["ky_lo"])]
            else:
                hold = [(p_lo, r["ky_lo"]), (p_hi, r["ky_hi"])]
            for (pc, y) in hold:
                acc_ret.append(y); conf_ret.append(pc)
                bp.append(pc); by.append(y)
        kals[label] = {
            "n_rows": n,
            "n_retained_legs": len(acc_ret),
            "accuracy": round(_mean(acc_ret), 4) if acc_ret else None,
            "confidence": round(_mean(conf_ret), 4) if conf_ret else None,
            "brier": round(_mean([(p - y) ** 2 for p, y in zip(bp, by)]), 4) if bp else None,
        }

    artifact = {
        "generated_by": "scripts/multi_leg_sim_2026-10-15.py",
        "cohort": {
            "window": "2026-04-21..2026-10-09",
            "pin_local": "13:30", "leg_gap_F": LEG_GAP,
            "n_rows": len(rows),
            "n_with_kalshi_truth": sum(1 for r in rows if r["ky_lo"] is not None),
            "truth_axis": "strict crossing y = 1{settled_max > T}; grid truth is GHCN-based",
            "om_gfs_stats": {"bias_global_F": round(bias_global, 4),
                             "sigma_global_F": round(sigma_global, 4)},
        },
        "variant_fixed_sigma": fixed,
        "variant_decay_sigma": decay,
        "kalshi_truth_variant": kals,
        "note": "Simulation only. implied P&L uses the model's own probability as the "
                "entry/exit price proxy; it measures whether the two-leg + exit policy "
                "adds value under the model's own prices, not market-realized P&L.",
    }
    OUT_JSON.write_text(json.dumps(artifact, indent=2))
    print(json.dumps({"fixed": fixed, "kalshi": kals}, indent=2))

    # ---- markdown -----------------------------------------------------------
    lines = []
    lines.append("# Multi-Leg Simulation — two strikes, evolving om_gfs, 1/3/6h")
    lines.append("")
    lines.append("**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`")
    lines.append("**Script:** `scripts/multi_leg_sim_2026-10-15.py` · **Artifact:** `data/multi_leg_sim_2026-10-15.json`")
    lines.append("")
    lines.append("**Simulation, not a system test.** Read-only. No book writes.")
    lines.append("")
    lines.append("## Setup")
    lines.append("")
    lines.append(f"- Cohort: honest HIGH cohort with ASOS recs + om_gfs + grid truth, "
                 f"`{artifact['cohort']['window']}`, 13:30 station-local pin, **n = {len(rows)}** "
                 f"({artifact['cohort']['n_with_kalshi_truth']} also carry Kalshi truth).")
    lines.append(f"- Legs: `T_lo = floor(running_high@13:30) + 1` (om_gfs best-guess strike), "
                 f"`T_hi = T_lo + {int(LEG_GAP)}` (Dan's T75/T80 spacing).")
    lines.append("- Evolving forecast at tau: `mu_final = max(running_high(tau), om_gfs_bc_ps)`; "
                 "`P(final > T) = 1` if `running_high(tau) > T`, else the Gaussian crossing prob.")
    lines.append("- Truth: strict crossing `y = 1{settled_max > T}` (GHCN-based grid); Kalshi variant alongside.")
    lines.append("- Policy: `rh > T_hi` -> hold T_hi / exit T_lo; `rh > T_lo` -> hold T_lo / exit T_hi; "
                 "else hold both.")
    lines.append("")
    for name, blk, title in (("fixed", fixed, "Primary — fixed om_gfs sigma"),
                             ("decay", decay, "Sensitivity — sigma decays with the remaining peak window")):
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| holding time | n rows | retained legs | accuracy | confidence | Brier | P&L 2-leg | P&L 1-leg | gain |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for _, label in TAUS:
            d = blk[label]
            lines.append(f"| {label} after pin | {d['n_rows']} | {d['n_retained_legs']} | "
                         f"{d['accuracy']} | {d['confidence']} | {d['brier']} | "
                         f"{d['pnl_2leg_mean']} | {d['pnl_1leg_mean']} | {d['pnl_gain']} |")
        lines.append("")
        lines.append("Policy split (hold-both / hold-lo / hold-hi): ")
        lines.append("`" + " · ".join(
            f"{label}: {blk[label]['policy_split']['hold_both']}/{blk[label]['policy_split']['hold_lo']}/"
            f"{blk[label]['policy_split']['hold_hi']}" for _, label in TAUS) + "`")
        lines.append("")
    lines.append("## Kalshi-truth variant (fixed sigma, overlap sub-cohort)")
    lines.append("")
    lines.append("| holding time | n rows | retained legs | accuracy | confidence | Brier |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for _, label in TAUS:
        d = kals[label]
        lines.append(f"| {label} after pin | {d['n_rows']} | {d['n_retained_legs']} | "
                     f"{d['accuracy']} | {d['confidence']} | {d['brier']} |")
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    lines.append("- **accuracy** = fraction of retained legs that settle YES (the winner).")
    lines.append("- **confidence** = mean decision-time P(cross) of the retained legs.")
    lines.append("- **P&L** = implied per-contract dollars, entry at model p@13:30 and exit at model "
                 "p@tau (fees via `core.fees`). It measures whether the two-leg + exit policy is "
                 "additive *under the model's own prices*, not market-realized P&L.")
    OUT_MD.write_text("\n".join(lines) + "\n")
    print("wrote", OUT_JSON, OUT_MD)


if __name__ == "__main__":
    main()
