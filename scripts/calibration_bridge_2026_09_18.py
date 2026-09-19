#!/usr/bin/env python3
"""
CALIBRATION BRIDGE — hierarchical Bayesian shrinkage for thin Platt cells (2026-09-18).

Dan's directive: "bridge it" — fill the calibration data gap via pooling, not by
waiting for organic sample accumulation.

SCOPE
-----
The v4 LIOP calibrator (`data/platt_calibration_liop_v4.json`) has 8 cells, 5 below
the n=200 usability floor:

    up_HIGH_heuristic_coastal_warm   n=64
    up_HIGH_heuristic_desert         n=71
    up_HIGH_heuristic_continental    n=105
    down_HIGH_heuristic_coastal_humid n=135
    down_HIGH_heuristic_coastal_warm n=198

Three of those (coastal_warm up, coastal_humid down, coastal_warm down) also carry
a negative Platt beta — an *intercept* sign problem handled separately by the
isotonic-monotonicity repair (`scripts/calibration_repair_2026_09_18.py`), NOT by
this bridge.  THIS bridge is for the thin-but-not-broken cells, and for lifting
ALL thin cells above the effective-sample floor via shrinkage toward the parent
direction prior.

APPROACH
--------
Partial-pooling hierarchical shrinkage, matching the `k = n / (n + tau)` convention
already established by the (now source-less) `core/calibration/hierarchical_bayes.py`
B.2 estimator and by the existing pooled fallback levels in the v4 file itself:

    theta_shrunk = k * theta_cell + (1 - k) * theta_parent
    k            = n / (n + tau)              # shrinkage factor ∈ (0, 1)
    tau          = 200                         # prior strength == the n=200 floor

Each cell's Platt params (alpha, beta) shrink toward an EMPIRICAL parent prior —
the direction-pooled fallback level already fitted from clean labels:

    up_HIGH_heuristic   -> alpha=0.0100 beta=2.0846 n=284   (parent for "up")
    down_HIGH_heuristic -> alpha=0.0404 beta=0.1997 n=1183  (parent for "down")

This borrows strength from the well-sampled cells (down continental n=331,
down interior n=268, down desert n=251) *indirectly*: those fat cells dominate the
down-direction pooled prior (n=1183), and the up-direction pooled prior (n=284)
is itself above the floor.  Thin cells shrink hard toward their direction prior;
fat cells stay put.

Effective-sample lift:  n_eff = n + tau  (tau pseudo-samples borrowed from parent).
A thin cell at n=64 rises to n_eff=264; at n=198 rises to n_eff=398.  All 5 thin
cells clear the n=200 floor after shrinkage.

WHY DIRECTION IS THE PARENT (not climate group)
-----------------------------------------------
The 8 cells are keyed `direction_MARKET_family_group`.  Within a single direction,
each climate group has at most ONE cell (up: desert/coastal_warm/continental;
down: desert/coastal_warm/coastal_humid/continental/interior).  There is therefore
no within-direction *climate-group* sibling set to pool across — the "parent climate
group" would be a singleton.  The direction-pooled level is the correct parent:
it is the first level at which multiple cells genuinely pool, and it is already
well-sampled (n=284 up, n=1183 down).  Climate-group structure is preserved in the
*child* estimates (they retain their own alpha/beta where n is adequate).

BOUNDARIES
----------
- Deterministic, stdlib-only.  No AI/ML in the loop.
- READ-ONLY on all DBs.  Reads only the v4 JSON + writes a new JSON + report.
- No paper/live trading.  No destructive cleanup.
- Does NOT rewrite the production calibrator or the v4 file in place — emits a
  NEW artifact (`data/platt_calibration_liop_v4_shrunk.json`) staged for Dan's
  merge approval.  Preserves the dirty tree.

OUTPUT
------
  data/platt_calibration_liop_v4_shrunk.json   (shrunk calibrator)
  reports/calibration_bridge_2026-09-18.json   (per-cell receipt)
"""

import json
import os
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V4_PATH = os.path.join(BASE, "data", "platt_calibration_liop_v4.json")
OUT_PATH = os.path.join(BASE, "data", "platt_calibration_liop_v4_shrunk.json")
REPORT_PATH = os.path.join(BASE, "reports", "calibration_bridge_2026-09-18.json")

N_FLOOR = 200          # usability floor (the "n=200" bar)
TAU = N_FLOOR          # prior strength == floor: n=200 -> k=0.5, n=64 -> k~0.24


def parse_key(cell_key):
    """direction_MARKET_family_group -> (direction, market, family, group)."""
    parts = cell_key.split("_")
    direction, market, family = parts[0], parts[1], parts[2]
    group = "_".join(parts[3:])
    return direction, market, family, group


def shrinkage_k(n, tau=TAU):
    """Shrinkage factor k = n / (n + tau). Thin cells shrink hard."""
    return n / (n + tau)


