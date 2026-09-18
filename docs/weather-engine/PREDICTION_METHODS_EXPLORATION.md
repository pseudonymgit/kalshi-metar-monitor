# Prediction Methods Exploration

**Generated:** 2026-08-10 14:28 UTC
**Source:** Database inventory, existing signal codebase, sweep results
**Author:** B-Mode Sweep Buildout (Task 8)

---

## Executive Summary

The current GEFS-based signal framework achieves 51.7% baseline accuracy (0.98 Sharpe) with the 15-signal ensemble, improving to 66.7% accuracy (5.74 Sharpe) with the DJF/continental/12z regime filter. However, **ecmwf_bias_corrected** alone achieves **86.1% accuracy** (16.2 Sharpe), demonstrating that the best path forward is to leverage additional NWP model data and advanced prediction methods rather than adding more derivatives of the same GEFS ensemble.

This document explores 7 prediction methods ranked by feasibility × expected impact. The highest-priority recommendation is **Method 3 (Regime-Switching HMM)** because it can be built entirely with existing data and directly addresses the biggest known weakness: the JJA performance collapse (47.2% acc, -1.71 Sharpe).

---

## Method 1: Kalman Filter on METAR Data

### Method Description
A Kalman filter provides real-time state estimation of temperature given noisy METAR observations. The state evolves as:
- **State vector**: [temperature, dT/dt] at each station
- **Observation model**: METAR temperature readings with Gaussian noise (σ≈0.5°C)
- **Process model**: Temperature follows a random walk with drift, with diurnal cycle as control input
- **Output**: Filtered temperature estimate + prediction confidence at settlement time (18:00 ET)

The Kalman filter would:
1. Ingest 1-minute METAR data from `metar_backfill.db` (12.8M observations)
2. Track real-time temperature with sub-hourly resolution
3. Produce an updated temperature forecast that converges toward actual as settlement approaches
4. Generate a directional signal based on the gap between GEFS forecast and Kalman-filtered estimate

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| 1-minute METAR observations | ✅ | `metar_backfill.db` | 12,820,915 |
| Station climatology (diurnal curves) | ✅ | `six_hour_aggregates`, `station_climatology` | ~5K |
| GEFS forecast as prior | ✅ | `gefs_archive.db` | 365,960 |

### Expected Impact
**Estimated accuracy improvement:** +2-5pp overall, +5-10pp in intraday nowcasting

The Kalman filter offers diminishing returns the closer to settlement (METAR data already converges to truth). The main value is **intraday confirmation** — when METAR readings diverge from the GEFS forecast, the Kalman filter detects regime shifts faster than daily aggregation can.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| Design & math derivation | 4h | Define state/observation/process models, tune noise params |
| Implementation | 8h | Kalman filter class, prediction step, update step |
| Integration | 4h | Wire into signal pipeline, produce directional signal |
| Testing & tuning | 4h | Backtest against historical METAR data |
| **Total** | **20h (2.5 days)** | |

### Dependencies
- **Code**: `core/signals/kalman_metar_signal.py` (new)
- **Data**: `metar_backfill.db` (exists)
- **Infrastructure**: None (pure computation)
- **Existing code**: Can leverage `MetarTrendSignal` patterns

### Risk
LOW. Pure math, deterministic. No ML/AI gate triggers.

---

## Method 2: ECMWF IFS Direct Signal

### Method Description
The `tigge_archive.db` contains **181,400 ECMWF IFS ensemble forecasts** (50 members) across 20 stations from 2021-08-19 to 2026-08-06. This data is already in the codebase with member-level binary encoding. A direct ECMWF signal would:
1. Decode the 50-member ensemble to compute:
   - Ensemble mean temperature
   - Ensemble spread (confidence measure)
   - Probability of temperature increase/decrease (fraction of members going each direction)
   - Extreme member detection (members in tails)
2. Apply bias correction (ECMWF has known warm/cold biases depending on station)
3. Score confidence based on ensemble agreement (low spread = high confidence)

This is similar to the `ecmwf_bias_corrected` signal (86.1% acc) but would:
- Use **raw member data** from tigge_archive instead of bias-corrected values
- Compute **true ensemble probabilities** (50 members vs aggregated mean)
- Enable **member-level analytics** (ESDR, extreme value detection)

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| ECMWF IFS 50-member ensemble | ✅ | `tigge_archive.db` | 181,400 |
| Station metadata (coordinates, elevation) | ✅ | `station_metadata.json` | 20 |
| Settlement data for validation | ✅ | `kalshi_settlements.db` | 6,311 |

