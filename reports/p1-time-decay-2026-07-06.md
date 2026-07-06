# P1.5 — Time-Decay Weighted Signal Reliability (2026-07-06)

**Date:** 2026-07-06
**Stations:** 16 (post R4-1.2 purge)
**Method:** Exponential forgetting (decay=0.9, window=30 days)
**Confidence adjustment:** adjusted_conf = sqrt(raw_conf * reliability)
**LOP weighting:** reliability-weighted log-odds

## Aggregate Comparison

| Metric | Baseline | Time-Decay | Delta |
|--------|----------|------------|-------|
| Trade count | 16168 | 10070 | -6098 |
| Accuracy | 65.26% | 66.44% | +1.19% |
| Sharpe ratio | 0.300 | 0.326 | +0.026 |
| Brier score | 0.3160 | 0.3381 | +0.0221 |
| ECE | 0.2938 | 0.3365 | +0.0427 |
| Max drawdown | 9.39% | 9.50% | +0.10% |
| Binomial p | 0.0000 | 0.0000 | +0.0000 |

## Per-Station Comparison

| Station | Base Trades | Base Acc | Base Sharpe | Decay Trades | Decay Acc | Decay Sharpe | Delta |
|---------|-------------|----------|-------------|-------------|-----------|-------------|-------|
| KATL | 983 | 66.63% | 0.326 | 857 | 66.04% | 0.314 | -0.59% |
| KAUS | 982 | 64.66% | 0.293 | 785 | 68.15% | 0.372 | +3.49% |
| KBOS | 1130 | 65.31% | 0.307 | 819 | 68.62% | 0.377 | +3.31% |
| KDCA | 1079 | 65.99% | 0.316 | 1 | 0.00% | -20.500 | -65.99% |
| KDEN | 1113 | 69.81% | 0.412 | 816 | 68.87% | 0.381 | -0.94% |
| KDFW | 1007 | 63.85% | 0.269 | 819 | 66.42% | 0.321 | +2.57% |
| KHOU | 1013 | 63.77% | 0.271 | 860 | 67.09% | 0.338 | +3.32% |
| KLAX | 842 | 65.44% | 0.304 | 750 | 66.53% | 0.325 | +1.09% |
| KMDW | 1083 | 65.74% | 0.311 | 599 | 67.45% | 0.358 | +1.70% |
| KMIA | 793 | 70.11% | 0.420 | 733 | 72.44% | 0.474 | +2.33% |
| KMSP | 1095 | 63.93% | 0.270 | 638 | 66.30% | 0.332 | +2.37% |
| KNYC | 1099 | 63.33% | 0.259 | 1 | 0.00% | -20.500 | -63.33% |
| KPHL | 1090 | 66.15% | 0.313 | 1 | 0.00% | -20.500 | -66.15% |
| KPHX | 966 | 63.35% | 0.253 | 877 | 61.92% | 0.223 | -1.44% |
| KSEA | 1043 | 61.94% | 0.214 | 794 | 59.07% | 0.159 | -2.87% |
| KSFO | 850 | 64.82% | 0.288 | 720 | 66.11% | 0.317 | +1.29% |

## How It Works

### TimeDecaySignalManager

1. **Tracking**: Records per-signal per-station prediction outcomes with timestamps
2. **Reliability**: Computes exponentially weighted recent accuracy:
   - `reliability = Σ(decay^(t-i) * correct_i) / Σ(decay^(t-i))`
   - Most recent predictions get highest weight (decay=0.9)
   - Window of 30 days ensures adaptivity to regime changes
3. **Confidence adjustment**: `adjusted_conf = sqrt(raw_conf * reliability)`
   - Geometric mean of raw confidence and reliability
   - Penalizes overconfident signals with poor recent track record
4. **LOP weighting**: Signals with higher reliability get proportionally more weight
   via `log(reliability / (1 - reliability))` weighting in the opinion pool

## Notes

- All metrics computed from real METAR backfill data
- Walk-forward: 180-day train / 30-day test
- No AI/ML model calls in any loop
- TimeDecaySignalManager class added to core/signal_fusion.py
