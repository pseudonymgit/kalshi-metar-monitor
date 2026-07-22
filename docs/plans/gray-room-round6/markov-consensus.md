# Gray Room Round 6 — Markov Consensus

**Date:** 2026-07-21
**Moderator:** Reconciled from Expert 1 (Meteo Stats), Expert 5 (UQ), Expert 6 (Decision Theory)
**Status:** Consensus Recommendation

---

## Consensus Verdict

**Maybe — proceed with a cheap diagnostic test in Phase B, build the simplest Markov overlay in Phase C only if the test passes, and defer full HMM to Phase 4 unless empirical evidence demands more.**

All three experts agree the HMM/Markov concept has genuine theoretical value for regime detection. The disagreement is about *when* and *how urgently* — not *whether*. The cheapest possible test (proposed by E6) resolves this disagreement by producing objective, quantitative evidence before committing build resources.

---

## 1. Where the Experts Agree

| Point | Agreement |
|---|---|
| **Phase A** | ❌ No HMM. Focus exclusively on getting the analog matching / DP-GMM / fusion pipeline live. |
| **Theoretical value** | ✅ HMM adds temporal persistence, multivariate regime awareness, and probabilistic state transitions — none of which the existing deterministic `regime_signal` provides. |
| **Build cost** | ✅ 2–3 weeks for a simple implementation. |
| **Overcomplication risk** | ✅ Real but containable. All three recommend the simplest possible version first, not a full Bayesian HDP-HMM. |
| **Baseline comparison** | ✅ Benchmark any HMM build against an analog-only / regime-signal-only baseline; remove if improvement < 5%. |

---

## 2. Where the Experts Disagree

| Dimension | E1 (Meteo Stats) | E5 (UQ) | E6 (Decision Theory) |
|---|---|---|---|
| **Phase placement** | Phase C | Phase C | Phase 4 (after live trading data) |
| **Implementation flavor** | 3-state Gaussian HMM per station (T2m, MSLP, wind_speed) via `hmmlearn` | Empirical Markov chain from DP-GMM regime posteriors (Viterbi + empirical transition matrix) | Don't build yet; run diagnostic first |
| **Evidence bar before building** | Low — build and test | Low — build and test | High — need live trading data first |
| **Assessment of existing `regime_signal`** | It has 3 blind spots (transient, multivariate, no memory) | (No explicit critique; focuses on DP-GMM pathway) | Already captures ~80% of regime information; HMM is second-order |
| **Risk tolerance** | Build now, rip out if <5% improvement | Build simple version now, graduate to full HMM only if needed | Don't build until data proves it's needed |

---

## 3. What Resolves the Disagreement: E6's Cheap Test

E6 proposed a diagnostic that can be run **during Phase B** using existing pipeline components — no HMM needed. This test answers the core empirical question: *Does regime-conditioned behavior differ enough from the unconditional baseline to justify a Markov model?*

### Test: Regime-Split Combinatorial Search

Run the existing combinatorial search (optimal thresholds, optimal Kelly fractions) **split by `regime_signal` value** (stormy / neutral / stable). For each regime bucket, compute:

| Metric | Condition That Justifies HMM |
|---|---|
| **Optimal METAR threshold in stormy vs. stable** | Thresholds differ by >5 percentage points across regimes |
| **Optimal Kelly fraction by regime** | Stormy-regime Kelly > 2× stable-regime Kelly |
| **Optimal lane threshold by regime** | Per-lane thresholds differ significantly by regime (ANOVA p < 0.05 on threshold difference across regime buckets) |

### How to Run It

```
# Pseudocode for the diagnostic
for each regime_label in [stormy, neutral, stable]:
    filter trades/observations where regime_signal == regime_label
    run combinatorial_threshold_search(filtered_data)
    record optimal_METAR_threshold[regime_label]
    record optimal_Kelly_fraction[regime_label]
    record optimal_lane_thresholds[regime_label]

if thresholds_differ_by > 5pp across regimes:
    print("HMM RECOMMENDED — regime-split thresholds show meaningful variation")
else:
    print("HMM DEFERRED — regime_signal captures all first-order regime information")
```

**Data requirement:** The test needs per-decision-cycle `regime_signal` labels on the evaluation set. If these are not already logged, add a small logging line (hours of work, not days).

