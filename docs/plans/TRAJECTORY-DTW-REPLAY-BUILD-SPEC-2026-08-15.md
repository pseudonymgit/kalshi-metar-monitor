# Standalone Sequential DTW Replay Build Spec

Date: 2026-08-15 UTC  
Status: frozen small research fixture; no production/trading wiring

## 1. Scope and stop conditions

This build evaluates only the original multivariate **dynamic time warping (DTW)** trajectory method. It is standalone, deterministic, offline, and consumes the existing `data/metar_backfill.db` tables `metar_observations` and `settlement_epochs`. It does not call a network or model, and it does not import or modify production/trading paths. The current Euclidean live trajectory gate is not evidence.

Stop and report BLOCKED rather than fabricate data if causal observation timestamps, epoch identity, or settlement bucket identity cannot be proven. No k-NN, Euclidean nearest-vector, Kalman, HMM, regression, or hidden fallback is permitted.

## 2. Frozen fixture

- Station: `KATL` only; market: `HIGH` only.
- Query target dates: `2025-08-25`, `2025-08-26`, `2025-08-27`.
- Intraday cutoffs: `06:00:00Z`, `12:00:00Z`, `18:00:00Z`, `23:00:00Z` on each target date.
- Sequence length: five contiguous UTC calendar-day epochs, `[target_date - 4 days, ..., target_date]`.
- Historical universe: `settlement_epochs` rows with `market_type=HIGH`, `epoch_status=closed`, `terminal_state_reached=1`, a finite integer `settlement_bucket`, and complete feature sequence. The candidate target date must be at least five calendar days before the query target date (equivalently, candidate final epoch is before the query window starts with a one-day completed-outcome boundary).
- Minimum analog floor: 3 for this small fixture only. This is a coverage concession for a deterministic smoke fixture, not a validation threshold or production recommendation.
- Maximum reported analogs: 20, selected after DTW ranking.
- No random sampling, calibration, hyperparameter tuning, or full historical experiment.

The fixture is intentionally small enough to rerun quickly while retaining several successive query cutoffs and historical outcomes.

## 3. Identity, clocks, and causal cutoff

A source observation identity is `(station, date_utc, timestamp_utc)`, backed by the source table's unique key. A settlement identity is `(station, market_type, local_trading_date)` from `settlement_epochs`; duplicate identities are rejected rather than weighted twice.

For a query `(station, target_date, cutoff)`, an observation is eligible only when `timestamp_utc <= cutoff` (UTC). Prior days in the five-day query window therefore use only their already-completed observations; the target day uses the partial intraday prefix. Each daily feature epoch records its source observation IDs and latest eligible timestamp. Missing calendar days or missing required features reject the query; no future interpolation/fill is allowed.

The source `settlement_timestamp_utc` in this database is a backfill-generation timestamp, not a historical availability timestamp. It is not used as evidence of causal availability. For this fixture, a closed terminal settlement row is treated as resolved at the end of its labeled UTC target day, and the five-day date embargo ensures that candidate settlement completion precedes every query origin. This limitation is recorded in the receipt and prevents promotion beyond a fixture replay.

A candidate with target date on/after the query window start is explicitly rejected as `FUTURE_OR_OVERLAPPING_CANDIDATE`; a candidate with target date on/after the query target is additionally rejected as `FUTURE_TARGET_SETTLEMENT`. The query's own settlement bucket is never read while constructing a prediction.

## 4. Bucket identity and output

The target bucket is the exact integer `settlement_epochs.settlement_bucket`, documented by `generate_settlement_epochs.py` as rounded Fahrenheit HIGH temperature. No neighboring integer buckets are merged. The frozen output universe is integer labels `-50` through `130` (physical Fahrenheit schema range), including zero-probability buckets; this avoids deriving the probability schema from future/test outcomes.

Each canonical prediction row contains, at minimum:

- `event_id`, `station`, `market_type`, `target_date`, `as_of_epoch`, `cutoff`, `feature_set`, `sequence_dates`, and source sequence hash;
- full `bucket_probabilities`, `top_buckets`, `expected_range`, `confidence`, and `abstained`/`abstention_reason`;
- analog IDs, DTW distances, analog count, eligible/rejected counts, and the actual target bucket only in a separate scored-label field after prediction construction;
- `updated_at` equal to the cutoff (not wall-clock time), algorithm/configuration version, and canonical row hash.

