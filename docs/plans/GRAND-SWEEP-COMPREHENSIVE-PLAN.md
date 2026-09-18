# Grand Sweep Comprehensive Plan

**Date:** 2026-08-10
**Author:** Donna Paulsen
**Purpose:** Complete taxonomy, architecture, testing framework, and calibration plan for the weather engine sweep.
**Status:** Draft for Pseudonym review → Gray Room expert design review

---

## Part 1: Complete Component Taxonomy

### 1.1 Data Sources

| Database | Schema | Rows | Date Range | Completeness | Freshness | Used By |
|----------|--------|------|------------|--------------|-----------|---------|
| `metar_backfill.db` | 1-minute METAR observations (temp, pressure, wind, humidity) | 12,820,915 | ~2023-2026 | 19 stations, variable coverage | Static (backfill) | Trajectory, HMM, Live METAR |
| `gefs_archive.db` | GEFS ensemble members, temperature forecasts | 365,960 | ~2021-2026 | 20 stations, 6-31 members | Daily cron | All signals (baseline) |
| `tigge_archive.db` | ECMWF IFS 50-member ensemble | 181,400 | 2021-08 to 2026-08 | 20 stations, 50 members | Static | ECMWF IFS Direct, NWP Analog |
| `nwp_forecasts.db` | Multi-model NWP (ECMWF, GFS, ICON, GEM, ERA5, GEFS) | 461,980 | ~2021-2026 | 20 stations, 6 models, variable | Daily cron | Multi-Model Ensemble |
| `era5_archive.db` | ERA5 reanalysis daily temps | 40,760 | 2021-01 to 2026-07 | 20 stations | Static | ERA5 Normals, calibration |
| `kalshi_settlements.db` | Kalshi settlement prices and outcomes | 6,311 | ~2022-2026 | 20 stations, 40 markets | Daily | Validation, settlement accuracy |
| `iem_asos_1min.db` | IEM ASOS 1-minute raw observations | 754 MB | ~2023-2026 | 19 stations, 1-min resolution | Static | Goldilocks, microstructure |
| `forecast_disagreement_live.db` | NWS vs GFS disagreement | ~21/day | Current | 21 stations | Live daily | forecast_disagreement signal |
| `whale_watch.db` | Polymarket whale detection data | Variable | Current | All markets | Live | WhaleWatch conviction |
| `ecmwf_bias.db` | ECMWF bias correction factors | ~400 | 2021-2026 | Per station | Static | ecmwf_bias_corrected signal |
| `hrrr_bias.db` | HRRR bias correction factors | ~400 | Live | Per station | Live updates | hrrr_bias_corrected signal |

### 1.2 Signals — Complete Registry (43 files)

#### Active Signals (15 firing)

| Signal | File | Data Source | Purpose | Individual Acc | Status |
|--------|------|-------------|---------|:--------------:|--------|
| **gaussian** | `gaussian_signal.py` | GEFS ensemble | Temp using Gaussian model | 59.7% | Active |
| **gaussian_v2** | `gaussian_v2_signal.py` | GEFS ensemble | Improved Gaussian | 62.0% | Active |
| **pressure_delta** | `pressure_delta_signal.py` | GEFS pressure | Pressure change → temp | 63.4% | Active |
| **corrected_pressure_delta** | `corrected_pressure_delta.py` | GEFS + correction | Pressure delta with bias fix | 63.4% | Active |
| **forecast_disagreement** | `forecast_disagreement_signal.py` | NWS vs GFS | Disagreement edge | 66.3% | Active (geo-gap) |
| **calendar_climatology** | `calendar_climatology_signal.py` | Historical GEFS | DOY climatology | 65.6% | Active |
| **seasonal_regime** | `seasonal_regime_signal.py` | Historical data | Season-based classification | 66.7% (DJF) | Active |
| **ecmwf_bias_corrected** | `ecmwf_bias_corrected_signal.py` | ECMWF IFS + bias corr | Best single signal | 86.1% | Active |
| **metar_trend** | `metar_trend_signal.py` | METAR observations | Last-observed temp trend | 64.7% | Active |
| **persistence** | `persistence_signal.py` | METAR | Temp persists from yesterday | 51.9% | Active |
| **simple_trend** | `simple_trend_signal.py` | METAR | Simple trend following | 51.9% | Active |
| **wind_direction_shift** | `wind_direction_shift_signal.py` | GEFS wind | Wind pattern changes | 48.4% | Active (low volume) |
| **spike_reversion** | `spike_reversion_signal.py` | GEFS + METAR | Extreme deviation reversion | 71.4% (low volume) | Active |
| **goldilocks** | `goldilocks_signal.py` | IEM ASOS 1-min | Microstructure spike detection | 72.2% (18 trades) | Active |
| **radiational_cooling** | `radiational_cooling_signal.py` | GEFS + METAR | Night cooling detection | 34.7% (daily) | Active |

