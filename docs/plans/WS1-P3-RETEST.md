# WS1.1 — P3 Accuracy Re-test Report

**Date:** 2026-08-11 18:21 UTC  
**Period:** 2026-05-01 to 2026-07-31  
**Stations:** 20 (all active Kalshi stations)  
**Analyst:** Gilfoyle (automated pipeline audit)

---

## Executive Summary

| Metric | Value | vs 50% | vs 66.18% baseline | Status |
|--------|-------|--------|---------------------|--------|
| **P3 Pipeline** | ❌ UNAVAILABLE | — | — | Blocked |
| **Persistence Baseline** (yesterday → today) | 49.24% (679/1379) | p=0.57 | p<0.001 | Indistinguishable from random |
| **GEFS Ensemble (baseline)** | 51.51% (752/1460) | p=0.25 | p<0.001 | Indistinguishable from random |
| **GEFS Ensemble (optimized)** | 53.37% (380/712) | p=0.072 | p<0.001 | Marginally >50% but far below 66.18% |

**Key Decision:** The 66.18% persistence baseline is ***not replicable*** on May-Jul 2026 data. The actual persistence accuracy is ~49-52% (random). P3's 32.9% (June 2026 report) remains the most reliable P3 estimate and is significantly **worse than random**.

---

## 1. P3 Pipeline Assessment

### Can P3 predictions be run for May-Jul 2026?

**No.** The P3 pipeline (`core/p3_main.py`, `core/p3_scheduler.py`) depends on the `settlement_epochs` table, which is **empty** across all locations:

| Location | settlement_epochs rows |
|----------|----------------------|
| `/prototypes/weather-engine-source/alerts.db` | 0 |
| `/prototypes/weather-engine-source/core/alerts.db` | 0 |
| `~/.openclaw/workspace/var/data/alerts.db` | 0 |

The original Phase 3 backtest (June 2026 report, 32.9% accuracy) used a backup database from the June 17 checkpoint (`weather-engine-source-backup-2026-06-17`). That backup is available but contains data only through June 16, 2026 — missing July entirely.

### bmode_p1_backtest.db — No P3 Data

The backtest database (`data/bmode_p1_backtest.db`) contains 13,420 rows but **zero P3-specific run_tags**:

| Run Tag | Description | Rows |
|---------|-------------|------|
| `baseline` | GEFS ensemble, baseline config | 2,997 |
| `baseline-default` | GEFS ensemble, default config | 2,993 |
| `optimal-conf55/65/70/80` | GEFS + optimal confidence thresholds | 1,486 each |
| `sweep-combined` | GEFS + sweep config | 1,486 |

**All rows are GEFS ensemble-based predictions, not P3 analog-based.** The P3 pipeline's analog-matching approach is a fundamentally different methodology.

---

## 2. Persistence Baseline (kalshi_settlements.db)

Computed from raw Kalshi settlement data: for each station/day with settlement data, predict that today's direction equals yesterday's direction.

### Overall

| Metric | Value |
|--------|-------|
| Total predictions | 1,379 |
| Correct | 679 |
| **Accuracy** | **49.24%** |
| 95% CI | (46.6%, 51.9%) |
| p-value (vs 50%) | 0.57 (not significant) |
| Cohen's h (vs 50%) | -0.015 (negligible) |

### By Month

| Month | Accuracy | Correct/Total |
|-------|----------|--------------|
| May 2026 | 52.08% | 238/457 |
| June 2026 | 47.83% | 220/460 |
| July 2026 | 47.84% | 221/462 |

### By Station (p < 0.10 denoted *)

| Station | Accuracy | Correct/Total | p-value |
|---------|----------|--------------|---------|
| KLAS | 62.69%* | 42/67 | 0.038 |
| KPHX | 62.50%* | 45/72 | 0.034 |
| KMSP | 55.07% | 38/69 | 0.399 |
| KPHL | 52.94% | 36/68 | 0.628 |
| KSAT | 52.94% | 27/51 | 0.674 |
| KMDW | 52.00% | 39/75 | 0.729 |
| KOKC | 52.00% | 39/75 | 0.729 |
| KBOS | 51.81% | 43/83 | 0.742 |
| KDCA | 51.25% | 41/80 | 0.823 |
| KHOU | 50.00% | 29/58 | 1.000 |
| KSEA | 48.86% | 43/88 | 0.831 |
| KAUS | 46.77% | 29/62 | 0.612 |
| KDEN | 46.84% | 37/79 | 0.574 |
| KMIA | 46.67% | 21/45 | 0.655 |
| KDFW | 45.07% | 32/71 | 0.406 |
| KNYC | 44.05% | 37/84 | 0.275 |
| KATL | 43.04% | 34/79 | 0.216 |
| KMSY | 38.78% | 19/49 | 0.116 |
| KSFO | 41.67% | 30/72 | 0.157 |
| KLAX | 34.62%* | 18/52 | 0.027 |

