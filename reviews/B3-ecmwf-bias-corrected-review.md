# B3-ecmwf-bias-corrected Review

## REVISE

- **Missing BaseSignal interface implementation**: The ECMWF signal does not inherit from `BaseSignal` or implement the required `evaluate()` method that returns `(direction, confidence)` tuple
- **No signal name property**: Missing the `name` property required by BaseSignal interface
- **No min_lookback property**: Missing the `min_lookback` property required by BaseSignal interface
- **Incorrect registry integration**: Signal is instantiated directly in registry but doesn't conform to the BaseSignal interface expected by the ensemble system
- **API endpoint mismatch**: Uses `v1/ecmwf` endpoint instead of the plan-specified `models=ecmwf_ifs025` parameter

### Required Fixes:
1. Make ECMWFBiasCorrectedSignal inherit from BaseSignal
2. Implement `evaluate()` method that returns (direction, confidence) tuple
3. Add `name` property returning "ecmwf_bias_corrected"
4. Add `min_lookback` property returning appropriate lookback window
5. Fix API endpoint to use `models=ecmwf_ifs025` as specified in HRRR-PIVOT-PLAN.md
6. Ensure signal properly integrates with the signal registry system