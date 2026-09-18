# Trajectory DTW QC / Specification

**Date:** 2026-08-15  
**Scope:** Original meteorological trajectory lane only  
**Repository:** `prototypes/weather-engine-source`  
**Status:** QC specification; no production or trading change

## 0. Purpose and hard boundaries

This document specifies an auditable evaluation protocol for the **original DTW trajectory experiment**: match a query sequence of observed meteorological epochs (for example, daily temperature, humidity/dewpoint, pressure, wind speed, and circular wind direction) to historical weather sequences and use the eligible analog outcomes to produce a probability distribution over settlement buckets.

This is a research lane and a guide, not an execution override. It must not veto, force, resize, or silently replace the GEFS/ECMWF pipeline. The lane must remain distinct from:

- settlement-epoch or intraday market-trajectory matching;
- the existing linear `test_trajectory_gate.py` concept;
- the source-less `core/trajectory/*.pyc` Kalman/intraday path code; and
- any alternative predictor substituted under the DTW name.

**Explicit prohibition:** do **not** substitute k-nearest-neighbor (k-NN), nearest raw-vector search, Kalman filtering, regression, HMM, or another model for the DTW matcher. A fixed-size top-*K* list of DTW-ranked analogs is allowed; the ranking and distance must still be DTW under this specification. Kalman code or k-NN scripts may be used only as separately labeled comparators, never as the original DTW result.

## 1. Evaluation unit, clocks, and labels

### 1.1 Forecast origin and information set

Define a forecast origin `o = (station, target_date, as_of_epoch)` and record it in UTC. `as_of_epoch` is the last observation timestamp available to the lane. The query feature window may use only observations with timestamp `<= as_of_epoch`, after applying the production-like publication/availability lag. The settlement label is the target station/date outcome, resolved only after its official settlement/observation window closes.

For every input field, retain `source_timestamp`, `ingest_timestamp` (if available), `available_at`, and `used_at`. A value is eligible only when `available_at <= as_of_epoch`. If an upstream revision arrives after the origin, it is not backfilled into that origin's information set.

### 1.2 Target and bucket labels

A query predicts a predeclared bucket partition for the target settlement value (for example, `below_85`, `85-89`, `90-94`, etc.). Bucket boundaries, inclusivity, unit conversion, station mapping, and settlement source are frozen before any test run. The bucket label is never used in feature construction, normalization, candidate filtering, tuning, calibration, abstention, or weighting.

If the task is a threshold market, derive the event indicator from the same frozen settlement label. Evaluate both the full bucket distribution and the event probability; do not choose whichever metric looks better after seeing test results.

### 1.3 One target, one prediction

A target date/station has one scored prediction per declared origin. If multiple origins exist for a target, they are separate forecast instances with a predeclared aggregation rule; do not select the best origin retrospectively. Queries whose required information is missing are reported as unavailable/abstained, never silently replaced by a later snapshot.

## 2. Rolling-origin train / tune / test design

### 2.1 Outer rolling-origin protocol

Use a chronological, expanding-window evaluation. Let `T_0 < T_1 < ... < T_J` be test-origin cutoffs and let `H` be the maximum outcome look-ahead/settlement horizon plus any declared publication lag. For fold `j`:

```text
TRAIN_j = all complete origins/outcomes with target/settlement time < T_j - embargo
TUNE_j  = the immediately following chronological block, ending before T_j
TEST_j  = origins in [T_j, T_{j+1})
```

The embargo must be at least the maximum period during which a candidate's feature window or target outcome could overlap the test query. For a 5-day daily trajectory, use a minimum 5-day **target-date embargo** between the latest train/tune target and the first test query; use a larger embargo if the source's publication/settlement timing requires it. A candidate may not share any raw observation timestamp with a query, even if date labels differ.

Recommended minimum reporting layout (adjust only before the protocol is frozen):

