# Pipeline Trace: Data → Signals → Fusion → Calibration

**Expert:** Pipeline Trace (Data → Signals → Fusion → Calibration)
**Date:** 2026-08-13 (UTC)
**Scope:** Stages 1–5 (raw data ingestion through calibration); fusion UWC + LLOP.
**Method:** Direct DB inspection (sqlite3) + targeted `grep -n`/line reads of `prototypes/weather-engine-source`. All file:line evidence verified against source on 2026-08-13.

---

## Verdict summary

| Stage | Input source | Output | Reality status | Evidence |
|---|---|---|---|---|
| 1a. Kalshi settlements | Kalshi API (live + events backfill) | 9,487 rows | **REAL** (with caveats) | `data/kalshi_settlements.db`; ws5_common.py:26-94 |
| 1b. METAR observations | AWC TGTFP, IEM ASOS, NOAA ISD-Lite, NWS API, WeatherAPI | 12.8M rows, 73 stations, 2021-01-01→2026-08-13 | **REAL** | `data/metar_backfill.db`; backfill_metadata |
| 1c. GEFS archive | NOAA GEFS (31 members) | 365,960 rows, 2021-01-02→2026-08-14 | **REAL** (aggregated; provenance = fetch logs) | `data/gefs_archive.db` |
| 1d. TIGGE/ECMWF | TIGGE ECMWF ensemble | 181,400 rows, 2021-08-19→2026-08-06 | **REAL** (only 1 of 2 expected sources) | `data/tigge_archive.db` |
| 1e. NWP forecasts | ecmwf, era5, gefs_ens, gem, gfs, icon | 480,460 rows | **PARTIAL** (mixed backfill/live; era5 is reanalysis, not forecast) | `data/nwp_forecasts.db` |
| 1f. IEM ASOS | Iowa Environmental Mesonet | 5.38M rows, 20 stations, 2024-01-01→2026-08-04 | **REAL** | `data/iem_asos.db` |
| 1g. Station mapping | Kalshi discovery → cache → settlement_epochs → static list | 20 stations | **PARTIAL** (Kalshi discovery "will fail — DNS in container"; static list is the de facto source) | core/station_registry.py:141-168 |
| 2. Signal evaluation | 17 registered signals | (direction, confidence) | **PARTIAL** — data real, but **3 signals structurally one-directional** and **ecmwf_bias_corrected's backtest path is a METAR momentum heuristic, not ECMWF** | core/signals/__init__.py:110-132; warm_momentum_signal.py:139; cooling_failure_signal.py:155; radiational_cooling_signal.py:400-430; ecmwf_bias_corrected_signal.py:313-345 |
| 3. Ground truth | kalshi_temp[today] vs [yesterday], ε=0.5°F | 'up'/'down'/None | **REAL** (dedup verified: finalized > historical_api) | ws5_common.py:105-127, 26-94 |
| 4. Fusion (LLOP) | calibrated signal probs → weighted log-odds | fused prob | **PARTIAL** — math sound, weights default to equal when no MI matrix; calibration inputs are the leaky signal outputs (see §7) | core/fusion_logic.py:530-590, 590-660 |
| 5. Fusion (UWC) | Beta-binomial pool posteriors → agreement gate | 0.4839 acc, 279 trades | **REAL measurement, zero edge** — below naive-yesterday baseline (0.4845) and far below climatology (0.624) | data/signal_fusion_comparison.json (2026-08-13T06:16); data/baseline_comparison.json (2026-08-13T06:31) |
| 6. Calibration | Isotonic (PAVA) per signal×city, trained on data ≤ cutoff | calibrated P(correct) | **REAL structure, leaky inputs** — train/test split honored (cutoff=2024-12-31) but the "predictions" fed to it already contain the answer (see §7) | core/calibration_pipeline.py:209-260; scripts/run_sweep.py:102; ws5_common.py:430-470 |

---

## 1. Raw data ingestion

