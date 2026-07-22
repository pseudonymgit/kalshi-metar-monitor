# HRRR Collection Scope Document

## 1. HRRR Data Overview

**High Resolution Rapid Refresh** (HRRR) is NOAA's 3km resolution North American weather model providing hourly forecasts, ideal enhancement for Kalshi market predictions.

### Key Variables for Kalshi Markets
- `temperature_2m`: Current, min, max for HIGH/LOW temperature markets
- `temperature_2m_max`: Daily maximum for HIGH contracts
- `temperature_2m_min`: Daily minimum for LOW contracts
- `windspeed_10m`: Wind speed for any wind-based markets
- `surface_pressure`: Pressure readings for meteorological analysis
- `dewpoint_2m`: Dewpoint for moisture/comfort considerations
- `precipitation_sum`: Total precipitation accumulation

## 2. API Details

**Open-Meteo HRRR Endpoint:**
- URL: `https://apidata.meteostat.net/v2/hourly/hrrr`
- Rate Limits: 10,000 calls per day (generous free tier)
- Granularity: Hourly (real-time & forecast)
- Historical Coverage: 2 years back from current date
- Grid Resolution: 3km x 3km covering 50 US states

**Alternative Access:**
- Native NOAA API: Raw GRIB2 files, larger download volumes required
- Requires additional processing vs. the clean Open-Meteo wrapper

## 3. Storage Architecture

### Database Schema Considerations
Same NWP DB schema can be adapted with additions:

```sql
CREATE TABLE hrrr_forecasts (
    id INTEGER PRIMARY KEY,
    datetime_utc TEXT,           -- Full timestamp (24x more granular than GFS)
    station_icao TEXT,          -- Associated airport code
    latitude REAL, 
    longitude REAL,
    temperature_2m REAL,
    temperature_2m_max REAL,
    temperature_2m_min REAL,
    windspeed_10m REAL,
    surface_pressure REAL,
    dewpoint_2m REAL,
    precipitation_sum REAL,
    -- Additional 3km-specific parameters
    elevation REAL,
    grid_latitude REAL,
    grid_longitude REAL,
    forecast_horizon_hours INTEGER,
    data_collection_time DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Estimates:**
- Data volume: ~20 cities × 8,760 hours/year × 10 variables ≈ 1.75 million rows/year
- Daily additions: 20 stations × 24 hours × 10 variables = 4,800 new rows/day
- Storage impact: ~1GB per year with current schema structure

## 4. Collection Implementation Options

### Option 1: Standalone HRRR Collector (Recommended)
- Separate script: `scripts/hrrr_collect.py`
- Dedicated cron job: Hourly updates between 10-20 minutes past hour
- Independent processing pipeline maintaining data integrity separately
- Isolated error handling without impacting other NWP collection

### Option 2: Enhanced NWP Integration
- Extend `nwp_collect.py` with HRRR support flag
- Combined processing reduces devops overhead
- Risk of cross-integration issues if one feed has problems

### Option 3: Hybrid Pipeline
- Standalone collection feeding into general NWP storage layer
- Maintains independence while enabling signal processing reuse
- Best of both worlds but requires careful architecture planning

## 5. Market Enhancement Potential

### New Applications Enabled
1. **Intraday Confidence Updates**: Hourly refresh cycles boost Kalshi confidence
2. **Short-Term Contracts**: Enables 2-6 hour Kalshi contract predictions (if available)
3. **Daily Evolution Tracking**: Better capture temperature evolution curves (min/max swings)
4. **Real-Time Adjustments**: Sub-day model confidence adjustments based on latest reads
5. **Micro-Climate Sensitivity**: Higher resolution detects urban heat islands, coastal effects

### Expected Value
- Higher resolution = better accuracy for short-range weather markets (days 1-2)
- More frequent updates = faster reaction to model drift
- Better capture of rapidly evolving conditions (fronts, storms, etc.)

## 6. Trade-offs and Considerations

### Advantages
- **Spatial Resolution**: 3km vs. GFS 25km resolution (8x more detail)
- **Temporal Resolution**: Hourly vs. GFS 6-hourly updates (6x more frequent) 
- **Coverage**: Includes all US markets, focused on North American precision
- **Quality**: Operational analysis refined daily by NOAA with high reliability

### Disadvantages  
- **Geographic Limitation**: US-only focus (adequate for 20 city portfolio)
- **Computational Cost**: 48x more data points per unit time (6x frequency × 8x variables)
- **Latency**: Hourly updates might lag behind real-time METAR observations

### Risk Mitigation
- Maintain fallback to GFS/other data sources
- Careful monitoring of API usage caps (10k/day = ~2+ years if only 20 station calls)
- Implement caching layers to optimize repeat queries

## 7. Effort Estimation

### Development Time Estimate
- **Small (2-3 weeks)**: Basic HRRR integration with existing patterns
- **Medium (3-5 weeks)**: Robust implementation with error handling, monitoring, schema optimization
- **Large (5+ weeks)**: Full pipeline redesign incorporating historical backfill and advanced processing

### Recommended Timeline: **Medium effort (~4 weeks)**
- Week 1: Prototype integration & testing
- Week 2: Full implementation & error handling  
- Week 3: Integration with existing signals/verification
- Week 4: Production deployment & monitoring

### Resource Requirements
- Development time: 4-6 person-weeks
- Testing infrastructure for 8760-hour time series
- Potential additional storage for granular data

## 8. Historical Backfill Capabilities

### Open-Meteo Historical Range
- **Availability**: Up to 3-4 years of HRRR data available via API
- **Backfill Strategy**: Can backfill 2-3 years of hourly data
- **Value**: Significant historical dataset for training improved models
- **Execution**: Batch backfill job separate from real-time collection

### Considerations
- API rate limiting becomes important with large historical requests
- May require dedicated backfill schedule outside peak usage windows
- Estimated 3km coverage backfill: 10 years × 8760 hours = 87,600 data points per station

---

## Conclusion

HRRR integration presents a **high-value** opportunity for weather model enhancement with manageable development effort. The 48x increase in temporal/spatial resolution makes it compelling for intra-day market predictions and better model confidence scoring. Recommended to proceed with Medium-effort timeline for robust implementation.