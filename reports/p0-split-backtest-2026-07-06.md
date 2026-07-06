# P0 Split Backtest — Per-Signal Metrics (2026-07-06)

**Date:** 2026-07-06
**Stations:** 16 (post R4-1.2 purge)
**Walk-forward:** 180-day train / 30-day test
**Fee rate:** 5%

## Per-Signal Performance

| Signal | Trades | Correct | Accuracy | Sharpe | Brier | ECE | MaxDD | Binom p |
|--------|--------|---------|----------|--------|-------|-----|-------|---------|
| Regime (DTR-scaled) [R4-1.4] | 40 | 28 | 70.00% | 0.408 | 0.2350 | 0.1754 | 6.13% | 0.0083 |
| Calendar climatology (60d) | 5346 | 3684 | 68.91% | 0.384 | 0.2260 | 0.1095 | 4.71% | 0.0000 |
| Goldilocks reversion [R4-1.5] | 9508 | 6466 | 68.01% | 0.385 | 0.2565 | 0.2052 | 4.01% | 0.0000 |
| Gaussian (48d z-score) | 9948 | 6656 | 66.91% | 0.337 | 1.5946 | 1.0048 | 15.53% | 0.0000 |
| Reversion (30d z-score) | 16279 | 10419 | 64.00% | 0.291 | 1.0524 | 0.6491 | 12.19% | 0.0000 |
| Gaussian v2 (30d z-score) | 16279 | 10419 | 64.00% | 0.291 | 1.0524 | 0.6491 | 12.19% | 0.0000 |
| Pressure (delta) | 13774 | 8113 | 58.90% | 0.167 | 0.2632 | 0.1390 | 13.30% | 0.0000 |
| Late-day momentum | 13588 | 6564 | 48.31% | -0.058 | 0.2902 | 0.1956 | 100.00% | 0.0000 |

## Ensemble Leave-One-Out Analysis

| Signal Removed | Trades | Accuracy | Sharpe | Δ vs Full Ensemble |
|----------------|--------|----------|--------|-------------------|
| **(none — full ensemble)** | 14711 | **66.54%** | 0.332 | — |
| Pressure (delta) | 15568 | 63.75% | 0.274 | ↓2.79% |
| Late-day momentum | 16168 | 65.26% | 0.300 | ↓1.28% |
| Regime (DTR-scaled) [R4-1.4] | 14711 | 66.54% | 0.332 | ↓0.00% |
| Calendar climatology (60d) | 14711 | 66.54% | 0.332 | ↓0.00% |
| Goldilocks reversion [R4-1.5] | 14711 | 66.54% | 0.332 | ↓0.00% |
| Reversion (30d z-score) | 12998 | 66.96% | 0.343 | ↑0.43% |
| Gaussian v2 (30d z-score) | 12998 | 66.96% | 0.343 | ↑0.43% |
| Gaussian (48d z-score) | 12977 | 67.12% | 0.344 | ↑0.58% |

## Per-Station Ensemble Accuracy

| Station | Trades | Correct | Accuracy | Sharpe | Brier | MaxDD |
|---------|--------|---------|----------|--------|-------|-------|
| KDEN | 899 | 640 | 71.19% | 0.458 | 0.2269 | 10.67% |
| KMIA | 833 | 582 | 69.87% | 0.422 | 0.2648 | 12.86% |
| KMDW | 913 | 627 | 68.67% | 0.387 | 0.2501 | 9.20% |
| KDCA | 943 | 642 | 68.08% | 0.363 | 0.2630 | 6.68% |
| KPHL | 935 | 634 | 67.81% | 0.359 | 0.2614 | 10.12% |
| KBOS | 958 | 644 | 67.22% | 0.344 | 0.2651 | 7.47% |
| KATL | 960 | 639 | 66.56% | 0.336 | 0.2742 | 9.45% |
| KMSP | 894 | 592 | 66.22% | 0.323 | 0.2694 | 12.65% |
| KDFW | 887 | 586 | 66.07% | 0.320 | 0.2826 | 13.79% |
| KPHX | 1027 | 672 | 65.43% | 0.309 | 0.2911 | 21.10% |
| KLAX | 872 | 570 | 65.37% | 0.297 | 0.3154 | 10.28% |
| KAUS | 870 | 568 | 65.29% | 0.306 | 0.2919 | 5.92% |
| KSFO | 874 | 570 | 65.22% | 0.304 | 0.3001 | 15.53% |
| KNYC | 927 | 598 | 64.51% | 0.289 | 0.2793 | 9.82% |
| KSEA | 986 | 631 | 64.00% | 0.265 | 0.2992 | 11.33% |
| KHOU | 933 | 593 | 63.56% | 0.269 | 0.3056 | 16.71% |

## Notes

- All metrics computed from real METAR backfill data (807K observations, 16 stations)
- Walk-forward design: 180-day train / 30-day test, no circularity
- R4-1.4: Regime signal uses DTR-scaled adaptive threshold
- R4-1.5: Goldilocks signal uses asymmetric confidence (up=0.40 base, down=0.25 base)
- No AI/ML model calls in backtest loop
