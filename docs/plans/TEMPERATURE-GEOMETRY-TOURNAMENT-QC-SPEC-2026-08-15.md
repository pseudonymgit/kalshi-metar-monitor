# Temperature Model / Geometry Tournament — Independent QC Specification

**Date:** 2026-08-15 UTC  
**Version:** `temperature-geometry-tournament-qc-v1.0-2026-08-15`  
**Status:** research design only; no experiment, backtest, production, SignalRegistry, shadow-trading, or B-mode change  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`

## 1. Purpose and hard boundaries

This specification freezes a fair tournament for temperature forecasts and target geometry for NWS/Kalshi daily station **HIGH** and **LOW** outcomes. It tests whether the value representation (point, path extremum, member extremum, bucket distribution, calibrated distribution, or trajectory analogue) improves forecast accuracy. It is not a market-edge, P&L, trading, or deployment test.

The tournament may write only research artifacts under a dated output directory. It must not alter source databases, runtime modules, signal weights, gates, alert behavior, paper trading, or production configuration. No candidate may enter `SignalRegistry`, a shadow trader, a sweep, or B-mode from this document. A successful forecast gate authorizes only a separately reviewed research handoff.

The evaluator is **fail closed**. It must stop rather than infer a target day, issue vintage, availability time, member identity, local-day window, or missing value. Incomplete geometry is not a lower-quality score and is not repaired by interpolation, persistence, climatology, another source, or a different cycle.

## 2. Frozen identity and evaluation universe

### 2.1 Canonical row key

Every event is identified by exactly:

```text
(station, target_date_local, event_type)
```

where `event_type ∈ {HIGH, LOW}`. HIGH and LOW on the same station/date are different events and must never be pooled into one target before scoring. The target is the final canonical settlement/NWS value in degrees Fahrenheit for the station's local calendar date.

The frozen station registry is:

```text
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW
KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

The station registry must provide, for each station, the exact settlement/NWS identity, latitude/longitude, IANA timezone, DST rules, valid-date range, and source mapping. Unknown stations, aliases, and development/test stations are excluded with a recorded reason.

### 2.2 Target local day and event windows

For station `S` and local date `D`, derive:

```text
local_start(S,D) = 00:00:00 in S's IANA timezone on D
local_end(S,D)   = 00:00:00 in S's IANA timezone on D+1
valid_window     = [local_start, local_end) converted to UTC
```

Conversion must use the timezone database, including DST transitions; never add a fixed 24-hour offset in UTC. The HIGH geometry is the maximum over the declared local-day window. The LOW geometry is the minimum over that same window. If the authoritative settlement contract uses a different climate/reporting window, the source must supply that contract and the candidate registry must be versioned before scoring; the evaluator may not silently substitute midnight-to-midnight.

A forecast valid-time panel is eligible for a local-day geometry only when the source manifest proves the expected cadence and complete coverage of the whole declared window. At minimum, it must contain every expected valid timestamp in `[local_start, local_end)` exactly once, with no gap and no out-of-window value used. A panel without a proven cadence, endpoint convention, or complete expected timestamp set is `incomplete_geometry` and is ineligible. No interpolation, extrapolation, target-day observation, or neighboring-cycle repair is permitted.

`FIX12` candidates require one exact forecast valid timestamp at local 12:00 (or a source-declared equivalent whose timestamp is identical after UTC conversion). A nearby timestamp is not substituted. `PATH_EXT` and member-extrema candidates use all and only the complete expected timestamps in the local-day window.

### 2.3 Common universe, never candidate-specific denominators

Before any metric is inspected, build and freeze these sets:

- `U_master`: the Cartesian product of the frozen station list, target-local dates `2021-08-19..2026-08-13` inclusive, and `{HIGH, LOW}`.
- `U_outcome`: `U_master` keys with one unambiguous final outcome in the immutable outcome snapshot.
- `U_eligible`: keys passing the common station/date/event identity, outcome, cutoff, and source-lineage gates required by this tournament. It is built from provenance and eligibility fields only, never from accuracy.
- `K_common`: the sorted intersection of the pre-score eligible key sets for every registered arm declared **primary** in this version, including its source-vintage, member-identity, and complete local-day geometry gates. This intersection is frozen before predictions are scored or winners are selected. A candidate with no complete upstream inventory is not silently omitted: the tournament is `BLOCKED` until that arm is removed by a new versioned registry.