### Expected Impact
**Estimated accuracy improvement:** +10-15pp over baseline, comparable to ecmwf_bias_corrected (86.1%)

The key advantage over the existing ECMWF signal: **50 members** provide granular probability estimates. The GEFS ensemble only has 6-31 members depending on era. More members → better calibrated probabilities → higher Sharpe.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| Member decoding | 3h | Parse binary `member_values` field, convert to float array |
| Signal implementation | 6h | Directional prob, confidence scoring, bias correction |
| Integration | 4h | Register in signal pipeline, add to smoothing |
| Testing | 4h | Backtest against tigge_archive dates |
| **Total** | **17h (2 days)** | |

### Dependencies
- **Code**: `core/signals/tigge_ecmwf_signal.py` (new)
- **Data**: `tigge_archive.db` (exists, 54.8MB)
- **Infrastructure**: None
- **Reference**: Existing `ecmwf_bias_corrected_signal.py` patterns

### Risk
LOW. Data exists, code patterns exist. Need to handle binary member decoding correctly.

---

## Method 3: Regime-Switching Models (HMM)

### Method Description
A **hidden Markov model (HMM)** or **threshold autoregression (TAR)** that detects regime changes in temperature time series. The key insight from the sweep is:

> DJF: 66.7% acc, +$713 — WINTER WORKS
> JJA: 47.2% acc, -$264 — SUMMER LOSES MONEY

Rather than using a hard seasonal filter (DJF only), a regime-switching model would:
1. Learn **latent regimes** (e.g., "frontal passage", "stagnant high", "convective", "marine layer") from METAR data patterns
2. Assign probability to each regime at current time
3. Select the appropriate prediction model per regime:
   - **Frontal regime**: Use pressure_delta + wind_direction_shift signals
   - **Stagnant regime**: Use persistence + climatology
   - **Convective regime**: Reduce position sizes, higher confidence threshold
4. Output: regime-weighted prediction with confidence score

Implementation options:
- **HMM**: 3-5 hidden states, Gaussian observation model on (temp, pressure, humidity, wind)
- **TAR**: Threshold model where forecast mode switches based on pressure gradient or CAPE proxy
- **Markov-switching**: Regression coefficients switch based on unobserved regime state

The codebase already has a `regime_signal.py` and `seasonal_regime` signal, plus `phase12_2_markov_results.json` suggesting prior Markov model experiments.

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| METAR observations (temp, pressure, wind, humidity) | ✅ | `metar_backfill.db` | 12.8M |
| Station climatology | ✅ | `station_climatology` | 4,774 |
| GEFS ensemble features | ✅ | `gefs_archive.db` | 365,960 |
| Historical regime analysis | Partial | `phase12_2_markov_results.json` | 1 file |
| Settlement outcomes | ✅ | `kalshi_settlements.db` | 6,311 |

### Expected Impact
**Estimated accuracy improvement:** +5-10pp overall, JJA recovery from -47.2% to 55%+

This directly addresses the single biggest underperformance in the system. If summer performance can be brought to breakeven or slightly positive, annualized returns improve significantly.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| Feature engineering | 6h | Extract regime-relevant features from METAR |
| HMM implementation (hmmlearn or custom) | 8h | Fit 3-5 state HMM, Viterbi decoding |
| TAR implementation | 4h | Threshold discovery, regime assignment |
| Integration & calibration | 6h | Weight signals by regime probability |
| Testing | 4h | Walk-forward validation per regime |
| **Total** | **28h (3.5 days)** | |

### Dependencies
- **Code**: `core/regime_switching.py` (new), or `hmmlearn` package
- **Data**: All exists
- **Infrastructure**: `hmmlearn` pip install (pure Python, no binary deps)
- **Existing code**: Can reuse `regime_signal.py` and `seasonal_regime` patterns

### Risk
MEDIUM. HMMs are well-understood but require careful feature selection. Risk of overfitting regimes that don't generalize.

---

## Method 4: Bayesian Structural Break Detection

### Method Description
Bayesian change-point detection identifies **structural breaks** in temperature time series — moments when the underlying data-generating process changes. Unlike HMM (which has discrete regimes), structural break models can detect:
- Gradual shifts (urban heat island effect at KNYC)
- Sudden jumps (cold front passage)
- Variance changes (convective vs stable conditions)

