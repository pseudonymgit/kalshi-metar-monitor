# Weather Engine Lever Audit

Current Settings: 2026-07-12T15:43:50.622965+00:00

## Active Levers

### Confirmation Filter
- **Active**: Yes
- **Threshold**: Minimum 2 signals agreeing
- **Source**: B6.2-B6.4 experiments

### Skill Gating
- **Active**: Yes
- **Threshold**: Brier Skill Score > 0.0 (positive BSS required)
- **Source**: SH2 experiments

### Kalman Smoothing
- **Active**: Yes (simulation)
- **Process Variance**: 0.1
- **Measurement Variance**: 0.1
- **Source**: B6.3 experiment

### Weighted Ensemble
- **Active**: Yes
- **Source Weights**: From best_calibration_params.json
- **Weights**: Adjustable per signal performance

### Risk Controls
- **Consecutive Loss Limit**: 8
- **Source**: Risk Controls (B1.5 baseline requirements)

