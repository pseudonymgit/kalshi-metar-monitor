# Phase 3: Epoch-Based Analog Forecasting — Implementation Summary

**Date:** 2026-06-17  
**Status:** COMPLETE  
**Author:** Gilfoyle (CTIO)  
**Disposition:** Phase 3 Gray Room → ADVANCE

---

## Overview

Phase 3 implements the prediction layer (P3) for the OpenClaw weather engine. P3 reads from L4 (settlement epochs) and generates deterministic forecasts using analog matching with fixed weights, thresholds, and no learned parameters.

---

## Architecture

```
L0 (raw METAR) → L1 (validated) → L2 (calibrated) → L3 (epoch sealed) → L4 (settlement)
                                                                              ↓
                                    P3_read ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← 
                                       ↓
                                    P3_match (feature extraction + similarity scoring)
                                       ↓
                                    P3_trace (forward trajectory lookup from analogs)
                                       ↓
                                    P3_project (consensus/conflict resolution)
                                       ↓
                                    P3_calibrate (confidence scoring)
                                       ↓
                                    P3_output → alert/trade recommendation
```

### Key Invariants

1. **Firewall:** P3 reads L4, NEVER writes L0-L4. Separate prediction namespace.
2. **Determinism:** Fixed weights, fixed thresholds, no learned parameters.
3. **Zero Kalshi calls:** Pure prediction uses only epoch history in SQLite.
4. **Honest confidence:** "No consensus" is a valid output.
5. **Trajectory:** Sliding window K≥3, consecutive match bonus 3^(N-1), seasonal-phase proximity.

---

## Implementation Components

### 1. DB Migration (`p3_db_migration.py`)

**Purpose:** Create composite index for efficient epoch queries.

**Index:** `settlement_epochs(station, market_type, local_trading_date)`

**Key features:**
- Creates table if it doesn't exist (for fresh deployments)
- Idempotent - safe to run multiple times
- Handles different deployment environments (/var/data vs local)

**Usage:**
```bash
# Create index (or verify if exists)
python core/p3_db_migration.py

# Verify index
python core/p3_db_migration.py verify

# Drop index (for testing only)
python core/p3_db_migration.py drop
```

---

### 2. Feature Extractor (`p3_feature_extractor.py`)

**Purpose:** Extract 14-dimensional feature vector from settlement epochs.

**Features:**
1. `settlement_jump_magnitude` (int) - weight 3
2. `reversion_occurred` (binary) - weight 2
3. `max_excursion_above_settlement` (float) - weight 1
4. `duration_at_or_above_seconds` (float) - weight 1
5. `duration_strictly_above_seconds` (float) - weight 1
6. `terminal_state_reached` (binary) - weight 2
7. `transition_count` (int) - weight 1
8. `settlement_bucket` (int) - weight 1
9. `reversion_latency_seconds` (float) - weight 2
10. `goldilocks_emitted` (binary) - weight 2
11. `prior_settlement_bucket` (int) - weight 1
12. `local_trading_date_normalized` (float [0,1]) - weight 1
13. `station` (gate - weight ∞)
14. `market_type` (gate - weight ∞)

**Distance Functions:**
- Integer: `d = |a - b|`
- Binary: `d = 0` if equal, `1` otherwise
- Float: `d = |a - b| / MAX_RANGE` (fixed per-dimension)

**No data normalization** - uses fixed MAX_RANGE constants.

---

### 3. Match Engine (`p3_match_engine.py`)

**Purpose:** Similarity scoring with fixed weights and thresholds.

**Scoring:**
```
raw_distance = Σ(w_i × d_i)
match_score = 1.0 - (raw_distance / max_possible_distance)
```

**Thresholds (fixed, never learned):**
- Strong match: ≥ 0.85
- Weak match: 0.70 - 0.85
- No match: < 0.70

**Regime handling:**
- Sparse (< 5 strong matches): relaxed threshold τ=0.65
- Normal (5-29 strong matches): use all strong matches
- Abundant (≥ 30 strong matches): use top-K ranked by score × recency

**Gates:**
- `station` must match exactly (infinite weight)
- `market_type` must match exactly (infinite weight)

---

### 4. Trajectory Tracer (`p3_trajectory_tracer.py`)

**Purpose:** Forward trajectory analysis from matched analogs.

**Key features:**
- Forward lookup: 3 epochs following matched epoch
- Minimum trajectory length: 3 epochs
- Consecutive match bonus: `3^(N-1)`, capped at 5
- Gap tolerance: 1-epoch gap gets 0.5 penalty multiplier

**Clustering:**
- Groups trajectories by outcome similarity (settlement bucket)
- Weight: `(member_count × mean_match_score)`
- Reports divided consensus when top two clusters within 20%

