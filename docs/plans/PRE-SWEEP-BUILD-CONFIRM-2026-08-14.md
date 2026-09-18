# Pre-Sweep Build Confirmation — 2026-08-14

**Evidence-based confirmation of the pre-sweep build against current post-build code.**
All tests run against HEAD commit `48587dfab`.

---

## FP (First Principles) Decision Basis

### No Defect Found

Complete signal-level audit (17/17 signals inspected via `inspect.getsource()`) confirms:
- **ZERO** signals read observed weather data from `days[idx]` (the target day).
- All signals use history-only indices (`idx-1`, `idx-2`, `idx-N`) via `_safe_get()` or list iteration.
- The only `days[idx]` accesses are for identity metadata (`station`, `date`) — benign for accuracy.
- The 2026-08-13 pipeline trace claim that `ecmwf_bias_corrected` reads `days[idx].get('high')` is **INCORRECT** for the current code — it now reads `days[idx-1].get('high')`.

**The pre-sweep build is sound. No specification, fix, or QC1 iteration is needed.**

---

## 1. Targeted Tests: test_pre_sweep_build.py

**Result: 13/13 PASS** (0.67s)

| Test | Target | Status |
|------|--------|--------|
| S6-AT6.2 | cooling_failure days[idx] mutation invariance | ✅ PASS |
| S7-AT7.0 | P&L zeroing does not change ranking | ✅ PASS |
| S5-AT5.6 | Zero-trade config excluded from ranking | ✅ PASS |
| S1 (is_calibrated) | is_calibrated() tracks refitted state | ✅ PASS |
| S3-AT3.2 | Brier on identical index sets | ✅ PASS |
| S4 | exclude_degenerate default False | ✅ PASS |
| S6-AT6.1 | No 'today_temp' in cooling_failure_signal.py | ✅ PASS |
| S6-AT6.3 | hist_z_scores used for z_streak/current_z | ✅ PASS |
| S1-AT1.3 | CALIBRATION_PIPELINE_OPTIONS isotonic-only | ✅ PASS |
| S2 | NESTED_FOLDS — 4 folds, contiguous | ✅ PASS |
| S5 | Registry assertion pattern | ✅ PASS |
| S7 | (PROXY) labels and ranking_metric | ✅ PASS |
| E2E-fixture | Full deterministic end-to-end | ✅ PASS |

## 2. QC Gate Check: test_qc_check.py

**Result: 10/10 PASS**

| Gate | Status |
|------|--------|
| GATE 1: All 12 pre-sweep tests pass | ✅ PASS |
| GATE 2: Compile/import all 7 modified files | ✅ PASS |
| GATE 3: Fold geometry — non-zero eval, separate holdout | ✅ PASS |
| GATE 4: Fold call semantics & date disjointness | ✅ PASS |
| GATE 5: Brier raw/calibrated sample counts equal | ✅ PASS |
| GATE 6: Degenerate comparison same eval dates | ✅ PASS |
| GATE 7: Exception counters + zero-trade ranking guard | ✅ PASS |
| GATE 8: Ranking invariant accuracy-only | ✅ PASS |
| GATE 9: Output schema, proxy labels, ranking_metric | ✅ PASS |
| GATE 10: Git diff — no unexpected changes | ✅ PASS |

## 3. Signal-Level Target-Day Audit (FP Review)

| # | Signal | Weather data source | Status |
|---|--------|-------------------|--------|
| 1 | gaussian | `_safe_get(days, idx-1, 'high')` | ✅ CLEAN |
| 2 | gaussian_v2 | `_safe_get(days, idx-1, 'high')` | ✅ CLEAN |
| 3 | pressure_delta | `_safe_get(days, idx-N, 'pressure')` for N≥1 | ✅ CLEAN |
| 4 | forecast_disagreement | `_safe_get(days, idx-1, 'high')` | ✅ CLEAN |
| 5 | calendar_climatology | `_safe_get(days, idx-1, 'high')` | ✅ CLEAN |
| 6 | corrected_pressure_delta | Same as pressure_delta | ✅ CLEAN |
| 7 | seasonal_regime | `_safe_get(days, idx-1, 'temp')` | ✅ CLEAN |
| 8 | ecmwf_bias_corrected | `days[idx-1].get('high')`, `days[idx-2].get('high')` | ✅ CLEAN |
| 9 | metar_trend | `days[idx-1].get('high')`, `days[idx-2].get('high')` | ✅ CLEAN |
| 10 | wind_direction_shift | `idx-1..idx-lookback` | ✅ CLEAN |
| 11 | eighty_two_member_ensemble | `station/date` from idx (metadata), high from `idx-1` | ✅ CLEAN |
| 12 | spike_reversion (deg) | `_safe_get(days, idx-1, 'high'/'low')`, `idx-2` | ✅ CLEAN |
| 13 | radiational_cooling (deg) | `days[idx-1]` | ✅ CLEAN |
| 14 | warm_momentum (deg) | iterates `range(start, idx)` → `idx-` indices | ✅ CLEAN |
| 15 | cooling_failure (deg) | `hist_z_scores` only, S6-proven | ✅ CLEAN |
| 16 | asos_microstructure | `evaluate()` returns `(None, 0.0)` | ✅ CLEAN |
| 17 | temperature_trend | `evaluate()` returns `(None, 0.0)` | ✅ CLEAN |

**Method:** `inspect.getsource()` on each signal's `evaluate()` method via `SignalRegistry` at runtime.

## 4. Build Target File Diff Summary

| File | Status | Changes (vs HEAD) |
|------|--------|-------------------|
| `scripts/run_sweep.py` | ✅ No diff | Already committed |
| `scripts/ws5_common.py` | ✅ No diff | Already committed |
| `scripts/sweep/config.py` | ⚠️ Expected diff | REGISTERED_SIGNAL_NAMES import, kalshi_fee(), pipeline options |
| `core/fusion_logic.py` | ⚠️ Expected diff | is_calibrated() method |
| `core/signals/cooling_failure_signal.py` | ✅ No diff | Already committed |
| `scripts/ws5_review_harness.py` | ✅ No diff | Already committed |
| `scripts/_ws5_readiness_diagnostics.py` | ✅ No diff | Already committed |

## 5. Data Layer Verification

| Source | Row Count | Date Range | Status |
|--------|-----------|------------|--------|
| `kalshi_settlements.table` | 9,507 rows (7,872 w/ non-null temp) | 2021-08-19 → 2026-08-11 | ✅ REAL |
| `metar_observations` | 12,822,456 rows, 73 stations | 2021-01-01 → 2026-08-14 | ✅ REAL |
| `daily_stats` | 58,064 rows, 30 stations | 2021-01-01 → 2026-08-14 | ✅ DERIVED |

## 6. Accuracy Claims

### What the sweep WILL claim:
- Directional accuracy under 4-fold nested temporal CV
- Per-signal accuracy and fire rates
- Calibration quality (Brier, ECE)
- Config ranking by accuracy (proven accuracy-only)
- Outer holdout evaluation on [2026-05-15, 2026-08-13]
- Comparison vs degenerate-excluded run

### What the sweep will NOT claim:
- Real P&L (explicitly marked `(PROXY)`)
- Market microstructure conclusions
- Geographic generalization (20 US stations)
- Platt-scaling calibration quality

---

**Status: ✅ BUILD CONFIRMED — 13/13 tests PASS, 10/10 QC gates PASS, ALL 17 signals verified history-only. GO.**