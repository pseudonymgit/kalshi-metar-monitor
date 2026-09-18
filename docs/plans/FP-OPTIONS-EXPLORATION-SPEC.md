# FP-OPTIONS-EXPLORATION-SPEC.md

## Item 7: Full Options Exploration

### Problem Statement
The existing 7 ranked prediction methods in `PREDICTION_METHODS_EXPLORATION.md` need full build specs:
1. Regime-Switching HMM (score 25, 3.5 days)
2. ECMWF IFS Direct Signal (score 20, 2 days)
3. Multi-Model Ensemble (score 20, 3.5 days)
4. NWP Analog Ensemble (score 16, 3 days)
5. Kalman Filter on METAR (score 12, 2.5 days)
6. Bayesian Structural Breaks (score 6, 2.5 days)
7. ERA5 Temperature Normals (score 5, 1.5 days)

### Build Specs by Priority

#### 1. Regime-Switching HMM (Highest Priority)
**Architecture**: 3-5 state HMM on METAR features (temp variance, pressure tendency, humidity, wind)
**Data Pipeline**: METAR observations → feature extraction → HMM training
**Integration**: Regime probability → confidence modulation
**Testing**: Walk-forward validation per regime
**Effort**: 28 hours (3.5 days)
**Risk**: MEDIUM
**Priority**: P0

#### 2. ECMWF IFS Direct Signal
**Architecture**: Decode 50-member ensemble from `tigge_archive.db`
**Data Pipeline**: Binary member decoding → ensemble statistics
**Integration**: Direct probability replacement for GEFS ensemble
**Testing**: Backtest against ECMWF bias-corrected performance
**Effort**: 17 hours (2 days)
**Risk**: LOW
**Priority**: P1

#### 3. Multi-Model Ensemble
**Architecture**: Combine GFS, ECMWF, ICON, GEM from `nwp_forecasts.db`
**Data Pipeline**: Multi-model alignment → Bayesian model averaging
**Integration**: Consensus scoring with spread-based confidence
**Testing**: Backtest against historical outcomes
**Effort**: 28 hours (3.5 days)
**Risk**: LOW
**Priority**: P1

#### 4. NWP Analog Ensemble
**Architecture**: ECMWF analog matching using `tigge_archive.db`
**Data Pipeline**: Feature extraction → k-NN search → probability estimation
**Integration**: Independent prediction stream
**Testing**: Cross-validation analog quality
**Effort**: 24 hours (3 days)
**Risk**: MEDIUM
**Priority**: P2

#### 5. Kalman Filter on METAR
**Architecture**: State estimation [temperature, dT/dt] with METAR observations
**Data Pipeline**: 1-minute METAR → Kalman filtering → settlement prediction
**Integration**: Intraday confirmation signal
**Testing**: Validation against historical METAR data
**Effort**: 20 hours (2.5 days)
**Risk**: LOW
**Priority**: P2

#### 6. Bayesian Structural Breaks
**Architecture**: Change-point detection on temperature time series
**Data Pipeline**: Cumulative statistics → Bayesian break detection
**Integration**: Confidence modulation during break periods
**Testing**: Break detection accuracy validation
**Effort**: 20 hours (2.5 days)
**Risk**: MEDIUM-HIGH
**Priority**: P3

#### 7. ERA5 Temperature Normals
**Architecture**: ERA5 reanalysis normal calculation
**Data Pipeline**: 30-day rolling means → anomaly detection
**Integration**: Forecast bias correction signal
**Testing**: Calibration/validation only
**Effort**: 13 hours (1.5 days)
**Risk**: LOW
**Priority**: P3

### Implementation Roadmap
**Week 1**: Regime-Switching HMM (addresses JJA collapse)
**Week 2**: ECMWF IFS Direct (leveraging existing 86.1% accuracy)
**Week 3**: Multi-Model Ensemble (finish existing implementation)
**Weeks 4-5**: NWP Analog + Kalman Filter
**Deferred**: Bayesian Breaks + ERA5 Normals

### Data Needed
- All data exists in current databases
- METAR, ECMWF, multi-model, ERA5 archives available

### Code Needed
- 7 new prediction modules
- Feature extraction pipelines
- Integration wrappers
- Validation frameworks

### Total Build Effort
**150 hours** (18.75 days) for all 7 methods

### Risk Assessment
**Overall LOW** — Data exists, methods well-understood

### Priority Order
**Regime-HMM → ECMWF → Multi-Model → NWP-Analog → Kalman → Bayesian → ERA5**

### Improvement Focus
**Regime-Switching HMM** — This directly addresses the biggest weakness (JJA performance collapse from 47.2% to 55%+). Should improve overall accuracy by +5-10pp and recover summer profitability.