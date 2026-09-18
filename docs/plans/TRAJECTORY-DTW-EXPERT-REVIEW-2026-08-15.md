# Expert Review: Exact DTW Trajectory Experiment

**Date:** 2026-08-15 UTC  
**Status:** Research/specification only; no production or trading change  
**Scope:** Original meteorological trajectory concept: DTW matching of epoch sequences to produce bucket-level predictive distributions and recommendations.

## 1. Executive decision

Advance a standalone, walk-forward experiment, but do not treat the existing trajectory modules as the implementation of this experiment.

The method under review is **dynamic time warping (DTW) over ordered weather-epoch sequences**. It is not k-nearest neighbors, a generic analog classifier, a Kalman filter, or a single-epoch weighted-Euclidean gate. The current repository contains useful research artifacts but also several incompatible paths:

- `scripts/a5_dtw_scaletest.py` is the closest existing DTW implementation. It uses five-day sequences, z-score normalization, same-station candidates, a Sakoe-Chiba band, and a temporal separation rule. Its reported scale result was 66.73% directional accuracy over 2,507 directional cases, but that is not evidence of calibrated bucket probabilities.
- The older trajectory research was diagnosed as having raw-scale distance, overlapping windows, corrupted METAR values, and broken scalar wind-direction encoding (`docs/weather-engine/diagnostics/trajectory-agreement-root-cause.md`).
- `core/trajectory_lane.py` and `core/trajectory_confirmation_gate.py` currently rely on epoch-level weighted Euclidean matching and gate/modulator behavior. Those are not substitutes for this DTW study and must not be silently relabeled as DTW results.
- `TRAJECTORY-DTW-ROADMAP-2026-08-15.md` correctly establishes temporal separation, feature ablations, rolling-origin CRPS tuning, sequential replay, and ADVANCE/PARK/KILL disposition.

The output of this document is a reproducible research contract. It intentionally leaves integration disabled.

## 2. Frozen prediction contract

### 2.1 Target and identity

Each prediction has an immutable key:

```text
station | market_side | target_settlement_date | target_bucket_schema
       | query_as_of_timestamp | local_timezone | sequence_definition
```

The target is the official settlement value for the target date and side (HIGH or LOW), mapped to the bucket schema that was in force for that market. Do not use a subsequently corrected or differently bucketed label without retaining the original label lineage.

All timestamps are stored in UTC and station-local time. Sequence ordering is by station-local observation time after deterministic conversion from UTC. DST transitions are represented by UTC timestamps plus local offset; local wall-clock labels alone are not a valid ordering key.

### 2.2 Epoch definition

The primary experiment uses a fixed **daily station epoch** so it is comparable to the existing five-epoch A5 artifact:

```text
E(s, d) = daily summary for station s and station-local date d,
          using only observations available by the declared cutoff.
```

The canonical sequence is five consecutive daily epochs ending at the latest eligible epoch. The target outcome is the next settlement date after the sequence endpoint. For an intraday replay, the last daily epoch is a partial, as-of summary at a declared local cutoff; it is not replaced by the eventual full-day summary. The candidate corpus uses the same elapsed-day/cutoff construction for historical dates.

A future hourly implementation may be evaluated as a separately named sequence definition (for example, fixed hourly bins), but it must not be mixed with the daily results. The sequence definition, epoch aggregation, required coverage, and cutoff are part of the experiment hash.

An epoch records:

- station and UTC/local date/time bounds;
- observation count and coverage flags;
- temperature summary appropriate to the market side (HIGH: max-so-far/max-of-day summary; LOW: min-so-far/min-of-day summary, with the side fixed before matching);
- relative humidity summary;
- sea-level pressure summary;
- predeclared optional channels only when the ablation includes them;
- no settlement outcome in the query sequence.

Do not fill a missing current epoch with a future observation. A sequence with insufficient coverage is either rejected under the predeclared rule or scored with an explicit missing-channel mask; the choice is fixed before results are inspected.

### 2.3 Exact DTW distance

For a query sequence `Q = (q_1, ..., q_n)` and candidate sequence `C = (c_1, ..., c_m)`, first normalize every channel using statistics fitted on the training portion of the current rolling fold only. Let `x_i,f` be the normalized value for feature `f` at epoch `i`, and `w_f` be the fixed channel weight for the ablation.

