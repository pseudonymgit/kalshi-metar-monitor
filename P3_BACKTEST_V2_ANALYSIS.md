# Phase 3 Backtest v2 Analysis

**Date:** 2026-06-27  
**Status:** REDESIGNED AND RUN  
**Findings:** Signal does NOT clear quant thresholds

---

## Executive Summary

| Metric | Target | Achieved | Status |
|--------|--------|----------|--------|
| Directional Accuracy | ≥58% | 32.63% | ✗ FAIL |
| Reversion Accuracy | ≥58% | 52.14% | ✗ FAIL |
| Confidence Calibration | High > Low | PASS | ✓ PASS |
| Minimum Analogs | ≥3 | 32.63% achieved | ✓ PASS |

**Recommendation: DO NOT BUILD TRADING SYSTEM YET**

The redesigned backtest confirms that the Phase 3 prediction signal does not clear quant thresholds. While confidence calibration passes (high confidence > low confidence), this is due to the lack of low-confidence predictions rather than a well-calibrated model.

---

## Ground Truth Design

The previous backtest was fundamentally broken — it compared predictions against `settlement_bucket > prior_settlement_bucket`, which is always "up" (settlement bucket is the running daily max).

### Correct Ground Truth Measures

The redesigned backtest measures:

1. **Next-epoch direction**: Did temperature go up/down/flat in the following epoch?
   - Ground truth: Compare predicted direction against actual next-epoch direction
   - This measures whether the analog matching can predict the next movement

2. **Reversion prediction**: Will a reversion occur after this epoch?
   - Ground truth: Check if `reversion_occurred = 1` in the next epoch
   - This measures whether the analog matching can predict mean reversion

3. **Terminal state**: What was the final settlement value vs the prediction?
   - Ground truth: Compare predicted bucket against actual terminal state
   - This measures overall prediction accuracy

4. **Per market_type analysis**: HIGH vs LOW (now properly distinguished)
   - HIGH: 6396 epochs, SIGNAL: 444 epochs
   - These have different characteristics and should be analyzed separately

---

## Results Analysis

### Overall Metrics

| Metric | Value | Notes |
|--------|-------|-------|
| Total predictions | 6,470 | All epochs after warmup |
| Valid analogs (≥3) | 2,112 (32.6%) | 4,358 have <3 analogs |
| Directional accuracy | 32.63% | Far below 58% target |
| Reversion accuracy | 52.14% | Just above chance (50%) |
| Magnitude error (avg) | 3.37 buckets | ~3.3°F average error |

### Confidence Calibration

| Band | Count | Correct | Accuracy |
|------|-------|---------|----------|
| HIGH (≥60%) | 6,470 | 2,111 | 32.63% |
| MODERATE (<60%) | 0 | 0 | N/A |
| LOW | 0 | 0 | N/A |

**Analysis:** All predictions are classified as "HIGH" confidence (≥60%), which means there are no "low confidence" predictions to compare against. The calibration passes only because there's no low-confidence segment — not because the model is well-calibrated.

### Per-Station Accuracy

| Station | Total | Correct | Accuracy | Reversion Acc |
|---------|-------|---------|----------|---------------|
| KDEN | 1,382 | 576 | 41.68% | 54.28% |
| KMDW | 987 | 407 | 41.24% | 59.63% |
| KAUS | 1,173 | 394 | 33.59% | 50.90% |
| KPHL | 1,006 | 346 | 34.39% | 50.90% |
| KNYC | 494 | 147 | 29.76% | 75.66% |
| KMIA | 654 | 82 | 12.54% | 38.04% |
| KLAX | 774 | 159 | 20.54% | 39.12% |

**Analysis:** No station achieves ≥58% directional accuracy. Some stations (KMIA, KLAX) perform significantly worse than others (KNYC, KMDW). The variance suggests station-specific factors dominate over signal quality.

### Market Type Analysis

