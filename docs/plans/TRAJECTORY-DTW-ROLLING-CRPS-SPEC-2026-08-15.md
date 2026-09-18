# Rolling-Origin DTW Trajectory CRPS Evaluation Specification

**Date:** 2026-08-15 UTC  
**Status:** frozen research gate; no production, trading, integration, or live-data use

## 0. Scope and method boundary

This is a separate rolling-origin evaluation of the completed standalone `scripts/trajectory_dtw_replay.py`. It uses exact multivariate DTW only. A fixed top-K list of DTW-ranked historical analogs is permitted, but this lane must not substitute k-NN, nearest raw-vector search, Kalman filtering, HMMs, regression, or any fallback distance/model. Outputs are research artifacts only and require separate review before any use elsewhere.

The available source is the local SQLite `data/metar_backfill.db`, tables `metar_observations` and `settlement_epochs`. No network calls, production imports, schedulers, trading code, market prices, or output labels in feature construction are allowed.

## 1. Frozen local inventory and date windows

At execution, the runner records row counts, min/max dates, schema fingerprints, and SHA-256 hashes for the source database and this script/spec. The frozen local inventory is the six stations with complete daily settlement coverage through 2025-08-27 and long METAR history:

`KATL, KAUS, KBOS, KDCA, KDEN, KDFW`; market `HIGH` only; closed terminal settlement rows only. To keep the bounded run substantive and rerunnable on the available CPU while avoiding an uncontrolled sweep, the scored cohort is predeclared as `KATL` and `KDFW`; the other four stations remain frozen inventory evidence and are not silently substituted into the scored results.

Available settlement window: `2021-01-01` through `2025-08-27` for each selected station (1,700 rows/station at freeze). Available METAR windows are recorded per station and are not extended or imputed. Query origins are daily target dates; four intraday causal cutoffs are emitted for each target date: `06:00:00Z`, `12:00:00Z`, `18:00:00Z`, and `23:00:00Z`. The bounded run uses `2024-01-01` through `2025-03-31` as the outer evaluation horizon.

## 2. Frozen rolling geometry

The folds are expanding, chronological, and fixed before test scoring:

| Fold | TRAIN target dates | TUNE target dates | locked TEST target dates |
|---|---|---|---|
| R1 | 2021-01-01..2023-12-31 | 2024-01-01..2024-06-30 | 2024-07-01..2024-09-30 |
| R2 | 2021-01-01..2024-06-30 | 2024-07-01..2024-09-30 | 2024-10-01..2024-12-31 |
| R3 | 2021-01-01..2024-09-30 | 2024-10-01..2024-12-31 | 2025-01-01..2025-03-31 |

Every fold contains the two-station scored cohort and four cutoffs when causal query features exist; the six-station inventory and its available date ranges remain in the source manifest. A five-calendar-day target-date embargo is applied: candidates must end strictly before the query five-day sequence window begins. Thus candidate target date `< query_target_date - 4 days`, and no candidate raw observation identity may overlap any query sequence. Fold train/tune/test manifests are sorted immutable event identities and hashes. Tune labels are used only to select the predeclared grid choice. Test labels are attached only after the locked prediction is built and never affect candidates, scaling, feature availability, tuning, abstention, or selection.

The evaluation unit is `(station, market_type, target_date, cutoff, feature_arm)`. Each unit has one prediction or explicit abstention. Test folds are scored once under the selected configuration. After a fold is complete its labels may enter later training history only according to the frozen fold table; no prior result is revised.

## 3. Causal cutoff and settlement-timestamp limitation

An observation is eligible for a query only when `timestamp_utc <= cutoff` in UTC. Daily features use only the eligible prefix for the target day and eligible observations for the four completed prior days. No future interpolation, finalized daily value, revision, or settlement bucket is used to construct a query or candidate feature. Candidate sequences use their own end-of-day observation prefix and their settlement outcome is required to be in the past under the five-day embargo.

`settlement_epochs.settlement_timestamp_utc` and related close timestamps are backfill-generation/derived timestamps, not proven historical ingestion or publication times. This run therefore proves timestamp-causal use of the local METAR rows and date-level outcome separation only; it **does not claim ingestion-time causality**. The limitation is a hard reason not to advance to integration.

## 4. Feature arms (predeclared ablation)

All four arms are emitted independently and use the same causal daily aggregation and exact DTW machinery:

1. `temp_only`: `temp_max_f`.
2. `temp_humidity`: `temp_max_f`, Magnus-relative-humidity `rh_pct`.
3. `temp_humidity_pressure`: the preceding fields plus mean `pressure_mb`.
4. `current_level_plus_first_difference`: `temp_max_f` plus `temp_first_difference_f = current day temp_max_f - prior day temp_max_f`.

The fourth arm is intentionally a temperature level-plus-shape ablation, not a replacement predictor. The first difference is computed only from the two causal daily aggregates in the sequence. No settlement label, target bucket, outcome-derived status, or future observation is used. Required-feature absence rejects that arm/query; no per-test feature dropping or future fill is allowed.

For each fold and arm, location/scale is fit from TRAIN sequence values only (station-pooled within the declared six-station universe), then applied unchanged to tune/test candidates and queries. Mean/population standard deviation is used; zero scale is `1.0`. Equal feature weights sum to one unless the fixed search grid below selects the declared equal-weight DTW arm (no free learned weights).

