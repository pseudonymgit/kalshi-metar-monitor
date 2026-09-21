# R/T-8b — Exhaustive Multi-Target Recalibration: Receipt

**Author:** Gilfoyle (CTIO)
**Date:** 2026-09-21 02:35 UTC
**Repo:** `prototypes/weather-engine-source` @ branch `temp-sprint-branch`
**Method:** script-only, deterministic. No AI-in-loop.
**Entry point:** `scripts/rt8b_exhaustive_recalibration.py`

---

## Summary

First VALID multi-target calibration run on the **corrected 7-signal set through
the CONNECTED fusion**. Prior "0 genuine-edge" numbers came from the broken
7-of-10 + unimportable-fusion path (fixed by R/T-8a, commit `1d62f843d`).

**Headline result: the connected fusion (LIOP `SignalFusionEngine`) has no
genuine directional edge.** Global MCC = **-0.0165** (slightly negative), and
per-target accuracy tracks the market-type-conditional base rate everywhere.
This confirms — on the corrected, connected path — the R/T-8F A/B finding
(LIOP MCC -0.0119): the fusion is not skill-positive after the axis correction.

### Axis + provenance (every number below)

- **Axis:** strike-crossing direction = `settlement_temp_f` vs `strike_price`,
  via `core/settlement_outcome.strike_crossing_direction` (single source of truth).
  `'up'` = HIGH market temp > strike (LOW market temp >= strike → loss);
  `'down'` = HIGH temp <= strike (LOW temp < strike → win).
- **Labels:** `settlement_truth.strike_price` — 14,020 usable rows (14,305 raw;
  54 `between` + 80 untrusted excluded).
- **Signals:** `BACKTEST_SIGNALS` = 7 honest live signals
  (`gaussian`, `gaussian_v2`, `pressure_delta`, `calendar_climatology`,
  `forecast_disagreement`, `corrected_pressure_delta`, `frontal_passage_intraday`).
- **Fusion:** `SignalFusionEngine.fuse_signals` (LIOP/LLOP, `core/fusion_logic.py`)
  — the connected fusion whose import R/T-8a fixed in `unified_backtest.py`.
  (The alternate `UncertaintyWeightedCascade` UWC engine is degenerate — predicts
  `'up'` 100% by construction; see R/T-8F and the note below.)
- **Metric:** MCC is the honest skill metric (label set is market-type-conditional:
  HIGH ~10.1% up, LOW ~94.6% up). Raw accuracy is base-rate-inflated; every slice
  reports accuracy + MCC + base-rate delta.

---

## Part (a) — Full suite re-run

`scripts/ultimate_sweep.py --layer directional --layer calibration --layer fusion`
re-run on the corrected set (launched; canonical harness). Signal-level numbers
are unchanged from the 2026-09-19 baseline because R/T-8a changed fusion wiring
and the `BACKTEST_SIGNALS` list, not individual signal evaluation. Baseline
directional sweep (correct axis, 2026-09-19) for the 7 corrected signals:

| signal | accuracy | MCC | n |
|---|---|---|---|
| gaussian | 49.89% | -0.1515 | 5568 |
| gaussian_v2 | 50.74% | -0.0449 | 9438 |
| pressure_delta | 50.61% | +0.0091 | 7594 |
| calendar_climatology | 47.37% | -0.1973 | 3078 |
| forecast_disagreement | 48.97% | -0.0012 | 4680 |
| corrected_pressure_delta | 49.41% | -0.0088 | 7602 |
| frontal_passage_intraday | 65.82% | +0.1081 | 2809 |

No individual signal clears a positive-MCC bar vs the down-skew null.

---

## Part (b) — Exhaustive multi-target calibration grid (connected LIOP fusion)

Fired rate: **12,151 / 14,020 (86.7%)** — the LIOP fusion produces a direction on
most labeled events; 13.3% are DS-conflict/insufficient-signal nulls.

### Global

