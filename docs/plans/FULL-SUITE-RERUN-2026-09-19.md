# FULL-SUITE RE-RUN — 2026-09-19

## Verdict (TL;DR)

**Deterministic full re-run complete.** Three correctness fixes (axis, NWP °C→°F unit, contemporaneous lookahead gate) landed on `temp-sprint-branch`. The net effect on the entire test suite is:

1. **Zero already-scored signals moved.** Every signal that was scored in the prior run has *identical* MCC / accuracy / n / Sharpe — the corrections did **not** perturb any existing conclusion.
2. **11 signals un-blocked from NO_FIRES / NOT_REACHABLE → now score.** These were previously silent (never produced a direction) because of the unit seams + contemporaneous gate + registry re-export gaps. They now fire.
3. **Zero conclusion flips (edge ↔ no-edge).** None of the 11 newly-fired signals shows edge (all `NO_EDGE`, MCC ≤ +0.055). No signal that was `NO_EDGE` became `PASS`, and none that was `PASS` (none existed) degraded.

**Bottom line: the corrections fixed *coverage* (made 11 signals testable), not *conclusions*.** No previously-held "edge" was invalidated, and no new edge emerged. The suite remains uniformly no-edge under the honest MCC/base-rate metric.

---

## Scope & Method

- **Harness:** `scripts/ultimate_sweep.py` (R/T-1 canonical), run WITHOUT `--dry-run` → full scoring.
- **Axis:** strike-crossing — `settlement_temp_f` vs `strike_price` via `core/settlement_outcome.strike_crossing_direction`.
- **Ground truth:** `settlement_truth.strike_price` (14,305 rows; 14,130 usable labels after exclusions).
- **Deterministic:** fixed ordering, no RNG in scoring loop.
- **Read-only:** no signal re-enable/wiring; no storage bloat.

### The three corrections (from git history, landed between the two runs)

| # | Fix | Commits |
|---|---|---|
| 1 | Contemporaneous NWP gate applied to ALL signals reading `nwp_forecasts` (`fetch_date <= target_date`) | `3efcde780`, `4ec0ec7d8` (ERA5 lookahead removal from calendar_climatology) |
| 2 | NWP °C→°F unit + registry seams repair (un-blocks NO_FIRES signals) | `6dbdaade4`, `18901d56d`, `d4ed3b546` |
| 3 | Axis correction + DAL/DFW→KDFW + ASOS sub-hourly source | `4e6020dd1`, `e84f202ab`, `bc4ef27ed` (and earlier 12:37–14:18 axis work) |

### Labels (identical in both runs — axis was already correct in the "before" run)

- **n = 14,130** (up = 4,855, down = 9,275); base-rate UP = 34.36%.
- By market type: HIGH base-rate up = 10.1% (n=10,079); LOW base-rate up = 94.6% (n=4,051).
- temp_source: ghcn 13,889 / kalshi_finalized 79 / weatherapi 162.
- Excluded: between 54, untrusted 80.

---

## BEFORE / AFTER — Full Signal Delta

**BEFORE** = `reports/ultimate_sweep_20260919-123629` (12:15:56Z, pre-correction)
**AFTER** = `reports/ultimate_sweep_20260919-212026` (20:56:11Z, post-correction)

### A. Signals that CHANGED (status transition)

| signal | BEFORE | AFTER | acc | MCC | Sharpe | n |
|---|---|---|---|---|---|---|
| ai_composite | NO_FIRES | NO_EDGE | 50.67% | +0.0130 | 0.21 | 4823 |
| cloud_cover_index | NO_FIRES | NO_EDGE | 55.50% | +0.0107 | 1.76 | 6054 |
| dual_polarity | NO_FIRES | NO_EDGE | 49.41% | -0.0088 | -0.19 | 7602 |
| eighty_two_member_ensemble | NO_FIRES | NO_EDGE | 58.02% | -0.0040 | 2.58 | 13515 |
| eighty_two_member_ensemble_ece | NO_FIRES | NO_EDGE | 58.02% | -0.0040 | 2.58 | 13515 |
| eighty_two_member_ensemble_pooled | NO_FIRES | NO_EDGE | 54.13% | -0.0043 | 1.32 | 13515 |
| feels_like_delta | NO_FIRES | NO_EDGE | 50.08% | +0.0537 | 0.03 | 4197 |
| nwp_analog | NO_FIRES | NO_EDGE | 49.08% | -0.0225 | -0.29 | 4609 |
| nwp_analog_ensemble | NO_FIRES | NO_EDGE | 49.08% | -0.0225 | -0.29 | 4609 |
| seasonal_regime | NOT_REACHABLE | NO_EDGE | 46.41% | +0.0552 | -1.14 | 9243 |
| trajectory_state_vector | NO_FIRES | NO_EDGE | 49.49% | -0.0148 | -0.16 | 13057 |

**11 signals transitioned; 0 flipped to edge.** All newly-fired signals land at `NO_EDGE`.

