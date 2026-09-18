# Temperature-Prediction Shadow Experiments — Frozen QC Specification

**Date:** 2026-08-15 UTC  
**Status:** research-only QC contract; not an execution, deployment, paper-trading, or B-mode authorization  
**Scope:** ECMWF IFS Direct, GEFS-only, GEFS+ECMWF combined, and HMM diagnostic

## 1. Purpose and non-goals

This document freezes one evaluator contract so the four temperature-prediction approaches can be compared on the same station/date/event population, information set, issue timing, labels, and metrics. It is the common gate for T1/T2/T3 in `TEMPERATURE-SHADOW-ROADMAP-2026-08-15.md`.

The experiment evaluates forecast quality only. It must not calculate P&L, select trades, emit orders, alter live/paper/shadow trading behavior, or modify the deterministic B-mode signal loop. A good forecast score is not evidence of market edge.

The four lanes are:

1. **ECMWF IFS Direct:** ECMWF/TIGGE member forecast, evaluated without GEFS input.
2. **GEFS-only:** GEFS member forecast, evaluated without ECMWF input.
3. **GEFS+ECMWF combined:** a pre-registered, equal-source combination of the first two lanes.
4. **HMM diagnostic:** the regime-switching HMM (`core/regime_switching_hmm.py`) evaluated as an out-of-sample diagnostic/diagnostic forecast adapter only.

> **HMM boundary (hard):** HMM is ML/diagnostic. It cannot enter the deterministic B-mode loop, modify B-mode confidence, change a gate, or influence sizing without a separate written approval and a separate integration/QC review. This document does not grant that approval.

## 2. Frozen target and identity

### 2.1 Canonical target

The target is the final NWS/Kalshi settlement station daily temperature for a target **local calendar date** `D` and event direction:

- `event_type = HIGH`: daily high temperature;
- `event_type = LOW`: daily low temperature.

The scored value is `y(station, D, event_type)` in degrees Fahrenheit, using the canonical finalized settlement/outcome source selected by the data-lineage audit. HIGH and LOW on the same station/date are different events and must never be collapsed.

Directional labels, when reported, compare the target value with the strictly prior settled value for the same station and event type:

```text
actual_delta = y(S,D,E) - y(S,prior_settled_date<D,E)
actual_direction = up    if actual_delta > +0.5°F
                   down  if actual_delta < -0.5°F
                   flat  otherwise (excluded from directional accuracy)
```

The `0.5°F` epsilon is frozen for this run. The continuous target remains scored for flat cases; only directional metrics exclude them.

### 2.2 Station universe and common keys

The pre-registered station universe is the 20 stations already used by the temperature shadow work:

