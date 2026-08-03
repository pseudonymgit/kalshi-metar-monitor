# Gray Room — Fusion + Lanes Design: Expert 1 (Bayesian Statistician)

**Date:** 2026-08-03
**Model:** luna-pro (reasoning mode)
**Questions:** Q1 (cascade math), Q4 (two-pool ensemble / 82-member correlation)
**Status:** PRE-DISPATCH (individual expert round)

---

## Executive Summary: The Cascade Is Not Yet Bayesian

The existing FP-MULTI-SIGNAL-FUSION.md is an excellent *architecture* document with a Bayesian-sounding name, but it is not Bayesian. It uses point-estimate frequentist statistics throughout, introduces ad-hoc magic constants (e.g., +0.05 agreement boost), and treats uncertainty as a fixed number rather than a distribution that propagates through layers. 

The 3-layer decomposition is *structurally correct* (different quantities predicted → different layers). But the math needs a rewrite for true Bayesian uncertainty propagation. The biggest single issue: **the current design discards uncertainty at every layer boundary**, which defeats the entire Bayesian promise.

Below, all findings are tagged by category with dispositions.

---

## ERRORS

### E-1 [🔴 HIGH] — The Cascade Is Not Bayesian; It Is Frequentist Point-Estimate

**Current design:** `P(T > B) = count(T_i > B) / 82` — a single scalar. Uncertainty is not tracked.

**Why this is wrong for Bayesian claims:** A Bayesian cascade propagates *distributions*, not point estimates. The current design produces one number per station per bucket. The next layer receives a number, not a posterior. Uncertainty width — which encodes exactly how much we should trust the estimate — is lost.

**Impact:** Every downstream calculation (Goldilocks shift, Kelly sizing) uses a point estimate with no uncertainty. When ensemble spread is wide (high member disagreement), the point estimate looks the same as when all 82 members agree — the cascade cannot distinguish "confident 60/40" from "uncertain 60/40."

**Fix — Full Beta-Binomial Layer 1:**

```
Prior:  θ ~ Beta(α₀, β₀)
        α₀ = β₀ = 1  (uniform, no prior information)

After observing k exceedances out of n members:
Posterior: θ | data ~ Beta(α₀ + k, β₀ + n_eff - k)

Mean:    E[θ] = (α₀ + k) / (α₀ + β₀ + n_eff)
Variance: Var[θ] = (α₀ + k)(β₀ + n_eff - k) / ((α₀+β₀+n_eff)²(α₀+β₀+n_eff+1))
```

Where `n_eff` replaces raw member count `n` to account for member correlation (see Q4 below).

**Downstream effect (Layer 2):** Instead of `P(S > B) = P_ensemble × (1-P_G) + P(T > B-3.2) × P_G`, the Goldilocks layer receives a Beta distribution and marginalizes:

```
P(S > B | data) = ∫ P(S > B | θ) × Beta(θ; α, β) dθ
```

Where `P(S > B | θ) = θ × (1-P_G) + P(T > B - ε | data) × P_G`.

This integral has a closed form via Beta-Binomial conjugacy — no MCMC required.

**Disposition:** **ADVANCE** — This is a math-only change to the fusion design doc. Zero code changes needed at this stage. Effort: ~2 hours specification work.

---

### E-2 [🔴 HIGH] — Temporal Freshness Weighting Does Not Exist

**Current design:** Fresnel discussion of 6h/2h/1.5h half-lives in the framing document, but no implementation math in FP-MULTI-SIGNAL-FUSION.md. The actual cascade spec mentions half-lives as a desideratum without defining how they apply.

**Why this is an error:** Without freshness weighting, an 8-hour-old GEFS run carries the same weight as a 1-hour-old run. The cascade will be path-dependent on run schedule rather than information content.

**Fix — Precision Decay, Not Probability Decay:**

The freshness weight decays the *precision* (inverse variance) of the posterior, not the mean. This correctly widens uncertainty as information ages without biasing the estimate:

```
τ_i = half-life of signal i

At time t since signal was last updated:
  decay_factor(t) = exp(-ln(2) × t / τ_i)

The posterior Beta(α, β) is decayed as:
  α_decayed = 1 + (α - 1) × decay_factor(t)
  β_decayed = 1 + (β - 1) × decay_factor(t)

This preserves:
  E[θ] = α/(α+β) unchanged (mean is not biased by age)
  Var[θ] increases as decay_factor → 0 (uncertainty grows)
```

**Example:** At t=12h with τ=6h (GEFS):
```
decay = exp(-ln(2) × 12/6) = exp(-2 × ln(2)) = 0.25
α_decayed = 1 + (α-1) × 0.25
β_decayed = 1 + (β-1) × 0.25
```
After 24h (4 half-lives): decay = 0.0625 — posterior is 93.75% back to uniform.