### 1a. Kalshi settlements — REAL
- `data/kalshi_settlements.db`, table `kalshi_settlements`: 9,487 rows, 2021-08-19 → 2026-08-10.
- Source-type split (verified by query): `finalized` = 2,390 (598 with source=NULL + 1,792 `live_api`), `historical_api` = 7,097 (3,411 `events_backfill` + 1,465 `historical_api` + 2,220 `live_api`-typed + 1). Matches the 2,390/7,097 claim.
- Sample values are plausible real settlements (e.g., KLAX KXHIGHLAX 2026-05-28 = 66.0°F, id=600).
- **Caveats:** (1) ~600 `finalized` rows have `source=NULL` — provenance gap; (2) a small number of rows carry `series='KXHIGHTEST'` and `station='TEST'` — test-market rows inside the settlement table (would be excluded by station registry filter, but present). 20 real stations + TEST.
- Dedup logic verified: `ws5_common.py:26-30` SOURCE_PRIORITY `finalized(0) > historical_api(1) > live_api(2)`; `deduplicate_settlements()` at ws5_common.py:36-94 keeps first row per (station, date) ordered by source_type — since ORDER BY sorts alphabetically, `finalized` wins. **Verified in code — REAL.**

### 1b. METAR backfill — REAL
- `data/metar_backfill.db` (2.4 GB): `metar_observations` 12,822,053 rows, 73 stations, 2021-01-01T00:00 → 2026-08-13T06:28 (near-live). `daily_stats` 58,063 rows. `settlement_epochs` 98,640 rows (2021-01-01 → 2026-07-26). `station_climatology` 4,774 rows (20 stations, day-of-year means/stds).
- Sources (verified): `awc_tgftp` (16.7K), `iem_asos_backfill` (921K), `iem_asos_backfill_43` (10.4M), `isd-lite` (1.2M), `nws_api` (82K), `weatherapi_history` (148K), etc. — all real NOAA/NWS/IEM/WeatherAPI channels.
- **Gaps:** `raw_metar` is NULL for a large share of the early period (sample rows from 2021-01-01 have `raw_metar=None`); only parsed fields. `backfill_metadata` says `date_range_start=2021-01-01`, `end=2026-07-07` with source `NOAA_ISD_Lite_HTTPS`, `total_stations=21` — the original backfill covered 21 stations; the 73-station breadth comes from later ingestion. 2026-07-27 → 2026-08-13 present in metar_observations but settlement_epochs stops 2026-07-26.
- **Stale-station check:** climatology exists for exactly 20 stations (matches the 20-station trading universe); observations table has 73 stations (includes extra airports never traded).

### 1c. GEFS archive — REAL (aggregated)
- `data/gefs_archive.db`: `gefs_archive` 365,960 rows, 20 stations, target 2021-01-02 → 2026-08-14, with `ensemble_mean/min/max`, `member_values`, `n_members`, `init_cycle`, `fetch_timestamp`. Real NOAA GEFS 31-member output as fetched. Note target dates extend to 2026-08-14 (forecast horizon beyond "today" 2026-08-13).

### 1d. TIGGE/ECMWF — REAL but incomplete
- `data/tigge_archive.db`: 181,400 rows, **only source `tigge_ecmwf`**, 2021-08-19 → 2026-08-06. The "82-member ensemble" (GEFS 31 + ECMWF 51) therefore depends on this single-source TIGGE archive; `ecmwf_archive.db`, `gefs_operational.db`, `gefs_reforecast.db` are all **0 bytes** (empty) — the dedicated ECMWF channels were never populated.

### 1e. NWP forecasts — PARTIAL
- `data/nwp_forecasts.db`: `nwp_forecasts` 480,460 rows; models `ecmwf, era5, gefs_ens, gem, gfs, icon`; target 2021-01-02 → 2026-08-19; 20 stations; `fetch_log` 661 rows. **Problems:** (1) `era5` is a reanalysis (past observations), not a forecast — including it as an "NWP forecast" contaminates any skill claim; (2) coverage is uneven (backfill_progress table shows per-station status); (3) not all 6 models × 20 stations × full date range are complete. The "30 days of Edge 20 collection" is only the recent live slice; the rest is backfill.

### 1f. IEM ASOS — REAL
- `data/iem_asos.db`: `asos_observations` 5,377,113 rows, 20 stations, 2024-01-01T00:00 → 2026-08-04T23:51, with `source` + `collected_at` and a `collection_tracking` table (20 rows). Genuine Iowa Environmental Mesonet 1-minute/5-minute data. The 1-min variant (`iem_asos_1min.db`, 790 MB) also exists. Real.

