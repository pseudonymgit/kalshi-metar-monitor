# Backfill Operations Status — 2026-07-31 15:10 UTC

## Running Backfills

| Backfill | PID | Runtime | Status | Progress |
|----------|-----|---------|--------|----------|
| TIGGE ECMWF 51-member | 815062/815063 | 2 min | Running | 2 parallel workers, 2020-01 and 2020-02 |
| ERA5 reanalysis | 771500 | 13h 26m | Running | 31,772 rows, 17/20 stations complete |
| WeatherAPI | 771277 | 13h 31m | Running | 99K daily, 2.38M hourly rows |

## Completion Percentages

### TIGGE ECMWF (51-member ensemble)
- **Target:** 78 months × 20 stations × 9 steps = ~1.4M rows
- **Current:** 0 rows (just started)
- **Progress:** 0%
- **ETA:** ~2-4 hours with 2 parallel workers
- **Bottleneck:** ECDS API staging time (2-10 min per request)

### ERA5 (reanalysis daily max/min temps)
- **Target:** 20 stations × 5 years = 36,500 rows
- **Current:** 31,772 rows (17 stations complete, 3 partial)
- **Progress:** 87%
- **ETA:** ~1-2 hours for remaining 3 stations + KPHX 2025
- **Complete stations:** KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX (partial)
- **Remaining:** KSAT, KSEA, KSFO (2022-2025), KPHX (2025)

### WeatherAPI (hourly + daily weather)
- **Target:** 20 stations × 16 years (2010-2026) = ~116,800 daily rows
- **Current:** 99,212 daily rows, 17 stations in checkpoint
- **Progress:** ~85% of daily records
- **Bottleneck:** 1.5s API rate limit (API terms)

## Key Deliverables Complete

1. ✅ **`data/kalshi_settlements.db`** — 6,070 records, finalized + historical_api
2. ✅ **`scripts/kalshi_settlement_upkeep.py`** — daily cron for Kalshi API reconciliation
3. ✅ **OpenClaw cron job installed** — `kalshi-settlement-upkeep` at 08:00 UTC
4. ✅ **`scripts/tigge_backfill.py`** — rebuilt from bytecode, ECDS MARS format verified
5. ✅ **`scripts/parallel_tigge_backfill.py`** — multiprocess parallel runner (2 workers)

## Files Created/Restored

| File | Size | Purpose |
|------|------|---------|
| `scripts/kalshi_settlement_upkeep.py` | 11.9 KB | Daily Kalshi DB upkeep |
| `scripts/tigge_backfill.py` | 21.9 KB | Rebuilt from bytecode, ECDS MARS format |
| `scripts/parallel_tigge_backfill.py` | 7.7 KB | Parallel TIGGE backfill runner |
| `scripts/sweep/__init__.py` | — | Sweep package (rebuilt) |
| `scripts/sweep/config.py` | 1.7 KB | Sweep config (rebuilt) |
| `scripts/sweep/data.py` | 5.1 KB | Sweep data loader (rebuilt) |
| `scripts/sweep/kalshi_sweep_eval.py` | 19.2 KB | Kalshi sweep evaluator (rebuilt) |
| `docs/weather-engine/kalshi_pipeline_report_2026-07-31.md` | 5.1 KB | Full report |

## Notes

- **Sweep rebuild is halted** per Dan's instruction. The 5,000-config sweep against Kalshi ground truth was run once (88% accuracy, negative PnL with real fees). Will re-run after ECMWF data arrives.
- **TIGGE backfill** uses the rebuilt ECDS MARS format pipeline. 2 workers running in parallel. Each worker handles one month of data for all 20 stations.
- **ERA5** is on the old CDS API (cds.climate.copernicus.eu), not ECDS. The script was started before the repo corruption and is working correctly.
- **WeatherAPI** uses a 1.5s rate limit per API terms. Cannot be parallelized without additional API keys.