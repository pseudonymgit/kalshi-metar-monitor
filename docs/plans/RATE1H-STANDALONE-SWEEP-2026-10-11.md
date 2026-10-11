# rate_1h Strike-Crossing Sweep — STANDALONE (2026-10-11)

**Script:** `scripts/sweep_intraday_rate1h_crossing.py`  
**Machine artifact:** `data/sweep_intraday_rate1h_crossing.json`  
**Population:** full ASOS station-day cohort — 16512 station-days, 128308 samples, 20 stations (2024-01-01 → 2026-10-11). **Independent of the market benchmark gate.**

## Contract

- `rate_1h = tmpf_mean(bucket) − mean(tmpf_mean of the 4 prior buckets)`
- strict running `high_so_far` over buckets ≤ decision instant
- target strike = next integer strictly above `high_so_far`
- label `crossed = settled_max > strike`; `==` is a LOSS
- `tmpf` is native °F (no C→F conversion)
- CI = block bootstrap by local settlement date, 10000 reps

## Per-window results

| window | n | base P(cross) | Brier(base) | ±thr | P(cross\|up) | P(cross\|down) | sep (pp) | 95% CI (pp) | Brier(rate, LOO) |
|---|---|---|---|---|---|---|---|---|---|
| 06Z | 14310 | 0.8201 | 0.1475 | ±0.5 | 0.8187 | 0.8001 | 1.86 | [-0.38, 4.13] | 0.1472 |
| 06Z | 14310 | 0.8201 | 0.1475 | ±1.0 | 0.8057 | 0.7781 | 2.76 | [-0.27, 5.79] | 0.1469 |
| 10Z | 13952 | 0.82 | 0.1476 | ±0.5 | 0.872 | 0.7784 | 9.36 | [7.34, 11.37] | 0.1467 |
| 10Z | 13952 | 0.82 | 0.1476 | ±1.0 | 0.8857 | 0.7497 | 13.59 | [10.25, 16.78] | 0.1468 |
| 12Z | 14244 | 0.8245 | 0.1447 | ±0.5 | 0.9236 | 0.7272 | 19.64 | [17.74, 21.58] | 0.1392 |
| 12Z | 14244 | 0.8245 | 0.1447 | ±1.0 | 0.9533 | 0.7203 | 23.3 | [20.66, 26.03] | 0.1405 |
| 14Z | 14534 | 0.8272 | 0.143 | ±0.5 | 0.8948 | 0.5945 | 30.03 | [26.77, 33.31] | 0.132 |
| 14Z | 14534 | 0.8272 | 0.143 | ±1.0 | 0.9259 | 0.5788 | 34.71 | [29.58, 39.68] | 0.1315 |
| 16Z | 14224 | 0.8084 | 0.1549 | ±0.5 | 0.866 | 0.4021 | 46.39 | [42.42, 50.31] | 0.1401 |
| 16Z | 14224 | 0.8084 | 0.1549 | ±1.0 | 0.8864 | 0.3555 | 53.09 | [47.63, 58.35] | 0.1421 |
| 17Z | 14077 | 0.7606 | 0.1821 | ±0.5 | 0.8277 | 0.3739 | 45.38 | [41.96, 48.87] | 0.1657 |
| 17Z | 14077 | 0.7606 | 0.1821 | ±1.0 | 0.8535 | 0.3254 | 52.81 | [48.07, 57.34] | 0.1679 |
| 18Z | 14025 | 0.6753 | 0.2193 | ±0.5 | 0.7785 | 0.2556 | 52.29 | [49.45, 55.16] | 0.1916 |
| 18Z | 14025 | 0.6753 | 0.2193 | ±1.0 | 0.8088 | 0.183 | 62.58 | [59.24, 65.93] | 0.1971 |
| 20Z | 14385 | 0.363 | 0.2312 | ±0.5 | 0.5512 | 0.0686 | 48.26 | [46.54, 49.95] | 0.1958 |
| 20Z | 14385 | 0.363 | 0.2312 | ±1.0 | 0.5911 | 0.0468 | 54.44 | [52.31, 56.57] | 0.2079 |
| 22Z | 14557 | 0.0582 | 0.0548 | ±0.5 | 0.1838 | 0.0047 | 17.91 | [16.4, 19.45] | 0.0507 |
| 22Z | 14557 | 0.0582 | 0.0548 | ±1.0 | 0.2087 | 0.0018 | 20.69 | [18.42, 23.07] | 0.0523 |

## Discriminative power (independent of the market gate)

Across 18 (window × threshold) cells, the up-minus-down P(cross) separation is positive in 18 and its 95% block-bootstrap CI excludes zero in 16.

Strongest separation: 18Z at ±1.0 °F → +62.58 pp (95% CI [59.24, 65.93]).

The rate-conditioned predictor beats the base-rate constant on leave-one-date-out Brier in 9/9 windows (largest in the afternoon, e.g. 06Z 0.1475→0.1469, 10Z 0.1476→0.1467, 12Z 0.1447→0.1392, 14Z 0.1430→0.1315); the gain is modest — the momentum axis is a discriminator, not a standalone predictor.

**Verdict: the 1-hour rate-of-change DOES show discriminative power on the full ASOS cohort.** Windows whose CI excludes zero are actionable signal; the rest are directionally consistent but not individually conclusive. No live wiring.

## Reproduce

```bash
.venv-gefs/bin/python scripts/sweep_intraday_rate1h_crossing.py
```