## 5. DTW, candidate, K, recency, and search parameters

Exact banded DTW is inherited from the standalone implementation: weighted Euclidean local cost over scaled vectors, Sakoe-Chiba radius in `{0, 1}`, path-length normalization, and deterministic predecessor ties `diagonal, up, left`. Candidate ranking is `(distance, station|market_type|target_date)`.

The only tuning grid is frozen as:

- feature arm: all four arms;
- radius: `0` or `1`;
- K: `10` or `20` (uniform empirical analog mass);
- recency weighting: `none` or exponential half-life `365` days, measured from query target date and normalized over selected analogs;
- no distance cutoff, no station pooling alternative, no calibration/search fallback, no uncontrolled sweep.

Distances are computed from TRAIN-eligible candidates for each tune query; the selected `(arm, radius, K, recency)` minimizes mean tune CRPS, tie-broken by higher tune coverage, then lower radius, lower K, `none` recency, and frozen arm order. The selected choice is locked before the corresponding TEST fold. The test candidate search is restricted to data allowed by the fold geometry and never uses test labels.

Minimum analog policy is `K=10` for a non-abstained prediction; fewer than 10 complete eligible candidates yields `ABSTAIN_INSUFFICIENT_ANALOGS`. Because this is a research evaluation, candidate rows are retained in rejection counts by reason (`future`, `overlap/embargo`, `missing sequence`, `missing feature`, duplicate identity). No rule is widened after an abstention.

## 6. Fixed bucket universe and scoring

Settlement labels are exact integer `settlement_bucket` values. The probability universe is fixed before the run to integer buckets `-50..130`, including zero-mass buckets. The label is joined only after prediction construction. For a probability vector `p`, discrete CRPS/RPS is:

`sum_b (CDF_p(b) - 1[label <= b])^2` over the fixed bucket universe with unit bucket width.

CRPS is the primary tuning and reporting endpoint. Secondary diagnostics are exact-bucket multiclass log loss, multiclass Brier score, top-1 and top-3 accuracy, top-confidence ECE using fixed confidence bins `[0,.1),...,[.9,1]`, and 50/80/90% central interval coverage and width. Metrics are reported pooled and by station, feature arm, cutoff/lead, fold, and target-bucket strata where support exists. A fixed climatology baseline (TRAIN label histogram, smoothed only with deterministic zero handling) is reported on the same scored cohort; it is descriptive and not used to retune the DTW arm.

Abstentions are explicit. Coverage is non-abstained prediction units divided by eligible units; CRPS and secondary scores are reported on scored units with counts, and abstention reasons are never hidden. Sparse strata with fewer than 5 scored units are retained with `insufficient_support=true` and are not used for a gate claim.

## 7. Confidence intervals and block bootstrap

For every pooled/fold/arm/lead/station metric with support, the runner emits deterministic percentile 95% confidence intervals using 1,000 block-bootstrap replicates, seed `20260815`. A block is `(station, target_date)` with all cutoffs and arms kept together; blocks are sampled with replacement in sorted identity order. If fewer than 2 blocks exist, CI bounds are null and support is flagged sparse. No bootstrap result influences tuning or gate disposition.

## 8. Phase 1 QC requirements

The run fails closed on: source hash/inventory mismatch, duplicate observation or settlement identities, duplicate `(event, cutoff, arm)` output identity, future/overlap candidates, any raw observation identity overlap, test label use before prediction completion, non-causal timestamp use, non-finite DTW values, unstable ordering, missing fixed bucket universe, or any feature field derived from an output label. Every rejection and abstention reason is counted and manifests preserve offending identities where applicable. Fold manifests include exact train/tune/test IDs, causal cutoffs, candidate eligibility policy, and source/code hashes.

## 9. Gates

**ADVANCE** requires all QC/hash gates, byte-identical rerun, at least three locked test folds, coverage `>= 50%`, finite primary scores in every supported fold, no material secondary calibration/coverage regression versus the fixed climatology, and a predeclared CRPS improvement in at least two of three folds with the bootstrap interval not contradicting the direction. Even if met, this artifact remains research-only because ingestion-time availability is unproven; a separate review is mandatory.

**PARK** is the default when the implementation is deterministic and leak-free but the history/coverage is sparse, intervals overlap baseline, gains are unstable or arm-specific, or the settlement timestamp limitation prevents a causal claim. Preserve artifacts; do not widen rules or retune on tests.

**KILL** applies to confirmed leakage, duplicate/revision/hash failure, non-reproducibility, coverage below the floor, materially worse CRPS across the locked folds, or any method substitution. A KILL disposition applies to this experiment only and never licenses a relabeled replacement model.

## 10. Required isolated artifacts and receipt

The runner writes only under `data/trajectory_dtw_rolling_crps_20260815/`: canonical results, fold manifests, QC/rejection counts, source/code/spec hashes, per-stratum metrics, selected tune configurations, and a run manifest. It must be run twice to separate output paths and require byte-identical canonical result bytes and SHA-256. The receipt `.meta/continuity/weather-engine/2026-08-15-trajectory-dtw-rolling-crps.md` records exact commands, hashes, results, limitations, the accuracy suggestion, and PARK/ADVANCE/KILL disposition. No production or trading file is changed.
