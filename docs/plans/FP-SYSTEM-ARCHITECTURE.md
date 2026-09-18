# First Principles System Architecture: Three-Layer Cascade

**Date:** 2026-08-10
**Author:** Gilfoyle Subagent
**Context:** Design discussion from `memory/2026-08-10-sweep-fix-specs-design-discussion.md`

## Executive Summary

The weather engine architecture is a **three-layer cascade** that processes temperature belief from broad to specific, incorporating market price and probabilistic reasoning at each stage. This replaces the previous batch-oriented approach with a live, sequential narrowing framework.

## Layer 1: Temperature Belief (Sequential Narrows over Day)

A live probability tracker that updates as data arrives throughout the day:

```
09:00 UTC: Direction UP @ 80%, range 70-80°F @ 70%
09:30 UTC: Trajectory + regime narrows to 76-79°F @ 67%
10:30 UTC: Live METAR confirms 76°F → 72%, map to bucket
12:00 UTC: New METAR shows 77°F → shift probability to next bucket
15:00 UTC: Settlement approaches, nowcasting tightens
```

### Key Components

#### 1. Trajectory Prediction (Rewritten)
**Purpose:** Meteorological epoch-sequence matching that narrows temperature range
**Implementation:** `core/live_probability_tracker.py` (replaces `p3_trajectory_tracer.py`)
**Data:** METAR observations, TIGGE archive, NWP forecasts
**Method:** Dynamic Time Warping (DTW) on METAR sequences
**Output:** Temperature range probability distribution

#### 2. Regime-Switching HMM
**Purpose:** Latent weather regimes gate confidence
**Implementation:** `core/regime_switching_hmm.py`
**Data:** METAR features (temp variance, pressure tendency, humidity, wind)
**Method:** 3-5 state Hidden Markov Model
**Output:** Regime probability → confidence modulation

#### 3. Live METAR Integration
**Purpose:** Independent observation constraint — Bayesian update
**Implementation:** Existing METAR pipeline + Bayesian update
**Data:** `metar_backfill.db` (12.8M observations)
**Method:** Sequential Bayesian filtering
**Output:** Posterior temperature probability

#### 4. Consensus Boost Mechanism
**Purpose:** +X boost when N independent signals agree
**Implementation:** `core/consensus_boost.py`
**Logic:** Base probability + boost when agreement threshold met
**Calibration:** Originally +0.25 for 82-member ensemble, recalibrate for 4-signal core

### Layer 1 Output
P(T > B) for each bucket, updated hourly with narrowing range and increasing confidence

## Layer 2: Settlement Belief

### Goldilocks Lane (Meta-Lane)
**Purpose:** Detects fleeting microstructure events at bucket boundaries
**Components:**
- `spike_reversion` absorbed as detection input
- `pressure_tendency` confirmation added
- Trajectory integration for meteorological context
**Output:** P(settlement artifact | conditions) — modifies bucket target

## Layer 3: Decision & Bet Sizing

### Market Price Integration
**Edge Calculation:** Edge = P(T > B | trajectory) — Market_Price
**Purpose:** Market price gates three scenarios:
1. **Get out:** P(T > B) narrowed below price threshold, or edge reversed
2. **Change:** Market price moved but P stayed same → better edge in different bucket
3. **Not worth entering:** Edge < fees + slippage budget

### Sizing Framework
**WhaleWatch Conviction Multiplier:**
- 1.0 default
- 1.25 DETECTED
- 1.5 HIGH_CONVICTION

**Kelly Base:** Quarter-Kelly (0.25) conservative cap

**2-Bucket Sizing:**
- **High-confidence** (≥0.80): Kelly sizing
- **Low-confidence** (0.55-0.79): Fixed sizing

**Regime-Aware Thresholds:**
- DJF trades at lower confidence (0.55)
- JJA needs higher confidence (0.80+)

### Three Decisions per Trade
1. **Enter:** Edge > threshold
2. **Exit:** Edge reversed
3. **Change:** Better edge elsewhere

## Data Requirements

### Primary Data Sources
- `metar_backfill.db` — 12.8M METAR observations (temperature belief)
- `tigge_archive.db` — ECMWF IFS ensemble (trajectory, consensus)
- `gefs_archive.db` — GEFS ensemble (baseline)
- `nwp_forecasts.db` — Multi-model data (consensus)
- Kalshi market prices (edge calculation)
- WhaleWatch data (conviction multiplier)

### Feature Engineering
- Temperature variance (regime detection)
- Pressure tendency (goldilocks confirmation)
- Humidity/wind patterns (regime classification)
- Ensemble spread (confidence scoring)

## Implementation Roadmap

### Phase 1: Regime-Switching HMM (Item 1)
- Feature extraction from METAR
- HMM training with hmmlearn
- Regime probability integration
- Walk-forward validation
- Target: JJA recovery from 47.2% to 55%+

### Phase 2: Walk-Forward + 2-Bucket Strategy (Item 2)
- Regime-specific optimization
- Dual sizing implementation
- Fee-aware calibration
- Target: Fix inverted optimization problem

### Phase 3: Trajectory Rewrite (Item 3)
- DTW sequence matching
- Market price integration
- WhaleWatch integration
- Consensus boost mechanism
- Target: Live probability tracking with range narrowing

## Accuracy Improvement Targets

| Component | Target Improvement | Rationale |
|-----------|:-------------------:|-----------|
| Regime-HMM | +5-10pp JJA | Addresses summer collapse |
| Trajectory rewrite | +3-5pp overall | Better range prediction |
| Consensus boost | +2-4pp | Improved signal agreement |
| Market price integration | +1-3pp Sharpe | Better edge capture |
| WhaleWatch multiplier | +1-2pp | Conviction-based sizing |

## Risk Assessment

**Technical Risks:**
- HMM overfitting to historical regimes
- DTW computational complexity
- Real-time market price integration latency
- Bayesian update stability

**Mitigation:**
- Walk-forward validation for regimes
- Efficient DTW implementations
- Market price caching
- Robust Bayesian filtering

## Standing Directives

- Every module must include accuracy-improvement suggestions
- Use Design→QC→Build→QC→Confirm loop
- All data exists — no new data collection needed
- Focus on real-world accuracy with maximum lead time
- Bayesian/probabilistic approaches preferred over fixed-weight schemes

## Next Steps

1. Execute Items 1-3 with the specified loop
2. Monitor Layer 1→Layer 2→Layer 3 handoffs
3. Validate sequential narrowing behavior
4. Integrate market price and WhaleWatch data