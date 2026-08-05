# Trajectory Lane Design Document

## Overview
The trajectory lane is a **meteorological epoch-sequence matching system** that answers: "Given the last N days of observed weather at a station, what settlement buckets did historically similar trajectories produce?" It serves as a **heavy informant** to guide trade selection, not a gate that overrides the primary ensemble signal.

## Current Implementation

### Existing Code: `core/p3_trajectory_tracer.py`
**Purpose**: Matches **settlement epochs** (intraday temperature dynamics) for Phase 3.
**Features**: settlement_jump_magnitude, reversion_occurred, terminal_state_reached
**Limitation**: Designed for market microstructure, not meteorological pattern matching.

### Import Relationships
The module is imported by 5 other modules:
1. `core/p3_match_engine.py` - Primary analog matching
2. `core/p3_decision_engine.py` - Decision logic  
3. `core/p3_backtest.py` - Backtesting
4. `core/p3_signal_generator.py` - Signal generation
5. `tests/test_p3_trajectory.py` - Unit tests

### Key Functions
- `get_forward_trajectory()`: Extract forward trajectory from matched analog
- `trace_all_trajectories()`: Main entry point for trajectory analysis
- `cluster_trajectories_by_outcome()`: Group similar trajectories
- `get_consensus_projection()`: Get primary/secondary consensus

## Design Philosophy: Heavy Informant, Not Gate

### Role Definition
The trajectory lane provides **contextual intelligence** to:
- Confirm or question the primary ensemble signal
- Provide alternative probability estimates based on pattern matching
- Flag regime divergence between model forecasts and observed patterns
- Guide position sizing with confidence modulation

### When It Overrides the Ensemble Signal
**NEVER as a hard override**. The trajectory lane may:
1. **Reduce position size** when trajectory confidence is low (< 0.05 weight)
2. **Increase position size** when trajectory strongly confirms ensemble (> 0.10 weight)
3. **Flag for manual review** when trajectory diverges directionally from ensemble

## Confidence Thresholds

### Trajectory Quality Score (`traj_quality`)
Composite score [0, 1] based on:
- **Analog count** (40% weight): <20=0, 20-50=0.3, 50-100=0.6, >100=1.0
- **DTW similarity** (30% weight): Mean distance of top-10 matches [0, 1]
- **Station match** (20% weight): 1.0 if same-station matches exist, 0.5 if cross-station only  
- **Recency** (10% weight): 1.0 if corpus includes last 90 days, 0.5 otherwise

### Weight Allocation (`w_traj`)
Maximum influence on combined probability:
```python
w_traj = 0.15 * traj_quality  # Cap at 0.15
combined_prob = (1 - w_traj) * ensemble_prob + w_traj * trajectory_prob
```

### Decision Rules

#### High Confidence Trajectory (traj_quality ≥ 0.7)
- **Same direction as ensemble**: Increase position by 10-20%
- **Opposite direction**: Flag for manual review, reduce position by 50%
- **Neutral/conflicting**: Maintain base position

#### Medium Confidence Trajectory (0.3 ≤ traj_quality < 0.7)
- **Same direction**: Small position boost (5-10%)
- **Opposite direction**: Small position reduction (10-20%)
- **Neutral**: No adjustment

#### Low Confidence Trajectory (traj_quality < 0.3)
- Ignore for position sizing
- Log for analysis only

## Data Sources

### Primary: METAR Observations
- **Source**: `data/metar_backfill.db`
- **Variables**: Temperature, humidity, pressure, wind speed/direction
- **Time resolution**: Hourly observations
- **Coverage**: 1,468,161 records across 21 stations

### Secondary: Settlement Outcomes  
- **Source**: `data/kalshi_settlements.db`
- **Variables**: Settlement temperature, bucket classification
- **Coverage**: 6,171 records across 1,750 dates × 21 stations

### Epoch-Based Analog Matching
**Epoch definition**: Daily meteorological state vector:
```python
{
  "date": "2026-08-05",
  "station": "KNYC",
  "temp_max": 85.2,      # For HIGH markets
  "temp_min": 72.1,      # For LOW markets  
  "rh_mean": 65.5,
  "pressure_mean": 1018.2,
  "wind_speed_mean": 8.3,
  "wind_dir_prevailing": "SW",
  "settlement_bucket": "85-90"  # Outcome label
}
```

## Sequence Matching Methodology