Predictions are keyed by `(event_id, cutoff, feature_set)`, with one canonical row per key. Stable ordering is by target date, cutoff, and feature-set order.

## 5. Feature ladder and scaling

Three predeclared feature sets are emitted independently:

1. `temp_only`: causal daily maximum temperature (`temp_max_f`);
2. `temp_humidity`: temperature plus causal relative humidity (`rh_pct`), computed from observed temperature/dewpoint with the fixed Magnus formula;
3. `temp_humidity_pressure`: the preceding two plus causal mean pressure (`pressure_mb`).

Rows lacking a required feature are rejected for that feature set; dewpoint is not silently relabeled as RH. Physical bounds are fixed before the run: temperature/dewpoint `[-50,130] F`, RH `[0,100]`, pressure `[800,1100] mb`. Daily aggregation is `max(temp_f)` and `mean` for RH/pressure over eligible observations. No future fill is permitted.

For each query/cutoff/feature set, location and scale are fitted from eligible historical candidate sequence values only (past-only, query-specific fixture rule), using mean and population standard deviation; zero scales become `1.0`. The same transform is applied to query and candidates. This is a fixed causal transform, not independent sequence normalization.

## 6. DTW recurrence and deterministic ranking

For scaled feature vectors `x_i`, `y_j`, local cost is weighted Euclidean distance:

```text
cost(i,j) = sqrt(sum_k w_k * (x_i[k] - y_j[k])^2)
```

Weights are equal within each selected feature set and sum to one. Exact DTW uses a Sakoe-Chiba radius of `1`:

```text
D(i,j) = cost(i,j) + min(D(i-1,j), D(i,j-1), D(i-1,j-1))
DTW(x,y) = D(n,m) / path_length
```

The boundary is `D(0,0)=0`, all other boundary cells infinity. Path length is tracked along the chosen minimum-cost path; ties use diagonal, up, then left precedence. No unconstrained retry or alternate distance is allowed. Analogs sort by `(distance, candidate_id)` where `candidate_id=station|market_type|target_date`.

Analog probabilities are uniform over the top reported analogs. Every declared bucket is normalized from counts; equal-distance ties are stable. Expected range is the 10th-to-90th weighted analog bucket interval, and confidence is the highest bucket probability only when not abstaining.

## 7. Abstention and rejection

Return `ABSTAIN_INSUFFICIENT_ANALOGS` when fewer than three eligible, complete candidates remain. Return `ABSTAIN_MISSING_QUERY_EPOCH` for a missing required query epoch/feature. Leakage, duplicate identity, non-finite distance, or conflicting sequential identity is a hard QC error, never a fallback. Abstained rows retain a null probability distribution and explicit reason.

## 8. Sequential update identity/idempotence

Replay state is append-only and keyed by `(event_id, cutoff, feature_set)`. Reapplying the identical canonical row is a no-op and preserves the state hash. A different payload for an existing key raises `REVISION_ERROR`; it is not silently overwritten. State hashes are SHA-256 over canonical sorted JSON bytes. The prediction sequence is therefore reproducible from the same ordered event stream and frozen inputs.

## 9. Determinism and acceptance

Canonical JSON is UTF-8, sorted keys, compact separators, fixed six-decimal float serialization, and no NaN/Infinity. The fixture runner writes `data/trajectory_dtw_replay_fixture_20260815.json` and its SHA-256. A rerun with identical source bytes, code, configuration, and runtime must be byte-identical. Focused tests must cover: known DTW recurrence/path, causal candidate eligibility, no target-day leakage, duplicate key rejection, sequential idempotence/conflicting revisions, probability normalization, abstention, deterministic ties, and byte-identical fixture rerun.

The fixture is accepted only if `py_compile`, focused pytest, causal/leakage assertions, all three feature sets, canonical output/hash, and a second rerun byte comparison pass. This is a deterministic implementation gate, not an accuracy promotion gate.

## 10. Known limitation and accuracy suggestion

Because the available `settlement_epochs.settlement_timestamp_utc` values are backfill timestamps rather than historical publication timestamps, this fixture proves date-level causal separation but not original ingestion-time availability. Accuracy-focused next step: add a causal current-level plus first-difference feature ablation, selected only inside a rolling tune block and scored with CRPS; do not run it in this frozen build.