Implementation via **PyMC** or **Ruptures** library:
1. Compute cumulative temperature statistics (CUSUM)
2. Apply Bayesian change-point detection (binary segmentation or PELT)
3. Detect break points in:
   - Mean temperature (urban heat island, seasonal transitions)
   - Temperature variance (frontal activity, convective conditions)
   - Forecast error variance (model skill changes)
4. Use detected breaks to:
   - Adjust confidence floor (higher during break periods = uncertainty)
   - Switch signal weights (different signals work before vs after cold fronts)
   - Trigger regime transitions for the HMM

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| METAR temperature history | ✅ | `metar_backfill.db` | 12.8M |
| ERA5 reanalysis (ground truth) | ✅ | `era5_archive.db` | 40,760 |
| NWP forecast errors | ✅ | GEFS/ECMWF archives | 365-547K |

### Expected Impact
**Estimated accuracy improvement:** +1-3pp overall

Bayesian breaks are primarily a **confidence modulation** technique rather than a primary predictor. The main value is in detecting when the market regime has changed (e.g., cold front just passed), allowing the system to switch from persistence-based to pressure-trend-based prediction.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| CUSUM & segmentation setup | 4h | Compute cumulative stats, baseline detection |
| Bayesian model implementation | 8h | PyMC model or Ruptures integration |
| Signal integration | 4h | Wire break detection into confidence scoring |
| Testing | 4h | Validate break detection accuracy |
| **Total** | **20h (2.5 days)** | |

### Dependencies
- **Code**: `core/bayesian_break_detector.py` (new)
- **Data**: All exists
- **Infrastructure**: `ruptures` or `pymc` pip install
- **Existing code**: Can integrate with existing confidence modulation

### Risk
MEDIUM-HIGH. Bayesian models are computationally expensive. PELT (Pruned Exact Linear Time) is faster but less flexible. May not provide enough marginal benefit over simple moving-window statistics.

---

## Method 5: NWP Analog Ensemble (ECMWF IFS Members)

### Method Description
An **analog ensemble** (AnEn) finds historical ECMWF IFS forecasts that are most similar to the current forecast, and uses their observed outcomes to predict the current outcome. This is different from the existing `NwpAnalogSignal` (which uses k-NN on NWP features from `nwp_forecasts.db`).

The ECMWF IFS analog approach would:
1. For each current ECMWF forecast vector (50-member ensemble mean, spread, skew, extremes):
2. Search `tigge_archive.db` for historical ECMWF forecasts with similar characteristics
3. Match within ±30 days (seasonal window) and ±5°C of current forecast
4. Retrieve the **actual METAR outcomes** for top-K analogs
5. Compute directional probability from observed outcomes
6. Weight by analog similarity (cosine distance or Euclidean)

Advantages over current NWP analog:
- **50 ECWMF members** vs smaller GEFS ensemble → more granular
- **5+ years of data** (2021-2026) → 1,825+ forecast dates × 20 stations
- **Member-level** → can compute ensemble ESDR, spread, extreme probabilities

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| ECMWF IFS 50-member forecasts | ✅ | `tigge_archive.db` | 181,400 |
| METAR observations (outcomes) | ✅ | `metar_backfill.db` | 12.8M |
| Station metadata | ✅ | `station_metadata.json` | 20 |

### Expected Impact
**Estimated accuracy improvement:** +8-12pp over baseline (GEFS-based)

This is the most direct replacement for the current GEFS ensemble. The 50-member ECMWF ensemble is more skillful than the 6-31 member GEFS. The analog approach provides a completely independent prediction stream.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| Feature extraction from tigge_archive | 4h | Compute analog features from 50-member ensemble |
| Analog search implementation | 6h | K-D tree or brute-force seasonal window search |
| Probability computation | 4h | Beta-binomial estimate with similarity weighting |
| Integration | 4h | Register as signal, wire into pipeline |
| Testing | 6h | Cross-validate analog quality, backtest |
| **Total** | **24h (3 days)** | |

### Dependencies
- **Code**: `core/signals/ecmwf_analog_signal.py` (new)
- **Data**: `tigge_archive.db` (exists, 54.8MB)
- **Infrastructure**: None (K-D tree from scipy)
- **Existing code**: Can reuse `NwpAnalogSignal` architecture

### Risk
LOW-MEDIUM. Deterministic k-NN. The main risk is analog quality — ECMWF forecasts may have too few similar historical analogs at 50-member resolution.

---

## Method 6: Open-Meteo Multi-Model Ensemble

