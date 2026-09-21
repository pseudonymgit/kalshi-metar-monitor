# R/T-8b — Exhaustive Multi-Target Recalibration

- **Generated:** 2026-09-21T02:34:13Z
- **Axis:** strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction
- **Labels:** settlement_truth.strike_price (14,305 rows via /historical backfill)
- **Signals:** BACKTEST_SIGNALS (7 honest live signals, R/T-8a corrected)
- **Fusion:** SignalFusionEngine (LIOP/LLOP, core/fusion_logic.py) — the connected fusion whose import R/T-8a fixed
- **Excluded:** {'between_excluded': 54, 'untrusted_excluded': 80, 'no_temp': 0, 'unknown_market_type': 0}

**Fusion note:** R/T-8F (on-branch) A/B: LIOP MCC -0.0119 (non-degenerate, default); UWC degenerate (always-up). Calibration target = LIOP.

## Totals

- **labeled_events:** 14020
- **fused_fired:** 12151
- **fused_null_rate:** 0.13331

## Global fused accuracy (correct axis)

`acc= 50.12% MCC=-0.0165 baseΔ=-15.51 n=12151`

## Per-station

| station | result |
|---|---|
| KATL | acc= 54.71% MCC=+0.0905 baseΔ=-0.29 n=340 |
| KAUS | acc= 53.21% MCC=+0.0493 baseΔ=-23.67 n=1263 |
| KBOS | acc= 52.86% MCC=+0.0431 baseΔ=-2.29 n=350 |
| KDCA | acc= 49.86% MCC=-0.0031 baseΔ=-5.23 n=363 |
| KDEN | acc= 50.35% MCC=-0.0332 baseΔ=-21.11 n=862 |
| KDFW | acc= 55.49% MCC=+0.1057 baseΔ=-1.19 n=337 |
| KHOU | acc= 53.53% MCC=+0.0594 baseΔ=-5.16 n=368 |
| KLAS | acc= 54.92% MCC=+0.0753 baseΔ=-4.64 n=366 |
| KLAX | acc= 46.55% MCC=-0.0829 baseΔ=-15.01 n=653 |
| KMDW | acc= 47.36% MCC=-0.0571 baseΔ=-21.18 n=1459 |
| KMIA | acc= 48.70% MCC=-0.0593 baseΔ=-23.09 n=1152 |
| KMSP | acc= 54.12% MCC=+0.0679 baseΔ=-0.82 n=364 |
| KMSY | acc= 53.24% MCC=+0.0223 baseΔ=-6.47 n=340 |
| KNYC | acc= 45.83% MCC=-0.0907 baseΔ=-29.97 n=1438 |
| KOKC | acc= 55.06% MCC=+0.0828 baseΔ=-0.60 n=336 |
| KPHL | acc= 44.55% MCC=-0.0925 baseΔ=-18.13 n=844 |
| KPHX | acc= 54.90% MCC=+0.0908 baseΔ=-3.56 n=337 |
| KSAT | acc= 55.17% MCC=+0.0901 baseΔ=+0.94 n=319 |
| KSEA | acc= 52.12% MCC=+0.0266 baseΔ=-7.58 n=330 |
| KSFO | acc= 48.48% MCC=-0.0576 baseΔ=-5.45 n=330 |

## Per-regime (HIGH / LOW)

| regime | result |
|---|---|
| HIGH | acc= 51.02% MCC=-0.0300 baseΔ=-38.83 n=8661 |
| LOW | acc= 47.88% MCC=+0.0647 baseΔ=-46.59 n=3490 |

## Per-season

| season | result |
|---|---|
| DJF | acc= 49.23% MCC=-0.0030 baseΔ=-21.07 n=2401 |
| JJA | acc= 52.33% MCC=+0.0215 baseΔ=-7.94 n=4105 |
| MAM | acc= 55.13% MCC=+0.0249 baseΔ=-8.55 n=3920 |
| SON | acc= 34.72% MCC=-0.0506 baseΔ=-41.62 n=1725 |

## UP vs DOWN directions (separately)

| predicted | n | precision | actual up-rate | actual down-rate |
|---|---|---|---|---|
| up | 5725 | 33.54% | 33.54% | 66.46% |
| down | 6426 | 64.89% | 35.11% | 64.89% |

## Calibration pairs (UP precision vs DOWN precision per target)

### station

| target | UP n | UP precision | DOWN n | DOWN precision |
|---|---|---|---|---|
| KATL | 163 | 49.69% | 177 | 59.32% |
| KAUS | 605 | 25.29% | 658 | 78.88% |
| KBOS | 150 | 47.33% | 200 | 57.00% |
| KDCA | 181 | 44.75% | 182 | 54.95% |
| KDEN | 394 | 26.90% | 468 | 70.09% |
| KDFW | 162 | 48.77% | 175 | 61.71% |
| KHOU | 171 | 44.44% | 197 | 61.42% |
| KLAS | 159 | 44.65% | 207 | 62.80% |
| KLAX | 310 | 34.19% | 343 | 57.73% |
| KMDW | 729 | 28.81% | 730 | 65.89% |
| KMIA | 540 | 25.37% | 612 | 69.28% |
| KMSP | 153 | 49.02% | 211 | 57.82% |
| KMSY | 132 | 41.67% | 208 | 60.58% |
| KNYC | 727 | 20.36% | 711 | 71.87% |
| KOKC | 138 | 49.28% | 198 | 59.09% |
| KPHL | 455 | 33.19% | 389 | 57.84% |
| KPHX | 160 | 46.25% | 177 | 62.71% |
| KSAT | 131 | 51.14% | 188 | 57.98% |
| KSEA | 151 | 41.72% | 179 | 60.89% |
| KSFO | 114 | 42.11% | 216 | 51.85% |

### regime

| target | UP n | UP precision | DOWN n | DOWN precision |
|---|---|---|---|---|
| HIGH | 4121 | 9.20% | 4540 | 88.99% |
| LOW | 1604 | 96.07% | 1886 | 6.89% |

### season

| target | UP n | UP precision | DOWN n | DOWN precision |
|---|---|---|---|---|
| DJF | 1238 | 29.56% | 1163 | 70.16% |
| JJA | 1796 | 40.92% | 2309 | 61.20% |
| MAM | 1389 | 37.94% | 2531 | 64.56% |
| SON | 1302 | 22.43% | 423 | 72.58% |