The primary accuracy comparison uses the exact same `K_common` for every candidate, including candidates that have a different native source or geometry. If `K_common` is empty, the tournament is `BLOCKED`; it is never replaced with a favorable candidate-specific intersection. If a candidate cannot produce a value on a `K_common` key because of a row-level failure discovered after the universe freeze, its value is missing and the candidate fails its coverage gate; that key is not removed after the fact.

A secondary coverage ledger reports every `U_outcome` key and every candidate's status (`eligible`, `incomplete_geometry`, `missing_issue`, `missing_member`, `missing_outcome`, etc.). Secondary method-specific scores are descriptive only and cannot support a winner claim. The key file and its hash are created before metric calculation.

## 3. Information cutoff and provenance contract

### 3.1 Common as-of cutoff

For every `(S,D,E)`, the forecast information cutoff is frozen to:

```text
asof_cutoff_ts_utc = 12:00:00 UTC on calendar day D-1
```

A selected forecast revision must satisfy both:

```text
issue_ts_utc      <= asof_cutoff_ts_utc
available_ts_utc  <= asof_cutoff_ts_utc
```

The evaluator selects the latest eligible revision using this deterministic order:

1. greatest proven `issue_ts_utc`;
2. greatest proven `available_ts_utc`;
3. ascending immutable `revision_id`;
4. ascending `source_content_sha256`.

A row lacking a dated issue time, independently auditable availability time, valid-time identity, revision identity, or content hash is ineligible. A cycle label (`12Z`), `target_date`, `step`, database row order, filesystem mtime, fetch time, script run time, or publication schedule is not a substitute for a timestamp. Conflicting revisions with no deterministic ordering are `ambiguous_revision` and fail closed.

### 3.2 No target-day leakage

Prediction-side code may receive only source forecasts and records known strictly before `asof_cutoff_ts_utc`. Allowed history includes prior finalized outcomes, prior observations, source metadata, and pre-cutoff model fields. The following are label-only and may be read only after predictions and all selection decisions are frozen:

- target-day METAR/ASOS observations;
- target-day daily statistics, aggregates, or derived extrema;
- target-day settlement/outcome values and corrections;
- any post-cutoff forecast revision or source file;
- any feature computed using the target outcome or target-day observation.

For target `D`, all feature timestamps must be `< min(target local-day start, asof_cutoff_ts_utc)`. A mutation audit must replace target-day observations and outcomes with different values and prove that prediction rows, eligibility, fitted parameters, selected candidate IDs, and key sets do not change; only the post-prediction score attachment may change.

### 3.3 Outcome and revision provenance

The outcome snapshot is immutable and hashed. Each outcome row must include station identity, local target date, HIGH/LOW identity, value, source report/series identity, revision identifier, and snapshot hash. An outcome correction creates a new versioned run; it must not silently rewrite an earlier artifact or alter a forecast-side fit. A missing or ambiguous HIGH/LOW outcome excludes the key from `U_outcome` with a reason and can block the run if common-universe requirements are not met.

## 4. Exact candidate registry

The following registry is exhaustive for version 1. No unlisted parameter, source, geometry, calibration, feature, threshold, fallback, or ensemble weight may be tested and called part of this tournament. Every registered candidate is evaluated for HIGH and LOW by applying the event-specific transform stated below.