**Implementation:** The decay is applied at each cascade step. When a new GEFS run arrives, the prior entering Layer 1 is the decayed posterior from the last run. This means old runs smoothly lose influence without a hard cut-off.

**Freshness half-lives for the three signals entering the cascade:**

| Signal | τ (half-life) | Rationale |
|--------|:-------------:|-----------|
| GEFS ensemble | 6h | Model has 6h refresh; old run is superseded by new |
| ECMWF ensemble | 6h | Same refresh cadence as GEFS (different times, same logic) |
| Frontal passage | 2h | Fronts move through at hours, not days; 6h stale |
| Nowcast (METAR) | 1.5h | METAR observations every 1-3h; fastest decay |
| Forecast aggregation | 6h | Tied to model run schedule |

**Disposition:** **ADVANCE** — Critical spec addition. ~30 min to add to the design doc.

---

### E-3 [🟡 MEDIUM] — Goldilocks Layer Uses Point Estimate of ε

**Current design:** `P(S > B) ≈ P_ensemble × (1-P_G) + P(T > B - 3.2) × P_G`

Uses `E[ε] = 3.2°F` as a single number, discarding the full spike magnitude distribution.

**Why this is an error:** The distribution of Goldilocks spike magnitudes is right-skewed. The mean (3.2°F) ≠ median (≈2.5°F) ≠ typical mode (≈1.5°F). A few large spikes (7-10°F) pull the mean up. Using the mean overstates the settlement probability shift for the modal case.

**More importantly:** The *decision boundary sensitivity* depends on the full distribution, not the mean. If P_G = 0.2 and the ensemble says P(T > B) = 0.45, using the mean says:
```
P(S > B) = 0.45 × 0.8 + 0.70 × 0.2 = 0.50
```
But if the true spike distribution is multimodal (either +1°F or +8°F), the 70% probability at B-3.2 is wrong — it should be a mixture at B-1 and B-8.

**Fix — Marginalize Over ε Distribution:**

```
P(S > B | data) = P_G ∫ P(T > B - ε | data) × f_ε(ε) dε + (1 - P_G) × P(T > B | data)
```

For implementation efficiency, discretize the spike magnitude distribution into 3 bins (from empirical validation):

| ε Bin | Probability | Interpretation |
|:-----:|:----------:|----------------|
| 0-2°F | ~0.55 | Modal small spike |
| 2-5°F | ~0.30 | Medium spike |
| 5+°F  | ~0.15 | Rare large spike (check for sensor malfunction) |

Then:
```
P(S > B) = (1-P_G) × P(T > B) + P_G × [0.55 × P(T > B - 1) + 0.30 × P(T > B - 3.5) + 0.15 × P(T > B - 6)]
```

**Disposition:** **ADVANCE** — Requires fetching the empirical ε distribution from validation data (already collected). ~1 hour to update spec.

---

### E-4 [🟡 MEDIUM] — WhaleWatch Contradiction Handling Is Ad-Hoc

**Current design:**
```
If whale_direction ≠ ensemble_direction:
    conviction_multiplier = 1 - 0.5 × anomaly_score
```

This uses a hard-coded 0.5 penalty factor with no principled basis.

**Fix — Bayesian Evidence Ratio:**

When WhaleWatch detects an anomaly in direction d_w, while the ensemble posterior has expected direction d_e:

```
If d_w = d_e:
    Posterior odds = Prior odds × LR(anomaly | same_direction)
    conviction_multiplier = posterior_odds / prior_odds

If d_w ≠ d_e:
    The anomaly provides evidence AGAINST the ensemble estimate.
    
    P(ensemble_wrong | anomaly) = P(anomaly | ensemble_wrong) × P(ensemble_wrong) / P(anomaly)
    
    conviction_multiplier = 1 - P(ensemble_wrong | anomaly)
```

Where `P(anomaly | ensemble_wrong)` is estimated from historical data: when the ensemble was wrong, did WhaleWatch detect an anomaly at the corresponding bucket?

**Simplification for v1:** Replace with a Bayes-factor lookup table:
| Anomaly Type | Same Direction BF | Opposite Direction BF |
|-------------|:-----------------:|:--------------------:|
| HIGH (z>3) | 2.5 | 0.3 |
| MEDIUM (z>2.5) | 1.5 | 0.6 |
| LOW (z<2.5) | 1.1 | 0.9 |

Then `conviction_multiplier = BayesFactor_same` or `conviction_multiplier = BayesFactor_opposite`.

**Disposition:** **PARK** — We need empirical data to estimate the Bayes factors. Can't determine from first principles. Requires ~8 weeks of WhaleWatch shadow-mode data to populate the lookup table.

---

## IDEAS

### I-1 — Bayesian Surprise Metric as Meta-Signal

