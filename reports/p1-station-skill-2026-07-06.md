# P1.2-P1.3 — Per-Station Skill Gating (2026-07-06)

**Date:** 2026-07-06
**Stations:** 16 (post R4-1.2 purge)
**Method:** 30-day rolling Brier Skill Score vs persistence and climatology baselines
**Bootstrap:** Block bootstrap, block size=5, 2000 resamples, 95% CI
**Decision rule:** TRADE if BSS > 0 against both baselines, else NO-TRADE

## Trade Selection Table

| Station | Market | BSS (Persistence) | BSS (Climatology) | CI Lower | CI Upper | Decision |
|---------|--------|-------------------|-------------------|----------|----------|----------|
| KATL | HIGH | +0.2272 | -0.5709 | +0.2533 | +0.4906 | NO-TRADE |
| KATL | LOW | +0.2219 | -0.6985 | -0.0984 | +0.2493 | NO-TRADE |
| KAUS | HIGH | +0.3893 | -0.0391 | +0.1491 | +0.4503 | NO-TRADE |
| KAUS | LOW | +0.1753 | -0.8545 | -0.0260 | +0.2533 | NO-TRADE |
| KBOS | HIGH | +0.4944 | +0.0370 | +0.2266 | +0.4775 | TRADE |
| KBOS | LOW | +0.0616 | -0.6684 | -0.0384 | +0.2780 | NO-TRADE |
| KDCA | HIGH | +0.4871 | +0.0984 | +0.2673 | +0.5199 | TRADE |
| KDCA | LOW | +0.1217 | -0.6851 | -0.1541 | +0.2153 | NO-TRADE |
| KDEN | HIGH | +0.4565 | -0.0064 | +0.3184 | +0.5039 | NO-TRADE |
| KDEN | LOW | +0.3370 | -0.4076 | -0.0747 | +0.2343 | NO-TRADE |
| KDFW | HIGH | +0.6209 | +0.3914 | +0.1940 | +0.5030 | TRADE |
| KDFW | LOW | -0.0170 | -0.6946 | +0.0166 | +0.2861 | NO-TRADE |
| KHOU | HIGH | +0.4845 | +0.0770 | +0.2483 | +0.5271 | TRADE |
| KHOU | LOW | +0.0766 | -0.7970 | -0.0308 | +0.3005 | NO-TRADE |
| KLAX | HIGH | +0.4058 | +0.0159 | +0.2681 | +0.5361 | TRADE |
| KLAX | LOW | +0.0991 | -0.6597 | -0.1303 | +0.2016 | NO-TRADE |
| KMDW | HIGH | +0.4739 | -0.0055 | +0.2539 | +0.5207 | NO-TRADE |
| KMDW | LOW | +0.3000 | -0.6495 | +0.1145 | +0.3545 | NO-TRADE |
| KMIA | HIGH | +0.5296 | -0.0046 | +0.2956 | +0.5232 | NO-TRADE |
| KMIA | LOW | +0.3188 | -0.5195 | -0.1042 | +0.2728 | NO-TRADE |
| KMSP | HIGH | +0.2920 | -0.2640 | +0.0382 | +0.3760 | NO-TRADE |
| KMSP | LOW | +0.3134 | -0.2708 | -0.0564 | +0.2884 | NO-TRADE |
| KNYC | HIGH | +0.6075 | +0.2771 | +0.1014 | +0.4255 | TRADE |
| KNYC | LOW | +0.3104 | -0.1801 | -0.1095 | +0.2895 | NO-TRADE |
| KPHL | HIGH | +0.3841 | -0.0880 | +0.1510 | +0.4776 | NO-TRADE |
| KPHL | LOW | -0.0417 | -0.5973 | -0.1382 | +0.2048 | NO-TRADE |
| KPHX | HIGH | +0.0521 | -0.5392 | -0.0281 | +0.3137 | NO-TRADE |
| KPHX | LOW | -0.1004 | -0.8452 | -0.1974 | +0.1737 | NO-TRADE |
| KSEA | HIGH | +0.1798 | -0.3173 | -0.1550 | +0.2591 | NO-TRADE |
| KSEA | LOW | +0.3661 | -0.5237 | -0.0133 | +0.2760 | NO-TRADE |
| KSFO | HIGH | +0.5294 | +0.3376 | +0.2282 | +0.5328 | TRADE |
| KSFO | LOW | -0.3697 | -1.1860 | -0.1033 | +0.1873 | NO-TRADE |

## Skilled vs Unskilled Stations (HIGH market)

- **Skilled (7):** KBOS, KDCA, KDFW, KHOU, KLAX, KNYC, KSFO
- **Unskilled (9):** KATL, KAUS, KDEN, KMDW, KMIA, KMSP, KPHL, KPHX, KSEA

## All-Stations vs Skilled-Only Comparison

| Metric | All Stations | Skilled Only | Delta |
|--------|-------------|-------------|-------|
| Trade count | 16168 | 7020 | -9148 |
| Accuracy | 65.26% | 64.63% | -0.63% |
| Sharpe ratio | 0.300 | 0.287 | -0.012 |
| Max drawdown | 9.39% | 9.07% | -0.33% |

## Notes

- BSS > 0 means the model outperforms the baseline (persistence or climatology)
- Persistence baseline: today's direction = yesterday's direction
- Climatology baseline: 15-day rolling mean of outcomes
- Block bootstrap preserves time-series autocorrelation (block size=5)
- All metrics computed from real METAR backfill, no AI/ML in loop
