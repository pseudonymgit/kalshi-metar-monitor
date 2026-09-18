# ASOS Wind/Pressure Calibration Supplement

**Purpose:** Use ASOS 1-minute wind and pressure data (80-100% coverage across all 20 stations) to improve calibration curves for pressure- and wind-dependent signals in the current pipeline.

## Background

The IEM ASOS database (`data/iem_asos.db`) contains 5.37M observations across 20 stations. While temperature only has 8-10% coverage (except KNYC at 99.8%), **wind (sknt, drct) and pressure (alti) have true 1-minute resolution** at 80-100% coverage for all 20 stations.

This means we have a high-resolution ground truth for variables that several of our signals depend on.

## Signals That Could Benefit

### 1. pressure_delta / corrected_pressure_delta
- Current: Uses model forecast pressure or hourly METAR pressure
- ASOS: ~1-min pressure readings — can compute true pressure tendency with much higher resolution
- **Opportunity:** Validate and calibrate short-term pressure change thresholds using ASOS ground truth
- **Sweep parameter:** `use_asos_pressure_calibration` (bool)

### 2. wind_direction_shift (frontal passage)
- Current: Uses METAR wind at ~hourly intervals
- ASOS: ~1-min wind data — can detect wind shifts with minute precision
- **Opportunity:** Build true wind shift climatology per station, calibrate detection thresholds
- **Sweep parameter:** `use_asos_wind_calibration` (bool)

### 3. Cold front detection (lane-level)
- Current: Frontal passage nowcast uses METAR + NWP
- ASOS: 1-min wind + pressure can confirm/deny frontal passages with high precision
- **Opportunity:** Use ASOS as validation truth for front arrival time and intensity
- **Sweep parameter:** `use_asos_frontal_validation` (bool)

### 4. Pressure drift/bias correction
- ASOS altimeter pressure is calibrated to sea level — compare against model surface pressure
- Build per-station pressure bias tables (time-of-day, seasonal)
- Flag stations with known pressure sensor drift

## Implementation

### Phase 1: Extract Statistics (can run now)
```python
SELECT station, 
       COUNT(*) as n,
       MIN(alti) as min_p, MAX(alti) as max_p,
       AVG(alti) as mean_p, STDEV(alti) as std_p,
       MIN(sknt) as min_w, MAX(sknt) as max_w,
       AVG(sknt) as mean_w, STDEV(sknt) as std_w
FROM asos_observations
WHERE alti IS NOT NULL
GROUP BY station
```

### Phase 2: Build Calibration Tables
- Per-station pressure tendency climatology (1-hour, 3-hour, 6-hour changes)
- Per-station wind speed/direction climatology with diurnal and seasonal breakdowns
- Cross-validate against METAR ground truth

### Phase 3: Sweep Integration
- Add `use_asos_pressure_calibration` and `use_asos_wind_calibration` as boolean sweep parameters
- When enabled, replace METAR-based wind/pressure references with ASOS-derived values
- Compare calibration accuracy with and without ASOS enrichment

## Notes
- This is a **supplement** — it improves existing signal calibration, not a new signal
- The main bottleneck is signal quality (all 5 signals derive from METAR) — ASOS wind/pressure only helps a subset
- Should be tested as part of the main sweep, not as a standalone change
- KNYC has 99.8% temp coverage too — if we use ASOS for KNYC wind/pressure, we might also get temp calibration for KNYC

## Files
- DB: `data/iem_asos.db`
- Schema: station, valid, tmpf, dwpf, sknt, drct, alti
- Script: `scripts/backfill_iem_asos.py`
