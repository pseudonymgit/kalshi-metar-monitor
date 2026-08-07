# Signal Correlation Matrix — Spearman ρ

**Generated:** 2026-08-07T05:11:32.733436

Pairwise Spearman rank correlation (ρ) between all active signals.

Values near +1.0 = highly correlated (vote same direction).

Values near -1.0 = inversely correlated (vote opposite).

Values near 0.0 = orthogonal (independent signals).

Active signal count: 12

## Redundancy Summary

| Signal                              | Accuracy  | nTrades  | Mean |ρ|   | Max |ρ|    | Classification       | Disposition  |
| ----------------------------------- | --------- | -------- | ---------- | ---------- | -------------------- | ------------ |
| persistence                         | 0.519     | 3138     | 0.1270     | 1.0000     | HIGH_REDUNDANCY      | KILL         (simple_trend(1.00)) |
| simple_trend                        | 0.519     | 3138     | 0.1270     | 1.0000     | HIGH_REDUNDANCY      | KILL         (persistence(1.00)) |
| radiational_cooling                 | 0.184     | 38       | 0.1366     | 0.2910     | ORTHOGONAL           | ADVANCE      |
| wind_direction_shift                | 0.515     | 487      | 0.0548     | 0.2910     | ORTHOGONAL           | ADVANCE      |
| gaussian_v2                         | 0.609     | 2151     | 0.0452     | 0.2222     | ORTHOGONAL           | ADVANCE      |
| spike_reversion                     | 0.446     | 1138     | 0.0475     | 0.1630     | ORTHOGONAL           | ADVANCE      |
| gaussian                            | 0.631     | 1290     | 0.0560     | 0.1536     | ORTHOGONAL           | ADVANCE      |
| pressure_delta                      | 0.641     | 1991     | 0.0316     | 0.1186     | ORTHOGONAL           | ADVANCE      |
| goldilocks                          | 0.477     | 2155     | 0.0370     | 0.1126     | ORTHOGONAL           | ADVANCE      |
| ecmwf_bias_corrected                | 0.845     | 3138     | 0.0376     | 0.0919     | ORTHOGONAL           | ADVANCE      |
| calendar_climatology                | 0.646     | 697      | 0.0379     | 0.0856     | ORTHOGONAL           | ADVANCE      |
| forecast_disagreement               | 0.609     | 1663     | 0.0352     | 0.0856     | ORTHOGONAL           | ADVANCE      |

## Interpretation Guide

| Category | |ρ| Range | Signal Quality | Action |
|----------|:---------:|:--------------:|:------:|
| **ORTHOGONAL** | < 0.3 | Excellent | Keep both — provide independent information |
| **LOW** | 0.3 – 0.5 | Good | Some overlap but still useful together |
| **MODERATE** | 0.5 – 0.7 | Fair | Redundant — consider down-weighting one |
| **HIGH_REDUNDANCY** | ≥ 0.7 | Poor | Highly redundant — one should be killed or fused |

## Disposition Guide

- **ADVANCE**: Signal is orthogonal or low-correlation — keep in ensemble

- **KILL**: Signal is highly redundant — remove or fuse with its pair

- **PARK**: Needs further investigation before disposition

