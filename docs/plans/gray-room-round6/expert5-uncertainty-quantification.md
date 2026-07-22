# Expert 5: Uncertainty Quantification Framework

**Gray Room — Round 6**  
**Date:** 2026-07-21  
**Focus:** Probabilistic trajectory prediction from NWP analogs for Kalshi high/low weather markets

---

## 1. Epistemic vs Aleatoric Uncertainty

### Separation Strategy

**Aleatoric (irreducible):** Captured by the residual variance of trajectories *within* the analog ensemble. Even with infinite data, two days with identical NWP inputs will produce different outcomes. This is the fundamental noisy process — $N(0, \sigma^2_{\text{aleatoric}})$ where $\sigma^2_{\text{aleatoric}}$ is estimated from intra-analog variance.

**Epistemic (reducible with more data):** Captured by sensitivity of the analog space to sample size. The key metric is **analog density**: how many good analogs exist for the current NWP state vector.

**Implementation:**

```python
def decompose_uncertainty(nwp_state, archive):
    analogs = find_analogs(nwp_state, archive, k=100)
    
    # Aleatoric: variance of outcomes among tightest analogs
    tight = select_by_distance(analogs, top_pct=0.1)
    aleatoric = variance(trajectories(tight))
    
    # Epistemic: function of analog count below distance threshold
    threshold = distance_quantile(analogs, 0.95)
    count_below = len([a for a in analogs if a.distance < threshold])
    
    # Beta prior: assuming uniform prior, posterior alpha = 1 + count_below
    # Epistemic uncertainty ~ Beta posterior width
    n_total = len(archive)
    epistemic_factor = 1.0 / (1.0 + count_below / sqrt(n_total))
    
    return {
        "aleatoric": aleatoric,
        "epistemic": aleatoric * epistemic_factor,  # scaled up when data sparse
        "total": aleatoric * (1.0 + epistemic_factor)
    }
```

**Effect on trajectory distribution:** When epistemic uncertainty dominates, the trajectory distribution widens and becomes less reliable—the tails are less constrained by data. Traders should receive an **uncertainty composition** flag:

| Composition | Signal | Action |
|------------|--------|--------|
| Aleatoric >> Epistemic | Trust the distribution shape | Normal position sizing |
| Epistemic >> Aleatoric | Distribution shape is fragile | Reduce position size, widen thresholds |
| Both high | Hot mess | Skip or min-size only |

### Accuracy Improvement: Bayesian Bootstrap of Analogs

Instead of a single analog set, run 100 bootstrap resamples of the archive and compute a distribution over trajectory distributions. The spread of this meta-distribution is a direct measure of epistemic uncertainty. This is computationally cheap (resampling indices) and gives you second-order uncertainty for free.

---

## 2. Out-of-Archive Events

### Detection

An "out-of-archive" event is defined as: the distance to the nearest analog exceeds a **dynamic threshold** calibrated to the 99th percentile of historical nearest-neighbor distances.

### Decision Tree

```
NWP State Vector
    │
    ├─ Good analog found (d < P99_historical)
    │   └─ Normal operation
    │
    ├─ Marginal analog (P99 < d < P99.9)
    │   ├─ Widen analog window: relax distance threshold by 20%
    │   ├─ If still insufficient → weight by inverse distance + climatology prior
    │   └─ Flag: "marginally novel regime, increased uncertainty"
    │
    └─ No analog (d > P99.9)
        ├─ Step 1: Check if it's a known pattern at different lead time
        │   (e.g., pattern exists at T+24 but not T−0 → shift window)
        ├─ Step 2: Fall back to **climatology + trend prior**
        │   └─ Climatology = seasonal average trajectory ± seasonal variance
        ├─ Step 3: Compute blended distribution:
        │   P_final = 0.3 × P_analog_weighted + 0.7 × P_climatology
        │   (weights shift toward climatology as distance increases)
        └─ Decision: **Skip the trade** unless there's a strong seasonal signal
            (e.g., always > 90°F in July in Phoenix → climatology is informative)
```

### Concrete Rule for Kalshi Markets

**Skip threshold:** If analog distance > P99.9 AND climatology trajectory has > 40% probability on *both* sides of the high/low market boundary (i.e., climatology is indecisive), **do not trade**. The uncertainty tax will destroy expected value.

### Fallback Data: NWP Ensemble Consensus

Even without historical analogs, the 5 NWP models' *instantaneous* agreement/disagreement provides signal. If all 5 models predict a temperature spike despite no historical precedent, trust the physics more than the statistics.

---

## 3. Multi-Modal Distributions

### Why Histograms Fail

Simple histograms: (a) require arbitrary bin boundaries, (b) smooth out sharp bifurcations, (c) don't naturally represent bimodality as a parameter.

### Approach: Bayesian Gaussian Mixture Model (GMM)

Fit a **GMM with automatic component selection** to each trajectory distribution:

