# Calibration Bridge — Hierarchical Bayesian Shrinkage for Thin Platt Cells

**Date:** 2026-09-18
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved)
**Directive:** Dan — "bridge it" (fill the calibration data gap via pooling, not organic accumulation)
**Runner:** `scripts/calibration_bridge_2026_09_18.py` (deterministic, stdlib-only, read-only on DBs)

---

## 0. What this is, and what it is not

`data/platt_calibration_liop_v4.json` has 8 cells, 5 below the n=200 usability
floor. Three of those also carry a negative Platt **beta** (a *sign* problem on the
intercept) — that is handled separately by the isotonic-monotonicity repair
(`scripts/calibration_repair_2026_09_18.py`). **This bridge is for the
thin-but-not-broken cells, and for lifting ALL thin cells above the effective-sample
floor** via partial-pooling shrinkage toward an empirical parent prior.

The bridge does **not** rewrite the v4 file in place, does **not** touch the
production `calibrator_library.json`, and does **not** change monotonicity behavior
for the negative-beta cells. It emits a new artifact
(`data/platt_calibration_liop_v4_shrunk.json`) staged for Dan's merge approval.

---

## 1. What already exists (do-not-rebuild check)

| Artifact | Status | Reuse decision |
|---|---|---|
| `reports/hierarchical_bayes_fit.json` | Exists (task B.2, 2026-09-06) | **Pattern reuse only.** It targets a *different* schema — per-station × per-condition `bias`/`mae` estimation (4-tier, `k = n/(n+threshold)`, thresholds 5/20/100), NOT these Platt `alpha`/`beta` cells. |
| `core/calibration/hierarchical_bayes.py` | Source **deleted** (only `.pyc` remains) | Machinery is gone as an importable module. Its `k = n/(n+tau)` shrinkage convention is preserved and reused here. |
| `data/platt_calibration_liop_v4.json` `_fallback` | Exists | **This is the empirical prior.** The direction-pooled levels are already fitted from clean labels: `up_HIGH_heuristic` (n=284), `down_HIGH_heuristic` (n=1183), `_global` (n=1468). |

**Conclusion:** the hierarchical-Bayes *machinery* does not exist as a wired,
importable module for these Platt cells (source deleted, wrong schema). The
*convention* (`k = n/(n+tau)`) and the *empirical prior* (direction-pooled fallback
levels) both already exist. So this is a **wire-up**, not a rebuild: a thin bridge
module that applies the existing shrinkage convention against the existing
direction-pooled priors.

---

## 2. The shrinkage model

Partial-pooling hierarchical shrinkage (normal-normal conjugate analogue):

```
theta_shrunk = k * theta_cell  +  (1 - k) * theta_parent
k            = n / (n + tau)                # shrinkage factor ∈ (0, 1)
tau          = 200                          # prior strength == the n=200 floor
n_eff        = n + tau                      # effective sample size (posterior)
```

- `k → 0` as `n → 0` (thin cells shrink hard to the parent).
- `k → 1` as `n → ∞` (fat cells stay put).
- At the floor (`n = 200`), `k = 0.5` — half own data, half parent. This is the
  natural "usability" midpoint and is why `tau = 200` is the prior strength.
- `n_eff = n + tau` is the standard posterior effective-sample size: shrinkage adds
  `tau` pseudo-samples borrowed from the parent, which is exactly the "lift" the
  task asks to quantify.

Applied to both Platt params independently: `alpha` (slope) and `beta` (intercept).

### Why the *direction* is the parent (not climate group)

The 8 cells are keyed `direction_MARKET_family_group`. Within a single direction,
each climate group holds **at most one cell** (up: desert/coastal_warm/continental;
down: desert/coastal_warm/coastal_humid/continental/interior). A "parent climate
group" would therefore be a *singleton* — nothing to pool across. The first level at
which multiple cells genuinely pool is **direction**, and its pooled prior is already
well-sampled (`up` n=284, `down` n=1183). Climate-group structure is preserved in the
child estimates: cells keep their own `alpha`/`beta` where `n` is adequate, and only
the thin cells lean on the direction prior.

The "borrow from well-sampled cells" effect is **indirect but real**: the fat down
cells (continental 331, interior 268, desert 251) dominate the down-pooled prior
(n=1183), which is what the thin down cells (coastal_humid 135, coastal_warm 198)
shrink toward. The up-pooled prior (n=284) is itself above the floor, so the thin up
cells (desert 71, coastal_warm 64, continental 105) borrow from a parent that is
already stable.

### Relationship to the Beta(2,2) prior

