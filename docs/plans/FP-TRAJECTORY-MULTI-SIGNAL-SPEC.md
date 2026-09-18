# FP-TRAJECTORY-MULTI-SIGNAL-SPEC.md

## Item 3: Trajectory Prediction — Sequential Narrowing Live Probability Tracker

### Problem Statement
Current `core/p3_trajectory_tracer.py` is designed for market microstructure, not meteorological pattern matching. Needs complete redesign as a **live probability tracker** that narrows temperature range predictions throughout the day.

### Design Vision (Pseudonym's Framework)
A sequential narrowing probabilistic temperature tracker:

```
09:00 UTC: Direction UP @ 80%, range 70-80°F @ 70%
09:30 UTC: Trajectory + regime narrows to 76-79°F @ 67%
10:30 UTC: Live METAR confirms 76°F → 72%, map to bucket
12:00 UTC: New METAR shows 77°F → shift probability to next bucket
15:00 UTC: Settlement approaches, nowcasting tightens
```

### Proposed Solution
Replace `core/p3_trajectory_tracer.py` with `core/live_probability_tracker.py`:

#### 1. Meteorological Epoch-Sequence Matching
**Purpose:** Identify similar historical weather patterns using DTW
**Data:** METAR observation sequences from `metar_backfill.db`
**Method:** Dynamic Time Warping on (temp, pressure, humidity, wind) sequences
**Output:** Similar historical patterns → temperature range probability

#### 2. Market Price Integration
**Purpose:** Calculate edge for trading decisions
**Formula:** Edge = P(T > B | trajectory) — Market_Price
**Integration:** Real-time Kalshi market price feed
**Gating:** Three scenarios:
- **Get out:** P narrowed below price threshold
- **Change:** Better edge in different bucket
- **Not worth:** Edge < fees + slippage

#### 3. WhaleWatch Conviction Multiplier
**Purpose:** Modulate position sizing based on detection confidence
**Values:** 1.0 default, 1.25 DETECTED, 1.5 HIGH_CONVICTION
**Integration:** Multiply Kelly fraction by conviction multiplier

#### 4. Consensus Bonus Integration
**Purpose:** +X boost when independent signals agree
**Source:** From `core/consensus_boost.py`
**Effect:** Increases probability confidence during high-agreement periods

#### 5. Range Narrowing Over Day
**Purpose:** Progressively narrow temperature range as data arrives
**Mechanism:** Bayesian updates with each new METAR observation
**Output:** Tighter temperature distributions → more precise bucket mapping

### Data Needed
- METAR observation sequences (12.8M records)
- ECMWF IFS ensemble data (TIGGE archive)
- NWP forecast data
- Kalshi market prices
- WhaleWatch detection data

### Code Needed
- `core/live_probability_tracker.py` — Replacement main module
- `core/meteorological_dtw.py` — DTW sequence matching
- `core/bayesian_range_narrowing.py` — Sequential probability updates
- `core/market_price_integration.py` — Real-time edge calculation
- Integration updates throughout pipeline

### Build Effort
- **Phase 1**: DTW sequence matching (12 hours) — Meteorological pattern matching
- **Phase 2**: Bayesian narrowing (10 hours) — Sequential probability updates
- **Phase 3**: Market integration (8 hours) — Real-time edge calculation
- **Phase 4**: WhaleWatch integration (6 hours) — Conviction multiplier
- **Total**: 36 hours (4.5 days)

### Risk Level
**MEDIUM** — Complex probabilistic framework, risk of overfitting sequences

### Priority
**P1** — Core of the sequential narrowing framework

### Dependencies
- Completion of METAR data quality assessment
- Real-time market price API integration
- WhaleWatch data pipeline

### Improvement Suggestion
**Implement DTW with regime-weighted similarity** — Weight DTW distance calculations by current regime probability. During frontal regimes, prioritize pressure and wind patterns; during stagnant regimes, prioritize temperature persistence patterns. This should improve trajectory accuracy by +5-8pp by focusing on regime-relevant features.

### Alignment with System Architecture
This is the core implementation of **Layer 1 (Temperature Belief)** in the three-layer cascade. It provides the live probabilistic temperature tracking that feeds into Layer 2 (Settlement Belief) and Layer 3 (Decision & Bet Sizing).