- **Development history:** earliest complete history through the first origin;
- **Tune block:** a contiguous 10–20% chronological block used only for selecting hyperparameters, feature set, calibration, thresholds, and policy;
- **Test blocks:** at least three chronological blocks, each containing enough station-date cases to report per-station and pooled results. Prefer monthly or quarterly blocks rather than random rows.

For each test block, fit/replay using only data that would have existed immediately before that block. After a test block is scored, its labels may enter the next fold's training history, but never revise the prior fold's choices. Report pooled metrics and fold-by-fold metrics; do not pool rows across folds to tune a setting.

### 2.2 Nested tuning

All choices are made inside each outer fold:

1. Fit feature availability rules, normalization statistics, candidate index, and DTW configuration on `TRAIN_j` only.
2. Use `TUNE_j` only to choose among the predeclared grid (window length, DTW radius, feature weights, minimum analog count, distance cutoff, recency policy, calibration method, and abstention thresholds).
3. Lock the selected configuration and refit permitted state using `TRAIN_j ∪ TUNE_j` **without using test labels**. Normalization and calibration may be refit on this combined pre-test history only if the procedure is declared before scoring.
4. Freeze the complete artifact and score `TEST_j` once.

No manual inspection of test outcomes, test bucket frequencies, test distance distributions, test calibration, or test trade/PnL may cause a rerun with changed settings. A failed or incomplete fold is an explicit missing fold, not a reason to search settings until it passes.

### 2.3 No future epoch / settlement leakage

The implementation must enforce all of the following:

- Never use future observations, future forecast revisions, future daily aggregates, or future settlement labels in a query feature window.
- Never normalize a test query or candidate with statistics calculated from the whole dataset, from the test period, or from dates after the origin. For each fold, stats are fit on pre-origin training history (or a rolling, past-only window) and then applied unchanged to tune/test.
- Never fill an unavailable epoch using a future observed value. A fill may use only a past-only rule (for example, interpolation between two known values is **not** allowed if the right-hand value was not available at origin). If a declared operational forecast fill is used, it must be timestamp-eligible and tagged as model-filled.
- Never use target settlement temperature, bucket, final high/low, or any post-settlement quality flag to decide whether a candidate, feature, or query is eligible.
- Never allow a candidate sequence's outcome horizon to overlap the query origin or embargo. Candidate outcome must have been fully resolved before the query's origin minus the embargo.
- Never let duplicate revisions, mirrored database rows, or multiple source records give one date extra weight. Deduplicate by a frozen identity key before splitting.
- Never calculate coverage, missingness thresholds, feature selection, station pooling, climate zones, or outlier bounds using future/test information. Fit rules on training only; apply them forward.

The QC harness should fail closed with a `LEAKAGE_ERROR` when an input violates these rules, and the artifact must include the offending IDs/timestamps.

## 3. Query and candidate sequence definition

### 3.1 Epochs and windows

The primary candidate is a contiguous sequence of `L=5` daily epochs ending at a candidate query/target date, with a predeclared secondary `L=3` analysis. A daily epoch is built only from observations available by the epoch's cutoff. If the operational query is intraday, the daily aggregate must be a causal-to-origin aggregate, not a finalized end-of-day aggregate.

For every sequence store:

```text
station, target_date, origin/as_of_epoch, epoch timestamps,
feature names/order/units, raw-source IDs, missingness mask,
fill mask and fill method, normalization artifact ID, sequence hash
```

A sequence is **contiguous by source epoch labels**, not merely five rows after sorting. Missing calendar days do not become adjacent just because the database has no row. The default policy is to reject a sequence with a missing required epoch. A secondary, predeclared gap policy may permit at most one missing epoch only when a past-only eligible fill exists; it must carry a penalty and be analyzed separately. Never use a future GEFS/settlement value as a convenient gap fill.

### 3.2 Candidate eligibility

For query `q=(s_q, d_q, o_q)` a historical candidate `c=(s_c, d_c)` is eligible only if:

1. it is a **completed historical target** with a resolved outcome and complete candidate feature sequence;
2. every candidate feature was available no later than its own epoch cutoff;
3. `c` is strictly before the query under the embargo rule: candidate target/settlement completion `< o_q - embargo`; for same-station daily windows, require the candidate window's final epoch to end at least `L` days before the query window starts, unless a larger source-specific embargo is declared;
4. it does not share any raw observation row, forecast run, settlement row, or duplicated source identity with the query;
5. it is not the query date, a duplicate of the query, or a future revision of the query;
6. it passes the frozen physical QC and missingness rules learned from training;
7. it belongs to the predeclared universe (same-station first; cross-station/climate-zone pooling only if selected on tune and logged); and
8. it is not filtered using its realized outcome, bucket, or post-origin information.

Candidate inclusion is based on **information available before the candidate's own outcome**, not on whether its eventual outcome resembles the query. Record rejected-candidate counts by reason. If the eligible set is empty or fewer than the minimum analog count, return `ABSTAIN_INSUFFICIENT_ANALOGS` rather than widening rules after the fact.

Do not use a soft station boost as a hidden universe change. For each run declare one of: same-station only; predeclared same-climate pool; or a fixed, training-only station universe. Report all station and zone contributions.

## 4. DTW distance and feature scaling

### 4.1 Distance definition

Let `x_1..x_n` be the query sequence and `y_1..y_m` a candidate. The lane uses multivariate DTW with a predeclared local cost and a Sakoe–Chiba window:

```text
D(i,j) = cost(x_i, y_j) + min(D(i-1,j), D(i,j-1), D(i-1,j-1))
DTW(x,y) = D(n,m) / path_length   # path normalization frozen before tuning
```

The local cost is weighted Euclidean distance over scaled features. For wind direction, use a unit-vector representation `(sin θ, cos θ)` or an explicitly circular angular distance; do not treat degrees as a linear scalar. The implementation must log the exact recurrence, boundary conditions, path normalization, radius/window, and feature order. Fast/approximate DTW is allowed only if its approximation and deterministic tie behavior are declared and validated against exact DTW on a QC sample.

The DTW window/radius is a hyperparameter, never chosen from test distances. Candidate grid must be frozen before running (for example, radius 0, 1, or 2 epochs for 3/5-day sequences; a radius larger than the sequence length is not meaningful). Use the same selected setting for all test cases in a fold. Do not silently switch to unconstrained DTW for difficult cases.

### 4.2 Feature scaling rules

Scale every numeric feature before distance calculation so pressure cannot dominate temperature and wind. The scaling policy is causal and fold-local:

- Fit location/scale parameters on `TRAIN_j` only, preferably by station × season when training support meets a declared minimum; otherwise fall back to station, then a training-only pooled fallback.
- Use robust, frozen handling for outliers (physical bounds plus training-fitted median/IQR or mean/std). Do not let test values change the scale. Clip only to bounds fit/declared before test and log clipping.
- Apply the **same** fitted transform to the query and all eligible candidates in that fold. Do not independently z-score each sequence: that erases level information relevant to settlement buckets.
- Circular wind direction is encoded before scaling; sin/cos should have fixed semantics and no future-derived scaling unless the fold explicitly fits it on training.
- A feature with insufficient training coverage is either excluded for the entire fold or handled by the declared missingness path; it must not be opportunistically dropped per test row based on its outcome.
- Feature weights are nonnegative and sum to one. Weight candidates are tuned only on `TUNE_j`, then frozen for `TEST_j`. Store them in the artifact.

The default QC feature set is `temp`, `RH or dewpoint`, `pressure`, `wind speed`, and circular wind direction. If RH is unavailable, do not silently substitute dewpoint under the same feature name: declare the alternative feature set as a separate configuration and score it separately. Day-of-year context, if used, must be causal, circular, and included in the frozen schema.

### 4.3 Deterministic ties and ranking

Sort analogs by `(dtw_distance, candidate_station, candidate_target_date, candidate_id)` using a stable total order. The top-*K* cap is a reporting/aggregation cap after DTW ranking, not a k-NN substitute. Equal distances must produce identical ordering across runs.