Per-epoch local cost:

```text
cost(i,j) = sqrt( sum_f w_f * mask(i,j,f) * (Q_i,f - C_j,f)^2
                 / sum_f w_f * mask(i,j,f) )
```

`mask` excludes a missing channel from both numerator and denominator; a minimum valid-channel rule prevents a nearly empty epoch from becoming a match. The DTW recurrence is:

```text
D(0,0) = 0
D(i,j) = cost(i,j) + min(D(i-1,j), D(i,j-1), D(i-1,j-1))
```

Only cells inside the predeclared Sakoe-Chiba band `|i-j| <= r` are evaluated. The returned distance is the accumulated path cost divided by path length (and not by an arbitrary candidate count). The path, endpoint, path length, valid-channel count, and raw/normalized distance must be retained in the diagnostic artifact. A deterministic tie-break is required: distance, then candidate endpoint date, then station ID.

This is sequence warping, not nearest-neighbor feature lookup. Selecting the lowest-distance DTW paths as analogs is permitted because the analogs are ranked by the DTW sequence distance; implementing a k-NN classifier or replacing the recurrence with a vector-space neighbor search is out of scope. Kalman filtering, state-space smoothing, or Bayesian filtering is also out of scope for this experiment.

### 2.4 Candidate eligibility

For a query as-of time `T_q`, a candidate sequence may use only historical observations and must have a resolved outcome before `T_q` in the replay. Candidate sequences:

1. have the same station and market side in the primary experiment;
2. use the identical epoch definition and cutoff offset;
3. contain the same target-bucket schema;
4. do not overlap the query sequence;
5. are separated by at least one full sequence length plus the declared outcome/settlement lag. The existing A5 five-day separation is a minimum compatibility check, not permission to use overlapping windows;
6. are removed if any value fails physical QC or if the candidate's outcome is missing/ambiguous.

Cross-station pooling, climate zones, and station bonuses are separate, predeclared ablations. They are not blended into the primary estimate after seeing scores.

## 3. Feature set and ablations

### 3.1 Minimal feature ladder

The minimal ladder is fixed before any tuning:

| ID | Channels | Purpose |
|---|---|---|
| F0 | temperature | Persistence/trend baseline; identifies how much signal is temperature alone |
| F1 | temperature + relative humidity | Adds moisture/heat-capacity information without synoptic pressure |
| F2 | temperature + relative humidity + pressure | Minimal multi-variable trajectory; primary candidate |

Temperature and humidity are computed from the same as-of observation set. Pressure is sea-level pressure if available; the source and QC rule are recorded. Each feature ladder has its own normalization statistics, but the statistics are fit only on the training fold. Channel weights and any missingness treatment are fixed within a fold before validation scoring.

The baseline weights should be simple and documented, not optimized against the final test: equal weights after z-scoring is the cleanest first comparison. A small predeclared weight grid may be tuned on the training/validation split only; no test-derived weights are allowed.

### 3.2 Predeclared additions

The following additions may be tested one at a time (and only later as a named combination if the first-stage evidence warrants it):

- `A1`: dewpoint (or specific humidity, but never both without a separate redundancy test);
- `A2`: temperature first difference/slope between adjacent epochs;
- `A3`: pressure first difference/24-hour tendency;
- `A4`: wind speed;
- `A5`: wind direction as `sin(direction), cos(direction)` (never scalar compass codes);
- `A6`: cloud-cover fraction or precipitation indicator, subject to coverage/QC;
- `A7`: diurnal temperature range, only where both extrema are observable by the as-of cutoff.

The addition list is frozen in the run manifest. No cloud, wind, radiation, precipitation, NWP, market price, or settlement-derived feature may be added because it looks useful in an intermediate result. A feature is admitted only if its availability timestamp, physical bounds, missingness, and training-fold normalization are documented.

### 3.3 Required ablation reporting

For every feature set report the same query rows, or a declared coverage-adjusted cohort in addition to the common cohort. Report overall and station/season/lead-time strata. A feature set that wins only by dropping hard cases is not an accuracy improvement. Include:

