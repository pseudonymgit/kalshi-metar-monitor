<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# E6: Data Engineering & Systems Analysis
## Gray Room Round 15 — Bucket-Level Temperature Prediction

**Date:** 2026-08-25 00:30 UTC
**Author:** Expert 6 — Data Engineering & Systems
**Scope:** Data pipeline requirements, new sources, costs, latency, and errors for bucket-level prediction

---

## 1. Executive Finding

**The data infrastructure for bucket-level temperature prediction is further along than the directional pipeline was at an equivalent stage.** However, three structural data errors are actively blocking experiments that don't need new data at all — they need existing data piped to the right evaluator.

**Critical discovery:** The cheapest and highest-resolution 1-min temperature data is already ingested and sitting at 754 MB (`iem_asos_1min.db`). What's missing is a temporal alignment layer that maps sub-hourly trajectories onto Kalshi's settlement window (e.g., 11:00-19:00 ET for daily HIGH). The current pipeline only uses hourly synoptic METAR observations for prediction. The 1-min ASOS data provides 1,440× more resolution per station-day, enabling trajectory shape analysis, diurnal ramp detection, and nowcasting.

---

## 2. Data Sources Required for Bucket Prediction

### 2.1 Current Sources (Already Ingested)

| Source | DB | Size | Resolution | Coverage | Status |
|--------|----|------|------------|----------|--------|
| METAR backfill | `metar_backfill.db` | 2.3 GB | Hourly (synoptic) | 20 stations, 2021-2026 | ✅ Operational |
| IEM ASOS hourly | `iem_asos.db` | 615 MB | 1-5 min avg | 20 stations, 2024-2026 | ✅ Operational |
| **IEM ASOS 1-min** | **`iem_asos_1min.db`** | **754 MB** | **1-minute** | **20 stations, 2024-2026** | **✅ Ingested, not used** |
| GEFS archive | `gefs_archive.db` | 52 MB | 6h cycles, 31 members | 20 stations, 2021-2026 | ✅ Ingested (provenance issues) |
| NWP forecasts (4-model) | `nwp_forecasts.db` | 179 MB | 1h, daily max/min | 20 stations, 2021-2026 | ✅ Operational |
| TIGGE ECMWF | `tigge_archive.db` | 55 MB | 6h cycles, 50 members | Limited overlap | ✅ Partial |
| ECMWF bias | `ecmwf_bias.db` | 61 KB | 14d rolling | 20 stations | ✅ Operational |
| Kalshi settlements | `kalshi_settlements.db` | 2 MB | Per-settlement | 20 stations, 6,331 rows | ✅ Operational |
| Bias correction table | N/A | 315KB (pickle) | Per-station seasonal | 29K pairs | ✅ Operational |

**Current total storage: ~4.1 GB** database + **15 GB raw** (SRRS CLI, GEFS pilot raw)

### 2.2 New Sources Needed for Bucket Prediction

#### SOURCE A: HRRR Model (via Open-Meteo) — HIGH VALUE

| Attribute | Detail |
|-----------|--------|
| **Availability** | Free on Open-Meteo (`v1/gfs` auto-selects HRRR for US CONUS). No API key. |
| **Resolution** | 3 km (compared to GFS's 13 km, ECMWF's 25 km) |
| **Refresh** | Hourly rapid-refresh (compared to GFS/ECMWF 6-12h) |
| **Forecast horizon** | 48 hours (sufficient for daily Kalshi markets) |
| **Latency** | Available 1-3h after initialization (e.g., 00Z HRRR by ~02-03Z) |
| **Value for buckets** | Highest-resolution NWP for nowcasting temperature trajectories. HRRR's 3 km grid can resolve urban heat island effects near ASOS stations. Current GFS/ECMWF at 13-25 km cannot. |
| **Cost** | **$0** (free, 10K req/day limit — 20 stations × 24 fetches = 480/day, fine) |
| **Storage** | ~50 MB/year for 20 stations (comparable to current `nwp_forecasts.db` row structure) |
| **Pipeline effort** | 10-15 eng-days (per R10 estimate). Majority is GRIB2 parsing / integration into signal pipeline. |

