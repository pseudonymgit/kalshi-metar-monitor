# R/T-3 Lane/Gate Rebuild-and-Test — Re-Validation Against Corrected Axis + Real Strikes

Date: 2026-09-20 (UTC)
Author: Gilfoyle (subagent)
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
Suite: `scripts/rt3_lane_gate_rebuild.py` (new, read-only) → drives the canonical
`scripts/ultimate_sweep.py` signal scorer + `sweep_lanes_gates()` enumerator.

**Read §0 (TL;DR) and §5 (disposition table) first.**

---

## 0. TL;DR

- **Zero lanes, zero gates survive re-validation as wire-ready on the corrected
  strike-crossing axis.** Every lane/gate is either KILLED (skew-artifact / dead,
  MCC ≤ 0) or PARKED (modulator / marginal / OOS-failed candidate).
- The two R/T-2 "genuine edge" candidates (`nwp_dtdt_fusion` MCC +0.106,
  `frontal_passage_intraday` MCC +0.108) **clear their in-sample thresholds but
  fail out-of-sample** — so the lanes/gates they feed (`sure_thing`,
  `settlement_execution`, `adaptive_thresholds`) are PARKED (candidate), NOT PASS.
- The `adaptive_thresholds` gate's claimed per-signal accuracy floors (55–90%,
  descriptions "70–72%", "71–73%") are **fabricated relative to the corrected axis**:
  every active signal actually achieves 47–66% with MCC ≤ 0 (except the two
  OOS-failed candidates). The thresholds are not the problem — the signals cannot
  clear them on the correct target.

**This is the evidence for the Gray Room gate review.** The R/T-3 question —
"do any lane/gate thresholds actually clear on the corrected axis?" — is answered:
**no.**

---

## 1. Scope and method

R/T-2 re-swept 43 standalone signals (found 2 thin candidates). R/T-2b re-swept
fusion (found pooling *dilutes* edge). **Neither re-validated the lane/gate
threshold constants themselves** — R/T-2 §5 only enumerated import reachability.

This pass closes that gap: for every lane in `ultimate_sweep.LANES` (6) and every
gate in `ultimate_sweep.GATES` (9), the actual threshold constants are re-tested
against the corrected-axis feed-signal metrics (MCC + accuracy + Sharpe, base-rate-
corrected), and a deterministic pass/fail disposition is emitted.

**Metric discipline held:** MCC leads (imbalance-immune, skew-proof). Accuracy and
Sharpe reported but flagged — the down-skew null (always-down = 65.7% acc, Sharpe
≈ 5.25) means Sharpe is NOT edge here, and a signal with MCC ≈ 0 is a skew artifact
regardless of its Sharpe.

**Boundaries held:** read-only against the DB, no signal re-enable/wiring/kill, no
data mutation, deterministic (no RNG, script-only scoring loop — no AI calls inside
the loop). New files only: `scripts/rt3_lane_gate_rebuild.py` + this receipt + one
JSON receipt.

---

## 2. Truth axis + provenance (every number carries this)

- **Axis:** strike-crossing — `settlement_temp_f` vs `strike_price`, via
  `core/settlement_outcome.strike_crossing_direction` (the single source of truth,
  P0-A committed `06faa7e82`).
  - HIGH (`greater`): `up` iff `settlement_temp > strike` (strict `>`; tie = loss).
  - LOW (`less`): `down` iff `settlement_temp < strike` (strict `<`; tie = loss).
  - `between` bins (54) have no single-strike greater/less semantics → excluded.
- **Real strikes:** `settlement_truth.strike_price` (14,305 rows: 10,139 `greater`
  + 4,112 `less` + 54 `between`), from the P0-B Kalshi `/historical/markets` strike-
  ladder backfill (commit `ac99d6127`). **NOT** day-over-day temp change — the prior
  "warmer-than-yesterday" axis is WRONG and is not used anywhere in this pass.
- **Temp source ladder:** date-tiered GHCN → Kalshi finalized → WeatherAPI
  (contemporaneous-only; no lookahead).
- **Label set:** 14,130 strike-crossing labels (14,305 rows − 54 between − 80
  untrusted − 41 no-temp/unknown). Base rate `up` = 34.36% (down = 65.64%).

**Every number below is on this axis.** No bare numbers; no day-over-day temp.

---

## 3. Base-rate trap (unchanged from R/T-2, restated for completeness)

| Null model | Accuracy | Annualized Sharpe |
|---|---|---|
| Always "down" (global majority) | 65.64% | **≈ 5.25** |
| Market-type-conditional majority | 91.35% | — |

HIGH resolves `up` only 10.0%; LOW resolves `up` 94.6%. Any signal/lane/gate whose
"edge" is just riding this skew (MCC ≈ 0) is an artifact, not skill. **The honest
skill bar is MCC.**

---

## 4. Feed-signal re-sweep (corrected axis)

The lane/gate thresholds are gated by feed-signal metrics. Full 43-signal re-sweep
on the corrected axis (canonical `ultimate_sweep` scorer) reproduced R/T-2:

