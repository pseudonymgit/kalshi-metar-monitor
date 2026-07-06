# P0.2-P0.3 — Full Backtest Results (Phase 1 Fixes Applied)

**Date:** 2026-07-06
**Branch:** main (post-R4-1.1 through R4-1.6 merge)
**Database:** /home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db
**Stations:** 16 (after R4-1.2 purge of KLAS, KMSY, KOKC, KSAT, KDAL)
**Walk-forward:** 180-day train / 30-day test
**Fee rate:** 5%

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Directional accuracy | 65.26% |
| Win rate | 65.26% |
| Sharpe ratio (with fees) | 0.300 |
| Brier score | 0.3160 |
| ECE | 0.2938 |
| Max drawdown | 9.39% |
| Trade count | 16168 |
| Binomial p-value | 0.0000 |
| 95% CI | [64.55%, 66.04%] |

## Per-Station Accuracy Breakdown

| Station | Trades | Correct | Accuracy | Sharpe | MaxDD | Binom p |
|---------|--------|---------|----------|--------|-------|---------|
| KATL | 983 | 655 | 66.63% | 0.326 | 9.39% | 0.0000 |
| KAUS | 982 | 635 | 64.66% | 0.293 | 6.98% | 0.0000 |
| KBOS | 1130 | 738 | 65.31% | 0.307 | 9.07% | 0.0000 |
| KDCA | 1079 | 712 | 65.99% | 0.316 | 6.62% | 0.0000 |
| KDEN | 1113 | 777 | 69.81% | 0.412 | 5.51% | 0.0000 |
| KDFW | 1007 | 643 | 63.85% | 0.269 | 13.59% | 0.0000 |
| KHOU | 1013 | 646 | 63.77% | 0.271 | 18.55% | 0.0000 |
| KLAX | 842 | 551 | 65.44% | 0.304 | 11.27% | 0.0000 |
| KMDW | 1083 | 712 | 65.74% | 0.311 | 11.19% | 0.0000 |
| KMIA | 793 | 556 | 70.11% | 0.420 | 12.61% | 0.0000 |
| KMSP | 1095 | 700 | 63.93% | 0.270 | 11.47% | 0.0000 |
| KNYC | 1099 | 696 | 63.33% | 0.259 | 9.47% | 0.0000 |
| KPHL | 1090 | 721 | 66.15% | 0.313 | 10.39% | 0.0000 |
| KPHX | 966 | 612 | 63.35% | 0.253 | 22.20% | 0.0000 |
| KSEA | 1043 | 646 | 61.94% | 0.214 | 14.01% | 0.0000 |
| KSFO | 850 | 551 | 64.82% | 0.288 | 12.08% | 0.0000 |

## Phase 1 Fixes Applied

1. **R4-1.1:** P&L mark-to-market fix (thread-safe price cache)
2. **R4-1.2:** Purged negative-EV markets (KLAS, KMSY, KOKC, KSAT, KDAL)
3. **R4-1.3:** Settlement-window entry timing (T-18h to T-2h)
4. **R4-1.4:** Signal 4 regime-adaptive threshold (DTR-scaled)
5. **R4-1.5:** Goldilocks confidence split (up/down directional)
6. **R4-1.6:** Cluster budget caps + same-city pair hedging

## Regime Strategy (Edge 14)

| Regime | Trades | Accuracy | Original Claim | Status |
|--------|--------|----------|----------------|--------|
| Stable + Reversion=0 (UP) | 59 | 50.85% | 97.5% | FAILS |
| Stable + Reversion=1 (DOWN) | 14 | 78.57% | 69.8% | HOLDS |