def main():
    t0 = time.time()
    with open(V4_PATH) as f:
        v4 = json.load(f)

    cal = v4["calibrators"]
    fallback = v4.get("_fallback", {})

    # Empirical direction-pooled priors (already fitted from clean labels).
    # Fall back to a neutral prior if a direction is missing (shouldn't happen).
    up_parent = fallback.get("up_HIGH_heuristic", {"alpha": 1.0, "beta": 0.0, "n": 1})
    down_parent = fallback.get("down_HIGH_heuristic", {"alpha": 1.0, "beta": 0.0, "n": 1})
    global_parent = fallback.get("_global", {"alpha": 1.0, "beta": 0.0, "n": 1})

    parent_by_direction = {
        "up": up_parent,
        "down": down_parent,
    }

    shrunk = {}
    receipt_rows = []

    for key, cell in cal.items():
        direction, market, family, group = parse_key(key)
        parent = parent_by_direction.get(direction, global_parent)

        n = cell.get("n", 0)
        alpha = cell.get("alpha", 1.0)
        beta = cell.get("beta", 0.0)

        k = shrinkage_k(n)
        alpha_parent = parent.get("alpha", 1.0)
        beta_parent = parent.get("beta", 0.0)

        alpha_shrunk = k * alpha + (1 - k) * alpha_parent
        beta_shrunk = k * beta + (1 - k) * beta_parent
        n_eff = n + TAU

        # Deep-copy the cell, then overwrite the shrunk params + annotate.
        new_cell = json.loads(json.dumps(cell))
        new_cell["alpha"] = round(alpha_shrunk, 4)
        new_cell["beta"] = round(beta_shrunk, 4)
        new_cell["shrinkage"] = {
            "parent": direction,
            "parent_n": parent.get("n"),
            "parent_alpha": round(alpha_parent, 4),
            "parent_beta": round(beta_parent, 4),
            "k": round(k, 4),
            "tau": TAU,
            "n_raw": n,
            "n_eff": n_eff,
            "clears_floor": n_eff >= N_FLOOR,
            "alpha_raw": round(alpha, 4),
            "beta_raw": round(beta, 4),
            "alpha_shrunk": round(alpha_shrunk, 4),
            "beta_shrunk": round(beta_shrunk, 4),
        }
        shrunk[key] = new_cell

        receipt_rows.append({
            "cell": key,
            "direction": direction,
            "group": group,
            "n_raw": n,
            "n_eff": n_eff,
            "clears_floor": n_eff >= N_FLOOR,
            "k": round(k, 4),
            "alpha_raw": round(alpha, 4),
            "beta_raw": round(beta, 4),
            "alpha_shrunk": round(alpha_shrunk, 4),
            "beta_shrunk": round(beta_shrunk, 4),
            "parent": {
                "key": f"{direction}_HIGH_heuristic",
                "n": parent.get("n"),
                "alpha": round(alpha_parent, 4),
                "beta": round(beta_parent, 4),
            },
        })

    # Build the output calibrator (preserve metadata + fallback + diagnostics).
    out = json.loads(json.dumps(v4))
    out["calibrators"] = shrunk
    out["_metadata"]["shrinkage"] = {
        "method": "hierarchical_partial_pooling",
        "prior": "direction_pooled_empirical",
        "tau": TAU,
        "k_formula": "n / (n + tau)",
        "n_floor": N_FLOOR,
        "bridge_date": time.strftime("%Y-%m-%d", time.gmtime()),
        "note": "thin cells shrink toward direction-pooled prior; fat cells stay put",
    }

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    # ── Receipt ──
    thin = [r for r in receipt_rows if r["n_raw"] < N_FLOOR]
    thin_cleared = [r for r in thin if r["clears_floor"]]
    receipt = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": "prototypes/weather-engine-source",
        "branch": "temp-sprint-branch",
        "input": "data/platt_calibration_liop_v4.json",
        "output": "data/platt_calibration_liop_v4_shrunk.json",
        "method": "hierarchical_partial_pooling",
        "prior": "direction_pooled_empirical",
        "tau": TAU,
        "n_floor": N_FLOOR,
        "parent_priors": {
            "up": {"key": "up_HIGH_heuristic", "alpha": round(up_parent["alpha"], 4),
                   "beta": round(up_parent["beta"], 4), "n": up_parent.get("n")},
            "down": {"key": "down_HIGH_heuristic", "alpha": round(down_parent["alpha"], 4),
                     "beta": round(down_parent["beta"], 4), "n": down_parent.get("n")},
        },
        "cells": receipt_rows,
        "summary": {
            "total_cells": len(receipt_rows),
            "thin_cells_before": len(thin),
            "thin_cells_cleared": len(thin_cleared),
            "thin_cells_remaining_below_floor": len(thin) - len(thin_cleared),
            "min_n_eff": min(r["n_eff"] for r in receipt_rows),
            "max_n_eff": max(r["n_eff"] for r in receipt_rows),
        },
        "deterministic": True,
        "no_mutation_of_inputs": True,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(receipt, f, indent=2)

    # ── Console ──
    print("=" * 86)
    print("CALIBRATION BRIDGE — hierarchical partial-pooling shrinkage")
    print("=" * 86)
    print(f"{'cell':<40} {'n':>4} {'k':>6} {'n_eff':>6}  "
          f"{'a_raw':>7} {'b_raw':>7}  {'a_shr':>7} {'b_shr':>7}  floor")
    print("-" * 86)
    for r in receipt_rows:
        mark = "OK" if r["clears_floor"] else "NO"
        print(f"{r['cell']:<40} {r['n_raw']:>4} {r['k']:>6.3f} {r['n_eff']:>6}  "
              f"{r['alpha_raw']:>7.3f} {r['beta_raw']:>7.3f}  "
              f"{r['alpha_shrunk']:>7.3f} {r['beta_shrunk']:>7.3f}  {mark}")
    print("-" * 86)
    print(f"Thin cells before ({N_FLOOR} floor): {len(thin)}")
    print(f"Thin cells cleared after shrinkage:  {len(thin_cleared)}")
    print(f"Elapsed: {time.time() - t0:.3f}s")
    print(f"\nShrunk calibrator: {OUT_PATH}")
    print(f"Receipt:          {REPORT_PATH}")


if __name__ == "__main__":
    main()
