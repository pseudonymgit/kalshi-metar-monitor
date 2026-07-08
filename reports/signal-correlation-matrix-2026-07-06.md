# S4/N3 — Signal Correlation Matrix & Redundancy Report

**Date:** 2026-07-06
**Signals analyzed:** 10
**Stations:** 20

## Signal Fire Counts

| Signal | Total Fires |
|--------|-------------|
| reversion | 22372 |
| gaussian | 13794 |
| regime | 47 |
| gaussian_v2 | 22372 |
| pressure | 18475 |
| climatology | 7353 |
| goldilocks | 13101 |
| pressure_regime_interaction | 0 |
| dtr_trend | 23673 |
| wind_direction_shift | 3723 |

## Pairwise Direction Agreement Rate

| Signal A | Signal B | Agreement | Co-fires | Corr | MI (bits) |
|----------|----------|-----------|----------|------|----------|
| reversion | gaussian | 0.800 | 1639 | 0.605 | 0.2943 |
| reversion | regime | 0.600 | 45 | 0.047 | 0.0000 |
| reversion | gaussian_v2 | 1.000 | 1642 | 1.000 | 0.9998 |
| reversion | pressure | 0.530 | 1640 | 0.020 | 0.0026 |
| reversion | climatology | 0.718 | 1516 | 0.207 | 0.1505 |
| reversion | goldilocks | 0.823 | 1635 | 0.422 | 0.3375 |
| reversion | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| reversion | dtr_trend | 0.384 | 1642 | 0.071 | 0.0396 |
| reversion | wind_direction_shift | 0.512 | 1255 | -0.005 | 0.0002 |
| gaussian | regime | 0.644 | 45 | -0.047 | 0.0000 |
| gaussian | gaussian_v2 | 0.800 | 1639 | 0.605 | 0.2943 |
| gaussian | pressure | 0.524 | 1637 | -0.003 | 0.0013 |
| gaussian | climatology | 0.889 | 1516 | 0.323 | 0.4768 |
| gaussian | goldilocks | 0.901 | 1632 | 0.473 | 0.5264 |
| gaussian | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| gaussian | dtr_trend | 0.434 | 1639 | 0.056 | 0.0122 |
| gaussian | wind_direction_shift | 0.510 | 1252 | 0.026 | 0.0010 |
| regime | gaussian_v2 | 0.600 | 45 | 0.047 | 0.0000 |
| regime | pressure | 0.591 | 44 | -0.083 | 0.0000 |
| regime | climatology | 0.769 | 39 | 0.043 | 0.0000 |
| regime | goldilocks | 0.622 | 45 | -0.156 | 0.0000 |
| regime | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| regime | dtr_trend | 0.467 | 45 | 0.112 | 0.0000 |
| regime | wind_direction_shift | 0.458 | 24 | -0.159 | 0.0000 |
| gaussian_v2 | pressure | 0.530 | 1640 | 0.020 | 0.0026 |
| gaussian_v2 | climatology | 0.718 | 1516 | 0.207 | 0.1505 |
| gaussian_v2 | goldilocks | 0.823 | 1635 | 0.422 | 0.3375 |
| gaussian_v2 | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| gaussian_v2 | dtr_trend | 0.384 | 1642 | 0.071 | 0.0396 |
| gaussian_v2 | wind_direction_shift | 0.512 | 1255 | -0.005 | 0.0002 |
| pressure | climatology | 0.516 | 1514 | 0.008 | 0.0006 |
| pressure | goldilocks | 0.530 | 1633 | -0.053 | 0.0023 |
| pressure | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| pressure | dtr_trend | 0.527 | 1640 | -0.004 | 0.0022 |
| pressure | wind_direction_shift | 0.531 | 1255 | 0.016 | 0.0029 |
| climatology | goldilocks | 0.830 | 1515 | 0.191 | 0.3339 |
| climatology | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| climatology | dtr_trend | 0.466 | 1516 | 0.026 | 0.0029 |
| climatology | wind_direction_shift | 0.495 | 1167 | 0.037 | 0.0001 |
| goldilocks | pressure_regime_interaction | 0.000 | 0 | 0.000 | 0.0000 |
| goldilocks | dtr_trend | 0.418 | 1635 | 0.067 | 0.0192 |
| goldilocks | wind_direction_shift | 0.510 | 1251 | -0.041 | 0.0008 |
| pressure_regime_interaction | dtr_trend | 0.000 | 0 | 0.000 | 0.0000 |
| pressure_regime_interaction | wind_direction_shift | 0.000 | 0 | 0.000 | 0.0000 |
| dtr_trend | wind_direction_shift | 0.510 | 1255 | 0.016 | 0.0002 |