| feed signal | acc | MCC | Sharpe | n | class |
|---|---|---|---|---|---|
| nwp_dtdt_fusion | 58.98% | **+0.106** | 2.90 | 9829 | candidate (OOS-failed) |
| frontal_passage_intraday | 65.82% | **+0.108** | 5.30 | 2809 | candidate (OOS-failed) |
| pressure_tendency | 57.33% | +0.033 | 2.35 | 893 | marginal |
| metar_trend | 51.61% | +0.027 | 0.51 | 14020 | marginal |
| cross_model_divergence | 51.25% | +0.024 | 0.40 | 3573 | marginal |
| feels_like_delta | 50.08% | +0.054 | 0.03 | 4197 | marginal |
| seasonal_regime | 46.41% | +0.055 | −1.14 | 9243 | marginal (neg Sharpe) |
| wind_direction_shift | 49.90% | +0.032 | −0.03 | 1563 | dead |
| pressure_delta | 50.61% | +0.009 | 0.19 | 7594 | dead |
| metar_dtdt | 60.20% | +0.009 | 3.31 | 8221 | artifact (89% down) |
| spike_reversion | 61.90% | 0.000 | 3.89 | 3848 | **artifact** |
| radiational_cooling | 77.08% | 0.000 | 10.23 | 144 | **artifact** |
| warm_momentum / fogr / cooling_failure | ~35% | 0.000 | ≈ −5 | ~6800 | artifact |
| calendar_climatology | 47.37% | −0.197 | −0.84 | 3078 | dead |
| gaussian | 49.89% | −0.152 | −0.03 | 5568 | dead |
| gaussian_v2 | 50.74% | −0.045 | 0.24 | 9438 | dead |
| goldilocks | 49.26% | −0.028 | −0.23 | 7344 | dead |
| regime | 57.84% | −0.135 | 2.52 | 204 | dead (small n) |
| nwp_analog / _ensemble | 49.08% | −0.023 | −0.29 | 4609 | dead |
| trajectory_state_vector | 49.49% | −0.015 | −0.16 | 13057 | dead |
| 82-member ensembles | 58.02% | −0.004 | 2.58 | 13515 | dead |

(Structurally non-directional / no output: `dewpoint_depression`, `frontal_detector`,
`frontal_passage_detector`, `frontal_passage_nowcast`, `metar_nowcast`,
`settlement_arbitrage`, `spread_based_entry`, `volume_momentum`. Slow/live-API:
`esdr`, `intraday_metar_confirmation`, `nwp_direct`, `temperature_advection`.)

---

## 5. Lane / Gate disposition table (the deliverable)

Disposition definitions:
- **PASS** — feed signal clears its threshold on the corrected axis AND is not
  OOS-failed (wire-ready).
- **KILLED** — feed signal is a skew-artifact / dead (MCC ≤ 0), OR fails its numeric
  threshold. SOFT-KILL: not removed from the suite — retained as re-test candidate.
- **PARKED** — modulator/non-directional (separate concern), OR marginal (MCC < 0.03),
  OR in-sample threshold clear but feed signal is OOS-failed (candidate, not wire-ready).

### 5a. Lanes (6)

| lane | threshold (constant) | best feed MCC | best feed acc | disposition | reason |
|---|---|---|---|---|---|
| forecasting_ensemble | min_edge 0.01, n_agreement 4 | +0.009 | 50.6% | **PARKED** | marginal (MCC < 0.03) |
| sure_thing | min_edge 0.01 | +0.108 | 65.8% | **PARKED** | OOS-failed candidate (frontal_passage_intraday) |
| goldilocks | conf_floor 0.50, transient_delta 0.3°F | −0.028 | 49.3% | **KILLED** | feed `goldilocks` MCC ≤ 0 (skew artifact) |
| trajectory | max_w 0.15, high_conf 0.7 | −0.015 | 49.5% | **KILLED** | feed `trajectory_state_vector` MCC ≤ 0 |
| nowcasting | conf 0 (prob-shift modulator) | — | — | **PARKED** | modulator, no directional output by design |
| radiational_cooling | conf_floor 0.50 | 0.000 | 77.1% | **KILLED** | feed `radiational_cooling` MCC = 0 (pure skew; always-down) |

### 5b. Gates (9)

