# NWP Direct Forecast Signal — Implementation Spec

## Source: Gray Room Expert Panel (2026-07-21)
- Expert 1 (Meteorological Statistics): Vetted 92.7% as real, not seasonal cycle artifact
- Expert 2 (Signal Fusion / Quant Finance): Bayesian log-odds fusion strategy
- Expert 3 (Operational Meteorology): Model hierarchy, variable selection, forecast horizon

## 1. Signal: `NwpDirectSignal`

### Location
`core/signals/nwp_direct_signal.py`

### Interface
Implements `BaseSignal` (same as other signals):
- `name` property: returns `"nwp_direct"`
- `min_lookback` property: returns `0` (no METAR lookback needed)
- `evaluate(idx, days)` → `(direction, confidence)` — **NOT IMPLEMENTED** (no station context from days list)
- `evaluate_for_station(station, date, conn)` → `(direction, confidence)` — PRIMARY interface

### Core Logic

```
For each station, for each date:
  1. Load temperature_2m_max from NWP DB for today's target_date
  2. Load temperature_2m_max from NWP DB for tomorrow's target_date
  3. If tomorrow > today → UP; if tomorrow < today → DOWN; else → NO SIGNAL
  4. Multi-model ensemble: repeat for each available model (GFS, ECMWF, ICON, GEM)
  5. Confidence = (up_votes + down_votes) / total_votes  (fraction agreeing on majority direction)
  6. If GFS data available AND no other models → GFS-only mode, confidence = 0.75
```

### Data Source
- Table: `nwp_forecasts`
- Column: `temperature_2m_max` (verified: 25,609 records, Jan 2025 - Jul 2026)
- Join: consecutive `target_date` values for same `station` + `model` + `variable`
- Note: `fetch_date` is the date the forecast was pulled from API. `target_date` is the date the forecast is FOR. The pairwise comparison uses consecutive `target_date` values, which may come from different `fetch_date` values (different model runs). This is acceptable — the model's forecast for consecutive days is the best available directional signal.

### Cache Strategy
- Cache `(station, date, model) → temperature` lookups in memory
- Cache `(station, date) → (direction, confidence)` once computed
- Cache size: ~20 stations × 365 days × 5 models = 36,500 entries max

### Validation
GFS directional accuracy on 20 stations: **92.7%** (1,863/2,010 predictions)
- Best station: KDCA 96.3%
- Worst station: KSFO 85.0%
- vs calendar_climatology baseline: 64.3% → GFS +28.4pp

## 2. Deseasonalization Validation Test

### Protocol
For each (station, date) pair:
1. Compute GFS-predicted tomorrow vs today direction
2. Compute calendar_climatology-predicted direction (historical average for tomorrow vs today)
3. Compare both against actual settlement direction
4. McNemar's test on discordant pairs (GFS correct, climatology wrong) vs (GFS wrong, climatology correct)

### Test Complete
✅ GFS significantly beats climatology (chi²=448.5, p≈0)
✅ GFS wins 88.6% of disagreements (655/739)
✅ All 20 stations show GFS > climatology by 16-41pp

## 3. Model Selection

### Primary Model: GFS
- Accuracy: 92.7% (2,010 predictions)
- Coverage: All 20 stations, Jan 2025 - Jul 2026
- Real operational forecast (not reanalysis)

### Secondary Model: ECMWF
- Accuracy: 84.6% (264 predictions) — limited sample, not statistically robust
- Typically superior to GFS in operational meteorology
- Use when available to supplement GFS

### Tertiary Model: ERA5
- Accuracy: 84.2% (10,179 predictions)
- **WARNING**: ERA5 is a reanalysis (retrospective best estimate), not a forecast
- Expert 3 recommends: NEVER use as primary directional signal for forward-looking positions
- Use ONLY for validation and historical analysis

### Excluded: ICON, GEM
- No overlap with settlement data in current DB
- Data only from 2026

### Recommendation
- Primary: GFS (proven accuracy, wide coverage)
- Ensemble: GFS + ECMWF + ICON + GEM when available (model agreement = confidence)
- ERA5: validation only

## 4. Confidence Assignment

### Multi-Model Mode
When 2+ models have data:
- confidence = max(up_votes, down_votes) / total_votes
- Range: [0.5, 1.0] (0.5 = tied, 1.0 = unanimous)

### GFS-Only Mode
When only GFS data is available:
- confidence = 0.75 (conservative, below GFS's 92.7% accuracy)

### Future Enhancement: Per-Station Calibration
- Track GFS accuracy per station over time
- Adjust confidence based on recent performance

## 5. Fusion Strategy

### Bayesian Log-Odds Fusion (per Expert 2)

```
w_NWP = log(p_NWP / (1 - p_NWP))
w_METAR = log(p_METAR / (1 - p_METAR))

fused_log_odds = (w_NWP * nwp_signal + w_METAR * metar_signal) / (w_NWP + w_METAR)
fused_probability = expit(fused_log_odds)  # logistic sigmoid
```

### Estimated Weights
- GFS: p=0.927 → w=2.53
- Best METAR combo: p=0.723 → w=0.96
- Ratio: NWP gets ~2.6x weight of METAR signal

### Disagreement Protocol
When GFS and METAR disagree:
- If GFS confidence > 0.85 → use GFS direction (92.7% accuracy justifies)
- If GFS confidence ≤ 0.85 → defer trade (skip)
- When both agree → high confidence, increase position size

## 6. Registration

### `core/signals/__init__.py`
```python
from .nwp_direct_signal import NwpDirectSignal

# In create_signal_registry():
'nwp_direct': NwpDirectSignal(db_path=db_path),
```

### `core/paper_trading_engine.py`
- Wire into signal evaluation pipeline
- Integrate with AgreementGate and ensemble voting
- Apply dewpoint modulator where applicable

## 7. Combinatorial Search Integration

### Add to Signal List
After registering, add `nwp_direct` to the signal list in combinatorial search:
- Total signals: 10 (was 9 with goldilocks removed)
- New combos: test all 2^10 - 1 = 1,023 combos (up from 511)
- Expect: GFS + METAR combos to outperform METAR-only combos

## 8. Expert Test Protocol

### Test 1: GFS vs Climatology (COMPLETE)
- Compare GFS directional accuracy vs calendar_climatology on same dates
- McNemar's test for paired nominal data
- Result: GFS +28.4pp, p≈0 ✅

### Test 2: GFS + METAR Combo Comparison
- Run combinatorial search with nwp_direct included
- Compare best combos with vs without nwp_direct
- Expected: nwp_direct+METAR-combo > METAR-only-combo

### Test 3: Out-of-Sample Validation
- Hold out most recent 30 days
- Train on everything before that
- Verify GFS accuracy on held-out data

### Test 4: Model Agreement Analysis
- When all 4 models (GFS, ECMWF, ICON, GEM) agree → expected accuracy >95%
- When only GFS fires → expected accuracy 92.7%
- When no model fires → no trade