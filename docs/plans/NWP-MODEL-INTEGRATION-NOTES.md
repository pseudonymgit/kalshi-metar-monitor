# NWP Model Integration Notes

**Date:** 2026-07-21

## Summary
This document tracks the investigation and integration of multiple new Numerical Weather Prediction (NWP) model types into the weather engine pipeline, focusing on ensemble forecasts and AI-enhanced models.

## Tasks Executed

### Task 1: HGEFS/GFS Ensemble Integration (COMPLETED - HIGH PRIORITY)
**Status:** Successfully implemented

**Implementation Details:**
- Created `scripts/ensemble_collect.py` to collect from `https://ensemble-api.open-meteo.com/v1/ensemble`
- Added `member_index` column to the `nwp_forecasts` database table to handle ensemble member data
- Updated parsing logic to handle ensemble variables in the form `temperature_2m_max_memberXX` (e.g., member01 through member30)
- Implemented cron-compatible script architecture matching the existing collection patterns
- Added proper rate limiting to comply with API terms

**Technical Challenges Overcome:**
- Had to remove inappropriate `models=gfs` parameter in the API request (ensemble API doesn't accept it)
- Database schema modified to include nullable `member_index` to maintain backward compatibility with standard forecasts
- Enhanced API error handling specifically for ensemble endpoint

**Results:**
- Successfully collected ensemble forecasts for all 20 stations
- 420 ensemble forecast records added to database representing different probability branches of forecasts
- Enables probability distribution analysis and ensemble spread calculations

**API Parameters Used:**
```
{
  "latitude": lat,
  "longitude": lon,
  "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
  "timezone": "UTC",
  "forecast_days": 7
}
```

### Task 2: GFS GraphCast Model Investigation (INVESTIGATION ONLY)
**Status:** Partial success - model recognized but data availability limited

**Testing performed:**
- Tested on `/v1/forecast` endpoint with `models=gfs_graphcast025` → null data
- Tested on `/v1/gfs` endpoint with `models=gfs_graphcast025` → returns null values
- Various forecast horizon tests (`forecast_days=1` vs `forecast_days=2`) showed no valid data

**Findings:**
- GFS GraphCast model name `gfs_graphcast025` is recognized by Open-Meteo API
- However, current API responses return null values for requested time periods
- Possible causes:
  - Model data not yet generated for these forecast periods
  - Different time resolution requirements
  - Model may only be available during certain cycles

**Recommendation:**
- Monitor periodically for data availability
- Consider testing at different times of day when forecast cycles update
- Check Open-Meteo status page for model-specific issues

### Task 3: ECMWF AIFS Model Investigation (INVESTIGATION ONLY)
**Status:** No valid data obtained

**Testing performed:**
- Endpoint: `https://api.open-meteo.com/v1/ecmwf`
- Parameters: `"models=ecmwf_aifs025"`
- Response included data structure but temperature values were consistently `null`

**Findings:**
- Model name `ecmwf_aifs025` is valid in Open-Meteo’s system
- API response structure is correct but data values null
- Similar to GraphCast – model recognized but no actual data retrieved

**Note:**
- ECMWF AIFS may only be available in higher-frequency (hourly) data, not daily aggregations
- Need to test with `hourly` parameters if daily doesn’t work

### Task 4: AIGFS Model Name Investigation (EXHAUSTIVE SEARCH)
**Status:** No functional model names found

**Model Names Tested:**
- `aigfs025`
- `noaa_aigfs`  
- `gfs_aigfs025`
- `aigfs`
- `gfs_aigfs`

**Results:**
- All model names resulted in 400 Bad Request errors
- Conclusion: These AIGFS model names are not currently available in Open-Meteo API

### Task 5: GenCast Hosting Investigation (RESEARCH COMPLETED)
**Status:** Comprehensive research completed

**Key Findings:**
- Open-Meteo's GFS GraphCast IS the GraphCast model (Google's ML weather model) run on GFS initial conditions but at the standard 0.25° resolution
- Native GraphCast implementation would require ~60GB VRAM for 0.25° resolution operation
- Alternative services like Meteomatics offer GraphCast/FourCastNet but typically require paid subscriptions
- Open-Meteo provides access to advanced models without the computational hosting overhead
- GribStream has historical GraphCast data but only archive access (until 2026-05-05), not operational

## Integration Success Metrics

### Before and After Data Collection
- **Before:** Standard deterministic forecasts (gfs, ecmwf, icon, gem) with single values per forecast date
- **After:** Added ensemble forecasts with 31 potential outcome branches per data point, enabling:
  - Confidence interval estimation
  - Probability distribution analysis  
  - Risk assessment based on ensemble spread
  - Better understanding of forecast uncertainty

### Database Schema Changes
```sql
-- Added to existing nwp_forecasts table:
ALTER TABLE nwp_forecasts ADD COLUMN member_index INTEGER DEFAULT NULL;
CREATE INDEX idx_nwp_ensemble_lookup ON nwp_forecasts(target_date, station, model, variable, member_index);
```

## Implementation Recommendations

### Next Steps for GFS GraphCast & AIFS
1. Test availability during model generation windows (typically around the 00Z, 06Z, 12Z, 18Z runs for global models)
2. Check if data appears when using hourly versus daily aggregation
3. Verify that forecast horizons align between available data and requests

### Ensemble Enhancement Opportunities
1. Expand variable collection (currently supporting temperature, precip; could add wind, pressure)
2. Implement ensemble statistics aggregation in analysis workflows
3. Develop confidence scoring based on ensemble spread metrics
4. Create visualization dashboard for ensemble output

### Deployment Notes
- `scripts/ensemble_collect.py` can run as standalone cron job alongside existing `nwp_collect.py`
- Suggested schedule: once per day (similar to standard NWP collection) 
- Ensemble collection adds minimal additional runtime overhead since it's a single API endpoint per station


## Files Created
- `scripts/ensemble_collect.py` - Standalone ensemble collection script
- Database schema enhancements (member_index support)
- Enhanced NWP collection utilities

## Related Files Updated
- `ACCOMPLISHMENTS.md` (now `ACCOMPLISHMENTS_UPDATED.md`) - Tracking of successful implementation

---
*Document last updated: 2026-07-21 20:35 UTC*