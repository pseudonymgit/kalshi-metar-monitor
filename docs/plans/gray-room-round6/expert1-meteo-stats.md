# Expert 1: Meteorological Statistician / Bayesian — Probabilistic Trajectory Prediction via Analog Ensembles

**Date:** 2026-07-21
**Author:** Gray Room — Meteo Stats Working Group
**Status:** Design Spec

---

## 1. Analog Matching Methodology

### 1.1 Distance Metric

We need a **multivariate, scale-aware distance** that handles 21 heterogeneous variables (temperature, wind components, pressure, advection, cloud cover) over multiple forecast horizons (e.g., 0–72h at 3h or 6h steps).

**Recommendation: Mahalanobis distance on a compressed feature space.**

| Component | Detail |
|---|---|
| **Primary metric** | Mahalanobis distance: $D_M(x, y) = \sqrt{(x - y)^T \Sigma^{-1} (x - y)}$ |
| **Why** | Accounts for covariance between variables (e.g., T2m and T850 are correlated; wind speed and advection are correlated). Without this, heavily correlated variables are double-counted. |
| **Preprocessing** | PCA/EOF compression on the 21-variable × N-horizon matrix → retain top K components explaining ≥95% variance. This reduces noise, handles multicollinearity, and speeds computation. |
| **Alternative** | Energy distance (Szekely 2003) if we want a distribution-free metric that is robust to non-Gaussian margins. Higher computational cost. |

### 1.2 Variable Weighting

**Yes, weight variables — but learn weights, don't guess them.**

| Method | Detail |
|---|---|
| **Option A: Gradient-based weight learning** | Treat weights as hyperparameters. Minimize CRPS (Continuous Ranked Probability Score) on a validation set via Nelder-Mead or Bayesian optimization. Variables that contribute more to predictive skill get higher weight. |
| **Option B: Mutual information weighting** | Compute $I(X_i; Y)$ between each variable and the observed trajectory outcome. Weights $w_i \propto I(X_i; Y)$. Simple, interpretable, but univariate. |
| **Option C: Variable-group weighting** | Group variables by physical process (thermodynamics: T2m, T850, dewpoint; dynamics: wind U, V, MSLP; clouds: total cloud, low cloud). Assign one weight per group. Reduces parameter count from 21 to ~5. |

**Suggestion:** Start with **Option C** (5-group weights), verify with Option A.

### 1.3 Optimal Number of Analogs (N)

**N = 20–50, tuned via cross-validation.**