- bucket CRPS and log loss;
- Brier score for each bucket/event where defined;
- calibration/ECE and reliability counts;
- top-1 and top-k bucket accuracy;
- coverage/abstention rate;
- effective analog sample size and distance distribution;
- query count and missing-data exclusion count.

## 4. Analog count, distance, and recency policy

### 4.1 Count and distance

The tuning grid is deliberately small:

```text
K ∈ {10, 20, 40, 80, 120}
band radius r ∈ {0, 1, 2} for five daily epochs
normalized-distance form ∈ {path-mean, path-mean with valid-channel mask}
```

`K=40` is the descriptive default, not a production setting. The chosen K must be selected by validation CRPS and then frozen for the untouched test period. Always retain all candidates through at least K=120 when feasible so post-hoc diagnostics do not require rerunning a differently filtered matcher.

A distance-quality floor is also tuned on training/validation. The preferred representation is an empirical training-fold quantile (`q50`, `q75`, or `q90`) of valid DTW distances, rather than an absolute distance copied between stations. A query with no candidate below the selected floor abstains. Do not silently replace a sparse analog set with distant candidates merely to meet K.

The predictive weights for accepted analogs are:

```text
u_i = exp(-d_i / tau) * exp(-age_i / h)
```

where `d_i` is normalized DTW distance, `age_i` is days from candidate endpoint to query origin, `tau` and half-life `h` are tuned on rolling validation. The unweighted K-count estimate must also be reported as an ablation. The normalized analog weights, K, floor, and effective sample size are emitted with every prediction.

### 4.2 Recency settings

The recency grid is:

```text
half-life h ∈ {30, 90, 180, 365} days, plus no-recency baseline
```

Recency weighting is applied only after temporal eligibility and only to past candidates. It does not make a recent but poor DTW match acceptable. The candidate date, age, distance, station, and outcome must be visible in the artifact. A recent candidate is not independent of the query merely because it receives a decay weight.

Primary pooling is same-station. A predeclared comparison may test same climate class or geographically adjacent stations, but it must use station-specific normalization or a training-fitted pooled normalization and must report the change in sample composition. The old same-station distance bonus and cross-zone penalty must not be used as an undocumented compromise.

## 5. Rolling-origin CRPS tuning and evaluation

### 5.1 Split design

Use chronological rolling-origin evaluation independently by station, with shared parameter candidates and station-level diagnostics. A recommended initial design is:

- minimum historical training window: 180 days (expandable window is a separate declared variant);
- validation window: 30 days;
- test window: final 60–90 days, untouched until the configuration is frozen;
- origin cadence: daily where data supports it, otherwise weekly;
- enforce the outcome-resolution lag before any row can enter a training corpus.

At each origin, fit normalization, bucket prior/smoothing constants, distance floor, `K`, band, `tau`, and recency choices on training/validation only. Select one configuration using mean validation CRPS with a predeclared tie-break toward simpler/fewer channels and higher coverage. Refit on all data available before the final test origin, then evaluate the frozen configuration once on the test period. Do not choose a station, season, or bucket-specific exception after looking at test outcomes.

### 5.2 Scoring

For ordered settlement buckets `b_1...b_M`, turn the predictive mass `p` into a cumulative distribution and compute the discrete ranked probability score/CRPS:

```text
CRPS(p, y) = sum_j (CDF_p(b_j) - 1[y <= b_j])^2 * bucket_width_j
```

Use a consistent bucket-width convention, especially for open-ended buckets; report it in the manifest. Log loss is a secondary sharpness check and requires a positive probability for every eligible bucket (apply only a predeclared training-fit Dirichlet smoothing rule). Brier and ECE are reported for binary bucket events such as `T >= threshold`. Accuracy is diagnostic, not the tuning objective.

Compare against at least:

1. training-fold climatology by station/season;
2. persistence or prior-settlement distribution where valid;
3. a simple non-DTW temperature-only trajectory baseline;
4. the relevant existing forecast baseline, if its as-of information set is replayable.

A DTW variant advances only if it improves out-of-sample CRPS against the baseline with uncertainty intervals and does not produce materially worse calibration or pathological abstention in key strata.