#### Redundant Signals (identified by correlation analysis)

| Cluster | Signals | Mean ρ | Max ρ | Keep | Kill |
|---------|---------|:------:|:-----:|:----:|:----:|
| **A — GEFS composite** | gaussian, gaussian_v2, forecast_disagreement, calendar_climatology | 0.53-0.57 | 1.0 | gaussian_v2 | 3 killed |
| **B — METAR trend** | metar_trend, persistence, simple_trend | 0.36 | 1.0 | persistence | 2 killed |
| **C — Spike detection** | spike_reversion, radiational_cooling | 0.48-0.49 | 1.0 | spike_reversion | 1 killed |

**Geniunely unique signals** (will form 4-signal core):
- ecmwf_bias_corrected (ECMWF IFS independent forecast)
- pressure_delta (pressure change physics)
- seasonal_regime (climatological context)
- wind_direction_shift (weather pattern changes)

#### Non-Firing Signals (25 files classified)

**Category (a) — Base/Non-signal (6):**
- base_signal.py — Abstract base class
- weatherapi_base_signal.py — Abstract base
- dual_polarity_signal.py — Wrapper class (inner signals are used)
- dewpoint_depression_modulator.py — Helper function module
- frontal_passage_detector.py — Helper function module
- intraday_metar_confirmation.py — Utility class

**Category (b) — Deprecated (1):**
- frontal_detector_signal.py — Explicitly deprecated, returns (None, 0.0)

**Category (c) — Needs Data Source (7):**
- ai_composite_signal.py — Needs ai_forecasts table (collection pipeline needed)
- esdr_signal.py — Needs member_index column in nwp_forecasts (query rewrite)
- metar_nowcast_signal.py — Points to wrong DB (easy path fix to metar_backfill.db)
- hrrr_bias_corrected_signal.py — Live HRRR API only (not backtestable)
- temperature_advection_signal.py — Live GFS API only (not backtestable)
- nwp_direct_signal.py — Needs station context (evaluate_for_station wiring)
- nwp_dtdt_fusion_signal.py — Needs NWP delta data pipeline

**Category (d) — Has Bugs (1):**
- frontal_passage_nowcast_signal.py — Evaluate() signature mismatch (easy fix)

**Category (e) — Genuinely Doesn't Work (10):**
- cloud_cover_index_signal.py — Needs cloud cover field (schema change)
- cross_model_divergence_signal.py — Needs multi-model data
- eighty_two_member_ensemble_signal.py — GEFS is 31 members now (deprecate)
- feels_like_delta_signal.py — Needs wind/humidity fields
- fogr_reversion_signal.py — FOG events are rare (~1% of days)
- intraday_metar_confirmation_signal.py — Needs intraday METAR data
- metar_dtdt_signal.py — Threshold too strict
- pressure_tendency_signal.py — Duplicate of pressure_delta (58% fire rate)
- regime_signal.py — 0.1% fire rate (too low)
- volume_momentum_signal.py — Needs volume data
- settlement_arbitrage_signal.py — Needs market price data

**Signals formally retired:**
- pressure_tendency — Duplicate of pressure_delta
- eighty_two_member_ensemble — GEFS now 31 members

