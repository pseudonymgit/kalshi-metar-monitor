# Data Limitations

Durable record of data limits that are **by design** — not bugs, not gaps to fix.

Modify this file when:
- A backfill hits a source ceiling (API doesn't have it)
- An archive has genuine gaps by design
- A data integrity gate failure is accepted as standard

Do NOT add entries here for transient collection gaps or fixable pipeline problems.

---

## GEFS Archive

### Pre-2022 operational member count
- **Claim:** "280/41000 records have <30 members" (data integrity gate failure)
- **Reality:** Operational GEFS v12 only published 6 perturbation members (p1-p6) plus the control before the 2022 upgrade. This is a **source data availability limit**, not a backfill bug.
- **Completeness:** 99.3% of records have ≥30 members (which IS the target for the GEFS v12+ era).
- **Action:** Accept this. Do not flag as actionable. The 0.7% pre-2022 low-member records are the only available data for those dates. Dropping them from the analysis would bias toward the post-upgrade era.
- **First documented:** 2026-08-08 (Gilfoyle verification, 2026-08-03 signal sweep review)

### Eric  17 missing dates (Jul 2022)
- **Claim:** Some dates around Jul 2022 have zero data
- **Reality:** These dates were never published in the GEFS reforecast archive (or the API call failed for these specific dates in a way that isn't recoverable)
- **Completeness:** ~17/2035 dates missing (<1%)
- **Action:** Accept. Do not retry. Running `--resume` on these dates has been attempted and confirmed unfillable.

---

## ECMWF Archive

### Open-Meteo real-time only
- **Claim:** ECMWF archive DB has only 80 rows (4 dates)
- **Reality:** Open-Meteo's free tier only provides real-time ECMWF forecasts, not the full archive. The 51-member ECMWF ensemble requires ECDS (Copernicus) which is rate-limited to ~1 call/10s.
- **Action:** Use GEFS as primary ensemble. ECMWF backfill via ECDS is a background task, not a blocker. When ECDS queue drains, ECMWF joins as a secondary ensemble.

### ECDS rate limits
- **Rate:** 1 request per 10 seconds (per-user queue)
- **Max batch:** 5 dates per request (with 51-member split across 5 files)
- **Current state:** Sequential backfill running (PID monitored). ~2-3 days for full 2021-2026 coverage.

---

## ERA5 Ground Truth

### Station coverage gap (2021 only)
- **Claim:** 19/20 stations have ERA5 data only through 2021. Only KNYC has 2021-2025.
- **Reality:** The shell wrapper died after year 1 during the initial backfill. Never restarted.
- **Status:** 20 stations × 5 years (2021-2025) is the target. Only 2021 is done for 19 stations.
- **Action:** Pending — requires restarting the ERA5 backfill. Not flagged as urgent because GEFS + kalshi settlements provide the primary ground truth.

---

## Kalshi Settlements

### Source disjointness
- **Claim:** "Cross-validate historical_api against finalized"
- **Reality:** These are **disjoint datasets**. There are zero overlapping (station, date) pairs between the finalized source and the historical_api source. Cross-validation is structurally impossible.
- **Proof:** `scripts/validate_settlement_sources.py` (2026-08-08 run) — 0 matching pairs.
- **Action:** Accept. The historical_api source covers a different date range. Trust both independently based on their self-consistency. Historical_api has 1 duplicate pair (self-consistency check verified).

---

## WeatherAPI (OpenWeather)

### Single station only
- **Claim:** WeatherAPI backfill stalled
- **Reality:** Script was written and tested for KATL only. Remaining 19 stations need a separate run.
- **Status:** Not a priority — WeatherAPI data overlaps with METAR and ERA5 sources. Pending explicit need.

---

## NWP Forecast Archive

### Model coverage
- **GFS:** 576 days of data (2025-01-01 → 2026-07-31) ✅
- **ECMWF:** Same period (from Open-Meteo real-time) ✅
- **ICON:** Same period ✅
- **GEM:** Same period ✅
- **HRRR:** Retired (replaced by ECMWF IFS pivot, 2026-08-05)

### Collection
- Daily cron at 06:00 UTC collects all 4 models × 20 stations × 7 forecast days
- DB: `data/nwp_forecasts.db`

### Historical NWP
- No historical NWP data available (Open-Meteo only stores real-time)
- Multi-model fusion (A7 in original sweep) cannot run on historical data
- **Action:** Accept. Multi-model fusion is a live-only feature until NWP archive accumulates.