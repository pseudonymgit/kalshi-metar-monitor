# Phase 3 B6 Experiment Results

**Date:** 2026-07-20

## B6.2: Confirmation Filter Experiment
- Directional accuracy: 51.82%
- Sharpe ratio: 0.037
- Trade count: 6,466
- Agreement threshold: 2 signals
- Confidence threshold: 0.5
- Risk state: STABLE

## B6.3: Kalman Smoothing Experiment  
- Directional accuracy: 100.00%
- Sharpe ratio: 1000.000
- Trade count: 238
- Processed 11 of 20 available stations
- Used Kalman parameters: process_var=0.1, measurement_var=0.2
- Optimized confirmation parameters: agreement threshold 4, confidence 1.0

Note: Kalman smoothing performance likely needs additional validation due to extremely high reported Sharpe (1000+), which is unusually high for realistic financial returns. The very low trade count (238 vs 6466 for confirmation filter) suggests this may require further investigation.