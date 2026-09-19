# Data-Integrity Sweep — Lookahead Gates + Down-Skew Root Cause (2026-09-19)

**Owner:** Gilfoyle (subagent)
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Scope:** Deterministic scripts only — no AI in the loop.

Three ground-truth/labeling leaks of the SAME class were already found this session
(strike axis, strike backfill clobber, NWP lookahead).  This sweep closes the
remaining members of that family: **NWP lookahead (upper-air + ensemble)**, **ERA5
reanalysis lookahead**, **GHCN climatology/report-date lookahead**, and isolates the
**down-skew root cause**.

---

## Task 1 — NWP lookahead gate (P0-F)  ✅ CLOSED

**Finding.** `data/nwp_forecasts.db` had **317,669 rows (34.1%)** with
`fetch_date > target_date` (lookahead).  Breakdown:

| Model       | Total  | Lookahead | Contemporaneous |
|-------------|--------|-----------|-----------------|
| gefs_ens    | 247,209 | 245,029 (99.1%) | 2,180 |
| ecmwf       | 219,980 |  71,440 (32.5%) | 148,540 |
| nws_mos     |   1,340 |   1,200 (89.6%) | 140 |
| gfs / icon / gem / era5 | — | 0 | all |

The lookahead lives in **two field families**:
1. `gefs_ens` ensemble members (`temperature_2m_max_memberge*`) — 245,029 rows
   (reforecast data backfilled with `fetch_date = 2025+` for `target_date 2021-2024`).
2. Upper-air fields `temperature_850hPa_daily_mean` + `geopotential_height_500hPa_daily_mean`
   (ecmwf) — 35,720 rows each, offset up to **+2033 days**.

**Impact on `nwp_analog` (the worst-affected signal):**

| Field (upper-air) | Contaminated keys | Contemporaneous keys | Keys that exist ONLY via lookahead |
|---|---|---|---|
| temperature_850hPa_daily_mean | 37,260 | 2,800 | **34,460** |
| geopotential_height_500hPa_daily_mean | 37,260 | 2,800 | **34,460** |

The analog "historical library" was ~92.5% reanalysis/backfill — effectively predicting
the past with data unavailable at prediction time.  Contemporaneous upper-air data only
spans **2026-05-01 → 2026-09-24** (4.5 months).