**Concept:** Track the KL divergence (a.k.a. Bayesian surprise) between consecutive Layer 1 posteriors:

```
KL(Beta(α_t, β_t) || Beta(α_{t-1}, β_{t-1}))
```

When a new GEFS run arrives and the posterior shifts dramatically (KL > threshold), it signals a **regime change** — the atmosphere has transitioned to a different state. This is intrinsically interesting:

- High surprise + whale anomaly = high conviction (two independent signals agree on change)
- High surprise + no whale = natural regime shift (trade normally)
- Low surprise + whale anomaly = noise or manipulation (reduce conviction)

**Implementation:** Simple KL divergence between two Beta distributions has a closed form:
```
KL(Beta(α',β') || Beta(α,β)) = ln[B(α,β) / B(α',β')] + (α'-α)ψ(α') + (β'-β)ψ(β') + (α-α'+β-β')ψ(α'+β')
```

where ψ is the digamma function and B is the Beta function.

**Potential impact:** Early warning of correctly-priced transitions. If the market hasn't updated to the new GEFS run but our cascade has, we get a brief window of superior information.

**Disposition:** **PARK** — Interesting but unvalidated. Needs a backtest: "does high-KL ⟹ better trade outcome?" on historical data. Effort: ~4 hours to spec the experiment.

---

### I-2 — WhaleWatch as Prior Precision Modulator

**Current design:** WhaleWatch modulates the *mean* of the Kelly fraction via a multiplier.

**Alternative idea:** WhaleWatch should modulate the *precision* (inverse variance) of the Layer 1 posterior, not the Layer 3 sizing. If a whale is detected moving in the same direction as our ensemble, our posterior should be *narrower* (more confident), not our bet larger.

**Mechanism:**

```
If whale matches ensemble direction and anomaly_score > threshold:
    Effective n_eff_increase = anomaly_score × n_whale_equivalent
    Where n_whale_equivalent is a calibration parameter (default: 3)
    
    α_updated = α + k + anomaly_score × n_whale_equivalent × direction_correct
    β_updated = β + (n_eff + anomaly_score × n_whale_equivalent) - (k + ...)
    
    (Only the precision increases, mean shifts only if the whale provides directional evidence)
```

**Why this might be better than a Kelly multiplier:** It applies the conviction signal at the *probability estimation* layer, where Bayesian theory says it belongs. The Kelly fraction then naturally produces a larger bet because the edge is estimated with greater confidence, not because of an ad-hoc multiplier.

**Disposition:** **PARK** — Theoretically cleaner but introduces additional calibration parameters. The Kelly multiplier approach is simpler and sufficient for v1. Revisit if WhaleWatch produces too many false positives in shadow mode.

---

### I-3 — Evidence Accumulation Bridge to Trajectory Lane

**Concept:** The trajectory lane (Q3) produces bucket-level recommendations from historical pattern matching. This can enter the cascade as a **likelihood ratio** on the Layer 1 prior.

If historical patterns suggest bucket B is 2× more likely given current trajectory, the prior for that bucket should be inflated by a Bayes factor of 2:

```
Prior odds (bucket B) = Trajectory BF × Climatology odds
Posterior odds = Prior odds × Ensemble LR
```

This is a clean Bayesian bridge between the trajectory lane and the cascade. The trajectory lane is not a separate decision — it's a prior centering that the ensemble data then updates.

**Disposition:** **PARK** — Depends on trajectory lane design (Q3). Note for panel discussion.

---

## IMPROVEMENTS / SPECS

### S-1 [ADVANCE] — Full Bayesian Cascade Specification

**Spec:** Replace all point estimates in FP-MULTI-SIGNAL-FUSION.md with Beta-distribution propagation.

**The complete three-layer cascade in Bayesian form:**

