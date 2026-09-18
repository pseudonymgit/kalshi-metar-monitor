# Trajectory DTW Rolling-CRPS Expanded-Cohort Scope / QC Addendum

**Date:** 2026-08-15 UTC  
**Status:** frozen before expanded test scoring; research-only; no production, trading, integration, or live-data use  
**Parent protocol:** `TRAJECTORY-DTW-ROLLING-CRPS-SPEC-2026-08-15`  
**Parent runner:** completed standalone `scripts/trajectory_dtw_rolling_crps.py`

## 1. Purpose and non-goals

This addendum expands the original bounded rolling-origin DTW evaluation beyond the scored `KATL/KDFW` cohort. It does not revise the matcher, feature arms, scoring, folds, candidate embargo, tuning grid, or gate rules. It is an isolated accuracy/QC run only.

Non-goals are unchanged: no k-NN, Kalman, HMM, regression, Euclidean fallback, network calls, production imports, market prices, trading simulation, scheduler, paper/live trading, or integration. A favorable result is not an authorization to use the lane elsewhere.

## 2. Predeclared expanded cohort

The expanded scored station cohort is the complete six-station inventory already frozen in the parent specification:

`KATL, KAUS, KBOS, KDCA, KDEN, KDFW`

The market is `HIGH` only. Selection was made before expanded test scoring from the parent inventory because each station has the same locally available closed terminal settlement range (`2021-01-01..2025-08-27`, 1,700 rows at the parent freeze) and long METAR history beginning `2021-01-01`. This is a cohort expansion, not an opportunistic station-by-station selection after observing expanded test outcomes.

Stations outside this six-station inventory are excluded from the scored cohort even if newer local rows exist. In particular, stations with later or incomplete settlement windows are not added in this run. The six stations remain station-pooled for training-only normalization exactly as declared by the parent protocol; analog candidates remain same-station, not cross-station.

The outer evaluation horizon and target-date cohort are unchanged from the completed evaluator's locked geometry:

- development/train history begins `2021-01-01`;
- tune/test dates use the three locked expanding folds below;
- only Monday target dates are emitted;
- four UTC causal cutoffs are emitted for every eligible station/date: `06:00:00Z`, `12:00:00Z`, `18:00:00Z`, `23:00:00Z`;
- the four cutoffs are the predeclared intraday lead/cutoff strata. No retrospective best-cutoff selection is allowed.

| Fold | TRAIN target dates | TUNE target dates | locked TEST target dates |
|---|---|---|---|
| R1 | 2021-01-01..2023-12-31 | 2024-01-01..2024-01-31 | 2024-02-01..2024-02-29 |
| R2 | 2021-01-01..2024-01-31 | 2024-02-01..2024-02-29 | 2024-03-01..2024-03-31 |
| R3 | 2021-01-01..2024-02-29 | 2024-03-01..2024-03-31 | 2024-04-01..2024-04-30 |

The expanded run therefore predeclares six stations × four cutoffs × Monday dates × three locked test folds × four arms. Test labels are attached only after each locked prediction is built.

## 3. Frozen method and QC inheritance

All parent settings remain frozen:

- exact banded multivariate DTW, path-length normalization, and deterministic predecessor order `diagonal, up, left`;
- sequence length five daily epochs and five-calendar-day target-date embargo;
- arms `temp_only`, `temp_humidity`, `temp_humidity_pressure`, and `current_level_plus_first_difference`;
- fold-local train-only normalization, then permitted train+tune refit before locked test scoring;
- grid radius `{0,1}`, K `{10,20}`, recency `{none, half_life_365d}`, minimum analog count `10`;
- fixed integer bucket universe `-50..130` and discrete bucket RPS/CRPS primary score;
- secondary log loss, Brier, ECE, top-1/top-3, central 50/80/90% coverage and interval width;
- deterministic sorted `(station,target_date)` block bootstrap, 1,000 replicates, seed `20260815`;
- explicit rejection/abstention accounting, duplicate identity checks, fixed output identity checks, finite-distance checks, and byte-identical rerun requirement.

The expanded runner must fail closed on source hash/inventory mismatch, duplicate source identities, future or overlapping candidates, non-finite DTW values, missing fixed buckets, unstable order, or label use before prediction completion. Per-station and per-cutoff (lead) metrics are required; sparse strata remain flagged rather than omitted.

## 4. Causal limitation retained

The local METAR rows are filtered by `timestamp_utc <= as_of_epoch`, and query target-day aggregates use only the causal prefix at each cutoff. Candidate sequences use completed historical daily prefixes and remain behind the embargo. This proves timestamp-causal use of the local rows and date-level outcome separation only.

`settlement_timestamp_utc` and related close fields are backfill/derived timestamps, not proven historical ingestion/publication availability. The expanded run therefore **does not claim ingestion-time causality**. This remains a hard reason not to advance to integration, regardless of accuracy.

## 5. Required isolated artifacts

Use a fresh output root, without overwriting the bounded run:

`data/trajectory_dtw_rolling_crps_20260815_expanded/`

Two independent replicate directories must contain the canonical result and source manifest. The result must include source/code/addendum hashes, inventory and row ranges, fold manifests, selected configurations/full grid, all locked test predictions, rejection/abstention counts, pooled/fold/station/lead metrics, and QC flags. A separate canonical `per_station_lead_metrics.json` is required for explicit station × cutoff reporting. A run manifest records all artifact paths and SHA-256 hashes.

The run must be executed twice from the same immutable database snapshot. Canonical result bytes, source manifests, station-lead metrics, and run manifests must compare byte-for-byte between replicates. No network or production/trading file may be touched.

## 6. Predeclared disposition gates

Use the parent gates without alteration:

- **ADVANCE:** all hard QC/hash gates pass; byte-identical rerun; all three locked test folds; coverage at least 50%; finite primary score in each supported fold; no material calibration/coverage regression versus fixed climatology; and predeclared CRPS improvement in at least two of three folds with bootstrap direction not contradicted. Even then, ingestion-time causality remains unproven and separate review is mandatory.
- **PARK:** deterministic and leak-free but evidence is inconclusive, history/coverage is sparse, station/lead results are unstable, intervals overlap baseline, or the causal timestamp limitation prevents a causal claim. Preserve artifacts; do not retune or widen rules.
- **KILL:** confirmed leakage, duplicate/revision/hash failure, non-reproducibility, coverage below floor, materially worse locked-fold CRPS, or method substitution. KILL applies to this experiment only.

The disposition is recorded after the two locked deterministic replicates and QC inspection. No paper/live trading or integration step follows this run.
