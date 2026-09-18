# Temperature Model / Target-Geometry Tournament — Expert Review

**Date:** 2026-08-15 UTC  
**Status:** research/design specification only; no production, SignalRegistry, shadow-trading, trading, or execution change  
**Repository:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`

## 1. Decision summary

Run one predeclared, vintage-aware tournament to determine which forecast **representation** best predicts the official station-local daily HIGH and LOW outcomes. The tournament compares point, path-extreme, quantile/probability, calibrated-distribution, and trajectory-derived-extrema geometries under the same information barrier and key-matched outcome universe.

The tournament is not a license to select a profitable market strategy. It produces research artifacts and an `ADVANCE`, `PARK`, or `KILL` disposition for an accuracy arm only. No winner is wired into production without a new review.

**Hard source rule:** GEFS is eligible only when its issue/availability, valid-time, member, and encoding provenance gates pass. ECMWF IFS/TIGGE is eligible only under the same gates. A GEFS+ECMWF candidate is eligible only when both sources pass independently and are aligned on issue/cutoff, station/date/event keys, valid-window geometry, and revision semantics. A cycle label, database `target_date`, fetch time, or row order is not an issue timestamp.

## 2. Frozen target and information barrier

### 2.1 Outcome identity

The canonical outcome key is:

```text
(station, target_date_local, event_type ∈ {HIGH, LOW})
```

- `HIGH` is the official NWS settlement-station maximum for local calendar date `D`.
- `LOW` is the official NWS settlement-station minimum for local calendar date `D`.
- HIGH and LOW are separate targets, even on the same station/date.
- Preserve source units, raw value, report identity, revision/provenance, and rounding semantics.
- A Kalshi bucket is not a substitute for the NWS station label unless a separate equivalence audit proves it.

The frozen starting station universe is:

```text
KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW
KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO
```

The station registry must contain exact NWS/settlement identity, coordinates, timezone/DST rules, and valid-from/to metadata. Local-day UTC windows are derived from that registry, not from a universal UTC midnight boundary.

### 2.2 Cutoff, vintage, and lead

Primary comparison cutoff:

```text
cutoff_time_utc(D) = 12:00:00 UTC on calendar day D-1
```

A selected model vintage must prove:

```text
issue_time_utc        <= cutoff_time_utc(D)
availability_time_utc <= cutoff_time_utc(D)
```

Select the latest eligible vintage by actual issue time; use immutable revision/content hash as deterministic tie-breakers. Report issue time, availability time, cycle, first/last valid time, and lead to local-day start/end. `step=24` is a lead from a proven issue timestamp, not a target-date label.

The prediction-side information set may contain only source values and metadata available by the cutoff. Target-day observations, daily extremes, settlement outcomes, corrected labels, post-cutoff revisions, and target-day-derived regime values are label-only. A mutation test must show that changing a target-day outcome cannot change prediction rows, selected vintage, candidate eligibility, or fitted parameters.

### 2.3 Frozen temporal partitions

Use the existing research partition contract; do not move boundaries after inspecting results:

| Partition | Dates | Use |
|---|---|---|
| `FIT_HISTORY` | `2021-08-19..2025-12-31` | Rolling-origin candidate/tuning evaluation and training-only transforms |
| `CALIBRATION` | `2026-01-01..2026-02-28` | Final pre-holdout fit of already-selected calibration parameters |
| `LOCKED_HOLDOUT` | `2026-03-01..2026-08-13` | One untouched final comparison |

The 2026-08-14 archive date is outside the locked outcome window. If a source cannot prove vintage or label provenance for a row, exclude and count it; do not infer metadata.

## 3. Candidate registry (frozen before scoring)

The registry below is the complete primary tournament. Any additional candidate requires a new versioned registry and cannot be added after validation results are seen. Candidate IDs include source, event side, cutoff/lead stratum, and geometry; all parameter values are part of the manifest hash.

### 3.1 Mandatory controls

1. Station-side training climatology, fitted only on labels prior to each origin.
2. Strictly prior same-station/same-event persistence, where a valid prior label exists.
3. Raw source ensemble mean/median and raw source empirical CDF, where the source passes its provenance gate.

Controls are scored on the same eligible key set whenever possible. A candidate that wins only by having easier or fewer rows is not an accuracy win.

### 3.2 Geometry families

| Family | Candidate definition | Primary output |
|---|---|---|
| `POINT_STEP` | One model value at a predeclared valid step/anchor. Candidate steps are `{18, 24, 30, 36}` hours where available; valid-time-to-local-day mapping is exact. A point is not silently interpolated or substituted across steps. | Point forecast; optional degenerate distribution only as a separately named control |
| `VALID_PATH_EXTREME` | For HIGH, maximum; for LOW, minimum of valid forecast samples inside the station-local target-day window. No target-day observations. Native daily-extreme fields, if available, are separate candidates from sampled-path extrema. | Point extreme and, where members exist, an empirical distribution |
| `ENSEMBLE_QUANTILE` | Empirical member quantiles `q ∈ {0.10, 0.25, 0.50, 0.75, 0.90}` from the selected geometry; median is the primary point estimate. | Point quantile and central intervals |
| `MEMBER_FRACTION` | Weighted fraction of valid members satisfying a predeclared event: HIGH `y >= threshold`, LOW `y <= threshold`, or membership in the frozen settlement bucket. Threshold/bucket schema is fixed before scoring. | Event/bucket probabilities |
| `CALIBRATED_DISTRIBUTION` | Raw weighted empirical CDF passed through a training-only calibration method: identity/raw, station-side-lead affine location/spread correction, or monotone quantile mapping. The method is selected in rolling validation and fit only on prior labels. | Full continuous CDF, intervals, event probabilities |
| `TRAJECTORY_EXTREMA` | Match an as-of forecast/observation trajectory to eligible historical trajectories using the predeclared DTW grid; derive a weighted distribution of historical target HIGH/LOW extrema. Candidate analog outcomes must be resolved before the query origin and may not overlap the query sequence. | Point median/mean, full extrema distribution, interval |

`VALID_PATH_EXTREME` is direction-aware by construction: never use a maximum to score LOW or a minimum to score HIGH. `TRAJECTORY_EXTREMA` is a distinct family from the current trajectory lane; no existing runtime implementation is silently treated as this experiment.

### 3.3 Source/model arms

Each geometry is evaluated separately for each eligible source:

- **GEFS-only:** verified member IDs, expected count, decoding, issue/availability, and local valid-time geometry. Retain observed panel classes (including 6, 30, and 31) rather than padding or relabeling them.
- **ECMWF IFS/TIGGE-only:** verified origin/issue and availability, response/object lineage, member identity, encoding round-trip, and valid-time geometry. A stored `n_members=50` is not proof of identity or vintage.
- **GEFS+ECMWF balanced mixture:** only on the exact aligned common key set after both source gates pass. Default weights are 0.5 total mass per source and equal conditional weight among that source's valid members. A naive concatenation may be retained as a labeled diagnostic, but it cannot be the recommended combined winner because member count is not independent information.

Source-specific missing rows are counted separately from the common comparison cohort. If ECMWF fails, the combined lane is `BLOCKED`, not GEFS-only by fallback. If GEFS fails, no combined score is produced.

### 3.4 Trajectory parameter grid

To keep the trajectory family predeclared and bounded:

```text
sequence length: 5 daily epochs
DTW band radius: {0, 1, 2}
analog count K: {20, 40, 80}
recency half-life: {none, 90, 180 days}
feature ladder: {temperature; temperature+RH; temperature+RH+pressure}
```

Normalization is fit on the training side of each fold. Candidate sequences are same-station and same-event in the primary arm, temporally non-overlapping, and separated by the declared outcome-resolution lag. Analog weights, distance floor, effective analog sample size, and abstentions are retained. A trajectory candidate with no adequate analog support is unavailable, not filled with distant or future examples.

## 4. Required stratification and specialization

Every result is reported separately for:

1. `event_type`: HIGH versus LOW;
2. station;
3. lead/issue stratum and cutoff;
4. season: DJF, MAM, JJA, SON;
5. predeclared weather regime, if available at cutoff.

A regime label must be built from past-only data. If an HMM or similar model is used, it is fit/refit on prior training data and uses filtered/as-of probabilities, never a full-sample or future-smoothed state. Regime is diagnostic unless a separate calibration review approves it.

### 4.1 Hierarchical selection rule

Specialization is a nested, out-of-sample decision—not a permission to choose the best cell after looking at the holdout. Use this fallback hierarchy:

```text
pooled event × source × cutoff/lead
  → station × event × source × cutoff/lead
    → station × event × source × cutoff/lead × season
      → station × event × source × cutoff/lead × regime