## 5. Analog outcome aggregation and CRPS tuning

### 5.1 Distribution construction

For each eligible analog, carry its settlement value or bucket label and a deterministic weight. The default is uniform over the selected DTW analog set; a distance-decay weight is allowed only as a predeclared, tune-selected option. If recency or station weights are used, they must be fixed functions of information available at the origin and normalized without target labels.

Produce:

- a full bucket probability vector (including zero-count declared buckets);
- effective analog count and raw count;
- weighted and unweighted counts if weighting is enabled;
- distance summary (minimum, median, selected-cutoff, and top-K mean);
- station/season/regime composition;
- uncertainty interval or bootstrap/Dirichlet procedure fixed before test;
- a quality/coverage state used by abstention.

Never report the modal bucket alone as the probability forecast.

### 5.2 CRPS and tuning without test peeking

For ordinal/continuous settlement outcomes, use the declared CRPS definition (or an explicitly declared discrete/threshold-integrated CRPS for buckets). Tune `L`, radius, feature weights, analog cap/cutoff, distance decay, pool, and any smoothing strength by minimizing mean **tune-fold CRPS** over `TUNE_j`. Use a primary metric and tie-breakers declared before the run (recommended: CRPS primary; log loss and coverage/calibration diagnostics as tie-breakers).

Do not tune on test CRPS, test log loss, test Brier, test calibration, test accuracy, test bucket hit rate, or test trade/PnL. Do not select a result because its test confidence interval is favorable. Test CRPS is a one-pass report only. If several settings are compared in a public report, disclose the full predeclared grid and multiplicity; no post-test winner is promoted.

For small tune blocks, use a predeclared minimum sample rule and report instability. If no configuration clears the minimum tune coverage/finite-score rule, the fold is `PARK`, not a reason to widen the grid.

## 6. Bucket recommendation scoring

A recommendation is a statistical output, not an order. For bucket `b`, compute:

```text
p_traj(b)       = aggregated analog probability
p_market(b)     = market probability, only if the experiment explicitly compares prices
edge(b)         = p_traj(b) - p_market(b)
uncertainty(b)  = predeclared interval/standard error
conservative(b) = lower confidence bound for a long/event recommendation
```

Use a predeclared score such as:

```text
score(b) = edge(b) - λ * uncertainty(b) - ρ * data_quality_penalty
```

where `λ`, `ρ`, minimum edge, and quality penalty are selected on tune only. A bucket is **RECOMMEND** only when it has sufficient effective analog count, finite uncertainty, conservative edge above the minimum, and no QC/coverage violation. Otherwise return `NO_EDGE` or `ABSTAIN`; never force the highest-probability bucket to be tradeable.

For a nested ladder, evaluate logical consistency (for example, `P(T≥90) <= P(T≥85)`) and normalize the vector. If independent bucket estimates violate monotonicity, apply a predeclared training-fitted or deterministic isotonic/PAVA projection and log the before/after vector. Do not repair only the test cases that look bad.

## 7. Probability calibration

Calibration is a learned post-processing step and must be isolated from the matcher:

1. Generate out-of-sample trajectory probabilities on the training/tune chronology using an inner rolling-origin procedure.
2. Fit the declared calibrator (recommended candidates: identity, beta calibration, or isotonic only when sample support is adequate) on inner-train predictions and labels.
3. Select the calibrator and any shrinkage toward a training-only climatology on the inner tune block by log loss/Brier/CRPS as predeclared.
4. Apply the frozen calibrator once to outer-test predictions. Never fit it on outer-test outcomes.

For multi-bucket outputs, preserve simplex validity and ladder monotonicity. Report pre- and post-calibration reliability, expected calibration error with bin definitions fixed in advance, Brier/log loss, CRPS, sharpness, and sample counts. Do not call a forecast calibrated because one test bin looks close to its frequency; require fold-consistent diagnostics and uncertainty intervals.