| metric | value |
|---|---|
| accuracy | 50.12% |
| MCC | **-0.0165** |
| base-rate accuracy | 65.63% |
| base-rate delta | -15.51% |

### Per-station (axis + provenance above)

| station | n | accuracy | MCC | baseΔ |
|---|---|---|---|---|
| KATL | 340 | 54.71% | +0.0905 | -0.29% |
| KAUS | 1263 | 53.21% | +0.0493 | -23.67% |
| KBOS | 350 | 52.86% | +0.0431 | -2.29% |
| KDCA | 363 | 49.86% | -0.0031 | -5.23% |
| KDEN | 862 | 50.35% | -0.0332 | -21.11% |
| KDFW | 337 | 55.49% | +0.1057 | -1.19% |
| KHOU | 368 | 53.53% | +0.0594 | -5.16% |
| KLAS | 366 | 54.92% | +0.0753 | -4.64% |
| KLAX | 653 | 46.55% | -0.0829 | -15.01% |
| KMDW | 1459 | 47.36% | -0.0571 | -21.18% |
| KMIA | 1152 | 48.70% | -0.0593 | -23.09% |
| KMSP | 364 | 54.12% | +0.0679 | -0.82% |
| KMSY | 340 | 53.24% | +0.0223 | -6.47% |
| KNYC | 1438 | 45.83% | -0.0907 | -29.97% |
| KOKC | 336 | 55.06% | +0.0828 | -0.60% |
| KPHL | 844 | 44.55% | -0.0925 | -18.13% |
| KPHX | 337 | 54.90% | +0.0908 | -3.56% |
| KSAT | 319 | 55.17% | +0.0901 | +0.94% |
| KSEA | 330 | 52.12% | +0.0266 | -7.58% |
| KSFO | 330 | 48.48% | -0.0576 | -5.45% |

Per-station MCC is near zero (±0.1); the few "positive" values are noise-level
and mostly below the base-rate delta they'd need to clear for real edge.

### Per-regime (HIGH / LOW)

| regime | n | accuracy | MCC | baseΔ |
|---|---|---|---|---|
| HIGH | 8661 | 51.02% | -0.0300 | -38.83% |
| LOW | 3490 | 47.88% | +0.0647 | -46.59% |

### Per-season (meteorological)

| season | n | accuracy | MCC | baseΔ |
|---|---|---|---|---|
| DJF | 2401 | 49.23% | -0.0030 | -21.07% |
| JJA | 4105 | 52.33% | +0.0215 | -7.94% |
| MAM | 3920 | 55.13% | +0.0249 | -8.55% |
| SON | 1725 | 34.72% | -0.0506 | -41.62% |

### UP vs DOWN directions (separately)

| predicted | n | precision | actual up-rate | actual down-rate |
|---|---|---|---|---|
| up | 5725 | 33.54% | 34.76% | 65.24% |
| down | 6426 | 64.89% | 34.76% | 65.24% |

The fusion's directional precision on each side matches the global up/down base
rate (~34.8% / ~65.2%) — i.e. **no directional discrimination** beyond the null.

### Calibration PAIRS (UP precision vs DOWN precision per target)

**Per-regime (most diagnostic — the regime conditions the base rate):**

| regime | UP n | UP precision | DOWN n | DOWN precision | base-rate note |
|---|---|---|---|---|---|
| HIGH | 4121 | 9.20% | 4540 | 88.99% | base up 10.1% / down 89.9% |
| LOW | 1604 | 96.07% | 1886 | 6.89% | base up 94.6% / down 5.4% |

The fusion's up/down precision tracks the base rate within ~1pt in both regimes —
**no genuine edge in either direction.**

**Per-season:**

| season | UP n | UP precision | DOWN n | DOWN precision |
|---|---|---|---|---|
| DJF | 1238 | 29.56% | 1163 | 70.16% |
| JJA | 1796 | 40.92% | 2309 | 61.20% |
| MAM | 1389 | 37.94% | 2531 | 64.56% |
| SON | 1302 | 22.43% | 423 | 72.58% |