### 1.3 Parameters — Complete Sweep Registry (42 params)

#### Signal-Specific Parameters (Group: SignalSpecific)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `pressure_delta_window` | int | 1-5 | 3 | 3 | - |
| `wind_shift_threshold` | float | 30-180 | 90 | 90 | - |
| `cal_shrinkage_strength` | float | 0.0-0.5 | 0.0 | 0.0 | calibration_mode != 0 |
| `mod_directional_weight` | float | 0.0-1.0 | 0.3 | 0.3 | - |

#### Confidence Parameters (Group: Confidence)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `edge_threshold` | float | 0.001-0.15 | 0.03 | 0.001 | - |
| `confidence_floor` | float | 0.5-0.95 | 0.55 | 0.80 | - |

#### Gate Parameters (Group: Gates)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `agreement_n` | int | 2-8 | 4 | 4 | validation_only |
| `agreement_m` | int | 2-8 | 4 | 4 | validation_only |
| `stop_loss_kind` | int (cat) | 0/1/2 | 0 | 2 (trailing) | - |
| `calibration_mode` | int (cat) | 0/1/2/3 | 0 | ? | validation_only |

#### Lever Parameters (Group: Levers)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `position_sizing_model` | int (cat) | 0/1/2 | 0 (fixed) | 1 (kelly) | - |
| `capital_base` | float | 5K-50K | 10K | 10K | - |

#### Modulator Parameters (Group: Modulators)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `fusion_mode` | int (cat) | 0/1/2 | 0 (average) | 2 (select_best) | - |

#### Calibration Parameters (Group: Calibration)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `bin_width` | float | 0.02-0.1 | 0.05 | ? | calibration_mode != 0 |
| `window_days` | int | 30-365 | 90 | ? | calibration_mode != 0 |
| `min_samples` | int | 10-100 | 30 | ? | calibration_mode != 0 |

#### Validation Parameters (Group: Validation)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `holdout_start_frac` | float | 0.5-0.9 | 0.8 | ? | validation_only |

#### Regime Parameters (Group: Regime)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `regime_season_filter` | str (cat) | all/djf/mam/jja/son | all | djf | - |
| `regime_region_filter` | str (cat) | all/continental/subtropical/marine/arid | all | continental | - |
| `regime_cycle_filter` | str (cat) | all/00z/06z/12z | all | 12z | - |
| `goldilocks_lane_enabled` | int (bool) | 0/1 | 0 | ? | validation_only |
| `trajectory_lane_enabled` | int (bool) | 0/1 | 0 | ? | validation_only |

#### Station Parameters (Group: StationSizing)

| Parameter | Type | Range | Default | Optimal | Dependency |
|-----------|------|-------|---------|---------|------------|
| `station_tier1_cap` | int | 5-20 | 20 | 10 | - |
| `station_tier2_cap` | int | 5-20 | 10 | 10 | - |
| `station_tier3_cap` | int | 1-10 | 5 | 1 | - |

### 1.4 Lanes

| Lane | File | Purpose | Status | Proposed Design |
|------|------|---------|--------|-----------------|
| **Goldilocks** | `core/lane2_goldilocks.py`, `core/signals/goldilocks_signal.py` | Fleeting temperature microstructure at bucket boundaries | Active (low volume) | Meta-lane: spike_reversion absorbed, pressure_tendency added, trajectory for confirmation |
| **Trajectory** | `core/p3_trajectory_tracer.py` | Meteorological epoch-sequence matching | Active (microstructure-only) | Rewrite as sequential narrowing live probability tracker (DTW + Bayesian narrowing + market price) |

### 1.5 Fusion Methods

| Method | How It Works | Used In | Status |
|--------|-------------|---------|--------|
| **Average fusion** | Simple mean of signal probabilities | Fusion mode 0 | Active |
| **Weighted fusion** | Weighted mean (learning-based weights) | Fusion mode 1 | Active |
| **Select best** | Pick signal with highest confidence | Fusion mode 2 | Active |
| **Bayesian log-odds** | Log-odds combination with prior | Spec in FP-MULTI-SIGNAL-FUSION.md | Not implemented |
| **Consensus boost** | Base probability + boost when N signals agree | Proposed | Design phase |
| **WhaleWatch conviction** | Multiplier on Kelly sizing | Layer 3 | Planned |

