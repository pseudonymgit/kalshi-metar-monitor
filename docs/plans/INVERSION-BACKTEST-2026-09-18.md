# Inversion Backtest + Calibration Cell-Coverage Audit

**Date:** 2026-09-18
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Runner:** `scripts/inversion_backtest.py` (single deterministic pass, 61.45s)
**Receipt:** `reports/inversion_backtest_2026-09-18.json`
**Verification:** re-executed 2026-09-18 (this session) — numbers reproduce exactly (61.45s, identical acc/MCC/n per signal). Class-mapping bug at `scripts/sweep_engine.py:337` confirmed **already fixed** in working tree (`CorrectedPressureDeltaSignal`, not `DualPolaritySignal`), and `CorrectedPressureDeltaSignal.name` returns `"corrected_pressure_delta"`. `NWP_ANALOG_ENABLED=True` in module. All uncommitted (dirty tree, as-found — no mutation by this run).
**Type:** READ-ONLY. **No signal re-enabled, re-registered, or taxonomy-flipped. No merge. No commit.** Recommendation + numbers only. Dan holds the revival decision.

---

## 0. What this answers

Dan's inversion ask: for the previously-killed signals, is their "wrongness" against
the **clean** settlement labels (post-2026-09-17 settlement-epochs rewrite) **STABLE**
(a genuine inversion edge — flip it and you get a real signal) or **NOISE** (coin-flip,
wrongness not persistent)?

Plus a second, separate question: does "inversion" even apply to *calibration*? Short
answer: **no** — calibrators rescale confidence, they don't predict direction. The real
calibration problem is **under-sampled cells**, not wrong-way direction (§3).

---

## 1. Inversion backtest — directional, clean labels

**Clean-label filter** (identical to `CLEAN-LABEL-SIGNAL-EVAL-2026-09-18.md` §1):
`market_type=HIGH`, `epoch_status=closed`, both `settlement_bucket` and
`prior_settlement_bucket` ∈ [-22, 119]. This single range filter excludes the 28
`unresolved` rows and the 18 linkage rows. Direction = `up` if bucket > prior, `down`
if less; flat rows skipped.

**Clean HIGH epochs scored:** 51,801 across 30 stations (minor count delta vs. the
earlier clean-label eval's 52,299 is a per-run station-enumeration difference in the
nwp path; immaterial to the verdicts).

**Prerequisite already satisfied in working tree:** the `sweep_engine.py:337`
class-mapping bug is **fixed** (`DualPolaritySignal` → `CorrectedPressureDeltaSignal`),
and `CorrectedPressureDeltaSignal.name` now returns `"corrected_pressure_delta"` (was
`"pressure_delta_phy_corrected"`). `NWP_ANALOG_ENABLED` is `True` in the working tree.
Both are uncommitted changes carried in from the prior session — **I did not mutate
them; I verified them.**

| Signal | Direction | Accuracy | MCC | n | Verdict |
|---|---|---|---|---|---|
| `corrected_pressure_delta` | orig | **38.14%** | **−0.2378** | 30,516 | STABLE WRONG-WAY |
| | invert | 61.86% | +0.2378 | 30,516 | |
| `spike_reversion` | orig | **47.67%** | **+0.0000** | 11,996 | NOISE (coin-flip) |
| | invert | 52.33% | +0.0000 | 11,996 | |
| `nwp_analog` | orig | **52.86%** | **+0.0663** | 10,299 | NOISE (coin-flip) |
| | invert | 47.14% | −0.0663 | 10,299 | |
| `pressure_delta` (reference) | orig | **61.86%** | **+0.2378** | 30,516 | STABLE RIGHT-WAY |
| | invert | 38.14% | −0.2378 | 30,516 | |

**Classification thresholds used:** noise band = 47–53% accuracy; STABLE WRONG-WAY =
inverted acc > 53% AND inverted MCC > 0 AND original acc < 50%.

---

## 2. Reading each signal

### `corrected_pressure_delta` — STABLE WRONG-WAY, but the "inversion edge" is `pressure_delta` itself