If there is not enough calibration data, use identity or a predeclared training-only climatology and mark `CALIBRATION_FALLBACK`; do not fit a flexible calibrator to a sparse test block.

## 8. Abstention policy

Abstention is a valid forecast outcome and must be evaluated, not hidden. Return `ABSTAIN` when any predeclared condition holds, including:

- fewer than `N_min` eligible or effective analogs;
- required feature/epoch missingness or an impermissible fill;
- DTW distance beyond a tune-selected cutoff;
- wide uncertainty / conservative edge not above threshold;
- calibration unavailable under its minimum support rule;
- leakage, duplicate, timestamp, or deterministic-artifact QC failure;
- out-of-domain station/season/regime with no approved fallback.

Default research floors should be conservative (for example, `N_min >= 20` raw analogs and a separately higher effective-count floor for weighted analogs), but exact values are tune parameters frozen before test. Report **coverage** (`non-abstained / eligible queries`), performance conditional on non-abstention, performance on all queries with abstentions scored as no recommendation, and abstention reasons. A system cannot pass by abstaining on hard test cases unless the coverage floor was predeclared and met.

## 9. Sequential update identity

The online/replay state is append-only and causal. After a target's settlement has completed and the source record is final, append exactly one identity-keyed outcome to the historical corpus for future origins. A sequential update may change:

- the candidate index by adding the newly completed candidate;
- training-only normalization statistics for a future fold/origin;
- calibration state when its declared update schedule is reached.

It may **not** change a previously issued prediction, its query features, its candidate eligibility, its DTW distance, its selected hyperparameters, or its test label. Reprocessing the same input must be idempotent (`upsert` by immutable `candidate_id`/source identity with conflict detection); a conflicting revision produces `REVISION_ERROR` and a new versioned artifact, never an in-place mutation of history.

Every update records `state_before_hash`, `input_event_id`, `state_after_hash`, `effective_at`, and the next eligible origin. The identity check must prove that the same ordered event stream and same frozen artifact reproduce the same prediction sequence.

## 10. Deterministic artifacts and hashes

Each fold/run must emit a manifest before scoring and a result manifest after scoring. At minimum include:

- repository commit/tree ID (or explicit dirty-tree file hash list);
- exact spec ID and runner version;
- source database/table/query fingerprints and row counts;
- immutable sorted input ID list/hash for train, tune, and test;
- feature schema/order/units, physical QC rules, missingness/fill masks;
- normalization/scaling table hash;
- DTW recurrence, radius, path normalization, feature weights, tie-break order;
- candidate eligibility/embargo policy and rejected counts;
- selected configuration and the full candidate grid;
- calibration and abstention configuration;
- random seed (preferably `0`/no randomness) and library/runtime versions;
- per-query prediction hash, per-fold score hash, and final report hash;
- UTC generation time only as metadata, never as a decision input.

Use canonical UTF-8 JSON (sorted keys, fixed float formatting, no NaN/Infinity) and SHA-256. Hash bytes, not pretty-printed semantic objects. A rerun with identical inputs, code, configuration, and environment must produce byte-identical canonical prediction artifacts and identical hashes. If nondeterministic parallel reduction is unavoidable, use a stable ordered reduction and test it across worker counts.

The QC gate fails on any hash mismatch, changed candidate order, changed number of eligible cases, or unrecorded input revision. Keep raw manifests and logs; do not overwrite a prior run.

## 11. Reporting requirements

Every run must report, by fold and pooled:

- query count, eligible count, abstain count/reasons, and coverage;
- analog count/effective count and DTW distance distribution;
- bucket CRPS, event CRPS/Brier/log loss, top-bucket accuracy only as a secondary diagnostic;
- pre/post-calibration reliability and ladder monotonicity violations;
- station, season, target bucket, and distance/analog-count strata;
- comparisons against a predeclared climatology/persistence baseline;
- sensitivity to 3-day vs 5-day windows and selected DTW radius;
- train/tune/test date ranges and embargo proof;
- exact artifact hashes and any failed QC checks.