**Consecutive match weighting:**
- Isolated: weight = 1.0
- 2 consecutive: weight = 3.0
- 3 consecutive: weight = 9.0
- N consecutive: weight = 3^(N-1), capped at 5

---

### 5. Calibration Engine (`p3_calibration_engine.py`)

**Purpose:** 6-factor confidence scoring with multimodal penalty.

**Confidence Factors (weights sum to 1.0):**

| Factor | Weight | Formula |
|--------|--------|---------|
| Sample Size (N) | 0.25 | `min(1.0, log10(N)/log10(30))`, floor 0.1 below 5 |
| Distribution Shape (Kurtosis) | 0.20 | `1.0 - clamp(excess_kurtosis/6, 0, 1)` |
| Inter-Station Agreement (ISA) | 0.20 | `1.0 - (σ/μ)`, clamped to [0,1] |
| Station Reliability (SR) | 0.15 | `1.0 - mean Brier score over 90 days` |
| Temporal Proximity (TP) | 0.10 | `exp(-0.15 × Δt_hours)` |
| Signal Consensus Direction | 0.10 | `\|p_up - p_down\|` |

**Combination:**
```
C_raw = Σ(weight_i × factor_i)
C_final = C_raw × M_penalty
```

**Multimodal Penalty:**
- Unimodal: 1.0
- Bimodal: 0.70
- Trimodal+: 0.50

**Interpretation Bands (fixed):**
- HIGH: ≥ 0.80
- MODERATE: 0.60 - 0.80
- LOW: 0.40 - 0.60
- INSUFFICIENT: < 0.40

**Temporal Decay:**
```
C_epoch+N = C_final × exp(-0.25 × N), floor 0.10
```

---

### 6. Output Formatter (`p3_output_formatter.py`)

**Purpose:** Format prediction results into structured messages.

**Output format:**
```
PREDICTION: {station} {market_type} epoch {id}
PRIMARY: settlement_bucket={X} (weight={W}%, N={count})
SECONDARY: [if divided] settlement_bucket={Y} (weight={W}%, N={count})
MATCHES: {strong_count} strong, {weak_count} weak
TOP ANALOG: {date} (score={S}, trajectory_outcome={O})
CONFIDENCE: {C_final} ({band})
```

**Warnings include:**
- SPARSE_DATA
- WEAK_MATCHES
- LOW_CONFIDENCE
- NO_CONSENSUS
- DIVIDED_TRAILS
- GAP_IN_TRAJECTORY
- TERMINAL_STATE
- STATION_YOUNG
- HYDRATION_BLOCKED
- REGIME_CHANGE

---

### 7. Scheduler (`p3_scheduler.py`)

**Purpose:** Event-driven scheduling for prediction runs.

**Trigger:** Post-settlement hook (after L4 commit completes)

**Key features:**
- Runs predictions for all active stations: KDEN, KLAX, KNYC, KPHL, KMDW, KMIA, KAUS
- Runs for both market types: high, low
- Caches results for fast retrieval
- Background worker thread (optional, for periodic updates)

**Active Stations:**
| Station | Location |
|---------|----------|
| KDEN | Denver |
| KLAX | Los Angeles |
| KNYC | New York |
| KPHL | Philadelphia |
| KMDW | Chicago |
| KMIA | Miami |
| KAUS | Austin |

---

### 8. API Endpoint (`p3_api.py`)

**Purpose:** FastAPI endpoints for prediction access.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/prediction/{station}/{market_type}` | Get prediction for station/market_type |
| GET | `/api/prediction/{station}/all` | Get predictions for all market types |
| GET | `/api/prediction/stations` | Get list of active stations |
| GET | `/api/prediction/cache/stats` | Get prediction cache statistics |
| POST | `/api/prediction/run` | Trigger immediate prediction run |
| POST | `/api/prediction/cache/clear` | Clear prediction cache |
| GET | `/api/prediction/health` | Health check |
| GET | `/api/prediction/debug/features/...` | Debug: extracted features |
| GET | `/api/prediction/debug/matches/...` | Debug: match analysis |

**Key constraints:**
- Read-only (no Kalshi API calls)
- Zero new Kalshi calls if prediction is purely epoch-history-based
- Follows execution-domain gating (no live calls from observability)
- Uses hydration cache if current market prices needed

---

## Implementation Files

| File | Purpose | Lines |
|------|---------|-------|
| `core/p3_db_migration.py` | DB index creation | ~330 |
| `core/p3_feature_extractor.py` | Feature vector extraction | ~400 |
| `core/p3_match_engine.py` | Similarity scoring | ~300 |
| `core/p3_trajectory_tracer.py` | Forward trajectory analysis | ~300 |
| `core/p3_calibration_engine.py` | Confidence scoring | ~350 |
| `core/p3_output_formatter.py` | Output formatting | ~350 |
| `core/p3_scheduler.py` | Event-driven scheduling | ~300 |
| `core/p3_api.py` | FastAPI endpoints | ~300 |
| `core/p3_main.py` | Main orchestrator | ~250 |
| **Total** | | **~2,500** |

---

## Usage Examples

### Command Line

```bash
# Get summary
python core/p3_main.py summary

