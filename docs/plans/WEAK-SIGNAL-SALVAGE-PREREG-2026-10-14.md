# Pre-registration — weak-signal salvage ("is there anything useful in the old signals?") — 2026-10-14

**Author:** Gilfoyle (rolled-up expert). **Written BEFORE any test is run.** Any
threshold, cohort, or metric changed after this file is committed is a post-hoc fit
and must be flagged as such in the final report.

## 0. Question (Dan, 2026-10-14)

> "Is there anything useful to do with the old directional accuracy signals? Do any
> help the new analysis/decision/prediction model? Can any inform trajectory or our
> other functions?"

**Settled premise (do NOT re-litigate):** *fusion of the weak signals hurts.* Best
single `om_gfs(bc,ps)` = 0.1067; equal-weight fusion = 0.1458; even tuned to
`w_om=0.80` the fusion only returns to 0.1063. So the question is **not** "how do we
fuse them." The question is whether any weak signal has **standalone salvage value in
a different role**: (a) telling us *when* to trust `om_gfs` less, or (b) supplying a
*residual correction*.

## 1. Fixed analysis frame (frozen)

- **Cohort:** the honest station-days `2026-04-21 → 2026-10-09`, rebuilt from the
  shared honest loader (`/tmp/we_grid_2026-10-13.pkl.gz` + `/tmp/we_asos_recs_2026-10-13.pkl.gz`
  + `om_gfs` cache). Pin = **13:30 station-local**. Axis = strict crossing,
  `T = floor(hsf@13:30)+1`, `crossed = settled_max > T` (`==` is LOSS). Truth =
  canonical GHCN settlement. **Look-ahead-free** (every predictor point-in-time).
- **Target signal for all residual tests:** `om_gfs(bc,ps)` at 13:30 (the incumbent).
  Baselines at 13:30 (from `HONEST-SIGNAL-ACCURACY-2026-10-14.md`):
  `om_gfs_bc_ps` **0.1067**, `gefs` 0.3342, `rate_1h_pit` 0.3617, `market` 0.6194.
- **Multiple-comparison discipline:** 4 hypotheses. α = 0.05 two-sided per test;
  with Bonferroni (α/4 = 0.0125) for the two regression tests. Every test that
  "clears" must survive **both** the primary statistic **and** an out-of-sample split.

## 2. The four pre-registered hypotheses

### H1 — NWP cross-source DISAGREEMENT is a usable quality flag
- **Predictor:** at 13:30, spread of the NWP level members. Two variants:
  - `spread5` = std across available {gfs, gem, icon, ecmwf, gefs} levels (°F).
  - `spread6` = std across {gfs, gem, icon, ecmwf, gefs, om_gfs_raw} levels (°F).
  - Also recorded: range (max−min) as a robustness check.
- **Outcome:** `om_gfs(bc,ps)` Brier at 13:30, and its level error `|om_gfs_raw − settled_max|`.
- **Primary test:** quartile bins of `spread5` over rows with ≥3 members present
  (adjacent-value cuts, ≤2 decimal precision, deduped, so all 4 bins are populated).
  Q1=low, Q4=high.
  - **USABLE iff** `Brier(Q4) − Brier(Q1) ≥ 0.02` **AND** Spearman `spread5` vs squared
    om_gfs error `> 0` with two-sided `p < 0.05`.
  - **WEAK / not wired** if `0.01 ≤ ΔBrier < 0.02` or `0.02 ≤ p < 0.05`.
  - **NO USE** if `ΔBrier < 0.01` or Spearman `p ≥ 0.05`.
  - "Monotone across quartiles" is a **bonus** observation, not a gate.
- **Threshold rationale:** the incumbent Brier is 0.107; a gate that can't move the
  metric by ≥ ~20% of its own value (0.02 absolute) measured on ≥ ~500 rows is not
  worth wiring. 0.02 is the smallest effect that survives the cohort's noise scale.

### H2 — `rate_1h` (0.362, weak as a probability) as a *residual* correction
- **Predictor:** `rate_1h` point-in-time at 13:30 — both the raw 1h rate and the
  deflated nowcast rise `d = mu − hsf` (`rate_pit(...)["rate"]` and `["mu"]−["hsf"]`).
- **Outcome:** `om_gfs` level residual `e = settled_max − om_gfs_raw`.
- **Test:** OLS `e = a + b·rate_feature`. Test `b` with a **paired block bootstrap by
  station** (preserves within-station serial dependence), B = 2000, seed 20261014.
- **USABLE iff** the bootstrap 95% CI on `b` excludes 0 **AND** the implied correction
  improves honest Brier by **≥ 0.005** on a **held-out half** (split by alternating
  station-days; fit on train, apply on test). Both required.
- **NO USE** otherwise. If USABLE, the role is *residual correction*, never a fusion member.

### H3 — `market` (0.619) as a *lag* indicator
- **Predictor:** market mid at the 13:30 strike (`market_p_at`).
- **Outcome:** `om_gfs` level residual `e = settled_max − om_gfs_raw`.
- **Test:** OLS `e = a + b·market_mid`, paired bootstrap by station (as H2). Prior
  register finding: `corr ≈ −0.002` ⇒ expect null.
- **USABLE iff** CI on `b` excludes 0 **AND** held-out Brier improves ≥ 0.005.
- **NO USE** otherwise.

### H4 — `gefs` SPREAD (not mean) as an *uncertainty / position-sizing* input
- **Predictor:** GEFS ensemble spread `gefs_sd` (°F) at 13:30.
- **Outcome:** magnitude of `om_gfs` level error `|om_gfs_raw − settled_max|`.
- **Test:** Spearman `gefs_sd` vs `|e|` (two-sided, Bonferroni α/4 = 0.0125).
  Non-parametric on purpose (no normality assumption on `|e|`).
- **USABLE iff** Spearman `> 0` and `p < 0.0125` **AND** the top spread quartile has
  `mean|e|` at least **1.5×** the bottom quartile's **AND** an OLS `|e| = a + b·gefs_sd`
  has bootstrap 95% CI on `b` excluding 0. Role would be *sizing*, not probability.
- **NO USE** otherwise.

## 3. What "clears" means for wiring

For every signal that clears its gate the report must state, in the same section:
(a) the proposed role, (b) the expected benefit measured on the held-out half,
(c) whether wiring is worth the complexity **relative to shipping `om_gfs` alone**.
A signal that clears on ≤ ~100 rows, or whose benefit does not survive the held-out
split, is reported as **NO USE** even if the in-sample number looks good.

## 4. Honest-zero clause

If nothing clears, the report says so plainly. No manufactured value. The four tests
are independent; it is a perfectly acceptable result that all four are NO USE.

## 5. Outputs (frozen)

- `scripts/disagreement_quality_flag_2026-10-14.py` (all four tests; shared cohort)
- `data/weak_signal_salvage_2026-10-14.json` · `artifacts/weak-signal-salvage/*.json`
- `docs/plans/WEAK-SIGNAL-SALVAGE-2026-10-14.md`
- Read-only on every book; no trades writes; no temp DBs.