### Dynamic Time Warping (DTW)
- **Algorithm**: FastDTW with radius=5
- **Distance metric**: Weighted Euclidean with feature weights
- **Early abandon**: Skip if accumulated distance exceeds current best threshold

### Feature Weights
```python
FEATURE_WEIGHTS = {
    "temp_f": 0.35,      # Primary driver
    "rh_pct": 0.20,      # Humidity modulates heat perception
    "pressure_mb": 0.20, # Synoptic regime identifier
    "wind_speed_kt": 0.15, # Advection rate
    "wind_dir": 0.10     # Air mass origin (circular distance)
}
```

### Sequence Lengths
- **Primary**: N=5 days (captures synoptic patterns)
- **Secondary**: N=3 days (captures short-term persistence)
- System runs both and selects based on quality score

## Corpus Construction

### Corpus Size
- **Total station-days**: ~36,750 (1,750 dates × 21 stations)
- **With complete features**: ~28,000 (75% coverage for RH/pressure)
- **Unique 5-day trajectories**: ~32,000
- **Unique 3-day trajectories**: ~34,000

### Climate Zone Pooling
When same-station analogs <30, pool across climate zones:

| Zone | Stations | Weight Multiplier |
|------|----------|-------------------|
| Northeast | NYC, PHL, DCA, BOS | 1.0 |
| South | ATL, MIA, MSY, HOU | 1.0 |
| Midwest | MDW, MSP, ORD | 1.0 |
| Interior | DEN, OKC, DFW, AUS, SAT | 1.0 |
| West | LAX, SFO, SEA, PHX | 1.0 |
| Same station | (any) | 2.0 |

## Output Structure

### Diagnostic Packet
```json
{
  "station": "KMDW",
  "date": "2026-08-03",
  "trajectory_days": 5,
  "analog_count": 127,
  "analog_same_station": 43,
  "analog_same_climate": 84,
  "mean_dtw_distance": 0.18,
  "bucket_distribution": {
    "below_85": {"count": 83, "pct": 0.654, "se": 0.042},
    "at_least_85": {"count": 44, "pct": 0.346, "se": 0.042},
    "at_least_90": {"count": 12, "pct": 0.094, "se": 0.026}
  },
  "recommended_buckets": [
    {"bucket": "below_85", "traj_prob": 0.59, "action": "CONSIDER_POSITION"}
  ],
  "traj_quality": 0.32,
  "w_traj": 0.048,
  "regime_divergence": false
}
```

### Bucket Recommendation Logic
1. **Primary bucket**: Highest analog density with ≥20 analogs
2. **Secondary bucket**: Alternative with ≥15% share
3. **Not tradeable**: Buckets with <10 analogs or SE > 0.10

## Integration with GEFS Pipeline

### Parallel Execution
```
METAR data → Trajectory Feature Builder → Trajectory DB
                                      ↓
                                DTW Sequence Matcher
                                      ↓
                          Bucket distribution + quality
                                      ↓
                            Trade Selection Aggregator ← GEFS fraction
                                      ↓
                                Position sizing (Kelly)
```

### Confidence Modulation
The trajectory lane modifies the GEFS probability through weight `w_traj`:
- **High quality** (traj_quality > 0.7): w_traj = 0.10-0.15
- **Medium quality** (0.3-0.7): w_traj = 0.05-0.10  
- **Low quality** (<0.3): w_traj = 0.0

### Regime Divergence Flag
Raised when:
1. Ensemble probability > 0.65 for bucket X
2. Trajectory probability < 0.35 for same bucket
3. Both signals have confidence > 0.3

## Implementation Plan

### Phase 1: Core Matching (8 hours)
1. Feature builder: METAR → daily vectors (2h)
2. DTW sequence matcher with FastDTW (4h)
3. Corpus manager (2h)

### Phase 2: Integration (4 hours)
1. Bucket aggregator + confidence scorer (2h)
2. Trade selection integration (1h)
3. Diagnostic packet output (1h)

### Phase 3: Optimization (3 hours)
1. Climate-zone pooling (1h)
2. Multi-length matching (1h)
3. Performance tuning (1h)

## Gray Room Input Needed

1. **Feature selection priority**: Which secondary features (cloud cover, precipitation) add value?
2. **Climate zone definitions**: Are the proposed zones meteorologically meaningful?
3. **Weight cap debate**: Should w_traj be capped at 0.15 or higher for high-confidence matches?
4. **Missing data handling**: How aggressive should gap-filling be for RH/pressure?
5. **Corpus refresh frequency**: Daily update vs weekly rebuild?