## Redundancy Recommendations

- **REDUNDANT**: reversion ↔ gaussian_v2 (agreement=1.000, corr=1.000, MI=0.9998)
  - Consider dropping one of reversion / gaussian_v2

## Correlation Matrix (Confidence Pearson)

| | reversion | gaussian | regime | gaussian_v2 | pressure | climatology | goldilocks | pressure_regime_interaction | dtr_trend | wind_direction_shift |
|---|---|---|---|---|---|---|---|---|---|---|
| reversion | 1.000 | 0.605 | 0.047 | 1.000 | 0.020 | 0.207 | 0.422 | 0.000 | 0.071 | -0.005 |
| gaussian | 0.605 | 1.000 | -0.047 | 0.605 | -0.003 | 0.323 | 0.473 | 0.000 | 0.056 | 0.026 |
| regime | 0.047 | -0.047 | 1.000 | 0.047 | -0.083 | 0.043 | -0.156 | 0.000 | 0.112 | -0.159 |
| gaussian_v2 | 1.000 | 0.605 | 0.047 | 1.000 | 0.020 | 0.207 | 0.422 | 0.000 | 0.071 | -0.005 |
| pressure | 0.020 | -0.003 | -0.083 | 0.020 | 1.000 | 0.008 | -0.053 | 0.000 | -0.004 | 0.016 |
| climatology | 0.207 | 0.323 | 0.043 | 0.207 | 0.008 | 1.000 | 0.191 | 0.000 | 0.026 | 0.037 |
| goldilocks | 0.422 | 0.473 | -0.156 | 0.422 | -0.053 | 0.191 | 1.000 | 0.000 | 0.067 | -0.041 |
| pressure_regime_interaction | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 0.000 | 0.000 |
| dtr_trend | 0.071 | 0.056 | 0.112 | 0.071 | -0.004 | 0.026 | 0.067 | 0.000 | 1.000 | 0.016 |
| wind_direction_shift | -0.005 | 0.026 | -0.159 | -0.005 | 0.016 | 0.037 | -0.041 | 0.000 | 0.016 | 1.000 |

## MI Matrix

| | reversion | gaussian | regime | gaussian_v2 | pressure | climatology | goldilocks | pressure_regime_interaction | dtr_trend | wind_direction_shift |
|---|---|---|---|---|---|---|---|---|---|---|
| reversion | 0.0000 | 0.2943 | 0.0000 | 0.9998 | 0.0026 | 0.1505 | 0.3375 | 0.0000 | 0.0396 | 0.0002 |
| gaussian | 0.2943 | 0.0000 | 0.0000 | 0.2943 | 0.0013 | 0.4768 | 0.5264 | 0.0000 | 0.0122 | 0.0010 |
| regime | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| gaussian_v2 | 0.9998 | 0.2943 | 0.0000 | 0.0000 | 0.0026 | 0.1505 | 0.3375 | 0.0000 | 0.0396 | 0.0002 |
| pressure | 0.0026 | 0.0013 | 0.0000 | 0.0026 | 0.0000 | 0.0006 | 0.0023 | 0.0000 | 0.0022 | 0.0029 |
| climatology | 0.1505 | 0.4768 | 0.0000 | 0.1505 | 0.0006 | 0.0000 | 0.3339 | 0.0000 | 0.0029 | 0.0001 |
| goldilocks | 0.3375 | 0.5264 | 0.0000 | 0.3375 | 0.0023 | 0.3339 | 0.0000 | 0.0000 | 0.0192 | 0.0008 |
| pressure_regime_interaction | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| dtr_trend | 0.0396 | 0.0122 | 0.0000 | 0.0396 | 0.0022 | 0.0029 | 0.0192 | 0.0000 | 0.0000 | 0.0002 |
| wind_direction_shift | 0.0002 | 0.0010 | 0.0000 | 0.0002 | 0.0029 | 0.0001 | 0.0008 | 0.0000 | 0.0002 | 0.0000 |