```text
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW
KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

A row key is exactly:

```text
(station, target_date_local, event_type)
```

The **primary comparison population** is a complete-case intersection:

```text
K_common = K_master ∩ K_ECMWF ∩ K_GEFS ∩ K_combined ∩ K_HMM ∩ K_outcome
```

where `K_master` is the station universe crossed with the frozen date range and `{HIGH, LOW}`, and each method set contains only rows passing every eligibility rule below. All four methods must use the exact same sorted `K_common`; no method may add or remove rows after seeing outcomes or metrics.

A method-specific availability table may be reported as a secondary diagnostic, but it is not an accuracy comparison. It must never replace the common-key result. Every exclusion must have a machine-readable reason (`missing_forecast`, `missing_member`, `late_issue`, `ambiguous_revision`, `missing_prior_label`, `missing_outcome`, etc.).

## 3. Frozen date geometry and train/test split

The evaluator uses the following fixed target-date partitions. Dates are local station calendar dates and bounds are inclusive:

| Partition | Target dates | Use |
|---|---|---|
| `FIT_HISTORY` | `2021-08-19..2025-12-31` | HMM/model fitting and any pre-registered bias parameters |
| `CALIBRATION` | `2026-01-01..2026-02-28` | Optional confidence/interval calibration and threshold selection only |
| `LOCKED_SHADOW` | `2026-03-01..2026-08-13` | Final reported test/shadow score |

If a source cannot cover the full range, the evaluator retains the fixed boundaries and reports the resulting common-key coverage; it must not move the test boundary to improve coverage. A future rerun requires a new dated version of this contract.

All fitting is temporal and history-only. For a target date `D`, fitting may use only labels with target date `< D` and, for this frozen split, only the applicable partition preceding `D`. No `LOCKED_SHADOW` target or feature may enter model fitting, calibration, feature normalization, member weighting, threshold selection, HMM state selection, or combined-model weights.

Direct ECMWF and GEFS forecasts do not require statistical fitting, but any bias correction, post-processing, climatology, probability calibration, or model weighting is still fit only in `FIT_HISTORY` (and optionally frozen in `CALIBRATION`) and then applied unchanged to `LOCKED_SHADOW`.

## 4. Common forecast issue timing and information cutoff

### 4.1 Target and issue timestamps

For each `(S,D,E)`, the adapter must materialize:

- `target_start_ts_utc` and `target_end_ts_utc`, derived from the station timezone and local date `D`;
- `forecast_issue_ts_utc`: the model initialization/issue time;
- `forecast_available_ts_utc`: when the exact forecast revision became available to the evaluator;
- `valid_time_end_ts_utc`: the forecast valid period endpoint;
- `asof_cutoff_ts_utc`.

The common as-of cutoff is frozen to **12:00 UTC on the calendar day before the station's target local date**:

```text
asof_cutoff(S,D) = 12:00:00 UTC on (D - 1 calendar day)
```

A forecast row is eligible only if:

```text
forecast_available_ts_utc <= asof_cutoff(S,D)
forecast_issue_ts_utc     <= asof_cutoff(S,D)
valid_time covers exactly the target local date D
```

For each source/station/date/event, select the latest eligible issue according to this deterministic order:

1. greatest `forecast_issue_ts_utc`;
2. greatest `forecast_available_ts_utc`;
3. stable source revision ID / content hash ascending as the final tie-break.

This gives each method the latest forecast that was genuinely available by the same wall-clock cutoff. It does not permit one model to use a later cycle merely because that model publishes more often.

### 4.2 Provenance requirement

`fetch_timestamp` alone is not sufficient to establish model issue timing: it can be an ingestion time after the forecast was issued. GEFS rows may use the archive's `init_cycle` only when it is parsed into an unambiguous UTC timestamp and paired with a retrieval/availability timestamp. ECMWF/TIGGE rows must have equivalent issue and availability provenance in the archive or a versioned sidecar manifest.

Rows lacking auditable issue/availability timestamps are **ineligible**, not assigned a guessed cycle. In particular, do not infer ECMWF issue timing from `target_date`, database row order, filesystem mtime, or the time the QC script runs. The inventory must fail closed if the current TIGGE schema cannot prove this requirement.

### 4.3 Feature cutoff and leakage barrier

All non-NWP features, including METAR, climatology, station history, prior settlement, and HMM inputs, are restricted to information available strictly before the target local day and no later than the common cutoff:

```text
feature_cutoff_ts_utc = min(target_start_ts_utc, asof_cutoff_ts_utc)
```

Only records with `observation_ts_utc < feature_cutoff_ts_utc` may be read. The target-day METAR row, target-day `daily_stats`, target-day `six_hour_aggregates`, target-day settlement, and any derived target-day statistic are forbidden inputs, even if already present in a database.

The target outcome is read only after prediction generation, solely to attach the score. It must never be passed to a forecast, calibrator, fusion function, threshold, HMM, or selection routine.

## 5. Method contracts

### 5.1 ECMWF IFS Direct

- Source: ECMWF IFS/TIGGE archive, with an auditable issue/availability manifest.
- Input: ECMWF members for the exact station, event, target date, valid period, and selected as-of issue.
- Output: point forecast, member distribution (if available), interval/probability fields, member count, issue metadata, and source hash.
- It must not read GEFS values, GEFS-derived bias tables, target-day observations, or target-day outcomes.

### 5.2 GEFS-only

- Source: GEFS archive, using the exact target event/valid period and selected as-of issue.
- Input: GEFS members only; `init_cycle` is accepted only after UTC parsing and availability verification.
- Output: the same canonical schema as ECMWF, including point, distribution, interval/probability fields, member count, issue metadata, and source hash.
- It must not read ECMWF values or target-day observations/outcomes.

### 5.3 GEFS+ECMWF combined

The combined method is frozen as a **two-source, equal-source-weight ensemble**, not a raw concatenation that lets the 50-member source dominate the 31-member source:

1. Validate and summarize GEFS and ECMWF independently under the same cutoff.
2. Normalize each source distribution to equal total source weight (`0.5` GEFS, `0.5` ECMWF); members within a source receive equal conditional weight.
3. Combine the two weighted distributions. Preserve source-specific summaries and member counts in the artifact.
4. Do not duplicate, impute, or silently de-correlate members. Report the source correlation on common rows as a diagnostic.
5. Any alternative learned weighting is a new method and requires a new pre-registration; it cannot be tuned on `LOCKED_SHADOW`.

The combined lane is eligible only when both component forecasts independently pass all provenance, timing, validity, and member gates. There is no GEFS-only or ECMWF-only fallback inside the combined lane.

### 5.4 HMM diagnostic

The HMM may use only pre-cutoff METAR/meteorological history, with training and normalization restricted to `FIT_HISTORY` (and any explicitly frozen calibration restricted to `CALIBRATION`). It must be run station-by-station or with a pre-registered station-normalization procedure; random station sampling is prohibited for the scored artifact.

The HMM artifact must include:

- feature names and formulas;
- training date range and row counts;
- random seed and deterministic solver settings;
- state count and covariance/model settings;
- state-label mapping or a state-permutation-invariant diagnostic;
- regime probabilities at the forecast cutoff;
- the exact registered adapter used to turn regimes into a temperature point/distribution, if one exists.

If no pre-registered temperature adapter exists, HMM is reported as a regime/error-stratification diagnostic only and receives `NOT_COMPARABLE` for point-forecast accuracy; it may not be retrofitted after seeing test outcomes. Even if an adapter exists and the HMM scores well, it remains diagnostic/ML and is barred from the deterministic B-mode loop absent separate approval.

## 6. Missingness and member policy

Missingness is data, not a signal. No missing member, forecast, observation, or label may be filled by zero, ensemble mean, climatology, persistence, another source, or a target-day value unless that imputation is separately pre-registered and applied identically before the split. The default contract has **no imputation**.

For each ensemble source:

- `n_expected` comes from the source/issue-cycle metadata, not a hard-coded assumption when metadata is available.
- A member is present only if its value is finite, physically valid, and has the expected member identity.
- A row passes the member gate only when `n_present >= ceil(0.80 * n_expected)` and no duplicate member identity exists.
- The summary uses present members only and records `n_expected`, `n_present`, `n_missing`, and member IDs. Rows below the threshold are ineligible for that method.
- If the source cannot establish `n_expected`, the row is ineligible for the primary ensemble comparison; a scalar-only secondary report may be labeled separately.

The combined method requires both component member gates. It never pads one source to match the other's member count. HMM missing-feature rows are ineligible when a required feature is absent; no target-day backfill is allowed.

Coverage, missingness, and member-count distributions must be reported overall and by station, event type, season, and issue lead/cycle. Missingness must not be conditioned on correctness or used to choose a favorable subset.

## 7. Forecast revisions and outcome revisions

### 7.1 Forecast revisions

A forecast revision is identified by source, station, target date, event, issue timestamp, availability timestamp, revision ID, and content hash. The evaluator uses the latest revision available **at or before** the common cutoff, never the latest row currently in the database. A revision first ingested after the cutoff is not eligible for that historical as-of run.

If two eligible revisions have conflicting values and no deterministic revision ordering exists, the key is excluded with `ambiguous_revision`. Do not select an arbitrary first/last SQL row. The selected revision metadata and discarded revision IDs must be included in the row lineage.

### 7.2 Target/outcome revisions

The scored target is the final canonical settlement/outcome value available in the frozen outcome snapshot. Outcome corrections may change the score but may not change the forecast, fit state, common-key membership, or method selection. A corrected outcome requires a new versioned artifact and a manifest field identifying the outcome snapshot; do not overwrite the prior report silently.

A target-day settlement or corrected target-day observation is always label-only. It cannot enter calibration or HMM training for that target date.

## 8. Canonical prediction and artifact schema

Every eligible row must be serialized with at least:

```text
method, station, target_date_local, event_type,
target_start_ts_utc, target_end_ts_utc,
asof_cutoff_ts_utc, feature_cutoff_ts_utc,
forecast_issue_ts_utc, forecast_available_ts_utc, valid_time_end_ts_utc,
prediction_f, ensemble_p05_f, ensemble_p10_f, ensemble_p25_f,
ensemble_p50_f, ensemble_p75_f, ensemble_p90_f, ensemble_p95_f,
n_expected, n_present, n_missing, member_ids_or_hash,
actual_f, prior_actual_f, actual_direction,
source_revision_id, source_content_hash, exclusion_reason,
code_commit, artifact_version
```

`actual_f`, `prior_actual_f`, and `actual_direction` are populated only in the score attachment after prediction generation; they are absent from the prediction-side input passed to any method. For HMM diagnostic rows, the schema additionally records regime probabilities and adapter status.

Rows are sorted by `(station, target_date_local, event_type, method)`. The evaluator writes:

1. a canonical row-level prediction/score file;
2. a `K_common` key file;
3. a missingness/exclusion ledger;
4. a metric JSON/table with all denominators;
5. a manifest containing input paths, database/table queries, as-of cutoff, date partitions, timezone map, package/runtime versions, random seeds, configuration, source revision hashes, code commit, row counts, and SHA-256 hashes of every output.

Two runs with byte-identical inputs, code commit, configuration, and environment must produce byte-identical canonical rows, ledgers, metric outputs, and manifest hashes. Any nondeterministic solver must be disabled or seeded and its ordering fixed.

## 9. Metrics and required reporting

All primary metrics are computed on the exact `K_common` and reported separately for HIGH, LOW, pooled events, each station, and each season (DJF/MAM/JJA/SON). Also report issue-cycle/lead strata where provenance supports them.

### 9.1 Point accuracy

- MAE in °F (primary point metric);
- RMSE in °F;
- mean signed bias and median absolute error;
- paired per-key error difference against GEFS-only and ECMWF Direct;
- bootstrap 95% confidence intervals, resampling station/date blocks rather than treating every event as independent.

### 9.2 Direction and probabilistic quality

- directional accuracy using the frozen `±0.5°F` rule, with flat denominator shown;
- Brier score and log loss when a directional probability is emitted;
- CRPS when a full predictive distribution is emitted;
- 50/80/90% interval coverage, interval width/sharpness, and coverage by stratum;
- calibration/reliability curve, calibration slope/intercept, and ECE for probability-bearing outputs.

### 9.3 Operational integrity

- number and percentage of `K_master`, `K_outcome`, each method set, and `K_common`;
- excluded-key counts by reason;
- member availability (`n_expected`, `n_present`, `n_missing`);
- issue/availability lateness and revision counts;
- zero-leakage audit result and target-day access audit;
- deterministic rerun/hash result.

P&L, market prices, Kelly fractions, Sharpe, fills, and trade counts are out of scope and must not appear as acceptance evidence for this temperature-accuracy contract.

## 10. Acceptance gates

A run is **QC PASS** only when all applicable gates below pass:

1. **Identity gate:** `K_common` is generated before looking at metric results; all four methods score the same immutable, sorted keys; no duplicate `(station,date,event)` exists.
2. **Timing gate:** every selected forecast has auditable issue and availability timestamps and satisfies the common `12:00 UTC on D-1` cutoff; no post-cutoff revision is selected.
3. **Leakage gate:** target-day observations, target-day settlements, holdout outcomes, and future revisions cannot affect prediction-side state. Mutation tests must show that changing target-day observations/outcomes changes only the attached score, never the prediction, calibration, HMM state, or key set.
4. **Split gate:** no `LOCKED_SHADOW` label/feature is used for fitting, calibration, normalization, HMM state selection, or combination weights; calibration labels are strictly prior to each scored target.
5. **Missingness gate:** no silent imputation; member threshold and exclusion ledger pass; combined rows have two independently eligible sources.
6. **Determinism gate:** repeated identical runs produce identical row, metric, and manifest hashes; all seeds, ordering, environment, and source hashes are recorded.
7. **Coverage gate:** report coverage against `K_master` and `K_common`. For advancement beyond descriptive research, require at least 90% `K_common` coverage of the pre-registered station/date/event master within the frozen date range and no unexplained method-selective exclusions. A lower-coverage result is `DESCRIPTIVE_ONLY`, not a failed accuracy claim.
8. **Accuracy gate for a non-HMM candidate:** on paired `K_common` rows, the candidate must improve GEFS-only MAE by at least `0.10°F` with the 95% CI for the paired difference below zero, or improve directional accuracy by at least 2 percentage points with a lower confidence bound above zero. It must not be more than `0.25°F` worse in MAE in any station or HIGH/LOW stratum with at least 30 scored rows, and interval coverage may not fall more than 5 percentage points below its nominal level.
9. **Combined-model gate:** GEFS+ECMWF must meet the non-HMM accuracy gate against GEFS-only and be non-inferior to the better standalone parent under the same paired test. A gain caused only by a different key population is invalid.
10. **HMM gate:** HMM may pass only as `DIAGNOSTIC_PASS` when its out-of-sample regime/error diagnostic is reproducible and useful under the same split. It cannot receive a B-mode or production enablement status from this run; any future integration requires separate approval.

Results must be labeled `ADVANCE_TO_SEPARATE_RESEARCH`, `DESCRIPTIVE_ONLY`, `PARK`, or `KILL` per method. `ADVANCE_TO_SEPARATE_RESEARCH` means only that a separate market-impact study may be designed; it does not authorize trading.

## 11. Stop conditions and review handoff

Stop the run and mark the affected method `BLOCKED` if any of the following occurs: issue timing cannot be proven; current TIGGE rows lack availability lineage; a target-day read reaches a method; a holdout label enters fitting; duplicate revisions disagree; member identities cannot be reconstructed; common keys differ after scoring; deterministic rerun hashes differ; or satisfying a gate would require changing the frozen population.

The QC handoff must include the exact command/configuration, commit, input and output hashes, key counts, exclusion ledger, mutation-test results, metric tables with denominators and confidence intervals, limitations, and the final disposition for each lane. No trading or production artifacts are required or permitted.

## 12. One accuracy-focused suggestion

**Suggestion: make paired, key-matched MAE improvement with block-bootstrap confidence intervals the primary advancement statistic, and use direction accuracy only as a secondary metric.** Continuous temperature error is the actual prediction problem; paired scoring controls for station/date weather difficulty and prevents a model from appearing better because it scored an easier subset. This is especially important for the combined and HMM lanes, whose apparent gains can otherwise be driven by missing-key selection, regime mix, or a different directional firing threshold rather than more accurate temperatures.