---

## Part 2: Test Specification Matrix

### 2.1 Test Categories

Each component is tested in one or more of these ways:

| Test Type | Description | Used For |
|-----------|-------------|----------|
| **Fire-rate audit** | Does the signal/evaluate produce non-null output? | All signals |
| **Directional accuracy test** | % correct direction calls | Active signals |
| **Temperature accuracy test** | Did predicted range contain settlement temp? | All signals (currently stub) |
| **Market accuracy test** | Correct direction vs price at entry? | All signals (currently stub) |
| **P&L test** | Dollar result with current sizing | Active signals |
| **Sharpe test** | Risk-adjusted return | Active signals, configs |
| **Brier test** | Probability calibration quality | All signals, configs |
| **Max drawdown test** | Worst peak-to-trough loss | Active signals, configs |
| **Trade volume test** | Trades per day (sanity check) | All signals |
| **Geo holdout test** | Generalize to held-out stations? | All signals |
| **Walk-forward test** | Time-series generalization | All configs |
| **Regime breakdown** | Performance per season/region/cycle | All signals |

### 2.2 Expected Result Ranges

| Metric | Floor (bad) | Acceptable | Good | Excellent | Suspicious |
|--------|:-----------:|:----------:|:----:|:---------:|:----------:|
| Directional accuracy | <50% | 50-58% | 58-65% | 65-75% | >75% or <45% |
| Temperature accuracy | <50% | 50-60% | 60-70% | 70-80% | >85% |
| Market accuracy | <50% | 50-55% | 55-65% | 65-75% | >80% |
| Sharpe | <0 | 0-0.5 | 0.5-1.5 | 1.5-2.5 | >3.0 |
| Trades/day | <0.1 | 0.1-0.5 | 0.5-2.0 | 2.0-5.0 | >10.0 |
| P&L (per trade) | <-$50 | -$10 to $10 | $10-$100 | $100-$300 | >$500 |
| Brier | >0.25 | 0.20-0.25 | 0.15-0.20 | 0.10-0.15 | <0.05 |
| Max drawdown | >0.5 | 0.3-0.5 | 0.15-0.3 | 0.05-0.15 | <0.01 |

### 2.3 Acceptance Criteria

For a config to pass acceptance:
1. **Directional accuracy ≥ 55%** (above coin flip with margin)
2. **Sharpe ≥ 0.5** (positive risk-adjusted return)
3. **Max drawdown ≤ 0.5** (never lose more than half)
4. **P&L > $0** (must be profitable on holdout period)
5. **Trades ≥ 50** (minimum for statistical significance)
6. **Geo holdout gap ≤ 10pp** (generalizes across stations)

**Pass = all 6 criteria met.** Any config that fails any criterion is rejected.

---

## Part 3: Calibration Framework

### 3.1 Multi-Target Calibration

**Temperature Accuracy**
- **Definition:** Does the predicted temperature range/bucket contain the actual settlement temperature?
- **Method:** Compare predicted bucket (or bucket range) against settlement_epoch/terminal_state
- **Target:** ≥58% (Gray Room threshold)
- **Why separate from directional accuracy:** A config can correctly call "up" but be 10°F off. Temperature accuracy is the meteorological skill metric.

**Market Accuracy**
- **Definition:** Did we correctly call direction vs the market price at entry time?
- **Method:** Edge = P(T > B) — Market_Price at signal time. If edge > 0 and settlement direction matches predicted → correct. If edge > 0 and settlement direction opposite → incorrect.
- **Target:** ≥55%
- **Why separate:** A 60% temperature accuracy means nothing if the market has already priced it at 80¢. Market accuracy measures real trading skill.

