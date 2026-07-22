# Gray Room Round 6 — Expert 6: Decision Theory / Operations Research

## OR-Gate Fusion Architecture for Binary Kalshi Weather Markets

---

### 1. OR-Gate Optimality

**Framework choice:** The OR gate with confidence modulation is *approximately optimal* under a **Neyman-Pearson paradigm** where we treat the two lanes as conditionally independent detectors. Here's why:

#### Decision-Theoretic Rationale

Let:
- \( L_1, L_2 \) be binary signals from METAR (~72% accuracy) and NWP (TBD).
- \( c_1, c_2 \in [0,1] \) be the model-assigned confidence (posterior probability) attached to each signal.
- True state \( \theta \in \{HIGH, LOW\} \).
- Action space \( A \in \{TRADE_H, TRADE_L, NO\_TRADE\} \).

The OR gate implements the decision rule:

\[
\text{Trade if: } \max(L_1 \cdot c_1,\; L_2 \cdot c_2) > \tau
\]
with boosted confidence when both agree: \( \text{conf}_{\text{combined}} = 1 - (1-c_1)(1-c_2) \) for same-direction signals.

**Is this optimal?** Under a **Neyman-Pearson framework** with a false-trade budget (Type I error rate), the optimal fusion rule for two conditionally independent detectors is a **likelihood ratio test (LRT)**:

\[
\Lambda(x_1, x_2) = \frac{P(x_1, x_2 \mid \theta = HIGH)}{P(x_1, x_2 \mid \theta = LOW)} \gtrless \tau'
\]

The OR gate is a **special case** of the LRT when:
- Each lane's decision is binary (thresholded internally).
- The lanes are conditionally independent given the state.
- The cost structure is symmetric (false positive / false negative equally bad).

If these hold, the OR gate is equivalent to a particular point on the ROC curve of the fused system — and it is **Pareto-optimal in the Neyman-Pearson sense**.

**Caveats:**
- If NWP accuracy differs significantly from METAR, the unweighted OR loses information. A **weighted OR** or **Bayesian posterior fusion** would outperform.
- If lanes are *positively correlated* (both make same-direction errors), the OR gate's benefit collapses. Correlation must be measured.
- A **Bayesian decision rule** (minimize expected loss given posterior probability) is technically optimal for arbitrary loss functions, but requires calibrated probabilities from both lanes.

**Recommendation:** Use the OR gate as a pragmatic first implementation, but instrument it to compare against a Bayesian posterior fusion baseline. If NWP accuracy ≠ METAR accuracy, switch to weighted fusion.

---

### 2. Threshold Selection

#### Single-Lane vs. OR-Gate Thresholds

**Key insight:** The OR gate *lowers* the effective decision threshold because you trade if *either* lane says yes. This increases both true positives AND false positives.

**Threshold policy: separate per lane, not equal.**

For a single lane with accuracy \(a\), the optimal threshold balances expected value:

\[
\mathbb{E}[V] = P(\text{correct}) \cdot (\text{payout} - \text{spread}) \;-\; P(\text{wrong}) \cdot \text{spread}
\]

With Kalshi's 3.1¢ mean spread and $1 payout:

| Lane Accuracy | Break-even Confidence (single lane) |
|---|---|
| 72% (METAR) | \( 0.031 / (1 - 0.031) \approx 3.2\% \) above 50% |
| 60% (hypothetical NWP) | Same break-even, but *lower margin* |

So single-lane break-even is around **53.2% confidence** (for METAR). Anything above that has positive expected value.

**OR-gate thresholds should be asymmetric:**

- **High-accuracy lane (METAR):** Lower threshold. METAR at 72% is reliable; we want to let it fire freely. Threshold ~55% (slightly above break-even) to avoid noise.
- **Lower-accuracy lane (NWP):** Higher threshold. If NWP is, say, 60%, its false-positive rate is higher. Set threshold to ~60% to compensate.
- **Or, better:** derive threshold from the lane's *calibrated* posterior, not raw confidence. Use Platt scaling or isotonic regression.

**Why separate thresholds work better with OR:** Without separation, the OR gate lets the weaker lane drag down fusion performance. Separate thresholds let each lane operate at its own optimal operating point on its ROC curve.

#### How OR changes optimal threshold vs. single lane:

Let \( \alpha_1, \alpha_2 \) be the false-positive rates at chosen thresholds. The OR gate's combined FPR is:

\[
\alpha_{\text{OR}} = 1 - (1 - \alpha_1)(1 - \alpha_2) \approx \alpha_1 + \alpha_2 \quad \text{(for small } \alpha)
\]

So to maintain the same false-positive budget as a single lane, **each lane must operate at roughly half the FPR**, meaning *higher* thresholds per lane. Or accept a higher combined FPR (more trades but more losses).

**Recommendation:** Start with per-lane thresholds calibrated to yield:
- METAR FPR ≈ 0.25 (threshold ~55-60%)
- NWP FPR ≈ 0.20 (threshold ~60-65%)
- Combined FPR ≈ 0.40

This yields ~2× the trade volume of a single lane, with 60-70% net accuracy target.

