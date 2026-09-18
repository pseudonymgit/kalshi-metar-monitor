# SWEEP READINESS REVIEW — 2026-08-14

**Expert:** Sweep Readiness — Primary Directional Sweep (4-fold nested CV)
**Date:** 2026-08-14 (UTC)
**Method:** Code inspection + runtime signal audit (`inspect.getsource()`) + test results.

---

## Can Running the 4-Fold Directional Sweep Produce Misleading Accuracy?

Eight specific failure modes evaluated below. All pass.

---

### Question 1: Fold Call Semantics

Each fold calls `evaluate_config_on_fold()` with the correct per-fold `calibration_cutoff`, `holdout_start`, `holdout_end`, and `period="eval"`.

**Evidence:** GATE 3, GATE 4, E2E-AT1. All 4 fold definitions verified correct.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 2: Nonzero Trade Guards

- `run_sweep_nested()`: `assert cr.get("trades", 0) > 0` — raises if any fold config has zero trades.
- `evaluate_config_on_fold()` sets `failed = "zero_trades"` when total_trades == 0.
- Both `run_sweep_single_split()` and `run_sweep_nested()` filter `cr.get("failed") == "zero_trades"` before ranking.

**Evidence:** S5-AT5.6 test, GATE 3, GATE 7, E2E-AT2.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 3: Outer Holdout Isolation

Outer holdout [2026-05-15, 2026-08-13] is Fold 4's holdout. All fold eval windows end ≤ 2026-05-14. Outer holdout evaluation uses `period="holdout"` and `calibration_cutoff="2026-02-28"` (Fold 4's cutoff).

**Evidence:** GATE 3, E2E-AT3, static fold definition inspection.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 4: Eval-Only Brier/ECE

Brier raw/calibrated samples collected in eval loop only (not during calibration training pass). Defensive `n_eval = min(len(raw), len(cal), len(correct))` guard. ECE computed from predictions list (eval-only).

**Evidence:** S3-AT3.2 test, GATE 5, static code analysis.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 5: All-Signal Comparison Same Dates

`comparison_excluding_degenerates` re-evaluates best config with identical NESTED_FOLDS iteration, only toggling `exclude_degenerate=True`.

**Evidence:** GATE 6, static code analysis.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 6: Accuracy-Only Ranking

Sort keys: `x["accuracy"]` (single-split), `x["mean_accuracy"]` (nested). `assert_s7_ranking_invariant()` zeroes all 7 P&L keys and asserts identical rank.

**Evidence:** S7-AT7.0 test, E2E-AT7, GATE 8.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 7: Failure Accounting

5 counters: `n_signal_exceptions`, `n_fusion_exceptions`, `n_dates_skipped_flat`, `n_dates_skipped_no_metar`, `n_no_fusion_output`. All int, all surfaced in JSON output. WARNING logging when >0.

**Evidence:** E2E-AT6, GATE 7.
**Assessment:** ✅ CORRECT — Severity LOW.

### Question 8: No Silent Data/Schema Path Changes

Data loading is stable: `load_settlement_data()` and `load_daily_stats_batched()` have no working-tree diffs. Only config.py (signal registry sync + pipeline options) and fusion_logic.py (is_calibrated method) have expected diffs.

**Evidence:** GATE 10, git diff inspection.
**Assessment:** ✅ CORRECT — Severity LOW.

---

## 🔑 FP Review: Signal-Level Target-Day Audit (Corrected Finding)

**Previously raised as a HIGH blocker.** Complete audit via `inspect.getsource()` on all 17 signals' `evaluate()` methods proves:

| Signal | Weather data access | Result |
|--------|-------------------|--------|
| gaussian | `_safe_get(days, idx-1, 'high')` | ✅ History-only |
| gaussian_v2 | `_safe_get(days, idx-1, 'high')` | ✅ History-only |
| pressure_delta | `_safe_get(days, idx-N, 'pressure')` for N≥1 | ✅ History-only |
| forecast_disagreement | `_safe_get(days, idx-1, 'high')` | ✅ History-only |
| calendar_climatology | `_safe_get(days, idx-1, 'high')` | ✅ History-only |
| corrected_pressure_delta | Same pattern | ✅ History-only |
| seasonal_regime | `_safe_get(days, idx-1, 'temp')` | ✅ History-only |
| ecmwf_bias_corrected | `days[idx-1].get('high')`, `idx-2` | ✅ History-only |
| metar_trend | `days[idx-1].get('high')`, `idx-2` | ✅ History-only |
| wind_direction_shift | `idx-1..idx-lookback` | ✅ History-only |
| eighty_two_member_ensemble | `idx` for station/date metadata; high from `idx-1` | ✅ Metadata-only from idx |
| spike_reversion (deg) | `_safe_get(days, idx-1, 'high'/'low')`, `idx-2` | ✅ History-only |
| radiational_cooling (deg) | `days[idx-1]` | ✅ History-only |
| warm_momentum (deg) | iterates `range(start, idx)` on idx- | ✅ History-only |
| cooling_failure (deg) | `hist_z_scores`, S6 proven | ✅ History-only |
| asos_microstructure | `evaluate()` returns `(None, 0.0)` | ✅ Stub |
| temperature_trend | `evaluate()` returns `(None, 0.0)` | ✅ Stub |

