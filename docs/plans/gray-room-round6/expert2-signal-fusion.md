# Expert 2: Signal Fusion Architecture

## Gray Room Round 6 — Fusion Spec

**Date:** 2026-07-21
**Author:** Signal Fusion Expert
**Status:** Draft for Review

---

## Executive Summary

We have two decorrelated signal lanes — METAR (observation-based, ~72% accuracy) and NWP (forecast-model analog, ~76% reported) — and need a fusion layer that maximizes the combined edge. This document specifies the architecture, mathematics, implementation plan, and calibration procedures.

---

## 1. OR Gate Architecture vs. Bayesian Fusion

### Verdict: **Bayesian fusion is correct; OR gate is a rough approximation.**

**OR gate** — If either lane says "trade", trade with lower confidence — is a *decision rule*, not a fusion architecture. It discards information: it doesn't distinguish between one-lane-up vs. two-lanes-up, and it ignores the base rate and lane reliability.

**Bayesian fusion** is the principled framework. We want:

```
P(truth = UP | M, N) ∝ P(M | truth) * P(N | truth) * P(truth)
```

where `M` = METAR signal (UP/DOWN/SKIP), `N` = NWP signal, and `P(M | truth)` is the lane's conditional likelihood given the true direction.

Under conditional independence (see §5), this factorizes cleanly:

- **P(truth=UP | M=UP, N=UP)** — both agree → high posterior
- **P(truth=UP | M=UP, N=DOWN)** — disagree → posterior approaches prior
- **P(truth=UP | M=UP, N=SKIP)** — reduces to single-lane posterior

### Recommendation: Use **Bayesian fusion** as the primary architecture. Implement OR gate as a fallback/baseline for A/B comparison.

**Fusion Formula (Log-Odds Form)**

The cleanest implementation uses log-odds (logit):

```
logit(P) = ln( P / (1-P) )

logit(P_truth | M, N) = logit(prior) + LL_M + LL_N
```

where `LL` = log-likelihood ratio of each lane:

```
LL = ln( P(signal | truth=UP) / P(signal | truth=DOWN) )
```

This means:
- **Both agree**: `prior + 2 * LL` (strong boost)
- **One lane**: `prior + LL` (moderate)
- **Disagree**: `prior + LL_M(UP) + LL_N(DOWN)` — typically cancels to near-prior
- **Both skip**: `prior` (no information)

**Implementation Payoff:** Log-odds fusion is additive, debuggable, and trivially maps to confidence scores via `P = 1/(1+exp(-logit))`. It's a single file of ~50 lines of Python.

---

## 2. Confidence Modulation — When Both Agree

### Mathematically correct boost

When both lanes say UP (or both say DOWN), the correct confidence is derived from Bayes, not from arbitrary addition or multiplication.

**Bayesian boost (correct):**

```
P(UP | M=UP, N=UP) = P(UP) * P(M=UP|UP) * P(N=UP|UP) / Z
```

where Z normalizes over both outcomes.

**Example with numbers** (assuming prior = 0.55, each lane calibrated at 0.72):

| Scenario | P(UP \| signals) | Confidence Boost |
|---|---|---|
| Prior only (no signals) | 0.55 | — |
| One lane says UP | ~0.75 | +20pp |
| Both lanes say UP | ~0.87 | +32pp |
| One says UP, one SKIP | ~0.75 | +20pp (no change) |
| One says UP, one DOWN | ~0.55 | +0pp (back to prior) |

### Key insight: The boost is NOT additive.

If each lane individually gives +20pp, the combined boost is NOT +40pp — it's about +32pp. This is because the signals are (assumed) conditionally independent but not perfectly informative. The Bayesian calculation handles saturation naturally.

### Implementation in log-odds:

```
# Calibrated values (from training period):
# METAR: P(UP|M=UP) = 0.72  => LL_M = ln(0.72/0.28) ≈ 0.944
# NWP:   P(UP|N=UP) = 0.76  => LL_N = ln(0.76/0.24) ≈ 1.153
# Prior: P(UP) = 0.55       => logit_prior = ln(0.55/0.45) ≈ 0.201

# Both UP:
logit = 0.201 + 0.944 + 1.153 = 2.298
P = 1 / (1 + exp(-2.298)) ≈ 0.909

# One UP:
logit = 0.201 + 0.944 = 1.145
P = 1 / (1 + exp(-1.145)) ≈ 0.759
```

Both UP gives ~91% confidence vs. ~76% for one lane. This is the mathematically correct answer.

### Recommendation: Use the Bayesian/log-odds formulation. Do NOT use:
- ❌ Simple average (dilutes signal)
- ❌ Multiplication of probabilities (violates probability theory without normalization)
- ❌ Hard-coded boost factors (not adaptive to changing lane performance)

---

## 3. Disagreement Handling — UP vs. DOWN

### Analysis

When NWP says UP and METAR says DOWN, we have conflicting information from two ~70% accurate sources.

### Options ranked:

| Option | Logic | Recommended? |
|---|---|---|
| **3a. Skip trade** | No edge when signals conflict | ✅ **Primary recommendation** |
| 3b. Trade with prior | Use base rate only (prior) | ✅ Fallback if fill rate matters |
| 3c. Trade with lowest confidence | Pick the winning lane's confidence | ❌ Cherry-picks |
| 3d. Weight by recency/station | Heuristic tiebreaker | ❌ Adds complexity, limited gains |

### Recommendation: **Skip trade on disagreement.**

**Reasoning:**
- When signals disagree, the posterior collapses to ~prior (0.55), which is barely above break-even (0.50). After transaction costs, this is a losing bet.
- Expected value calculation: At confidence ~0.55 with transaction costs ~1-2%, expected edge ≈ 0.55×Gain - 0.45×Loss - costs ≈ negative or near-zero.
- **Exception:** If one lane is significantly more accurate than the other (e.g., NWP at 0.80, METAR at 0.72), the Bayesian posterior will tilt toward the stronger lane. In that case, trade if the posterior exceeds the calibrated threshold.

### Disagreement decision table:

```
                    NWP=UP    NWP=DOWN    NWP=SKIP
METAR=UP            TRADE     SKIP        TRADE
METAR=DOWN          SKIP      TRADE       TRADE
METAR=SKIP          TRADE     TRADE       SKIP
```

---

## 4. Calibration Independence

### Verdict: **Independently calibrate each lane first, then fuse.**

**Rationale:**
1. Each lane's conditional probability `P(truth | signal)` is a property of that lane's observation/forecast pipeline.
2. METAR errors come from sensor noise, rapid local weather changes, and observation timing.
3. NWP errors come from model drift, resolution limits, and forecast horizon.
4. These are fundamentally different error sources.

**Procedure:**

1. **Holdout period calibration** — On a 3-month holdout (disjoint from training), compute:
   - For METAR: `P(truth=UP | M=UP)` = fraction of M=UP calls where actual direction was UP
   - For NWP: `P(truth=UP | N=UP)` = fraction of N=UP calls where actual direction was UP
   - Same for DOWN and conditional on SKIP

2. **Convert to likelihood ratios:**
   ```
   LR_UP = P(M=UP | truth=UP) / P(M=UP | truth=DOWN) 
   ```
   (These are the calibrated LL values used in the log-odds formula.)

3. **Joint calibration check** — After fusion, validate that `P(truth | M, N)` is well-calibrated on the holdout. If the fused model is overconfident (predicted 90% but actual 80%), apply Platt scaling or temperature scaling to the fused logit.

### Why not jointly calibrate?
Joint calibration (learning `P(truth | M, N)` directly from 4 outcomes) is *possible* but statistically weak — the 4-cell joint distribution (M=UP/DOWN × N=UP/DOWN) has only 3 degrees of freedom, calibrated on limited data. Independent calibration uses 2+2=4 cells with more data per cell. Independent is more sample-efficient and more robust.

---

## 5. Signal Correlation — Preserving Independence

### The risk

The conditional independence assumption (`P(M,N | truth) = P(M|truth) * P(N|truth)`) is the lynchpin of our Bayesian fusion. If violated, the fusion model will be overconfident.

**Potential overlap sources:**

| Overlap Source | METAR Lane | NWP Lane | Risk |
|---|---|---|---|
| Calendar climatology | Seasonal normal for date | Seasonal component in forecast models | **HIGH** — both use seasonal priors |
| Pressure delta | Barometric trend | Inherits barometric initialization | **LOW** — METAR is local, NWP is grid-scale |
| Forecast disagreement | Ensemble spread at METAR site | NWP multi-model spread | **MEDIUM** — correlated through actual weather volatility |

### How to test for conditional independence

**Method: Conditional mutual information**

```
I(M; N | truth) = Σ P(m, n, truth) * ln( P(m,n|truth) / (P(m|truth)*P(n|truth)) )
```

If this is significantly > 0, conditional independence is violated.

**Practical implementation:**

1. On holdout data, compute the 2×2×2 contingency table (M=UP/DOWN × N=UP/DOWN × truth=UP/DOWN)
2. Compute the conditional mutual information
3. Compare against a bootstrap null distribution (shuffle one variable within each truth class)

### If correlation is found

- **Option A: Learn the joint distribution.** Instead of factorizing, learn `P(M,N | truth)` directly from the 4-cell table per truth class. This is the exact Bayesian solution and handles correlation. It needs more data (4 parameters per truth class instead of 2), but with 18+ months of data it's feasible.
- **Option B: Decorrelate pre-fusion.** Subtract the shared component (e.g., remove calendar_climatology from one lane before fusing).
- **Option C: Penalize agreement.** Reduce confidence when both lanes give the same signal, proportional to the measured correlation.

### Recommendation:
Run the conditional mutual information test first. If correlation is below 0.05 nats (information-theoretic significance threshold), assume independence and use the factorized model. If above, switch to Option A (learn joint distribution) — it's only 2 extra parameters.

### Calendar climatology fix (high-value specific recommendation)