## 6. Bucket distribution and recommendation contract

Each query returns a full distribution, not only a direction:

```json
{
  "station": "KMDW",
  "target_date": "YYYY-MM-DD",
  "as_of": "YYYY-MM-DDThh:mm:ssZ",
  "sequence_hash": "...",
  "feature_set": "F2",
  "bucket_probabilities": {"...": 0.0},
  "expected_temperature": 0.0,
  "central_interval": [0.0, 0.0],
  "top_buckets": ["..."],
  "analog_count": 0,
  "effective_analog_n": 0.0,
  "distance_summary": {"best": 0.0, "p50": 0.0, "floor": 0.0},
  "abstain": true,
  "recommendation": "ABSTAIN"
}
```

The distribution is the weighted empirical distribution of candidate settlement buckets, with only the declared training-fit smoothing prior. `expected_temperature` is descriptive; do not pretend it is a separate point forecast. The central interval is the narrowest declared probability interval over ordered buckets.

Recommendation labels are research labels only:

- `PRIMARY_BUCKET`: highest probability bucket if quality and concentration rules pass;
- `PRIMARY_PLUS_ADJACENT`: top bucket plus an adjacent bucket when mass is split and combined coverage is useful;
- `WATCH`: distribution is informative but too diffuse for a primary recommendation;
- `ABSTAIN`: insufficient effective analogs, poor distance quality, missing coverage, or excessive dispersion.

Suggested predeclared abstention diagnostics are `effective_analog_n < 10`, no candidate below the distance floor, or maximum bucket probability below 0.25; thresholds must be selected before test evaluation and may be stratified only through rolling validation. These labels are not orders, trade signals, overrides, or position-sizing instructions.

## 7. Sequential intraday updates

Replay each target day at fixed station-local cutoffs (for example 06:00, 09:00, 12:00, 15:00, and 18:00), subject to the station's actual observation coverage. At cutoff `t`, construct the query from observations with timestamp `<= t` only. Rerun the same DTW matcher against historical candidate sequences built at the corresponding historical cutoff; do not compare a partial query with full-day candidate epochs.

Every update must record:

- immutable target identity and `as_of` timestamp;
- the exact observation IDs and maximum observation timestamp used;
- sequence values and missingness mask;
- analog IDs, dates, distances, and weights;
- bucket distribution, entropy, CRPS/log loss when the outcome is eventually available;
- change from the previous cutoff (probability delta, bucket switch, interval width, and recommendation label).

Updates are recomputation from the larger as-of information set, not a Kalman update and not an unlogged accumulation of likelihoods. A previously emitted probability must never be revised in place. The replay evaluator checks timestamp monotonicity, no future observation IDs, no post-cutoff correction, and deterministic repeatability.

Sequential success is not defined as monotonic narrowing on every day. Evaluate average CRPS by lead/cutoff, calibration, interval coverage, entropy reduction, late bucket-switch frequency, and harmful early overconfidence. A stable `WATCH` or `ABSTAIN` is preferable to a falsely precise recommendation.

## 8. Leakage and validity risks

| Risk | Required control |
|---|---|
| Query uses a future METAR or completed-day max/min | As-of filter on observation timestamp; partial epoch for intraday replay |
| Candidate window overlaps query window | Window-level interval exclusion, not merely endpoint exclusion |
| Candidate outcome is unresolved at the historical origin | Apply settlement/outcome availability lag; exclude unresolved candidates |
| Normalization statistics include validation/test dates | Fit stats within each training fold; persist fold hash |
| Distance floor, K, band, or recency chosen on test | Rolling validation only; freeze manifest before test |
| Bucket schema changed over time | Version and join the historical market schema; test label mapping |
| Duplicate observations or METAR corrections | Stable observation ID, deterministic deduplication, correction policy frozen |
| Corrupted physical values dominate daily MAX/MIN | Bounds and QC before aggregation; audit rejected values |
| Wind direction treated as scalar | If admitted, use sine/cosine and a missing/calm mask |
| Cross-station pooling leaks climate/label information | Primary same-station analysis; predeclare pool and fit normalization on training only |
| Sequential replay uses final query sequence for every cutoff | Build a separate as-of sequence and candidate corpus for each cutoff |
| Multiple experiments create selection bias | Run manifest, fixed ablations, correction for repeated comparisons, untouched final test |
| Target-day settlement or market state enters features | Outcome is label only; market prices and late-day observations are excluded from this lane |

