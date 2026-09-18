# KNYC Single-Station Sweep Results

**Run:** 2026-08-11T02:13:30–02:22:15 (524.6s)
**Command:** `python3 scripts/per_parameter_sweep.py --objective consensus --convergence jaccard --filter-json '{"station_list": ["KNYC"], "region": "all", "season": "all"}'`
**Scope:** 1 station (KNYC), 15 signals, 42 parameters, 283 evaluations

---

## 1. Pipeline End-to-End Verification

**Result: ✅ Pipeline runs end-to-end on a single station.**

All 5 levels completed without errors. Warnings are limited to:
- `FrontalDetectorSignal` deprecation (cosmetic, suppressible)
- `ConstantInputWarning` on Spearman correlation (constant arrays, benign)
- Missing forecast bias file (`forecast_bias.json`)

---

## 2. Sanity Checks

| Check | Result | Value | Notes |
|---|---|---|---|
| Null config | ✅ PASS | 0.517 | Near expected 50% |
| Permutation test | ✅ PASS | 0.5006 | Within 0.48–0.52 band |
| **Synthetic recovery** | ❌ **FAIL** | Recovered 0.1 vs known 1.0 | All kelly_fraction values produced identical PnL (490.15) |

**Critical:** The synthetic recovery test fails. The evaluator cannot distinguish between different values of `kelly_fraction` — all 5 values produce identical PnL, meaning the backtest is insensitive to certain parameters. This undermines trust in parameter optimization.

---

## 3. Noise Floor

- σ_noise = 0.0030 (0.3%) — below 1% threshold ✅
- Peak detection threshold = 0.0090
- All 5 seeds gave identical accuracy (0.517) — zero variance is suspicious

---

## 4. Sensitivity Pre-Screen

**16/42 parameters significant.**

| Parameter | Group | Effect | Peak Type | Optimal Value |
|---|---|---|---|---|
| stop_loss_kind | Gates | 6.16659 | EDGE | 2 |
| regime_season_filter | Regime | 3.55779 | CLEAR_PEAK | djf |
| regime_region_filter | Regime | 1.65003 | CLEAR_PEAK | continental |
| position_sizing_model | Levers | 0.72572 | CLEAR_PEAK | 1 |
| regime_cycle_filter | Regime | 0.65543 | EDGE | 12z |
| fee_type | Fees | 0.58851 | CLEAR_PEAK | 2 |
| mod_directional_weight | Confidence | 0.51053 | EDGE | 0.3 |
| confidence_floor | Signals | 0.50165 | CLEAR_PEAK | 0.8 |
| slippage_budget | Fees | 0.46387 | EDGE | 0.0 |
| station_tier3_cap | StationSizing | 0.41347 | EDGE | 1 |
| station_tier1_cap | StationSizing | 0.40163 | CLEAR_PEAK | 10 |
| station_tier2_cap | StationSizing | 0.14751 | CLEAR_PEAK | 10 |
| fusion_mode | Modulators | 0.07242 | EDGE | 2 |
| edge_threshold | Signals | 0.01086 | EDGE | 0.001 |
| cal_shrinkage_strength | Calibration | 0.01086 | EDGE | 0.0 |
| capital_base | Levers | 0.00726 | CLEAR_PEAK | 25000 |

**26 parameters with zero effect** — these are dead parameters that don't move the needle on KNYC.

---

## 5. Best Config by Consensus Bonus

### Individual Parameter Winners (Level 1)

| Parameter | Optimal | Indiv. Acc | Indiv. Sharpe | Indiv. PnL | Indiv. Trades |
|---|---|---|---|---|---|
| stop_loss_kind | 2 | 0.517 | 7.83 | +3,003 | 2,476 |
| position_sizing_model | 1 | 0.517 | 1.06 | +956 | 2,476 |
| regime_season_filter | djf | 0.602 | 4.55 | +489 | 522 |
| confidence_floor | 0.8 | 0.613 | 3.85 | +136 | 119 |
| mod_directional_weight | 0.3 | 0.613 | 3.85 | +136 | 119 |
| station_tier3_cap | 1 | 0.517 | 1.17 | +560 | 2,476 |
| fusion_mode | 2 | 0.519 | 1.05 | +523 | 2,474 |
| regime_region_filter | continental | 0.537 | 1.25 | +530 | 1,487 |
| slippage_budget | 0.0 | 0.517 | 1.02 | +508 | 2,476 |
| regime_cycle_filter | 12z | 0.521 | 1.38 | +344 | 1,252 |

