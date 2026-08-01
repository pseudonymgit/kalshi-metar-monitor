# First-Principles Design: Multi-Signal Decision Fusion

**Date:** 2026-08-01
**Author:** Donna Paulsen (Chief of Staff)
**Status:** Design Document
**Scope:** Fusion methodology for all weather-engine signals — 82-member ensemble, WhaleWatch, Goldilocks, nowcasting, forecast aggregation
**Key Question:** How do we combine fundamentally different signal types into a single trading decision?

---

## 1. Problem Statement

We have five signal types, each operating at different timescales and predicting different things:

| Signal | What It Predicts | Timescale | Data Type |
|--------|-----------------|-----------|-----------|
| 82-member ensemble (GEFS+ECMWF) | Actual temperature at settlement | Daily (f024) | Weather model members |
| Goldilocks predictive | Settlement ≠ actual temp (sensor artifact) | Daily | METAR + stability features |
| WhaleWatch | Informed trader has conviction | Intraday (30s-30min) | Order book microstrucure |
| Intraday nowcasting | Current temperature (METAR) | Sub-daily (1h) | Live METAR observations |
| Forecast aggregation | Multi-model agreement | Daily (f000-f024) | 4 deterministic models |

**The fundamental problem:** These signals cannot be averaged, weighted, or combined at the same layer because they predict different things. Averaging a temperature prediction with a microstructure conviction score is mathematically meaningless.

**The design must answer:**
1. How does each signal type enter the decision pipeline?
2. At what layer does each signal operate?
3. What is the mathematical combination rule?
4. How does fee-netting apply at each layer?

---

## 2. Mathematical Framework

### 2.1 Core Decision Chain

Every trade decision decomposes into three questions:

```
Q1: What will the temperature be at settlement?
    → Answered by: 82-member ensemble (primary)
    → Confidence-modulated by: forecast aggregation (secondary)

Q2: Will the reported settlement temperature differ from the actual temperature?
    → Answered by: Goldilocks predictive
    → This shifts the *target bucket*, not the temperature

Q3: How much should we bet given this information?
    → Kelly criterion (primary)
    → Confidence-modulated by: WhaleWatch (conviction multiplier)
    → Adjusted by: fee structure, fill probability
```

### 2.2 Layer Decomposition

The decision pipeline has three independent layers:

```
Layer 1: Temperature Belief                          Layer 2: Settlement Belief
┌─────────────────────────────┐                     ┌────────────────────────┐
│ 82-m ensemble → P(T > B)   │                     │ P(settle | T, G)       │
│ ┌─────────┐ ┌───────────┐  │                     │                        │
│ │ GEFS 31 │ │ ECMWF 51  │  │                     │ If G=0: settle = T    │
│ │ members │ │ members   │  │                     │ If G=1: settle = T±ε  │
│ └─────────┘ └───────────┘  │                     │ ε ~ N(μ_ε, σ_ε)       │
│         ↓ member-pooling    │                     │ μ_ε = P(G)·mean_spike  │
│    ensemble_fraction(T>B)  │                     │ σ_ε = fcn(P(G), σ_G)  │
│         ↓                   │                     └────────┬───────────────┘
│    P_ensemble(T > B)       │                              │
│    + forecast_agg_conf     │                              ↓
│         ↓                   │                     ┌────────────────────────┐
│    P_corrected = P_ensemble │                     │ P(settle > B) =        │
│    × (1 + agreement_boost) │                     │   P(T > B - ε)         │
└─────────────────────────────┘                     │   ≈ P(T > B) + P(G)·ε │
                                                    └────────────────────────┘
                                                                  ↓
Layer 3: Bet Sizing
┌─────────────────────────────────────────────────────────────────┐
│ Edge = P(settle > B) - market_price                            │
│ Kelly_fraction = Edge / (1 - edge_cap) (approximate)           │
│ WhaleWatch → conviction_multiplier ∈ [0, w_max]                │
│   w_max = clamp(anomaly_score / threshold, 0, 1.5)             │
│                                                                 │
│ Actual_kelly = Kelly_fraction × conviction_multiplier × tier_discount │
│ Position = bankroll × Actual_kelly × fill_probability          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Layer 1 — Temperature Belief (82-Member Ensemble)

### 3.1 Member Pooling (Settled — Gray Room R12 Unanimous)

The 82-member ensemble is treated as a single pool of 82 independent temperature forecasts. No "model weighting" — each member gets one vote.

**Rationale:** Member-pooling is the correct approach when:
- Individual members are exchangeable (same type of forecast)
- We have no principled reason to prefer one member over another
- The ensemble size is large enough (>30) for empirical distribution estimation

**Implementation:**
```
T_82 = {GEFS_1, ..., GEFS_31, ECMWF_1, ..., ECMWF_51}
P(T > B) = count(T_i > B for T_i in T_82) / 82
```

This gives us an empirical probability that the temperature will exceed bucket B, assuming equal member weight.

**Edge calculation:**
```
edge_raw = P(T > B) - market_price
edge_net = edge_raw - fee(B, P(T > B), contract_count)
```

Where `fee(B, P, C) = ceil(0.07 × C × P × (1-P)) / C` per Kalshi's fee formula.

**Edge for NO (temperature below bucket):**
```
P(T < B) = 1 - P(T > B)
edge_NO_raw = (1 - P(T > B)) - price_NO
edge_NO_net = edge_NO_raw - fee(B, 1-P(T > B), C)
```

### 3.2 Bias Correction

Before pooling, each member's temperature forecast is bias-corrected per station:

```
T_i_corrected = T_i - bias(station, season, T_i)
```

The bias table is the existing 29,000-pair calibration. Bias = mean(forecast - observation) for each station×season bin, with a rolling 30-day update.

### 3.3 Forecast Aggregation Modulator

The forecast aggregation module (4 deterministic models: GFS, ECMWF IFS, ICON, GEM) provides an independent agreement score:

```
agreement = fraction of models agreeing with ensemble direction
confidence_boost = max(0, (agreement - 0.5) × 0.1)
P_corrected = P_ensemble + confidence_boost
```

If all 4 models agree with the ensemble direction, confidence gets a +0.05 boost (5 percentage points). If only 2 agree, no boost. If <2 agree, the ensemble is less trustworthy.

**Why this is a confidence modulator, not a separate signal:** The deterministic models predict the same thing as the ensemble (temperature at a point). Adding them as separate "votes" would double-count. Using them as agreement check is cleaner.

---

## 4. Layer 2 — Settlement Belief (Goldilocks Modulator)

### 4.1 The Goldilocks Adjustment

Goldilocks events cause the *reported* settlement temperature to differ from the *actual* temperature by a transient sensor artifact. This is NOT a weather prediction — it's a measurement artifact.

**The key insight:** Goldilocks shifts the *settlement target*, not the temperature distribution. If conditions favor a Goldilocks spike, the settlement will be higher than the actual temperature by ~1-3°F for ~9% of days.

### 4.2 Mathematical Formulation

Let:
- `T` = actual temperature (what the 82-member ensemble predicts)
- `S` = settlement temperature (what Kalshi pays out based on NWS CLI)
- `G` = Goldilocks event indicator (0 or 1, predicted by the Goldilocks ML model)
- `ε` = spike magnitude when Goldilocks occurs

```
S = T + G × ε

where:
P(G=1) = Goldilocks_model(features)
ε ~ dist(mean_spike, std_spike)  — from empirical validation
```

The probability that settlement exceeds bucket B:

```
P(S > B) = P(T > B) × P(G=0) + P(T > B - ε) × P(G=1)
         = P_ensemble × (1 - P_G) + P(T > B - E[ε]) × P_G