Accuracy is useful but insufficient: a sharp, overconfident distribution can have good top-bucket accuracy and poor CRPS/calibration. The primary promotion metric is probabilistic and must be accompanied by coverage and stability.

## 12. Accuracy-focused suggestion

**Suggestion: add a causal “current-level plus trajectory-shape” DTW feature decomposition, evaluated as a predeclared ablation.** Keep raw/scaled temperature level (so a 90°F query does not match a 60°F sequence merely because both are rising), and add first differences or robust per-epoch changes for temperature, pressure, humidity, and wind. Tune the level/shape weights only on the rolling tune block, then freeze them for test.

**Why:** The original trajectory idea is about both *where the system is* (settlement-relevant level) and *how it got there* (front/advection/persistence shape). A shape-only or independently normalized sequence can erase level; a level-only distance misses dynamically different paths that happen to end at the same value. This suggestion targets accuracy without changing the matcher family, and the ablation makes any gain auditable rather than allowing feature creep. It must be rejected/parked if it improves test accuracy while worsening tune CRPS, calibration, or leakage evidence.

## 13. Decision gates

### ADVANCE

Advance the DTW lane only when all hard QC checks pass and, across the predeclared outer test folds:

- no leakage, duplicate, revision, or hash failure;
- coverage meets the predeclared floor and abstentions are not concentrated by cherry-picking;
- test CRPS is no worse than the predeclared baseline tolerance and shows a stable improvement in the majority of folds, with calibration not materially degraded;
- any claimed accuracy lift is accompanied by uncertainty intervals and no material degradation in log loss/Brier, calibration, or robustness strata;
- results reproduce byte-for-byte from the saved manifest; and
- the lane remains a guide with no production/trading wiring in this QC phase.

The numeric margin, minimum fold count, and coverage floor must be written into the run manifest **before** test scoring. A single favorable pooled number is not sufficient.

### PARK

Park when the implementation is deterministic and leak-free but evidence is inconclusive: insufficient history, sparse analogs, unstable station/season results, inadequate calibration support, tune/test score confidence intervals overlapping the baseline, or a potential gain that depends on a narrow stratum. PARK means preserve artifacts and collect more eligible history; do not widen eligibility, retune on test, or ship a hidden fallback.

### KILL

Kill the original DTW lane when any non-remediable hard failure occurs, including:

- confirmed future epoch/settlement leakage or inability to prove causal availability;
- non-reproducible predictions or untraceable input revisions;
- test performance materially worse than the frozen baseline across the required folds after correct scoring;
- no useful coverage under the declared abstention floor;
- severe miscalibration/overconfidence that cannot be repaired without test peeking; or
- the proposed implementation is actually k-NN, Kalman, settlement-epoch matching, or another substituted method rather than DTW.

KILL is a disposition of this experiment, not permission to relabel a replacement model as DTW. Any new method requires its own spec and separate train/tune/test artifacts.

## 14. QC checklist (sign-off)

- [ ] Forecast origin and source availability timestamps are present.
- [ ] Train/tune/test folds are chronological, embargoed, and frozen before scoring.
- [ ] Candidate outcomes and feature windows cannot overlap the query or future.
- [ ] Scaling is fit on training only and applied identically to query/candidates.
- [ ] DTW recurrence, radius, path normalization, weights, and tie ordering are logged.
- [ ] No k-NN/Kalman substitution or hidden station-pooling change exists.
- [ ] CRPS tuning and calibration are nested; outer test labels are untouched.
- [ ] Bucket recommendation and abstention thresholds were predeclared.
- [ ] Sequential state updates are idempotent and versioned.
- [ ] Canonical artifacts and SHA-256 hashes reproduce exactly.
- [ ] ADVANCE/PARK/KILL decision is supported by fold-level metrics and coverage.

**Conclusion:** This specification is a release gate for the original DTW trajectory experiment only. Until a run satisfies the controls above, its outputs are research diagnostics and must not be treated as validated trading evidence.