---

## 4. Single Roadmap Placement with Pre-Conditions

```
Phase A ───────────────────────── (No HMM. Basic pipeline only.)
     │
     ▼
Phase B ─── Run the cheap diagnostic (regime-split combinatorial search)
     │
     ├── TEST PASSES (thresholds differ >5pp):
     │       │
     │       ▼
     │   Phase C ─── Build simplest Markov overlay
     │       │
     │       │   Pre-conditions for Phase C build:
     │       │     1. Regime-split test passed (documented thresholds)
     │       │     2. DP-GMM (or other regime discovery) has stabilized
     │       │     3. Baseline CRPS benchmark exists without HMM
     │       │     4. Analog matching pipeline fully operational
     │       │
     │       ├── Build: empirical Markov chain from regime posteriors
     │       │   (E5's approach — avoids separate HMM training, reuses
     │       │   whatever regime discovery mechanism is in place)
     │       │
     │       ├── Validate: compare CRPS with vs. without Markov overlay
     │       │
     │       ├── Decision gate: if CRPS improvement < 5% after 4 weeks,
     │       │   remove Markov layer. If ≥ 5%, keep and tune.
     │       │
     │       └── Upgrade to full HMM only if empirical chain's transition
     │           matrix shows clear residual structure (Phase 4 option)
     │
     └── TEST FAILS (thresholds < 5pp apart):
             │
             ▼
         Phase 4 ─── Revisit only after 200+ live trades accumulate.
         No Markov build in Phase C. Re-test after more data.
```

### Pre-Conditions Summary

Before any Markov code is written (Phase C entry gate):

1. **Regime-split test passed** — documented evidence that optimal thresholds differ by >5 percentage points across regimes, or Kelly fractions by >2×.
2. **Baseline benchmark exists** — CRPS and PIT metrics for the regime-signal-only pipeline are stable and documented.
3. **Analog matching pipeline is operational** — No Markov dependency before the primary pipeline works.
4. **Regime discovery has stabilized** — If using DP-GMM, the number of states and emission parameters have converged across multiple retraining cycles.
5. **`regime_signal` labels are logged** — On every decision cycle, so the test and any future HMM training have the necessary data.

---

## 5. Cheapest Implementation if Test Passes

**Adopt E5's approach: empirical Markov chain on regime posteriors.** This is strictly simpler than E1's Gaussian HMM (no separate HMM training, no new dependency on `hmmlearn`, reuses whatever regime discovery mechanism exists). The implementation is:

1. Use the existing regime discovery (DP-GMM or other) to assign most-likely regime at each timestep (Viterbi or argmax).
2. Compute the empirical transition matrix from these assignments: `P(Rₜ | Rₜ₋₁)` = count of transitions from state i to state j / total transitions from state i.
3. Compute regime dwell-time distributions from the transition matrix diagonal (mean persistence days per regime).
4. Validate with held-out log-likelihood against a persistence (no-transition) baseline.
5. Feed `Rₜ` (most likely regime) and `P(Rₜ)` (regime probabilities) into the Bayesian log-odds fusion as a regime-conditioned prior.

**Only graduate to a full Gaussian HMM (E1's approach) if:** the empirical chain captures < 5% CRPS improvement AND the held-out log-likelihood shows clear residual temporal structure that a Gaussian HMM could exploit.

---

## 6. Summary Decision

| Question | Answer |
|---|---|
| **Should we proceed?** | **Maybe** — pending a cheap diagnostic test in Phase B. |
| **What's the test?** | Regime-split combinatorial search: threshold and Kelly differences across `regime_signal` buckets. |
| **If test passes?** | Build simplest Markov overlay in Phase C (empirical chain on regime posteriors). |
| **If test fails?** | Defer to Phase 4. HMM is mathematically unnecessary if existing `regime_signal` already captures all meaningful regime variation. |
| **How to keep it simple?** | Empirical transition matrix from existing regime assignments. No full HMM, no `hmmlearn`, no new training pipeline. |
| **When to rip it out?** | If CRPS improvement < 5% after 4 weeks of Phase C production data. |

**Bottom line from E6, endorsed by all three:** *Don't build now. Run the cheap test. Let data decide.*
