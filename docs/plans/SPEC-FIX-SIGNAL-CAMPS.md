# SPEC: Fix Signal Camp Cancellation

## Problem
17 signals split into competing camps:
- **Reversion camp**: gaussian, gaussian_v2, pressure_delta, corrected_pressure_delta, seasonal_regime, spike_reversion → predict DOWN when temps above normal
- **Momentum camp**: warm_momentum, cooling_failure → predict UP when temps above normal
- UWC fusion averages these → near-zero confidence

## Fix: CampDisputeResolution
Add a `CampDisputeResolution` step to the UWC cascade in `core/signal_fusion.py`:

1. Classify each signal into its camp (reversion vs momentum vs baseline)
2. Compute per-camp confidence (weighted average within each camp)
3. If camps disagree (signs opposite):
   - Use baseline camp's confidence if available
   - Otherwise, scale down confidence by disagreement factor
   - Never produce zero confidence — just reduce it
4. If camps agree: use normal fusion

## Camp Definitions
```python
REVERSION_CAMP = {"gaussian", "gaussian_v2", "pressure_delta", 
                  "corrected_pressure_delta", "seasonal_regime", "spike_reversion"}
MOMENTUM_CAMP = {"warm_momentum", "cooling_failure"}
BASELINE_CAMP = {"forecast_disagreement", "calendar_climatology", 
                 "ecmwf_bias_corrected", "metar_trend", "eighty_two_member_ensemble"}
```

## Files to Modify
- `core/signal_fusion.py` — add `CampDisputeResolution` class, wire into UWC cascade
- `core/signal_families.py` — add camp classification to SignalEntry

## Test
- When reversion and momentum both fire, confidence should be reduced but not zero
- When baseline camp agrees with one side, that side should win
- When camps agree, no change from current behavior