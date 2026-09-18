# B2.3 — Market-Threshold Parameter Grid: Freeze & Preregistration

**Doc ID:** B2-PARAM-GRID-FREEZE-2026-09-17
**Status:** PREREGISTERED (frozen — no post-hoc tuning permitted)
**Owner:** Ray / Gilfoyle (Weather Engine)
**Date:** 2026-09-17 UTC
**Workstream:** B2 — Real-market threshold validation
**Control-register item:** B2.3 (Freeze/preregister parameter grid)
**Branch context:** `temp-sprint-branch` @ `04be98a04`

---

## 1. Purpose

This document **freezes** the market-threshold parameter grid before any
backtest/sweep executes against real market prices. Freezing the grid
**before** evaluation makes the results reproducible and honest: no parameter
may be adjusted after observing results without a new, separately-registered
revision.

This is a preregistration artifact. Its whole value is that it is written
**before** B2.2 (real-price join) and B2.4 (execution metrics) run. The values
below are **fixed**, not ranges-to-explore.

---

## 2. Freeze discipline (the contract)

1. The grid below is immutable for the duration of the B2 sweep.
2. Any change requires a new doc `B2-PARAM-GRID-FREEZE-<date>-REV2.md` that
   (a) records the change, (b) states the reason, and (c) re-preregisters the
   full grid — it does **not** silently amend this doc.
3. Results are evaluated against the fixed success thresholds in §4. If a
   threshold is not met, the disposition is **fail-and-iterate**, not
   **tune-until-green**.
4. This freeze covers only the **market-threshold** grid (entry/edge/gating
   thresholds and evaluation gates). It does not freeze signal code, data
   lineage, or the 22-parameter sweep registry (which is already separately
   frozen in `PER-PARAMETER-SWEEP-DESIGN.md` §9).

---

## 3. The frozen grid

### 3.1 Canonical source

The fixed values below are drawn from the existing, committed canonical
configuration — **not** invented for this task:

- `config/regime_config_matrix.json` → `default_config.config` (harness-validated
  overrides, 2026-08-10 P0–P5 buildout)
- `docs/plans/PER-PARAMETER-SWEEP-DESIGN.md` §9 (22-parameter registry defaults)
- `docs/PHASE3-GRAY-ROOM-SYNTHESIS.md` (analog-count and confidence floors)
- `docs/plans/FORMAL-SUCCESS-CRITERIA.md` and `docs/plans/PROMOTION-RULES.md`
  (accuracy/Sharpe gates)

### 3.2 Entry / edge thresholds

| Parameter | Frozen value | Unit | Rationale |
|---|---|---|---|
| `edge_threshold` | **0.05** | confidence (edge) | Canonical `default_config` value; also `PER-PARAMETER-SWEEP-DESIGN.md` registry default (range 0.001–0.20). Minimum adjusted edge required to qualify a position. |
| `confidence_floor` | **0.60** | probability | Canonical default; below this a signal's calibrated confidence is not actionable. |
| `entry_price_min` | **0.08** | price delta | Canonical harness-validated override (registry default was 0.01; the validated matrix pins 0.08 as the lower entry-price bound). |
| `entry_price_max` | **0.50** | price delta | Canonical default (registry range 0.05–0.50). Upper entry-price bound. |
| `min_analogs` | **3** | count | Gray Room synthesis: "Minimum 3 analogs required before forecast generation" (never predict from a single match). |

### 3.3 Gating / agreement thresholds

| Parameter | Frozen value | Rationale |
|---|---|---|
| `agreement_n` | **2** | Registry default; minimum number of agreeing signal directions required. |
| `agreement_m` | **7** | Registry default; total signals in the N-of-M agreement gate. |
| `mod_gate_min_agreement` | **0.55** | Canonical harness override; minimum fused directional agreement fraction. |
| `mod_directional_weight` | **0.70** | Canonical harness override; directional-signal weight in fusion modulator. |
| `mod_agreement_exponent` | **1.5** | Canonical harness override; agreement weighting exponent. |

### 3.4 Position sizing / risk

| Parameter | Frozen value | Rationale |
|---|---|---|
| `position_sizing_model` | **0 (fixed)** | Canonical default; fixed sizing for the B2 real-price sweep (no Kelly escalation until calibration truth is proven). |
| `kelly_fraction` | **0.5** | Registry default; inactive when `position_sizing_model=0` but pinned so it is reproducible if a Kelly lane is later enabled under a new revision. |
| `max_contracts` | **175** | Canonical harness override (registry default 100); position-size ceiling. |
| `capital_base` | **5000** | Canonical harness override (USD); notional base for metric normalization. |
| `stop_loss_kind` | **0 (none)** | Registry default; no stop-loss in the B2 sweep (held-to-settlement evaluation; exit logic is separately L6). |

### 3.5 Cost / fee model