### B. Signals UNCHANGED (scored in both — delta = 0.0000 on every metric)

20 signals were scored in both runs with **identical** accuracy, MCC, Sharpe, and n. These are the signals that were already correct-axis and not NWP-unit-sensitive:

`calendar_climatology, cooling_failure, corrected_pressure_delta, cross_model_divergence, fogr_reversion, forecast_disagreement, frontal_passage_intraday, gaussian, gaussian_v2, goldilocks, metar_dtdt, metar_trend, nwp_dtdt_fusion, pressure_delta, pressure_tendency, radiational_cooling, regime, spike_reversion, warm_momentum, wind_direction_shift`

### C. Signals still NOT scoreable (unchanged — remain silent)

`dewpoint_depression, frontal_detector, frontal_passage_detector, frontal_passage_nowcast, metar_nowcast, settlement_arbitrage, spread_based_entry, volume_momentum` → **NO_FIRES** (never produce a direction).

`esdr, intraday_metar_confirmation, nwp_direct, temperature_advection` → **SKIPPED** (slow / live-API in read-only mode).

---

## Calibration Sweep (Brier / ECE)

Same pattern: the 11 newly-fired signals now have Brier/ECE; the 20 already-scored signals are unchanged.

| signal | Brier (before→after) | ECE (before→after) | n |
|---|---|---|---|
| ai_composite | — → 0.4024 | — → 0.3794 | 4823 |
| cloud_cover_index | — → 0.2492 | — → 0.1469 | 6054 |
| dual_polarity | — → 0.4098 | — → 0.3716 | 7602 |
| eighty_two_member_ensemble | — → 0.3782 | — → 0.3678 | 13515 |
| eighty_two_member_ensemble_ece | — → 0.3782 | — → 0.3678 | 13515 |
| eighty_two_member_ensemble_pooled | — → 0.3721 | — → 0.3496 | 13515 |
| feels_like_delta | — → 0.2578 | — → 0.1632 | 4197 |
| nwp_analog | — → 0.2590 | — → 0.0777 | 4609 |
| nwp_analog_ensemble | — → 0.2590 | — → 0.0777 | 4609 |
| seasonal_regime | — → 0.3711 | — → 0.3410 | 9243 |
| trajectory_state_vector | — → 0.3406 | — → 0.2937 | 13057 |

All other scored signals: Brier/ECE unchanged (delta = 0).

---

## Lane / Gate / Fusion / Calibration-Mode

All lanes, gates, fusion modes, and calibration modes are **reachable** in both runs — no change:

- **Lanes (6):** forecasting_ensemble, sure_thing, goldilocks, trajectory, nowcasting, radiational_cooling — all reachable.
- **Gates (9):** agreement, settlement_execution, production, station_skill, liquidity, trajectory_confirmation, spatial_coherence, adaptive_thresholds, crossfeed — all reachable.
- **Fusion (4):** none, uwc, majority, weighted — reachable.
- **Calibration (4):** platt, bma, emos, both — reachable.

---

## Experiment / Function Sweep

All six experiment groups present (unchanged): dtw (5 files), gaussian, goldilocks, kalman, trajectory, nwp_analog.

---

## Interpretation

**The three corrections were correctness fixes, not edge-creating changes.** Their effect:

1. **The contemporaneous gate + unit seam repairs restored *testability*** to 11 signals that were previously invisible to the suite (silently dropping out as NO_FIRES / NOT_REACHABLE). This is a data-integrity win — those signals were *silently absent* from all prior manifests, which is itself a reporting bug now closed.

2. **No conclusion changed.** The most important signals that DID fire with near-edge (positive MCC) before — `frontal_passage_intraday` (MCC +0.108), `nwp_dtdt_fusion` (+0.106) — are unchanged and remain `NO_EDGE` under the base-rate-adjusted test. Their positive raw MCC is explained by base-rate (LOW resolves down ~95%), not skill.

3. **`nwp_analog` is now honest.** It went from NO_FIRES (silently untestable) to firing at 49.08% acc / MCC -0.0225 / ECE 0.0777 — confirming the contemporaneous gate now correctly *excludes* the 34,460 lookahead-only keys that would otherwise have fabricated a fake edge. This is consistent with the QC re-verify (`9bc2c4a03`) that found "no h-24 directional edge, lead=0, station-concentrated."

**No item's conclusion flipped (edge ↔ no-edge).** The suite is uniformly no-edge after corrections — a *cleaner*, more honest no-edge than before, because 11 silent signals are now explicitly scored as no-edge rather than silently omitted.

---

## Receipts

- **AFTER (this re-run):** `reports/ultimate_sweep_20260919-212026.json` / `.md` — elapsed 1456.20s.
- **BEFORE (baseline):** `reports/ultimate_sweep_20260919-123629.json` / `.md` — elapsed 1234.19s.

Both are tracked JSON/MD manifests; identical schema, deterministic.