| ID | Family | Source/input | Exact representation and frozen parameters | Output |
|---|---|---|---|---|
| `CTRL-PERSIST-EVENT` | control | prior finalized outcome | Same station/event prior value, strictly prior to `D`; no smoothing | point |
| `CTRL-CLIM31-EVENT` | control | prior finalized outcomes | Station × event, circular 31-day day-of-year window; expanding history only; minimum 20 values; fallback is station × event expanding mean, otherwise missing | point |
| `GEFS-FIX12-MEAN` | point | GEFS | Equal-weight member mean at exact local 12:00; no bias correction | point |
| `GEFS-PATH-EXT-MEAN` | path | GEFS | Equal-weight member-mean path; HIGH=`max(path)`, LOW=`min(path)` over complete local-day panel | point |
| `GEFS-MEMBER-EXT-P50` | distribution | GEFS | For each member, local-day HIGH max or LOW min; point is empirical P50; equal member weights | point + empirical distribution |
| `GEFS-MEMBER-EXT-BUCKET` | distribution | GEFS | Same member extrema as above; integer bucket PMF for every Fahrenheit integer from -40 through 130, with mass assigned to the exact member value and clipped-range mass recorded separately | PMF/CDF |
| `GEFS-MEMBER-EXT-CAL` | calibrated distribution | GEFS | Same member extrema; one pooled source × event additive residual correction and empirical residual CDF fit on training history only; no station or regime parameter | point + distribution |
| `IFS-FIX12-MEAN` | point | ECMWF IFS/TIGGE | Equal-weight member mean at exact local 12:00; no bias correction | point |
| `IFS-PATH-EXT-MEAN` | path | ECMWF IFS/TIGGE | Equal-weight member-mean path; HIGH max/LOW min over complete local-day panel | point |
| `IFS-MEMBER-EXT-P50` | distribution | ECMWF IFS/TIGGE | Per-member local-day extrema; point empirical P50; equal member weights | point + empirical distribution |
| `IFS-MEMBER-EXT-BUCKET` | distribution | ECMWF IFS/TIGGE | Same member extrema; integer -40..130 bucket PMF/CDF; no imputation | PMF/CDF |
| `IFS-MEMBER-EXT-CAL` | calibrated distribution | ECMWF IFS/TIGGE | Same pooled source × event additive residual and empirical residual CDF procedure as GEFS; fit history only | point + distribution |
| `EQ-GEFS-IFS-EXT` | combined | independently valid GEFS + IFS | Each source produces member extrema independently; total source weight 0.5/0.5, equal conditional member weight within source; no source fallback | point + distribution |
| `EQ-GEFS-IFS-BUCKET` | combined | independently valid GEFS + IFS | Same equal-source member-extrema distribution, rendered as integer -40..130 PMF/CDF | PMF/CDF |
| `DTW-GEFS-K8` | trajectory diagnostic | GEFS complete mean path + prior history | Z-normalized mean paths; Sakoe–Chiba band 2 samples; 8 nearest training analogs; equal analog weight; point is mean analog local-day extrema; no test labels | point + analog spread |

Registry rules:

1. `HIGH` always selects maximum and `LOW` always selects minimum; there is no direction reversal or shared extrema statistic.
2. The combined IDs require both parent sources to pass every provenance, geometry, and member gate on the same key. They have no GEFS-only or IFS-only fallback.
3. A calibrated candidate may fit only the explicitly stated pooled source × event parameters; station, direction, season, lead, or regime specialization is a different candidate and is not implicitly included.
4. The controls are mandatory baselines. A candidate that does not beat the predeclared incumbent control and source baseline cannot advance.
5. A candidate with incomplete local-day geometry is absent from `K_common` only if the pre-score registry inventory marks the entire arm unavailable; otherwise its missing value is retained and the arm fails coverage. No geometry failure may be hidden by a different representation.

## 5. Forecast panel, member identity, and missingness

### 5.1 Member identity

Every ensemble member must have a stable source-issued member ID tied to the selected issue/revision. Values without member IDs are not an ensemble. Array position, blob order, SQL row order, or `n_members` alone is not identity evidence unless the immutable source manifest proves that ordering and records the mapping.

For each source/issue/station/date/event, record `n_expected` from verified issue-cycle metadata, the complete expected ID set, present IDs, missing IDs, duplicate IDs, and unknown IDs. A member is present only if its value is finite, physically valid, uniquely identified, and attached to the selected vintage.

The primary member gate is:

```text
n_present >= ceil(0.80 * n_expected)
```

A row fails with `missing_member`, `duplicate_member`, `unknown_member`, or `below_member_threshold` as applicable. No member is replaced by zero, mean, climatology, persistence, another source, another issue, or target-day information. Panels with different nominal counts are retained as different strata; a 30-member panel is not padded to 31, and a six-member panel is not expanded.

For equal member weights, record `effective_N = 1 / sum(w_i^2) = n_present`; if a future registered candidate uses other weights, record the exact weights and effective N. The combined arms record source weights, source-specific member IDs, and effective N separately. Correlation is diagnostic and never permission to duplicate or de-correlate members.

### 5.2 Missingness is a scored result