### Method Description
The `nwp_forecasts.db` contains **461,980 forecasts** from **6 models** (era5, ecmwf, gem, gfs, icon, gefs_ens) across 20 stations. This is the richest NWP data asset in the codebase.

A multi-model ensemble would:
1. For each forecast date, collect all available model predictions:
   - ECMWF (operational)
   - GFS (operational)
   - ICON (German Weather Service)
   - GEM (Canadian)
   - ERA5 (reanalysis — historical only)
   - GEFS ensemble mean
2. Compute multi-model statistics:
   - **Multi-model mean**: Average of all available models
   - **Multi-model spread**: Variance between models (uncertainty proxy)
   - **Consensus score**: Fraction of models agreeing on direction
   - **Model-specific weights**: Historical skill per model per station (learned)
3. Apply Bayesian model averaging (BMA):
   - Weight models by historical Brier score per station
   - Dynamic weighting based on recent forecast skill (rolling 90-day window)
4. Output: consensus directional probability with confidence

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| Multi-model NWP forecasts | ✅ | `nwp_forecasts.db` | 461,980 |
| METAR observations (outcomes) | ✅ | `metar_backfill.db` | 12.8M |
| Station-level calibration | ✅ | Existing calibration pipeline | Various |
| Historical model error | Partial | `ecmwf_bias.db`, `hrrr_bias.db` | 2 files |

### Expected Impact
**Estimated accuracy improvement:** +5-10pp over single-model (GEFS baseline), competitive with ECMWF alone

Multi-model ensembles consistently outperform single models in NWP literature (Molteni et al., Lorenz). The 6-model spread provides a natural confidence measure — when models agree, confidence is high.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| Data pipeline for multi-model extraction | 4h | Query nwp_forecasts.db, align on common dates |
| Model weighting (BMA) | 8h | Bayesian model averaging implementation |
| Consensus computation | 4h | Agreement fraction, spread-based confidence |
| Integration & calibration | 6h | Wire into pipeline, per-station calibration |
| Testing | 6h | Backtest against historical outcomes |
| **Total** | **28h (3.5 days)** | |

### Dependencies
- **Code**: `core/signals/multi_model_ensemble_signal.py` (new, or finish existing `multi_model_ensemble.py`)
- **Data**: `nwp_forecasts.db` (exists, 146.7MB)
- **Infrastructure**: None
- **Existing code**: `multi_model_ensemble.py` already partially built (PENDING status in registry)

### Risk
LOW. Multi-model ensembles are well-understood. The data exists. There's already a partial implementation (`core/multi_model_ensemble.py`). The main effort is finishing and validating the existing code.

---

## Method 7: ERA5 Reanalysis Temperature Normals

### Method Description
ERA5 reanalysis (Copernicus) provides the most accurate historical temperature record — it's a physics-based reanalysis that assimilates all available observations. The `era5_archive.db` has **40,760 daily records** from 2021-01-01 to 2026-07-31 across 20 stations.

Usage as a signal:
1. Compute **ERA5 temperature normals** (30-day rolling mean, 14-day rolling mean)
2. Compute **ERA5 anomaly** = current ERA5 temperature - ERA5 normal
3. Compare with **GEFS forecast anomaly** = GEFS forecast - ERA5 normal
4. Signal: if GEFS forecast anomaly differs significantly from ERA5 current anomaly:
   - If GEFS predicts warming but ERA5 is cooling → SELL (overestimate)
   - If GEFS predicts cooling but ERA5 is warming → BUY (underestimate)
5. This is a **forecast bias correction** signal, not a standalone predictor

### Data Needed
| Data | Exists? | Source | Rows |
|------|---------|--------|:----:|
| ERA5 daily temperatures | ✅ | `era5_archive.db` | 40,760 |
| GEFS forecasts | ✅ | `gefs_archive.db` | 365,960 |
| Station metadata | ✅ | `station_metadata.json` | 20 |

### Expected Impact
**Estimated accuracy improvement:** +1-3pp (as a bias correction signal)

ERA5 is the most accurate historical temperature record, but it's retrospective — it won't be available in real-time. The main value is for **calibration and validation** rather than real-time prediction. It can improve bias correction of GEFS forecasts.

### Build Effort
| Phase | Hours | Description |
|-------|:-----:|-------------|
| ERA5 normal computation | 3h | Rolling means, seasonal normalization |
| Bias correction integration | 4h | Compare GEFS anomaly vs ERA5 anomaly |
| Signal implementation | 3h | Directional signal from anomaly gap |
| Testing | 3h | Validate against backtest data |
| **Total** | **13h (1.5 days)** | |

