# Phase 2.5 — ERA5 Upper-Air Backfill Handoff

**Date:** 2026-07-19 04:42 UTC
**Scripts:** `scripts/era5_upper_air_backfill.py`, `scripts/setup_cds_api.sh`
**Test:** `tests/test_era5_upper_air.py`
**Previous:** `2026-07-19-phase2-backtest-handoff.md` (Phase 2.4 — Temperature Advection validation)

---

## Objective

Build and deploy a CDS API-based backfill script to download historical ERA5 pressure-level data (850-mb temperature, u/v wind components, 500-mb geopotential height) for all 20 Kalshi weather stations, compute 850-mb temperature advection, and store in the NWP database.

This enables historical validation of Signal 6 (Temperature Advection), which currently has only 19 live records and cannot be backtested.

## Current State

### What was built

| File | Purpose |
|---|---|
| `scripts/setup_cds_api.sh` | CDS API setup — checks/installs `cdsapi`, creates ~/.cdsapirc |
| `scripts/era5_upper_air_backfill.py` | Main backfill script (~920 lines) |
| `tests/test_era5_upper_air.py` | Integration test suite (~420 lines) |

### Backfill script features

- **CDS API integration** — uses the official `cdsapi` Python library to download ERA5 pressure-level data (variables: t, u, v, z at 850/500 mb)
- **Small-area requests** — 1°×1° bounding box around each station (~5×5 grid points at 0.25° resolution) enables gradient computation for advection
- **Temperature advection computation** — centered finite differences, `ADV_850 = -u·∂T/∂x - v·∂T/∂y`
- **Progressive downloading** — monthly chunks, checks DB for existing data before each request
- **Resumption support** — skips months already fully populated per station
- **Retry with backoff** — 5 retries, exponential backoff 30s→300s for CDS rate limits
- **Storage** — writes to existing `nwp_forecasts.db` using the EAV schema with `model='ERA5'` and variable names: `temperature_850hPa`, `wind_u_850hPa`, `wind_v_850hPa`, `geopotential_500hPa`, `temperature_advection_850hPa`

### Stations covered

20 Kalshi weather market stations: KATL, KBOS, KDEN, KDFW, KDTW, KEWR, KIAH, KJFK, KLAS, KLAX, KMIA, KMSP, KNYC, KORD, KPHL, KPHX, KSAN, KSEA, KSFO, KSLC

### Date range

2025-01-01 to present (2026-07-19) = ~18 months

## Next Actions

1. **Configure CDS API access** — run `bash scripts/setup_cds_api.sh` with a CDS API key (register free at https://cds.climate.copernicus.eu)

2. **Run tests** — `cd prototypes/weather-engine-source && python3 -m pytest tests/test_era5_upper_air.py -v`

3. **Execute backfill** — `python3 scripts/era5_upper_air_backfill.py`
   - This will make ~360 CDS API requests (20 stations × 18 months)
   - Estimated runtime: 6-12 hours depending on CDS queue
   - The script is resumable — safe to interrupt and restart

4. **Validate advection data** — after ≥30 days of data, re-run Phase 2.4 standalone backtest for Signal 6

5. **Deploy daily GFS upper-air collection** — the existing `nwp_collect.py` already fetches `temperature_850hPa` and `geopotential_height_500hPa` from GFS. Verify u/v wind components are being collected for live advection.

## Files Referenced

- `prototypes/weather-engine-source/scripts/era5_upper_air_backfill.py` — main script
- `prototypes/weather-engine-source/scripts/setup_cds_api.sh` — setup
- `prototypes/weather-engine-source/tests/test_era5_upper_air.py` — tests
- `prototypes/weather-engine-source/core/signals/temperature_advection_signal.py` — Signal 6 (uses advection data)
- `prototypes/weather-engine-source/data/nwp_forecasts.db` — target database
- `prototypes/weather-engine-source/.meta/continuity/weather-engine/2026-07-19-phase2-backtest-handoff.md` — previous handoff

## Stop Conditions

- **Escalate if:** CDS API rejects requests after multiple retries (API key issue or service outage)
- **Escalate if:** NetCDF processing produces all-NaN results for any station
- **Stop if:** Backfill completes successfully — signal will still need ≥30 days of historical data for meaningful backtest

## Escalation Path

- **Technical (Gilfoyle):** CDS API connectivity, NetCDF parsing, advection computation bugs
- **Strategic (Gerri):** Whether to invest in daily CDS polling vs. relying on GFS for live data