```
────────────────────────────────────────────────────────────────────────────
LAYER 1: Temperature Belief
────────────────────────────────────────────────────────────────────────────

Input: 82 bias-corrected member temperatures (GEFS + ECMWF)
Output: Posterior Beta(α₁, β₁) = P(T > B | ensemble)

Step 1.1: For each member, compute binary exceedance:
    e_i = 1 if T_i_corrected > B else 0
    k = Σ e_i

Step 1.2: Compute effective sample size (see Q4):
    n_eff = f(n, ρ_avg) where ρ_avg = average pairwise member correlation
    
Step 1.3: Update (freshness-decayed prior from last run → posterior):
    Prior from last run (decayed): Beta(α_prior, β_prior)
    Posterior: Beta(α₁ = α_prior + k, β₁ = β_prior + n_eff - k)

Step 1.4: Apply forecast aggregation as precision boost (not mean shift):
    If 4/4 models agree: n_eff_boosted = n_eff × 1.10
    If 3/4 models agree: n_eff_boosted = n_eff × 1.05
    If 2/4 models agree: no change
    If 1/4 or 0/4: n_eff_boosted = n_eff × 0.90
    
    This is the correct Bayesian treatment: agreement narrows uncertainty (more
    effective samples), it does not shift the mean. The old +0.05 constant was 
    an ad-hoc mean shift which is mathematically wrong.

────────────────────────────────────────────────────────────────────────────
LAYER 2: Settlement Belief (Goldilocks)
────────────────────────────────────────────────────────────────────────────

Input: Layer 1 posterior Beta(α₁, β₁), Goldilocks prediction P_G, ε distribution
Output: Posterior Beta(α₂, β₂) = P(S > B | ensemble, Goldilocks)

Step 2.1: Decompose into mixture:
    With probability (1 - P_G): settlement = actual temperature
        → P(S > B | no Goldilocks) ~ Beta(α₁, β₁)
    
    With probability P_G: settlement = actual + ε
        → P(S > B | Goldilocks) = ∫ P(T > B - ε) × f_ε(ε) dε
        → Discretized: Σ w_j × Beta(α₁, β₁) shifted by ε_j
        → Approximation: Beta(α₁_shifted, β₁_shifted) where the mean is 
          shifted by E[ε | Goldilocks] and variance increases by Var[ε]

Step 2.2: Compute moments of the mixture:
    E[S] = (1-P_G) × E[θ] + P_G × E[θ_shifted]
    Var[S] = (1-P_G)(Var[θ] + E[θ]²) + P_G(Var[θ_shifted] + E[θ_shifted]²) - E[S]²

Step 2.3: Match moments to a Beta:
    α₂ = E[S] × (E[S] × (1-E[S]) / Var[S] - 1)
    β₂ = (1-E[S]) × (E[S] × (1-E[S]) / Var[S] - 1)

(This is moment-matching, the standard approximation for Beta mixtures.)

────────────────────────────────────────────────────────────────────────────
LAYER 3: Bet Sizing (Kelly + WhaleWatch)
────────────────────────────────────────────────────────────────────────────

Input: Layer 2 posterior Beta(α₂, β₂), market price m, WhaleWatch anomaly a
Output: Kelly fraction f*

Step 3.1: Edge from posterior:
    Edge = E[θ₂] - m
    Edge uncertainty = Var[θ₂]  (larger Var → less confident edge)
    
    Effective edge (discounting uncertainty):
    edge_effective = Edge - c_v × sqrt(Var[θ₂])
    where c_v is a variance penalty (default: 1.0 → 1σ discount)

Step 3.2: Kelly fraction (fee-adjusted):
    fee = Kalshi fee formula (confirmed: 0.07¢ per contract)
    edge_net = edge_effective - fee
    f*_raw = edge_net / (1 - m)

Step 3.3: WhaleWatch conviction modulation (see I-2 for alternative):
    conviction_mult = bayes_factor(anomaly_type, direction_alignment)
    
    If no anomaly: conviction_mult = 1.0
    If HIGH, same direction: 2.5
    If HIGH, opposite: 0.3
    If MEDIUM, same: 1.5
    If MEDIUM, opposite: 0.6
    If LOW, same: 1.1
    If LOW, opposite: 0.9

Step 3.4: Final position:
    f* = f*_raw × conviction_mult × tier_discount
    Position = min(f*, 0.25) × bankroll  (quarter-Kelly cap)
────────────────────────────────────────────────────────────────────────────
```

**Implementation path:**
1. Add `n_eff` computation function (takes correlation matrix → effective sample size)
2. Replace `ensemble_fraction.py` with `cascade_layer1.py` returning Beta parameters
3. Add `cascade_layer2.py` for Goldilocks moment-matching
4. Add `cascade_layer3.py` for Bayes-factor Kelly modulation
5. Keep the existing pipeline architecture; swap in new modules at each layer

**Effort:** 3-4 days for the math implementation, plus 1 day for unit tests (Beta distribution library functions exist in scipy.stats).

**Disposition:** **ADVANCE**

---

### S-2 [ADVANCE] — Cascade Orchestration Timestamp Protocol

**Spec:** Every signal entering the cascade must carry a `last_observed_utc` timestamp. The freshness decay is applied deterministically based on this timestamp.

**Implementation:**
```
class CascadeSignal:
    posterior_alpha: float
    posterior_beta: float
    last_observed_utc: datetime  # when the data was fresh
    half_life_hours: float       # signal-specific
    signal_type: str             # "gef ensemble" | "ecmwf ensemble" | "nowcast" | "frontal"
    
    def get_decayed_prior(self, current_time: datetime) -> Tuple[float, float]:
        dt_hours = (current_time - self.last_observed_utc).total_seconds() / 3600
        decay = exp(-ln(2) * dt_hours / self.half_life_hours)
        decay = max(decay, 0.01)  # floor to prevent numerical underflow
        return (
            1 + (self.posterior_alpha - 1) * decay,
            1 + (self.posterior_beta - 1) * decay
        )
```