### Dependencies
- **Code**: `core/signals/era5_bias_signal.py` (new)
- **Data**: `era5_archive.db` (exists, 3.1MB)
- **Infrastructure**: None

### Risk
LOW. Simple computation. The main concern is that ERA5 is reanalysis (not real-time), so this signal can only be used for calibration/validation, not live trading.

---

## Ranking by Feasibility × Expected Impact

| Rank | Method | Feasibility (1-5) | Impact (1-5) | Score | Build Effort | Priority |
|:----:|--------|:-----------------:|:------------:|:-----:|:------------:|:--------:|
| **1** | **Regime-Switching HMM** | 5 | 5 | **25** | 28h (3.5 days) | 🏆 HIGHEST |
| **2** | **ECMWF IFS Direct Signal** | 5 | 4 | **20** | 17h (2 days) | HIGH |
| **3** | Multi-Model Ensemble (Open-Meteo) | 5 | 4 | **20** | 28h (3.5 days) | HIGH |
| **4** | NWP Analog Ensemble (ECMWF) | 4 | 4 | **16** | 24h (3 days) | MEDIUM |
| **5** | Kalman Filter on METAR | 4 | 3 | **12** | 20h (2.5 days) | MEDIUM |
| **6** | Bayesian Structural Breaks | 3 | 2 | **6** | 20h (2.5 days) | LOW |
| **7** | ERA5 Temperature Normals | 5 | 1 | **5** | 13h (1.5 days) | LOW |

### Feasibility Scoring

| Score | Meaning |
|:-----:|---------|
| 5 | Data exists, code patterns exist, no ML/AI, low risk |
| 4 | Data exists, moderate implementation effort |
| 3 | Data exists but needs significant processing |
| 2 | Data partial, high model risk |
| 1 | Missing data, unproven approach |

### Impact Scoring

| Score | Meaning |
|:-----:|---------|
| 5 | Addresses biggest known weakness (JJA collapse) |
| 4 | Would become primary prediction stream |
| 3 | Meaningful improvement to existing pipeline |
| 2 | Incremental improvement |
| 1 | Calibration/validation only |

---

## Implementation Roadmap

### Phase 1 (Week 1): Regime-Switching HMM
- Extract regime features from METAR (temp variance, pressure tendency, humidity, wind)
- Fit 3-5 state HMM on full history
- Validate regime assignments against known weather patterns
- Wire into confidence scoring (higher floor during convective regimes)
- **Expected: +3-5pp JJA recovery**

### Phase 2 (Week 2): ECMWF IFS Direct
- Decode 50-member `member_values` from tigge_archive.db
- Build signal with full ensemble probability scoring
- Bias correct per station
- **Expected: 80-86% accuracy match to existing ECMWF signal**

### Phase 3 (Week 3): Multi-Model Ensemble
- Finish existing `multi_model_ensemble.py` implementation
- Compute model weights from historical Brier scores
- Build consensus scoring with spread-based confidence
- **Expected: +5-10pp over GEFS baseline**

### Phase 4 (Week 4-5): NWP Analog Ensemble & Kalman Filter
- Build ECMWF analog signal using tigge member data
- Implement Kalman filter for intraday nowcasting
- Test and validate both

### Deferred: Bayesian Breaks & ERA5 Normals
- Build when regime-switching models need structural break detection for regime transitions
- ERA5 normals can be computed in 1 day as a calibration improvement

---

## Summary

The highest-impact, lowest-risk prediction method to build next is the **Regime-Switching HMM**, because:
1. **All data exists** — 12.8M METAR observations + full NWP archives
2. **Addresses the #1 problem** — JJA performance collapse (-47.2% acc)
3. **Complements all other methods** — regime probabilities can gate any signal
4. **No ML/AI gate** — HMMs are statistical models, not ML (unless we count hmmlearn as ML)
5. **Quickest path to improvement** — 3.5 days build, 5-10pp potential accuracy improvement

The **ECMWF IFS Direct Signal** and **Multi-Model Ensemble** are close seconds, leveraging existing data that's already proven to work (ECMWF bias-corrected: 86.1% acc). All three can be built in parallel.

---

*Analysis performed by B-Mode Sweep Buildout (Tasks 8). Source: Database inventory (`tigge_archive.db`, `nwp_forecasts.db`, `era5_archive.db`, `metar_backfill.db`, `gefs_archive.db`), sweep results (`data/per_parameter_results.json`), existing signal codebase.*