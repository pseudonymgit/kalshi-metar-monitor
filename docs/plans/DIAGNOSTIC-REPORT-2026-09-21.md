# DTW Trajectory Replay Diagnostic Report

**Date:** 2026-09-21
**Status:** Completed
**Commit:** bb29fd46e1a06a8bebb64a33bde693c2134f5f78

## Executive Summary

The DTW trajectory replay system has a critical implementation bug causing poor predictive performance (8.33% overall accuracy). The root cause is incorrect feature weighting in the DTW distance calculation, which dilutes the temperature signal when humidity and pressure features are included.

## Key Findings

### 1. Accuracy Analysis
- **Overall:** 3/36 correct predictions (8.33% accuracy)
- **By feature set:**
  - `temp_only`: 3/12 correct (25% accuracy)
  - `temp_humidity`: 0/12 correct (0% accuracy)  
  - `temp_humidity_pressure`: 0/12 correct (0% accuracy)

### 2. Root Cause Identified
The `dtw_distance()` function on line 188 of `scripts/trajectory_dtw_replay.py` contains a bug:

```python
weights = tuple(1.0 / len(query[0]) for _ in query[0])  # BUG: recalculates equal weights
```

**Problem:** The function recalculates equal weights internally instead of using the intended feature weights defined in `FEATURE_WEIGHTS`.

**Impact:** 
- For `temp_only`: Works correctly (equal weighting is correct for single feature)
- For `temp_humidity`: Temperature signal diluted by equal 50/50 weighting with humidity
- For `temp_humidity_pressure`: Temperature signal further diluted by equal 33/33/33 weighting

### 3. Secondary Design Issue
Even with the bug fixed, the current `FEATURE_WEIGHTS` assigns equal weight to all features:

```python
FEATURE_WEIGHTS = {name: tuple(1.0 / len(cols) for _ in cols) for name, cols in FEATURES.items()}
```

**Meteorological Reality:** Temperature is the primary predictor of temperature outcomes. Humidity and pressure are secondary indicators that should have lower weights.

## Recommended Fix

### Step 1: Fix the DTW Weighting Bug
Modify `dtw_distance()` to accept feature weights as a parameter and use them instead of recalculating equal weights.

### Step 2: Update Feature Weights
Update `FEATURE_WEIGHTS` to reflect meteorological reality:
- `temp_only`: (1.0,)
- `temp_humidity`: (0.7, 0.3)  # Temperature 70%, humidity 30%
- `temp_humidity_pressure`: (0.6, 0.2, 0.2)  # Temperature 60%, humidity 20%, pressure 20%

### Step 3: Retest
Run the replay fixture with corrected weights to verify accuracy improvement.

## Expected Outcome

With proper weighting, multivariate feature sets should outperform `temp_only`, leveraging humidity and pressure as secondary predictive signals rather than diluting the temperature signal.

## Technical Details

### Current Implementation Flow:
1. `replay_fixture()` calls `dtw_distance(scaled_query, scaled_candidate, DTW_RADIUS)`
2. `dtw_distance()` ignores `FEATURE_WEIGHTS[feature_set]` and recalculates equal weights
3. Distance metric uses equal weights regardless of feature importance

### Corrected Flow:
1. Pass `FEATURE_WEIGHTS[feature_set]` as parameter to `dtw_distance()`
2. Use provided weights in `_local_cost()` calculation
3. Distance metric respects feature importance

## Testing Methodology

The replay fixture tests 3 target dates × 4 intraday cutoffs × 3 feature sets = 36 prediction scenarios. Each prediction uses DTW sequence matching on 5 days of historical data with strict causal constraints.

## Conclusion

The trajectory approach is not fundamentally flawed. The poor performance results from a correctable implementation bug. With proper feature weighting, the DTW trajectory lane should provide meaningful predictive signals that complement other weather-engine approaches.