### Merged Optimal Config Performance

| Metric | Baseline | Optimal | Δ |
|---|---|---|---|
| **Accuracy** | 0.517 | **0.681** | +0.164 ✅ |
| **Sharpe** | 0.98 | **11.92** | +10.93 ✅ |
| **PnL** | +490.2 | **-479.1** | -969.3 ❌ |
| **Trades/day** | 1.88 | 1.02 | -46% |
| **Brier** | 0.331 | 0.313 | -0.018 ✅ |
| **Max drawdown** | 2.2% | 3.0% | -0.8% ❌ |
| **Consensus bonus** | 0.8693 | **0.8498** | -0.0195 ❌ |

**The merged config trades accuracy for PnL.** Sharpe is high because trades are few (high confidence) but the PnL per trade is negative on average. The consensus bonus degraded, meaning the optimal config is less "agreement-compatible" than baseline.

---

## 6. Best Config by Holdout Performance

### Walk-Forward Windows

| Window | Period | Accuracy | Sharpe | PnL | Trades |
|---|---|---|---|---|---|
| Window 1 | 2021–2025 | 0.674 | 11.69 | -76.65 | 49 |
| Window 2 | 2026 | 0.667 | 20.01 | -5,140.80 | 24 |
| Window 3 | 2021–2026 | 0.671 | 13.65 | +182.55 | 73 |

**Holding accuracy across windows is consistent (0.667–0.674).** However:
- PnL is negative in all windows except the full range
- Window 2 (2026 only) shows a catastrophic -5,140 PnL with only 24 trades
- This suggests the config picks extremely high-confidence trades that lose money

### Odd/Even Split

| Split | Accuracy |
|---|---|
| Odd years | 0.671 |
| Even years | 0.676 |
| Regime shift | ⚠️ Flagged |

### Robustness

| Metric | Value |
|---|---|
| Mean accuracy | 0.6809 |
| Std accuracy | 0.0000 |
| Noise sensitive | No |

**Zero std across 5 seeds is suspicious** — it suggests the evaluation is deterministic to the point of being brittle, or that the backtest doesn't incorporate stochastic elements.

---

## 7. Walk-Forward Comparison (Discovery vs Holdout)

| Metric | Discovery (Level 1) | Holdout (Level 4 Win 1) | Drop |
|---|---|---|---|
| Accuracy | 0.681 | 0.674 | -0.007 (1.0%) |
| Sharpe | 11.92 | 11.69 | -0.23 (1.9%) |
| PnL | -479.1 | -76.65 | Improves? |

**The out-of-sample drop is small (1% accuracy), but this is misleading because:**
- Both discovery and holdout have negative PnL
- The holdout window has only 49 trades vs 73 in full range
- Window 2 (2026) shows extreme negative PnL (-5,140) — the config is not robust

---

## 8. Sanity Check Details

### ✅ Null Config: PASS
- Accuracy: 0.517 (expected ~50%)
- No systematic bias

### ✅ Permutation Test: PASS
- Accuracy: 0.5006 (expected ~50%)
- No data leakage detected

### ❌ Synthetic Recovery: FAIL
- Parameter: `kelly_fraction`
- Known optimum: 1.0
- Recovered: 0.1
- All 5 values produced identical PnL (490.15)
- **Root cause:** The evaluator doesn't distinguish between different kelly_fraction values on KNYC. This suggests the backtest is insensitive to risk management parameters on a single-station, all-seasons setup.

---

## 9. Top Parameter Effects