### 1g. Station mapping — PARTIAL
- `core/station_registry.py:141-168`: fallback chain is **1. Kalshi discovery → 2. cache file → 3. settlement_epochs table → 4. static list**. The docstring itself says "Kalshi discovery (will fail — DNS in container)" (line 21, 153). So in practice the authoritative source is the **static hardcoded list** `_RESEARCH_STATION_CODES` (verified against Kalshi settlement data, comment at line 42). It's real (matches the 20 settlement stations), but it is NOT series-based discovery as the mission brief hoped — it's a hardcoded fallback that became the primary.

## 2. Signal evaluation — PARTIAL (real data, degenerate/leaky logic)

- `REGISTERED_SIGNAL_NAMES` = 17 names (`core/signals/__init__.py:110-132`): gaussian, gaussian_v2, pressure_delta, forecast_disagreement, calendar_climatology, corrected_pressure_delta, seasonal_regime, ecmwf_bias_corrected, metar_trend, wind_direction_shift, eighty_two_member_ensemble, spike_reversion, radiational_cooling, warm_momentum, cooling_failure, asos_microstructure, temperature_trend.
- **Dead in practice:** `seasonal_regime` fired 0 times in the latest fusion run (`data/signal_fusion_comparison.json` per_signal: `"seasonal_regime": {"confidence": 0.0, "fire_rate": 0.0, "fired": 0}`). Registry comments (__init__.py:16-18) confirm 10 signals were removed earlier for ρ=0.000 — the registry has churned hard.

### Degenerate (structurally one-directional) signals — CONFIRMED
- `warm_momentum` → returns **only `'up'`** (`core/signals/warm_momentum_signal.py:139`); never emits 'down'. Its "directional accuracy" is just the base rate of up days when it fires.
- `cooling_failure` → returns **only `'up'`** (`core/signals/cooling_failure_signal.py:155`). Same.
- `radiational_cooling` → returns **only `'down'`** (`core/signals/radiational_cooling_signal.py` evaluate docstring + flow: 'down' if radiational cooling detected, else None).
- `spike_reversion` → both directions in principle (`_evaluate_high` at line 145 returns 'down' after up-spike), but HIGH-path only fires on up-spikes → predict-down, LOW-path only on down-spikes → predict-up (line 173+). Direction is mechanically determined by the setup, not by evidence about the next day.
- Any claimed "0%/100% directional split" for these is therefore **structural, not empirical**.

### ecmwf_bias_corrected at 79% — ARTIFACT, not real ECMWF skill
- The backtest path `evaluate()` (`core/signals/ecmwf_bias_corrected_signal.py:313-345`) does **NOT touch ECMWF data at all**: `direction = 'up' if today > yesterday else 'down'` using METAR daily highs, confidence from 3-day trend consistency. It is a same-day METAR momentum heuristic.
- The real-ECMWF path `evaluate_for_station()` (line 352-395) exists but: (a) compares forecast mid-temp to a **hardcoded 70°F neutral baseline** (`direction = 'up' if mid_temp > 70.0 else 'down'`) — a temperature-LEVEL classifier, not a day-over-day direction predictor; (b) requires `STATIONS` lat/lon and the ecmwf_bias.db; it is not what the 79% figure measures.
- **Why 79% is fiction:** the backtest computes direction from `days[idx]` vs `days[idx-1]` where `idx` = the target day's METAR row, and the ground truth is the same day's Kalshi direction (see §3/§7). METAR high direction and Kalshi settlement high direction for the same physical day are highly correlated because they measure the same quantity → ~79% is same-day correlation, **not predictive skill**.

### Look-ahead in evaluate() index handling — CONFIRMED
- `scripts/ws5_common.py:437-440`: `metar_idx = metar_date_to_idx[today]`; `signal.evaluate(metar_idx, metar_days)`. Signals that use `days[idx]` (the target day) — e.g., ecmwf_bias_corrected (`days[idx].get('high')`), warm_momentum (`highs[-1]` = today) — **see the full target-day METAR record at prediction time**. Since settlement direction is determined by the observed target-day high, the "prediction" is made after the outcome is knowable.

## 3. Ground truth — REAL

