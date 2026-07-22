# Expert 1 Follow-Up: Hidden Markov Models for Regime Detection

**Date:** 2026-07-21
**Author:** Gray Room — Meteo Stats Working Group
**Status:** Decision Memo
**Topic:** Should we add an HMM/Markov chain for regime detection?

---

## Verdict

**Yes, proceed — but only as a Phase C additive layer, not a Phase A or B dependency.** A lightweight 3-state HMM per station adds genuine regime-awareness that complements both the existing `regime_signal` and the analog matching pipeline. It fills a gap neither currently addresses: **temporal persistence of hidden meteorological states.** However, it should be implemented as a lightweight overlay, not a deep architectural change.

---

## 1. Compatibility with Analog Matching

They are **complementary, not conflicting.** There are three clean integration paths:

- **Pre-filter (recommended):** Use the HMM to classify current regime state (stable, transitioning, stormy). Feed that state as a categorical feature *into* the analog distance metric. Analogs are then matched only within the same regime class — reducing false analog neighbors that look similar in T2m/MSLP but belong to a fundamentally different atmospheric mode.
- **Post-hoc calibrator:** Run analog matching normally for all regimes, then use HMM regime probability as a mixing weight in the Bayesian bootstrap (regime with higher posterior mass gets more weight in the trajectory distribution).
- **Parallel signal:** Use HMM as an independent regime signal fused downstream (in `signal_fusion.py`). This adds negligible latency but provides a regime-classification feature the analog pipeline can ignore.

Path 1 (pre-filter) is the strongest option — Mahalanobis distance already captures similarity within a regime, but cross-regime analog matches are often misleading. Regime pre-filtering directly addresses the "false analog" failure mode without changing the core distance metric.

---

## 2. Value vs. Existing `regime_signal`

The existing `regime_signal` is a **deterministic threshold detector** on temperature volatility + slope (vol < 1.0, |slope| < 0.5). It has three blind spots an HMM addresses:

| Gap | Existing `regime_signal` | HMM |
|---|---|---|
| **Transient regimes** | Fires only after vol/slope thresholds are breached — no warning. | An HMM detects regime *transitions* probabilistically. The hidden state can shift to "stormy" *before* volatility spikes, not after. |
| **Multivariate blind spot** | Only uses temperature high. | Uses T, pressure, and wind simultaneously — a passing low-pressure system with stable temperature is missed entirely by the current signal. |
| **No state memory** | Each day is evaluated independently. | Markovian state persistence means once in "stormy," you stay likely stormy until evidence accumulates for a transition. This matches real atmospheric behavior (fronts linger 2–5 days). |

The HMM does **not** replace `regime_signal` — it adds probabilistic, multivariate, memory-aware regime classification that the existing signal fundamentally cannot provide.

---

## 3. Where It Fits in Phase A/B/C

| Phase | Priority | Action | Rationale |
|---|---|---|---|
| **Phase A** | ❌ Skip | Don't build yet. | Phase A should focus on getting analog matching basic pipeline running: Mahalanobis distance, N selection, BB-KS. HMM adds no value during pipeline debugging. |
| **Phase B** | ⏸ Optional | Prototype offline. | If Phase A achieves CRPS targets without regime awareness, HMM is a nice-to-have. If false-analog contamination is > 10% of CRPS error, accelerate. |
| **Phase C** | ✅ Build | Integrate HMM pre-filter into analog retrieval. | This is where the HMM earns its keep — regime-conditional analog matching + regime-as-a-feature in the distance metric. |

**Recommended build order:** Analog pipeline (Phase A) → Calibration + validation (Phase B) → HMM pre-filter (Phase C).

---

## 4. Simplest Useful Implementation

**3-state Gaussian HMM per station on (T2m, MSLP, wind_speed), 1-hour resolution.**

```python
# Core: ~40 lines using hmmlearn
from hmmlearn import hmm
import numpy as np

def build_regime_hmm(obs: np.ndarray, n_states: int = 3) -> hmm.GaussianHMM:
    """
    obs: (n_timesteps, 3) array of [T2m, MSLP, wind_speed]
    Returns fitted GaussianHMM with 3 hidden states.
    """
    model = hmm.GaussianHMM(
        n_components=n_states,
        covariance_type="diag",       # diagonal = fast, captures per-variance
        n_iter=100,
        random_state=42,
    )
    model.fit(obs)
    return model

# State labels inferred from means:
# State 0: stable (low T variance, moderate MSLP, low wind)
# State 1: transitioning (moderate T, falling MSLP, rising wind)
# State 2: stormy (high T variance, low MSLP, high wind)
```

**Why this works and fits:**

- **Station-level:** Each station gets its own HMM. Training is independent and embarrassingly parallel — 20 stations × ~100ms fit = ~2 seconds total.
- **3 states** is the minimum useful: stable / transitioning / stormy. Adding more states (4–5) increases parameter count without clear benefit for this problem.
- **Gaussian emissions** are appropriate for T2m, MSLP, and wind speed — all approximately Gaussian at hourly resolution after mild outlier clipping.
- **hmmlearn** is already in standard Python ML stacks. No exotic dependencies.

**Training protocol:** Sliding window of 90 days, refit every 7 days. Emission parameters drift seasonally; a 90-day window avoids stale distributions while maintaining enough data for EM convergence.

**Output at inference time:** For each station at each forecast cycle, output the posterior probability of each regime state:

```json
{
  "station": "KNYC",
  "forecast_cycle": "2026-07-21T12:00Z",
  "regime_probs": {
    "stable": 0.89,
    "transitioning": 0.10,
    "stormy": 0.01
  },
  "most_likely_regime": "stable"
}
```

---

## 5. Overcomplication Risk

**The risk is real but containable.** Three failure modes to avoid:

| Failure Mode | When It Happens | Mitigation |
|---|---|---|
| **Too many states** | Someone adds 5+ hidden states "to be thorough." Curse of dimensionality on transition matrix. | Enforce 3 states. Add a 4th only if 3-state log-likelihood plateaus with clear residual structure. |
| **HMM as crutch** | Team stops tuning analog matching because "HMM will fix it." | HMM is an overlay, not a replacement. Analog matching bears the primary predictive weight. |
| **Real-time computational debt** | Re-fitting HMM every hour per station on growing data. | Fixed 90-day sliding window with 7-day refit = ~200 observations × 20 stations × once weekly. Trivial. No per-minute compute burden. |

**Stop condition:** Benchmark the analog pipeline with and without HMM pre-filter. If CRPS improvement < 5% after 4 weeks of Phase C production data, rip it out. The HMM should earn its keep against a measurable baseline.

**Worst case:** You implement ~100 lines of code plus one `pip install hmmlearn`, it doesn't improve metrics, and you delete it. Risk: small. Upside: meaningful.

---

## Summary Decision

| Question | Answer |
|---|---|
| Compatible with analog matching? | Yes — best as a regime pre-filter for analog retrieval. |
| Adds value over existing `regime_signal`? | Yes — multivariate + probabilistic state transitions + temporal memory. |
| Phase placement? | **Phase C only.** Not a Phase A/B dependency. |
| Simplest implementation? | 3-state Gaussian HMM per station (T2m, MSLP, wind_speed) via `hmmlearn`. |
| Overcomplication risk? | Real but containable — benchmark against analog-only baseline; remove if < 5% CRPS improvement. |

**Go ahead in Phase C.** Don't let it distract from getting the analog matching pipeline live in Phase A.