| N Range | Behavior |
|---|---|
| **N < 10** | Distribution too sparse; high variance; calibration unstable. |
| **N = 20–50** | Sweet spot for ensemble spread vs. bias. Common in operational analog methods (e.g., ECMWF's 20-member EPS). |
| **N > 100** | Diminishing returns; distribution drifts toward climatology; locality degrades. |

**Tuning rule:** Choose N that minimizes CRPS on a held-out validation set. Sweep N in {5, 10, 20, 30, 50, 100, 200}.

---

## 2. Trajectory Distribution

### 2.1 Method

**Recommendation: Bayesian Bootstrap with Kernel Smoothing (BB-KS).**

| Step | Description |
|---|---|
| **1. Retrieve N analogs** | Find nearest N historical NWP patterns (by Mahalanobis distance). |
| **2. Assign Dirichlet weights** | Sample $N$ weights from Dirichlet(1, 1, ..., 1) — the Bayesian bootstrap. Repeat $M = 500$ times. |
| **3. Weighted kernel density** | For each bootstrap sample, construct a kernel density estimate (KDE) over the observed trajectory outcomes (e.g., temperature change at each lead time). Kernel width = Silverman's rule-of-thumb scaled by the analog distances (closer analogs get narrower kernels). |
| **4. Aggregate** | Average the $M$ KDEs → final continuous probability density. |

**Why not alternatives:**

| Alternative | Pro | Con |
|---|---|---|
| **Simple histogram** | Easy | Discretization artifacts; poor with small N. |
| **KDE only** | Smooth | Ignores sampling uncertainty in analog selection. |
| **Bayesian bootstrap only** | Handles sampling uncertainty | Discrete; no smoothing. |
| **BB-KS (recommended)** | Both smoothness and uncertainty quantification | Slightly more computation (500× KDE — trivial). |

### 2.2 Output Representation

For each forecast horizon $h$ (e.g., +6h, +12h, ..., +72h), output:

```json
{
  "horizon_h": "+12",
  "variable": "T2m_change",
  "distribution": {
    "p10": -1.2,
    "p25": 0.1,
    "p50": 1.5,
    "p75": 3.0,
    "p90": 4.1,
    "mean": 1.6,
    "n_analogs": 30,
    "analog_weights": [0.042, 0.038, ...]
  }
}
```

Also produce **discrete bins** as required: probability of temperature change in each bin:

```json
{
  "bins": [
    {"range": "(-inf, -4]", "prob": 0.02},
    {"range": "(-4, -2]", "prob": 0.05},
    {"range": "(-2, 0]",  "prob": 0.08},
    {"range": "(0, 2]",   "prob": 0.35},
    {"range": "(2, 4]",   "prob": 0.35},
    {"range": "(4, inf)", "prob": 0.15}
  ]
}
```

---

## 3. Calibration

### 3.1 Calibration Methods

**Recommendation: Isotonic Regression + Beta-Binomial Adjustment.**

| Method | Detail |
|---|---|
| **Isotonic regression** | Non-parametric: learn a monotonic mapping from raw probability → calibrated probability. Applied per-bin or per-quantile. Works well with ≥1000 validation samples. |
| **Platt scaling** | Parametric (logistic): $P_{cal} = 1 / (1 + \exp(a \cdot P_{raw} + b))$. Good for binary bins, weak for multi-bin. |
| **Beta-binomial adjustment** | Bayesian: treat each bin probability as a Beta posterior $Beta(\alpha + k, \beta + N - k)$ where $k$ is observed hits and $N$ is forecast count. Shrinks extreme probabilities toward climatology. |

**Two-stage approach:**
1. Apply **Beta-binomial** shrinkage to raw analog proportions (handles small-N overconfidence).
2. Apply **isotonic regression** across the full validation set (handles residual miscalibration patterns).

### 3.2 Calibration Diagnostics

| Metric | Target | When |
|---|---|---|
| Reliability diagram | Diagonal (45°) | Per-bin |
| PIT histogram | Uniform | Continuous distribution |
| CRPS | Minimize | Overall skill |
| Brier score decomposition | Reliability < 0.01 | Per-bin |

### 3.3 Continuous Calibration Tracking

Maintain a sliding-window calibration database (last 90 days of forecasts). Re-fit isotonic regression weekly. If the reliability diagram shows consistent drift (e.g., 3 consecutive weeks of miscalibration > 0.05), flag for retraining.

---

## 4. Coverage: 18 Months of Data — Is It Enough?

### 4.1 Coverage Analysis

| Aspect | Assessment |
|---|---|
| **Seasonal cycles** | 18 months = 1.5 annual cycles. Marginal for capturing full seasonal envelope. Risk: missing a winter pattern's analog in summer (or vice versa). |
| **Synoptic diversity** | Depends on location. Mid-latitudes: ~18 months covers ~550 synoptic-scale systems. Likely sufficient for common patterns. |
| **Extreme events** | Insufficient. A 1-in-10-year event has ~15% chance of appearing in 18 months. Rare events will be poorly represented. |
| **Dimensionality** | 21 variables × ~1300 forecast times = 27,300 data points per station. The curse of dimensionality is real: even with 18 months (~13,000 hourly obs), analog density is sparse in high-D space. |

### 4.2 Recommendations

| Recommendation | Rationale |
|---|---|
| **Target 36–60 months** | 3–5 years captures multiple seasonal cycles and more rare events. |
| **Use PCA compression** | Reduce 21 variables to 5–10 EOFs. Mitigates curse of dimensionality. |
| **Multi-station pooling** | Pool analogs across stations with similar climate regimes (e.g., all coastal stations, all inland stations). Effectively multiplies the analog library by 5–10×. |
| **Synthetic analogs for extremes** | Perturb existing analogs with small (0.1σ) Gaussian noise to generate near-miss cases. Improves density around rare events. |

**Verdict:** 18 months is **barely sufficient** for common patterns, **insufficient** for extremes. Use PCA + multi-station pooling to compensate.

---

## 5. Validation

### 5.1 Validation Framework

**Goal:** Prove that the full probabilistic trajectory distribution is better than a simpler direction-confidence signal.

| Test | Method | What It Proves |
|---|---|---|
| **CRPS comparison** | Compute CRPS for trajectory distribution vs. CRPS for a binary direction model (e.g., "70% chance up"). Lower CRPS = better. | Continuous distribution adds value. |
| **Brier skill score** | Decompose Brier score for each bin. Compare against climatology and against a binary direction model. | Calibration + resolution improvement. |
| **Probability integral transform (PIT)** | At each forecast, compute $PIT_i = F(y_i)$ where $y_i$ is the observed outcome. PIT should be Uniform(0,1). | Distribution is well-calibrated. |
| **Murphy diagrams** | Plot expected loss vs. decision threshold for the full distribution vs. the binary model. | Economic value of probabilistic forecasts. |
| **Sharpness** | Mean width of 90% prediction interval. Narrower = better (conditional on calibration). | Practical usefulness. |

### 5.2 Validation Protocol

1. **Temporal cross-validation** — Train on months 1–12, validate on months 13–18. Then slide forward: train on 2–13, validate on 14–18. Report mean CRPS.
2. **Seasonal holdout** — Hold out entire winter (Dec–Feb) from training, test on it. Repeat for summer. This tests season-agnostic generalization.
3. **Station-level split** — Train on 15 stations, test on 5 unseen stations. Tests spatial generalization.
4. **Head-to-head: distribution vs. binary** — For each forecast, compute the CRPS of the full distribution and the CRPS of a discretized binary version. Report mean CRPS ratio.

### 5.3 Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| CRPS improvement over binary | ≥ 15% | < 5% |
| PIT Kolmogorov-Smirnov test | p > 0.05 | p < 0.01 |
| Brier reliability component | < 0.02 | > 0.05 |
| 90% interval coverage | 85–95% | < 80% or > 96% |

---

## 6. Limitations & Failure Modes

### 6.1 Biggest Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Non-stationarity** — Climate/system drift over 18 months means historical patterns are not representative of future. | High | Test for drift via rolling mean of NWP bias. If drift > 0.5σ, retrain or use adaptive analog library (discard > 6-month-old samples). |
| **Curse of dimensionality** — 21 variables in high-D space mean analog distances are all similar (distances concentrate). | High | PCA compression to 5–10 dimensions. Monitor the "effective rank" of the analog distance matrix. |
| **Rare event blind spots** — Extreme trajectories never seen in training data get zero probability. | High | Add synthetic perturbations. Use a "safety" prior: mix analog distribution with a climatological background (e.g., 80% analog, 20% climatology). |
| **Temporal autocorrelation** — Consecutive forecast cycles share analogs, creating false confidence. | Medium | Ensure analog library excludes the ±7-day window around the target date. |
| **Variable non-Gaussianity** — Variables like cloud cover (bounded, bimodal) violate Mahalanobis normality assumption. | Medium | Use copula-based distance or transform each variable to Gaussian via quantile mapping before computing distance. |
| **Multi-model inflation** — 5 models × 30 analogs = 150 trajectories, but they are not independent. | Medium | Treat each model as a separate ensemble member. Use a Bayesian model averaging (BMA) step to weight models by recent performance. |

### 6.2 Accuracy-Improvement Suggestion (Recommended)

**Accuracy-Improvement #1: Time-Lagged Analog Ensembles (TLAE)**

Instead of matching only the current forecast cycle, match **the last 3 forecast cycles** as a combined pattern.

- Current forecast: initialized at T=0
- Also include T−6h, T−12h forecast outputs for the same valid time
- Analog distance is computed over the concatenated 3-cycle feature vector

**Why this works:** NWP models have cycle-to-cycle persistence. A forecast that consistently predicts a warm bias over 3 cycles is more reliable than one that flips. The 3-cycle pattern captures model error autocorrelation, which dense analog matching uses to infer trajectory confidence.

**Expected improvement:** +5–10% CRPS reduction over single-cycle analog matching, based on operational experience with lagged ensembles (e.g., NECP's GEFS reforecast studies).

**Accuracy-Improvement #2: Analog Distance Weighting with Exponential Decay**

Weight analogs by distance: $w_i \propto \exp(-\lambda D_i)$, not just inverse distance or uniform.

- $\lambda$ controls how quickly weight drops with distance
- Tune $\lambda$ via CRPS minimization on validation set
- Prevents distant analogs from contaminating the distribution

**Why this works:** Not all N analogs are equally good. Exponential weighting gracefully degrades the influence of mediocre matches while keeping them in the ensemble for uncertainty quantification.

---

## Appendix A: Implementation Sketch

```python
def analog_trajectory(
    forecast: np.ndarray,        # shape: (21, n_horizons)
    historical_nwp: np.ndarray,  # shape: (n_samples, 21, n_horizons)
    historical_obs: np.ndarray,   # shape: (n_samples, n_horizons)
    n_analogs: int = 30,
    n_bootstrap: int = 500,
    pca: PCA = None,
    lambda_exp: float = 0.5,
) -> dict:
    # 1. Compress via PCA
    f_flat = forecast.reshape(1, -1)
    h_flat = historical_nwp.reshape(n_samples, -1)
    if pca:
        f_flat = pca.transform(f_flat)
        h_flat = pca.transform(h_flat)

    # 2. Compute Mahalanobis distances
    cov_inv = np.linalg.inv(np.cov(h_flat, rowvar=False))
    dists = np.array([mahalanobis(f_flat[0], h, cov_inv) for h in h_flat])

    # 3. Select N nearest analogs
    idx = np.argsort(dists)[:n_analogs]
    analog_obs = historical_obs[idx]
    analog_dists = dists[idx]

    # 4. Exponential distance weights
    raw_weights = np.exp(-lambda_exp * analog_dists)
    base_weights = raw_weights / raw_weights.sum()

    # 5. Bayesian bootstrap
    trajectories = []
    for _ in range(n_bootstrap):
        w = np.random.dirichlet(np.ones(n_analogs))
        w *= base_weights
        w /= w.sum()
        kde = KDE(analog_obs, weights=w, bw_method='silverman')
        trajectories.append(kde)

    # 6. Aggregate
    return aggregate_distribution(trajectories)
```

## Appendix B: Key References

1. Hamill, T.M. & Whitaker, J.S. (2006). "Probabilistic Quantitative Precipitation Forecasts Based on Reforecast Analogs." *Mon. Wea. Rev.*
2. Delle Monache, L. et al. (2013). "Kalman filter and analog-based approaches." *Wea. Forecasting.*
3. Gneiting, T. & Katzfuss, M. (2014). "Probabilistic forecasting." *Annu. Rev. Stat. Appl.*
4. Bröcker, J. & Smith, L.A. (2007). "Increasing the reliability of reliability diagrams." *Wea. Forecasting.*
5. Efron, B. (2012). "Bayesian inference and the parametric bootstrap." *Ann. Appl. Stat.*