A data audit should explicitly count rows affected by each exclusion and publish a leakage audit alongside every score file.

## 9. Integration gates

No production/trading integration is authorized by this document. The following gates are required before any adapter is considered:

### Gate A — Data and deterministic replay

- T0 contract passes for every station/side/cutoff;
- no future observation/outcome IDs in replay audit;
- corrupted-value and duplicate audits pass;
- same input produces byte-identical prediction/artifact hashes;
- common-cohort and coverage accounting are complete.

### Gate B — Standalone predictive evidence

- frozen rolling-origin configuration and untouched test;
- CRPS improvement versus climatology and persistence/non-DTW baselines, with confidence intervals;
- no material calibration regression;
- station, season, and lead-time strata reported, including weak stations;
- analog distances and effective sample sizes support the claimed recommendations;
- recommendation/abstention coverage is acceptable and not achieved by silently dropping difficult rows.

### Gate C — Shadow-only operation

If Gate B passes, run as a read-only shadow informant. Store distributions and sequential traces without changing signal, order, risk, or sizing paths. Compare live data availability and replay parity. Any failure parks the lane.

### Gate D — Separate integration specification

A later proposal must define the exact interface, maximum probability influence, conflict handling, observability, rollback, and independent acceptance test. This trajectory lane may inform bucket selection, but it must not override the primary directional engine or become a blocking confirmation gate without a new, separately approved spec. There is no automatic path from this research document to trading.

Disposition is `ADVANCE` only when all gates pass, `PARK` when the method is scientifically valid but coverage/calibration is insufficient, and `KILL` when it fails the frozen method or cannot beat the declared baselines after leakage controls.

## 10. Accuracy-focused improvement suggestion

**Evaluate a phase-aware (slope-augmented) DTW variant as the first accuracy improvement.** Add the predeclared `A2` channel—temperature first difference between adjacent epochs—with a small fixed training-fold weight, while retaining temperature level, humidity, and pressure. The same DTW recurrence then matches both the state and whether the trajectory is warming, cooling, or turning; two sequences with similar temperatures but opposite recent movement no longer receive an artificially close match. This is especially relevant near frontal passages and bucket boundaries, where level-only analogs can recommend the wrong adjacent bucket.

Accept this improvement only if rolling-origin bucket CRPS improves against F2 on the common cohort, with no calibration/coverage regression and gains that persist across stations and lead times. The rationale is accuracy-focused rather than complexity-focused: it adds information about trajectory phase while preserving the required DTW method, and the acceptance rule prevents a coincidental test-period gain from becoming a new default.

## 11. Implementation boundary and artifact checklist

This review authorizes documentation and standalone experiment design only. It does not authorize edits to `core/`, schedulers, market integrations, alert paths, configuration, or trading behavior.

A compliant future experiment should emit:

- run manifest with code/data/schema hashes;
- fold boundaries and as-of policy;
- feature/ablation and channel-weight manifest;
- candidate eligibility and leakage audit;
- per-query DTW path and analog diagnostics;
- bucket distributions/recommendations and abstentions;
- rolling-origin metric tables and uncertainty intervals;
- sequential update traces;
- ADVANCE/PARK/KILL decision with reasons.

Relevant source documents reviewed: `TRAJECTORY-DTW-ROADMAP-2026-08-15.md`, `TRAJECTORY-LANE-DESIGN.md`, `FP-TRAJECTORY-MULTI-SIGNAL-SPEC.md`, `TRAJECTORY-CONFIRMATION-GATE-SPEC.md`, `GOLDILOCKS-TRAJECTORY-DESIGN-FRAME.md`, `scripts/a1_dtw_directional.py`, `scripts/a2_dtw_normalize.py`, `scripts/a3_dtw_matching.py`, `scripts/a5_dtw_scaletest.py`, `core/trajectory_lane.py`, and `core/trajectory_confirmation_gate.py`.