**Integration:** The GEFS cron pipeline broadcasts new signals as they arrive. The cascade receives them, applies freshness decay to the prior, then performs the Bayesian update. Between updates, the posterior decays.

**Effort:** ~1 day for the orchestration layer.

**Disposition:** **ADVANCE**

---

## ELEPHANTS

### EL-1 [🔴] — The 82-Member Pool Is a Mathematical Illusion at ρ > 0.85

**The uncomfortable truth:** With average pairwise member correlation ρ > 0.85, 82 members provide effectively **1.2 independent temperature forecasts.**

**The math:**
```
n_eff = n / (1 + (n-1) × ρ)

At ρ = 0.85, n = 82:  n_eff = 82 / (1 + 81 × 0.85) = 82 / 69.85 = 1.17
At ρ = 0.60, n = 82:  n_eff = 82 / (1 + 81 × 0.60) = 82 / 49.6 = 1.65
At ρ = 0.30, n = 82:  n_eff = 82 / (1 + 81 × 0.30) = 82 / 25.3 = 3.24
```

**What this means:**
- The naive `count(T_i > B) / 82` overstates our confidence by a factor of ~70×
- The Beta posterior from `Beta(1 + k, 1 + 82 - k)` has variance ~70× smaller than the true uncertainty
- The cascade will be **dangerously overconfident** — it will think its probability estimate is far more precise than it is
- This causes over-betting via Kelly (narrow posterior → high conviction → large position)

**The thing nobody wants to say:** If GEFS and ECMWF share essentially the same atmospheric physics (which they do — both are operational global NWP models assimilating largely the same data), then 82 members are not 82 independent samples. They're 1-2 independent samples with 80 perturbations. The cascade should treat the ensemble as a **2-member ensemble** (GEFS pool mean + ECMWF pool mean), not an 82-member ensemble.

**Recommended fix — Pool-of-pools approach:**

```
Instead of treating 82 members as exchangeable:
  
  Step 1: Compute pool-level exceedance probabilities:
    θ_GEFS = count(GEFS_i > B) / 31  ← point estimate per pool
    θ_ECMWF = count(ECMWF_i > B) / 51 ← point estimate per pool
  
  Step 2: Compute effective sample size per pool:
    n_GEFS_eff = 31 / (1 + 30 × ρ_GEFS_internal)  ← within-GEFS correlation
    n_ECMWF_eff = 51 / (1 + 50 × ρ_ECMWF_internal) ← within-ECMWF correlation
  
  Step 3: Compute cross-pool correlation ρ_cross = Corr(θ_GEFS, θ_ECMWF)
  
  Step 4: Combined posterior from two Beta distributions with correlation ρ_cross:
    This is a bivariate Beta, approximated via:
    - Effective overall sample size: n_total_eff = n_GEFS_eff + n_ECMWF_eff in the 
      uncorrelated limit
    - As ρ_cross → 1: n_total_eff → max(n_GEFS_eff, n_ECMWF_eff)
    - As ρ_cross → 0: n_total_eff → n_GEFS_eff + n_ECMWF_eff
    
    Compromise formula:
    n_total_eff = n_GEFS_eff + n_ECMWF_eff × (1 - ρ_cross)
    k_total = n_GEFS_eff × θ_GEFS + n_ECMWF_eff × (1 - ρ_cross) × θ_ECMWF
```

**This is the single most important finding I produce.** If the cascade does not account for member cross-correlation, it will be dangerously overconfident and over-bet.

**Disposition:** **ADVANCE** — Must be measured and implemented before the cascade goes live.

---

### EL-2 [🔴] — The Bayesian Label Is Costing Us Real Engineering

**The uncomfortable truth:** The current system runs at 66.17% accuracy with a Sharpe of 11.36 on 2,096 trades. The Bayesian cascade has NOT been shown to improve either metric. We are adding mathematical complexity to a system that already works. Bayesian uncertainty propagation will widen Kelly bet sizes (because uncertainty → variance → smaller bets in the variance-penalized version) — this will likely *reduce* P&L, not increase it.

**Why it might still be worth it:** The Bayesian cascade protects against *bad* decisions, not just makes *good* ones. If it prevents one catastrophic over-bet (Kelly over-betting is the classic destroyer of trading accounts), it pays for itself. But the framing needs to be honest: this is **risk management** math, not profit-maximization math. The profit comes from the ensemble calibration (66.17%), not from the Bayesian wrapping.

**Disposition:** **ADVANCE** (but reframe the narrative — call it "Uncertainty-Weighted Cascade" not "Bayesian Cascade" to set correct expectations)

---