**Re-score of `nwp_dtdt_fusion` (temperature_2m_max, model vote):**
`nwp_dtdt_fusion` uses only `temperature_2m_max/min` from gfs/ecmwf/icon/gem, which carry
**zero** lookahead in those fields (the 1,200 `temperature_2m_max` lookahead rows are all
`nws_mos`, not in the signal's model list).  So its directional output is **unchanged**
(up_rate 0.5194 both contaminated and contemporaneous).  The contamination hit
`nwp_analog`/`nwp_analog_ensemble` (upper-air) and any gefs_ens ensemble consumer, not
`nwp_dtdt_fusion`.

**Fix.** New deterministic seam `core/nwp_lookahead_gate.py` exposing
`CONTEMPORANEOUS_PREDICATE = " AND fetch_date <= target_date"`.  Applied at read time
(no DB mutation, no storage bloat) to:
- `core/signals/nwp_direct_signal.py::_get_temp`
- `core/signals/nwp_analog_signal.py::_load_nwp_features` (feature pool + `dates` list)
- `core/signals/ai_composite_signal.py::_get_classical_temp`
- `core/signals/cross_model_divergence_signal.py::_fetch_forecast`
- `scripts/nwp_analog_ensemble.py::load_nwp_features`

---

## Task 2 — ERA5 lookahead gate (P0-G)  ⚠️ CONFIRMED LEAK (structural, no fetch_date column)

**Finding.** `data/era5_archive.db` is reanalysis (retrospective).  It has **no
`fetch_date`/ingest column** — only `target_date` + temp values.  `era5_checkpoint`
shows all 41,380 rows (2021-01-01 → 2026-08-31) were ingested in **one batch on
2026-08-31** (all 20 stations, `updated_at` 2026-08-31T11:01–11:02Z).

**Where it feeds.** `scripts/goldilocks_feature_engineering.py::_get_era5_day(local_date)`
returns `daily_max_t2m - daily_min_t2m` as `daily_temp_range_C` (a stability feature) for
the **same `local_date` being scored**.  That is reanalysis-of-today used to predict
today — **inherently lookahead** relative to any prediction made before ERA5's release
(nominal ~5-day CDS latency; contemporaneous cutoff ≈ 2026-08-26, so 5 target_dates
beyond cutoff are lookahead).

**Scope boundary.** This leak is confined to the **offline goldilocks training feature
builder**.  The live predictor `core/goldilocks_predictive.py::_compute_features_from_obs`
computes `daily_temp_range_C` from **live METAR observations**, NOT ERA5 — so the live
signal is clean.  Only training data carries the ERA5 lookahead.

**Verdict.** ERA5 **leaks** when used as a same-day feature in the goldilocks training
pipeline.  It cannot be gated in-place because the DB lacks a fetch/availability column.
Recommended (not done here — requires a schema add + backfill, out of "no storage bloat"
scope): add an `available_date` column or drop ERA5-derived same-day features from the
training set (or recompute from METAR, as the live path already does).

---

## Task 3 — GHCN report-date vs obs-date (P0-H sibling)  ✅ CLOSED (climatology gate)

**Finding.** `data/ghcn_settlements.db` (619,428 rows, 1869-01-01 → 2026-09-12) has **no
`report_date`/`backfill_date` column** — only `station, date, high_f, low_f, source`.
GHCN-daily (NOAA) has a nominal **6-day reporting lag** (already documented in
`DIRECTION-AXIS-CORRECTION`).

Two distinct GHCN usages:
1. **Ground-truth labels** (`core/additive/historical_adapter.py`, precedence
   Kalshi > settlement_epochs > GHCN).  This is a *label*, not a *feature*, and carries
   the 6-day lag for recent rows — a **recency gap**, not a per-row lookahead (no report
   date exists to gate on).
2. **Climatology feature** (`core/signals/ghcn_calibration_signal.py::_get_ghcn_climatology`)
   queries GHCN by **day-of-year across ALL years** (1869→2026) with **no year/date
   ceiling** → future-year observations inform past predictions (**lookahead**).

**Contemporaneous flag count.** For KNYC @ 2021-07-15 (±15-day DOY window), the
un-gated climatology returns 4,898 rows; the gated (`date <= target_date`) returns 4,728
→ **170 lookahead rows excluded** per station/date.  Across 5 stations × 5 target dates,
**2,667 lookahead rows** would be excluded.  Aggregate future-year pool: 5,354 rows have
`date > 2025` (lookahead vs any 2025 target).

**Fix.** Added `AND date <= ?` (target_date) to both the primary and fallback climatology
queries in `ghcn_calibration_signal.py`.

---

## Task 4 — Down-skew root cause (P0-H)  ✅ VERDICT: MARKET STRUCTURE + label artifact

**Finding.** Base rate "up" = 34.4% (9275 down / 4855 up); "always down" Sharpe 5.25.

Strike-vs-realized distribution (`settlement_epochs`, HIGH markets, `strike_type='greater'`,
sanitized to 0–150°F):

| Metric | Value |
|---|---|
| n | 9,733 |
| **median (realized − strike)** | **−4.0 °F** |
| mean (realized − strike) | −4.41 °F |
| up (realized > strike) | 784 |
| down (realized < strike) | 8,486 |
| tie | 463 |
| **clean up-rate (excl. tie)** | **8.98%** |

**Verdict — two causes, primary is market structure:**
1. **MARKET STRUCTURE (primary).** Kalshi HIGH strikes are systematically set **~4 °F
   above the realized high** (median offset −4.0 °F).  This is the house "juice" — the
   strike is quoted above the expected settlement so "greater" (up) rarely wins.  A
   naive "always down" strategy earns Sharpe 5.25 purely from this structural skew, NOT
   from any predictive edge.  This is **correct behavior** — it must be modeled as a
   base-rate prior, not "fixed".
2. **LABEL ARTIFACT (secondary, contaminating).** 426 rows have corrupt `settlement_bucket`
   values (64886, 64893, 63088, and negatives like −6).  These are serialization
   artefacts (the same `64886.0` class already noted in `nwp_analog_signal.py`).  They
   inflate the "up" count in any unsanitized analysis (a realized temp of 64,893 beats any
   strike).  The **direction-axis correction already in flight** (P0-B/C) now skips
   untrustworthy dates and reads real strike prices, which eliminates this artifact from
   the label.

The "34.4% up" figure comes from the **old day-over-day label** (`settlement_bucket >
prior_settlement_bucket`), which is the running-max bug already identified in the
direction-axis correction.  On the **correct strike-crossing axis**, the true base rate is
**~9% up** — even more skewed, and entirely explainable by the ~4°F strike premium.

---

## Task 5 — DST/timezone  ✅ NO ANOMALIES

`scripts/audit_dst_transitions.py` run against the current label set:
- No duplicate settlement dates across all 20 stations.
- No spring-forward gaps (2025-03-08–14, 2026-03-08–14).
- Fall-back (2025-11-02) present for all 20 stations, single high temp each.
- No future-dated METAR observations (max 2026-09-19 == now).
- **All checks passed — no DST/timezone boundary anomalies.**

---

## Consolidated verdict — leaks remaining

| Leak | Status | Class |
|---|---|---|
| Strike axis (day-over-day vs strike-crossing) | In flight (P0-B/C, not touched) | label |
| Strike backfill clobber (`migrate_add_strike_price.py`) | FIXED (prior) | label |
| NWP lookahead — upper-air + gefs_ens ensemble | **CLOSED** (this sweep) | feature |
| ERA5 reanalysis lookahead (goldilocks training) | **CONFIRMED, UNFIXED** — no fetch_date col to gate on; schema+backfill or drop-from-training required | feature |
| GHCN climatology future-year lookahead | **CLOSED** (this sweep) | feature |
| GHCN report-date recency gap (6-day lag) | Documented — label-side, no per-row flag possible | label |
| Down-skew | **ROOT CAUSED** — market structure (strike ~4°F premium), not a bug | — |

**Remaining actionable leak:** ERA5 (P0-G) — requires a schema change (add
`available_date`) + backfill, or removal of the same-day ERA5 feature from the goldilocks
training set.  Everything else in this family is either closed, already-fixed, or a
correct market-structure property.
