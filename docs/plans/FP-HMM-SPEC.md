# FP-HMM-SPEC.md

## Item 1: Regime-Switching Hidden Markov Model

### Problem Statement
The weather engine suffers from seasonal performance collapse, particularly in JJA (summer) where accuracy drops to 47.2%. A regime-switching HMM can detect latent weather patterns and modulate confidence based on regime probability.

### Design Vision
A 3-5 state Hidden Markov Model that:
1. Learns latent weather regimes from METAR features
2. Modulates signal confidence based on regime probability
3. Enables regime-specific optimization thresholds
4. Recovers JJA performance from 47.2% to 55%+

### Proposed Solution
Build `core/regime_switching_hmm.py`:

#### 1. Feature Extraction
**Input:** METAR observations from `metar_backfill.db` (12.8M records)
**Features:**
- Temperature variance (regime volatility)
- Pressure tendency (frontal regimes)
- Humidity patterns (air mass characteristics)
- Wind direction/speed (weather pattern changes)
- Cloud cover (stability indicators)

#### 2. HMM Architecture
**States:** 3-5 latent weather regimes
**Model:** Gaussian HMM using `hmmlearn`
**Training:** Walk-forward validation per season
**Output:** Regime probability distribution

#### 3. Confidence Modulation
**Mechanism:** Regime probability → confidence multiplier
**Mapping:**
- High-confidence regimes: 1.0-1.3x multiplier
- Low-confidence regimes: 0.7-1.0x multiplier
- Transition regimes: dynamic adjustment

#### 4. Integration Points
- Layer 1: Temperature belief confidence modulation
- Layer 2: Goldilocks lane regime context
- Layer 3: Regime-aware decision thresholds

### Data Requirements
- `metar_backfill.db` — 12.8M METAR observations
- Feature engineering: rolling statistics (6h, 12h, 24h windows)
- Station normalization for geographic consistency

### Code Needed
- `core/regime_switching_hmm.py` — Main HMM implementation
- `core/metar_feature_extractor.py` — Feature engineering
- `core/regime_confidence_modulator.py` — Confidence mapping
- `core/walk_forward_hmm.py` — Validation framework
- Integration updates throughout pipeline

### Build Effort
- **Phase 1**: Feature extraction (8 hours) — METAR feature engineering
- **Phase 2**: HMM training (10 hours) — Model architecture and training
- **Phase 3**: Walk-forward validation (8 hours) — Regime stability testing
- **Phase 4**: Integration (6 hours) — Confidence modulation wiring
- **Total**: 32 hours (4 days)

### Risk Level
**MEDIUM** — HMM overfitting risk, regime definition validation

### Priority
**P0** — Addresses core JJA performance collapse

### Dependencies
- METAR data quality assessment
- Feature correlation analysis
- Walk-forward validation framework

### Improvement Suggestion
**Implement regime-weighted DTW similarity** — Use HMM regime probabilities to weight Dynamic Time Warping distance calculations. During frontal regimes, prioritize pressure and wind patterns; during stagnant regimes, prioritize temperature persistence patterns. This should improve trajectory accuracy by +5-8pp by focusing on regime-relevant features.

### Alignment with Sequential Narrowing Framework
This integrates with **Layer 1 (Temperature Belief)** of the three-layer cascade. The HMM provides regime context that helps narrow temperature predictions throughout the day, enabling more accurate probabilistic temperature tracking and better confidence modulation for Layer 3 decision making.