| Parameter | Frozen value | Rationale |
|---|---|---|
| `fee_type` | **0 (none)** | Registry default; real Kalshi fee schedule is applied at the L7/real-quote layer, not inside the threshold sweep. |
| `fee_deduction` | **0.5** | Registry default; inactive when `fee_type=0` but pinned for reproducibility. |
| `slippage_budget` | **0.001** | Registry default; fraction slippage reserve. |

### 3.6 Calibration / fusion / modulators

| Parameter | Frozen value | Rationale |
|---|---|---|
| `calibration_mode` | **1 (ece-binned)** | Canonical harness override (registry default 0=platt); ECE-binned calibration selected in the validated matrix. |
| `cal_bin_width` | **0.05** | Canonical harness override; ECE bin width. |
| `cal_window_days` | **180** | Canonical harness override; calibration rolling window. |
| `cal_min_samples` | **20** | Canonical harness override; minimum samples before calibration is trusted. |
| `cal_shrinkage_strength` | **0.0** | Canonical harness override; no shrinkage applied in B2. |
| `fusion_mode` | **0 (average)** | Registry default; simple average fusion for the threshold sweep. |
| `member_weighting` | **3 (combined)** | Canonical harness override (registry default 0=uniform); ensemble-member weighting scheme. |
| `spatial_coherence_enabled` | **0** | Registry default; spatial-coherence modulator disabled for the threshold sweep (evaluated separately). |
| `goldilocks_lane_enabled` | **0** | Registry default; Goldilocks lane disabled in B2. |
| `trajectory_lane_enabled` | **1** | Canonical harness override (registry default 0); trajectory lane enabled in the validated matrix. |
| `nowcast_enabled` | **0** | Canonical harness override; nowcast disabled in B2. |
| `nowcast_min_confidence` | **0.25** | Canonical harness override (inactive while `nowcast_enabled=0`). |
| `nowcast_boost_cap` | **0.05** | Canonical harness override (inactive while `nowcast_enabled=0`). |
| `nowcast_lead_hours` | **3** | Canonical harness override (inactive while `nowcast_enabled=0`). |
| `kl_dro_lambda` | **0.5** | Registry default; KL-regularization weight (DRO). |
| `holdout_start_frac` | **0.7** | Registry default; holdout split fraction for validation. |

### 3.7 Regime filters

| Parameter | Frozen value | Rationale |
|---|---|---|
| `regime_season_filter` | **all** | No season filter in the B2 threshold sweep (seasonal regime is a separate axis, already enumerated in the regime matrix). |
| `regime_region_filter` | **all** | No region filter in the B2 threshold sweep. |

---

## 4. Success thresholds (evaluation gates)

These are the **fixed** gates the B2 results are evaluated against. They are
separate from (and stricter than) the broader Gray Room thresholds where the
market-threshold sweep is the first real-price confirmation.

| Metric | Threshold | Source |
|---|---|---|
| Directional accuracy (vs Kalshi settlement) | **≥ 58%** | Gray Room hard gate (`FP-MULTI-SIGNAL-FUSION.md`; TOOLS.md) |
| Calibrated confidence (prediction confidence) | **≥ 65%** | Gray Room threshold; promotion floor (`PROMOTION-RULES.md`) |
| Sharpe ratio | **≥ 1.0** | Gray Room threshold; promotion floor (`CONTINUOUS-KELLY-SPEC.md`, `PROMOTION-RULES.md`) |
| Minimum analogs per forecast | **≥ 3** | Gray Room synthesis (`PHASE3-GRAY-ROOM-SYNTHESIS.md`) |
| Label-permutation / luck-elimination | **p < 0.01 or accuracy > 2σ above shuffle mean** | `FORMAL-SUCCESS-CRITERIA.md` Criterion 1 |

**Disposition rule:** If a config in the frozen grid fails any gate, the result
is recorded as a **fail**, not a trigger to adjust the grid. Only a new,
separately-registered revision may change a value.

---

## 5. What this freeze does NOT cover

- Signal-family code and logic (owned by the signal register).
- Data lineage / provenance (owned by S-track).
- The 22-parameter sweep registry in `PER-PARAMETER-SWEEP-DESIGN.md` §9 (already
  independently frozen).
- Live DB, daemon, paper trading, or live trading — all out of scope for this
  doc and for the B2 sweep itself (monitor/shadow mode only).

---

## 6. Verification / receipt

- **File:** `docs/plans/B2-PARAM-GRID-FREEZE-2026-09-17.md` (this doc).
- **Scope honored:** only this single doc created; no code, config, data, or
  other docs modified.
- **Freeze basis:** values transcribed from committed canonical config
  (`config/regime_config_matrix.json`, `PER-PARAMETER-SWEEP-DESIGN.md` §9,
  Gray Room synthesis) — not invented.
- **No backtests run, no parameters tuned, no live DB/daemon touched.**