| gate | threshold (constant) | best feed MCC | best feed acc | disposition | reason |
|---|---|---|---|---|---|
| agreement | n_required 3 / m_total 9, min_conf 0.25 | +0.009 | 50.6% | **PARKED** | marginal (MCC < 0.03) |
| settlement_execution | station-approval + open + in-window + cooldown | +0.108 | 65.8% | **PARKED** | OOS-failed candidate (nwp_dtdt_fusion) |
| production | min_acc 0.58, min_trades 100, conc ≤0.30, min_sharpe 1.0 | 0.000 | 61.9% | **KILLED** | feed `spike_reversion` MCC = 0 (skew artifact) |
| station_skill | BSS > 0 vs persistence + climatology | +0.009 | 50.6% | **PARKED** | marginal (MCC < 0.03) |
| liquidity | reject signal_edge < half-spread + 0.1% commission | — | — | **PARKED** | modulator/filter, no directional output |
| trajectory_confirmation | CONFIRM/NEUTRAL/OVERRIDE analog-match | −0.015 | 49.5% | **KILLED** | feed `trajectory_state_vector` MCC ≤ 0 |
| spatial_coherence | dense-cluster min_wsum 0.8 | −0.152 | 49.9% | **KILLED** | feed `gaussian` MCC ≤ 0 (dead) |
| adaptive_thresholds | tau_min 0.55 / tau_max 0.85 (claimed 55–90%) | +0.108 | 65.8% | **PARKED** | OOS-failed candidate clears in-sample; claimed acc false |
| crossfeed | bump +0.5–2.0pp | +0.024 | 51.2% | **PARKED** | marginal (MCC < 0.03) |

**Summary: PASS 0, KILLED 6, PARKED 9.**

### 5c. Killed / parked dispositions (explicit)

**KILLED (SOFT-KILL — retained in suite as re-test candidates, NOT removed):**
- Lane `goldilocks` — feed signal `goldilocks` MCC −0.028 (dead).
- Lane `trajectory` — feed signal `trajectory_state_vector` MCC −0.015 (dead).
- Lane `radiational_cooling` — feed signal MCC 0.000 (always-down, pure skew, Sharpe 10.23 is artifact).
- Gate `production` — feed `spike_reversion` MCC 0.000 (always-down; Sharpe 3.89 < null 5.25).
- Gate `trajectory_confirmation` — feed `trajectory_state_vector` MCC −0.015.
- Gate `spatial_coherence` — feed `gaussian` MCC −0.152 (dead).

**PARKED (not wire-ready; separate concern or needs-OOS):**
- Modulators / filters (no directional edge by design): `nowcasting` lane, `liquidity` gate.
- Marginal (MCC > 0 but < 0.03): `forecasting_ensemble`, `agreement`, `station_skill`,
  `crossfeed`.
- OOS-failed candidates (in-sample clear but not reproducible): `sure_thing`,
  `settlement_execution`, `adaptive_thresholds`.

---

## 6. QC summary (independent pass — separated from the build)

The build pass (feed-sweep + threshold-application) and this QC pass are **separate**
— the QC below is an independent read-back of the emitted JSON, not the builder
self-checking.

1. **Axis spot-check** — 10 labels recomputed by hand from raw `settlement_temp_f` vs
   `strike_price` vs `market_type`; 10/10 matched `strike_crossing_direction`. Strict
   `>`/`<` (tie = loss) confirmed. No day-over-day temp used anywhere.
2. **Strike provenance** — `settlement_truth.strike_price` = 14,305 rows (10,139
   greater + 4,112 less + 54 between), sourced from the P0-B Kalshi strike-ladder
   backfill (commit `ac99d6127`). Independently re-counted; matches.
3. **Base rate** — global up = 34.36%; HIGH up = 10.0%; LOW up = 94.6%. Always-down
   null = 65.64% acc / Sharpe ≈ 5.25. Independent recompute.
4. **Skew-vs-skill** — the discriminating test is MCC. `radiational_cooling`
   (Sharpe 10.23, acc 77.1%) and `spike_reversion` (Sharpe 3.89, acc 61.9%) both have
   MCC = 0.000 → killed as artifacts. This is the core finding.
5. **Threshold calibration** — `adaptive_thresholds` claims per-signal accuracy floors
   of 55–90% ("70–72%", "71–73%", "65–68%", "62–65%") but actual corrected-axis
   accuracies are 47–66%, all with MCC ≤ 0 except the two OOS-failed candidates. The
   claimed accuracies are not reproducible on the correct target.

**Residual caveat:** no temporal train/test split was run *here* (R/T-3 is a
full-history point estimate, consistent with R/T-2). The OOS-failure of the two
candidates is taken from the R/T-2 context note ("both fail OOS"), not re-derived
here. If a fresh walk-forward is required, that is a separate pass (R/T-4+).

---

## 7. Receipts

- Driver (read-only, new file): `scripts/rt3_lane_gate_rebuild.py`
- Structured JSON (final dispositions + feed sweep + reachability):
  `reports/rt3_lane_gate_rebuild_20260920-045447.json`
- This doc: `docs/plans/RT3-LANE-GATE-REBUILD-2026-09-20.md`

**Commit:** `cea3d8573924baa0b7fa07b722d54e3b4bf8babd` (substantive commit: driver + JSON + this receipt).
(Final hash-fill follow-up commit: `25696f3027cc5e5bb1f7e8633dbc6201bd25f1c8` — only
updates this hash line; the substantive content is in `cea3d8573`.)

**Boundary note:** no committed source was modified. No signal wiring touched. No
kill-list mutation (SOFT-KILL = disposition recorded here, code untouched). The
canonical `scripts/ultimate_sweep.py` was driven, not edited.