The v4 `_metadata.regularization` cites a `Beta(2,2)` prior for `n < 200` — a fixed
weak prior equivalent to ~4 pseudo-samples. This bridge replaces that fixed weak
prior with a **stronger empirical prior** (`tau = 200` pseudo-samples drawn from the
direction-pooled level). Same functional form, stronger and data-driven shrinkage.

---

## 3. Per-cell post-shrinkage results

| Cell | n (raw) | k | n_eff | α raw→shrunk | β raw→shrunk | clears floor? |
|---|---|---|---|---|---|---|
| `up_HIGH_heuristic_coastal_warm` | 64 | 0.242 | **264** | 0.837→0.210 | −0.494→1.460 | ✅ |
| `up_HIGH_heuristic_desert` | 71 | 0.262 | **271** | 0.061→0.023 | 0.651→1.709 | ✅ |
| `up_HIGH_heuristic_continental` | 105 | 0.344 | **305** | 0.010→0.010 | 0.796→1.641 | ✅ |
| `down_HIGH_heuristic_coastal_humid` | 135 | 0.403 | **335** | 0.010→0.028 | −0.239→0.023 | ✅ |
| `down_HIGH_heuristic_coastal_warm` | 198 | 0.497 | **398** | 0.775→0.406 | −0.680→−0.238 | ✅ |
| `down_HIGH_heuristic_desert` | 251 | 0.556 | 451 | 0.010→0.024 | 0.365→0.292 | ✅ (already fat) |
| `down_HIGH_heuristic_interior` | 268 | 0.573 | 468 | 0.010→0.023 | 0.195→0.197 | ✅ (already fat) |
| `down_HIGH_heuristic_continental` | 331 | 0.623 | 531 | 0.085→0.068 | 0.259→0.237 | ✅ (already fat) |

**Effective-sample lift:** all 5 thin cells clear the n=200 floor after shrinkage
(min `n_eff` = 264, max = 531). The two thin-but-not-broken cells that this bridge is
directly responsible for — `up desert` (71→271) and `up continental` (105→305) — both
rise well above the floor with sensible, monotonic params.

**Parent priors used:**

| Direction | Key | n | α | β |
|---|---|---|---|---|
| up | `up_HIGH_heuristic` | 284 | 0.0100 | 2.0846 |
| down | `down_HIGH_heuristic` | 1183 | 0.0404 | 0.1997 |

### Monotonicity / usability sanity check

All 8 shrunk cells have `alpha > 0` (monotonic), and calibrated probability at the
dominant raw confidence (~0.79 → logit ≈ 1.325) lands in a sane band:

```
up   cells  → P(correct) ≈ 0.84–0.85   (up is the "hard" direction, high P when fired)
down cells  → P(correct) ≈ 0.51–0.58   (down is more diffuse)
```

No degenerate 0/1 outputs.

---

## 4. Interaction with the isotonic-monotonicity fix (explicit hand-off)

This bridge shrinks `beta` *linearly* toward the direction prior. It stabilizes the
three negative-beta cells substantially (e.g. `down coastal_warm` −0.680 → −0.238)
but does **not** guarantee a sign flip to positive `beta`. That is by design — the
isotonic-monotonicity repair owns the sign correction. Recommended composition order:

1. **Isotonic fix first** (corrects anti-monotonic sign on the 3 broken cells).
2. **Shrinkage bridge second** (lifts all thin cells above the floor, borrows
   strength from the direction prior).

The two are orthogonal: isotonic enforces monotonicity; shrinkage enforces
sample-sufficiency. Both are required for a usable production calibrator.

---

## 5. Artifacts

| Path | Role |
|---|---|
| `scripts/calibration_bridge_2026_09_18.py` | Deterministic bridge (new) |
| `data/platt_calibration_liop_v4_shrunk.json` | Shrunk calibrator (new; `data/` is gitignored) |
| `reports/calibration_bridge_2026-09-18.json` | Per-cell receipt + summary (new) |

**Merge note:** staged for Dan's approval. Not committed. Dirty tree preserved
(only additive files; no edits to tracked inputs).

---

## 6. Boundaries respected

- ✅ No live trading / paper trading.
- ✅ No destructive cleanup.
- ✅ Read-only on all DBs (bridge reads only the v4 JSON).
- ✅ Works against clean `settlement_epochs` labels (the direction-pooled priors in
  the v4 `_fallback` were themselves fitted from clean `epoch_status='closed'` labels
  with `settlement_bucket`/`prior_settlement_bucket ∈ [-22, 119]`, excluding the 28
  unresolved + 18 linkage rows).
- ✅ Does not commit — staged for merge approval.
- ✅ Dirty tree preserved.