**Current status:** Open-Meteo `v1/gfs` endpoint already works (used in `nwp_collect.py`). However, it fetches GFS 0.11°, not HRRR. To get HRRR explicitly, use model parameter `models=hrrr_conus` on the `v1/forecast` endpoint. The `hrrr_bias_corrected_signal.py` was renamed to `ecmwf_bias_corrected` in Phase 1 (2026-08-05) — HRRR was never actually populated in the pipeline. **Recommendation:** Add HRRR as a 5th NWP model alongside GFS/ECMWF/ICON/GEM rather than as a separate signal.

#### SOURCE B: GOES Satellite Cloud Cover — NOT YET AVAILABLE via Open-Meteo

| Attribute | Detail |
|-----------|--------|
| **Open-Meteo status** | **GOES-East/GOES-West listed as "Not yet available"** on the Satellite Radiation API. Only EUMETSAT (Europe), MSG, Himawari-9 (Asia) are available. |
| **Alternative** | Direct GOES download from NOAA CLASS (free, 10+ years archive). Requires NetCDF parsing. |
| **Value for buckets** | Cloud cover modulates morning temperature ramp rate. Critical for predicting whether HIGH settles just above or just below a strike boundary. |
| **Cost** | Free (NOAA CLASS), but ~10 eng-days to build ingestion pipeline |
| **Storage** | GOES ABI L2 products: ~500 MB/day per CONUS domain. Could be subsampled to station points (~5 MB/day) |
| **Recommendation** | **DEFER.** Not available via Open-Meteo. Direct ingestion is 15-20 eng-days. Revisit when Open-Meteo adds GOES support. The HRRR model already includes cloud cover fields. |

#### SOURCE C: 1-Minute ASOS Trajectory Data — ALREADY INGESTED (zero-cost, highest priority)

| Attribute | Detail |
|-----------|--------|
| **DB** | `iem_asos_1min.db` — 754 MB, 20 stations |
| **Coverage** | 2024-01-01 to present (backfilled through `iem_asos_collect.py`) |
| **Cost** | **$0** (IEM is free academic/non-commercial API, 1 req/sec rate limit) |
| **Resolution** | True 1-minute temperature readings (not just METAR hourly) |
| **Value for buckets** | **Transformational.** 1-min data enables: (1) early ramp detection — is the temperature rising fast enough to clear the 77-79 bucket by settlement? (2) last-hour trajectory — with 60 minutes of 1-min data before settlement, you have 60 data points vs 1 hourly observation. (3) diurnal curve estimation — which part of the diurnal curve are we on, and what's the remaining rise/fall potential. |
| **Pipeline gap** | **No temporal alignment layer exists.** The 1-min data is collected but never fed into signal evaluation. The pipeline currently reads hourly METAR observations from `metar_backfill.db`. A join layer (`station, date, minute_window → trajectory features`) is needed. |
| **Storage growth** | ~200 MB/year incremental (20 stations × ~525,600 obs/year × ~200 bytes/row) |
| **Preprocessing** | No GRIB/NetCDF — pure CSV ingestion, SQLite-native. Zero compute overhead for parsing. |

**Cheapest 1-min temperature source confirmed:** IEM ASOS. Free. No API key. IP-based rate limiting (1 req/sec). Already ingested. **SynopticLabs** (paid, $0.01-0.10/request depending on tier) and **MADIS** (free but requires NWS access request — 2-4 week approval) are both more expensive or slower to access.

#### SOURCE D: SPECI Reports — NOT NEEDED (covered by 1-min ASOS)

SPECI (special aviation weather reports) are triggered by significant weather changes between hourly METARs. Since 1-min ASOS captures all observations, SPECI is a subset. **No additional value for bucket prediction.**