The ledger must preserve missingness by station, event, date, source, issue cycle, lead, season, and member-count class. Missingness may not be conditioned on outcome correctness or used to choose a favorable denominator. A candidate with insufficient coverage is `PARK`/`BLOCKED`, not a candidate with a conveniently smaller sample.

## 6. Nested temporal design: train, tune, locked holdout

The split is fixed before any run:

| Layer | Inclusive target-local dates | Use |
|---|---|---|
| `TRAIN_HISTORY` | 2021-08-19 through 2025-12-31 | Fit registered model parameters and inner-selection decisions only |
| `TUNE` | 2026-01-01 through 2026-02-28 | Select candidate family/geometry and predeclared specialization only |
| `LOCKED_HOLDOUT` | 2026-03-01 through 2026-08-13 | One final, untouched report after all decisions are frozen |

`TUNE` is itself nested into two chronological folds:

- `TUNE-01`: 2026-01-01..2026-01-31, with fitting history ending 2025-12-31;
- `TUNE-02`: 2026-02-01..2026-02-28, with fitting history ending 2026-01-31.

Any fitted parameter for a row must use only labels strictly before that row's target date. Candidate selection is performed once, after both tune folds, using predeclared pooled and fold-stability rules. The locked holdout is never used to choose a candidate, geometry, source, calibration, threshold, station, direction, regime, or fallback.

Before candidate-family selection, any learned configuration inside a registered arm uses an inner expanding walk-forward fit within `TRAIN_HISTORY`: minimum 365 prior local-date observations for a pooled parameter, no random shuffling, and a deterministic tie-break by registry order. If an arm cannot satisfy its fit-history requirement, it is unavailable; no dates are moved between partitions.

After selection on `TUNE`, the selected registered candidate is refit using `TRAIN_HISTORY + TUNE` labels strictly before 2026-03-01, with its parameters and feature schema frozen. The refit is not a new opportunity to change the candidate or specialization. All locked predictions are generated before any locked outcomes are attached.

## 7. Selection, multiple testing, and specialization control

### 7.1 Primary comparison

Primary point accuracy is paired MAE on the immutable `K_common`, with station/date block resampling. Secondary metrics are RMSE, signed bias, median absolute error, directional accuracy using the frozen `±0.5°F` rule, CRPS for distributions, bucket log loss/Brier where defined, interval coverage, and sharpness. Every denominator is reported.

The incumbent for each comparison is `CTRL-CLIM31-EVENT`; the source control comparison is also against the relevant raw GEFS/IFS arm. The primary improvement margin is `0.10°F` MAE. A lower MAE without the predeclared margin is not a win.

### 7.2 Correction for model/family search

All registry IDs are considered pre-registered, including candidates that later fail availability. On each tune fold and the pooled tune set:

1. compute paired station/date-block differences against the incumbent;
2. use a deterministic 10,000-replicate block bootstrap with seed `20260815` and blocks defined by station × local date (HIGH and LOW remain separate events within a block);
3. apply Romano–Wolf step-down adjustment at `q=0.10` within each family for the one-sided hypothesis of at least `0.10°F` improvement;
4. apply Holm–Bonferroni across the family-level winning hypotheses at `alpha=0.05`;
5. require the selected candidate to win the pooled tune set and both monthly tune folds, with no family-adjusted tune lower confidence bound crossing zero.

A candidate selected because of a raw unadjusted minimum is a selection failure. Ties within `0.01°F` adjusted MAE are resolved in this order: lower adjusted MAE, lower RMSE, higher coverage, registry ID lexicographic order. This tie-break is fixed before scoring.

The locked holdout is a single confirmatory report for the candidate fixed by tune. Report paired confidence intervals and family-adjusted comparisons, but do not reselect a locked winner. If locked results contradict tune, retain the preselected candidate result and label the arm `PARK` or `KILL`; do not use the holdout to revise the registry.

### 7.3 Station/direction specialization anti-overfit rules

Specialization is not an automatic post-processing step. **No specialization arm is active in the v1 registry above.** A future run may add at most one explicitly named `STATION`, `EVENT`, or `STATION×EVENT` arm per parent family through a new dated registry/spec version; no station × lead × season × regime interaction is allowed in this tournament.

When such a separately versioned arm exists, it may be considered only when all conditions hold:

- it is registered before the run and consumes one of at most three specialization slots per parent family;
- the cell has at least 40 tune events, at least 20 events in each tune month, and at least 80 training events before the first tune fold;
- it improves the pooled parent by at least `0.10°F` adjusted tune MAE, with a family-corrected lower confidence bound below zero, in both tune folds;
- no cell with at least 20 tune events is more than `0.25°F` worse than the pooled parent;
- the hierarchy is chosen once (station **or** event **or** station×event), never searched as a combinatorial ladder;
- an unsupported cell uses the predeclared pooled parent, and that fallback is reported; it may not be silently dropped;
- selection is frozen before locked scoring and is not changed because one locked station or direction looks favorable.

The station×event threshold is intentionally demanding given the short tune window. If a cell cannot meet it, the correct result is pooled or unavailable, not a lower threshold. Specialization must also pass the family gate on the common universe; a gain caused by removing difficult stations/directions is invalid.

## 8. Family-level gates and dispositions

Each arm receives `ADVANCE_TO_SEPARATE_RESEARCH`, `DESCRIPTIVE_ONLY`, `PARK`, `KILL`, or `BLOCKED`.

### 8.1 Mandatory integrity gates

The tournament is QC PASS only if all are true:

1. exact station/date/event identity and no duplicate canonical keys;
2. common cutoff, dated issue, and independently auditable availability pass;
3. local-day HIGH/LOW geometry and expected cadence are proven;
4. member identity, expected count, missingness, and revision ordering pass;
5. target-day mutation test shows no prediction-side effect;
6. `K_common` is generated before metrics and is immutable afterward;
7. repeated identical runs produce identical row, metric, and manifest hashes;
8. all input/output/code/config/runtime hashes and denominators are present.

Any hard-gate failure blocks the affected arm. If a shared input failure means the common universe cannot be established, the whole tournament is `BLOCKED`.

### 8.2 Family gates

A family can advance only if its integrity gates pass, it has at least 90% coverage of `K_common` (and reports coverage against `U_outcome`), and its selected arm satisfies all of:

- tune improvement of at least `0.10°F` MAE with the correction procedure above;
- locked paired MAE improvement of at least `0.10°F` with a 95% block-bootstrap lower confidence bound above zero;
- no more than `0.25°F` locked MAE degradation in any station or HIGH/LOW stratum with at least 30 scored events;
- for directional output, a predeclared at-least-2 percentage-point gain is secondary evidence only and cannot rescue a failed point-MAE gate;
- for distribution arms, no more than 5 percentage points under nominal coverage for 50%, 80%, or 90% intervals and no unexplained log-loss/CRPS failure;
- no materially different key population, missingness pattern, or geometry than the comparator that explains the apparent gain.

A family with valid provenance but insufficient coverage is `DESCRIPTIVE_ONLY` or `PARK`, never a failed accuracy claim. A family whose premise requires guessed issue times, guessed geometry, member imputation, target-day data, or post-hoc denominators is `BLOCKED`/`KILL` as specified by the review owner.

### 8.3 Required reporting strata

Report pooled, HIGH, LOW, each station, season (DJF/MAM/JJA/SON), issue cycle/lead, and member-count/effective-N stratum where support exists. A stratum with fewer than 30 events is descriptive and must show its denominator; it cannot drive a specialization or gate.

## 9. Deterministic row artifacts and hashes

Write these artifacts under `artifacts/temperature_geometry_tournament/<run_id>/` without modifying inputs:

1. `candidate_registry.json` — exact registry, version, parameters, and registry hash;
2. `evaluation_universe.jsonl` — `U_master`, outcome status, common eligibility, and frozen `K_common` marker;
3. `prediction_rows.jsonl` — one canonical row per candidate/key/status;
4. `exclusion_ledger.jsonl` — one row per excluded or missing candidate/key and machine-readable reason;
5. `metrics.json` and `metrics.csv` — metrics, confidence intervals, adjusted p-values, and all denominators;
6. `selection_receipt.json` — tune-fold scores, corrected selection, tie-break, and locked candidate IDs;
7. `run_manifest.json` — lineage and hashes.

Canonical JSON is UTF-8, LF-delimited, sorted object keys, no insignificant whitespace, finite IEEE-754 numbers rendered with a single specified formatter, and explicit `null` for unavailable values. JSONL rows are sorted by:

```text
(station, target_date_local, event_type, candidate_id, row_status)
```

