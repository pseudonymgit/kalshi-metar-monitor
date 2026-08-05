# Goldilocks Lane Design Document

## Overview
The Goldilocks lane detects **fleeting temperature microstructure alerts** at bucket boundaries using 1-minute ASOS observations. It targets transient spikes that cross bucket boundaries but don't establish new daily maxima, providing edge in HIGH markets where the daily max remains below the boundary.

## Current Implementation

### Existing Code: `core/lane2_goldilocks.py`
**Status**: Killed as ML signal, converted to pure deterministic math (B-mode post-Gray-Room)
**Purpose**: Detects "instant cross revert" events where temperature crosses a bucket boundary but reverts quickly
**Key fix**: `running_daily_max` snapshotted BEFORE adding current observation for correct spike delta calculation

### Algorithm Components
1. **Bucket boundary crossing detection**: temp crosses integer boundary going up
2. **Transient spike check**: spike_delta < TRANSIENT_DELTA_THRESHOLD (0.3°F)
3. **Reversion detection**: temp drops below boundary - REVERSION_MARGIN (0.2°F)
4. **Signal firing**: Only on reversion, not crossing

## Data Sources

### Primary: IEM ASOS 1-minute Observations
**Database**: `data/iem_asos_1min.db` (754 MB)
**Coverage**: 19 stations with good data quality
**Variables**: Temperature (°F) at 1-minute resolution
**Time range**: Approximately 2.6 years of data

### Critical Data Limitation: KNYC (New York City)
**⚠️ WARNING**: KNYC has only 27,000 observations over 2.6 years
- **Expected**: 2.6 years × 365 days × 24 hours × 60 minutes ≈ 1,366,560 observations
- **Actual**: ~27,000 observations (2% coverage)
- **Effective resolution**: ~1 observation per 50 minutes
- **Implication**: Goldilocks lane **will not work** for KNYC - insufficient temporal resolution for microstructure detection

### Station Coverage Analysis
| Station | Estimated Observations | Coverage | Goldilocks Viability |
|---------|-----------------------|----------|---------------------|
| KNYC | 27,000 | 2% | **NOT VIABLE** |
| KMDW | ~800,000 | 58% | VIABLE |
| KATL | ~900,000 | 66% | VIABLE |
| KDFW | ~850,000 | 62% | VIABLE |
| KDEN | ~750,000 | 55% | VIABLE |
| KLAX | ~820,000 | 60% | VIABLE |
| ... 18 other stations | 500K-1M each | 36-73% | VIABLE |

## Design: Fleeting Tick Microstructure Alerts

### Detection Thresholds

#### 1. Temperature Delta Threshold
**`TRANSIENT_DELTA_THRESHOLD = 0.3°F`**
- Rationale: A spike that exceeds the previous daily max by less than 0.3°F is likely measurement noise or brief fluctuation
- Calibration: Based on ASOS instrument precision (±0.2°F) plus small genuine fluctuations

#### 2. Boundary Exceedance Threshold  
**`EXCEEDED_THRESHOLD = 0.5°F`**
- Rationale: Must exceed bucket boundary by at least 0.5°F to count as genuine crossing
- Prevents false signals from temperatures hovering near boundary

#### 3. Reversion Margin
**`REVERSION_MARGIN = 0.2°F`**
- Rationale: Must drop at least 0.2°F below boundary to confirm reversion
- Accounts for measurement noise while ensuring meaningful drop

#### 4. Late-Day Gate
**`LATE_DAY_UTC_HOUR = 18` (2pm ET / 11am PT)**
- Rationale: Daily maximum likely established by late afternoon
- Prevents premature signals when daily max could still rise

### What Constitutes a "Fleeting Tick"?
A fleeting tick must satisfy ALL conditions:
1. **Crossing**: Temperature crosses integer bucket boundary going up (e.g., 84.9°F → 85.1°F)
2. **Transience**: `spike_delta < 0.3°F` (barely exceeds previous daily max)
3. **Significance**: `exceeded_by ≥ 0.5°F` (genuinely above boundary)
4. **Reversion**: Drops below `boundary - 0.2°F` within observation window
5. **Timing**: Occurs after 18:00 UTC (daily max established)

### Mathematical Definition
```python
def is_fleeting_tick(prev_temp, curr_temp, running_max_before, boundary):
    crossed_up = prev_temp < boundary <= curr_temp
    spike_delta = curr_temp - running_max_before if running_max_before else float('inf')
    exceeded_by = curr_temp - boundary
    
    is_transient = spike_delta < TRANSIENT_DELTA_THRESHOLD
    is_significant = exceeded_by >= EXCEEDED_THRESHOLD
    
    return crossed_up and is_transient and is_significant
```

## Signal Generation Logic

