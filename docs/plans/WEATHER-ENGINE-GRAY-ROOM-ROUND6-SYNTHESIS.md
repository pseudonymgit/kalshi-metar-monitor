# Gray Room Round 6 — NWP-METAR Fusion & Probabilistic Trajectory

**Date:** 2026-07-21
**Panel:** 6 experts
**Moderator:** Donna Paulsen

---

## The Advisory Question

We have two independent signal lanes:
- **METAR lane** — 9 observation-based signals at ~72% accuracy
- **NWP lane** — 5-model forecast ensemble (GFS, ECMWF, ICON, GEM, ERA5), 18+ months, 20 stations, 21 variables

We want to build a fusion architecture that produces probabilistic trajectory predictions (not just binary "up/down") and uses both lanes to inform trading decisions.

---

## Expert Dispositions

| Expert | Domain | Output | Disposition |
|---|---|---|---|
| E1 | Meteorological Statistics | Probabilistic trajectory from analog matching | **ADVANCE** |
| E2 | Signal Fusion / Quant Finance | Bayesian log-odds fusion > OR gate | **ADVANCE** |
| E3 | Operational Meteorology | Forecast horizon reliability per variable | **ADVANCE** |
| E4 | Behavioral Economics / Market Micro | Market microstructure edge analysis | **ADVANCE** |
| E5 | Information Theory / UQ | Uncertainty quantification framework | **ADVANCE** |
| E6 | Decision Theory / OR | Optimal decision framework | **ADVANCE** |

**All 6 experts ADVANCE.** No KILLs. No PARKs.

---

## Key Consensus Points

### 1. FUSION ARCHITECTURE — Log-odds Bayesian, not OR gate

All experts agree: the OR gate is a reasonable **baseline** but mathematically suboptimal. The correct fusion approach is **Bayesian log-odds**:

```
logit(P(truth | METAR, NWP)) = logit(prior) + LL_metar + LL_nwp
```

Where `LL` = log-likelihood ratio of each lane.

**Why:** OR gate ignores prior probability, doesn't handle correlation properly, and doesn't have a principled disagreement resolution mechanism.

**Disagreement handling:** When NWP says UP and METAR says DOWN, the posterior collapses to ~prior (≈0.55). After the 3.1¢ spread, this is negative EV. **Skip the trade** — unanimous consensus.

**Both-agree boost:** ~+32pp (from 72% to ~78%), computed by Bayes, not ad-hoc addition.

### 2. CLIMATOLOGY RESIDUALIZATION (Critical Fix from E2)

Both METAR (calendar_climatology) and NWP share a seasonal baseline — they'll both say "July is hot." This violates the conditional independence assumption and causes overconfidence.

**Fix:** Decompose `calendar_climatology = NWP_seasonal_baseline + residual`. Use only the residual in the METAR lane. Expected: **+2-3pp** on fused accuracy, ~30 lines of code.

### 3. PROBABILISTIC TRAJECTORY METHODOLOGY (E1 + E5)

| Component | Method | Rationale |
|---|---|---|
| Distance metric | Mahalanobis on PCA-compressed (6-8 dims) 21-variable fingerprint | Accounts for variable correlation |
| Analog count N | CRPS-driven optimization: scan k=10..500, pick min CRPS via 5-fold CV | Data-driven, not arbitrary |
| Distribution output | Bayesian bootstrap (N resampled trajectories) | Handles small-N, produces full CDF |
| Multi-modality | Bayesian GMM with Dirichlet Process | Automatically handles bimodal weather outcomes |
| Calibration | Probability Integral Transform (PIT) histogram + isotonic regression on CDF quantiles | Ensures distribution is well-calibrated |
| Fallback: no good analog | Fall back to climatology distribution from same calendar window | Better than skipping |

### 4. FORECAST HORIZON HIERARCHY (E3)

| Horizon | METAR | NWP | Fusion strategy |
|---|---|---|---|
| 0-6h | Dominant | Weak (t=0 spin-up) | METAR-primary, NWP as sanity check |
| 6-12h | Good | Good | **Optimal fusion window** — both lanes contribute |
| 12-24h | Weakening | Strong | NWP-primary, METAR as trend confirmation |
| 24-48h | Dead | Moderate (seasonal) | NWP-only, reduced confidence |
| 48h+ | Irrelevant | ECMWF/GFS out to 168h for temp | Not actionable for Kalshi daily markets |