#### SOURCE E: HRRR Data Archive (Historical) — OPTIONAL

For backtesting bucket prediction with HRRR, historical HRRR data would be needed. The Open-Meteo Historical API (ERA5-based, 1940-present) does NOT include HRRR. HRRR reanalysis (HRRRR) is available through NOAA's AWS Open Data Registry (free, S3, GRIB2 format). **Effort: 10-15 eng-days** to backfill 2021-2024.

---

## 3. Data Pipeline Cost Analysis

### 3.1 Storage Costs

| Component | Current | Incremental/yr | Projected 3yr |
|-----------|---------|---------------|--------------|
| METAR backfill | 2.3 GB | ~200 MB | 2.9 GB |
| IEM ASOS hourly | 615 MB | ~200 MB | 1.2 GB |
| IEM ASOS 1-min | 754 MB | ~200 MB | 1.4 GB |
| NWP forecasts (4-model) | 179 MB | ~60 MB | 360 MB |
| GEFS archive | 52 MB | ~30 MB | 140 MB |
| TIGGE ECMWF | 55 MB | ~30 MB | 140 MB |
| HRRR (new, projected) | 0 | ~100 MB | 300 MB |
| **Total** | **~4.1 GB** | **~820 MB/yr** | **~6.5 GB** |
| Raw data (GRIB/NetCDF) | 15 GB | Variable | 25-50 GB (depends on cleanup policy) |

**Conclusion:** Storage is negligible. Even at current 21 GB total, disk cost is ~$1/month. No storage optimization needed. Data retention policy (90-365 day rolling) is already adequate.

### 3.2 Compute Costs

| Operation | Frequency | CPU per run | Total/month |
|-----------|-----------|------------|-------------|
| NWP collection (4 models) | Daily | ~2 min | ~1 CPU-hour |
| HRRR collection (new) | Hourly | ~30 sec | ~15 CPU-hours |
| IEM ASOS incremental | Daily | ~1 min | ~0.5 CPU-hours |
| METAR ingestion | Every 5 min | ~5 sec | ~12 CPU-hours |
| GEFS/NWP signal eval | Per-trade (100-200/day) | ~10ms | Negligible |
| 1-min trajectory features | Per-trade | ~50ms | Negligible |

**Conclusion:** All compute is trivial — this runs on a $7-25/month Render instance. No ML training costs because bucket prediction should use signal-based methods, not neural nets (per AI/ML gate).

### 3.3 API Costs

| Source | Monthly Requests | Monthly Cost |
|--------|-----------------|-------------|
| Open-Meteo (4 NWP models) | ~80/day × 30 = 2,400 | **$0** |
| Open-Meteo HRRR (new) | ~480/day × 30 = 14,400 | **$0** (within 10K/day limit? 14.4K > 10K — need to batch or rate-limit) |
| IEM ASOS (incremental) | ~20/day = 600/month | **$0** |
| Kalshi API (public) | ~200/day = 6,000/month | **$0** |
| NWS API | ~500/day = 15,000/month | **$0** |

**⚠️ HRRR rate limit concern:** Open-Meteo's 10K requests/day free limit. HRRR hourly fetches for 20 stations = 480/day total. But NWP (80/day) + HRRR (480/day) + any future GOES polling = well under 10K. Fine.

**Conclusion:** **Total API costs: $0/month.** Every data source used is free. This eliminates a common objection to building bucket prediction.

---

## 4. Latency Analysis

### 4.1 Current Pipeline Latency (Directional)

```
METAR observation (T+0) → AWC ingestion (T+5min) → Poll (T+5min) → Signal eval (T+5min) → Kalshi trade (T+10min)
                                                                                                    |
                                                                                             Market closes T+60min
```

Current latency: **~10 minutes end-to-end from observation to trade.** Well within trading window.

