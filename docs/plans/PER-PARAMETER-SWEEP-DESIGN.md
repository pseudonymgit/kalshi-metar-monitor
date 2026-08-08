# Per-Parameter Sweep Design — Definitive Specification

**Author:** Final Synthesis Expert (resolving 3-expert review with Dan's full-arsenal requirements)
**Date:** 2026-08-07
**Status:** Buildable Specification — Ready for Gilfoyle Implementation
**Objective:** Replace the shotgun 5000-config LHS random sweep with a principled, buildable optimization pipeline. Grid-first, Bayesian-if-needed. ~183 evaluations primary path; ~283 with conditional BO escalation.

---

## Table of Contents

1. [Executive Summary: The Decision] (#1-executive-summary-the-decision)
2. [Level 0: Sanity-Check & Sensitivity Pre-Screen] (#2-level-0-sanity-check--sensitivity-pre-screen)
3. [Level 1: Per-Parameter Grid Sweep] (#3-level-1-per-parameter-grid-sweep)
4. [Level 2: Interaction & Validation] (#4-level-2-interaction--validation)
5. [Level 3: Conditional Bayesian Optimization] (#5-level-3-conditional-bayesian-optimization)
6. [Level 4: Walk-Forward & Robustness] (#6-level-4-walk-forward--robustness)
7. [Multi-Metric Baselines] (#7-multi-metric-baselines)
8. [Stopping Criteria & Decision Tree] (#8-stopping-criteria--decision-tree)
9. [Complete Parameter Registry] (#9-complete-parameter-registry)
10. [Evaluation Function Interface] (#10-evaluation-function-interface)
11. [Error Handling & Edge Cases] (#11-error-handling--edge-cases)
12. [Implementation Steps for Gilfoyle] (#12-implementation-steps-for-gilfoyle)

---

## 1. Executive Summary: The Decision

### 1.1 Why Not Pure Bayesian Optimization?

Bayesian optimization (BO) is the right tool for joint high-dimensional optimization with unknown interactions. But it is NOT the right tool for this first sweep for one dominant reason: **we don't yet know if interactions are material.** Adding BO pre-commits to ~100 sequential evaluations, a new dependency (`scikit-optimize`), 3 new tuning parameters (kernel, noise, acquisition), and a scalarization function that must be correct on the first try.

The third-party reviewer's argument wins: grid-first, BO-if-needed.

### 1.2 Why Not Pure Grid?

The grid spec (93 + 53 = 146 evaluations) is excellent and sufficient under the assumption of weak interactions. But Dan specifically asked about interaction depth (pairs, triples, groups, 20s). The grid spec's pairwise test only covers the top 3 parameters — too narrow for production confidence.

### 1.3 The Definitive Resolution

**Primary path (Levels 0→1→2→4):** ~183 evaluations, no BO required.
- Sanity-check suite first (verify evaluation function is correct)
- Sensitivity pre-screen to reduce 20 parameters to ~8 significant
- Per-parameter grid on significant parameters (interpretable charts)
- Full pairwise interaction test on ALL significant parameters (not just top 3)
- Group-level meta-interaction test
- Walk-forward validation for overfitting protection

**Escalation path (Level 3, conditional):** Add 0-100 evaluations if Level 2 finds material interactions.
- Hot-start GP from grid results
- Limited to interacting subgroup only (not full 20-parameter space)
- Scikit-optimize implementation

**Dan's full-arsenal structure (sensitivity → per-parameter → interactions → groups → stop) is the organizing framework.** Each level has explicit stopping criteria based on noise floor.

### 1.4 Why This Resolves the Expert Contradictions

| Disagreement | Resolution |
|---|---|
| Grid vs Hybrid vs BO-if-needed | Grid-first, BO-if-needed. The grid's own interaction test (Level 2) gates the BO decision. No pre-commitment. |
| 186 vs 276 evaluations | 183 primary. 283 with BO. Both numbers are correct under their respective assumptions — this spec makes the assumption explicit and testable. |
| Scalarization: running stats vs fixed refs | Running statistics, calibrated from Level 0 sensitivity results. The code example that used fixed refs was wrong — removed. |
| Phase 3 eval count: 53 vs 69 | 53 is the correct validation protocol. The 69-eval variant included a categorical-continuous grid that was redundant with Level 3. |
| Noise parameter: fixed vs learned | Fixed at 0.005 for GP if BO is triggered. The grid uses noise-aware peak detection (requiring peak-to-valley > 3σ), not a GP noise parameter. |
| Missing Sections 11-17 | All consolidated into this document. No placeholders. |

| Finding | Severity | Resolution |
|---|---|---|
| 5 errors found by third-party reviewer | Critical | All 5 fixed (see above) |
| No backtest sanity-check suite | Critical | Level 0 added: synthetic data, permutation, null-config |
| "Accuracy" undefined | Critical | Section 7 defines all 8 metrics unambiguously |
| Parameter dependency structure ignored | High | Section 9 includes PARAM_DEPENDENCIES map |
| No production safety valve | High | Section 8.3: degradation threshold, rollback, paper-trading |
| No regime-shift handling | Medium | Level 4: walk-forward + odd/even year split |
| Missing complete parameter list | High | Section 9: full 22-parameter table |
| Missing evaluation function interface | High | Section 10: function signature, keys, return schema |
| Missing output schema | Medium | Section 12 includes JSON schema |
| Missing error handling | Medium | Section 11: crashes, non-convergence, degenerate results |

---

## 2. Level 0: Sanity-Check & Sensitivity Pre-Screen

### 2.1 Purpose

Before any optimization, verify that the evaluation function is correct and identify which parameters matter. This prevents GIGO (garbage in, garbage out) at Levels 1-4.

### 2.2 Sanity-Check Suite (Mandatory Prerequisite)

**Nothing below Level 0 runs until all 3 checks pass.**

#### Check 1: Null-Config Test
```
config = ALL_DEFAULTS
result = evaluate_config(config, seed=42, window=FULL_WINDOW)

PASS if: abs(result['accuracy'] - 0.50) < 0.03   (≈50% within 3% margin)
FAIL if: result['accuracy'] > 0.53 or result['accuracy'] < 0.47
```

If the default config shows >53% accuracy, something is systematically biased (data leakage, look-ahead, survivorship). **Stop and investigate before any optimization.**

#### Check 2: Permutation Test (Label Shuffle)
```
shuffled_labels = random_shuffle(y_true)
config = BEST_GUESS_CONFIG
result = evaluate_config_permuted(config, shuffled_labels, ...)

PASS if: abs(result['accuracy'] - 0.50) < 0.02   (≈50% within 2%)
FAIL if: result['accuracy'] > 0.52
```

If a config shows >52% accuracy on randomly permuted labels, there is data leakage (future information in the features). **Stop and fix data pipeline before any optimization.**

#### Check 3: Synthetic Data Recovery
```
# Create a synthetic weather signal with KNOWN optimal parameters
synth_config = {
    'edge_threshold': 0.05,
    'kelly_fraction': 1.0,
    'kl_dro_lambda': 0.5,
    # ... all other params at defaults
}

# Generate synthetic data where this config is provably optimal
synth_data = generate_synthetic_weather_data(known_optimum=synth_config)

# Run a mini-sweep of edge_threshold around 0.05
sweep_results = sweep_param('edge_threshold', [0.01, 0.03, 0.05, 0.07, 0.10], 
                              window=synth_data, seed=42)

PASS if: argmax(sweep_results) == 0.05  (recovers known optimum)
FAIL if: argmax(sweep_results) far from 0.05
```

**Cost of sanity-check suite:** 3 evaluations. Irrelevant compared to the cost of optimizing a broken evaluation function.

### 2.3 Sensitivity Pre-Screen (Evaluations: 30)

**Purpose:** Reduce the 22-parameter space to only the parameters that materially affect the metric. This is Dan's "sensitivity analysis first" requirement.

**Method:** For each parameter, evaluate at 2 extreme values + midpoint, holding all other parameters at defaults.

```
for each parameter p in [p₁, p₂, ..., p₂₂]:
    x_low = p.min_bound
    x_mid = (p.min_bound + p.max_bound) / 2   (or categorical default)
    x_high = p.max_bound
    
    config_high = DEFAULT_CONFIG.copy()
    config_high[p.name] = x_high
    
    config_low = DEFAULT_CONFIG.copy()
    config_low[p.name] = x_low
    
    s_high = evaluate_config(config_high, ...)['scalarized_metric']
    s_low = evaluate_config(config_low, ...)['scalarized_metric']
    effect = abs(s_high - s_low)
    
    if effect > NOISE_FLOOR:   # typically 0.5% of metric range
        mark as SIGNIFICANT — include in Level 1 sweep
    else:
        mark as INSENSITIVE — freeze at default for Level 1
```

**Expected result:** 5-8 of 22 parameters will be significant. The others will show sub-noise effects.

**Why not use the full 5-7 point grid for sensitivity?** Because sensitivity only needs extremes + midpoint. The full grid is Phase 1 of Level 1. Separating them avoids spending 7 evaluations on parameters that turn out to be insensitive.

**Cost:** 22 parameters × 2 extremes = 44 evaluations for the full set. But the midpoints are shared (DEFAULT_CONFIG is common), so: 22 × 2 + 1 = **45 evaluations** → round to **~30 evaluations** by batching (many parameters share configs).

### 2.4 Noise Floor Estimation (Required before Level 1)

```
# Run DEFAULT_CONFIG 5 times with different seeds
config = ALL_DEFAULTS
accuracies = []
for seed in [42, 101, 777, 2024, 9999]:
    result = evaluate_config(config, seed=seed)
    accuracies.append(result['accuracy'])

NOISE_STD = std(accuracies)
NOISE_FLOOR = 2 * NOISE_STD / sqrt(5)   # 95% CI, 5 replicates
```

**If NOISE_FLOOR > 1.0%** (absolute accuracy), the evaluation is too noisy. Increase backtest window length or replicate each evaluation 3 times before proceeding.

**If NOISE_FLOOR < 0.3%**, use σ_noise = 0.3% in peak detection (minimum detectable effect).
**If NOISE_FLOOR > 0.3%**, use the measured NOISE_FLOOR as σ_noise.

**Cost:** 5 evaluations (one-time calibration, reusable across all levels).

### 2.5 Level 0 Total

| Sub-phase | Evaluations |
|---|---|
| Sanity-check suite | 3 |
| Sensitivity pre-screen | ~30 |
| Noise floor estimation | 5 |
| **Level 0 Total** | **~38** |

---

## 3. Level 1: Per-Parameter Grid Sweep

### 3.1 Scope

Only significant parameters (identified in Level 0). Typically 5-8 parameters. Insignificant parameters are frozen at their Level 0 default value.

### 3.2 Continuous Parameter Grid

**For each significant continuous parameter:**
- Sample **7 evenly spaced values** across the full range.
- Exception: `slippage_budget [0.0-0.01]` and `fee_deduction [0.0-1.0]` use 5 points (small range or linear behavior expected).

**For the log-scaled parameter (`max_contracts [10-5000]`):**
- Sample 7 points on a log scale: `10, 30, 100, 300, 1000, 3000, 5000`.

**Evaluation protocol:**
- Fixed seed across all evaluations in Level 1 (use Level 0 noise floor seed).
- Record all 8 metrics (Section 7) for every evaluation.
- Primary objective: scalarized score (Section 7.3).

### 3.3 Categorical Parameter Evaluation

**For each significant categorical parameter:**
- Try all values (3-5 per parameter, see Section 9).

**Insensitive categorical parameters:** freeze at default. Do not evaluate.

### 3.4 Peak Detection

For each parameter, apply the peak detector to the 7-point grid:

```
Let p = [p₁, ..., pₙ] sorted, s = [s₁, ..., sₙ] scores.

Define: Δ⁺(i) = sᵢ₊₁ - sᵢ, Δ⁻(i) = sᵢ - sᵢ₋₁

if Δ⁻(i) > 0 AND Δ⁺(i) < 0:
    peak at i
    if |Δ⁺(i)| < σ_noise AND |Δ⁻(i)| < σ_noise:
        PLATEAU — do not refine
    else:
        CLEAR PEAK — refine in Phase 1.2
    
if multiple peaks exist with valley > σ_noise between them:
    BIMODAL — refine both peaks
    
if peak at position 1 or n:
    EDGE — extend range 20% beyond boundary
```

### 3.5 Phase 1.2: Refinement

Applied only to parameters with CLEAR PEAK or BIMODAL classification:

| Classification | Refinement Action | Evaluations |
|---|---|---|
| CLEAR PEAK (interior) | 3 points: peak ± 0.25 × step_size | 3 |
| CLEAR PEAK (edge) | Extend 20% beyond edge, then 3 points | 5 |
| BIMODAL | 3 points around PEAK₁ + 3 around PEAK₂ | 6 |
| PLATEAU | No refinement. Use midpoint. | 0 |

If refined peak is at a new endpoint, repeat refinement one more level: 2 additional points.

### 3.6 Log-Scaled Parameter Refinement

Same logic as continuous, but refine on log scale. If Phase 1 shows increasing accuracy up to the upper edge (5000), extend to 10000 with one additional point.

### 3.7 Level 1 Evaluation Budget

Assume 8 significant parameters (conservative upper bound — typical is 5-6):

| Parameter Type | Count | Points/Param | Evaluations |
|---|---|---|---|
| Continuous (significant) | ~5 | 7 | 35 |
| Log-scaled (significant) | ~1 | 7 | 7 |
| Categorical (significant) | ~2 | 3-4 | 8 |
| **Phase 1.1 (coarse) total** | | | **~50** |
| | | | |
| Continuous (clear peak refine) | ~3 | 3 | 9 |
| Continuous (edge refine) | ~1 | 5 | 5 |
| Continuous (bimodal refine) | ~0.5 | 6 | 3 |
| Log-scaled refine | ~1 | 3 | 3 |
| **Phase 1.2 (refine) total** | | | **~20** |
| | | | |
| **Level 1 Total** | | | **~70** |

**If 8 parameters are all significant, Level 1 ≈ 70 evaluations. If only 5 are significant, ≈ 44 evaluations.**

### 3.8 Level 1 Output

For each significant parameter:
- Optimal value (peak)
- Type: CLEAR / PLATEAU / BIMODAL / EDGE
- Full 7-point sweep data (for interpretable charts)
- Sensitivity score: `(max_score - min_score) / max_score`

For insignificant parameters:
- Frozen at default
- Sensitivity score: `< noise floor`

---

## 4. Level 2: Interaction & Validation

### 4.1 Build the Optimal Config

```
C₀ = DEFAULT_CONFIG.copy()
for each parameter p (significant or not):
    C₀[p.name] = p.optimal_value  (peak, midpoint, or chosen bimodal peak)
```

### 4.2 ±1-Step Validation (Evaluations: 1 + 2×P)

For each of the P parameters (all 22, not just significant):

```
C_minus = C₀.copy()
C_minus[p] = p.optimal - 1_step   (or nearest Phase 1/2 grid point below)

C_plus = C₀.copy()
C_plus[p] = p.optimal + 1_step   (or nearest grid point above)

s₀ = evaluate_config(C₀)
s⁻ = evaluate_config(C_minus)
s⁺ = evaluate_config(C_plus)

Δ⁻ = s₀ - s⁻
Δ⁺ = s₀ - s⁺
```

| Condition | Interpretation |
|---|---|
| `Δ⁻ < σ_noise AND Δ⁺ < σ_noise` | Independent at optimum — parameter is stable |
| `Δ⁻ > σ_noise OR Δ⁺ > σ_noise` | Locally sensitive — expected for CLEAR PEAK |
| `Δ⁻ > σ_noise AND Δ⁺ > σ_noise AND opposite signs` | Genuine optimum — verified |
| `Δ⁻ > σ_noise AND Δ⁺ > σ_noise AND same sign` | Optimum is at edge of sweep range — may need extension |

**Cost:** 1 (C₀) + 2 × 22 = **45 evaluations**.

### 4.3 Full Pairwise Interaction Test (Evaluations: C(P_sig, 2) × 4)

Test ALL pairs of the S = number of significant parameters (not just top 3):

```
For each pair (a, b) from the S significant parameters:
    s₀₀ = evaluate at (opt_a, opt_b)
    s₀₁ = evaluate at (opt_a, opt_b + 1_step)
    s₁₀ = evaluate at (opt_a + 1_step, opt_b)
    s₁₁ = evaluate at (opt_a + 1_step, opt_b + 1_step)
    
    I = s₁₁ + s₀₀ - s₁₀ - s₀₁   # interaction metric
    
    if |I| > σ_noise:
        material interaction detected
        if s₁₁ > s₀₀:
            # Interaction improves the optimum
            FLAG for Level 3 escalation
```

**Cost:** C(S, 2) × 4 evaluations. For S=8: 28 × 4 = **112 evaluations** (too many).

**Optimization:** Use a 2×2 grid (4 evals) per pair. For S=8, total = 112. This is the largest evaluation block in Level 2.

**Alternative (recommended):** Reduce pairwise testing to only the top 5 most sensitive parameters (identified in Level 0 + Level 1). C(5,2) = 10 pairs × 4 = **40 evaluations**. This is the default.

**Escalation condition for full pairwise test:** If any interaction among the top 5 exceeds a moderate threshold (|I| > 1.5 × σ_noise), expand to the full S significant set. Add C(S,2) - C(5,2) additional pairs.

**Cost:** 40 evaluations (default) to 112 (if escalation triggered).

### 4.4 Group-Level Meta-Interaction Test (Evaluations: ~20)

Test whether the 6 parameter groups (Signals, Fees, Gates, Levers, Lanes, Modulators) interact with each other:

```
# Build group-optimal configs
G₁ = config with all Signal params at their optimal values, others at defaults
G₂ = config with all Fee params at their optimal values, others at defaults
G₃ = ...Gates...
G₄ = ...Levers...
G₅ = ...Lanes...
G₆ = ...Modulators...

# Test each group against C₀
for each group Gi:
    test_config = C₀ with Gi's params set to Gi's optimal values
    s_group = evaluate(test_config)
    s_C0 = evaluate(C₀)
    
    if s_group > s_C0 + σ_noise:
        meta-interaction detected — Gi's per-parameter optima don't combine
        add Gi to the list of groups needing joint optimization
```

**If meta-interactions are found (typically 0-2 groups),** run a small joint optimization on the interacting groups: LHS grid of 50-100 configs varying the ~5-10 parameters across all interacting groups.

**Cost:** 6 group tests × 1 evaluation each = **6 evaluations**. + 50-100 if joint optimization triggered.

**Cost with joint optimization:** 6 + 80 = **~86 evaluations total for Level 2 meta-interaction path**. This is only triggered if meta-interactions exist.

### 4.5 Level 2 Eval Budget Summary

| Sub-phase | Evaluations |
|---|---|
| ±1-step validation | 45 |
| Pairwise interaction (top 5 pairs) | 40 |
| Group-level meta-interaction test | 6 |
| **Level 2 Base Total** | **~91** |
| **If pairwise escalation triggered** | +72 (additional pairs) = 163 |
| **If joint optimization triggered** | +80 (LHS on interacting groups) = 177 |

**Primary path (no escalation):** 91 evaluations.
**Full worst case:** 177 evaluations (rare — typically 0-1 triggers fire).

### 4.6 Interaction Decision Gate

After Level 2, evaluate the **gate condition** for Level 3:

```
INTERACTION_COUNT = number of pairs with |I| > σ_noise
JOINT_IMPROVEMENT = max(total gain from any interaction, from group test)

if INTERACTION_COUNT > 0 AND JOINT_IMPROVEMENT > σ_noise:
    ESCALATE to Level 3
else:
    SKIP Level 3 — interactions are negligible
    Proceed directly to Level 4
```

**Expected outcome:** In most cases, interactions are small (< σ_noise) and Level 3 is skipped. The grid was sufficient. Total: 38 + 70 + 91 + 5 = **~204 evaluations**.

---

## 5. Level 3: Conditional Bayesian Optimization

### 5.1 Trigger Condition

Only runs if Level 2 found material interactions (see Section 4.6 gate condition).

### 5.2 Scope Restriction

BO is NOT run on the full 22-parameter space. It is restricted to the **interacting subgroup** only:

```
INTERACTING_PARAMS = set of all parameters involved in any 
                     pair with |I| > σ_noise, plus all parameters
                     in any interacting group (from meta-interaction test)
```

If 3 pairs are detected among 5 parameters, the interacting subgroup is those 5 parameters. The remaining 17 parameters are frozen at their Level 1 optimal values.

### 5.3 Implementation

Using `scikit-optimize`:

```python
from skopt import gp_minimize
from skopt.space import Real, Integer, Categorical

# Build search space for interacting params only
space = []
param_map = {}
for p in INTERACTING_PARAMS:
    spec = p.spec  # from Section 9 registry
    if spec['type'] == 'continuous':
        space.append(Real(spec['min'], spec['max'], name=p.name))
    elif spec['type'] == 'integer':
        space.append(Integer(spec['min'], spec['max'], name=p.name))
    elif spec['type'] == 'categorical':
        space.append(Categorical(spec['values'], name=p.name))
    param_map[p.name] = len(param_map)  # index in the space list

# Prepare hot-start from grid results
# Each grid evaluation becomes one training point
# Non-interacting params are set to their Level 1 optima
X_init = []  # list of tuples matching space dimensions
y_init = []  # list of scalarized scores
for record in LEVEL_1_RESULTS:
    x = []
    for p_name in INTERACTING_PARAMS:
        x.append(record['config'][p_name])
    X_init.append(tuple(x))
    y_init.append(record['scalarized_score'])

# Run BO
result = gp_minimize(
    lambda x: -scalarized_metric(
        evaluate_config(build_config(x, param_map))
    ),
    space,
    n_calls=100,          # BO evaluations
    x0=X_init,            # Hot-start from grid
    y0=y_init,
    acq_func='EI',        # Expected Improvement
    noise=0.005,          # Fixed noise level
    initial_point_generator='sobol',  # if no grid data available
)
```

### 5.4 GP Configuration

| Parameter | Value | Rationale |
|---|---|---|
| **Kernel** | Matérn 5/2 + ARD | Twice-differentiable; realistic for financial surfaces |
| **Acquisition** | EI (Expected Improvement) | Parameter-free; balanced exploration/exploitation |
| **Noise** | Fixed at 0.005 for first 50 evals | Prevents GP overfitting with sparse data |
| **Noise (after 50)** | Learned via MLE | Enough data to estimate reliably |
| **Refit frequency** | Every 20 evaluations | Balances accuracy vs computational cost |
| **Prior mean** | Constant mean (learned) | Avoids zero-mean bias toward 0 |
| **Categorical encoding** | One-hot + ARD (6 categories max) | ARD naturally learns irrelevant dimensions |

### 5.5 Convergence & Stopping

```
# Track the best observed value
best_observed = max(y_init)
for i in range(100):
    # After each batch of 10 evaluations
    if i % 10 == 0 AND i >= 20:
        gp_best = gp.predict(best_candidate)  # GP's predicted best
        ei_next = gp_minimize_result.fun       # expected improvement at next point
        
        if ei_next < 0.001:                    # EI below threshold
            STOP_LEVEL_3 — converged
        elif abs(gp_best - best_observed) < 0.002:  # GP & observed converge
            STOP_LEVEL_3 — saturated
```

### 5.6 Level 3 Eval Budget

| Sub-phase | Evaluations |
|---|---|
| Hot-start (from Level 1) | 0 (existing data) |
| BO evaluations | 100 |
| **Level 3 Total** | **100** |

---

## 6. Level 4: Walk-Forward & Robustness

### 6.1 Walk-Forward Validation (Evaluations: 5)

Test the optimal config across different time windows:

```
C_final = the best config from Level 1, 2, or 3

windows = [
    (2020-01-01, 2021-12-31),   # Year 1-2
    (2022-01-01, 2023-12-31),   # Year 3-4
    (2024-01-01, 2025-12-31),   # Year 5-6
]

for each window in windows:
    result = evaluate_config(C_final, seed=42, window=window)
    record(result['accuracy'], result['sharpe'], result['pnl'])

accuracy_variance = max(accuracies) - min(accuracies)

if accuracy_variance > 2 * σ_noise:
    FLAG: config is regime-sensitive — accuracy varies significantly by year
```

### 6.2 Odd/Even Year Split (Evaluations: 2)

```
C_odd = optimize on odd years (2021, 2023)
C_even = optimize on even years (2022, 2024)

test C_odd on even years
test C_even on odd years

if accuracy(C_odd_on_even) - accuracy(C_even_on_even) > σ_noise:
    FLAG: regime shift between odd/even years
    config is sensitive to some yearly pattern
```

**Note:** This is a minimal check with only 2-3 years of data. With more historical data (5+ years), use a formal moving-window cross-validation.

### 6.3 Robustness Validation (Evaluations: 5)

Run the optimal config with 5 different random seeds:

```
seeds = [42, 101, 777, 2024, 9999]
results = [evaluate_config(C_final, seed=s) for s in seeds]

mean_acc = mean([r['accuracy'] for r in results])
std_acc = std([r['accuracy'] for r in results])

if std_acc > σ_noise:
    FLAG: config is noise-sensitive — variance exceeds expected noise floor
```

### 6.4 Level 4 Eval Budget

| Sub-phase | Evaluations |
|---|---|
| Walk-forward (3 windows) | 3 |
| Odd/even split | 2 |
| Robustness (5 seeds) | 5 |
| Shared evaluations | -5 (some overlap with walk-forward) |
| **Level 4 Total** | **~5** |

---

## 7. Multi-Metric Baselines

### 7.1 Metric Definitions

Every evaluation returns a dict with exactly these 8 metrics:

| Metric | Key | Definition | Range | Direction |
|---|---|---|---|---|
| **Directional Accuracy** | `accuracy` | Fraction of trades where the predicted direction (UP/DOWN) matches the actual settlement direction. Computed across ALL generated signals (not just traded). | 0.0-1.0 | ↑ higher |
| **Temperature Accuracy** | `temp_accuracy` | Fraction of predictions where the implied temperature change direction matched the actual METAR temperature delta over the forecast horizon. | 0.0-1.0 | ↑ higher |
| **Market Direction Accuracy** | `market_accuracy` | Fraction of traded contracts where the directional prediction (YES/NO) matched the settlement. This is the realized trading accuracy. | 0.0-1.0 | ↑ higher |
| **Total PnL** | `total_pnl` | Net profit/loss in USD across all trades in the backtest window. Includes all transaction costs, slippage, and fees. | unbounded | ↑ higher |
| **Sharpe Ratio** | `sharpe` | Annualized risk-adjusted return: mean(excess return) / std(return) * sqrt(252). | 0.0-5.0+ | ↑ higher |
| **Brier Score** | `brier` | Mean squared error of probability forecasts: (1/N) * Σ(p_i - o_i)² where p is predicted probability and o is binary outcome. | 0.0-1.0 | ↓ lower |
| **Trade Frequency** | `trades_per_day` | Average number of trades executed per trading day in the backtest window. | 0.0-∞ | informational |
| **Max Drawdown** | `max_drawdown` | Largest peak-to-trough decline in cumulative PnL as a fraction of peak equity. | 0.0-1.0 | ↓ lower |

### 7.2 Recording Protocol

Every evaluation stores all 8 metrics. The evaluation function returns a dict, not a scalar:

```python
def evaluate_config(config, seed, window) -> dict:
    """
    Returns dict with keys: accuracy, temp_accuracy, market_accuracy,
    total_pnl, sharpe, brier, trades_per_day, max_drawdown
    """
```

**Storage format (sweep-results/metrics/phase1/param_name.json):**
```json
{
  "parameter": "edge_threshold",
  "phase": "LEVEL_1_COARSE",
  "values": [0.001, 0.034, 0.067, 0.100, 0.133, 0.167, 0.200],
  "metrics": {
    "accuracy": [0.62, 0.65, 0.68, 0.67, 0.66, 0.64, 0.63],
    "sharpe": [0.8, 1.1, 1.4, 1.3, 1.2, 1.0, 0.9],
    "total_pnl": [1200, 3400, 5600, 5100, 4300, 2800, 1500],
    "brier": [0.24, 0.22, 0.19, 0.20, 0.21, 0.23, 0.24],
    "temp_accuracy": [0.58, 0.61, 0.64, 0.63, 0.62, 0.60, 0.59],
    "trades_per_day": [12, 10, 7, 6, 5, 4, 3],
    "max_drawdown": [0.15, 0.12, 0.08, 0.09, 0.10, 0.13, 0.14]
  },
  "scalarized_score": [-0.32, 0.15, 0.68, 0.55, 0.41, -0.05, -0.28],
  "optimal_value": 0.067,
  "peak_type": "CLEAR_PEAK",
  "noise_floor": 0.005
}

### 7.3 Scalarized Objective Function

The scalarized score drives optimization decisions (peak detection, pairwise interaction comparison).

**Formula:**

$$f(x) = \alpha \cdot \frac{\text{acc}(x) - \mu_{\text{acc}}}{\sigma_{\text{acc}}} + \beta \cdot \frac{\text{Sharpe}(x) - \mu_{\text{Sharpe}}}{\sigma_{\text{Sharpe}}} + \gamma \cdot \frac{\text{PnL}(x) - \mu_{\text{PnL}}}{\sigma_{\text{PnL}}} - \delta \cdot \frac{\text{Brier}(x) - \mu_{\text{Brier}}}{\sigma_{\text{Brier}}} - \epsilon \cdot \frac{\text{DD}(x) - \mu_{\text{DD}}}{\sigma_{\text{DD}}}$$

**Default weights:**
- $\alpha = 0.35$ (accuracy)
- $\beta = 0.35$ (Sharpe)
- $\gamma = 0.15$ (PnL)
- $\delta = 0.10$ (Brier score — subtracted; lower is better)
- $\epsilon = 0.05$ (max drawdown — subtracted; lower is better)

**Normalization:** Running statistics $\mu$ and $\sigma$ are recomputed every 20 evaluations across ALL observed data points. This keeps the scalarization scale-invariant as the optimization discovers better-performing regions.

**Implementation:**

```python
def compute_scalarized_score(metrics: dict, mu_stats: dict, sigma_stats: dict) -> float:
    """Compute scalarized objective with running statistics.
    
    mu_stats and sigma_stats are refit every 20 evaluations across all observed data.
    """
    alpha, beta, gamma, delta, epsilon = 0.35, 0.35, 0.15, 0.10, 0.05
    
    acc_norm = (metrics['accuracy'] - mu_stats['accuracy']) / max(sigma_stats['accuracy'], 0.01)
    sharpe_norm = (metrics['sharpe'] - mu_stats['sharpe']) / max(sigma_stats['sharpe'], 0.1)
    pnl_norm = (metrics['total_pnl'] - mu_stats['total_pnl']) / max(sigma_stats['total_pnl'], 100)
    brier_norm = (metrics['brier'] - mu_stats['brier']) / max(sigma_stats['brier'], 0.01)
    dd_norm = (metrics['max_drawdown'] - mu_stats['max_drawdown']) / max(sigma_stats['max_drawdown'], 0.01)
    
    return (alpha * acc_norm + beta * sharpe_norm + gamma * pnl_norm
            - delta * brier_norm - epsilon * dd_norm)
```

---

## 8. Stopping Criteria & Decision Tree

### 8.1 Level-by-Level Stop Conditions

| Level | Stop When | Irrelevant Condition |
|---|---|---|
| **Level 0** | All 3 sanity checks pass AND noise floor estimated | GIGO risk — if checks fail, do not proceed |
| **Level 1** | All significant parameters swept + refined | Insignificant parameters frozen; plateau refinement skipped |
| **Level 2** | If interaction gate says NO: skip Level 3 entirely | If gate says YES: Level 3 is mandatory |
| **Level 3** | EI < 0.001 OR GP predicted best ≈ observed best | If Level 3 never triggered: no BO cost |
| **Level 4** | Variance across windows < 2 × σ_noise | If variance > 2σ: regime-flagged for human review |

### 8.2 Overall Decision Tree

```
START:
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Level 0: Sanity-Check Suite (3 evals)                   │
│ - Null-config test (accuracy ≈ 50%?)                    │
│ - Permutation test (accuracy ≈ 50% on shuffled labels?) │
│ - Synthetic data recovery (finds known optimum?)        │
└────────────────────┬────────────────────────────────────┘
                     │
        FAIL ┌───────▼───────┐ PASS
             │               │
             ▼               ▼
      STOP: FIX          ┌─────────────────────────────────────────┐
      DATA /             │ Level 0: Sensitivity Pre-Screen (~30)   │
      EVAL FUNCTION      │ + Noise floor estimation (5)            │
             │           └────────────────────┬────────────────────┘
             │                                │
             ▼                                ▼
                                     ┌─────────────────────────────────────────┐
                                     │ Level 1: Per-Parameter Grid (~70 evals)│
                                     │ - Significant params only (5-8 of 22)   │
                                     │ - 7-point coarse + 3-point refine      │
                                     │ - Categorical: all values               │
                                     └────────────────────┬────────────────────┘
                                                          │
                                                          ▼
                                     ┌─────────────────────────────────────────┐
                                     │ Level 2: Interaction & Validation (~91) │
                                     │ - ±1-step validation (45 evals)          │
                                     │ - Pairwise test top 5 sig pairs (40)    │
                                     │ - Group meta-interaction test (6)       │
                                     └────────────────────┬────────────────────┘
                                                          │
                                                          ▼
                                              ┌───────────┴───────────┐
                                     GATE:    │                       │
                                     NO       │                       │ YES
                                     material │                       │ material
                                     inter-   │                       │ inter-
                                     actions  │                       │ actions
                                          ┌───▼───┐              ┌────▼────┐
                                          │       │              │         │
                                          ▼       │              ▼         │
                                     ┌─────────┐ │      ┌────────────────┐ │
                                     │ SKIP    │ │      │ Level 3: BO    │ │
                                     │ Level 3 │ │      │ (100 evals on  │ │
                                     └────┬────┘ │      │ interacting    │ │
                                          │      │      │ subgroup only) │ │
                                          │      │      └───────┬────────┘ │
                                          │      │              │          │
                                          ▼      │              ▼          │
                                     ┌──────────────────────────────────────┘
                                     │
                                     ▼
                          ┌─────────────────────────────────────────┐
                          │ Level 4: Walk-Forward & Robustness (~5) │
                          │ - 3-window walk-forward                 │
                          │ - Odd/even year split                   │
                          │ - 5-seed robustness validation          │
                          └────────────────────┬────────────────────┘
                                               │
                                               ▼
                          ┌─────────────────────────────────────────┐
                          │ OUTPUT: optimal-config.json + report.md │
                          └─────────────────────────────────────────┘
```

### 8.3 Production Safety Valve

If the optimized config is deployed to production:

1. **Paper-trading period (minimum 30 days):** Run the new config in parallel with the current production config. Compare accuracy, Sharpe, PnL. Do NOT trade live with the new config until paper-trading shows improvement.

2. **Degradation threshold:** If live-trading accuracy drops below (paper-trading accuracy - 2 × σ_noise) for 5 consecutive trading days, auto-rollback to previous config. This catches regime shifts and config-environment mismatch.

3. **Max drawdown limit:** If live PnL drops more than 20% from peak equity, halt trading and email Dan. Do not automatically restart.

4. **A/B test protocol:** Run the new config on 50% of trades, the old config on 50%. Compare performance after 100 trades per arm. If the new config is not significantly better (p < 0.10 via one-sided t-test), revert to old config.

---

## 9. Complete Parameter Registry

### 9.1 Full 22-Parameter Table

| # | Name | Type | Min | Max | Default | Unit | Group | Depends On |
|---|---|---|---|---|---|---|---|---|
| 1 | `edge_threshold` | continuous | 0.001 | 0.20 | 0.05 | confidence | Signals | — |
| 2 | `confidence_floor` | continuous | 0.30 | 0.95 | 0.60 | probability | Signals | — |
| 3 | `member_weighting` | categorical | 0 | 3 | 0 | index | Signals | — |
| 4 | `entry_price_min` | continuous | 0.001 | 0.10 | 0.01 | price delta | Signals | — |
| 5 | `entry_price_max` | continuous | 0.05 | 0.50 | 0.15 | price delta | Signals | — |
| 6 | `max_contracts` | integer (log) | 10 | 5000 | 100 | contracts | Signals | — |
| 7 | `kl_dro_lambda` | continuous | 0.0 | 1.0 | 0.5 | — | Signals | — |
| 8 | `fee_type` | categorical | 0 | 3 | 0 | index | Fees | — |
| 9 | `slippage_budget` | continuous | 0.0 | 0.01 | 0.001 | fraction | Fees | — |
| 10 | `fee_deduction` | continuous | 0.0 | 1.0 | 0.5 | fraction | Fees | fee_type != 0 |
| 11 | `agreement_n` | integer | 2 | 5 | 2 | signals | Gates | — |
| 12 | `agreement_m` | integer | 5 | 11 | 7 | signals | Gates | — |
| 13 | `stop_loss_kind` | categorical | 0 | 2 | 0 | index | Gates | use_stop_loss=True |
| 14 | `calibration_mode` | categorical | 0 | 3 | 0 | index | Gates | — |
| 15 | `kelly_fraction` | continuous | 0.1 | 4.0 | 0.5 | — | Levers | position_sizing ∈ {1,2} |
| 16 | `position_sizing_model` | categorical | 0 | 2 | 0 | index | Levers | — |
| 17 | `capital_base` | continuous | 1000 | 100000 | 10000 | USD | Levers | — |
| 18 | `goldilocks_lane_enabled` | boolean | 0 | 1 | 0 | flag | Lanes | — |
| 19 | `trajectory_lane_enabled` | boolean | 0 | 1 | 0 | flag | Lanes | — |
| 20 | `fusion_mode` | categorical | 0 | 2 | 0 | index | Modulators | — |
| 21 | `spatial_coherence_enabled` | boolean | 0 | 1 | 0 | flag | Modulators | — |
| 22 | `holdout_start_frac` | continuous | 0.5 | 0.8 | 0.7 | fraction | Validation | — |

### 9.2 Parameter Dependency Map

```python
PARAM_DEPENDENCIES = {
    'kelly_fraction': {
        'depends_on': 'position_sizing_model',
        'active_when': {'position_sizing_model': [1, 2]},  # Kelly variants
        'default_when_inactive': 1.0  # default if fixed sizing (model=0)
    },
    'fee_deduction': {
        'depends_on': 'fee_type',
        'active_when': {'fee_type': [1, 2, 3]},  # Any fee model except 0
        'default_when_inactive': 1.0  # not deducted if no fee
    },
    'stop_loss_kind': {
        'depends_on': 'use_stop_loss',  # assumed flag
        'active_when': {'use_stop_loss': [True]},
        'default_when_inactive': 0
    }
}
```

**Impact on Level 1:** When sweeping a dependent parameter, only evaluate on configs where its dependency is satisfied. For example, `kelly_fraction` is only swept with `position_sizing_model ∈ {1, 2}`. When the dependency is not satisfied, use the default value.

**Impact on Level 3 (BO):** The GP search space for dependent parameters is restricted to their active regions. This prevents wasted evaluations on structurally meaningless configurations.

### 9.3 Categorical Parameter Values

| Parameter | Values | Description |
|---|---|---|
| `member_weighting` | 0=uniform, 1=accuracy, 2=recency, 3=combined | How to weight ensemble members |
| `fee_type` | 0=none, 1=Kalshi, 2=ForecastEx, 3=Polymarket | Fee schedule to apply |
| `stop_loss_kind` | 0=none, 1=fixed_pct, 2=trailing | Stop-loss mechanism |
| `calibration_mode` | 0=platt, 1=ece-binned, 2=beta, 3=ensemble | Calibration method applied to signal probabilities |
| `position_sizing_model` | 0=fixed, 1=kelly, 2=fractional_kelly | How to size positions |
| `fusion_mode` | 0=average, 1=weighted, 2=select_best | How to fuse multiple signals |

---

## 10. Evaluation Function Interface

### 10.1 Function Signature

```python
def evaluate_config(
    config: dict,
    seed: int = 42,
    window: tuple = None,  # (start_date, end_date), None = full window
    synthetic_data: dict = None  # for Level 0 sanity checks only
) -> dict:
    """
    Run a full backtest on the given configuration.
    
    Args:
        config: Dict mapping parameter names to values (see Section 9 for valid keys)
        seed: Random seed for reproducible backtest
        window: (start_date, end_date) tuple of datetime.date objects.
                If None, uses the full historical window.
        synthetic_data: If provided, runs on synthetic data instead of real data.
                        Used only for Level 0 sanity checks.
    
    Returns:
        Dict with all 8 metrics (see Section 7.1 for key definitions)
    
    Raises:
        ConfigError: If config contains invalid parameter name or value
        BacktestError: If the backtest engine fails during execution
    """
```

### 10.2 Config Validation

```python
def validate_config(config: dict) -> tuple[bool, list[str]]:
    """
    Validate a config dict against the parameter registry.
    
    Returns:
        (is_valid, error_messages)
        
    Checks:
    - Every key is a valid parameter name from Section 9
    - Every value is within [min, max] (continuous) or in Categorical.values
    - Dependency conditions are satisfied (e.g., kelly_fraction has position_sizing in {1,2})
    - No extraneous keys
    """
```

### 10.3 Constants

```python
DEFAULT_SEED = 42
DEFAULT_WINDOW = (date(2020, 1, 1), date(2025, 12, 31))
MAX_EVAL_TIMEOUT_SECONDS = 120  # individual evaluation timeout

# Parameter registry (single source of truth)
PARAMETER_REGISTRY = {
    'edge_threshold': {
        'type': 'continuous',
        'min': 0.001, 'max': 0.20, 'default': 0.05
    },
    # ... all 22 parameters
}
```

### 10.4 Existing Infrastructure

Use the existing `backtest_engine.run_config()` function. The sweep layer wraps it:

```python
import importlib

# The existing backtest engine
backtest = importlib.import_module('engine.backtest')

def evaluate_config(config, seed=42, window=None):
    """Wrapper - delegates to the existing backtest engine."""
    validate_config(config)  # raises ConfigError on invalid config
    
    result = backtest.run_config(
        config=config,
        random_seed=seed,
        date_range=window or DEFAULT_WINDOW
    )
    
    return {
        'accuracy': result['directional_accuracy'],
        'temp_accuracy': result['temperature_accuracy'],
        'market_accuracy': result['market_accuracy'],
        'total_pnl': result['total_pnl'],
        'sharpe': result['sharpe_ratio'],
        'brier': result['brier_score'],
        'trades_per_day': result['avg_trades_per_day'],
        'max_drawdown': result['max_drawdown'],
    }
```

---

## 11. Error Handling & Edge Cases

### 11.1 Backtest Crash

If `evaluate_config()` raises an exception for a specific parameter value:

```python
def safe_evaluate(config, seed, window):
    """Wrapper with crash recovery."""
    try:
        result = evaluate_config(config, seed, window)
        return result
    except Exception as e:
        log_error(f"Evaluation failed for config {config}: {e}")
        # Return a sentinel value - worst possible score
        return {
            'accuracy': 0.0,
            'temp_accuracy': 0.0,
            'market_accuracy': 0.0,
            'total_pnl': -1e12,
            'sharpe': -10.0,
            'brier': 1.0,
            'trades_per_day': 0.0,
            'max_drawdown': 1.0,
        }
```

The sentinel ensures the crash value is never chosen as optimal.

### 11.2 Degenerate Peak Detection

If the peak detector finds 0 peaks (flat surface):

```
if len(peaks) == 0:
    if max_score - min_score < σ_noise:
        # Truly flat - use midpoint
        optimal = midpoint
        peak_type = 'PLATEAU'
    else:
        # Monotonic trend - optimum is at an edge
        if score[0] > score[-1]:
            optimal = values[0]
        else:
            optimal = values[-1]
        peak_type = 'EDGE'
```

### 11.3 GP Non-Convergence (Level 3 only)

If the GP fitting fails (Cholesky error, singular kernel matrix):

```python
def safe_fit_gp(X, y):
    """GP fitting with fallback."""
    try:
        gp = GaussianProcessRegressor(kernel=Matérn52(ARD=True), ...)
        gp.fit(X, y)
        return gp
    except np.linalg.LinAlgError as e:
        # Singular matrix - add jitter to diagonal
        log_warning(f"GP Cholesky error: {e}. Adding jitter.")
        gp = GaussianProcessRegressor(
            kernel=Matérn52(ARD=True),
            alpha=1e-6,  # jitter on diagonal
            optimizer=None  # skip hyperparameter opt when unstable
        )
        gp.fit(X, y)
        return gp
```

### 11.4 Edge Case: 82-Member Ensemble

The 82-member ensemble is treated as 1 signal with 3 calibration variants (see Section 19.4 in prior drafts). For Level 1, it's a single categorical parameter. For deep-dive (Level 2+), the internal parameters of the ensemble (GEFS vs ECMWF weight, bias correction window) would be separate parameters — but these are NOT in the current sweep. They belong in a component-specific deep-dive after the cross-cutting sweep is complete.

### 11.5 Edge Case: Boundary Optima

If the optimal value for any parameter is at the extreme of its defined range:

1. Extend the range by 20% in the direction of the rising trend.
2. Evaluate one additional point at the new extreme.
3. If the trend continues upward, the true optimum is beyond the original range.
4. Flag this for human review: "OPTIMAL AT BOUNDARY — true optimum may be outside original range for parameter {name}."
5. Do NOT automatically extend the range for future sweeps — this is a design decision for Dan.

### 11.6 Edge Case: Categorical-Continuous Interaction

If a categorical parameter interacts with a continuous parameter (e.g., `calibration_mode` changes the optimal `edge_threshold`), the pairwise interaction test in Level 2 WILL detect this because it includes all pairs of significant parameters regardless of type.

---

## 12. Implementation Steps for Gilfoyle

### 12.1 Output Schema

**`sweep-results/optimal-config.json`:**

```json
{
  "meta": {
    "timestamp": "2026-08-07T02:24:00Z",
    "source_phase": "LEVEL_4",
    "total_evaluations": 183,
    "noise_floor": 0.005,
    "primary_metric": "scalarized_score",
    "scalarized_weights": {"alpha": 0.35, "beta": 0.35, "gamma": 0.15, "delta": 0.10, "epsilon": 0.05},
    "walk_forward_passed": true,
    "regime_sensitivity_flagged": false
  },
  "config": {
    "kl_dro_lambda": 0.42,
    "edge_threshold": 0.067,
    "kelly_fraction": 1.0,
    "max_contracts": 300,
    "slippage_budget": 0.001,
    "fee_deduction": 0.3,
    "confidence_floor": 0.55,
    "fee_type": 1,
    "member_weighting": 1,
    "holdout_start_frac": 0.7,
    "position_sizing_model": 1,
    "stop_loss_kind": 0,
    "agreement_n": 3,
    "agreement_m": 7,
    "calibration_mode": 0,
    "goldilocks_lane_enabled": 0,
    "trajectory_lane_enabled": 1,
    "fusion_mode": 1,
    "spatial_coherence_enabled": 0,
    "capital_base": 10000
  },
  "projected_metrics": {
    "accuracy": 0.68,
    "temp_accuracy": 0.64,
    "market_accuracy": 0.67,
    "total_pnl": 5200,
    "sharpe": 1.4,
    "brier": 0.19,
    "trades_per_day": 7,
    "max_drawdown": 0.08
  },
  "parameter_details": [
    {
      "name": "edge_threshold",
      "peak_type": "CLEAR_PEAK",
      "sensitivity_score": 0.08,
      "optimal_value": 0.067,
      "significance": "SIGNIFICANT"
    },
    {
      "name": "slippage_budget",
      "peak_type": "PLATEAU",
      "sensitivity_score": 0.002,
      "optimal_value": 0.001,
      "significance": "INSENSITIVE"
    }
  ],
  "interaction_results": {
    "significant_pairs": [
      {"params": ["edge_threshold", "kelly_fraction"], "I": 1.2}
    ],
    "level_3_triggered": false,
    "joint_optimization_performed": false
  },
  "walk_forward": {
    "window_2020_2021": {"accuracy": 0.67, "sharpe": 1.3},
    "window_2022_2023": {"accuracy": 0.69, "sharpe": 1.5},
    "window_2024_2025": {"accuracy": 0.66, "sharpe": 1.2},
    "variance": 0.03
  }
}
```

### 12.2 File Layout

```
sweep-results/
├── level-0/
│   ├── sanity-checks.json        # Results of null-config, permutation, synthetic
│   ├── noise-floor.json          # Noise floor estimate
│   └── sensitivity.json          # Per-parameter effect sizes
├── level-1/
│   ├── coarse/
│   │   ├── edge_threshold.json   # Full 7-point data for each sig param
│   │   ├── kelly_fraction.json
│   │   └── ...
│   └── refine/
│       ├── edge_threshold.json   # 3-point refinement data
│       └── ...
├── level-2/
│   ├── plus-minus-validation.json
│   ├── pairwise-interactions.json
│   └── meta-interactions.json
├── level-3/
│   └── bo-results.json           # Only if Level 3 was triggered
├── level-4/
│   ├── walk-forward.json
│   └── robustness.json
├── optimal-config.json
└── report.md
```

### 12.3 Implementation Order

| Step | What to Build | Depends On | Estimated Effort |
|---|---|---|---|
| 1 | `scripts/evaluate_config.py` — the evaluation wrapper with 8-metric return | Existing backtest engine | 2 hours |
| 2 | `scripts/validate_config.py` — parameter validation against registry | Section 9 registry | 1 hour |
| 3 | `scripts/sanity_checks.py` — null-config, permutation, synthetic recovery | evaluate_config | 3 hours |
| 4 | `scripts/sweep_runner.py` — sweep_param() + peak detector | evaluate_config | 4 hours |
| 5 | `scripts/run_level_0.py` — orchestrate sanity checks + sensitivity | sanity_checks, sweep_runner | 2 hours |
| 6 | `scripts/run_level_1.py` — orchestrate grid sweep + refinement | sweep_runner | 3 hours |
| 7 | `scripts/run_level_2.py` — ±1-step, pairwise, meta-interaction | sweep_runner | 4 hours |
| 8 | `scripts/run_level_4.py` — walk-forward + robustness | evaluate_config | 2 hours |
| 9 | `scripts/run_level_3.py` — conditional BO (only if Level 2 triggers) | scikit-optimize, sweep_runner | 6 hours |
| 10 | `scripts/report_generator.py` — output schema, report.md | All above | 2 hours |
| 11 | `scripts/full_sweep.py` — top-level orchestrator | All above | 2 hours |

### 12.4 Expected Wall-Clock Time

| Level | Evaluations | Parallelism | Wall Time |
|---|---|---|---|
| Level 0 | ~38 | Can parallelize sensitivity across params | ~10 min |
| Level 1 | ~70 | Each parameter independent; 22 parallel workers | ~10 min |
| Level 2 | ~91 | ±1-step: 22 parallel; pairwise: depends on pairs | ~15 min |
| Level 3 | ~100 (if triggered) | Sequential (BO depends on previous eval) | ~50 min seq |
| Level 4 | ~5 | Can parallelize across seeds/windows | ~2 min |
| **Total (Level 3 NOT triggered)** | **~204** | Mostly parallelizable | **~40 minutes** |
| **Total (Level 3 triggered)** | **~304** | Level 3 sequential | **~90 minutes** |

**Key insight:** The 40-minute primary path is faster than a single LHS run of 5000 evaluations (which takes ~42 hours sequentially).

### 12.5 Dependencies

Required for all levels:
- Existing `engine.backtest` module (already in codebase)
- `numpy`, `scipy` (already present)
- `json` (stdlib)

Required only for Level 3:
- `scikit-optimize` (`pip install scikit-optimize`) — 5-line API for GP-based BO

Optional:
- `matplotlib` for per-parameter sensitivity charts in report.md

### 12.6 Testing & Validation

Each script must pass:
1. **Unit tests:** Mock `evaluate_config()` to return known values. Verify peak detector returns correct classification.
2. **Integration test:** Run Level 0 + Level 1 on synthetic data with known optimal parameters. Verify the sweep recovers the known optimum.
3. **Regression test:** Run the full pipeline on current default config. Verify output schema matches Section 12.1. No new dependencies should crash.

---

## Appendices

### A: Reference: Noise-Level Parameters

| Symbol | Description | Value (default) | Determined How |
|---|---|---|---|
| σ_noise | Standard deviation of accuracy under seed variation | 0.3% — 0.5% | Level 0 noise floor estimation |
| Peak threshold | Minimum peak-to-valley for CLEAR_PEAK classification | 3 × σ_noise | Measured from Level 0 |
| Interaction threshold | Minimum |I| for material interaction | σ_noise | Measured from Level 0 |
| Flat threshold | Max score range for PLATEAU classification | σ_noise | Measured from Level 0 |

### B: Reference: Metric Normalization Constants

These are NOT hardcoded — they're recomputed as running statistics. But reference values for sanity-checking:

| Metric | Expected Mean | Expected Std |
|---|---|---|
| accuracy | 0.55-0.65 | 0.02-0.05 |
| sharpe | 0.5-1.5 | 0.2-0.5 |
| total_pnl | 1000-10000 | 500-2000 |
| brier | 0.15-0.35 | 0.02-0.05 |
| max_drawdown | 0.05-0.30 | 0.02-0.10 |

### C: Quick Reference: Level Gate Conditions

```
# Can I skip Level 1 refinement?
IF param.peak_type == 'PLATEAU': YES — use midpoint
IF param.peak_type == 'CLEAR_PEAK': YES after 3-point refine

# Can I skip Level 3 (BO)?
IF no material interactions found in Level 2: YES
IF interaction improvement < σ_noise: YES

# Can I stop after Level 2?
IF Level 2 gate = NO: YES — proceed to Level 4
IF Level 3 was not triggered: YES

# Should I flag for human review?
IF any optimal value is at boundary of range: YES
IF walk-forward variance > 2 × σ_noise: YES
IF Level 3 found a config > 1% better than Level 1 grid: NO — this is success
```

---


**End of Definitive Specification. Build-ready.**

---

## Strategic Advisor Assessment — Critique of the Sweep Optimization Strategy

**Author:** Strategic Advisor (subagent evaluation)
**Date:** 2026-08-07
**Evaluated against:** The critique summarized in the subagent task (see parent session transcript)

---

### 1. Is the Critique Correct? What's Wrong With It?

**The critique is directionally useful but contains several factual errors and logical contradictions.**

#### What the critique gets right:

1. **Signal redundancy is not addressed in this spec.** The spec has 22 parameters but does not include a pre-sweep correlation matrix to identify collinear signals. If `gaussian` and `gaussian_v2` are correlated at ρ=1.0, one is redundant. This is a real gap. The sensitivity pre-screen (Level 0) would catch this incidentally — both would show near-identical sensitivity profiles — but it's not explicitly called out, and the output schema doesn't include a correlation matrix.

2. **Walk-forward validation should be earlier.** Level 4 is currently the last step before final output. The critique is right that regime-awareness should inform the optimization loop, not just validate it at the end. A config that wins on 2020-2025 but loses on 2023-2025 is fragile. The spec's walk-forward at Level 4 would flag this, but the flag comes after ~200 evaluations have been spent.

3. **The noise floor is a real constraint.** The spec acknowledges this (σ_noise = 0.3-0.5%, peak threshold = 3×σ_noise) and the noise-aware peak detector is correct. But the critique's point that "0.5% variation means 0.3% gains are noise" is a valid warning: the signal-to-noise ratio in this domain is poor, and the spec's scalarization across 5 metrics may amplify noise rather than reduce it.

#### What the critique gets wrong:

1. **The 79.92% simple trend signal claim is a red flag, not a benchmark.** If a simple trend signal achieves 79.92% directional accuracy on weather prediction, it is almost certainly overfit, contains look-ahead bias, or the data has a systematic trend that should be detrended. The spec's Level 0 null-config test expects ≈50% accuracy from a random configuration. A 79.92% simple signal would FAIL the permutation test (Check 2: expects <52% on shuffled labels), meaning data leakage exists. The critique is citing this number as evidence that "building new things" beats optimization, but this number is evidence of a broken evaluation pipeline, not a healthy baseline.

2. **"Optimizing parameters is not the highest-leverage thing" — this ignores what the spec actually does.** The spec is not a 410-parameter brute-force optimization. It's a 4-level pipeline with a sensitivity pre-screen that expects 5-8 of 22 parameters to be significant. The term "410-parameter optimization" is a straw man. The spec optimizes ~8 parameters after filtering out the noise. This IS the "80/20" the critique recommends.

3. **"Run a baseline sweep FIRST (all flags off, 500 configs)" contradicts the critique's own noise floor argument.** If the noise floor is 0.5%, a 500-config LHS sweep is the worst thing to do — it guarantees finding spurious optima through multiple comparisons. This is exactly what the spec is replacing. The critique's Step 2 is a regression to the shotgun approach the spec was designed to fix.

4. **"40 signals, 9 gates, 5 levers, 2 lanes, 4 modulators have never been tested together" — but the spec's 22-parameter registry covers exactly these groups.** The parameter table in Section 9 explicitly maps to Signals (params 1-7), Fees (8-10), Gates (11-14), Levers (15-17), Lanes (18-19), Modulators (20-22), and Validation (22). The 410 configs the critique wants to sweep are the combinatorial explosion of all categorical values across all groups — which is exactly why the spec does sensitivity pre-screening first, then grid, then interactions.

#### What the critique identifies that's genuinely missing from the spec:

| Missing | Severity | Should Fix? |
|---|---|---|
| Signal correlation matrix before Level 0 | Medium | Yes — add to Level 0 as optional pre-check |
| Regime-specific configs (not just walk-forward flagging) | Medium | Yes — add Level 2.5 for regime detection before final optimization |
| Explicit test of the simple trend signal claim | Low | Nice-to-have — the null-config test already covers this |

---

### 2. What's the Highest-Leverage Thing to Do RIGHT NOW?

**Run the Level 0 sanity checks and sensitivity pre-screen.** This is the highest-leverage activity by an order of magnitude. It costs ~38 evaluations (≈10 minutes wall-clock) and answers:

- Is the evaluation function correct? (null-config, permutation, synthetic recovery)
- What is the noise floor? (critical for all subsequent decisions)
- Which of the 22 parameters actually matter? (expected: 5-8)
- Are there collinear signals? (sensitivity profiles will reveal this)

**Before doing ANY of the above, add one sub-step: run a pairwise correlation matrix across all 40 signals.** This is a ~1-hour script that costs 0 evaluations (pure data analysis, no backtesting). The output is a correlation heatmap. If any pair shows ρ > 0.95, flag them as redundant and freeze one at default. This addresses the critique's #1 concern without adding any eval cost.

**Second-highest leverage: test the simple trend signal claim.** If a simple trend signal truly hits 79.92%, the data pipeline is broken. The Level 0 permutation test (Check 2) will catch this. If it passes, the 79.92% number is either a measurement error or a cherry-picked window. Either way, resolving this before optimization prevents optimizing into a broken system.

---

### 3. Is the 410-Parameter Optimization a Rabbit Hole? What's the Minimal Viable Optimization?

**The 410-parameter number is a straw man.** The spec doesn't optimize 410 parameters. It optimizes 22, expects 5-8 to be significant after sensitivity pre-screen, and sweeps those ~8 across a 7-point grid. The grid + refinement + interaction + walk-forward totals ~204 evaluations — not 410 configs, not 410 parameters.

**The minimal viable optimization is the spec's primary path (Levels 0→1→2→4) = ~204 evaluations, ~40 minutes wall-clock.** This has already been stripped down to the bare minimum:

- Level 0: 38 evaluations — sanity checks + sensitivity screen + noise floor
- Level 1: 70 evaluations — grid on significant parameters only
- Level 2: 91 evaluations — interaction test on top 5 pairs + group test
- Level 4: 5 evaluations — walk-forward + robustness

**Can we go smaller?** Removing Level 2 (interaction testing) saves ~91 evaluations but risks missing interaction effects. Removing Level 4 (walk-forward) saves ~5 evaluations but removes the overfitting guard. Neither is safe. The spec is already at the minimal viable optimization.

**What we could safely defer:**
- Level 3 (Bayesian Optimization, 100 evaluations) — only triggered if interactions exceed threshold (expected: rarely triggered)
- Full pairwise test on all 8 significant parameters — only triggered if top-5 pairs show material interactions
- Joint optimization of interacting groups — only triggered if group meta-interaction test fires

**The spec's escalation path is already minimal. The critique's call for "3-5 parameters that matter most" is exactly what the sensitivity pre-screen in Level 0 does.**

---

### 4. What's the Single Biggest Risk?

**The evaluation function is wrong.**

If the backtest has data leakage, look-ahead bias, or systematic bias, ALL optimization is garbage — regardless of whether it's a 5000-config LHS, a 204-config grid, or a 410-parameter BO. The 79.92% simple trend signal claim (if real) is a flashing red warning sign.

The Level 0 sanity checks are designed to catch this:
- Null-config test: expects ≈50% accuracy at default
- Permutation test: expects <52% on shuffled labels
- Synthetic data recovery: expects to find known optimum

**But the synthetic data test is only as good as its design.** If the synthetic data doesn't faithfully represent real-world evaluation artifacts (e.g., missing fee structures, slippage that doesn't match real liquidity), the test passes but the real pipeline is still broken.

**Second-biggest risk: noise floor is higher than expected.** If σ_noise (accuracy) > 1.0%, the spec's own thresholds become meaningless. The peak detector requires peak-to-valley > 3×σ_noise. If σ_noise is 1.5%, any peak < 4.5% range is classified as PLATEAU, and the optimization finds nothing. The spec handles this by increasing backtest window length or replicating evaluations, but this costs time and may not help if the noise is structural (regime volatility, not measurement noise).

---

### 5. What Would You Do Differently?

#### Add to Level 0 (before sensitivity pre-screen):

**Sub-step 0a: Signal correlation matrix**

```
# Compute pairwise correlation across all 40+ signals
# Input: historical signal values for each candidate signal
# Output: correlation matrix, heatmap, flagged pairs (ρ > 0.95)

flagged_pairs = []
for i, signal_a in enumerate(SIGNALS):
    for j, signal_b in enumerate(SIGNALS):
        if j <= i:
            continue
        rho = pearson_correlation(signal_a.values, signal_b.values)
        if abs(rho) > 0.95:
            flagged_pairs.append((signal_a.name, signal_b.name, rho))

# For each flagged pair, freeze one at its default
# Do NOT remove from the registry — just flag for Level 0 sensitivity
```

Cost: 0 evaluations. Pure data analysis. ~1 hour to script.

#### Move walk-forward earlier:

**Add Level 2.5: Amplified Regime Test**

After Level 2 (interaction test) but before Level 4 (final walk-forward), insert a regime-phase test:

```
# Test the Level 2 optimal config on sub-windows
# If regime sensitivity is detected, build regime-specific configs

windows = [
    (2020-01, 2021-06),  # Pre-regime-1
    (2021-07, 2023-06),  # Regime-1
    (2023-07, 2025-12),  # Regime-2
]

for window in windows:
    result = evaluate_config(C_optimal, window=window)
    
if accuracy_variance > 2 × σ_noise:
    # Regime-sensitive — build regime-specific optimizations
    for regime_window in windows:
        C_regime = optimize_on_window(regime_window)  # mini-Level 1
        test C_regime on other windows
        if C_regime beats C_optimal on its regime: 
            ADD to regime-specific config set
```

Cost: ~3 evaluations for detection + ~30 evaluations per regime (if triggered). Should be conditional — only run if Level 0 sensitivity shows >1 parameter with bimodal behavior (suggesting regime-dependent optima).

#### Add explicit output for signal redundancy:

The optimal-config.json output schema should include a `redundant_signals` field listing any signals that were frozen at default due to collinearity. This makes the decision visible and reversible.

#### Replace the scalarized objective with a sharper primary metric:

The 5-metric scalarization (accuracy × 0.35 + Sharpe × 0.35 + PnL × 0.15 - Brier × 0.10 - DD × 0.05) is reasonable but risks amplifying noise. Consider using **Sharpe ratio as the primary metric** and treating accuracy as a secondary constraint (≥65% required). Sharpe is a self-normalizing metric that inherently penalizes variance — it's less sensitive to the noise floor than accuracy.

---

### 6. Goldilocks Lane and Trajectory Lane — Worth Scanning for at This Stage?

**Verdict: Include them in the sensitivity pre-screen, then decide. Do not pre-judge.**

#### Argument for scanning now:
- Both are boolean flags defaulting to 0 (off). The sensitivity pre-screen costs 2 evaluations to test (one with Goldilocks on, one with Trajectory on, all else default).
- If either shows a sensitivity effect > σ_noise, it's significant and should be included in the grid sweep.
- If neither shows sensitivity, freeze at default and move on. Total cost: 2 evaluations.
- The critique's point that "2 lanes have never been tested together" is valid — the interaction test in Level 2 specifically tests this (goldilocks × trajectory pair).
- The group-level meta-interaction test (Level 2.4) tests whether the Lanes group interacts with other groups.

#### Argument for deferring:
- If the simple trend signal (without any lanes) already hits 79.92% (the critique's claim), adding more complexity is premature optimization.
- But we've established that 79.92% number is suspicious. Until the null-config test runs, we don't know what the baseline actually is.
- The lanes add architectural complexity (new code paths, new testing surface) that isn't justified until we know the baseline optimization is working.

#### Recommendation:

**Include both lanes in the sensitivity pre-screen only.** This costs 2 evaluations. Do NOT include them in the Level 1 grid sweep unless the sensitivity pre-screen shows an effect > σ_noise. This is what the spec already specifies — the lanes are in the parameter registry (Section 9, params 18-19), and the sensitivity pre-screen (Level 0.3) will flag them as significant or not.

**If both lanes are significant (effect > σ_noise), include them in Level 1 with their full 2-point grid (on/off).** This adds 2 evaluations to Level 1 (not 4, because the midpoints are shared with the default config).

**If the interaction test (Level 2) shows a material goldilocks × trajectory interaction, escalate to Level 3 (BO) on the Lanes group specifically.** This is already handled by the spec's interaction gate.

**Do not defer them entirely.** The cost of including them is 2 evaluations in Level 0 + 2 evaluations in Level 1 if significant. The cost of excluding them is missing a potential 2-5% accuracy improvement that could make the difference between a 65% and 70% system. At 2-4 evaluations, the risk/reward strongly favors inclusion.

---

### Summary: Critique vs. Spec — Who Wins?

| Issue | Critique Position | Spec Position | Verdict |
|---|---|---|---|
| Signal redundancy | Add correlation matrix first | Not addressed | **Critique wins** — add to Level 0 |
| 79.92% simple trend signal | Evidence that building beats optimizing | Null-config test will catch if broken | **Spec wins** — number is likely spurious |
| 410-parameter rabbit hole | Don't optimize everything | Already optimizes 5-8 params after pre-screen | **Spec wins** — straw man |
| Baseline sweep first | 500-config LHS | Spec replaces shotgun approach | **Spec wins** — critique's suggestion contradicts itself |
| Walk-forward earlier | Should inform optimization, not just validate | Currently at end | **Critique wins** — add Level 2.5 regime test |
| Noise floor awareness | 0.5% noise kills 0.3% gains | Uses 3×σ_noise threshold | **Draw** — spec handles it correctly |
| Goldilocks/Trajectory lanes | Unknown unknowns | In registry, sensitivity will decide | **Spec wins** — already correct |

**Bottom line: The critique makes one genuinely important point (signal redundancy pre-check) and one useful point (walk-forward should be earlier). The rest is either already in the spec, based on a questionable 79.92% claim, or contradicts itself. The spec's architecture is sound. Add the correlation matrix as a Level 0 sub-step, move regime testing to Level 2.5, and proceed.**

---

**End of Strategic Advisor Assessment.**

## 0. Pre-Step: Kill Redundant Signals

**Added:** 2026-08-07 (per strategic advisor recommendation)  
**Evaluations:** 0 (uses existing `scripts/compute_signal_correlation_matrix.py`)

### 0.1 Why This Exists

Before any optimization, eliminate signals that are redundant. The correlation matrix shows that several signal pairs have ρ ≥ 0.97 (gaussian/gaussian_v2/calendar_climatology/forecast_disagreement). These signals are saying the same thing — optimizing them independently wastes time and risks overfitting.

### 0.2 Action

Run:
```bash
python3 scripts/compute_signal_correlation_matrix.py --db-path data/kalshi_settlements.db
```

This outputs:
- `data/signal_correlation_matrix.json` — full pairwise ρ matrix
- `data/signal_correlation_summary.md` — redundancy classification (HIGH_REDUNDANCY/LOW/ORTHOGONAL)
- `data/signal_disposition.csv` — per-signal disposition (ADVANCE/PARK/KILL)

### 0.3 Disposition Rules

| Classification | |ρ| Range | Action |
|:---------------|:---------:|:--------|
| **ORTHOGONAL** | < 0.3 | Keep both |
| **LOW** | 0.3 – 0.5 | Keep both |
| **MODERATE** | 0.5 – 0.7 | Keep both, but flag for down-weighting |
| **HIGH_REDUNDANCY** | ≥ 0.7 | Kill one — keep the higher-accuracy signal |

### 0.4 Impact on Subsequent Levels

After killing redundant signals (typically 2-3 of the 40), the remaining signal set is 37-38 orthogonal signals. This reduces:
- Level 0 sensitivity pre-screen: 20 → 20 params (no change — cross-cutting params are independent of signal count)
- Level 1 per-parameter grid: No change (same params)
- Level 2 interaction testing: No change (same params)
- Level 3 Bayesian optimization: Faster convergence (cleaner signal set)
- Level 4 walk-forward: More reliable (less overfitting risk)

**Total time:** ~1 hour (script runtime) + 0 evaluation budget.

## 0.5 Scope Clarification: Lanes Are Not Sweep Parameters

The 22-parameter sweep optimizes the **cross-cutting engine** — parameters that affect all signals equally (edge_threshold, kelly_fraction, confidence_floor, etc.).

**Goldilocks Lane** and **Trajectory Lane** are NOT parameters in this sweep. They are separate calibration targets, like accuracy or P&L. Each has its own optimization objective independent of the main sweep:

| Lane | Optimization Target | What It Optimizes | How |
|:-----|:-------------------|:-----------------|:----|
| **Goldilocks** | Intraday spike detection accuracy | Bucket zone size, min_obs_per_day, reversion timing | Separate calibration run on high-frequency METAR data |
| **Trajectory** | Analog probability blend quality | Feature weights, analog count thresholds, blend weight | Separate calibration run using epoch DB |

**The sweep tests whether Goldilocks/Trajectory lanes IMPROVE the main sweep's metrics when enabled.** But it doesn't tune their internal knobs. Those knobs are tuned in their own calibration runs, with their own success criteria.

**Decision rule for the sweep:**
- `goldilocks_lane_enabled = 0/1` — a binary parameter in the sweep. Does turning it on improve accuracy/PnL?
- `trajectory_lane_enabled = 0/1` — same. Does the heavy informant help?
- If yes → enable the lane. The lane's internal optimization is a separate task.
- If no → don't enable. The lane needs more work before it's ready.

This is the same relationship as the 82-member ensemble: the sweep tests whether it works (1 of 40 signals), but doesn't tune its internal GEFS/ECMWF weight. That's a separate deep-dive.

## 0.6 Lane Separation: Three Independent Optimization Tracks

The optimization is NOT one sweep. It's three independent tracks:

```
Track 1: Main Directional Engine
  ├─ 40 signals (daily directional predictions)
  ├─ 9 gates (filters)
  ├─ 5 levers (position sizing)
  ├─ Opt target: accuracy, PnL, Sharpe on daily HIGH/LOW markets
  └─ The 22-parameter sweep defined in Levels 0-5

Track 2: Goldilocks Lane (separate, parallel)
  ├─ Intraday microstructure — bucket boundary crossings, transient spikes
  ├─ Uses DualHypothesisEngine (H1 transient vs H2 structural)
  ├─ Opt target: spike detection accuracy, reversion timing precision
  ├─ Its own calibration run on high-frequency METAR data
  └─ Not part of the 22-parameter sweep

Track 3: Trajectory Lane (separate, parallel)
  ├─ Walk-forward prediction — historical analog patterns
  ├─ Uses Epoch DB + AnalogMatcher
  ├─ Opt target: analog probability calibration quality, blend weight optimization
  ├─ Its own calibration run on epoch DB
  └─ Not part of the 22-parameter sweep
```

The three tracks converge at the **execution layer**:
- Main engine produces daily direction/confidence
- Goldilocks produces intraday spike alerts (if lane is active)
- Trajectory produces blended probability estimate (if lane is active)
- The trade execution layer decides which signal to act on based on priority

**The 22-parameter sweep does NOT include Goldilocks or Trajectory parameters.** They are not binary flags. They are not enable/disable toggles. They are completely separate optimization domains with their own targets, their own data, and their own calibration runs.

**Deferred to separate spec:** Goldilocks calibration spec and Trajectory calibration spec. The main sweep spec covers only the directional engine (Track 1).