- `compute_ground_truth_direction(temp_today, temp_yesterday, epsilon=0.5)` at `scripts/ws5_common.py:105-127`: 'up'/'down'/None(flat). Uses `kalshi_temp` directly, not settlement_bucket. 0.5°F epsilon confirmed.
- Dedup `finalized > historical_api` verified in code (ws5_common.py:26-94, see §1a). **REAL.**
- **Coverage skew:** per-station settlement counts are highly uneven (KMDW ≈ 1,581 vs KSAT ≈ 85). Consequence: per-station accuracy claims on thin stations (KSAT, KPHL, KMIA, KHOU in the UWC run had 3-8 trades) are statistical noise; only KMDW/KDFW-style high-coverage stations support any claim. Aggregated numbers are dominated by KMDW's weight.

## 4. Fusion (LLOP) — PARTIAL

- `core/fusion_logic.py:32+` `SignalFusionEngine`: Layer 0 = per-signal×city isotonic calibration (`calibrate()` → P(correct)), Layer 1/2 = Log-Odds Linear Opinion Pool (`fuse_signals()` at line 590+). Implementation is standard: clamp p to [0.01,0.99], log-odds, weight, expit.
- **Weights:** `compute_fusion_weights()` (line 540+) uses MI-based decorrelation **only if** an MI matrix is supplied; otherwise `current_weights = [1/n]*n` (equal weights). `fuse_signals()` (line 611-617) falls back to equal weights when sizes mismatch. In the latest run the LLOP path used whatever weights the caller set; the honest reading is **equal-weight LLOP** unless a caller passes an MI matrix.
- **LLOP 63.84% claim:** could not be reproduced from current artifacts — no file in `docs/` or `data/` carries 0.6384 for LLOP. The closest documented number is `data/signal_accuracy_report.json` (2026-07-02) with per-station "Reversion" accuracy 63.5–67% — but that file also shows **impossible values** (`brier_score` 1.008, `avg_conf_correct` 1.348 — confidences above 1.0), so it is a legacy/uncalibrated artifact, not evidence of real edge. **Treat the 63.84% LLOP figure as unverified.**
- Whether LLOP measures real edge: it inherits the §2/§7 leak — calibrated inputs are "predictions" that already contain the target day's observation, so any favorable LLOP number is inflated by the same same-day-correlation artifact.

## 5. Fusion (UWC) — REAL measurement, NO edge

