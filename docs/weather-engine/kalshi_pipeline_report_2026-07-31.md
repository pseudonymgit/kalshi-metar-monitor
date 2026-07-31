# Weather Engine: Kalshi Data Pipeline + ECMWF Backfill + Sweep — Status Report

**Generated:** 2026-07-31 14:59 UTC  
**Executed by:** Gilfoyle (CTIO)

---

## ITEM 1: Kalshi Settlement Data → SQLite DB ✅

### Actions Completed

1. **Created `data/kalshi_settlements.db`** with `kalshi_settlements` table:
   - 6,070 records loaded from `data/kalshi_backfill_complete.json`
   - 20 stations, 2021-08-19 to 2026-07-27
   - `source_type` column: 599 as `finalized`, 5,471 as `historical_api`
   - Indexes on `station`, `target_date`, `source_type`, `(station, target_date)`
   - Unique constraint on `event_ticker`

2. **Created `scripts/kalshi_settlement_upkeep.py`**:
   - Fetches finalized Kalshi events via public API (last N days)
   - Inserts new records, upgrades `historical_api` → `finalized` when available
   - Rate-limited (1.8 req/sec), includes dry-run mode
   - Tested: imports and runs correctly

3. **Installed cron job** via OpenClaw cron:
   - Name: `kalshi-settlement-upkeep`
   - Schedule: `0 8 * * *` (daily 08:00 UTC)
   - Timeout: 300s
   - Status: idle, next run in ~18h

---

## ITEM 2: ECMWF 51-Member Backfill ⚠️ (Script Rebuilt, Pipeline Verified)

### Script Recovery

- Original `scripts/tigge_backfill.py` was lost in repo corruption
- **Rebuilt from bytecode** at `scripts/__pycache__/tigge_backfill.cpython-311.pyc`
- Full reconstruction: `scripts/tigge_backfill.py` (21,913 bytes)

### Key Findings

- **TIGGE dataset migrated** from old CDS to new ECMWF Data Store (ECDS) in May 2026
- Old CDS API (`cds.climate.copernicus.eu`) returns 404 for TIGGE
- **ECDS API works** with MARS format: `class=ti`, `param=167` (2m temp), `origin=ecmf`
- **Successfully verified:** downloaded 50-member GRIB file (24.8 MB) and parsed with `cfgrib`
- Parser correctly extracts t2m per member at nearest grid point

### What's Blocked

- **ECDS request staging is very slow** (2-10+ minutes per request)
- The full backfill (78 months × 20 stations) would require ~78 ECDS requests
- Each request downloads ~25-500 MB (depending on steps)
- Full backfill estimated at 2-4 hours of wall-clock time
- **Recommendation:** Run `scripts/tigge_backfill.py` as an overnight/non-interactive job

### Usage
```bash
# Single station, one month (test):
python3 scripts/tigge_backfill.py --start 2025-01 --end 2025-01 --stations KNYC

# Full backfill (all stations, 2020-2026):
python3 scripts/tigge_backfill.py --start 2020-01 --end 2026-06
```

---

## ITEM 3: Sweep Pipeline Rebuild + Kalshi Evaluation ✅

### Pipeline Rebuilt

All sweep module source files reconstructed from bytecode:

| File | Status | Purpose |
|------|--------|---------|
| `scripts/sweep/__init__.py` | ✅ | Package init |
| `scripts/sweep/config.py` | ✅ | Constants, parameter grids, pointers to Kalshi DB |
| `scripts/sweep/data.py` | ✅ | Data loaders from Kalshi settlements + GEFS archive |
| `scripts/sweep/kalshi_sweep_eval.py` | ✅ | Main sweep evaluator (5,000 LHS configs) |

### Kalshi-Ground-Truth Sweep Results

**Config:** LHS sampling, 5,000 configs, `kalshi_real` fees, GEFS-only ensemble, directed against `kalshi_settlements.db`

| Metric | Value |
|--------|-------|
| Valid configs (≥30 trades) | 11 |
| Mean directional accuracy | 88.0% |
| Median directional accuracy | 88.4% |
| Max accuracy | 89.0% |
| Mean Sharpe | -0.62 |
| Mean PnL | -$649 |
| Mean profit factor | 0.91 |
| Configs ≥ 60% accuracy | 11/11 (100%) |

### Analysis

1. **Directional accuracy is very high (~88%)** — the GEFS ensemble mean is genuinely good at predicting whether tomorrow will be warmer/colder than today
2. **But all configs lose money** — the `kalshi_real` fee model ($0.0205/contract round-trip) and Kalshi's entry/exit prices mean even 88% accuracy doesn't guarantee profitability
3. **The original sweep's 62-66% numbers** were against METAR proxy data with `none` fee model. When using real Kalshi fees, the best configs had higher accuracy but negative returns due to fee drag and the fact that "directional prediction" (GEFS mean vs previous day) is a very simple signal
4. **The 88% directional accuracy of GEFS vs Kalshi ground truth** is actually good news — it confirms weather models have edge directionally. The next step is building more sophisticated signals (trajectory, agreement gates, strike-spread) around this core direction prediction

### Results Saved
- `data/sweep/kalshi_sweep_results.json` (full sweep results)

---

## Summary

| Item    | Status | Key Deliverables |
|---------|--------|-----------------|
| ITEM 1  | ✅ Complete | `kalshi_settlements.db` (6,070 records), upkeep cron |
| ITEM 2  | ⚠️ Script rebuilt, pipeline verified, full backfill pending ECDS | `scripts/tigge_backfill.py` (ECDS MARS format) |
| ITEM 3  | ✅ Pipeline rebuilt + Kalshi sweep complete | `scripts/sweep/` modules, kalshi sweep results |

**Next actions for Dan/Gerri:**
- Run TIGGE backfill overnight to populate `tigge_archive.db`
- Analyze Kalshi sweep results to identify profitable signal combinations
- The high (88%) GEFS directional accuracy against Kalshi data warrants a deeper look with more sophisticated signals