**P&L**
- **Definition:** Dollar result after fees, slippage, position sizing
- **Method:** simulate_trade() with current fee structure
- **Target:** Positive on holdout
- **Why separate:** A config with 65% accuracy can lose money if fees are too high or sizing is wrong.

**Sharpe**
- **Definition:** Mean(P&L) / std(P&L)
- **Method:** Per-trade P&L series time-weighted
- **Target:** ≥0.5
- **Suspicious:** >3.0 (aggressive configs inflate this)

**Brier**
- **Definition:** Mean((p_hat - outcome)^2)
- **Method:** Per-trade Brier score
- **Target:** <0.25
- **Interpretation:** Lower is better. 0.25 = random guessing.

**Max Drawdown**
- **Definition:** Maximum peak-to-trough P&L decline
- **Target:** ≤0.5
- **Suspicious:** <0.01 (too perfect, likely overfitting)

### 3.2 Scalarized Objective Options

**Option A: Weighted Sum (Current)**
```
score = α × norm(acc) + β × norm(sharpe) + γ × norm(pnl) - δ × norm(brier) - ε × norm(dd)
```
Weights: α=0.35, β=0.35, γ=0.15, δ=0.10, ε=0.05

**Option B: Consensus Bonus (Proposed)**
```
thresholds = {acc: 0.55, sharpe: 0.5, pnl: 0, brier: 0.25, dd: 0.5}
passes = sum(metric >= threshold for metric in metrics)
score = base + bonus × (passes / 5)
```
Bonus grows non-linearly: 5/5 passes is not 5× better than 1/5, but more like 10× better.

**Option C: Pareto Front**
- Non-dominated sorting of all configs
- Config A dominates config B if A is better on ALL 5 metrics
- Pareto front = set of non-dominated configs
- Select from front based on risk preference

**Test: Run all three, compare results.** Report which configs are selected by each method and why.

### 3.3 Regime-Specific Calibration

| Regime | Confidence Floor | Kelly Fraction | Stop Loss | Expected Acc |
|--------|:---------------:|:--------------:|:---------:|:------------:|
| **DJF** | 0.55 | 0.50 | Trailing | 60-67% |
| **MAM** | 0.65 | 0.35 | Trailing | 52-58% |
| **JJA** | 0.80 | 0.25 | None | 45-52% |
| **SON** | 0.65 | 0.35 | Trailing | 50-56% |

Per-station calibration: station_tier1 cap determines which stations get which calibration profile.

---

## Part 4: Method Comparison

| Method | Cal Target | Weight | Consensus | Pareto | Notes |
|--------|:----------:|:------:|:---------:|:------:|-------|
| Weighted sum | Single blended | Linear | No | No | Current approach, dominated by acc+sharpe |
| Consensus bonus | Multi-threshold | N/A | Non-linear | No | Mirrors ensemble agreement pattern |
| Pareto front | Multi-objective | N/A | No | Yes | Most conservative, selects trade-offs |

**Recommended:** Test all 3. Report which selects the best config by real out-of-sample P&L.

---

## Part 5: Integration & Dependency Map

```
DATA → SIGNAL → FUSION → GATE → DECISION → SIZING

metar_backfill.db → Trajectory (DTW) ─┐
                                      │
gefs_archive.db → GEFS signals ───────┤
                                      ├──→ FUSION → stop_loss → edge_calc → sizing
tigge_archive.db → ECMWF signals ─────┤              │             │
                                      │              │             └── market_price
nwp_forecasts.db → Multi-Model ───────┘              │
                                                     └── confidence_floor
                                                        │
                                                        └── regime_filter
                                                           │
                                                           ├── season (DJF/JJA/...)
                                                           ├── region (continental/...)
                                                           └── cycle (12z/...)
whale_watch.db → WhaleWatch ───────────────────────────── conviction_multiplier
                                                                        │
                                                                        └── kelly_fraction
```