---

### 3. False Positive Management

**Yes, the OR gate increases false positives by definition.** The math:

If each lane has FPR \( \alpha \), the combined FPR is \( 1 - (1-\alpha)^2 = 2\alpha - \alpha^2 \approx 2\alpha \).

**Mitigation strategies (in priority order):**

#### a) Boosted confidence is real but insufficient alone

When both lanes agree, the combined confidence using **Dempster-Shafer style fusion**:

\[
\text{conf}_{\text{both}} = 1 - (1 - c_1)(1 - c_2)
\]

For \( c_1 = 0.60, c_2 = 0.55 \): combined confidence = \( 1 - 0.4 \times 0.45 = 0.82 \).

This means when both agree, we have high conviction — good. The problem is the **middle zone** where one lane fires weakly and the other says nothing.

The boosted confidence when both agree **does** compensate for some false positives — because you can size positions bigger on both-agree signals, earning more on the winners. But it doesn't prevent the false positives from the one-lane-firing cases.

#### b) Size modulation by agreement type

| Agreement Type | Confidence | Sizing | Expected contribution |
|---|---|---|---|
| Both agree (same direction) | High | Full Kelly | High positive EV |
| One lane fires (other neutral) | Medium | Half Kelly | Positive but thin |
| Both fire (conflicting) | Throw out | Zero | — |
| Neither fires | Zero / climatology | Minimal | Cautious |

This is the **best practical defense**: don't trade at full size on single-lane signals. The OR gate says "yes" to trade, but position sizing says "small position."

#### c) Climatology baseline filter

Before taking a single-lane trade, check if the signal is a *significant deviation* from climatology. If METAR says HIGH but it's July in Phoenix, that's climatology — skip it. Only trade true deviations.

#### d) False-positive budget pacing

If the system aims for 100 trades before going live with real money, cap the false-positive acceptance at N trades. Track live FPR and adjust thresholds dynamically.

---

### 4. Cost of Waiting (Opportunity Cost)

**The missed-trade problem:** If neither lane fires but the binary market turns out right, we suffer an opportunity cost that is structurally invisible to the system (we never log it as a mistake).

**Quantifying the cost:**

Let:
- \( P_{\text{miss}} \) = probability that a profitable trade exists but neither lane fires
- \( \bar{V} \) = expected value of a trade when one lane fires
- \( \text{Freq}_{\text{trade}} \) = how often each lane fires

The opportunity cost per day:

\[
\text{Cost}_{\text{wait}} = P_{\text{miss}} \cdot \bar{V} \cdot \text{Freq}_{\text{miss}}
\]

If METAR fires 150 days/year, NWP fires 180, they overlap 100 days, and we miss 30 profitable days at $0.05 EV each: **$1.50/year opportunity cost** — negligible relative to execution costs. But scale matters: with 20 cities × 2 contracts, missed trades could be $60/year.

#### Climatology Fallback — Recommended

**Proposal:** Introduce a **climatology baseline trading lane** that:
1. Computes the unconditional probability of HIGH/LOW from the historical record for each city/date.
2. Trades only when:
   - Neither lane has fired, AND
   - The market-implied probability deviates significantly from climatology (e.g., market says 70% HIGH but climatology says 55% HIGH → edge exists)

This acts as a **minimum-information Bayesian prior**. The fallback trades at very small size (0.1× Kelly) and captures low-hanging opportunity cost.

**Implementation rule:**

\[
\text{If } \max(L_1 c_1, L_2 c_2) < \tau \text{ AND } |p_{\text{market}} - p_{\text{climatology}}| > \delta: \text{ trade climatology bias}
\]

Where \( \delta \) is determined from backtesting (suggest start: 15 percentage points).

#### Risk of climatology fallback

The fallback can increase trade volume by 20-40% with lower accuracy (~55-58% expected). This drags down overall win rate but may increase total P&L. **Run a separate backtest for the fallback lane** before enabling.

---

### 5. Kelly Sizing with Two Signal Lanes

#### Standard Kelly for a Single Lane

Given edge \( e = P(\text{correct}) - 0.5 \) and payout structure (binary, $1 for $1):

\[
f^* = \frac{p \cdot (1) - (1-p) \cdot (1)}{(1)} = 2p - 1
\]

For METAR at 72%: \( f^* = 0.44 \) → bet 44% of capital per signal.

**Problem:** With 20 cities × 2 contracts, Kelly is far too aggressive. Standard solution: **fractional Kelly** (1/4 or 1/10).

#### Two-Lane Kelly Adjustments

With two independent lanes, the optimal Kelly fraction depends on **whether you can size independently per signal or must size once per contract.**

**Case A — Simultaneous signals (both lanes fire on same contract):**
- Combined edge: \( e_{\text{combined}} \) = improved accuracy from fusion
- Kelly for both-agree: \( f^*_{\text{both}} = 2 p_{\text{both}} - 1 \)
- If both-agree accuracy reaches 82%: \( f^* = 0.64 \)