Each row contains at least:

```text
candidate_id, family, station, target_date_local, event_type,
local_start_ts, local_end_ts, valid_timestamps,
asof_cutoff_ts_utc, issue_ts_utc, available_ts_utc,
source, revision_id, source_content_sha256,
geometry_id, cadence_minutes, n_expected, n_present, n_missing,
member_ids_or_hash, effective_n, prediction_f,
quantiles_or_pmf, actual_f, fit_end_date, partition,
row_status, exclusion_reason, code_commit, spec_version
```

`actual_f` is null in prediction-side artifacts and is populated only in a separate score-attachment stage after the prediction file and selection state are frozen. `actual_f` is never passed back to a fitting or selection function.

For each canonical row, compute `row_sha256 = SHA256(canonical_row_bytes)`. Compute each file SHA-256 over exact bytes, then a sorted file-hash list and `run_fingerprint = SHA256(spec_hash || registry_hash || code_commit || config_hash || input_hashes || environment_lock_hash)`. The manifest records query text, source paths, source schema hashes, timezone database version, package/runtime versions, deterministic seed, sort rules, partition boundaries, counts, exclusion totals, and every output hash. The manifest's own self-hash is computed over a canonical copy with `manifest_sha256` set to null.

A deterministic rerun with the same source bytes, code commit, registry, configuration, environment lock, and seed must reproduce byte-identical row files, key files, metrics, selection receipt, and manifest (apart from an explicitly excluded wall-clock execution field, which is not allowed to affect the run fingerprint). Any difference is a QC failure.

## 10. Stop criteria

Stop immediately and mark the run `BLOCKED` if any of the following occurs:

- an issue timestamp or availability timestamp cannot be proven;
- a revision is selected using fetch time, database order, mtime, or an inferred schedule;
- local station timezone/DST mapping is absent or HIGH/LOW settlement-window equivalence is unproven;
- a path does not have complete expected local-day geometry, cadence, or unique valid timestamps;
- a scalar is treated as both HIGH and LOW without a registered event geometry;
- member IDs, expected counts, or missing-member identities cannot be reconstructed;
- a member, forecast, label, or feature is silently imputed;
- target-day observations, outcomes, corrected settlements, or post-cutoff revisions reach prediction-side state;
- any holdout label reaches fitting, calibration, normalization, candidate selection, or specialization;
- duplicate canonical keys or conflicting revisions remain unresolved;
- `K_common` changes after scoring or after viewing results;
- an apparent gain depends on a candidate-specific denominator or removed difficult stratum;
- deterministic rerun hashes differ;
- a gate can pass only after changing the frozen date range, cutoff, station list, candidate registry, missingness rule, or metric;
- an execution request would modify production, trading, SignalRegistry, or B-mode behavior.

Do not replace a stopped run with an easier subset and do not report zero skill for a blocked arm. Report the exact blocked gate, affected keys, hashes, and recovery evidence required for a new versioned run.

## 11. Accuracy-focused suggestion

**Suggestion:** make paired, key-matched continuous MAE on the complete common universe the primary advancement statistic, with station × local-date block-bootstrap confidence intervals; use directional accuracy and market-bucket scores only as secondary diagnostics.

This is the most accuracy-focused choice because the tournament's central question is whether a candidate estimates the actual station-local daily extreme more closely, not whether a threshold happened to move in the profitable direction. Pairing every candidate on the same keys controls for weather difficulty, while blocking by station/date avoids treating HIGH and LOW from the same weather day or repeated dates as independent evidence. It also prevents an apparent geometry win from being caused by easier coverage, different missingness, or a favorable target-day subset.

## 12. QC completion checklist

A reviewer may mark the tournament complete only when the artifact set includes:

- frozen registry and registry hash;
- immutable `U_master`, `U_outcome`, `U_eligible`, and `K_common` files;
- source issue/availability/revision evidence;
- station-local HIGH/LOW window and cadence audit;
- member identity/missingness ledger;
- leakage mutation-test receipt;
- nested train/tune/locked split receipt;
- corrected family selection and specialization receipt;
- locked metrics with confidence intervals and denominators;
- deterministic rerun/hash receipt;
- per-family disposition and stop/block reasons.

This document is a QC plan only. No experiments or production changes are implied or authorized by its existence.