```

**Simplification for implementation:** Replace ε with E[ε] (mean spike magnitude ≈ 3.2°F from validation data):

```
P(S > B) ≈ P_ensemble × (1 - P_G) + P(T > B - 3.2) × P_G
```

### 4.3 When This Matters Most

Goldilocks adjustment has the largest impact when:
- `P_ensemble(T > B)` is near 0.5 (most sensitive to small shifts)
- `P_G` is high (conditions strongly favor a spike)
- The spike magnitude pushes the ensemble probability from 0.4→0.6 or similar

**Example:** If ensemble says 45% chance of exceeding bucket, and Goldilocks says 20% chance of a +3.2°F spike:
```
P(S > B) = 0.45 × 0.8 + P(T > B - 3.2) × 0.2
```
If the spike pushes P(T > B - 3.2) to ~0.7:
```
P(S > B) = 0.45 × 0.8 + 0.7 × 0.2 = 0.36 + 0.14 = 0.50
```
The edge changes from -0.05 (if market is at 0.50) to 0.00 — small but meaningful at the margin.

### 4.4 LOW Market Symmetry

For LOW markets (temperature below bucket), Goldilocks LOW events cause a downward spike:
```
S = T - G_low × ε_low
P(S < B) = P(T < B) × (1 - P_G_low) + P(T < B + E[ε_low]) × P_G_low
```

---

## 5. Layer 3 — Bet Sizing (WhaleWatch + Kelly)

### 5.1 Kelly Criterion (Base)

The Kelly criterion determines the optimal fraction of bankroll to bet given an edge:

```
f* = edge / (P_win × odds - edge)    — simplified Kelly for binary bets
or
f* = (P_win × (odds - 1) - (1 - P_win)) / (odds - 1)   — full Kelly
```

For Kalshi binary options:
```
odds = (1 - entry_price) / entry_price  (for YES)
edge = P(S > B) - market_price
f* = (P_win × (1/entry_price - 1) - (1 - P_win)) / (1/entry_price - 1)
```

With fee adjustment (the fee consumes part of the edge):
```
effective_entry_price = entry_price + fee_per_contract_per_side
effective_edge = P_win × (1/effective_entry_price - 1) - (1 - P_win)
```

**Constraint:** Kelly is capped at 25% of bankroll per trade (quarter-Kelly for volatility reduction).

### 5.2 WhaleWatch Conviction Multiplier

WhaleWatch detects order book anomalies that indicate informed trading. This provides an independent signal that *someone else has conviction* in a particular outcome. It does not tell us *what the outcome is*, only that someone with presumably better data is acting.

**The math:** WhaleWatch is a multiplier on the Kelly fraction, not on the probability:

```
conviction_multiplier = 1.0  (no anomaly — default)

If SUSPECTED:  conviction_multiplier = 1.1
If DETECTED:   conviction_multiplier = 1.25
If HIGH_CONVICTION:  conviction_multiplier = 1.5

f_adjusted = f* × conviction_multiplier × tier_discount
```

**Why multiplier, not probability adjustment:**
- WhaleWatch detects *conviction*, not *temperature*. It says "someone is confident," not "the temperature will be X."
- A multiplier on sizing correctly captures: "I'm more confident in my estimate" → "I should bet more"
- If WhaleWatch detects an anomaly in the *opposite* direction from our ensemble, it's a contradiction signal:
  ```
  If whale_direction ≠ ensemble_direction:
      conviction_multiplier = 1 - 0.5 × anomaly_score
  ```

**Limits:** The conviction multiplier is capped at 1.5 (never bet more than 50% extra due to WhaleWatch alone) and floored at 0.2 (never reduce to zero — the ensemble is still a valid signal even if WhaleWatch contradicts).

### 5.3 Tier Liquidity Discount

From the station liquidity tiers:

| Tier | Stations | Discount | Rationale |
|:----:|:---------|:--------:|:----------|
| T1 | KNYC, KORD, KLAX, KDFW, KATL, KBOS, KPHL, KDCA | 0% | High volume, tight spreads |
| T2 | KSEA, KMIA, KDEN, KMDW, KPHX, KHOU | 25% | Moderate volume, wider spreads |
| T3 | KSFO, KLAS, KMSP, KMSY, KOKC, KSAT, KAUS | 50% | Low volume, execution risk |

```
f_adjusted = f* × conviction_multiplier × (1 - tier_discount)
```

### 5.4 Fill Model Discount

Not all orders fill at our target price. The fill probability adjusts the final position:

```
fill_probability = exp(-spread / median_spread × 0.5)    — heuristic
position = bankroll × f_adjusted × spread_model_factor
```

When spread is wide, fewer orders fill at limit price, so reduce position. When spread is tight, fill probability is high.

---

## 6. Nowcasting + Forecast Aggregation (Bayesian Prior)

### 6.1 Where They Enter the Pipeline

These signals enter at Layer 1 as a Bayesian update on the temperature belief, not as separate layers.

### 6.2 Nowcasting (METAR Live Data)

Intraday nowcasting provides current temperature from METAR observations (updated every 1-3 hours). This is most useful in the final 6 hours before settlement, when the daily max/min is likely already observed.

**Bayesian formulation:**

```
P(T > B | nowcast) updates P(T > B | ensemble) as follows:

If current_temp already exceeds B:
    P(T > B) ≈ 1.0 (settlement max cannot be below current temp)

If current_temp is far from B and settlement is close:
    P(T > B | ensemble) dominates (ensemble knows the overnight forecast)

If current_temp is near B and conditions are stable:
    P(T > B | nowcast) ≈ indicator(current_temp > B)
```

**Implementation:** The nowcasting signal is a binary gate in the final 6 hours:
```
If hours_to_settlement < 6 and abs(bucket - current_temp) < 2:
    P(T > B) = 0.9 if current_temp > bucket else 0.1
Else:
    P(T > B) = P_ensemble  (no override)
```

### 6.3 Forecast Aggregation (Agreement Score)

As specified in §3.3, the forecast aggregation agreement score is a confidence modulator on the ensemble probability. It applies at all times, not just near settlement:

```
agreement = fraction of (GFS, ECMWF IFS, ICON, GEM) agreeing with ensemble direction
If agreement == 4:     P_corrected = min(P_ensemble + 0.05, 1.0)
If agreement == 3:     P_corrected = min(P_ensemble + 0.025, 1.0)
If agreement == 2:     P_corrected = P_ensemble (no change)
If agreement < 2:      P_corrected = max(P_ensemble - 0.025, 0.0)
```

---

## 7. Complete Decision Pipeline

```
                        ┌──────────────────────────┐
                        │ GEFS 31 + ECMWF 51       │
                        │ (82 members)              │
                        └─────┬────────────────────┘
                              │ bias correction
                              ↓
                        ┌──────────────────────────┐
                        │ Member-pooling            │
                        │ P(T > B) = fraction       │
                        └─────┬────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ↓               ↓               ↓
    ┌─────────────────┐ ┌──────────┐ ┌──────────────┐
    │ Forecast agg    │ │Nowcast   │ │Goldilocks    │
    │ (confidence     │ │(final 6h │ │(settlement   │
    │  boost)         │ │ override)│ │  shift)      │
    └────────┬────────┘ └────┬─────┘ └──────┬───────┘
              │               │               │
              └───────┬───────┘               │
                      ↓                       │
              ┌──────────────┐                │
              │ P_corrected  │◄───────────────┘
              └──────┬───────┘
                     │
                     ↓  edge = P_corrected - market_price - fee
              ┌──────────────┐
              │ Kelly        │◄── WhaleWatch (conviction multiplier)
              │ Sizing       │◄── Tier discount
              └──────┬───────┘
                     │
                     ↓
              ┌──────────────┐
              │ Position     │
              │ (bankroll ×  │
              │  fraction)   │
              └──────────────┘
```

### 7.1 Gate: Pre-Trade Checks

Before ANY trade executes:

1. **GEFS integrity check** — All 31 members present? ECMWF 51 present? If not, no trade.
2. **Bias correction check** — Is the bias correction for this station×season populated? If not, no trade.
3. **Minimum agreement check** — Configurable gate: require ≥3 signals to agree on direction.
   - 82-member ensemble counts as 1 vote
   - Goldilocks counts as 0.5 vote (weak — only ~7% of days)
   - WhaleWatch counts as 0.5 vote (weak — market structure, not weather)
   - Nowcasting counts as 0.25 vote (only active in final 6h)
   - Forecast aggregation counts as 0.25 vote (agreement metric, not independent)
4. **Fee gate** — `edge_net < $0.02` → skip (below fee death zone threshold)
5. **Time gate** — `hours_to_settlement < 1` → skip (too late for fill)
6. **Bucket gate** — market_price < $0.15 or > $0.85 → skip (fee death zone)

---

## 8. Open Questions & Design Decisions

### 8.1 Weighting Sensitivity

The 82-member pooling assumes all members are independent and equally accurate. In reality:
- GEFS and ECMWF have different spatial resolutions (~25km vs ~9km)
- ECMWF is generally more accurate for mid-latitude temperature
- But ECMWF has fewer members (51 vs 31 pooled, so 62% of the pool)

**Question:** Should we debias by model performance? E.g., weight ECMWF members 1.2× and GEFS 1.0× based on historical accuracy?

**Recommendation (R12):** Start with equal pooling. Measure GEFS-ECMWF correlation after ECMWF completes. If correlated >0.85, member-pooling is fine. If <0.85, test per-model weighting in Phase B walk-forward.

### 8.2 Goldilocks Edge Threshold

Goldilocks only adds value when P(G) > some minimum. Below this threshold, the adjustment is noise:

**Recommendation:** Gate Goldilocks at P(G) > 0.15 (roughly 2× the base rate of 7.4%). Below this, skip the settlement adjustment.

### 8.3 WhaleWatch Direction

WhaleWatch detects anomalies but not their direction. If a whale accumulates YES contracts, our ensemble might say NO. What happens?

**Recommendation:** Contradiction reduces conviction — we don't believe the whale more than our ensemble. Only amplify when whale direction matches ensemble direction:
```
If sign(whale_bias) == sign(ensemble_edge):
    conviction_multiplier = anomaly_score / threshold
