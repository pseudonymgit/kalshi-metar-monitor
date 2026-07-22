# Gray Room Round 6 — Expert 6 Follow-up: HMM / Markov Chain for Regime Detection

**Verdict: Do not add HMM in Phase 2.5. Revisit in Phase 4 after live trading data exists.**

---

## 1. Would HMM regime detection improve the framework?

Yes — in theory. If a Hidden Markov Model correctly identifies latent regimes (stormy, transitional, stable), it would let us condition the entire decision chain on the regime state. In a stormy regime the base rate of HIGH events is higher, signal accuracy improves (more observable weather), and lane correlation changes. This would shift optimal Kelly sizing upward (higher prior → thinner edge needed) and lower per-lane thresholds. In a stable regime, we'd tighten thresholds and reduce sizing.

However, **the benefit is marginal given what we already have.** The existing pipeline already includes `regime_signal` (likely a smoothed composite of recent observations) and the `spatial coherence gate` (which checks whether nearby stations agree — itself a proxy for regime width). These two features collectively capture most of the first-order regime information an HMM would extract. The HMM's main marginal contribution would be capturing *persistence* (the Markov transition structure: how long regimes tend to last). That's real but second-order.

## 2. HMM interaction with Bayesian log-odds fusion

The cleanest integration is a **regime-conditioned Bayesian prior**. Instead of:

`P(HIGH | signals) ∝ P(signals | HIGH) · P(HIGH)`

We'd use:

`P(HIGH | signals, Rₜ) ∝ P(signals | HIGH, Rₜ) · P(HIGH | Rₜ)`

The log-likelihood ratios become regime-specific:
- In stormy regime: log-odds threshold shifts left (lower bar to trade HIGH)
- In stable regime: log-odds threshold shifts right (higher bar)
- Regime-specific likelihood functions (METAR and NWP may have different ROC curves per regime)

This is elegant but costly: it requires regime-labeled training data, regime-specific calibration for each lane, and a Viterbi or forward-backward pass on every decision tick. The Kelly sizing also becomes regime-conditioned (three sizing tables × N regimes).

## 3. Build order

| Phase | What | Why |
|---|---|---|
| Phase 2.5 (now) | OR-gate fusion, calibration, per-lane thresholds | Core engine must work |
| Phase 3 | Climatology fallback, spatial coherence tuning, regime_signal tuning | Get the cheap proxies working |
| Phase 4 | HMM regime detection | Only if Phase 3 data shows regime_signal leaves money on the table |

## 4. Decision-theoretic cost-benefit

The complexity budget is real. An HMM adds: (a) model selection (how many regimes? Gaussian vs categorical emissions?), (b) training pipeline with labeled or EM-inferred states, (c) regime-specific calibration tables, (d) regime-conditioned Kelly tables, (e) transition matrix updates, (f) cold-start problem (no regime estimate on first few ticks). That's 2-3 weeks of work for maybe 1-3 percentage points of accuracy gain — and the existing `regime_signal` + `spatial coherence` combination may already capture 80% of that gain at zero incremental cost. Until we see live trading data showing that regime accuracy varies significantly by regime (i.e., the regime_signal filter isn't doing enough), the HMM is a speculative optimization on top of an unproven base engine.

## 5. Simplest useful test

Run the combinatorial search that's already in the pipeline, but split results by `regime_signal` value (stormy/neutral/stable). Compute:

- **Optimal threshold for METAR in stormy vs stable regimes** — if they differ by >5 percentage points, HMM has potential value.
- **Optimal Kelly fraction by regime** — if stormy-regime Kelly is >2× stable-regime Kelly, the Markov structure matters.

If neither condition holds, HMM is mathematically unnecessary: the existing regime_signal captures all the regime information worth capturing, and adding transition dynamics won't move the needle.

---

## Bottom line

**No.** Do not add HMM now. Get the fusion engine live, calibrated, and running on real data. After 200+ trades, compute the regime-conditioned metrics above. If the numbers show regime matters beyond what `regime_signal` already captures, *then* invest in HMM. Until then, the marginal value doesn't justify the complexity cost.