**Model ranking (all horizons):** ECMWF > GFS ≈ ICON > GEM > ERA5

### 5. MISSING VARIABLES (E3)

Would improve NWP accuracy by 10-15% if collected:
- **Surface solar radiation** — primary driver of daytime max. GFS has this, we're not storing it.
- **Boundary layer height** — determines how much mixing occurs
- **Snow depth** — massive albedo effect on temps
- **Soil moisture** — affects diurnal temperature range through evapotranspiration

### 6. MARKET MICROSTRUCTURE EDGE (E4)

**Key finding:** The market **overweights NWP** (anchoring bias, narrative fallacy) and **underweights real-time METAR**. Divergence scenarios (NWP says 35°C, METAR reads 37°C) create 30¢+ arbitrage opportunities.

**Signal freshness:** NWP forecast half-life ~6h, METAR observation half-life ~2h. Add a **Signal Freshness Weighting** module that gates trading mode based on how recently each lane was updated.

**Execution timing:** GFS releases at ~06Z/18Z, ECMWF ~12Z. Alpha decays fastest in major cities (~90 min half-life), slowest in minor cities (~6h). Build an "NWP freshness timer" — trade aggressively right after NWP update, taper off.

### 7. DECISION FRAMEWORK (E6)

- **Separate per-lane thresholds:** Lower for METAR (~55% confidence), higher for NWP (~60-65%)
- **Three-tier sizing:** Both agree → full Kelly (0.16× at 1/4 Kelly), single lane → half Kelly, conflict → zero
- **Climatology fallback lane:** Trade market-vs-historical-priors at 0.1× Kelly when neither lane fires and deviation >15pp
- **Primary metric:** **Combined Sharpe** vs best single lane. Supporting: win rate by signal type, lift, false-trade/missed-trade rates, Brier calibration error

### 8. EXPECTED ACCURACY IMPROVEMENT (all experts)

| Improvement | Expected gain | Source |
|---|---|---|
| Calibration (Platt/isotonic) | +2-5pp fused accuracy | E6 |
| Climatology residualization | +2-3pp fused accuracy | E2 |
| Signal freshness weighting | +3-5% accuracy on stale-aware trades | E4 |
| Mahalanobis + PCA analog matching | +3-5pp NWP standalone | E1 |
| Missing variable collection | +10-15% NWP accuracy potential | E3 |
| **Total fused (conservative)** | **~76-80% combined accuracy** | All |

---

## Recommended Build Order

### Phase A: Foundation (1-2 days)
1. Build NWP standalone signal using Mahalanobis + PCA analog matching (E1)
2. Train isotonic calibration for both lanes independently (E6)
3. Run NWP standalone accuracy evaluation across 20 stations + all horizons

### Phase B: Fusion (2-3 days)
4. Implement climatology residualization (E2)
5. Build Bayesian log-odds fusion module
6. Implement three-tier Kelly sizing (E6)
7. Signal freshness weighting with NWP update timers (E4)

### Phase C: Production (3-5 days)
8. Collect missing variables (solar radiation, boundary layer, snow depth, soil moisture) (E3)
9. Implement continuous CDF trajectory output with calibration (E5)
10. Backtest fused system against best single lane
11. Purged CV on fused system

---

## Raw Expert Outputs

Full expert files at: `docs/plans/gray-room-round6/`
- E1: `expert1-meteo-stats.md` (286 lines)
- E2: `expert2-signal-fusion.md` (290 lines)
- E3: `expert3-meteorology.md` (386 lines)
- E4: `expert4-behavioral-market-micro.md` (241 lines)
- E5: `expert5-uncertainty-quantification.md` (350 lines)
- E6: `expert6-decision-theory.md` (310 lines)

---

## Route For

- **Gilfoyle** — Phase A & B implementation (technical execution)
- **Marty Byrde** — Capital allocation for Phase B sizing framework
- **Gerri Kellman** — Overall Phase sequencing and risk assessment