# Full Sweep Results — 2026-08-11

## Overview

| Metric | Value |
|---|---|
| **Timestamp** | 2026-08-11T01:27:26 UTC |
| **Duration** | 526.3 seconds (~8.8 min) |
| **Total Evaluations** | 283 (estimated) |
| **Stations** | 20 (all) |
| **Signals** | 15 (full ensemble) |
| **Objective** | Consensus bonus scoring |
| **Convergence** | Jaccard similarity |
| **Fast Mode** | No (full sweep) |

## Baseline vs Optimal Config

| Metric | Baseline | Optimal | Δ |
|---|---|---|---|
| **Accuracy** | 0.517 | **0.681** | **+0.164 (+31.7%)** |
| **Sharpe** | 0.983 | **11.917** | **+10.93** |
| **Total PnL** | 490.2 | **-479.1** | **-969.3** |
| **Brier** | 0.331 | 0.313 | -0.018 |
| **Max Drawdown** | 0.022 | 0.030 | +0.008 |
| **Trades/Day** | 1.88 | 1.02 | -0.86 |
| **N Trades** | 2,476 | 47 | -2,429 |
| **Consensus Score** | 0.800 | 0.800 | 0.000 |
| **Scalarized Score** | 1.226 | 3.658 | **+2.432** |

## Key Drivers (16 Significant Params of 42)

### Top 5 by Effect Size

| Param | Group | Effect | Optimal | Peak Type |
|---|---|---|---|---|
| **stop_loss_kind** | Gates | 6.167 | 2 (trailing) | EDGE |
| **regime_season_filter** | Regime | 3.558 | djf (winter) | CLEAR_PEAK |
| **regime_region_filter** | Regime | 1.650 | continental | CLEAR_PEAK |
| **position_sizing_model** | Levers | 0.726 | 1 (kelly) | CLEAR_PEAK |
| **regime_cycle_filter** | Regime | 0.655 | 12z | EDGE |

### Big Accuracy Levers

| Param | Value | Accuracy | Sharpe | PnL | Trades |
|---|---|---|---|---|---|
| confidence_floor=0.8 | vs 0.6 | 0.613 vs 0.517 | 3.85 vs 0.98 | 136 vs 490 | 119 vs 2,476 |
| mod_directional_weight=0.3 | vs 0.7 | 0.613 vs 0.517 | 3.85 vs 0.98 | 136 vs 490 | 119 vs 2,476 |
| regime_season=djf | vs all | 0.602 vs 0.517 | 4.55 vs 0.98 | 489 vs 490 | 522 vs 2,476 |
| regime_region=continental | vs all | 0.537 vs 0.517 | 1.25 vs 0.98 | 530 vs 490 | 1,487 vs 2,476 |

### Empty/Neutral Params

| Param | Reason |
|---|---|
| edge_threshold | All values give identical results (0.517 acc) |
| cal_shrinkage_strength | Minimal effect (0.01086) |
| capital_base | PnL scales proportionally — no real effect |
| station_tier1_cap | All values give 0.517 acc, only PnL changes |

## Interaction Analysis

- **±1-step validation**: 13/16 STABLE, 0 GENUINE_OPTIMUM
- **Pairwise interactions**: 0 material of 10 pairs tested
- **Group interactions**: 0 interacting groups
- **Level 3 (Bayesian)**: SKIPPED — no material interactions found

## Walk-Forward Validation

⚠️ **All three windows produce identical results** — indicates the walk-forward evaluation is not splitting the data window. All windows show:
- Accuracy: 0.6809
- Sharpe: 11.9167
- PnL: -479.1
- N Trades: 47

## Optimal Config

```
edge_threshold: 0.001         confidence_floor: 0.8
fee_type: 2 (forecastex)      slippage_budget: 0.0
stop_loss_kind: 2 (trailing)  position_sizing_model: 1 (kelly)
capital_base: 25000           fusion_mode: 2 (select_best)
cal_shrinkage_strength: 0.0   mod_directional_weight: 0.3
regime_season_filter: djf     regime_region_filter: continental
regime_cycle_filter: 12z      station_tier1_cap: 10
station_tier2_cap: 10         station_tier3_cap: 1
```

## Sanity Check

| Check | Result | Status |
|---|---|---|
| Null config (default) | 0.517 acc (≈50% +1.7%) | ✅ PASS |
| Permutation test | 0.5006 acc (≈50%) | ✅ PASS |
| Synthetic recovery | FAILED (kelly_fraction recovery) | ❌ FAIL |
| Noise floor | 0.0030 (0.3% — acceptable) | ✅ PASS |
| Walk-forward | 0.6809 stable | ⚠️ IDENTICAL windows |

## Problems Identified

### 1. Negative PnL on Optimal Config
The "optimal" config loses money (-$479) despite 68% accuracy. This is driven by the scalarized objective weighting accuracy (35%) and sharpe (35%) much higher than PnL (15%). With only 47 trades, the Sharpe is inflated by thousands of zero-return days.

### 2. Data Starvation from Combined Regime Filters
The optimal config combines `djf (winter) + continental + confidence_floor=0.8 + mod_directional_weight=0.3`. This is extremely restrictive:
- Only winter months (3 months/year)
- Only continental stations (9 of 20)
- High confidence threshold (0.8)
Result: only 47 trades over ~6 years (< 8 trades/year)

### 3. Walk-Forward Identity
All three walk-forward windows show identical metrics. This is a bug in the walk-forward evaluation — it's not splitting the data, just re-evaluating the same config thrice.

### 4. Consensus Bonus Failed to Fire
The consensus score remained at 0.800 baseline despite accuracy improvement. The consensus bonus formula requires ≥58% accuracy at ≥65% confidence, but the optimal config's confidence floor (0.8) and directional weight (0.3) may not be producing the right confidence distribution for the bonus to trigger.

### 5. Jaccard Convergence Not Observed
Jaccard convergence was requested but no convergence metrics appear in the output. The sweep ran all 283 evaluations without early termination.

## Improvement Suggestion

**Fix: Relax regime filters to avoid data starvation**

The current optimal config combines `djf + continental + confidence_floor=0.8` yielding only 47 trades. **Relax the season filter to `mam` (spring) or `all`** while keeping the continental region filter. This would increase trade count while maintaining the accuracy boost from focusing on continental stations.

**Why it helps**: The continental region filter alone gives 1,487 trades at 53.7% accuracy with positive PnL ($530). Adding the winter season filter drops to 522 trades with 60.2% accuracy. The marginal accuracy gain from winter is +6.5pp but at the cost of 65% fewer trades. Swapping to spring (MAM) gives 654 trades, 52% accuracy, and positive PnL ($165). The goal should be 500-1,000+ trades for statistical significance — the current 47 trades is not enough to trust the 68% accuracy number.

## Data Files

- `data/full_sweep_results.json` — Full results (108 KB)
- `data/per_parameter_results.json` — Original sweep output
- `data/per_parameter_summary.csv` — CSV summary
- `data/level-0/results.json` — Level 0 artifacts
- `data/level-1/results.json` — Level 1 per-parameter sweeps
- `data/level-2/results.json` — Level 2 interactions
- `data/level-4/results.json` — Level 4 walk-forward