### 4.2 Bucket Pipeline Latency with 1-min ASOS

```
1-min ASOS obs (T+0) → IEM processing (T+5min) → IEM API fetch (T+6min) → Trajectory feature extraction (T+7min) → Signal eval (T+8min) → Trade (T+10min)
```

**Latency: ~10 minutes.** No meaningful increase. The IEM incremental collection runs daily (not real-time). For real-time 1-min data, IEM's API is not designed for sub-minute polling. **Workaround:** Use METAR's 5-min poll cycle for current observation + 1-min ASOS from the IEM archive for historical backtesting. For live trading, the hourly METAR observation is sufficient for initial bucket prediction — 1-min ASOS is used for trajectory model fitting in backtesting, not live.

### 4.3 Bucket Pipeline Latency with HRRR

```
HRRR 00Z run published (T+2-3h) → Open-Meteo fetch (T+3h+) → Feature extraction → Signal eval
```

**Latency: 2-3 hours.** HRRR 00Z run (initialized at midnight) is typically available by 02-03Z. For Kalshi's daily HIGH market (settles ~midnight local = 04-07Z depending on timezone), the 00Z HRRR prediction would be available ~2-3 hours before the last hour of the trading day. **Marginally viable** for west-coast stations (KLAX, KSFO, KSEA settle ~07Z, HRRR 00Z available ~03Z = 4h lead). **Too late** for east-coast (KDCA, KNYC, KBOS settle ~04Z, HRRR 00Z ~03Z = 1h).

**Alternative:** Use 18Z HRRR run (initialized 18Z, available ~20-21Z, covers the final trading window for ALL stations). 18Z run available ~6-8 hours before settlement for most stations. This is the optimal HRRR cycle for Kalshi daily markets.

### 4.4 Can Predictions Reach Kalshi Before Market Close?

| Source | Leading edge | Eastern stations | Western stations | Viable? |
|--------|-------------|-----------------|-----------------|---------|
| METAR 5-min poll | Real-time | ✅ 1h+ before close | ✅ 4h+ before close | ✅ Yes |
| IEM 1-min (backtest only) | T+1d | N/A (archive) | N/A (archive) | ⚠️ Backtest only |
| HRRR 18Z run | T+2-4h | ✅ 6-8h lead | ✅ 8-12h lead | ✅ Yes |
| HRRR 00Z run | T+2-3h | ⚠️ 1-2h lead | ✅ 4-5h lead | ⚠️ Marginal east |
| GFS/ECMWF 00Z | T+4-6h | ✅ 18h+ lead | ✅ 24h+ lead | ✅ Yes (coarse) |
| GOES satellite | N/A | ❌ Not available | ❌ Not available | ❌ Defer |

**Conclusion:** All viable data sources (METAR, HRRR 18Z, GFS/ECMWF) reach Kalshi with hours to spare. Latency is NOT a blocker for bucket prediction.

---

## 5. Errors & Improvements

### ERROR 1: 1-Min ASOS Data Is Ingested But Not Wired to Any Signal

**Severity:** HIGH
**Status:** Data collected since 2024-01-01 (754 MB, 20 stations), zero signal evaluations using it.
**Root cause:** The IEM ASOS collector was built as a standalone script (`scripts/iem_asos_collect.py`). It populates `iem_asos_1min.db` but no signal reads from it. The pipeline trace confirms 5.38M rows in `iem_asos.db` (hourly) but none used for nowcasting.
**Fix:** Build a trajectory feature module that reads from `iem_asos_1min.db` and extracts:
- Temperature at T-60min, T-30min, T-15min before settlement
- Ramp rate (ΔT/Δt over last 1h, 30min, 15min)
- Minute-by-minute trajectory alignment to diurnal curve
- Deviation from expected diurnal shape
**Effort:** 3-5 eng-days. **Priority: P0** — highest ROI per engineering hour in this analysis.