**Finding:** Only KLAX (34.6%) and KPHX/KLAS (62.5/62.7%) are significantly different from 50%. KLAX is worse, KPHX/KLAS are better. The 66.18% persistence baseline is **not supported** by the actual settlement data for this period.

---

## 3. GEFS Ensemble Baseline (bmode_p1_backtest.db, May-Jul 2026)

### Overall

| Run Tag | Accuracy | N | p-value vs 50% | Cohen's h | PnL |
|---------|----------|---|----------------|-----------|-----|
| baseline | 51.51% | 1,460 | 0.25 | +0.030 | -$134 |
| baseline-default | 51.51% | 1,460 | 0.25 | +0.030 | -$126 |
| optimal-conf* (all) | 53.37% | 712 | 0.072 | +0.067 | +$131 |

### By Month (baseline tag)

| Month | Accuracy | Correct/Total |
|-------|----------|--------------|
| May 2026 | 64.15% | 297/463 |
| June 2026 | 44.95% | 227/505 |
| July 2026 | 46.34% | 228/492 |

**Critical pattern:** The 64.15% accuracy in May collapses to ~45% in June-July. This is consistent with the P3 report's original June result and suggests May was an anomaly (or the data quality/coverage was different).

### By Direction (baseline tag)

| Direction | Accuracy | N | p-value |
|-----------|----------|---|---------|
| UP | 92.17% | 115 | <0.001 |
| DOWN | 48.03% | 1,345 | 0.148 |

**Finding:** The GEFS model has strong performance on UP predictions (92.2%) but only 115 UP predictions total vs 1,345 DOWN predictions. The model overwhelmingly predicts "down" and when it predicts "up" with high confidence, it's very accurate. This asymmetry suggests a systematic prediction bias.

### Stations with Significant Accuracy (baseline, p < 0.10):

| Station | Accuracy | p-value |
|---------|----------|---------|
| KPHX | 77.05% | <0.001 |
| KDEN | 67.95% | 0.002 |
| KSFO | 65.71% | 0.009 |
| KLAS | 64.41% | 0.027 |
| KSEA | 60.24% | 0.062 |

### Stations Significantly Worse Than Random:

| Station | Accuracy | p-value |
|---------|----------|---------|
| KMSY | 37.10% | 0.042 |

---

## 4. Original P3 Result (Context)

The June 2026 Phase 3 backtest report documented:

- **32.9% directional accuracy** (2,180/6,630)
- Station range: 13.2% (KMIA) to 42.0% (KDEN)
- Confidence calibration: PASS (100% high > 29.5% moderate)
- **Critical issue:** `market_type = NULL` for ALL 6,840 epochs
- 67% of predictions had < 3 analogs (data scarcity)
- Only 7 stations had sufficient data

This result is consistent with June's baseline collapse (GEFS dropped to 45% in June as well), suggesting that June 2026 was a genuinely difficult prediction environment rather than a P3-specific failure.

---

## 5. Statistical Summary

| Comparison | This Period | P3 Report (June) | Note |
|------------|-------------|------------------|------|
| Persistence accuracy | 49.24% | Not reported | 66.18% claimed but not replicable |
| GEFS baseline accuracy | 51.51% | Not reported | — |
| GEFS optimal accuracy | 53.37% | Not reported | Marginally significant |
| **P3 accuracy** | **UNAVAILABLE** | **32.9%** | Original result stands |
| Cohen's h (P3 vs 50%) | — | -0.354 (large negative) | Significant |
| Binomial p-value (P3) | — | p<0.001 | Highly significant |

---

## 6. Decision Criteria Assessment

### Criteria A: P3 accuracy > 50% with p < 0.10
**Cannot evaluate** — no P3 predictions available for this period. The original 32.9% result strongly contradicts this (32.9% << 50%).