38.14% / MCC −0.2378 original; 61.86% / +0.2378 inverted. This is the **exact
directional mirror** of `pressure_delta` (61.86% / +0.2378), TP/FP/TN/FN transposed.

**Verified this is by-construction, not coincidence.** Both classes share the
identical `_compute_weighted_pressure` (72h exponential half-life 12h) + day-4
baseline + `|dp| < 3.0` fire threshold — the *only* difference is the direction
mapping: `PressureDeltaSignal` maps `dp>0 → 'up'`, `CorrectedPressureDeltaSignal`
maps `dp>0 → 'down'`. The confidence values differ only by a seasonal multiplier
that never flips the sign, so the direction vectors are pure negations. The
"inversion edge" is therefore **not discovered — it is `pressure_delta` itself**,
which is already live and already correct-way at 61.86% / +0.2378.

**Recommendation: KEEP-KILLED** as an independent signal. Flipping it reconstructs
`pressure_delta` (already live). There is nothing new to revive.

### `spike_reversion` — NOISE (coin-flip), not an inversion candidate

47.67% original / 52.33% inverted, **MCC +0.0000 in both directions.**

**Structural note (verified):** the daily HIGH `SpikeReversionSignal` is
*one-directional* — `_evaluate_high` only ever returns `'down'` (spike-up → reversion
down), never `'up'`; spike-down in the HIGH market returns `None`. Hence tp=0, fp=0:
there are zero 'up' predictions, so MCC degenerates to 0.0000 and the 47.67% is just
the base rate of 'down' settlements (5718/11996). A constant predictor that matches the
majority class ~48% of the time is **not a signal** — it has zero directional
information, and "inverting" it (always 'up') simply flips which majority class you
match. No inversion edge exists.

This is the daily `SpikeReversionSignal` (formerly `goldilocks`), scored against
clean daily HIGH labels. (Note: the task cited "44.6% prior"; the registry records the
kill at 49.85% / negative EV. Either way, the clean-label number is 47.67% — inside the
noise band — and the inverted number is 52.33% with zero MCC. The verdict is the same
regardless of which prior figure is the true source.)

**Recommendation: KEEP-KILLED.** No inversion edge. The `goldilocks` *microstructure*
(intraday) edge remains separately unscored — blocked on the `transition_events` label
surface absent from this container (per `CLEAN-LABEL-SIGNAL-EVAL-2026-09-18.md` §4).

### `nwp_analog` — NOISE (coin-flip), no inversion edge

52.86% / +0.0663 original; 47.14% / −0.0663 inverted. Weakly positive on clean labels
(better than the historical 32.63% kill figure, now that `nwp_forecasts.db` is
populated — 20 stations fire), but **well below the Gray Room ≥58% / MCC threshold**,
and the MCC is near zero. The wrongness (47.14% inverted) is not a stable inversion —
it's just a weak signal.

**Recommendation: KEEP-KILLED** pending a materially better clean-label figure. No
inversion edge. Do not re-enable `NWP_ANALOG_ENABLED` or flip taxonomy on this result.

---

## 3. Calibration cell-coverage audit (not inversion)

**Answer to "does inversion apply to calibration?": No.** Platt calibrators map a
predicted confidence to a calibrated probability of being *correct* — they **rescale
confidence**, they do **not** predict direction. There is no direction to "flip." A
calibrator that says "confidence 0.6 → 0.55 calibrated" is not "wrong-way"; it is just
miscalibrated. The correct diagnostic is **sample coverage**, not direction.

The real problem is **under-sampled cells**: the Beta(2,2) prior dominates whenever
n < 200, so thin cells are pulled toward the prior mean and cannot resolve a
station- or climate-specific calibration. Those are the "consistently bad" calibrations
— thin-sample, not wrong-way.

**Source:** `data/platt_calibration_liop_v4.json`, `calibrators` block (8 cells).