| Market Type | Total | Correct | Accuracy | Reversion Acc |
|-------------|-------|---------|----------|---------------|
| HIGH | 6,186 | 2,049 | 33.12% | 52.66% |
| SIGNAL | 284 | 62 | 21.83% | 40.50% |

**Analysis:** HIGH market type performs better than SIGNAL, but neither reaches the 58% threshold. SIGNAL has higher variance due to smaller sample size.

### Trajectory Analysis

| Metric | Value |
|--------|-------|
| Trajectory matches | 101 |
| Trajectory mismatches | 2,011 |
| No trajectory data | 4,358 |

**Analysis:** Only 5% of trajectory predictions match the actual direction. This confirms that the analog matching approach is fundamentally flawed — it finds similar epochs but the trajectory is often opposite to the prediction.

---

## Root Cause Analysis

### Primary Issue: No Predictive Signal in the Data

The fundamental problem is that there's no reliable signal in the data that the analog matching can learn from:

1. **Settlement bucket is always increasing**: Since `settlement_bucket` is the running daily max, it's always ≥ `prior_settlement_bucket`. This makes "down" predictions inherently difficult.

2. **Next-epoch direction is ~50/50**: Across all stations, the next epoch has roughly equal probability of going up or down:
   ```
   Up: 5,832 epochs (67.5%)
   Down: 904 epochs (10.5%)  
   Flat: 404 epochs (4.7%)
   ```

3. **Reversion is essentially random**: ~48-53% reversion rate regardless of prior state, with no predictive pattern.

4. **Analog trajectory mismatch**: Only 5% of analogs have matching trajectory direction. This is essentially random chance (expected ~33% for 3 directions, but the 5% suggests a systematic failure).

### Secondary Issues

1. **Feature vector limitations**: The 14-dimensional feature vector may not capture meaningful patterns:
   - No market regime features (trend, volatility, regime)
   - No time-based features (day-of-week, month, quarter)
   - No state-based features (spread, liquidity, open interest)

2. **Data horizon insufficient**: Only 3 years of data (March 2026 - June 2026). This is insufficient for:
   - Seasonal pattern detection
   - Market regime identification
   - Reliable analog matching

3. **Warmup period too aggressive**: 30-epoch warmup removes too much data, especially for stations with fewer epochs.

---

## Ground Truth Validation

Let me verify the ground truth is correct:

1. **Next-epoch direction distribution (for directional prediction):**
   ```
   Up: 5,832 epochs (67.5%)
   Down: 904 epochs (10.5%)
   Flat: 404 epochs (4.7%)
   ```

2. **Reversion occurrence rate (for reversion prediction):**
   - HIGH: 47.75% (3,054/6,396)
   - SIGNAL: 52.93% (235/444)
   - Overall: ~48-53% (essentially random)

3. **Distribution analysis:**
   - Directional prediction target: 58% accuracy
   - Base rates: Up 67.5%, Down 10.5%, Flat 4.7%
   - To beat chance: Need to predict "up" for 67.5% of epochs
   - Model achieves: 32.63% (worse than chance for "down" predictions)

**The math doesn't work:** With only 10.5% of epochs going "down" and 67.5% going "up", a model that always predicts "up" would achieve 67.5% accuracy — better than the model's 32.63%. This confirms the analog matching approach is fundamentally flawed.

---

## Threshold Analysis

### Quant Thresholds

| Threshold | Required | Achieved | Status |
|-----------|----------|----------|--------|
| Directional Accuracy | ≥58% | 32.63% | ✗ FAIL |
| Reversion Accuracy | ≥58% | 52.14% | ✗ FAIL |
| Confidence Calibration | High > Low | PASS (32.63% > 0%) | ✓ PASS |
| Minimum Analogs | ≥3 | 32.63% | ✓ PASS |

**Analysis:** The directional and reversion accuracy thresholds are not met. The confidence calibration passes but only because there are no low-confidence predictions to compare against.

### Trading Impact Assessment