Else:
    conviction_multiplier = max(0.2, 1 - 0.5 × anomaly_score)
```

### 8.4 Model Refresh Cadence

| Signal | Refresh | Notes |
|--------|---------|-------|
| 82-member ensemble | Daily (f024) | Both GEFS and ECMWF updated once daily |
| Goldilocks model | Monthly | Retrain with new data. Low event rate means slow drift |
| Bias correction | Daily (rolling 30d) | Per-station, per-season |
| WhaleWatch | Intraday (30s) | Live anomaly detection |
| Nowcasting | Hourly | On each METAR observation |
| Forecast aggregation | Daily | On each model run |

---

## 9. Testing & Validation Strategy

### 9.1 Backtest Requirements

Each layer must be validated independently before the pipeline can be trusted:

| Layer | Validation | Metric | Gate |
|:-----|:-----------|:------:|:----:|
| 82-member pooling | Directional accuracy vs Kalshi settlement | >58% | Hard gate from Gray Room |
| + Bias correction | Accuracy improvement with vs without | >+1pp | Soft |
| + Forecast agg | Agreement improves Sharpe | >+0.1 | Soft |
| + Goldilocks | P&L improvement with adjustment | >+5% | Soft |
| + WhaleWatch | P&L improvement with multiplier | >+5% | Soft |
| + Kelly sizing | Sharpe improvement vs equal-weight | >0 | Hard |
| + Tier discount | Reduces worst-case drawdown | - | Monitor |

### 9.2 Walk-Forward Validation

All signal additions must be validated with 3-stage walk-forward:

1. **Discovery:** Train on 2021-01-01 to 2024-12-31 → optimize parameters
2. **Temporal holdout:** Test on 2025-01-01 to 2025-06-30 → validate no overfitting
3. **Geographic holdout:** Test on held-out stations → validate no station-specific artifact

### 9.3 When to Wire Into Trading

Wiring into the paper trading loop happens ONLY when:
1. All layers are independently validated against settlement data
2. The 82-member sweep shows positive net edge after ALL fees
3. Walk-forward validation passes at all 3 stages
4. A minimum of 500 out-of-sample trades exist
5. Sharpe > 0.3 on the holdout test set

---

## 10. Summary of Decisions

| Decision | Recommendation | Source |
|:---------|:---------------|:------:|
| How to combine signals | Bayesian cascade at different layers (not additive) | This doc |
| 82-member weighting | Equal member-pooling, test per-model weighting | R12, R9 |
| Goldilocks entry | Settlement shift at Layer 2, not temperature at Layer 1 | This doc |
| WhaleWatch entry | Conviction multiplier at Layer 3 (Kelly), not probability adjustment | This doc |
| Nowcasting entry | Bayesian prior at Layer 1, only in final 6h | This doc |
| Forecast agg entry | Confidence boost at Layer 1, as agreement check | This doc |
| Goldilocks gate | P(G) > 0.15 required for adjustment | This doc |
| WhaleWatch contradiction | Reduce conviction, don't flip direction | This doc |
| Fee treatment | Per-contract, per-side, at edge calculation | R12 |
| Minimum trade gate | conf≥0.6, agree≥3, edge>2¢, price 0.15-0.85 | Phase 2 sweep best |
| Pre-wiring gate | 500 OOS trades, Sharpe > 0.3, 3-stage walk-forward | This doc |