```python
from sklearn.mixture import BayesianGaussianMixture

def trajectory_distribution(analog_trajectories, max_components=5):
    # Analog_trajectories shape: [n_analogs, n_time_steps]
    
    gmm = BayesianGaussianMixture(
        n_components=max_components,
        covariance_type="diag",
        weight_concentration_prior=0.01,  # strong Dirichlet process prior
        max_iter=500
    )
    gmm.fit(analog_trajectories)
    
    # Effective components: those with weight > cutoff
    effective = [i for i, w in enumerate(gmm.weights_) if w > 0.05]
    
    return {
        "components": effective,
        "means": gmm.means_[effective],
        "covariances": gmm.covariances_[effective],
        "weights": gmm.weights_[effective],
        "is_multimodal": len(effective) > 1
    }
```

**Key details:**
- Bayesian GMM auto-prunes unused components via Dirichlet process prior
- **Bimodality detection:** The dip test (Hartigan's dip test) on the 1D projection (e.g., temperature at market expiry). If significant (p < 0.05), flag as multimodal.
- For the trajectory *path* (not just endpoint), compute pairwise divergence of top-2 components using KL divergence. High KL → genuinely distinct paths, not just noise.

### Alternative (Lighter): Kernel Density + Mode Detection

If GMM is too heavy for real-time:
1. Fit 1D KDE to the trajectory endpoint
2. Find all local maxima via gradient ascent
3. Keep only modes separated by > 1 KDE bandwidth
4. Each mode gets a probability = integral of KDE over its basin of attraction

### Trading Implication

When the distribution is bimodal for a Kalshi high/low market:
- **Not necessarily a problem** if both modes are on the same side of the strike. E.g., "temp will be 100°F or 102°F" for a >99°F market — both modes hit.
- **Problem:** Modes straddle the strike. "Either 97°F or 101°F" for a >99°F market. In this case, trade sizing should be **halved** because the outcome is highly threshold-sensitive.

---

## 4. Confidence Calibration

### Beyond Isotonic Regression

Isotonic regression is fine for binary calibration but loses information for continuous distributions. Use:

#### Primary: Probability Integral Transform (PIT) Histogram

The gold standard for continuous probability calibration:

1. For each forecast F_i (the CDF), record the observed value x_i
2. Compute u_i = F_i(x_i)
3. If well-calibrated, u_i ~ Uniform(0, 1)

**Diagnostics:**
- **U-shape** → under-dispersed (too confident)
- **∩-shape** → over-dispersed (too uncertain)
- **Skew** → bias in specific direction

**Implementation:** Maintain a rolling PIT histogram (last 500 predictions). Run a Kolmogorov-Smirnov test against Uniform(0,1) every 50 predictions. If p < 0.01, flag for recalibration.

#### Secondary: CRPS (Continuous Ranked Probability Score)

CRPS = integral of (F_i(x) - 1{x_i ≤ x})² dx

**Advantages over MAE/RMSE:**
- Proper scoring rule — can't cheat by predicting the mean
- Directly measures distribution quality, not just point accuracy
- Decomposable into **reliability** + **resolution** + **uncertainty**

**Calibration adjustment:** Use CRPS to tune the analog ensemble spread. If CRPS is consistently high, inflate ensemble variance by a factor γ:

```python
γ = minimize_over_rolling_window(
    lambda g: mean_crps(forecasts * g, observations)
)
```

#### Tertiary: Variogram Score for Trajectories

CRPS is univariate. For trajectory *paths* (multi-step forecasts), use the **variogram score** of order p:

VS_p = Σ_i Σ_j w_ij (|y_i - y_j|^p - |x_i - x_j|^p)²

This penalizes incorrect spatial/temporal correlation structure.

### Accuracy Improvement: CRPS Decomposition Feedback Loop

Decompose CRPS into its three components:

| Component | Meaning | Fix |
|-----------|---------|-----|
| Reliability | How well PIT → Uniform | Apply copula-based drift correction |
| Resolution | How sharp forecasts are (ability to distinguish events) | Improve analog weighting |
| Uncertainty | Minimum achievable CRPS given noise | Lower bound — acceptance |

Track reliability/resolution ratio over time. If reliability degrades (calibration slipping), trigger a recalibration of the analog similarity metric using the most recent 200 samples.

---

## 5. Ensemble Spread as Uncertainty Signal

### Basic Approach

5 NWP models → 5 trajectories → **ensemble spread** = variance across models at each time step.

### Spread-Skill Relationship

Fit a regression: `error² = α + β × spread² + ε`

If β > 0 (spread correlates with error), the spread is informative. Use it to scale confidence.

### Practical Implementation

```python
def ensemble_confidence_boost(nwp_models, analog_forecast):
    # NWP ensemble statistics
    nwp_std = std_across_models(nwp_models)  # [n_time_steps]
    
    # Historical relationship: when NWP spread was X, what was the error?
    archive_spread = historical_nwp_spreads  # from past runs
    archive_errors = historical_forecast_errors
    
    # Fit: log(error) ~ log(spread)
    coeff = linregress(log(archive_spread), log(archive_errors)).slope
    
    # Expected error given current spread
    expected_error = exp(intercept + coeff * log(nwp_std))
    
    # Boost factor: how much better/worse than average
    baseline_error = mean(archive_errors)
    confidence_factor = baseline_error / expected_error
    
    # Analog forecast variance: scale by confidence factor
    adjusted_variance = analog_forecast.variance / confidence_factor
    
    return adjusted_variance
```

### When NWP Ensemble Agrees

If all 5 NWP models agree (spread < 0.5°C for temperature):
- **Reduce analog ensemble variance** by up to 30% (confidence boost)
- **But:** Check if this agreement is an artifact (e.g., all models use the same underlying GFS initialization). If models share lineage, agreement is less informative.

**Model diversity weighting:**
```
Model lineage map:
├─ GFS-derived: ECMWF, UKMO ← high correlation
├─ CMC-derived: CMC, JMA     ← high correlation  
└─ ICON: independent          ← highest weight per model

Effective independent models = 3, not 5
```

Adjust spread scaling by effective degrees of freedom:
```python
effective_n = estimate_independent_models(nwp_models)
scaling_factor = 1.0 + 0.5 * (5 - effective_n) / 5  # penalize correlated agreement
```

### Accuracy Improvement: Spread-Error Autoencoder

Train a small autoencoder on (NWP_spread, analog_variance, analog_density, climatology_variance) → predict observed CRPS. Use the latent representation as a learned uncertainty embedding. This captures non-linear interactions (e.g., "high NWP spread + low analog density = especially bad") that linear regression misses.

---

## 6. Minimum Information

### For Kalshi High/Low Markets

The market outcome is binary: Will temp exceed strike Y/N? But we trade via expected value, which requires a continuous probability estimate.

### Recommendation: Three-Tier Output

```
┌──────────────────────────────────────────────────┐
│  Tier 1: Continuous Density (always computed)    │
│  - Kernel density estimate on trajectory endpoint │
│  - 1000 samples from posterior predictive         │
│  - Full CDF available for pricing                 │
│  - Storage: ~8 KB per forecast                    │
├──────────────────────────────────────────────────┤
│  Tier 2: Binned Distribution (3-bucket)          │
│  - P(T < strike - δ), P(|T - strike| < δ),       │
│    P(T > strike + δ)                              │
│  - δ = 1.5× historical MAE at this lead time     │
│  - Used for: quick sanity check, dashboard        │
├──────────────────────────────────────────────────┤
│  Tier 3: Single Number (for execution)           │
│  - Expected P(T > strike)                         │
│  - With: confidence interval on that probability   │
│    (uncertainty about the probability itself)      │
│  - Used for: position sizing                       │
└──────────────────────────────────────────────────┘
```

### Granularity Decision

**3-bucket is insufficient alone.** Reason: Kalshi high/low markets settle on a specific strike. A 3-bucket distribution at $δ = 1.5°C$ can't distinguish P(T > 100°F) = 45% vs 55% when strike = 100°F. Those are very different expected values.

**Minimum viable output:** Continuous CDF at the market expiry time, plus the expected value and its 90% confidence interval.

**If you must compress to N buckets for bandwidth reasons:**
- N = 9 (deciles + midpoint). This preserves enough resolution for expected value estimation within ±2% of full density.
- Even better: store the CDF as a **piecewise linear function** with adaptive knots (more knots where CDF is steep). Typically needs only 15–20 (x,y) pairs for < 1% approximation error.

### Accuracy Improvement: Probabilistic Meta-Feature Vector

Ship a lightweight 5-element "fingerprint" with every prediction:

| Feature | Meaning | Use |
|---------|---------|-----|
| `p_exceed_strike` | Predicted probability | Direct trade input |
| `ci_width` | 90% CI width on that probability | Position sizing |
| `entropy` | Entropy of outcome distribution | Uncertainty flag |
| `n_effective_analogs` | Effective sample size | Trust weight |
| `nwp_consensus` | Mean NWP model prediction | Second opinion |

Traders (algorithmic or human) use these 5 numbers to size positions, not just the single probability. This prevents over-confidence in fragile predictions.

---

## Summary: Implementation Priority

| Priority | Component | Effort | Impact |
|----------|-----------|--------|--------|
| P0 | Continuous CDF output (Tier 1) | Low | Foundation for everything |
| P0 | PIT histogram + KS test | Low | Catch calibration drift early |
| P0 | Out-of-archive detection + clim fallback | Medium | Prevent catastrophic trades |
| P1 | Epistemic/aleatoric decomposition | Medium | Position sizing signal |
| P1 | Ensemble spread scaling | Medium | Free accuracy boost |
| P1 | CRPS decomposition feedback loop | Medium | Closed-loop calibration |
| P2 | Multi-modal GMM detection | High | Edge case, but high value when it fires |
| P2 | Variogram score for trajectories | High | Nice-to-have for path quality |

### One Accuracy Improvement to Implement First

**CRPS-driven analog count adaptation:** Currently you'd pick k (number of analogs) arbitrarily. Instead, scan k = 10..500 and pick the k that minimizes rolling CRPS. Rationale: too few analogs → high variance; too many → include irrelevant analogs → bias. This single tuning decision governs all downstream quality. Implement as a weekly offline recalibration; it's a 10-line change that compounds across every forecast.