### EL-3 [🟡] — Goldilocks Event Rate (7.4%) Means Layer 2 Is Inactive 92.6% of Days

**The uncomfortable truth:** The Goldilocks model predicts P(G=1) > 0.15 on <10% of days. That means Layer 2 of the cascade (settlement belief shift) is a no-op on >90% of trading days. We are building a three-layer Bayesian cascade where Layer 2 essentially doesn't fire.

**Impact:** For 90+% of trades, the cascade degenerates to a two-layer model:
- Layer 1: Temperature belief (same as today)
- Layer 3: Kelly sizing

This doesn't mean we shouldn't build Layer 2 — but the operational complexity of maintaining and validating a Goldilocks model that fires <10% of the time may not be justifiable. The 90th percentile trades (where Goldilocks matters) need to produce outsized returns to cover the cost of the other 90%.

**Recommendation:** Validate offline: take the 90-10 split (trades where G fires vs doesn't) and compute P&L contribution of each group. If the Goldilocks-adjusted trades contribute <20% of total P&L, KILL Layer 2 as a cascade component and handle it as a post-hoc override.

**Disposition:** **PARK** — Needs P&L attribution analysis before deciding.

---

### EL-4 [🟡] — The Cascade Won't Help Before Settlement Day

The cascade updates Layer 1 (temperature belief) on each GEFS/ECMWF run. But the GEFS ensemble *already* produces ~66% accuracy on Day 1 forecasts. The fundamental constraint on weather trading is not model architecture — it's that weather forecasting has physical upper limits on predictability. No cascade design can predict convective initiation at Day 4.

**What this means:** Most of the cascade's complexity applies to the f024 forecast (day-ahead). For f048-f120 (2-5 day), the ensemble spread dominates, and no Bayesian update structure will materially improve on a simple ensemble mean. The cascade is a ~1-day-horizon tool.

**Implication:** Don't spend 4 weeks building a cascade for f024-f120 when the existing GEFS baseline already handles f024. The cascade should be scoped to f024 horizon only, with the understanding that for longer horizons, the simple ensemble mean is the best we can do.

**Disposition:** **ADVANCE** — Scope restriction: cascade applies only to f024.

---

## Q4 — Two-Pool Ensemble (GEFS 31 + ECMWF 51) Detailed Analysis

### 4.1 Core Question: How Does the Cascade Handle Two Pools

**Short answer:** Treat them as separate exchangeable pools within Layer 1, each with its own Beta posterior, then combine using a hierarchical model that accounts for cross-pool correlation.

**Long answer — three approaches ranked by correctness:**

| Approach | Correctness | Complexity | Recommendation |
|----------|:-----------:|:----------:|:--------------:|
| **Pool-of-pools** (hierarchical Beta) | HIGH | 3/5 | ✅ ADVANCE |
| **Weighted member-pooling** | MEDIUM | 2/5 | Acceptable v1 |
| **Naive 82-count** | LOW | 1/5 | ❌ KILL |

---

### 4.2 Pool-of-Pools (Recommended)

**Rationale:** GEFS and ECMWF are different models with different physics, resolutions, and data assimilation. They should not be treated as exchangeable draws from the same distribution. Instead, each pool produces a pool-level exceedance probability, then these two probabilities are combined.

**Math:**

```
For each pool p ∈ {GEFS, ECMWF}:

Step 1: Compute within-pool effective sample size:
    n_eff_p = n_p / (1 + (n_p - 1) × ρ_within_p)
    where ρ_within_p = average pairwise member correlation within pool p

Step 2: Compute pool-level exceedance count:
    k_p = Σ i=1..n_p I(T_i > B)
    
Step 3: Pool-level posterior (uniform prior):
    θ_p ~ Beta(1 + k_p, 1 + n_eff_p - k_p)

Step 4: Compute cross-pool correlation ρ_cross from historical data:
    ρ_cross = Corr(θ_GEFS, θ_ECMWF) over historical dates
    
Step 5: Combine using correlation-adjusted weighting:
    
    Let w_G = n_eff_GEFS / (n_eff_GEFS + n_eff_ECMWF)  (precision-weighted)
    
    If ρ_cross < 0.5:  pools are meaningfully independent
        θ_combined = w_G × θ_GEFS + (1-w_G) × θ_ECMWF
        n_combined_eff = n_eff_GEFS + n_eff_ECMWF  (sum of effective sizes)
        
    If 0.5 ≤ ρ_cross < 0.85:  some overlap
        θ_combined = w_G × θ_GEFS + (1-w_G) × θ_ECMWF
        n_combined_eff = max(n_eff_GEFS, n_eff_ECMWF) + min(n_eff_GEFS, n_eff_ECMWF) × (1-ρ_cross)
        
    If ρ_cross ≥ 0.85:  pools are effectively redundant
        θ_combined = (n_eff_GEFS × θ_GEFS + n_eff_ECMWF × θ_ECMWF) / (n_eff_GEFS + n_eff_ECMWF)
        n_combined_eff = max(n_eff_GEFS, n_eff_ECMWF)
        (The smaller pool adds zero effective information)

Step 6: Combined posterior:
    k_combined = round(n_combined_eff × θ_combined)
    θ_combined ~ Beta(1 + k_combined, 1 + n_combined_eff - k_combined)
```

**Example calculations:**

| Scenario | ρ_GEFS | ρ_ECMWF | ρ_cross | n_eff_GEFS | n_eff_ECMWF | n_combined_eff |
|----------|:------:|:-------:|:-------:|:----------:|:-----------:|:--------------:|
| Optimistic | 0.30 | 0.25 | 0.30 | 1.79 | 2.61 | 4.40 |
| Realistic | 0.60 | 0.55 | 0.60 | 1.23 | 1.68 | 1.90 |
| Pessimistic | 0.85 | 0.80 | 0.85 | 1.06 | 1.22 | 1.22 |

**Even in the optimistic scenario, n_combined_eff = 4.4 — meaning the 82-member ensemble provides ~4 independent temperature forecasts.** This is the sobering reality.

---

### 4.3 What to Measure (Immediate Action)

Before ANY cascade implementation, measure these three quantities from the existing data:

| Metric | Data Source | How to Compute |
|--------|-------------|----------------|
| ρ_within_GEFS | 363,440 rows of GEFS archive | For each date×station×step, compute pairwise member correlation. Average across all pairs. |
| ρ_within_ECMWF | 75,799 rows of ECMWF archive | Same approach as GEFS. |
| ρ_cross | Both archives on overlapping dates (758 dates) | Compute θ_GEFS and θ_ECMWF for each date×station×step. Take Pearson correlation. |

**Effort:** ~2 hours to run the analysis on the existing Parquet/CSV data. Existing code in `prototypes/weather-engine-source/core/` can likely reuse `ensemble_diversity.py` and `rolling_calibration.py`.

---

### 4.4 What If ρ_cross > 0.85?

If cross-pool correlation exceeds 0.85, the 82-member ensemble is effectively 1-2 independent forecasts. The ECMWF backfill effort (>758 dates) has not meaningfully increased our independent information. **This is the primary risk to the backfill project's ROI.**

**Recommendation:** Even if ρ_cross > 0.85, the ECMWF pool still adds value for two reasons:

1. **Model diversity protects against systematic model failure.** If GEFS has a systematic bias on a specific weather pattern, ECMWF (with different physics) may not share that bias. The cascade handles this via the pool-level Beta posteriors — if one pool is systematically biased, its posterior mean shifts.

2. **The backfill validation check is binary, not granular.** The existing 29,000-pair calibration was done on GEFS-only. Adding ECMWF and re-running the calibration sweep is the only way to know if ECMWF improves accuracy. The correlation analysis tells us the *information ratio*; the calibration sweep tells us the *actual P&L impact*.

**Fail-forward strategy:**
```
if ρ_cross > 0.85:
    # Pools are redundant, but diversity may protect against failure
    # Continue with pool-of-pools using the pessimistic formula from §4.2
    # Expect: minimal improvement over GEFS-only, but lower failure risk
    
    # Key monitoring threshold:
    # If GEFS accuracy ever drops below 58% on a 90-day rolling window,
    # ECMWF becomes the primary pool (accuracy-derived weighting)
else:
    # Pools provide independent information
    # Full pool-of-pools from §4.2
    # Expected: marginal accuracy improvement (1-2pp) from ensemble diversity
```

---

### 4.5 Implementation Path for 82-Member Integration

| Step | Action | Prerequisite | Effort |
|:----:|--------|:-----------:|:-----:|
| 1 | Measure ρ_within_GEFS, ρ_within_ECMWF, ρ_cross from archive | ECMWF backfill complete (currently at 758/1,200+ dates) | 2h |
| 2 | Implement `pool_of_pools.py` — hierarchical Beta combination | Step 1 | 1d |
| 3 | Integrate into `cascade_layer1.py` | Step 2 | 0.5d |
| 4 | Run calibration sweep: GEFS-only vs GEFS+ECMWF pool-of-pools | Steps 2-3 | 1d (compute) |
| 5 | Compare P&L, Sharpe, accuracy, max drawdown across both configurations | Step 4 | 0.5d |
| 6 | Go/no-go decision: does ECMWF improve total P&L by >5%? If not, drop ECMWF from pipeline (keep as fault-tolerant backup) | Step 5 | Decision |

**Total effort:** ~3 days + backfill completion.

---

## Consolidated Output Table

### ERRORS
| # | Finding | Severity | Impact | Fix | Disp |
|---|---------|:--------:|:------:|-----|:----:|
| E-1 | Cascade uses point estimates, not distributions | 🔴 HIGH | Overconfident posteriors → over-betting via Kelly | Beta-binomial Layer 1 with uncertainty propagation | ADVANCE |
| E-2 | Temporal freshness weighting missing from spec | 🔴 HIGH | 8h-old GEFS = same weight as 1h-old | Precision-decay formula with τ=6h/2h/1.5h | ADVANCE |
| E-3 | Goldilocks uses point estimate E[ε]=3.2°F | 🟡 MEDIUM | Settlement shift overstated for modal case | Marginalize over 3-bin ε distribution | ADVANCE |
| E-4 | WhaleWatch contradiction handler is ad-hoc constant | 🟡 MEDIUM | No principled basis for 0.5 penalty | Bayes-factor lookup table (PARK — needs data) | PARK |

### IDEAS
| # | Idea | Impact | Test/Spec | Disp |
|---|------|:------:|-----------|:----:|
| I-1 | Bayesian surprise (KL divergence) as regime-change meta-signal | MEDIUM | "Does high-KL → better trade?" backtest on historical | PARK |
| I-2 | WhaleWatch as prior precision modulator instead of Kelly multiplier | MEDIUM | Simpler math, harder calibration. Test in shadow mode | PARK |
| I-3 | Trajectory lane enters cascade as Bayes factor on Layer 1 prior | MEDIUM | Depends on Q3 trajectory spec. Bridge, not gate | PARK |

### IMPROVEMENTS / SPECS
| # | Spec | Effort | Detail | Disp |
|---|------|:------:|--------|:----:|
| S-1 | Full Bayesian cascade spec (Beta-binomial propagation) | 4-5d | Complete math for all 3 layers, moment-matching, precision decay | ADVANCE |
| S-2 | Cascade timestampt protocol (CascadeSignal class) | 1d | Every signal carries last_observed_utc; freshness decay applied at each step | ADVANCE |
| S-3 | Pool-of-pools hierarchical combination for GEFS+ECMWF | 3d | Correlation-adjusted pooling, n_eff computation | ADVANCE |
| S-4 | Cross-pool correlation measurement from existing archive | 2h | Compute ρ_within and ρ_cross from Parquet/CSV data | ADVANCE |

### ELEPHANTS
| # | Elephant | Why Uncomfortable | Disp |
|---|----------|:-----------------:|:----:|
| EL-1 | 82-member ensemble is ~1-4 effective independent members at expected correlations | Says the ~$10K backfill effort may add negligible independent information | ADVANCE |
| EL-2 | Bayesian cascade may reduce P&L (optimal Kelly shrinks with uncertainty) | Bayes makes us *more conservative*, not more profitable | ADVANCE (reframe) |
| EL-3 | Goldilocks Layer 2 fires on <10% of trading days | A 3-layer cascade where Layer 2 is mostly dormant | PARK |
| EL-4 | Cascade only applies to f024; longer horizons can't be improved | The expensive cascade architecture is a 1-day tool | ADVANCE |

### CLEANUP STATUS
| Category | Total | ADVANCE | PARK | KILL |
|----------|:-----:|:-------:|:----:|:----:|
| ERRORS | 4 | 3 | 1 | 0 |
| IDEAS | 3 | 0 | 3 | 0 |
| IMPROVEMENTS/SPECS | 4 | 4 | 0 | 0 |
| ELEPHANTS | 4 | 3 | 1 | 0 |
| **Total** | **15** | **10** | **5** | **0** |

### WHAT TO DO NEXT
| Order | Item | Effort | Type | Depends On |
|:-----:|------|:------:|:----:|:----------:|
| 1 | Measure ρ_within_GEFS, ρ_within_ECMWF, ρ_cross from archive | 2h | SPEC/S-4 | ECMWF backfill at current 758 dates (sufficient) |
| 2 | Stop calling it "Bayesian Cascade" — use "Uncertainty-Weighted Cascade" in all docs | 0h | ELEPHANT/EL-2 | — |
| 3 | Add Beta-binomial Layer 1 spec to FP-MULTI-SIGNAL-FUSION.md | 2h | SPEC/S-1 | — |
| 4 | Add precision-decay freshness formula to FP-MULTI-SIGNAL-FUSION.md | 0.5h | SPEC/S-2 | — |
| 5 | Implement pool-of-pools hierarchical combination | 3d | SPEC/S-3 | Step 1 (correlation measurements) |
| 6 | Scope cascade to f024 only; f048+ uses simple ensemble mean | 0h | ELEPHANT/EL-4 | — |
| 7 | Full Bayesian cascade implementation | 4-5d | SPEC/S-1 | Steps 3-6 |

---

*End of Expert 1 (Bayesian Statistician) submission. Ready for panel discussion.