**Case B — Single signal (one lane fires):**
- Use the lane's individual Kelly fraction, but **halve it** because the OR gate admits lower-conviction trades
- METAR alone at 72%: use \( f = 0.22 \) (half Kelly) instead of 0.44
- NWP alone (say 60%): \( f = 0.10 \) (half Kelly from \( 2 \cdot 0.6 - 1 = 0.20 \))

**Case C — Portfolio across 20 cities:**
- Apply **Kelly for multiple simultaneous bets** (simplified: allocate capital across cities using \( f_i = e_i / \sigma_i^2 \) approximation)
- Cap total exposure at 25% of capital (fractional Kelly of 0.25)

#### Does the optimal Kelly fraction increase?

**Yes, but only for the both-agree sub-portfolio.** The overall Kelly fraction does NOT increase because you're adding lower-conviction single-lane trades. However, the **information ratio** of the portfolio should improve if the fusion adds value.

**Practical sizing table:**

| Signal Mode | Expected Accuracy | Kelly Fraction | Recommended (1/4 Kelly) |
|---|---|---|---|
| Both agree | ~82% | 0.64 | 0.16 |
| METAR only | ~72% | 0.44 | 0.11 |
| NWP only | ~60% | 0.20 | 0.05 |
| Climatology fallback | ~57% | 0.14 | 0.035 |

---

### 6. Evaluation Metrics

**Primary metric for the fused system:**

#### Combined Sharpe Ratio (Information Ratio)

\[
\text{IR}_{\text{fused}} = \frac{\mathbb{E}[R_{\text{fused}}]}{\sigma(R_{\text{fused}})} \cdot \sqrt{252}
\]

This is the **single best metric** because it captures both accuracy and variance. A system that wins 60% of trades but with tiny variance can be better than a system that wins 72% with high variance.

#### Required supporting metrics:

| Metric | Purpose | Target |
|---|---|---|
| **Combined Sharpe (IR)** | Risk-adjusted return | > 1.0 for paper, > 2.0 target |
| **System win rate** | Overall accuracy | > 58% (to exceed spread) |
| **Win rate by signal type** | Which fusion mode adds value | Both-agree > METAR-only > NWP-only |
| **Lift over best single lane** | Does fusion help? | Net P&L increase > 0 |
| **False-trade rate** | % of trades losing | < 42% |
| **Missed-trade rate** | Profitable days not traded | < 30% of total profitable days |
| **Calibration error (Brier score)** | Are confidences accurate? | < 0.15 |
| **Per-lane marginal contribution** | Each lane's standalone and conditional impact | Both > 0 |

#### Key Comparison: Is Fusion Actually Better?

The critical test is not just "does the fused system make money?" but rather: **"Is the fused system better than just running the best single lane at a higher threshold?"**

Run this comparison:

1. **Baseline A:** METAR-only at optimal single-lane threshold (e.g., 55%), full Kelly sizing.
2. **Baseline B:** NWP-only at its optimal threshold.
3. **Fused:** OR gate with per-lane thresholds and size modulation as designed.

**Decision rule:** Only adopt fusion if:

\[
\text{Net P\&L}_{\text{fused}} > \max(\text{Net P\&L}_{A}, \text{Net P\&L}_{B}) \quad \text{AND} \quad \text{IR}_{\text{fused}} > \max(\text{IR}_{A}, \text{IR}_{B})
\]

If fusion improves P&L but worsens Sharpe, consider running both lanes independently rather than fusing.

---

### Accuracy Improvement Suggestion

**Proposal: Calibrate both lanes before fusion, not after.**

Most classification models output uncalibrated scores. If METAR says "72% confidence," it likely means "72% of my training samples with this score were correct" — but the *distribution of scores* may be miscalibrated at the tails.

**Implementation:**

1. **Platt scaling** (logistic regression on lane outputs) to produce true posterior probabilities.
2. **Isotonic regression** as a nonparametric alternative (better if calibration is monotonic but not sigmoidal).
3. **Measure calibration by Brier score** on a holdout set.

**Why this improves accuracy:** Uncalibrated fusion amplifies errors. If METAR's 72% actually means 65% in practice, the OR gate's combined confidence formula (\( 1 - (1-c_1)(1-c_2) \)) is overconfident. Calibration fixes this at the source.

**Expected gain:** 2-5 percentage points in fused accuracy from calibration alone, at near-zero implementation cost.

---

### Summary Decision Framework

| Dimension | Recommendation |
|---|---|
| Fusion rule | OR gate as MVP; instrument Bayesian posterior for comparison |
| Thresholds | Separate per lane (lower for METAR, higher for NWP) |
| False positives | Accept ~2× FPR; mitigate with size modulation and climatology filter |
| Opportunity cost | Add climatology fallback at 0.1× Kelly |
| Kelly sizing | 1/4 Kelly; differentiate by signal type; cap portfolio at 25% |
| Primary metric | Combined Sharpe (IR), with win-rate breakdown by signal type |
| Go/no-go for fusion | Fused must beat *best single lane* on both P&L and Sharpe |

**Bottom line:** The OR gate is a reasonable starting point, but the real value comes from calibration, asymmetric thresholds, and signal-dependent sizing — not the fusion rule itself.
