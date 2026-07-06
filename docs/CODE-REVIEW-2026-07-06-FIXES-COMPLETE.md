# Code Review Fixes — Completion Artifact

**Date:** 2026-07-06  
**Commit:** `414c1b9` — `fix(code-review): 4 CRITICAL + 3 HIGH items from CODE-REVIEW-2026-07-06-FULL`  
**Review Document:** `docs/CODE-REVIEW-2026-07-06-FULL.md`

---

## CRITICAL Items

### CRITICAL #1: Paper trading engine signal list outdated
**File:** `core/paper_trading_engine.py`  
**Issue:** Conviction score pathway used old 7-signal list instead of full 11-signal ensemble. Phase 2 signals (pressure_regime_interaction, dtr_trend, wind_direction_shift, rdae_mos) were missing.  
**Fix:** Added `FULL_ENSEMBLE_SIGNALS` class constant with all 11 signals. Updated `enable_conviction_gating()` to default to this list.  
**Lines changed:** `enable_conviction_gating()` method (~line 709)  
**Status:** ✅ CLOSED

### CRITICAL #2: NWP analog SQL — era5 data mixing
**File:** `scripts/nwp_analog_ensemble.py`  
**Issue:** `load_nwp_features()` SQL query did not filter by model name, causing era5 (historical reanalysis) data to mix with forecast models (gfs/ecmwf/icon/gem).  
**Fix:** Added explicit `AND model IN (?,?,?,?)` filter to the SQL query, passing MODELS list as parameters.  
**Lines changed:** `load_nwp_features()` function (~line 37)  
**Status:** ✅ CLOSED

### CRITICAL #3: RDAE-MOS IsotonicRegressionCalibrator fit()/predict() broken
**File:** `core/rdae_mos.py`  
**Issue:** `fit()` method created grouping but `predict()` ignored the fitted state and recomputed bins locally. The trained mapping was never used.  
**Fix:** Rewrote `fit()` to build a proper monotonic mapping using Pool-Adjacent-Violators Algorithm (PAVA). Rewrote `predict()` to use the fitted mapping with linear interpolation between bin centers. Added `_fitted` and `_fitted_mapping` state variables.  
**Lines changed:** `IsotonicRegressionCalibrator` class (~line 330)  
**Status:** ✅ CLOSED

### CRITICAL #4: Dempster-Shafer conflict incorrectly implemented
**File:** `core/signal_fusion.py`  
**Issue:** DS conflict only checked direction counts and confidence magnitudes, not actual evidence vector conflicts in hypothesis space.  
**Fix:** Replaced with proper DS evidence theory: each signal becomes a mass function over {UP, DOWN, UNCERTAIN}. Conflict K = Σ over disjoint hypothesis pairs of m_i(A) × m_j(B). Combines evidence-space conflict (70% weight) with direction disagreement (30% weight).  
**Lines changed:** `dempster_shafer_conflict()` function (~line 1530)  
**Status:** ✅ CLOSED

---

## HIGH Priority Items

### HIGH: Kelly short support — negative edge handling
**File:** `core/fee_aware_kelly_position_sizing.py`  
**Issue:** `compute_kelly_fraction()` calculated negative edge but code only applied modifications for positive edge. Negative edge (shorting opportunity) was ignored — code continued LONG with reduced size instead of returning negative fraction.  
**Fix:** `compute_kelly_fraction()` now preserves the sign of the Kelly fraction (capped to [-0.5, +0.5]). `compute_position_size()` returns absolute USD amount with `direction` metadata ('long' or 'short'). Added `short_opportunity` and `has_negative_edge` metadata fields.  
**Lines changed:** `compute_kelly_fraction()` and `compute_position_size()` (~line 92)  
**Status:** ✅ CLOSED

### HIGH: NWP analog lookahead bias in library growth
**File:** `scripts/nwp_analog_ensemble.py`  
**Issue:** Library list desynchronization bug where `target_fv is None` branch appended to `library_dates` and `library_outcomes` but not `library_features`, causing index misalignment. Current day's data could appear in analog search for itself.  
**Fix:** Restructured the walk-forward loop: prediction is always made BEFORE library addition. All three library lists (dates, features, outcomes) are kept synchronized — always append to all three or none. Added explicit "STRICT WALK-FORWARD" comments.  
**Lines changed:** Walk-forward loop in `run_backtest()` (~line 250)  
**Status:** ✅ CLOSED

### HIGH: Dual-threshold removal — conviction gating vs legacy path
**File:** `core/paper_trading_engine.py`  
**Issue:** When conviction gating was enabled, the legacy `price_advantage_threshold = 0.08` path was still reachable, creating inconsistent behavior.  
**Fix:** Made the two paths mutually exclusive. The legacy threshold path is now in an explicit `else` branch that only runs when conviction gating is NOT enabled. Added clarifying comments.  
**Lines changed:** `place_paper_trade()` method (~line 799)  
**Status:** ✅ CLOSED

---

## Additional Fixes (from HIGH items in review)

### RDAE-MOS seasonal window now used
**File:** `core/rdae_mos.py`  
**Issue:** `SEASONAL_WINDOW = 15` was defined but never used in analog matching.  
**Fix:** Added day-of-year filtering (±15 days) to the analog search in `rdae_predictor()`, with a small seasonal distance penalty for dates further from the target.  
**Status:** ✅ CLOSED

### Backtest script signal list consistency
**Files:** `scripts/comprehensive_split_backtest.py`, `scripts/split_backtest_current.py`  
**Issue:** Backtest scripts had varying signal lists that didn't match paper trading engine's full ensemble.  
**Fix:** Added `FULL_ENSEMBLE_SIGNAL_NAMES` list to both scripts, documenting the canonical 11-signal ensemble. Added cross-reference comments to `core/paper_trading_engine.py FULL_ENSEMBLE_SIGNALS`.  
**Status:** ✅ CLOSED

---

## Summary

| # | Severity | Item | File(s) | Status |
|---|----------|------|---------|--------|
| 1 | CRITICAL | Paper trading signal list outdated | `paper_trading_engine.py` | ✅ |
| 2 | CRITICAL | NWP era5 data mixing | `nwp_analog_ensemble.py` | ✅ |
| 3 | CRITICAL | RDAE-MOS fit()/predict() broken | `rdae_mos.py` | ✅ |
| 4 | CRITICAL | DS conflict incorrectly implemented | `signal_fusion.py` | ✅ |
| 5 | HIGH | Kelly short support missing | `fee_aware_kelly_position_sizing.py` | ✅ |
| 6 | HIGH | NWP analog lookahead bias | `nwp_analog_ensemble.py` | ✅ |
| 7 | HIGH | Dual-threshold inconsistency | `paper_trading_engine.py` | ✅ |
| + | HIGH | enable_conviction_gating hardcoded 8 signals | `paper_trading_engine.py` | ✅ |
| + | HIGH | RDAE-MOS seasonal window unused | `rdae_mos.py` | ✅ |
| + | — | Backtest script consistency | `comprehensive_split_backtest.py`, `split_backtest_current.py` | ✅ |

**Commit:** `414c1b9`  
**Files modified:** 7 source files  
**All syntax validated:** Python AST parse passes on all modified files
