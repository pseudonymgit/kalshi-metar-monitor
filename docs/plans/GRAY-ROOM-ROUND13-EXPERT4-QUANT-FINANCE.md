# Gray Room Round 13 — Expert 4: Quant Finance
## Intraday Kelly Sizing & P&L Management

---

### Current System State (From Code Audit)

The system already implements:
- **Fee-aware Kelly:** f* = (p - c) / (1 - c) where c = ROUND_TRIP_FEE = 0.0205
- **Fractional Kelly:** fraction_kelly = 0.5 (half-Kelly default)
- **Edge-based tiers:** edge > 0.10 → 75% Kelly, 0.06–0.10 → 50%, 0.03–0.06 → 25%, < 0.03 → no trade
- **Confidence-based multiplier:** 80%+ → 2×, 70–80% → 1.5×, 60–70% → 1×, < 60% → 0.5×
- **Bankroll cap:** 8% per position, 10% trailing drawdown
- **Bayesian Beta-Binomial belief:** Beta(α=1, β=1) prior, posterior updates
- **Correlation clamp:** max 0.60 pairwise, risk budget 3D matrix

**Correction:** At p=0.671, c=0.0205 the correct fee-aware Kelly is:
f* = (0.671 - 0.0205) / (1 - 0.0205) = 0.6641 → **66.4% of bankroll**. With half-Kelly that's **33.2%** before edge/confidence scaling.

---

## 1. Intraday Kelly Decay: Time-Dependent Position Sizing

### The Problem
A signal at 00Z has ~18 hours of forecast evolution — 4 GEFS refresh cycles to potentially diverge. A signal at 18Z has only ~6 hours to settlement — far less time to shift.

**Kelly should not be constant across the trading day.**

### Recommended Intraday Kelly Schedule (Epoch-Based)

| Time Window | Hours to Settlement | Kelly Fraction | Rationale |
|---|---|---|---|
| 00Z – 06Z | 18–24h | 0.50× (base) | Full uncertainty window |
| 06Z – 12Z | 12–18h | 0.65× | First refresh confirms/adjusts |
| 12Z – 18Z | 6–12h | 0.85× | Two confirmations in |
| 18Z – 24Z | 0–6h | **0.40×** | Late cycle — lower liquidity, wider spreads, less reaction time |

### Implementation
Modify sizing to accept epoch_multiplier:
```
effective_kelly = raw_kelly × fraction_kelly × edge_tier × confidence × epoch_mult × drawdown_penalty
```

---

## 2. P&L Variance Management

### Observations
- Winner: +$325 | Loser: -$70 (4.64:1 win/loss ratio)
- 67.1% accuracy → ~85% of edge comes from the 32.9% loss cluster

### Does Kelly Need Adjustment for Binary Options?

**Yes — two critical adjustments:**

#### Adjustment 1: Payoff-aware binary Kelly
Current formula f* = (p-c)/(1-c) is a **continuous-bet approximation**. For binary options:

```
f* = p/b - q/a
where b = (1 - price - c) / price, a = 1.0

f* = (p × price) / (1 - price - c) - (1 - p)
```

At price ≈ 0.50 (at-the-money), this simplifies approximately to the current formula. But for deep ITM/OTM contracts (price = 0.15 or 0.85), the difference can be **20-30% of position size**.

**Recommendation:** Replace calculate_kelly_fraction() with binary_kelly_fraction(win_rate, entry_price, cost).

#### Adjustment 2: Consecutive-loss survival
With current parameters (8% cap, half-Kelly): max position ~$800. A $70 loss is 0.7% of $10k. Four consecutive losses = $280 = 2.8%. Well within 10% drawdown threshold.

The system can survive ~14 consecutive losses before the 10% drawdown trigger. At 33% loss rate, P(14 consecutive) = 0.329^14 ≈ 0.0004% — effectively never.

**Recommendation:** Add a **time-dependent daily loss limit** — 5% of bankroll per day tighter than the 10% trailing drawdown — to survive a "bad day" of 7-8 losses in 14 trades.

---

## 3. Re-evaluation Friction Cost

### The Friction
One-way adjustment cost = half-spread + slippage ≈ 1.275¢ per contract. Full reversal = 2.55¢ per contract.

### Optimal Cadence: Event-Driven, Not Time-Driven

| Trigger | Action | Cost | Frequency |
|---|---|---|---|
| GEFS refresh (every 4h) | Full re-eval | 1 round-trip | Fixed schedule |
| Frontal passage detected | Confidence boost only | 0 cost (no trade change) | Event-driven |
| Dewpoint confirms morning cap | Adjust confidence | 0 cost (no trade change) | Once per day |
| Spatial coherence change | Modulate existing | 0 cost (probability adjustment) | Per eval |

**Key insight:** Most intraday signals should **modulate confidence**, not trigger new trades. This avoids the friction cost entirely. Only the GEFS refresh (4-hourly) should trigger a full position re-evaluation.

---

## 4. Time-Dependent Confidence Thresholds

Current: static min_agreement=2, edge_min=0.03, conf_min=0.60

**Recommendation:** Thresholds should tighten as settlement approaches:

| Time | Min Edge | Min Confidence | Min Agreement | Rationale |
|---|---|---|---|---|
| 00Z–06Z | 0.02 | 0.50 | 1 | Wide net early |
| 06Z–12Z | 0.03 | 0.55 | 2 | Filter after first refresh |
| 12Z–18Z | 0.04 | 0.60 | 2 | Tighten — two confirms |
| 18Z–24Z | **0.06** | **0.65** | **3** | Highest bar — must be confident to trade in last window |

---

## 5. Estimation Uncertainty (Small Sample)

At 85 trades, 67.1% accuracy has 95% CI of approximately ±10pp (Agresti-Coull: ~59.3% to ~74.9%).

**Recommendation:** Use the **lower bound** of the confidence interval for Kelly sizing, not the point estimate. If the point estimate is 67% but the 95% CI lower bound is 59%, use 0.59 in the Kelly formula. This naturally produces smaller positions when less data is available.

As sample size grows (300+ trades), the CI tightens and converges to the point estimate. A good trigger: after 300 trades, switch from lower-bound to point-estimate Kelly.

---

## 6. Stale vs. Fresh Signals (Recency Weighting)

The 18Z GEFS cycle is only 6h from settlement. It should get higher decision weight (more accurate) but **lower Kelly multiplier** (not enough time to capitalize if it shifts).

**Resolution:** The epoch-based Kelly schedule already handles this — 18Z window at 0.40× Kelly multiplier despite having the highest information quality. This correctly balances:
- Higher forecast accuracy → higher confidence to enter a position
- Shorter horizon → smaller position because less time for edge to compound

---

## Summary: Top 5 Recommendations

1. **Implement epoch-based Kelly schedule** (0.50/0.65/0.85/0.40× across the trading day) by modifying PositionSizer to accept a time_multiplier parameter
2. **Replace Kelly formula** with `binary_kelly_fraction(win_rate, entry_price, cost)` for accurate binary-option sizing at asymmetric prices
3. **Use CI lower bound** for Kelly input at small sample sizes (n < 300); switch to point estimate at scale
4. **Add time-dependent confidence thresholds** that tighten toward settlement (edge 0.02→0.06, confidence 0.50→0.65)
5. **Add 5% daily loss limit** on top of existing 10% drawdown — tighter layer for bad-day protection