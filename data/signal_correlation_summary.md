# Signal Correlation Matrix — Spearman ρ

**Generated:** 2026-08-07T03:59:30.364114

Pairwise Spearman rank correlation (ρ) between all active signals.

Values near +1.0 = highly correlated (vote same direction).

Values near -1.0 = inversely correlated (vote opposite).

Values near 0.0 = orthogonal (independent signals).

Active signal count: 0

## Redundancy Summary

| Signal                              | Mean |ρ|   | Max |ρ|    | Classification       | Disposition  |
| ----------------------------------- | ---------- | ---------- | -------------------- | ------------ |

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