The calendar_climatology signal from METAR lane likely overlaps with NWP seasonal model baselines. **Mitigation:** Replace raw calendar_climatology with the *residual* after removing the seasonal climatology that NWP also encodes. This is straightforward:

```
climatology_residual = METAR_calendar_signal - NWP_seasonal_baseline
```

This preserves the observation-specific deviation while removing the shared seasonal prior. Implement as a preprocessing step before any signal generation.

---

## 6. Implementation Complexity — MVP

### Minimal Viable Product (MVP)

**Complexity: ~200 lines of Python. 2-3 days of engineering time.**

### MVP components:

```
weather-engine-source/
├── fusion/
│   ├── __init__.py
│   ├── calibrator.py        # Per-lane calibration from holdout
│   ├── bayesian_fuser.py    # Log-odds fusion + thresholding
│   └── independence_test.py # Conditional mutual information check
└── tests/
    └── test_fusion.py
```

### Step-by-step implementation plan:

1. **Collect signal outputs** — Both METAR and NWP lanes emit a triplet per station-date: `(signal_direction: UP/DOWN/SKIP, raw_confidence: float)`.

2. **Holdout split** — Take the last 3 months of the 18-month dataset as calibration holdout. First 15 months for training each lane's base model.

3. **Calibrate each lane** — On holdout, compute `P(truth | signal)` for each direction. Store as JSON:
   ```json
   {
     "metar": {"up_ll": 0.944, "down_ll": 0.850, "skip_ll": 0.0},
     "nwp":  {"up_ll": 1.153, "down_ll": 1.020, "skip_ll": 0.0},
     "prior": {"up_logit": 0.201}
   }
   ```

4. **Fusion function:**
   ```python
   def fuse(metar_signal, nwp_signal, calibration):
       logit = calibration["prior"]["up_logit"]
       if metar_signal in ("UP", "DOWN"):
           logit += calibration["metar"][f"{metar_signal.lower()}_ll"]
       if nwp_signal in ("UP", "DOWN"):
           logit += calibration["nwp"][f"{nwp_signal.lower()}_ll"]
       p_up = 1 / (1 + math.exp(-logit))
       if p_up > THRESHOLD:
           return "UP", p_up
       elif p_up < 1 - THRESHOLD:
           return "DOWN", 1 - p_up
       else:
           return "SKIP", p_up
   ```

5. **Run independence test** — Compute conditional mutual information. If significant, fall back to joint distribution.

6. **Evaluate** — Compare MVP against:
   - Baseline: single-lane (METAR only, NWP only)
   - OR gate fusion
   - Bayesian fusion

### Acceptance criteria for MVP:
- Bayesian fusion achieves ≥5pp accuracy improvement over best single lane
- Disagreement skip rate < 30% (if higher, indicates lanes too decorrelated to fuse effectively)
- Fused model is approximately well-calibrated (Brier score improves over each lane individually)

### Post-MVP improvements:
- **Dynamic calibration** — Rolling window calibration (last 30 days) to adapt to seasonal changes in lane performance
- **Station-specific fusion** — Some stations may benefit from trusting METAR more (coastal, rapid weather changes), others from NWP (stable continental climates)
- **Confidence-weighted fusion** — Replace hard LL values with confidence-interval-weighted estimates

---

## 7. Accuracy-Improvement Suggestion

### Primary suggestion: **Climatology decorrelation pre-processing**

**Problem:** The METAR lane's `calendar_climatology` signal shares an informational substrate with NWP models' seasonal baselines. When both lanes call UP because "it's summer and summer tends to be hot," the signals are correlated through the truth, violating conditional independence and causing overconfidence.

**Solution:** Decompose each METAR climatology signal into:

```
METAR_climatology = NWP_seasonal_baseline(day_of_year) + residual
```

Use only the **residual** in the METAR signal fusion. This:
- Preserves the METAR observation advantage (local deviation from model baseline)
- Removes the correlated seasonal component that NWP already captures
- Improves independence → tighter Bayesian posteriors → better calibration

**Expected impact:** +2-3pp on fused accuracy, primarily through reduced false positives in marginal scenarios where both lanes independently react to the same seasonal pattern.

**Implementation cost:** One additional preprocessing function (~30 lines), no changes to fusion core.

---

## Summary Decision Table

| Question | Decision | Rationale |
|---|---|---|
| Architecture | **Bayesian fusion (log-odds)** | Principled, handles all cases, trivially implemented |
| OR gate vs. Bayes | **Bayes**; keep OR as baseline | OR discards information, Bayes doesn't |
| Both agree boost | **Log-odds addition** | Equivalent to Bayes, mathematically correct |
| Disagreement | **Skip trade** | Posterior ≈ prior, negative expected value after costs |
| Calibration | **Independently** calibrate lanes, then fuse | Sample-efficient, error sources are fundamentally different |
| Correlation | **Test via CMI**; use joint distribution if significant | Proactive measure; ~30 lines of code |
| MVP complexity | **~200 lines, 2-3 days** | Bayesian fusion in 50 lines, calibration in 80 lines, testing in rest |
| Accuracy suggestion | **Climatology residualization** | Removes correlated seasonal component; +2-3pp expected |