- `core/signal_fusion.py:125+` `PoolOfPools`, `UncertaintyWeightedCascade` (line 78+ `FusionModeConfig`): pools → within-pool Beta posteriors via `compute_pool_beta` (n_eff correction) → combined Beta → agreement gate `n_agreement_gate: int = 4` (line 81).
- Pool definitions `CANONICAL_POOL_DEFINITIONS` (core/signals/__init__.py:141+): only 4 pools (gefs, ecmwf, climatology, eighty_two_member) with **hardcoded `rho_within` values (0.85/1.0)** and hardcoded freshness hours. The task's mention of a "fixed pool name mapping" is consistent with the `_POOL_SIGNAL_TO_REGISTRY` mapping (init.py:170-177), but the priors/correlations remain fixed constants — **not calibrated from data**.
- **0.4839 is a genuine measurement, and it is bad:** `data/signal_fusion_comparison.json` (generated 2026-08-13T06:16Z): `fusion_method="uwc", accuracy=0.4839, total_trades=279` (135/279 correct). Same-morning baseline (`data/baseline_comparison.json`, 06:31Z): naive_yesterday **0.4845**, three_day_avg 0.4281, seasonal_same_month 0.5549, climatology **0.624**. So UWC (48.4%) ≈ naive persistence (48.5%) and **far below a trivial climatology baseline (62.4%)**. It's not a structural bug in the number — it's a structurally uninformative system. Also note coverage collapse: only 279 trades total (vs baselines' ~4,700–5,400), i.e., the fusion gate is so strict it trades ~5% of days.

## 6. Calibration — REAL structure, leaky inputs

- `core/calibration_pipeline.py:209+` `refit()`: PAVA isotonic regressors, per-signal×city, with global fallback tiers; y clipped [0.05,0.95]; walk-forward `window_start` honored; direction-specific calibrators available (fallback hierarchy documented at line 222-230). **Sound structure.**
- **Train/test split honored:** `scripts/run_sweep.py:102` passes `calibration_cutoff_date="2024-12-31"`; ws5_common.py:425-437 only collects training samples for `today <= cutoff` and fits at line 470. Test period (2025-01-01+) is not used for fitting. **This part is REAL.**
- **Fatal input flaw:** the training pairs `(raw_conf, was_correct)` are generated from `signal.evaluate(metar_idx, ...)` at the target day's METAR index (§2), with correctness vs. the same day's settlement direction (§3). Because key signals read `days[idx]` (target-day observed data), the calibrator is trained on "predictions" that already contain the outcome. Isotonic calibration then faithfully maps leaky confidences to ~realized correctness — producing confident, useless probability estimates. **Calibration is well-built and correctly split; its inputs are the fiction.**

## 7. Direct answers

### Single biggest "this is fiction" point in data→calibration
**The evaluation loop predicts the target day's direction using the target day's observed METAR data.** `scripts/ws5_common.py:437-440` sets `metar_idx = metar_date_to_idx[today]` and calls `signal.evaluate(metar_idx, ...)`; signals like `ecmwf_bias_corrected` (`core/signals/ecmwf_bias_corrected_signal.py:313-345`) compute `direction = 'up' if days[idx].high > days[idx-1].high else 'down'` — and the ground truth (`compute_ground_truth_direction`, ws5_common.py:105-127) is `kalshi_temp[today] > kalshi_temp[yesterday]`, the same physical day's high measured by Kalshi. METAR daily high ≈ Kalshi daily-high settlement for the same station/day, so "accuracy" (including the ~79% ecmwf_bias_corrected figure) measures same-day METAR↔Kalshi correlation, not skill at predicting tomorrow. Every downstream layer (LLOP, UWC, isotonic calibration) inherits this. The 0.4839 UWC and 0.4845 naive numbers are the honest view: **nothing in the fused pipeline beats persistence, and a dumb climatology baseline (0.624) crushes it.**

### Most-real point (highest confidence)
**The raw observational layers.** METAR (12.8M obs, 8 real provider channels, 2021-01-01→2026-08-13) and IEM ASOS (5.38M obs, 2024-01-01→2026-08-04) are genuine NWS/IEM data with source columns and collection tracking; Kalshi settlements (9,487 rows) are real API captures with verified `finalized > historical_api` dedup (ws5_common.py:26-94). The data foundation is real; the signal/fusion/calibration *evaluation* is where reality ends.

### Improvement suggestion focused on accuracy (standing requirement)
**Re-anchor every signal and the ground truth to a strict "prediction made at T-1 close, evaluated against T settlement" protocol, and benchmark against the climatology baseline.** Concretely: (1) evaluate signals at `idx-1` with only data up to and including yesterday (drop any use of `days[idx]` — that's the leak; this alone should kill the 79% artifact); (2) score every fused output against `seasonal_same_month` (0.5549) and climatology (0.624) baselines already computed in `data/baseline_comparison.json` — any signal/fusion config that doesn't clear ~0.55 on held-out data is not tradeable, regardless of calibration quality; (3) then widen UWC coverage (279 trades is uninformative) by loosening the hardcoded gate/priors (`n_agreement_gate=4`, `rho_within=0.85/1.0`) until the gate itself is a fitted parameter. Reason: the entire accuracy chain is currently measuring how well METAR correlates with Kalshi on the same day; the only way to get real accuracy is to make the prediction happen *before* the outcome exists and to hold the fused system to the trivial baselines it currently loses to.

---

### Appendix — verification queries run (2026-08-13)
- `kalshi_settlements` GROUP BY source/source_type → 2,390 finalized / 7,097 historical_api; 21 stations incl. TEST; date span 2021-08-19→2026-08-10.
- `metar_backfill` tables/counts/ranges; sources list; backfill_metadata.
- `gefs_archive` / `tigge_archive` / `nwp_forecasts` / `iem_asos` counts + ranges + distinct sources/models.
- `ecmwf_archive.db`, `gefs_operational.db`, `gefs_reforecast.db` = 0 bytes (empty).
- `signal_fusion_comparison.json` (UWC 0.4839, 279 trades, per-station counts) and `baseline_comparison.json` (naive 0.4845 / climatology 0.624) — both generated 2026-08-13.