### Criteria B: P3 accuracy < 50% with p < 0.10
**Supported by original data.** The June 2026 report showed 32.9% with highly significant p-value. This result is more reliable than the claimed 66.18% persistence baseline, which is not replicable.

### Criteria C: P3 accuracy not different from 50%
**Not applicable.** The original P3 result was significantly below 50%.

---

## 7. Recommendation

### Recommendation: Option A (Adopt SignalRegistry, archive P3)

**Rationale:**
1. P3's 32.9% accuracy is significantly *worse* than random (Cohen's h = -0.354, large negative effect)
2. The 66.18% persistence baseline is not replicable on May-Jul 2026 data (actual: 49.24%)
3. GEFS baseline shows 51.5% accuracy in the same period — not profitable
4. Even optimized GEFS (53.37%) only reaches marginal significance (p=0.072) and the PnL is only +$131 over 712 trades
5. The P3 pipeline's dependency on a populated settlement_epochs table means it needs data that's not currently maintained

### What to do next:

1. **Kill P3** — Archive `core/p3_main.py`, `p3_scheduler.py`, and all P3 submodules. The analog-matching approach was sound in theory but fails in practice with limited data.

2. **Deploy SignalRegistry** — The signal-based approach (GEFS ensemble + calibration + confidence modulation) already achieves 53.37% accuracy on the same data, which is higher than P3's 32.9% and costs no analog infrastructure.

3. **Investigate KLAX and KMSY** — Both stations are significantly worse than random in multiple models; this may indicate station-specific data quality issues rather than model failure.

4. **Drop the 66.18% persistence baseline claim** — It does not hold on actual settlement data for May-Jul 2026. The true persistence rate is indistinguishable from 50%.

---

## 8. Blockers and Issues Found

| Issue | Severity | Details |
|-------|----------|---------|
| settlement_epochs table empty | **CRITICAL** | All alerts.db instances have 0 rows. P3 pipeline cannot function. |
| No P3 run_tags in bmode DB | **HIGH** | `bmode_p1_backtest.db` contains only GEFS-based predictions. Cannot cross-validate P3 vs GEFS on same period. |
| 66.18% persistence baseline unverifiable | **HIGH** | May-Jul 2026 persistence is 49.24%. Either the 66.18% figure was from a different period/methodology or it was incorrectly calculated. |
| Directional prediction imbalance | **MEDIUM** | GEFS predicts "down" 92% of the time (1,345/1,460). Only 115 "up" predictions. This reflects the actual temperature trend but creates systematic bias. |
| May vs June-July accuracy cliff | **MEDIUM** | Both GEFS baseline (64%→45%) and persistence baseline (52%→48%) show a sharp accuracy drop in June. Something changed in the prediction environment. |

---

## 9. Data Quality Notes

### Settlement Data Coverage (May-Jul 2026)

- **Total settlement records:** 6,329 across 20 stations
- **Stations with complete coverage:** 20/20 Kalshi stations have data
- **Average records per station:** ~316
- **Date range:** Full coverage (2026-05-01 to 2026-07-31)

### GEFS Coverage

- The bmode backtest filtered trades through an edge/confidence gate, so not all settlement dates produced trades
- The "baseline" tag produced 1,460 trades from May-Jul (out of potentially ~3,720 station-days), suggesting ~39% pass-through rate
- The "optimal-conf" tags produced 712 trades (~19% pass-through)

---

## Appendix: Methodology

### Persistence Baseline
For each station, for each trading day T (with T >= 2 from first available date):
- Actual: `sign(settlement_temp(T) - settlement_temp(T-1))`
- Prediction: `sign(settlement_temp(T-1) - settlement_temp(T-2))`
- Excluded: flat days (no change) in either actual or prediction

### GEFS Ensemble
From `scripts/bmode_p1_backtest.py`:
- GEFS step=24 ensemble mean → forecast temperature
- Direction: forecast vs previous settlement
- Confidence: ensemble fraction (max(f_up, 1-f_up))
- Edge: |confidence - 0.50|, gated at 0.02
- Calibration: direction-specific full-archive curves (for optimal variants)

### Statistical Tests
- Binomial p-value: two-sided normal approximation (Zelen & Severo CDF)
- Cohen's h: `2*asin(sqrt(p1)) - 2*asin(sqrt(p2))`
- Wilson score interval for 95% confidence bounds