### Instant Cross Revert Detection
```python
def detect_instant_cross_revert(daily_state, obs, bucket_boundary):
    # 1. Check crossing going up
    # 2. Create tracker if transient and significant
    # 3. Watch for reversion
    # 4. Fire signal only on reversion if all conditions met
```

### Signal Confidence Calculation
Base confidence = 0.50, modified by:
- **Spike delta bonus**: Smaller delta = more transient = higher confidence
- **Time-of-day bonus**: Later hour = daily max more established
- **Running max bonus**: Previous max well below boundary = higher confidence

### Prediction Outcome
Signal predicts: **Daily maximum will be BELOW bucket boundary**
- **True Positive**: Signal fires AND actual daily max < boundary
- **False Positive**: Signal fires AND actual daily max ≥ boundary

## Implementation Constraints

### Data Quality Gates
1. **Minimum observations**: ≥ 100,000 observations per station
2. **Temporal density**: ≥ 30% coverage (≥ 1 observation every 2 minutes on average)
3. **Missing data handling**: Skip days with >50% missing observations
4. **Instrument calibration**: Flag stations with implausible temperature jumps (>5°F between consecutive minutes)

### Station Exclusion List
Based on data coverage analysis:
- **KNYC**: EXCLUDED (27K obs, 2% coverage)
- Stations with <100K observations: Evaluate case-by-case
- Stations with >50% missing data in critical hours (12:00-20:00 UTC): Flag for review

### Bucket Boundary Selection
Goldilocks is most effective for boundaries where:
1. **Liquidity exists**: 75°F, 80°F, 85°F, 90°F thresholds
2. **Temperature variability**: Boundaries in the middle of station's annual range
3. **Observation density**: Sufficient 1-minute observations near boundary

## Performance Expectations

### Backtest Metrics
From existing implementation:
- **Instant cross revert**: Precision ~55-65%, Recall ~40-50%
- **Trend extrapolation**: Precision ~50-60%, Recall ~30-40%

### Station-Specific Performance
Expected performance variation by station:
- **High-performing**: Stations with dense observations and clear diurnal cycles (KMDW, KATL)
- **Medium-performing**: Stations with moderate observation density (KDEN, KPHX)  
- **Low-performing**: Stations with sparse data or marine influence (KNYC, KSEA)

### Seasonal Variation
- **Summer**: More boundary crossings at higher thresholds (85°F, 90°F)
- **Winter**: Fewer signals, lower thresholds (30°F, 35°F)
- **Transition seasons**: Mixed performance

## Integration with Trading System

### Signal Weighting
Goldilocks signals should be weighted by:
1. **Station coverage factor**: 0.0 for KNYC, 1.0 for stations with >80% coverage
2. **Time-of-day confidence**: Higher weight for signals after 20:00 UTC
3. **Spike delta factor**: Smaller delta = higher weight

### Position Sizing Impact
Maximum position adjustment from Goldilocks:
- **Strong signal** (high confidence, late day): ±10% position size
- **Weak signal** (low confidence, early day): ±5% position size
- **KNYC signals**: IGNORE (0% weight)

### Conflict Resolution
When Goldilocks conflicts with primary ensemble:
1. **High-confidence Goldilocks** (>0.7) vs **medium-confidence ensemble** (<0.6): Reduce position
2. **Medium-confidence Goldilocks** (0.4-0.7) vs **high-confidence ensemble** (>0.7): Small reduction
3. **Low-confidence Goldilocks** (<0.4): Ignore conflict

## Implementation Recommendations

### Phase 1: Data Validation (2 hours)
1. **Station coverage audit**: Verify observation counts for all 19 stations
2. **KNYC exclusion**: Implement hard exclusion for KNYC
3. **Data quality metrics**: Calculate coverage %, gap statistics

### Phase 2: Threshold Calibration (3 hours)
1. **Optimize thresholds**: Test TRANSIENT_DELTA_THRESHOLD from 0.2-0.5°F
2. **Boundary selection**: Identify most effective bucket boundaries per station
3. **Time-of-day optimization**: Test LATE_DAY_UTC_HOUR from 16-20

### Phase 3: Integration (2 hours)
1. **Weight calculation**: Implement station-specific weighting
2. **Conflict resolution**: Define rules for ensemble-Goldilocks conflicts
3. **Performance monitoring**: Add metrics tracking

## Gray Room Input Needed

1. **KNYC handling**: Should we attempt to fill gaps with nearby stations (KLGA, KJFK) or accept exclusion?
2. **Threshold calibration**: Are the current thresholds (0.3°F, 0.5°F, 0.2°F) optimal or should they be station-specific?
3. **Signal weighting**: What maximum weight should Goldilocks have in combined probability?
4. **Missing data imputation**: Should we interpolate short gaps (<5 minutes) in ASOS data?
5. **Seasonal adjustment**: Should thresholds or confidence calculations vary by season?