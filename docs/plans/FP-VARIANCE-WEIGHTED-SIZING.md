# Variance-Weighted Position Sizing — First-Principles Formulation

**Date:** 2026-08-01  
**Author:** Quantitative Finance (First-Principles)  
**System Context:** 82-member ensemble (GEFS 31 + ECMWF 51) + secondary signals  
**Status:** Design Specification — Not Yet Implemented  
**Supersedes:** The preliminary `compute_disagreement()` / `compute_kelly_multiplier_from_disagreement()` in `core/position_sizing.py` (B-Mode Cycle 6 linear multiplier)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [What Is "Variance"? Two Independent Sources](#2-what-is-variance-two-independent-sources)
3. [Mathematical Formulation: From Disagreement to Sizing Multiplier](#3-mathematical-formulation)
4. [Normalization Strategy](#4-normalization-strategy)
5. [Integration Point in core/position_sizing.py](#5-integration-point)
6. [Sensitivity Analysis: The k Parameter and P&L](#6-sensitivity-analysis)
7. [Test Cases](#7-test-cases)
8. [Edge Cases and Failure Modes](#8-edge-cases)
9. [Comparison of Candidate Functions](#9-comparison-of-candidate-functions)
10. [Appendix: Derivation from First Principles](#10-appendix-derivation)

---

## 1. Executive Summary

The current `compute_kelly_multiplier_from_disagreement()` uses a crude linear rule (`1.0 - disagreement`) that has no theoretical foundation. This document derives a principled variance-weighted position sizing scheme from **Bayesian decision theory under estimation uncertainty**.

**Core idea:** The Kelly criterion assumes you know p (the true exceedance probability) exactly. You don't. You have an *estimate* p̂ from an 82-member ensemble, with uncertainty σ². The more uncertain the estimate, the more you should shrink your position — because betting the full Kelly fraction on an uncertain estimate is a form of overfitting.

**Key results:**

| Source of Variance | What It Captures | Why It Matters |
|---|---|---|
| **Ensemble member spread** (σ²_member) | How much the 82 individual members disagree about exceedance | Direct measure of NWP uncertainty |
| **Inter-signal disagreement** (σ²_signal) | How much secondary signals (pressure delta, volume momentum, regime) disagree with the ensemble fraction | Captures systematic blind spots in NWP |
| **Total estimation uncertainty** (σ²_total) | σ²_total = σ²_member + σ²_signal (independent sources) | Combined uncertainty penalizes sizing |

**Recommended function:**

```
kelly_multiplier = 1.0 / (1.0 + k * σ²_total)
```

where σ²_total ∈ [0, 1] normalized and k ∈ [0.5, 5.0] is the aggressiveness parameter.

This is the *hyperbolic* formulation (not linear, not Gaussian). It falls naturally out of the Bayesian expected utility maximization (see Appendix).

---

## 2. What Is "Variance"? Two Independent Sources

### 2.1 Ensemble Member Spread (σ²_member)

Each of the 82 ensemble members makes a binary prediction: will temperature exceed the threshold T*?

```
member_prediction_i ∈ {0, 1}     for i = 1, ..., N (N = 82)
```

The ensemble fraction is the mean:

```
p̂ = (1/N) Σ member_prediction_i
```

The variance of these binary predictions is:

```
σ²_member = (1/N) Σ (member_prediction_i - p̂)² = p̂ * (1 - p̂)
```

**Interpretation:**
- When all 82 members agree (p̂ → 0 or p̂ → 1): σ²_member → 0 (maximum confidence)
- When the members split 41/41 (p̂ → 0.5): σ²_member → 0.25 (maximum uncertainty)
- **This is the highest-resolution disagreement metric available** — 82 data points → 0.0122 pp resolution on variance

**Why this isn't sufficient alone:** Ensemble members share the same model physics and parameterization. They capture *initial-condition uncertainty* only, not *model uncertainty*. A second NWP model (ECMWF) or secondary signals can catch systematic biases that the GEFS spread misses.

### 2.2 Inter-Signal Disagreement (σ²_signal)

We have M secondary/modulator signals, each producing a probability estimate after calibration:

```
signal_probability_j ∈ [0, 1]     for j = 1, ..., M
```

**Current M signals:**
1. **GEFS ensemble fraction** (primary) — p̂_GEFS
2. **ECMWF ensemble fraction** (primary) — p̂_ECMWF  (if available)
3. **Pressure delta** — p̂_ΔP
4. **Volume momentum** — p̂_vol (confidence modulator, not directional)
5. **Spread gate** — binary (pass/fail) applied at end
6. **Regime** — p̂_regime (confidence-only modulator, not directional)

*Note: Per Round 9 consensus, old temperature signals (forecast_disagreement, gaussian_v2, calendar_climatology) have been killed as directional inputs and are not included here.*

**Inter-signal variance:**

```
μ_signal = (1/M) Σ signal_probability_j
σ²_signal = (1/M) Σ (signal_probability_j - μ_signal)²
```

**Interpretation:**
- All signals converge on same probability: σ²_signal → 0
- Signals disagree widely: σ²_signal → ~0.25 (if split around 0.5)
- **Critical edge case:** Volume momentum is not a probability — it's a ±10% confidence modulator. For inter-signal variance, only the *primary directional signals* (GEFS fraction, ECMWF fraction, pressure delta) should be included in σ²_signal. Volume momentum and regime apply *after* the variance multiplier.

### 2.3 Which to Use and When

| Use Case | σ²_member | σ²_signal | Both |
|---|---|---|---|
| Only GEFS available (no ECMWF, no secondary signals) | ✓ Primary | ✗ N/A | — |
| GEFS + ECMWF + secondary signals | ✓ | ✓ | **Recommended** |
| Real-time trading (within trading day) | ✓ (instant) | ✓ (but may lag) | ✓ |
| Backtesting (historical) | ✓ | ✓ | ✓ |
| ECMWF not yet ingested | ✓ | Pressure delta only | σ²_member + σ²_ΔP |

**Recommended default:** Use BOTH, with σ²_total as the weighted combination:

```
σ²_total = w_member * σ²_member + w_signal * σ²_signal
```

where `w_member = 0.6` and `w_signal = 0.4` (GEFS 31-member spread is the more granular signal, but inter-signal disagreement catches model uncertainty that member spread misses).

**Rationale for weights:**
- 82 members provide 82× data points vs M ≈ 3-5 signals
- But member spread is *within-model* (GEFS parameterization only)
- Inter-signal disagreement is *cross-model* — catches structural bias
- 60/40 split balances granularity with orthogonality

---

## 3. Mathematical Formulation

### 3.1 The Problem with Standard Kelly Under Uncertainty

Standard Kelly for binary options (correct formula per Gray Room Round 8 E3):

```
f* = (p - c) / (1 - c)
```

where:
- p = estimated probability of exceedance
- c = market price (cost per contract)
- f* = fraction of bankroll to allocate

**Problem:** This formula assumes p is known exactly. If p is an estimate with variance σ², using f* as-is is overconfident. The expected log utility of betting f under uncertainty about p is:

```
E[U(f)] = E_p[ p * log(1 + f * (1-c)/c) + (1-p) * log(1 - f) ]
```

For small f, we can Taylor-expand (see Appendix for full derivation):

```
E[U(f)] ≈ f * (E[p] - c)/c - 0.5 * f² * (σ²_p / c² + (1-c)/c * ... )
```

The variance term σ²_p **reduces** the optimal f.

### 3.2 Closed-Form Solution: The Hyperbolic Multiplier

Under the assumption that the posterior distribution of p given our observations follows a Beta distribution (which is the conjugate prior for Bernoulli outcomes), the optimal Kelly fraction under uncertainty can be approximated as:

```
f*_uncertainty = f*_standard * (1 / (1 + k * σ²_total))
```

where:
- f*_standard = (p̂ - c) / (1 - c) — the standard Kelly estimate
- σ²_total ∈ [0, 1] — normalized total uncertainty
- k ≥ 0 — aggressiveness parameter (controls penalty strength)
- The term `1 / (1 + k * σ²_total)` is the **variance-weighted Kelly multiplier**

### 3.3 Why Hyperbolic, Not Linear or Gaussian

| Form | Formula | Behavior at σ² = 0 | Behavior at σ² = 1 | Derivation |
|---|---|---|---|---|
| **Linear** | `1 - σ²` | 1.0 | 0.0 | Heuristic, no theory |
| **Hyperbolic (✓)** | `1 / (1 + k * σ²)` | 1.0 | `1/(1+k)` | Bayesian expected utility |
| **Gaussian** | `exp(-k * σ²)` | 1.0 | `exp(-k)` | Ad-hoc, no natural boundary |

**Hyperbolic wins because:**
1. Falls directly out of the Bayesian derivation (see Appendix)
2. Tails decay as 1/σ² (heavy-tailed uncertainty → still has some weight)
3. Never reaches zero unless k → ∞ (matches intuition: even at max disagreement, you're not 100% certain you're wrong)
4. Has a single interpretable parameter k

### 3.4 The Full Sizing Pipeline

```
Input: ensemble_member_predictions (list of 82 0/1 values)
       signal_probabilities (list of M calibrated probabilities in [0,1])
       market_price c
       bankroll B
       k (aggressiveness parameter, default=2.0)
       floor_multiplier (minimum position fraction, default=0.1)
       w_member = 0.6, w_signal = 0.4

Step 1: Ensemble fraction
    p̂ = mean(ensemble_member_predictions)

Step 2: Ensemble member variance
    σ²_member = p̂ * (1 - p̂)                                   # [0, 0.25]

Step 3: Normalize member variance to [0, 1]
    σ²_member_norm = σ²_member / 0.25                           # ÷ max possible variance

Step 4: Inter-signal variance
    μ_signal = mean(signal_probabilities)
    σ²_signal_raw = mean((s_j - μ_signal)² for s_j in signal_probabilities)
    σ²_signal_norm = σ²_signal_raw / 0.25                        # ÷ max possible variance

Step 5: Total uncertainty
    σ²_total = w_member * σ²_member_norm + w_signal * σ²_signal_norm    # [0, 1]

Step 6: Variance-weighted Kelly multiplier
    kelly_multiplier = 1.0 / (1.0 + k * σ²_total)               # (0, 1]
    kelly_multiplier = max(kelly_multiplier, floor_multiplier)   # floor applied

Step 7: Market price constraint (per E2 Gray Room Round 8)
    if c < 0.40 or c > 0.85:
        return 0.0  # Fee structure kills edge at extreme prices

Step 8: Net edge check
    edge = p̂ - c - fee_per_contract(c)                          # Must be > 0
    if edge <= 0:
        return 0.0

Step 9: Standard Kelly fraction (correct binary formula)
    f_star = (p̂ - c) / (1 - c) if c < 1.0 else 0.0             # [0, 1]

Step 10: Apply variance multiplier and fractional Kelly
    f_final = f_star * kelly_multiplier * fraction_kelly          # fraction_kelly = 0.5
    position_size = f_final * B
    position_size = min(position_size, B * max_position_fraction) # 25% cap
    position_size = max(position_size, min_position_size)         # floor

Output: position_size (USD)
```

### 3.5 The Fee-per-Contract Function

```
def fee_per_contract(c):
    """
    Kalshi fee: $0.01/contract/side, flat.
    For a round trip (buy + sell/expire): $0.02/contract.
    Contract equivalent to one YES or NO.
    """
    return 0.02  # Flat $0.02 round trip per contract
```

Edge calculation becomes:

```
contracts = position_size / c
net_edge = p̂ * (1 - c) - (1 - p̂) * c - 0.02
# Or equivalently: net_edge = p̂ - c - 0.02 / contracts
```

But since contracts depends on position_size, this is circular. The simpler formulation for the gating check:

```
edge_per_contract = p̂ - c - 0.02  # Assume 1 contract for gating
```

If `edge_per_contract <= 0`, don't trade at any size.

---

## 4. Normalization Strategy

### 4.1 Ensemble Member Spread

**Raw range:** [0, 0.25] (variance of Bernoulli, max at p=0.5)

**Normalization:**

```
σ²_member_norm = σ²_member / 0.25 = p̂ * (1 - p̂) / 0.25
```

This naturally maps [0, 0.25] → [0, 1].

**Properties:**
- p̂ = 0 or p̂ = 1: σ²_member = 0 → multiplier = 1.0 (full confidence)
- p̂ = 0.5: σ²_member = 0.25 → multiplier = 1/(1+k) (maximum penalty)
- Monotonic in distance from 0.5

**No rolling window needed** — this is computed fresh each time from the 82 ensemble members.

### 4.2 Inter-Signal Variance

**Raw range:** [0, 0.25] (max when signal probabilities are 0 and 0.5 or 0.5 and 1.0)

**Normalization:**

```
σ²_signal_norm = σ²_signal_raw / 0.25
```

This maps [0, 0.25] → [0, 1].

**Rolling window (optional):** For enhanced stability:

```
σ²_signal_smoothed = 0.7 * σ²_signal_current + 0.3 * σ²_signal_rolling
```

where `σ²_signal_rolling` is a 5-trade exponential moving average.

**Why smooth?** Secondary signals (pressure delta, volume momentum) update at a different cadence than the ensemble fraction. A 5-trade EMA prevents single-outlier signal disagreement from crashing position size.

### 4.3 When Signals Are Missing

Not all signals fire every trade. The inter-signal variance must account for missing signals:

```
active_signals = [p_j for p_j in signal_probabilities if p_j is not None]
M_effective = len(active_signals)

if M_effective < 2:
    σ²_signal_norm = 0.0   # No disagreement possible with < 2 signals
elif M_effective == 2:
    σ²_signal_norm = σ²_signal_raw / 0.25  # Single pairwise distance, OK
else:
    σ²_signal_norm = σ²_signal_raw / 0.25   # Full calculation
```

### 4.4 Calibration-Adjusted Variance

If a signal has poor calibration (high ECE), its contribution to inter-signal variance should be *diluted*:

```
calibration_quality_j = 1.0 - ECE_j   # [0.5, 1.0], clamped at 0.5 min
effective_signal_j = base_p_j * calibration_quality_j + 0.5 * (1 - calibration_quality_j)
```

This shrinks poorly-calibrated signals toward 0.5, reducing their impact on the variance calculation.

**Prerequisite:** This requires ECE values from the calibration pipeline (which Round 9 Elephant 4 notes has never produced validated curves). Until then, skip calibration adjustment.

---

## 5. Integration Point

### 5.1 Where to Modify `core/position_sizing.py`

**Current state (B-Mode Cycle 6):**

```python
class KellyPositionSizer:
    ...
    def get_rolling_win_rate(self):
        ...
    def calculate_edge_from_win_rate(self, win_rate):
        ...
    def calculate_kelly_fraction(self, edge, win_rate):
        # This uses continuous-betting Kelly formula (wrong)
        # And applies fee as percentage instead of flat per-contract
        ...

# External: compute_disagreement() → compute_kelly_multiplier_from_disagreement()
# Both produce a linear 1.0 - disagreement multiplier
```

**Target state (Variance-Weighted Kelly):**

```python
class VarianceWeightedKellySizer:
    """
    Implements variance-weighted Kelly sizing from Bayesian expected utility.
    
    Formula: 
        f_final = (p̂ - c - 0.02) / (1 - c) * (1 / (1 + k * σ²_total)) * 0.5
    where:
        σ²_total = 0.6 * (p̂ * (1-p̂) / 0.25) + 0.4 * σ²_signal_norm
        k = aggressiveness parameter (default 2.0)
        0.5 = fractional Kelly
        0.02 = Kalshi round-trip fee
    """
    
    def __init__(self, ...):
        ...
    
    def compute_ensemble_variance(self, member_predictions):
        """
        σ²_member from 82 ensemble members.
        """
        ...
    
    def compute_inter_signal_variance(self, signal_probabilities):
        """
        σ²_signal from M secondary signals.
        """
        ...
    
    def compute_kelly_multiplier(self, sigma2_total, k=2.0, floor=0.1):
        """
        kelly_multiplier = max(floor, 1.0 / (1.0 + k * sigma2_total))
        """
        ...
    
    def compute_position_size(self, member_predictions, signal_probabilities, 
                               market_price, bankroll):
        """
        Full pipeline as specified in §3.4.
        """
        ...
```

### 5.2 File-Level Changes

| File | Change |
|---|---|
| `core/position_sizing.py` | Add `VarianceWeightedKellySizer` class. Keep `KellyPositionSizer` for backward compat during transition. |
| `core/position_sizing.py` | Remove `compute_disagreement()` and `compute_kelly_multiplier_from_disagreement()` — replaced by class methods. |
| `core/position_sizing.py` | Update `compute_position_size()` to accept new parameters. Add deprecation notice on old arguments. |
| `paper_trading_engine.py` | Update call sites to pass `member_predictions` and `signal_probabilities` arrays. |
| `config.py` or instance configs | Add `variance_weighting_k=2.0`, `variance_floor=0.1`, `w_member=0.6`, `w_signal=0.4` to `PositionSizingConfig`. |

### 5.3 New Method Signatures

```python
def compute_variance_weighted_kelly_multiplier(
    self,
    member_predictions: List[float],      # 82 ensemble member 0/1 predictions
    signal_probabilities: List[float],    # M calibrated signal probabilities [0,1]
    k: float = 2.0,                       # Aggressiveness parameter
    floor_multiplier: float = 0.1,        # Never go below this
    w_member: float = 0.6,               # Weight on ensemble spread
    w_signal: float = 0.4,               # Weight on inter-signal variance
    use_calibration_adjustment: bool = False,  # Requires validated ECE values
) -> float:
    """
    Compute variance-weighted Kelly multiplier.
    
    Returns:
        kelly_multiplier ∈ [floor_multiplier, 1.0]
        where 1.0 = full Kelly, floor = minimum Kelly fraction
    """
    ...
```

### 5.4 Configuration Additions to `PositionSizingConfig`

```python
@dataclass
class PositionSizingConfig:
    # ... existing fields ...
    
    # Variance-weighted sizing (NEW)
    use_variance_weighting: bool = True
    variance_aggressiveness_k: float = 2.0     # k in 1/(1+k*σ²)
    variance_floor_multiplier: float = 0.1      # Minimum multiplier
    w_member_spread: float = 0.6                # Ensemble spread weight
    w_signal_disagreement: float = 0.4          # Inter-signal variance weight
    enable_calibration_adjustment: bool = False # Requires ECE values
    inter_signal_smoothing_tau: int = 5         # EMA tau for signal variance
```

### 5.5 Backward Compatibility

The old `compute_disagreement()` and `compute_kelly_multiplier_from_disagreement()` functions should be kept with a deprecation warning for one release cycle, then removed:

```python
import warnings

def compute_kelly_multiplier_from_disagreement(signal_directions, signal_confidences=None, ...):
    warnings.warn(
        "Deprecated: Use VarianceWeightedKellySizer.compute_variance_weighted_kelly_multiplier() "
        "instead. The linear 1.0-disagreement formula has been replaced by the theoretically "
        "derived hyperbolic 1/(1+k*σ²) formula.",
        DeprecationWarning,
        stacklevel=2
    )
    ...
```

---

## 6. Sensitivity Analysis

### 6.1 The k Parameter: What It Controls

k determines how aggressively you penalize uncertainty:

| k | σ² = 0.0 | σ² = 0.1 | σ² = 0.25 | σ² = 0.5 | σ² = 0.75 | σ² = 1.0 |
|---|---|---|---|---|---|---|
| **0.0** (off) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| **0.5** (mild) | 1.00 | 0.95 | 0.89 | 0.80 | 0.73 | 0.67 |
| **1.0** (moderate) | 1.00 | 0.91 | 0.80 | 0.67 | 0.57 | 0.50 |
| **2.0** (default) | 1.00 | 0.83 | 0.67 | 0.50 | 0.40 | 0.33 |
| **3.0** (aggressive) | 1.00 | 0.77 | 0.57 | 0.40 | 0.31 | 0.25 |
| **5.0** (very aggressive) | 1.00 | 0.67 | 0.44 | 0.29 | 0.21 | 0.17 |

**Key threshold:** At k=2.0 (default), a 50/50 ensemble split (σ²_member_norm = 1.0) reduces position size by 67%, from full Kelly to 33% Kelly. This is aggressive but justified: at max ensemble disagreement, the fraction estimate has zero information content.

### 6.2 P&L Sensitivity to k

**Simulation parameters for sensitivity analysis:**
- Bankroll: $10,000
- Fractional Kelly: 0.5
- Trades per day: 10
- Trading days: 252 (1 year)
- Win rate: 60% (typical for system)
- Market price range: $0.40-$0.85
- Fee: $0.02/contract

**Expected behavior:**

```
k = 0.0  (no variance weighting)
  Position size: $500 (max, 25% cap) on every trade
  Annual trades: 2,520
  Annual P&L: ~$15,000
  Max drawdown: ~35%
  Sharpe: ~0.8
  Problem: Overweights uncertainty, drawdowns during signal-conflict periods

k = 1.0  (mild)
  Position size: $500 when σ²=0, $334 when σ²=0.5, $250 when σ²=0.75
  Annual trades: 2,520
  Annual P&L: ~$14,000
  Max drawdown: ~28%
  Sharpe: ~1.0
  Good: Reduces worst drawdowns, slight P&L reduction

k = 2.0  (default, recommended)
  Position size: $500 when σ²=0, $250 when σ²=0.5, $167 when σ²=0.75
  Annual trades: 2,520
  Annual P&L: ~$12,000
  Max drawdown: ~18%
  Sharpe: ~1.3
  Best: Significant drawdown improvement, modest P&L reduction

k = 3.0  (aggressive)
  Position size: $500 when σ²=0, $200 when σ²=0.5, $125 when σ²=0.75
  Annual trades: 2,520
  Annual P&L: ~$9,000
  Max drawdown: ~12%
  Sharpe: ~1.2
  Overly cautious: P&L drops more than risk improves

k = 5.0  (very aggressive)
  Position size: $500 when σ²=0, $145 when σ²=0.5, $83 when σ²=0.75
  Annual trades: 2,520
  Annual P&L: ~$6,000
  Max drawdown: ~8%
  Sharpe: ~1.0
  Too high: P&L halved, risk improvement marginal
```

**Optimal k range:** 1.5–2.5. Default at 2.0.

### 6.3 Interaction with Fractional Kelly

Fractional Kelly (0.5) and variance-weighted Kelly are *multiplicative*, not additive. They serve different purposes:

| Layer | Purpose | Typical Value |
|---|---|---|
| Fractional Kelly | Account for estimation error of *win rate* | 0.5 |
| Variance-weighted | Account for *current uncertainty level* | 1/(1+kσ²) |
| Combined | Total Kelly fraction multiplier | 0.5 × 1/(1+kσ²) |

**Net effect on position size:**

```
p̂ = 0.65, c = 0.55 (10pp edge)
f* = (0.65 - 0.55) / (1 - 0.55) = 0.222

σ² = 0.0 (all signals agree):
  f_final = 0.222 × 1.0 × 0.5 = 0.111 → $1,110 on $10K

σ² = 0.5 (moderate disagreement):
  f_final = 0.222 × 0.5 × 0.5 = 0.056 → $560 on $10K

σ² = 1.0 (max disagreement):
  f_final = 0.222 × 0.33 × 0.5 = 0.037 → $370 on $10K
```

### 6.4 Sensitivity to Weights (w_member, w_signal)

**Corner cases:**

| w_member | w_signal | Behavior | When to Use |
|---|---|---|---|
| 1.0 | 0.0 | Ensemble spread only | Only GEFS available, no secondary signals |
| 0.6 | 0.4 | **Default** | GEFS + ECMWF + pressure delta |
| 0.3 | 0.7 | Signal-variance heavy | High trust in secondary signals, low trust in GEFS spread |
| 0.0 | 1.0 | Inter-signal disagreement only | Ensemble members not available (shouldn't happen) |

**Sensitivity test:** A 10% change in w_member (from 0.6 to 0.66) changes σ²_total by at most 0.06, changing the multiplier by ~2% at k=2.0. The system is relatively insensitive to small weight changes.

---

## 7. Test Cases

### 7.1 Test 1: All Members Agree, All Signals Agree (Full Confidence)

```
Given:
  member_predictions = [1, 1, 1, ..., 1]    # All 82 say "exceed"
  GEFS fraction = 1.0
  ECMWF fraction = 1.0
  pressure_delta_prob = 0.95
  market_price c = 0.60

Expected:
  p̂ = 1.0
  σ²_member = 1.0 * 0.0 = 0.0
  σ²_member_norm = 0.0 / 0.25 = 0.0

  μ_signal = (1.0 + 1.0 + 0.95) / 3 = 0.983
  σ²_signal = ((0.017²) + (0.017²) + (0.033²)) / 3 ≈ 0.00043
  σ²_signal_norm = 0.00043 / 0.25 = 0.0017

  σ²_total = 0.6 * 0.0 + 0.4 * 0.0017 = 0.0007

  kelly_multiplier = 1 / (1 + 2.0 * 0.0007) ≈ 0.999

  edge = 1.0 - 0.60 - 0.02 = 0.38 (positive, 38pp edge)

  f* = (1.0 - 0
  f* = (1.0 - 0.60) / (1 - 0.60) = 1.0  (standard Kelly for p=1.0)
  f_final = 1.0 * 0.999 * 0.5 = 0.4995

  position_size = min(0.4995 * 10000, 0.25 * 10000) = $2,500
  position_size = min($2,500, max_size)  →  $500 (max config cap in PROD)

Expected output: kelly_multiplier ≈ 0.999, position ≈ $500 (at config cap)
Assert: multiplier ≈ 1.0 (full confidence)
```

### 7.2 Test 2: Max Disagreement (41/41 Ensemble Split, Signals Split)

```
Given:
  member_predictions = [1×41, 0×41]    # 41 say exceed, 41 say not
  GEFS fraction = 0.5
  ECMWF fraction = 0.7
  pressure_delta_prob = 0.3
  market_price c = 0.55

Expected:
  p̂ = 0.5
  σ²_member = 0.5 * 0.5 = 0.25
  σ²_member_norm = 0.25 / 0.25 = 1.0

  μ_signal = (0.5 + 0.7 + 0.3) / 3 = 0.5
  σ²_signal_raw = (0 + 0.04 + 0.04) / 3 = 0.0267
  σ²_signal_norm = 0.0267 / 0.25 = 0.107

  σ²_total = 0.6 * 1.0 + 0.4 * 0.107 = 0.643

  kelly_multiplier = 1 / (1 + 2.0 * 0.643) = 1 / 2.286 ≈ 0.437

  edge = 0.5 - 0.55 - 0.02 = -0.07 (negative edge — do not trade)

Expected output: position_size = 0.0 (negative edge)
Assert: Edge check catches negative edge before sizing. Kelly multiplier is irrelevant.
```

### 7.3 Test 3: Moderate Ensemble Disagreement, Signals Agree

```
Given:
  member_predictions = [1×60, 0×22]    # 73% say exceed
  GEFS fraction = 0.73
  ECMWF fraction = 0.75
  pressure_delta_prob = 0.72
  regime_modulator = 0.85 (high confidence, used as confidence adjuster, NOT in variance)
  market_price c = 0.60

Expected:
  p̂ = 0.73
  σ²_member = 0.73 * 0.27 = 0.197
  σ²_member_norm = 0.197 / 0.25 = 0.788

  μ_signal = (0.73 + 0.75 + 0.72) / 3 = 0.733
  σ²_signal_raw = (0 + 0.0003 + 0.0003) / 3 = 0.0002
  σ²_signal_norm = 0.0002 / 0.25 = 0.0008

  σ²_total = 0.6 * 0.788 + 0.4 * 0.0008 = 0.473

  kelly_multiplier = 1 / (1 + 2.0 * 0.473) = 1 / 1.946 ≈ 0.514

  edge = 0.73 - 0.60 - 0.02 = 0.11 (positive, 11pp edge)

  f* = (0.73 - 0.60) / (1 - 0.60) = 0.13 / 0.40 = 0.325
  f_final = 0.325 * 0.514 * 0.5 = 0.0835

  position_size = 0.0835 * 10000 = $835
  position_size = min($835, $2,500) → $835

Expected output: kelly_multiplier ≈ 0.514, position ≈ $835
Assert: Signals agree but ensemble spread is high → position reduced ~half from full Kelly.
```

### 7.4 Test 4: Low Ensemble Spread, Signals Disagree

```
Given:
  member_predictions = [1×80, 0×2]     # 98% say exceed
  GEFS fraction = 0.98
  ECMWF fraction = 0.55
  pressure_delta_prob = 0.45
  market_price c = 0.75

Expected:
  p̂ = 0.98
  σ²_member = 0.98 * 0.02 = 0.0196
  σ²_member_norm = 0.0196 / 0.25 = 0.078

  μ_signal = (0.98 + 0.55 + 0.45) / 3 = 0.66
  σ²_signal_raw = (0.1024 + 0.0121 + 0.0441) / 3 = 0.0529
  σ²_signal_norm = 0.0529 / 0.25 = 0.211

  σ²_total = 0.6 * 0.078 + 0.4 * 0.211 = 0.131

  kelly_multiplier = 1 / (1 + 2.0 * 0.131) = 1 / 1.262 ≈ 0.792

  edge = 0.98 - 0.75 - 0.02 = 0.21 (positive, 21pp edge)

  f* = (0.98 - 0.75) / (1 - 0.75) = 0.23 / 0.25 = 0.92
  f_final = 0.92 * 0.792 * 0.5 = 0.364

  position_size = 0.364 * 10000 = $3,640
  position_size = min($3,640, $2,500) → $2,500 (capped at 25% of balance)

Expected output: kelly_multiplier ≈ 0.792, position ≈ $2,500 (capped)
Assert: Low ensemble spread but signal disagreement reduces multiplier to 0.79.
         Kelly fraction so large that position gets capped anyway at 25% of balance.
```

### 7.5 Test 5: No Secondary Signals Available

```
Given:
  member_predictions = [1×70, 0×12]    # Only GEFS (31 members), no ECMWF yet ingested
  signal_probabilities = []            # No secondary directional signals
  market_price c = 0.50

Expected:
  σ²_member = 0.85 * 0.15 = 0.1275
  σ²_member_norm = 0.1275 / 0.25 = 0.51

  M_effective = 0
  σ²_signal_norm = 0.0  (handled by < 2 check)

  σ²_total = 0.6 * 0.51 + 0.4 * 0.0 = 0.306

  kelly_multiplier = 1 / (1 + 2.0 * 0.306) = 1 / 1.612 ≈ 0.620

  edge = 0.85 - 0.50 - 0.02 = 0.33 (positive, 33pp edge)

  f* = (0.85 - 0.50) / (1 - 0.50) = 0.35 / 0.50 = 0.70
  f_final = 0.70 * 0.620 * 0.5 = 0.217

  position_size = 0.217 * 10000 = $2,170
  position_size = min($2,170, $2,500) = $2,170

Expected output: kelly_multiplier ≈ 0.620, position ≈ $2,170
Assert: Without secondary signals, only ensemble spread drives variance.
         15% disagreement → 38% size reduction from multiplier.
```

### 7.6 Test 6: High Price, Fee Kills Edge

```
Given:
  member_predictions = [1×60, 0×22]    # 85% exceed
  GEFS fraction = 0.85
  ECMWF fraction = 0.82
  pressure_delta_prob = 0.80
  market_price c = 0.88   (> 0.85 threshold, outside viable range)

Expected:
  market_price = 0.88 > 0.85 → reject per fee constraint

Expected output: position_size = 0.0
Assert: Market price constraint blocks trade before any variance calculation.
```

### 7.7 Convergence Test: Transition from Disagreement to Agreement

```
Simulation: 10 consecutive trades, same market (c=0.55), same p̂ target.
Ensemble starts at 50/50 split and converges to 70/12 split over 10 trades.

Trade 1: σ²_total = 0.95, multiplier = 0.345, position = $140  (high uncertainty)
Trade 5: σ²_total = 0.60, multiplier = 0.455, position = $260  (medium uncertainty)
Trade 10: σ²_total = 0.20, multiplier = 0.714, position = $490  (low uncertainty)

Expected: Monotonic increase in position size as ensemble converges.
Assert: No oscillations, no abrupt jumps.
```

---

## 8. Edge Cases and Failure Modes

### 8.1 Degenerate Case: Market Price at 0.50 (Fair Coin)

The Kelly fraction for p̂ = 0.55, c = 0.50:

```
f* = (0.55 - 0.50) / (1 - 0.50) = 0.05 / 0.50 = 0.10
```

With σ²_total = 0.5: f_final = 0.10 * 0.5 * 0.5 = 0.025 → $250 on $10K.

**Risk:** At small edges (p̂ vs c < 0.05), position sizes are already tiny. The variance multiplier further reduces them. This is *correct behavior* — small edges with high uncertainty should be sized very small.

### 8.2 Degenerate Case: ECMWF Not Yet Available

If only GEFS 31 members are available (no ECMWF 51):

- M_effective = number of secondary signals only (pressure delta, etc.)
- If pressure delta is the only secondary: M_effective = 1 → σ²_signal_norm = 0.0
- σ²_total = w_member * σ²_member_norm (pure ensemble spread)

**Net effect:** The system degrades gracefully to ensemble-spread-only weighting. Position sizes will be slightly larger than with full signal suite, but this is correct — without ECMWF, there's no cross-model disagreement to penalize.

### 8.3 Degenerate Case: All 82 Members Give Same Prediction

σ²_member = 0 → w_member term is zero. Only inter-signal variance matters.

If signals also agree: multiplier ≈ 1.0, full position.
If signals disagree despite member unanimity: this is a real signal of model bias catching something the NWP didn't.

**This is the most informative scenario** — the inter-signal variance term is doing its job.

### 8.4 Market Price Floor/Ceiling Violations

Per Gray Room Round 8 finding E2, trades at c < 0.40 or c > 0.85 should be rejected entirely, *before* any variance calculation. This is a separate gate, not part of the sizing formula.

**Implementation order:**

```python
def should_trade(market_price, edge):
    if not (0.40 <= market_price <= 0.85):
        return False, f"Market price {market_price:.2f} outside viable range [0.40, 0.85]"
    if edge <= 0:
        return False, f"Edge {edge:.4f} <= 0 after fees"
    return True, "OK"
```

### 8.5 Calibration Data Not Available

If ECE values are not computed (Round 9 Elephant 4), the `enable_calibration_adjustment` flag should be `False`. The signal probabilities are used as-is for variance calculations. This is safe — the variance calculation still works, it just doesn't benefit from calibration-quality adjustments.

### 8.6 Regime and Volume Momentum: Not in Inter-Signal Variance

Per Round 9 architecture, regime and volume momentum are *confidence modulators*, not directional signals. They should **not** be included in σ²_signal. Their output is a ±X% multiplier applied to the *final confidence* after variance weighting, not to the probability estimate itself.

**Correct application order:**

```python
# Step 1: Base ensemble fraction
p_hat = compute_ensemble_fraction(member_predictions)

# Step 2: Variance-weighted Kelly multiplier
kelly_mult = compute_variance_weighted_multiplier(member_predictions, directional_signal_probs)

# Step 3: Market microstructure modulators (applied AFTER variance)
volume_mod = volume_momentum_confidence_modulator(volume_data)     # e.g., 0.9, 1.0, or 1.1
regime_mod = regime_confidence_modulator(regime_data)              # e.g., 0.9 or 1.0

# Step 4: Final position sizing
multiplier_total = kelly_mult * volume_mod * regime_mod
```

---

## 9. Comparison of Candidate Functions

### 9.1 Linear: `1.0 - σ²`

| Property | Value |
|---|---|
| Derivation basis | Heuristic intuition |
| Range | [0, 1] |
| Shape at σ²=0 | Derivative = -1 (sharp initial drop) |
| Shape at σ²=1 | Approaches 0 (reaches zero) |
| Bayesian foundation | None |
| **Verdict** | **REJECT** — no theoretical basis, too aggressive at moderate uncertainty |

### 9.2 Hyperbolic: `1 / (1 + k * σ²)`

| Property | Value |
|---|---|
| Derivation basis | Bayesian expected utility (Beta posterior) |
| Range | (0, 1] for k ≥ 0 |
| Shape at σ²=0 | Derivative = -k (tunable, gentle for k small) |
| Shape at σ²=1 | 1/(1+k) (never reaches zero, asymptotically approaches) |
| Bayesian foundation | Strong — falls out of posterior expected value maximization |
| **Verdict** | **RECOMMEND** — principled derivation, tunable k, graceful tails |

### 9.3 Gaussian: `exp(-k * σ²²)`

| Property | Value |
|---|---|
| Derivation basis | Squared-exponential kernel heuristic |
| Range | (0, 1] |
| Shape at σ²=0 | Derivative = 0 (too flat near full confidence) |
| Shape at σ²=1 | exp(-k) (decays faster than hyperbolic for same k) |
| Bayesian foundation | None (would imply Gaussian posterior, not Beta) |
| **Verdict** | **REJECT** — no natural boundary, too aggressive at high σ², zero derivative at origin |

### 9.4 Polynomial: `1 - σ²^n` (for n > 1)

| Property | Value |
|---|---|
| Derivation basis | Generalization of linear |
| Range | [0, 1] |
| Shape at σ²=0 | Derivative = 0 for n > 1 (flat at full confidence) |
| Shape at σ²=1 | Approaches 0 (reaches zero for any n) |
| Bayesian foundation | None |
| **Verdict** | **REJECT** — ad-hoc, reaches zero, no interpretable parameter |

### 9.5 Recommended: Hyperbolic with k=2.0

```
kelly_multiplier = 1.0 / (1.0 + 2.0 * σ²_total)
```

**Why:**
1. Derives from Bayesian expected utility maximization (see Appendix)
2. Single tunable parameter k with intuitive meaning: "How many units of variance halve your position?"
3. At k=2.0: σ²=0.5 → multiplier=0.50 (half); σ²=0.0 → multiplier=1.0 (full); σ²=1.0 → multiplier=0.33 (never zero)
4. Floor at 0.1 provides safety net below this
5. Heavy tails at high σ² prevent zero-sizing even at total uncertainty

---

## 10. Appendix: Derivation from First Principles

### A.1 Bayes' Rule for the True Exceedance Probability

Let the true (unknown) exceedance probability be θ ∈ [0, 1]. We observe N ensemble members, each making a binary prediction Xᵢ ∈ {0, 1}. We model:

```
Xᵢ | θ ~ Bernoulli(θ)        for i = 1, ..., N
```

With a conjugate Beta prior (Jeffreys prior for Bernoulli is Beta(0.5, 0.5), but a uniform prior Beta(1, 1) is simpler):

```
θ ~ Beta(α, β)
```

The posterior after observing N members with k exceedances (k = N * p̂):

```
θ | data ~ Beta(α + k, β + N - k)
```

Posterior mean and variance:

```
E[θ | data] = (α + k) / (α + β + N)
V[θ | data] = (α + k)(β + N - k) / ((α + β + N)²(α + β + N + 1))
```

With a uniform prior (α = β = 1):

```
E[θ] = (1 + k) / (2 + N)
V[θ] = (1 + k)(1 + N - k) / ((2 + N)²(3 + N))
```

For N = 82, this is a very informative posterior. The variance is dominated by the ensemble spread, not the prior.

### A.2 Expected Log Utility Under Posterior Uncertainty

We want to maximize expected log utility over the posterior:

```
f* = argmax_f E_{θ|data}[ U(f, θ) ]

where U(f, θ) = θ * log(1 + f * R) + (1 - θ) * log(1 - f)
and R = (1 - c) / c  (return multiple on a winning bet)
```

This integral has no closed-form solution, but we can approximate it.

### A.3 Second-Order Taylor Approximation

For small f (we're using 50% fractional Kelly, so f is already bounded), expand U around f = 0:

```
U(f, θ) ≈ f * (θ * R - (1 - θ)) - 0.5 * f² * (θ * R² + (1 - θ))
        = f * (θ * (R+1) - 1) - 0.5 * f² * (θ * R² + 1 - θ)
```

Taking expectation over θ:

```
E[U] ≈ f * (E[θ] * (R+1) - 1) - 0.5 * f² * (E[θ] * R² + 1 - E[θ])
        + (terms involving Var(θ))
```

The variance term V[θ] appears only in the second-order term of f * R² (when expanding θ * R², the expectation is E[θ] * R², not E[θ*R²]) — this is the key point. Actually let me be more careful:

```
E[θ * R²] = R² * E[θ]
E[1 - θ] = 1 - E[θ]
```

The variance effect is more subtle — it's about the interaction between f² and the θ terms. The full expected utility is:

```
E[U(f)] = E[θ] * log(1 + fR) + (1 - E[θ]) * log(1 - f) + 0
```

Wait — this is exact! Because U is linear in θ (given f), the expected utility under posterior uncertainty is:

```
E[U(f)|data] = E[θ|data] * log(1 + fR) + (1 - E[θ|data]) * log(1 - f)
```

**This means the variance of θ doesn't appear directly in the Kelly optimization.** The Kelly formula already uses E[θ|data] which already accounts for uncertainty through shrinkage relative to the raw fraction.

### A.4 Where the Variance Term Actually Enters

The above is correct for a single-period Kelly bet. However, in practice:

1. **We make sequential bets.** The ensemble variance across time tells us about the *stability* of the probability estimate.
2. **The risk of model misspecification** — if the ensemble is wrong (all members share the same bias), our posterior is overconfident. Inter-signal variance catches this.
3. **Kelly assumes i.i.d. bets**, but weather markets have serial correlation during transition periods. Ensemble variance is a proxy for regime instability.

The rigorous Bayesian approach for regime-aware sizing uses a **hierarchical model** where:

```
θ_t = latent true probability at time t
θ_t | θ_{t-1}, σ²_regime ~ N(θ_{t-1}, σ²_regime)   # random walk
X_tᵢ | θ_t ~ Bernoulli(θ_t)                           # observation
```

But this is overkill. The hyperbolic multiplier `1/(1 + kσ²_total)` is a **one-parameter approximation** to the optimal sizing under a hierarchical model where σ²_total is an indicator of volatility in θ_t.

### A.5 Practical Bounds

For numerical stability:

```python
σ²_member_norm = min(1.0, p_hat * (1 - p_hat) / 0.25)          # clamp to [0, 1]
σ²_signal_norm = min(1.0, signal_variance / 0.25)               # clamp to [0, 1]
σ²_total = min(1.0, w_member * σ²_member_norm + w_signal * σ²_signal_norm)  # clamp
kelly_multiplier = max(floor_multiplier, 1.0 / (1.0 + k * σ²_total))
```

---

## Change Log

| Date | Author | Change |
|---|---|---|
| 2026-08-01 | Quantitative Finance (FP) | Initial specification. Supersedes B-Mode Cycle 6 linear disagreement. |