**Live cascade:**
1. Data arrives (hourly METAR, daily GEFS, daily ECMWF, live price)
2. Each signal evaluates independently
3. Fusion combines probabilities (average/weighted/select_best)
4. Gates filter trades (stop_loss, confidence_floor, regime)
5. Edge calculated against market price
6. Sizing determined by Kelly + conviction multiplier + regime thresholds
7. Trade decision: enter/exit/change

---

## Part 6: Validation Protocol

### 6.1 Reproducibility
- **Seed-locked:** LHS seed = 42 fixed. All config generation deterministic.
- **Data frozen:** Sweep against a data snapshot, not live DB.
- **Config auditable:** Sweep output includes the exact config dict that produced each result.

### 6.2 Walk-Forward Validation
- **Split:** 80% discovery (2021-2025), 20% holdout (2025-2026)
- **Time-contiguous:** No random shuffle. No look-ahead.
- **Separation:** Best config selected on discovery only. Results reported on holdout only. Combined P&L is NOT used for selection.

### 6.3 Geographic Holdout
- **Split:** 17 stations for discovery, 3 held-out stations
- **Criterion:** Held-out station accuracy must be within 10pp of discovery station accuracy
- **If fails:** Signal is station-overfit. Apply station normalization or kill.

### 6.4 Statistical Significance
- **Binomal test:** Accuracy must be significantly > 50% (p < 0.05)
- **Minimum trades:** 50 trades per config for statistical power
- **Minimum stations:** 3 stations per config for geographic diversity

### 6.5 Convergence Criteria
Sweep stops early if:
- Last 20 configs show < 0.5pp accuracy change
- Last 20 configs show < 0.1 Sharpe change
- Minimum 100 configs evaluated (to avoid early stopping)

---

## Part 7: Gray Room Briefing Packet

### What the experts will see:
1. This comprehensive plan document
2. The three-layer cascade architecture (FP-SYSTEM-ARCHITECTURE.md)
3. The 7 FP fix specs
4. The multi-target calibration framework
5. The 3 scalarized objective options (weighted vs consensus vs pareto)
6. The validation protocol

### Instructions for experts:
- **Do NOT kill anything.** Correct and improve.
- Flag assumptions. Identify gaps. Suggest alternatives.
- Rate each component: KEEP / FIX / REPLACE
- For REPLACE items: what should replace it and why?

### Open questions for experts:
1. Consensus bonus vs weighted sum for scalarized objective — which is theoretically sounder?
2. DTW sequence matching vs Kalman filter for trajectory — which produces better temperature ranges?
3. Bayesian fusion (log-odds) vs simple averaging — does the complexity buy us anything?
4. Regime-specific calibration — is it worth the overhead or should we fix JJA at the signal level?
5. The 4-signal core — is it enough or do we need redundancy for robustness?
6. Per-station models vs pooled + normalization — which generalizes better?

---

## Appendix: What Changed from Previous Sweeps

| Issue | Old Sweep | New Sweep |
|-------|-----------|-----------|
| Kelly fraction | (0.1, 4.0) — over-betting | (0.1, 1.0) — never exceed optimal |
| Max contracts | (10, 5000) — insane | (10, 1000) — reasonable |
| Confidence floor | 0.5 | 0.55 minimum, regime-aware |
| LHS seed | None (non-reproducible) | seed=42 (reproducible) |
| Holdout reporting | Combined P&L only | Discovery + Holdout separate |
| Scalarized objective | Weighted sum only | Weighted + Consensus + Pareto |
| Temperature accuracy | Stub (== accuracy) | Proper bucket-range test |
| Market accuracy | Stub (== accuracy) | Proper edge-vs-price test |
| Signal reduction | None | 4-signal core proposed |
| Trajectory | Market microstructure | Sequential narrowing DTW |
| Goldilocks | Single signal | Meta-lane with multiple inputs |
| Live tracking | Daily batch only | Hourly sequential update |
| Market price | Used for validation | Used for edge calculation |
| WhaleWatch | Separate system | Integrated conviction multiplier |
| Dead signals | Ignored | Classified with fix plan |

---

*This document is the comprehensive sweep plan. Review → Expert design review (Gray Room) → Execute.*