### By Effect Size (L0 Prescreen)
1. **stop_loss_kind** (6.17) — Gates type drives most variance
2. **regime_season_filter** (3.56) — djf season filter is critical
3. **regime_region_filter** (1.65) — continental region filter
4. **position_sizing_model** (0.73) — sizing model choice
5. **regime_cycle_filter** (0.66) — 12z cycle alignment

### By Sensitivity Score (L1 Grid)
1. **mod_directional_weight** (366,860,000) — Extreme edge sensitivity
2. **slippage_budget** (636,290,000) — Extreme edge sensitivity
3. **station_tier3_cap** (584,240,000) — Extreme edge sensitivity
4. **edge_threshold** (38,460,000) — Extreme edge sensitivity
5. **cal_shrinkage_strength** (36,090,000) — Extreme edge sensitivity

**Note:** Extreme sensitivity scores (>1e6) indicate the metric is at a boundary where small changes produce massive scalarized score swings. These are often artifacts of the scalarization formula, not genuine signal.

---

## 10. Errors & Anomalies

| Severity | Issue | Impact |
|---|---|---|
| ❌ CRITICAL | Synthetic recovery FAILED | Pipeline cannot distinguish parameter values for kelly_fraction |
| ❌ CRITICAL | Optimal config PnL negative (-479) despite acc improvement | Consensus bonus degraded, config is not profitable |
| ⚠️ HIGH | Regime sensitive flagged (variance=0.0068) | Config does not generalize across time periods |
| ⚠️ HIGH | Window 2 PnL = -5,140 (only 24 trades) | Extreme outlier, config unstable |
| ⚠️ MEDIUM | 26/42 parameters have zero effect | Parameter space is too large for single-station optimization |
| ⚠️ MEDIUM | Robustness std = 0.0 across seeds | Deterministic evaluation, no stochastic testing |
| ⚠️ LOW | Walk-forward windows show regime shift | Odd/even years differ |
| ℹ️ INFO | Ridge sensitivity scores >1e6 | Edge peaks may be scalarization artifacts |
| ℹ️ INFO | Missing forecast_bias.json | Agreement uses raw forecasts |

---

## 11. Improvement Suggestion

### Primary: Add PnL-aware objective weighting

**The current consensus bonus optimizes for agreement with the ensemble, which produces a config that is accurate but unprofitable.** The merged config achieves 0.681 accuracy with negative PnL because the scalarization function weights accuracy and agreement heavily, allowing PnL to degrade.

**Fix:** Replace the pure consensus bonus with a weighted composite that includes PnL:
```python
composite_score = 0.3 * accuracy + 0.3 * sharpe_ratio + 0.4 * (pnl / max(abs(pnl), 1))
```
This ensures the optimizer cannot sacrifice all PnL for marginal accuracy gains. The 0.4 weight on PnL forces the search toward configurations that are both accurate *and* profitable.

**Why this helps:** The current sweep prefers configs that are "correct but don't trade much" — high accuracy/low volume. Adding PnL weight forces the optimizer to find parameter combinations that actually make money per trade, not just predict correctly on rare high-confidence signals.

---

## 12. Answers to Verification Questions

### Q1: Does the pipeline run end-to-end on a single station?
**Yes.** All 5 levels completed in 524.6s (8.7 minutes).

### Q2: Does consensus bonus select a config with positive P&L?
**No.** The consensus bonus-optimized config has negative PnL (-479.1) despite higher accuracy. The consensus bonus itself degraded from 0.869 to 0.850.

### Q3: Does walk-forward show genuine out-of-sample drop?
**Partially.** The accuracy drop is small (+0.164 → +0.157 in OOS, ~1% drop), but the PnL drop is catastrophic (window 2: -5,140). The config is regime-sensitive and not robust.

### Q4: Does synthetic recovery pass?
**No.** It fails for `kelly_fraction` — the evaluator cannot distinguish between parameter values, producing identical PnL (490.15) for all 5 values tested.

---

*Report generated by Gilfoyle subagent, 2026-08-11T02:13 UTC*