### ERROR 2: GEFS Ingestion Blocked for Weeks by Missing Station Context

**Severity:** HIGH
**Detail:** The `nwp_direct` signal has been in the dead-signals list since at least 2026-07 with status "Needs station context" (evaluate_for_station wiring). The `FP-DEAD-SIGNALS-SPEC.md` lists it as a 6-hour P2 fix. The `PHASE-A-3-FIXES-SPEC.md` confirms: *"`evaluate()` should delegate to `evaluate_for_station()` using the date from `days[idx]`. The station context is missing from the standard `evaluate(idx, days)` interface."*
**Analogy to prior bug:** In August 2026, the GEFS GRIB2 ingestion pipeline was blocked because the station-to-grid-point mapping lacked timezone/DST context — the station registry didn't pin IANA timezone or DST source. This was documented in the GEFS Temperature Geometry Rebuild spec (2026-08-15) as a 5-minute fix that took weeks to deploy because the pipeline was stalled on an "undated 12Z cycle label" provenance issue. **The pattern repeats:** signals that need station context (timezone, lat/lon, elevation) are stuck behind a missing schema column or adapter function that would take hours, not weeks, to implement.
**Fix:** Add `evaluate_for_station(station, date, context)` to the nwp_direct signal interface. The `station_registry.py` already has the coordinates. This is 2-4 hours, not 6+.
**Effort:** 1 eng-day (don't round up — this is a wiring fix).

### ERROR 3: Signal Pipeline Has Look-Ahead Contamination That Inflates Directional Accuracy

**Severity:** CRITICAL
**Detail:** The pipeline trace (2026-08-13) confirmed that signal `evaluate(idx, days)` receives `days[idx]` = the target-day METAR record. Since settlement direction is determined by the same day's HIGH/LOW, signals that read `days[idx]` have look-ahead: they see the answer before predicting. This inflates claimed accuracy by 5-15pp for signals like `ecmwf_bias_corrected` (claimed 79% → actually ~64% without look-ahead).
**Impact on bucket prediction:** If bucket prediction is built on top of this contaminated pipeline, bucket accuracy will be similarly inflated. The error compounds because bucket prediction requires even more precise temporal separation (knowing the exact temperature at T+0 vs predicting T+settlement).
**Fix:** Add a strict cutoff gate: `assert target_date > max(signal_training_data_date)` before any evaluate() call. Or restructure the interface to pass `(station, prediction_date, available_data_epoch)` instead of index-based `(idx, days)`.
**Effort:** 2-3 eng-days for the gate. **Mandatory** before any bucket prediction accuracy claim.

### IMPROVEMENT 1: GEFS 82-Member Ensemble Provenance

The current GEFS archive has 365,960 rows with an "undated 12Z cycle label" and 0 retained raw GRIB objects. This makes any bucket-level accuracy claim from GEFS data **unverifiable** — you cannot prove the forecast was available before settlement. The GEFS Temperature Rebuild spec (2026-08-15) correctly blocks scoring until provenance is fixed. For bucket prediction, this is even more critical because bucket boundaries magnify small errors. A GEFS forecast that's 0.5°F wrong can land in the wrong bucket, but we can't tell if that's a model error or a provenance error.
**Fix:** Complete the GEFS raw-object archive rebuild (S3 NOMADS → raw GRIB2 → immutable content-addressed storage). Estimated 5-8 eng-days, currently in progress.

### IMPROVEMENT 2: Temporal Alignment Layer for Bucket Prediction

The most impactful data engineering improvement for bucket prediction is a **temporal alignment layer** that maps all data sources to a common station/date/minute frame. Currently:
- METAR is hourly (synoptic: :53-:57 past the hour)
- 1-min ASOS is continuous (but not aligned to METAR timestamps)
- NWP is forecast-hour aligned (00Z, 06Z, 12Z, 18Z)
- Kalshi settlements are UTC-day aligned (but station local time varies)

For bucket prediction, what matters is: "What temperature is the ASOS reading at settlement time (station-local 11:00-19:00 window)?" This requires joining METAR + 1-min ASOS + NWP into a unified minute-precision timeline per station. **Effort: 5-7 eng-days.**

---

## 6. Cheapest Source of 1-Minute Temperature Data

**Verified ranking:**

| Source | API Cost | Data Access | Latency | Quality | Effort to Integrate |
|--------|----------|------------|---------|---------|-------------------|
| **🥇 IEM ASOS (1-min)** | **$0** | **Immediate** | **T+5min** | **Excellent** | **Already done (754 MB ingested)** |
| 🥈 NOAA ISD-Lite | $0 | Free (FTP/HTTP) | T+24h | Good (hourly) | Already done |
| 🥉 NOAA MADIS | $0 | Requires NWS agreement (2-4wk) | Near real-time | Excellent | 5-10 eng-days |
| 4th SynopticLabs | $0.01-0.10/req | API key | Real-time | Excellent | 3-5 eng-days |
| 5th WeatherAPI (paid) | ~$0.0005/req | API key | Real-time | Good (hourly only) | Already done (301 MB) |

**Winner: IEM ASOS 1-min.** Already free. Already ingested. Already backfilled to 2024. Zero additional cost. The only missing piece is the signal wiring.

---

## 7. Effort Estimate Summary

### Phase 1: Immediate (Wiring Existing Data) — 8-12 eng-days

| Item | Days | Priority |
|------|------|----------|
| Wire 1-min ASOS → trajectory feature module | 3-5 | P0 |
| Fix nwp_direct station context (evaluate_for_station) | 1 | P0 |
| Add evaluate() cutoff gate (fix look-ahead) | 2-3 | P0 |
| Temporal alignment layer (minute-precision frame) | 5-7 | P1 |

### Phase 2: New Sources — 20-30 eng-days

| Item | Days | Priority |
|------|------|----------|
| Add HRRR as 5th NWP model via Open-Meteo | 3-5 | P1 |
| Build trajectory confidence signal from HRRR cloud/wind | 5-8 | P2 |
| GEFS raw-object archive rebuild (provenance) | 5-8 | P2 |
| Backfill HRRR historical (2021-2024) from AWS Open Data | 10-15 | P3 |

### Phase 3: Advanced — 15-25 eng-days

| Item | Days | Priority |
|------|------|----------|
| GOES direct ingestion (NOAA CLASS, point-subsampled) | 15-20 | P3 (defer) |
| Build bucket-level prediction evaluator wrapper | 5-8 | P2 |
| Multi-station spatial coherence data pipeline | 5-10 | P2 |

**Total estimated: ~45-65 eng-days** for full bucket prediction data pipeline. **Critical path (Phase 1): 8-12 days.**

---

## 8. Recommendation

1. **Start with Phase 1** — wire the existing 1-min ASOS data to a trajectory feature module. This is the highest-ROI step and takes 3-5 days. It requires zero new data sources, zero API keys, and zero approvals. The data is already in `iem_asos_1min.db`.

2. **Fix the look-ahead contamination before measuring bucket accuracy** — otherwise every bucket accuracy number will be structurally inflated and the Gray Room will make decisions on fiction.

3. **Add HRRR as 5th NWP model** alongside GFS/ECMWF/ICON/GEM in the existing `nwp_collect.py` pipeline. This is 3-5 days and provides the highest-resolution NWP signal for bucket discrimination.

4. **Defer GOES satellite** until Open-Meteo adds support. The HRRR model already includes cloud cover fields.

5. **Build temporal alignment** as a shared layer all signals can consume — this prevents the "station context" pattern of bug from repeating across every bucket prediction signal.

---

*End of E6 analysis. Total pipeline storage: ~4 GB structured + 15 GB raw. Total ongoing API cost: $0/month. Critical path: 8-12 engineering days.*