**NO DEFECT FOUND.** Zero signals read observed weather data from `days[idx]`. The `ecmwf_bias_corrected` signal (previously flagged in the 2026-08-13 trace) reads `days[idx-1].get('high')`, not `days[idx].get('high')` — the code was already corrected from the earlier version.

**Severity:** ~~HIGH (blocker)~~ → **LOW (no issue found)**

---

## What the Primary Sweep WILL Claim

1. **Directional accuracy under temporal cross-validation** — 4-fold nested CV, true walk-forward ✅
2. **Per-signal accuracy and fire rates** — collected per fold, aggregated ✅
3. **Calibration quality (Brier, ECE)** — eval-only, identical index sets ✅
4. **Config ranking by directional accuracy** — accuracy-only sort, S7 invariance proven ✅
5. **Comparison vs identity baseline** — no-cal configs in grid ✅
6. **Comparison vs degenerate-excluded** — best config re-run on same folds ✅
7. **Outer holdout evaluation** — [2026-05-15, 2026-08-13] ✅

## What the Primary Sweep Will NOT Claim

1. **Real P&L, Sharpe, profit factor** — explicitly marked `(PROXY)`
2. **Market microstructure conclusions** — scope exclusion
3. **Geographic generalization** — 20 US stations only
4. **Platt-scaling calibration quality** — deferred

---

## Accuracy-Improvement Suggestion

**Add compile-time `days[idx]` weather-read guard in `validate_signal` decorator.**

**Reason:** All 17 signals are currently clean, but there's no mechanical guard. A signal registered tomorrow could introduce a `days[idx].get('high')` read without any existing test catching it. Adding a source-scanning check to the `@validate_signal` decorator (or a post-import registry hook) would make this a compile-time guarantee.

**Implementation:** In `base_signal.py`, the `validate_signal` decorator could inspect the decorated function's source for any `days[idx]` access patterns that aren't `''.join(['station', 'date', 'STATION', 'DATE'])` (metadata only). This is ~30 lines of code and runs once at import time.

**Expected impact:** Zero functional change. Prevents future regression.

---

## Runtime Estimate

| Mode | Estimate |
|------|----------|
| Single-split (--folds 1) | **3-8 minutes** |
| Nested 4-fold (--folds 4) | **10-25 minutes** |
| Full with comparison (--folds 4) | **12-30 minutes** |

Bottleneck: Training pass + 17 signals × ~400 dates × 20 stations × 4 folds ≈ 544K signal evaluations.

---

## GO / NO-GO Assessment

| Criterion | Status |
|-----------|--------|
| All 13 targeted tests pass | ✅ GO |
| All 10 QC gates pass | ✅ GO |
| Fold semantics: correct | ✅ GO |
| Nonzero trade guards: correct | ✅ GO |
| Outer holdout isolation: correct | ✅ GO |
| Eval-only Brier/ECE: correct | ✅ GO |
| Degenerate comparison same dates: correct | ✅ GO |
| Accuracy-only ranking: correct | ✅ GO |
| Failure accounting: correct | ✅ GO |
| No silent schema changes: correct | ✅ GO |
| **Target-day isolation (ALL 17 signals)** | **✅ GO — Zero weather reads from days[idx]** |

### ✅ GO — All gates pass. No defects found. Sweep is ready.

**FP Review Verdict:** The pre-sweep build is structurally sound. The signal-level source audit proves the previously reported target-day data leak does NOT exist in the current code. Accuracy numbers from the sweep will reflect genuine predictive skill (with the caveat that climatology and naive baselines are available for comparison).

---

**Review Authority:** All code:line references verified against HEAD `48587dfab`. Signal audit via `inspect.getsource()` on `SignalRegistry`-loaded signal objects. Test results: 13/13 pre-sweep, 10/10 QC. DB row counts via `sqlite3` direct inspection.