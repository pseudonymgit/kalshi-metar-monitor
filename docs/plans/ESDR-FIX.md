# ESDR Signal Fix — Ensemble Member Data Backfill Plan

## Current State (2026-07-22)

### Problem
The `esdr` signal (`core/signals/esdr_signal.py`) was producing **100% errors** during calibration. Two issues were identified:

1. **CRITICAL BUG (FIXED):** The signal was using a passed-in `conn` parameter (from the METAR/historical database `weather_data.db`) to query `nwp_forecasts`. The METAR database's `nwp_forecasts` table lacks the `member_index` column, causing `OperationalError: no such column: member_index`. Fixed by making `evaluate_for_station()` always self-resolve its own NWP database connection.

2. **ENSEMBLE DATA GAP (UNFIXED):** The NWP database only contains the ensemble **control forecast** (member_index=30, 420 rows across 20 stations) — not the full 31-member ensemble. The signal gracefully returns `(None, 0.0)` for all stations now (no errors), but cannot fire because `member_count < 10` everywhere.

### Database schema issue
The `nwp_forecasts` table in `data/nwp_forecasts.db` has `member_index` column but the UNIQUE constraint is:
```sql
UNIQUE(fetch_date, target_date, station, model, variable)
```
This prevents storing multiple ensemble members for the same forecast — the UNIQUE constraint **must** include `member_index` for per-member storage to work.

The `weather_data.db` also has an `nwp_forecasts` table but **without** the `member_index` column at all.

## What Ensemble Data Is Needed

The ESDR signal computes Ensemble Spread Divergence Rate:
- **Input:** 31 ensemble members with `temperature_2m_max` forecasts for each station
- **Logic:** Computes IQR (interquartile range) across members at each forecast target date, then measures whether spread is widening (ratio > 1.5x across consecutive dates)
- **Threshold:** Needs ≥10 distinct members to compute meaningful IQR; wants full 31
- **Model:** HGEFS (ECMWF Ensemble) or GEFS (GFS Ensemble)

## Data Source Options

### Option 1: Open-Meteo (Limited — Recommended for ongoing collection)
- **What's available:** Open-Meteo does NOT expose individual ensemble member values through its standard API.
- **What IS available:** `precipitation_probability` based on GEFS 30-member ensemble, but not raw member temperatures.
- **Verdict:** Cannot use Open-Meteo for ESDR. OK for ongoing deterministic forecasts.

### Option 2: CDS API (Copernicus Data Store) — ECMWF Ensemble
- **Source:** https://cds.climate.copernicus.eu/cdsapp#!/dataset/10.24381/cds.a4f2f9e6?tab=overview
- **Data:** ECMWF IFS HRES + ENS (50 members) at 0.4° resolution
- **Variables:** `2m_temperature` available as instantaneous fields
- **Format:** GRIB or NetCDF
- **Requirements:**
  - CDS API key (free registration)
  - `cdsapi` Python package
  - ~2 GB storage for regional subset per run
  - Covers all 20 Kalshi cities
- **Limitations:** ~6-hour latency, 24h forecast cycle

### Option 3: NOMADS / NCEP — GEFS
- **Source:** https://nomads.ncep.noaa.gov/
- **Data:** GEFS 30-member ensemble at 0.5° resolution
- **Variables:** `tmp2m` (temperature at 2m)
- **Format:** GRIB2
- **Requirements:**
  - Direct HTTP download (no API key)
  - `cfgrib` or `xarray` + `cfgrib` for parsing
- **Limitations:** GEFS resolution (0.5°) is coarser than ECMWF (0.4°)

### Option 4: AWS Open Data — GEFS
- **Source:** `s3://noaa-gefs-pds/`
- **Data:** GEFS v12, full ensemble, global at 0.25° resolution
- **Format:** GRIB2
- **Advantage:** Free, high-res, easy to subset

## Recommended Approach

### Phase 1: Schema Migration (immediate)
1. Modify `nwp_forecasts` table UNIQUE constraint to include `member_index`:
   ```sql
   -- Drop old constraint (SQLite needs table recreate for constraint change)
   CREATE TABLE nwp_forecasts_new (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       fetch_date TEXT NOT NULL,
       target_date TEXT NOT NULL,
       station TEXT NOT NULL,
       model TEXT NOT NULL,
       variable TEXT NOT NULL,
       value REAL,
       fetch_timestamp TEXT NOT NULL,
       member_index INTEGER DEFAULT NULL,
       UNIQUE(fetch_date, target_date, station, model, variable, member_index)
   );
   INSERT INTO nwp_forecasts_new SELECT * FROM nwp_forecasts;
   DROP TABLE nwp_forecasts;
   ALTER TABLE nwp_forecasts_new RENAME TO nwp_forecasts;
   -- Recreate indexes
   CREATE INDEX idx_nwp_lookup ON nwp_forecasts(target_date, station, model, variable);
   ```
2. Update `scripts/nwp_collect.py` to create the updated schema

### Phase 2: Backfill Script (next sprint)
Create `scripts/backfill_ensemble.py` that:
1. Uses AWS Open Data (`s3://noaa-gefs-pds/`) as primary source
2. For each of the 20 Kalshi cities, downloads GEFS member data for temperature_2m
3. Extracts max temperature per member per target date
4. Stores each member as a separate row with `model='gefss'` and `member_index=0..30`
5. Covers historical period matching existing NWP coverage (~2025-05-01 to present)

### Phase 3: Ongoing Collection (next sprint)
1. Add `gefss` (GEFS full ensemble) as a new model to the daily NWP collection
2. Update `scripts/nwp_collect.py` to either:
   - Fetch from AWS Open Data daily
   - OR use a GEFS-to-Open-Meteo bridge
3. Store each member as a separate row

### Phase 4: Activate ESDR (after Phases 1-3)
1. ESDR signal will auto-detect >10 members and begin firing
2. Monitor spread_ratio distribution to calibrate trigger threshold (current 1.5x is provisional)
3. Add spread-from-forecast-hour (hour 2 vs hour 6) logic when hourly ensemble data is available

## Effort Estimate

| Phase | Effort | Dependencies |
|-------|--------|-------------|
| Phase 1 (Schema) | 1 hour | None |
| Phase 2 (Backfill) | 2-3 days | AWS access, GRIB parsing tools |
| Phase 3 (Ongoing) | 1-2 days | Phase 1 complete |
| Phase 4 (Activate) | 1 day | Phases 1-3 complete |

## Files That Need Changes

| File | Change |
|------|--------|
| `scripts/nwp_collect.py` | Schema creation + ensemble fetch logic |
| `scripts/backfill_ecmwf.py` | May need updates for new schema |
| `core/signals/esdr_signal.py` | ✅ ALREADY FIXED — connection bug resolved |
| Data migration | New `scripts/backfill_ensemble.py` |

## Alternative: Simplified Approach

If full ensemble members are too expensive/complex, consider replacing ESDR with a simpler spread proxy:
- **Intra-model spread:** Compare GFS vs ECMWF vs ICON vs GEM forecast values for the same target date
- **Lag-based spread:** Compare yesterday's forecast vs today's forecast for the same target date
- **Model-consensus spread:** Standard deviation across all available NWP models

These would not require new data and could be implemented in <1 day.