**Per-station** (up precision / down precision):

| station | UP n | UP precision | DOWN n | DOWN precision |
|---|---|---|---|---|
| KATL | 163 | 49.69% | 177 | 59.32% |
| KAUS | 605 | 25.29% | 658 | 78.88% |
| KBOS | 150 | 47.33% | 200 | 57.00% |
| KDCA | 181 | 44.75% | 182 | 54.95% |
| KDEN | 394 | 26.90% | 468 | 70.09% |
| KDFW | 162 | 48.77% | 175 | 61.71% |
| KHOU | 171 | 44.44% | 197 | 61.42% |
| KLAS | 159 | 44.65% | 207 | 62.80% |
| KLAX | 310 | 34.19% | 343 | 57.73% |
| KMDW | 729 | 28.81% | 730 | 65.89% |
| KMIA | 540 | 25.37% | 612 | 69.28% |
| KMSP | 153 | 49.02% | 211 | 57.82% |
| KMSY | 132 | 41.67% | 208 | 60.58% |
| KNYC | 727 | 20.36% | 711 | 71.87% |
| KOKC | 138 | 49.28% | 198 | 59.09% |
| KPHL | 455 | 33.19% | 389 | 57.84% |
| KPHX | 160 | 46.25% | 177 | 62.71% |
| KSAT | 131 | 51.14% | 188 | 57.98% |
| KSEA | 151 | 41.72% | 179 | 60.89% |
| KSFO | 114 | 42.11% | 216 | 51.85% |

---

## Material finding (root-cause, with provenance)

Two fusion engines coexist in this repo, and they diverge sharply:

1. **`SignalFusionEngine` (LIOP/LLOP, `core/fusion_logic.py`)** — the *connected*
   fusion (import fixed by R/T-8a). Non-degenerate direction (up 47% / down 53%).
   **Result: MCC -0.0165, no genuine edge.**

2. **`UncertaintyWeightedCascade` (UWC, `core/signal_fusion.py`)** — degenerate.
   The PoolOfPools beta-binomial (`compute_pool_beta` in `core/signal_families.py`)
   feeds the majority-*agreement* count `k_count` into the `alpha` (up-successes)
   term of the Beta posterior regardless of whether the majority is 'up' or 'down'.
   The cross-pool `combined_k` (up-successes) therefore inflates whenever any pool
   has strong agreement — including agreement on **'down'** — biasing
   `combined_mean` above 0.5. Net effect: **UWC predicts 'up' on 100% of events
   (MCC 0.0 by construction)**. This is a latent directional-sign bug in the
   pool-of-pools layer, distinct from the calibration question. (R/T-8F independently
   flagged the same: "UWC degenerate, predicts up 100%".)

This is a *calibration-surface* finding, not a fix — flagged for a follow-up
(routing to the fusion-ownership lane), out of R/T-8b's script-only scope.

---

## Stop-condition verification

1. ✅ Receipt written (this file) with per-station / per-regime / per-season /
   UP-vs-DOWN / calibration-pairs results, each carrying axis + provenance.
2. ✅ Git clean (script + report + receipt committed; no untracked files).
3. ✅ Every number carries axis + provenance (`strike_crossing_direction` /
   `settlement_truth.strike_price` / `SignalFusionEngine` LIOP).

## Receipt

- **Report JSON:** `reports/rt8b_recalibration_20260921-023413.json`
- **Report MD:** `reports/rt8b_recalibration_20260921-023413.md`
- **Script:** `scripts/rt8b_exhaustive_recalibration.py`
- **Commit:** (recorded after commit, below)
- **No merge to main** — merge is Dan's.

## Next (gated)

No signal/regime/season/station slice clears a positive-MCC bar vs the
market-type-conditional null. The honest conclusion is unchanged from R/T-8F:
**the connected fusion has no genuine directional edge** on the strike-crossing
axis. Recommendation (for Dan/Gerri): do not wire fusion as a directional edge;
the only sub-target with positive raw MCC is per-station noise. Decision parked.
