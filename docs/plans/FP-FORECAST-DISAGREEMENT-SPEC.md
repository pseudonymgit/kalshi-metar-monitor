# FP-FORECAST-DISAGREEMENT-SPEC.md

## Item 5: forecast_disagreement Geo-Holdout Gap

### Problem Statement
The `forecast_disagreement` signal drops 30 percentage points accuracy when tested on held-out stations — it learns station-specific spread-to-direction relationships that don't generalize across geography.

### Proposed Solution
Three approaches to fix station-specific overfitting:

1. **Station-Normalized Spread**:
   - Divide raw spread by station's historical variance
   - `normalized_spread = raw_spread / station_variance`
   - Makes spread comparable across stations

2. **Per-Station Training**:
   - Train separate models for each station
   - Use station-specific calibration data
   - Avoid pooling across stations

3. **Regime Region Filter**:
   - Use `regime_region_filter` to exclude problematic stations
   - Pool within climate zones only
   - Northeast vs South vs Midwest etc.

### Data Needed
- Station variance statistics from historical data
- Climate zone definitions for regime filtering
- Holdout validation results per station

### Code Needed
- `core/station_normalization.py` — Spread normalization per station
- `core/per_station_training.py` — Station-specific model training
- Updates to `regime_region_filter` implementation
- Validation framework for geographic generalization

### Build Effort
- **Phase 1**: Station analysis (8 hours) — Variance calculations
- **Phase 2**: Normalization (6 hours) — Spread normalization implementation
- **Phase 3**: Per-station training (10 hours) — Individual model training
- **Phase 4**: Validation (8 hours) — Geographic holdout testing
- **Total**: 32 hours (4 days)

### Risk Level
**MEDIUM** — Normalization approach may not capture all station differences

### Priority
**P2** — Important for geographic robustness but not core accuracy

### Dependencies
- Completion of station variance analysis
- Climate zone definition validation

### Improvement Suggestion
**Implement station-normalized spread** — Divide forecast spread by each station's historical variance to create a comparable metric across stations. This should reduce the geographic holdout gap from -30pp to -5-10pp, making the signal more robust across different station types.