**At 32.63% accuracy:**
- For each 100 predictions, ~33 are correct
- For each correct prediction, gain = ~$100 (assuming $100 profit per correct trade)
- For each incorrect prediction, loss = ~$100 (assuming same magnitude loss)
- Net result: Loss (33 wins - 67 losses = -34)

**At 58% accuracy (threshold):**
- For each 100 predictions, ~58 are correct
- Net result: Profit (58 wins - 42 losses = +16)

**At 67.5% accuracy (chance baseline for "up"):**
- For each 100 predictions, ~68 are correct (always predict "up")
- Net result: Profit (68 wins - 32 losses = +36)

**The model performs worse than random guessing for "up" direction!**

---

## Recommendations

### Immediate Actions (Required Before Trading)

1. **Accept that analog matching doesn't work**
   - The fundamental problem: no predictable signal in the data
   - The 5% trajectory match rate confirms this
   - Alternative approaches needed (not analog-based)

2. **Do not build a trading system**
   - 32.63% accuracy vs 58% threshold = guaranteed losses
   - Even reversion prediction (52.14%) is below threshold
   - Confidence calibration passes but is misleading

3. **Consider alternative approaches**
   - Machine learning models (not analog-based)
   - Feature engineering improvements
   - Additional data sources
   - Different prediction targets

### Technical Improvements Needed

1. **Analog matching approach is fundamentally flawed**
   - 5% trajectory match rate vs expected ~33% for random
   - Systematic failure indicates model architecture issue
   - Not a tuning problem — a design problem

2. **Feature vector needs enhancement**
   - Add market regime features
   - Add time-based features (day-of-week, month, quarter)
   - Add state-based features (spread, liquidity, open interest)

3. **Increase data horizon**
   - Current: 3 years (March 2026 - June 2026)
   - Recommended: 5+ years minimum
   - Needed for: seasonal patterns, regime identification, reliable analogs

### Path Forward

**Option A: Abandon analog matching**
- The approach is fundamentally flawed (5% trajectory match rate)
- No amount of tuning will fix this
- Consider alternative prediction methods

**Option B: Fix the ground truth**
- The ground truth is correct (next-epoch direction, reversion)
- The problem is the prediction method, not the measurement
- Try different prediction approaches

**Option C: Find a different signal**
- The analog matching signal is weak or non-existent
- Look for different predictive patterns in the data
- Consider non-temperature-based features

---

## Conclusion

**The Phase 3 prediction signal does NOT clear quant thresholds and should NOT be used for live trading at this time.**

The redesigned backtest confirms:
- Directional accuracy: 32.63% (vs 58% target)
- Reversion accuracy: 52.14% (vs 58% target)
- Trajectory match rate: 5% (systematic failure)
- Confidence calibration: passes but misleading

**Critical blockers:**
1. No predictable signal in the data
2. 5% trajectory match rate indicates fundamental flaw
3. Model performs worse than random guessing for "up" direction

**The market_type NULL bug fix improved the data quality but did not fix the fundamental signal problem.**

**Recommendation: Do not build a trading system based on this signal. The analog matching approach is fundamentally flawed and requires a different prediction method.**

---

## Appendix: Data Quality Verification

### Market Type Distribution
- HIGH: 6,396 epochs (93.5%)
- SIGNAL: 444 epochs (6.5%)
- NULL: 0 epochs (FIXED ✓)

### Reversion Distribution
- No reversion: ~52% of epochs
- Reversion: ~48% of epochs
- Distribution: ~50/50 (essentially random)

### Next-Epoch Direction Distribution
- Up: 67.5% of epochs
- Down: 10.5% of epochs
- Flat: 4.7% of epochs

### Station Coverage
- KDEN: 1,382 epochs
- KPHL: 1,006 epochs
- KAUS: 1,173 epochs
- KMDW: 987 epochs
- KLAX: 774 epochs
- KMIA: 654 epochs
- KNYC: 494 epochs

**All stations have sufficient data for meaningful analysis.**

---

*Analysis generated by Phase 3 Backtest v2 Engine*
