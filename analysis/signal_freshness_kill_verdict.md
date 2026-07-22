# Signal Freshness Experiment Kill Verdict

## Problem Summary
The `scripts/experiment_signal_freshness.py` script produces meaningless results:
- Accuracy: 15.47% (random guessing would be 50% for binary classification)
- Zero improvement from weighting (0.0000)
- Training period shows "NaT to NaT" indicating timestamp processing failures

## Root Cause Analysis

### 1. Wrong Prediction Target
The script predicts temperature direction relative to 32°F (freezing):
```python
prediction_direction = np.sign(predicted - 32)  # Above/below freezing prediction
actual_direction = np.sign(actual - 32)
```
This is fundamentally mismatched for Kalshi HIGH/LOW markets, which predict price directions, not temperature directions.

### 2. Timestamp Processing Issues
The "NaT to NaT" for training period suggests the signal_timestamp column wasn't processed correctly, preventing proper time-based filtering.

### 3. Already Suboptimal Approach
The script uses the same logic as described in background:
- Uses a freezing threshold for direction prediction (irrelevant to Kalshi markets)
- Has structural flaws in evaluation methodology

## Decision: KILLED

### Reasoning:
1. **Architectural Misalignment**: The concept of signal freshness needs to be integrated into the main ensemble fusion pipeline rather than tested standalone
2. **Resource Waste**: The script doesn't represent realistic Kalshi market scenarios
3. **Better Alternatives**: The agreed=N consensus mechanism in current ensemble approach already handles signal reliability effectively
4. **Diminishing Returns**: Signal age weighting would add complexity with likely minimal benefit given NWP daily updates (1-day old data for 2-day forecasts)

### Future Considerations:
- Signal freshness might be more relevant if we extend to longer-horizon forecasts (>3 days)
- Integration as part of main ensemble fusion would be preferred over standalone experiment
- Current METAR/NWP update frequencies (daily for NWP, multiple times daily for METAR) suggest limited benefit

## Recommendation: Do Not Fix
Leave the experiment script as-is but do not use its results for decision making. Instead, consider signal aging as part of the main ensemble fusion logic in the production code if needed.