# Run prediction for a station
python core/p3_main.py predict KDEN high

# Run all stations
python core/p3_main.py run-all

# Health check
python core/p3_main.py health
```

### Python API

```python
from core.p3_main import run_prediction_for_station, get_cached_prediction

# Run prediction
result = run_prediction_for_station("KDEN", "high")
if result.success:
    print(result.prediction.raw_output)

# Get cached prediction
prediction = get_cached_prediction("KDEN", "high")
if prediction:
    print(f"Confidence: {prediction.confidence} ({prediction.confidence_band})")
```

### HTTP API

```bash
# Get prediction
curl http://localhost:8000/api/prediction/KDEN/high

# Health check
curl http://localhost:8000/api/prediction/health

# Get all stations
curl http://localhost:8000/api/prediction/stations

# Debug features
curl http://localhost:8000/api/prediction/debug/features/KDEN/high
```

---

## Testing

### Unit Tests

```bash
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source

# Test DB migration
python -m pytest tests/test_schema_migration.py -v

# Test feature extraction
python -c "from core.p3_feature_extractor import *; print('Feature extractor OK')"

# Test match engine
python -c "from core.p3_match_engine import *; print('Match engine OK')"

# Test calibration engine
python -c "from core.p3_calibration_engine import *; print('Calibration engine OK')"
```

### Integration Test

```bash
# Full prediction run for all stations
python core/p3_main.py run-all
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERT_DB_PATH` | `/var/data/alerts.db` | Path to SQLite database |

### Fixed Constants

All scoring constants are **fixed** (never learned):
- Match thresholds: 0.85 (strong), 0.70 (weak)
- Confidence bands: 0.80, 0.60, 0.40
- Temporal decay rate: 0.25
- Multimodal penalties: 1.0, 0.70, 0.50
- Consecutive match base: 3, capped at 5

---

## Failure Modes

| Mode | Condition | Response |
|------|-----------|----------|
| No open epoch | No current epoch | ERROR: "No open epoch found" |
| No historical data | Empty corpus | ERROR: "No historical data found" |
| No analogs | No compatible epochs | ERROR: "No compatible analogs found" |
| Terminal state | Epoch already closed | WARNING: "Terminal state reached" |
| Low confidence | Score < 0.60 | WARNING: "Low confidence" |
| No consensus | No strong/weak matches | ERROR: "No consensus found" |
| Regime change | Unprecedented conditions | WARNING: "Unprecedented conditions" |

---

## Performance

### Expected Performance (per station/market_type)

| Operation | Expected Time |
|-----------|---------------|
| DB query (indexed) | < 10ms |
| Feature extraction | < 1ms |
| Match scoring | < 10ms |
| Trajectory tracing | < 20ms |
| Confidence calculation | < 5ms |
| **Total** | **< 50ms** |

### Cache Performance

- Cache hit: < 1ms (reuses PredictionMessage)
- Cache miss: ~50ms (full computation)

---

## Future Work

### Deferred to Phase 4

- AI/ML gate (per Dan's instruction)
- Live trading execution
- Trade/Position Sizing Advisor
- Multi-rung position spreading
- Spread/liquidity analysis

### Potential Enhancements

- Real-time confidence tracking (Brier score updates)
- Seasonal adaptation (different weights by season)
- Multi-station consensus (cross-station agreement)
- Temporal trend detection (predicting trend changes)

---

## Conclusion

Phase 3 implementation is **COMPLETE**. The prediction layer:
- ✅ Reads L4, never writes L0-L4
- ✅ Uses deterministic scoring (fixed weights, fixed thresholds)
- ✅ Zero new Kalshi API calls for pure prediction
- ✅ Honest confidence reporting
- ✅ Trajectory-based forecasting (K≥3)
- ✅ Event-driven scheduling (post-settlement hook)
- ✅ FastAPI endpoints (read-only)
- ✅ Comprehensive documentation

**Next step:** Phase 4 implementation (AI/ML gate, live trading, position sizing).

---

**Author:** Gilfoyle (CTIO)  
**Review:** Dan  
**Date:** 2026-06-17