```

At each node, compare the specialized candidate against its parent using rolling validation and the family-level correction. Keep specialization only when it clears the minimum support, improves the declared primary metric, and does not fail a predeclared calibration/coverage guardrail. Otherwise use the parent configuration. Do not specialize independently for every metric; the primary metric determines selection and secondary metrics are guardrails/diagnostics.

A station, direction, lead, season, or regime exception must be represented as a versioned rule in the pre-holdout selection artifact. No manual exceptions, post-hoc station removal, or holdout-driven source choice is permitted.

### 4.2 Minimum support and abstention

The following gates are frozen before scoring:

- Candidate/family tuning cell: at least **240 eligible target rows**, at least **4 rolling origins**, and at least **2 seasons**.
- Station specialization: at least **180 eligible rows** in the station-side cell and at least **4 origins**.
- Season specialization: at least **120 eligible rows** in the season cell and at least **30 rows in each represented season cell**.
- Regime specialization: at least **120 eligible rows**, at least **40 rows in each regime state used**, and at least **4 origins**. Unstable or newly appearing regimes fall back to the parent.
- Directional score: report only with at least **50 non-flat** prior-outcome cases; otherwise mark directional accuracy unsupported.
- Calibration/Brier/ECE: require at least **100 scored event cases** and at least **20 observations in each realized event class**; fixed bins with insufficient counts are merged only by the predeclared bin rule.
- Locked-holdout claim: at least **60 eligible rows** for a cell and at least **30 non-flat rows** for a directional claim; below this, publish coverage only and mark the accuracy claim unsupported.

A candidate may abstain for missing source geometry, insufficient effective member/analog support, or a distance-quality failure. Abstention rate and coverage are always reported; no candidate is rewarded for dropping difficult rows.

## 5. Rolling-origin selection protocol

### 5.1 Fold geometry

Within `FIT_HISTORY`, use expanding, chronological origins with a minimum 180-day training history, a 30-day validation window, and a 60-day evaluation window where the data supports it. Origins are daily or weekly on a fixed cadence. All source weights, normalization, bias corrections, quantile maps, calibration curves, thresholds, trajectory distances, K/band/recency values, and specialization decisions are fit using information strictly before the applicable validation window.

For each origin:

1. construct the eligible source/vintage manifest without labels;
2. fit candidate transformations on the training prefix;
3. generate all registered candidates for the validation window;
4. score on the validation labels only after predictions are frozen;
5. retain paired row IDs, coverage, and missingness;
6. select within the declared family and specialization hierarchy.

The final pre-holdout configuration is selected from the aggregate rolling validation results. Then fit already-selected calibration parameters through `2026-02-28` using `FIT_HISTORY + CALIBRATION`; do not use locked-holdout labels for any fitting or selection. The locked holdout is scored exactly once with the frozen configuration.

### 5.2 Primary metrics by track

Do not collapse fundamentally different point and probabilistic outputs into an improvised single score:

- **Point track:** MAE is primary; RMSE and signed bias are secondary. Median/90th-percentile absolute error are diagnostics.
- **Distribution track:** CRPS is primary; Brier/ECE, interval coverage/width, and interval score are guardrails and diagnostics.
- **Direction track:** directional accuracy is secondary and never replaces continuous accuracy. Use the predeclared `0.5°F` flat band and strictly prior same-event outcome.

If a distribution supplies a point estimate, use its predeclared median (primary) or mean (sensitivity) consistently across all candidates. No point estimate may be chosen after seeing a holdout result.

### 5.3 Family-level multiple testing

The primary families are `POINT_STEP`, `VALID_PATH_EXTREME`, `ENSEMBLE_QUANTILE`, `MEMBER_FRACTION`, `CALIBRATED_DISTRIBUTION`, and `TRAJECTORY_EXTREMA`. Source, station, direction, lead, season, and regime variants are hypotheses within a family, not independent chances to declare a winner.

For each family:

1. use paired target-date residuals on the common eligible rows;
2. block resampling by target date (all stations together when pooled; station-date blocks for station-specific cells) to preserve serial and cross-station dependence;
3. calculate family-wide simultaneous confidence intervals using a max-statistic/bootstrap adjustment, or a predeclared Benjamini-Hochberg FDR procedure for directional discovery claims;
4. apply the same correction to all family candidates and specializations considered at that selection stage;
5. report the number of candidates tested, effective rows, missingness, adjusted p-values/confidence intervals, and the registry hash.

A family winner must beat its relevant control on the primary metric with an adjusted 95% interval excluding material degradation (the practical margin is frozen before scoring), satisfy support and coverage guardrails, and remain stable across the required strata. If no candidate clears the gate, the family is `PARK` or `KILL`, not a forced winner.

## 6. Metrics and reporting contract

All metrics are reported pooled and by HIGH/LOW, station, lead/cutoff, season, regime, source, member-count/effective-N stratum, and common versus source-specific cohort.

### 6.1 Continuous and directional

- **MAE (°F):** primary point error.
- **RMSE (°F):** tail-sensitive secondary error.
- **Signed bias (°F):** warm/cool or high/low systematic error.
- **Directional accuracy:** define `up` when forecast change from the strictly prior same-station/same-event outcome exceeds `+0.5°F`, `down` below `-0.5°F`, and `flat` otherwise. Report three-class accuracy, non-flat binary accuracy, coverage, confusion matrix, and flat count. No target-day outcome enters the forecast.

Use paired date-block uncertainty intervals and report denominators. Never report an empty or blocked metric as zero skill.

### 6.2 Probabilistic and interval

- **CRPS:** weighted empirical CDF or calibrated continuous distribution; preserve member/analog weights and effective N.
- **Brier score:** only for a fixed, predeclared event such as `HIGH >= threshold`, `LOW <= threshold`, or a frozen bucket event. The threshold and bucket semantics cannot vary after results are inspected.
- **ECE/reliability:** fixed probability bins and reported bin counts; merge sparse bins only under a frozen rule. Report reliability diagrams or tabular reliability when sample support permits.
- **Intervals:** nominal 50%, 80%, and 90% coverage, mean width, and interval score. Report conditional coverage by station, side, season, regime, and lead; widening intervals to obtain coverage must show the width tradeoff.

Report top-bucket accuracy only as a diagnostic for bucket outputs. P&L, fills, edge, and trading returns are explicitly out of scope.

## 7. Source and alignment gates

Before a candidate receives a score, its source must pass all applicable gates:

1. immutable input/source/query/code hashes;
2. exact issue and availability timestamps at or before cutoff;
3. exact station-local valid-time window and lead mapping;
4. unique member identity, verified expected count, valid encoding, and no silent imputation;
5. no duplicate conflicting revisions;
6. no target-day or post-cutoff leakage;
7. deterministic rerun and stable row/artifact hashes.

The GEFS-only arm may be reported as `BLOCKED` if its current archive cannot prove dated issue/availability despite a cycle label. ECMWF remains `BLOCKED` until TIGGE origin and availability lineage and blob encoding are proven. The combined arm remains `BLOCKED` unless both arms pass and the common station/date/event/vintage/valid-window keys are exact. These are provenance dispositions, not negative accuracy results.

## 8. Artifacts and disposition

The research runner must produce, without mutating production data:

- frozen candidate registry and SHA-256 hash;
- source/station/outcome lineage and input hashes;
- row-level eligibility/exclusion manifest with machine-readable reasons;
- fold/origin manifest and training/validation/holdout boundaries;
- candidate predictions before labels are joined;
- metric tables with denominators, uncertainty intervals, multiple-testing adjustment, and strata;
- specialization/fallback tree and support counts;
- deterministic rerun hash and leakage mutation-test result;
- a family disposition: `ADVANCE`, `PARK`, or `KILL`.

`ADVANCE` means accuracy research may continue under a separate review. `PARK` means recoverable provenance or support limitations prevent a fair conclusion. `KILL` means the candidate contract is disproven, unstable, or materially inferior after correction. No disposition changes runtime behavior.

## 9. Accuracy-focused suggestion

**First accuracy intervention after provenance repair: station × event-type (HIGH/LOW) × lead rolling bias correction with spread-preserving calibration.** Fit the correction only on labels strictly before each rolling origin, shrink sparse station/side cells toward a pooled station-season prior, and apply the location correction to the distribution/ensemble without collapsing its spread. Re-evaluate MAE/RMSE/bias, CRPS, and interval coverage against the raw candidate on the locked holdout.

This is the highest-leverage first test because airport/grid forecasts commonly carry persistent local-extreme offsets from elevation, coastal exposure, urban heat, and diurnal timing. Geometry selection can choose a better representation, and source balancing can prevent nominal member-count overweighting, but neither reliably removes a stable station/side bias. Spread-preserving calibration addresses that bias without manufacturing false certainty or leaking target labels.

## 10. Non-claims

This document does not establish GEFS or ECMWF superiority, NWS/Kalshi equivalence, profitable trading edge, or authorization to modify production signals. If a provenance, support, alignment, or leakage gate fails, the result is **[blocked]**, not an accuracy verdict.