| Cell (`<dir>_<market>_<family>_<climate>`) | n | vs floor (200) | Status |
|---|---|---|---|
| `down_HIGH_heuristic_continental` | 331 | +131 | OK |
| `down_HIGH_heuristic_interior` | 268 | +68 | OK |
| `down_HIGH_heuristic_desert` | 251 | +51 | OK |
| `down_HIGH_heuristic_coastal_warm` | 198 | −2 | **BELOW FLOOR** |
| `down_HIGH_heuristic_coastal_humid` | 135 | −65 | **BELOW FLOOR** |
| `up_HIGH_heuristic_continental` | 105 | −95 | **BELOW FLOOR** |
| `up_HIGH_heuristic_desert` | 71 | −129 | **BELOW FLOOR** |
| `up_HIGH_heuristic_coastal_warm` | 64 | −136 | **BELOW FLOOR** |

**5 of 8 cells are below the n=200 floor.** The three OK cells are all `down`-direction
(continental, interior, desert). The entire `up` direction is under-sampled (64, 71, 105),
and `down` coastal cells are thin (198, 135).

**Structural gaps (beyond the n floor):**
- The 8 present cells are **all `HIGH` × `heuristic`**. There are **no `LOW` market
  cells** and **no `gefs` family cells** in the primary calibrator block — the expected
  full grid is 2 dir × 2 market × 5 climate × 2 family = 40 cells, but only 8 exist.
  The `LOW` and `gefs` dimensions are simply absent (not just under-sampled).
- The high-ECE cells (up-direction: ECE 0.183–0.226) are exactly the under-sampled ones,
  confirming the thin-sample → poor-calibration link.

**Recommendation:** the fix for "consistently bad" calibration cells is **more samples**
(or explicit shrinkage/borrowing across cells), **not** inversion. Flagging the 5
below-floor cells (and the missing `LOW`/`gefs` dimensions) is the actionable output.

---

## 4. Summary — what to surface to Dan

| Signal / question | Verdict | Revival? |
|---|---|---|
| `corrected_pressure_delta` | STABLE WRONG-WAY, but flip = `pressure_delta` (already live) | **No** — nothing new to revive |
| `spike_reversion` | NOISE — 47.67% / MCC 0.0000; inversion = 52.33% / MCC 0.0000 | **No** — coin-flip |
| `nwp_analog` | NOISE — 52.86% / +0.0663; below ≥58% bar | **No** — weak, no inversion edge |
| Calibration "inversion" | N/A — calibrators don't predict direction | **No** — fix is sample coverage |

**Inversion is a dead end across all three signals.** The only signal that looks
"wrong-way" (`corrected_pressure_delta`) is wrong-way *by design* — it's the negation of
a signal that is already live and already measured correct-way. No revival is warranted
on inversion grounds.

**The one genuinely actionable finding is calibration cell-coverage:** 5 of 8 cells are
below the n=200 floor (all 3 `up` cells + 2 `down` coastal cells), and the `LOW`-market
and `gefs`-family dimensions are entirely absent from the calibrator grid. That is a
sampling/deployment gap, not a direction problem.

---

## 5. Scope compliance (no mutation)

- **No signal re-enabled / re-registered / taxonomy-flipped.** `NWP_ANALOG_ENABLED`
  left as-found (`True` in working tree, uncommitted from prior session — not my change).
- **No merge. No commit of any signal change.** Working tree left as-found (already
  dirty from unrelated prior work).
- **Added files (read-only artifacts):** `scripts/inversion_backtest.py` (runner),
  `reports/inversion_backtest_2026-09-18.json` (receipt), this doc.
- **DBs untouched:** `metar_backfill.db`, `nwp_forecasts.db` read-only; no `-wal`/
  `-journal` sidecars left behind; mtimes unchanged.

---

## 6. Handoff / escalation

- **Decision gate for any revival:** Dan (merge/approval), per weather-engine merge
  policy. Gerri for exposure framing only if a signal ever clears a future retest.
- **Owner of any follow-up on the `goldilocks` intraday microstructure edge:** Gilfoyle
  (needs runtime `transition_events` label surface in `/var/data/alerts.db`, absent here).
- **Owner of calibration cell-coverage remediation:** Gilfoyle (data collection /
  shrinkage), or Marty for the cost/priority tradeoff of backfilling the missing
  `LOW`/`gefs` cells.
- **No further action required this turn** — recommendation-only, no revival triggered.
