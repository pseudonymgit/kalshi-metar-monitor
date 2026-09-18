# Calibrated-vs-uncalibrated `pressure_delta` reliability rerun specification — 2026-08-15

## Status and authorization

**Prepared, not run. Controlled research lane only.** This document preregisters a paired reliability comparison of `pressure_delta` with calibration disabled versus the same signal with the explicit `historical_kalshi` calibration-label path. It authorizes neither execution in this session nor any sweep, threshold tuning, signal/config selection, production enablement, deployment, paper trading, shadow trading, live trading, capital action, or scheduler change.

The only intended execution is a later deterministic script run after the preflight gates below pass. The artifact must say **not run** until that happens.

## Evidence used to freeze this lane

### Verified calibration temporal-separation receipt

Source receipt: `.meta/continuity/weather-engine/2026-08-15-calibration-temporal-separation.md`.

- Calibration cutoff: **2026-02-28 inclusive**.
- Historical labels are explicitly keyed by `(station, target_date, market_direction)` where `market_direction` is `HIGH` or `LOW`.
- Historical labels are supplied separately through `calibration_label_source="historical_kalshi"`; the evaluator default remains `disabled`.
- Holdout/scored labels are never fit.
- Features use the latest METAR daily row with date strictly earlier than the target date.
- Audit counts: `rows_read=3512`, `unique_event_keys=3511`, `valid_event_keys=3510`, `ambiguous_event_keys=1`, `labels_emitted=3172`.
- The excluded ambiguous identity is `KMDW / 2024-02-28 / HIGH`; no arbitrary first/latest row was selected.
- Historical-label SHA-256: `da101fd2f38a32083747161cae44888e652661bcf38418ead01c1239a097d27d`.
- The receipt proves the opt-in label path and temporal separation; it does **not** represent a completed calibration rerun.

Kalshi is the explicit historical calibration source required by this lane, but it is not treated as an independently reacquired NWS truth source. The independent-label-source receipt remains blocked/parked. This limitation must be carried into the final result.

### Current `pressure_delta` reliability artifact

Source artifact: `data/pressure_delta_reliability_review_20260815.json`.

- Artifact status: `PARK`, because the disabled-calibration current and extension CP95 lower bounds were below the preregistered 55% floor.
- Existing arm: `pressure_delta` alone with `calibration_label_source="disabled"`; no comparator was evaluated.
- Current holdout: **2026-05-15 through 2026-08-13 inclusive**.
- Predeclared extension: **2026-03-01 through 2026-05-14 inclusive**.
- Canonical stations: `KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO`.
- Existing current eligible count/key hash: `1556` / `dfc1ac4070210474a00987efb39830c4d71eb6cc43fcace257848f54bc5302d1`.
- Existing extension eligible count/key hash: `1004` / `fd270b5459fe06d821d802b0b8af8ce2d92ea82413ec771232ed14186ab7b4d7`.
- Artifact SHA-256: `78bf99f46bf80addcb32d75e8d4c29ee46c7c6ff555ca94e78ccb90f3d25c05d`.

The prior artifact is a continuity/baseline receipt only. The rerun must reconstruct and hash its own explicit event-key universe and must not silently reuse a station/date-only population if HIGH/LOW event identity cannot be preserved.

## Objective

Measure whether the explicit historical-Kalshi calibration path improves probability reliability for `pressure_delta` without changing the signal, station/date/event population, temporal geometry, holdout windows, or evaluator inputs.

The comparison has two frozen arms:

- **Arm U — uncalibrated:** `calibration_label_source="disabled"`; no calibration fit.
- **Arm C — calibrated:** `calibration_label_source="historical_kalshi"`, supplied only with the prevalidated historical labels, fit using target dates `<= 2026-02-28`.

Arm C is the only difference. Both arms use fresh, identically configured fuser instances. Calibration state must never be shared from one arm into the other. The same raw signal outputs, signal list, evaluator, fusion method, confidence floor, epsilon, station set, dates, and fold geometry are used in both arms.

This is a reliability comparison, not a search for a better operating threshold. No parameter, station, period, stratum, bin, or result may be selected after inspection.

## Frozen evaluator and data contract

### Signal and evaluator

- Signal list exactly: `["pressure_delta"]`.
- Evaluator: `scripts/ws5_common.py::compute_fusion_accuracy` semantics.
- Fusion: `llop`.
- Fold: `fold_4_holdout` (record the exact evaluator fold identifier in every output row).
- Calibration method label: `isotonic`.
- Calibration cutoff: `2026-02-28` inclusive and never later.
- Raw signal confidence floor: `0.50`.
- Ground-truth flat epsilon: `0.5°F`; flat events are excluded, not relabeled.
- Prior-METAR rule: for target date `d`, use only the latest daily METAR row with date `< d`; a target-day METAR row is forbidden.
- No network, model, scheduler, trading, production-config, or live API calls.
- No source/config mutation other than the designated research artifact output.

### Explicit event universe

The rerun must construct one sealed eligible key set before evaluating either arm. The canonical identity is:

`(station, target_date, market_direction)`

where `market_direction` is explicitly `HIGH` or `LOW` from the Kalshi series/event identity. HIGH and LOW rows for the same station/date are distinct events and must never be collapsed to `(station, target_date)`.

A row is eligible only when all of the following hold:

1. Station is one of the exact 20 canonical stations above.
2. Target date lies in exactly one of the two frozen holdout windows.
3. The explicit HIGH/LOW event identity is reconstructable from the settlement source.
4. There is exactly one non-conflicting event value for that `(station, target_date, market_direction)` identity. Exact repeated copies may be coalesced deterministically; conflicting values or multiple event tickers are ambiguous and must be excluded or hard-fail according to the predeclared loader contract, never chosen by insertion order.
5. A strictly prior same-station settlement exists for the event-direction comparison, and the directional outcome is non-flat at epsilon `0.5°F`.
6. A strictly prior METAR daily snapshot exists.

The event-direction label is `up` when the current event-direction settlement minus its strictly prior same-station, same-`HIGH`/`LOW` settled event is at least `+0.5°F`, `down` when at most `-0.5°F`, and otherwise flat/excluded. The target-day settlement is used to form the scored outcome only. It is never passed to feature evaluation or calibration fitting for that scored row.

If the repository evaluator can only emit station/date rows, the harness must retain the explicit event key alongside each emitted row and prove that no duplicated HIGH/LOW event is being collapsed. If that proof cannot be made, stop before scoring.

### Shared universe and paired rows

Both arms receive the exact same sealed event-key list, source snapshot, station list, holdout bounds, prior-settlement rule, prior-METAR snapshot, and label. Every eligible event must produce one row per arm, including explicit abstentions. No row may be dropped because one arm has a less favorable outcome.

Required row identity and frozen fields:

- `station`
- `target_date`
- `market_direction` (`HIGH` or `LOW`)
- stable event ticker/identity provenance where available
- `label` (`up` or `down`)
- `period` (`current_holdout` or `extension`)
- `season` (`DJF`, `MAM`, `JJA`, or `SON`)
- `arm` (`uncalibrated` or `calibrated`)
- evaluator/fusion/fold/config identifiers
- `fired`, `prediction`, `confidence`, `correct`, and deterministic rejection reason

The arm key sets must be exactly equal, and the two arms' eligible key hashes must be byte-for-byte identical. A calibrated/uncalibrated difference in fired status, confidence, or fused prediction is an observed result, not permission to redefine the universe.

## Calibration fit geometry

### Arm U

Run with `calibration_label_source="disabled"`. No `fit_calibration` call is permitted. `calibration_cutoff_date` may remain recorded as `2026-02-28` for provenance, but it must not cause a fit in this arm.

### Arm C

Run with `calibration_label_source="historical_kalshi"` and the exact prevalidated labels from the calibration receipt. The label loader must preserve `(station, target_date, HIGH|LOW)` identity and reject duplicate/conflicting identities. Fit only labels with `target_date <= 2026-02-28`; reject any label overlapping either holdout or later than the cutoff.

For every holdout event date `d`, all fit labels must have target date `< d`. The scored target event and all holdout events are excluded from fitting, even if a source row is available. The fit pass uses only history-only signal evaluations at strictly prior METAR indices. The historical-label set, fit counts, label hash, calibrator state summary, and cutoff must be recorded.

The harness must instantiate Arm U and Arm C independently and must verify that the uncalibrated arm has no fitted calibration state. A failed or empty historical fit is a stop condition, not a reason to fall back to settlement-derived labels or silently run the calibrated arm as uncalibrated.

## Predeclared endpoints

### Primary paired reliability endpoint

On the common fired event keys where both arms emit a prediction and confidence, compare:

1. Brier score for correctness probability: `y=1` when that arm's prediction equals the event label, otherwise `y=0`; score `(y-confidence)^2`.
2. Expected calibration error (ECE), using the fixed ten equal-width bins `[0.00,0.10) ... [0.90,1.00]`, weighted by the common fired-row count. Use observed mean correctness and observed mean confidence within each bin; empty bins are reported as empty and contribute zero.

Report Arm U, Arm C, delta (C minus U), common fired `n`, and the exact row-key hash used for the endpoint. Lower Brier and lower ECE are better. This endpoint is descriptive research evidence; no single favorable metric authorizes promotion.

If a fused confidence is unavailable for an abstention, it is not imputed. Abstentions are handled through coverage and the arm-specific/paired population definitions below.

### Secondary reliability and accuracy outputs

For each period and aggregate across both periods, report for each arm:

- eligible event count, fired count, abstain count, coverage (`fired / eligible`),
- correct count, fired accuracy, exact two-sided 95% Clopper–Pearson interval (CP95) on fired correctness,
- Brier and ECE on the common fired population, plus arm-specific fired diagnostics clearly labeled as such,
- confidence-bin count, accuracy, mean confidence, and mean correctness for:
  - `<0.50` (under-floor diagnostic bucket),
  - `[0.50,0.60)`, `[0.60,0.70)`, `[0.70,0.80)`, `[0.80,0.90)`, `[0.90,1.00]`,
- rejection/abstention reason counts,
- directional balance (`up`/`down`) and fired correctness by direction.

The required strata are:

1. each of the exact 20 stations;
2. `market_direction=HIGH` and `market_direction=LOW`;
3. the cross-strata `(station, HIGH|LOW)` where support exists;
4. current holdout and extension separately.

Every stratum must include `eligible`, `fired`, `coverage`, `correct`, `accuracy`, CP95, Brier/ECE sample counts, confidence-bin rows, and missing/unsupported reason counts. Sparse strata are reported with their true counts and wide/undefined intervals where mathematically required; they are not pooled, dropped, or used to select a favorable conclusion.

Also report paired accuracy on event keys where both arms fired, including Arm U correct, Arm C correct, delta in percentage points, and exact discordance counts (`U correct/C incorrect`, `U incorrect/C correct`). An exact two-sided McNemar result may be reported as a descriptive paired diagnostic, but it is not a promotion gate and must not be used to search for a favorable subset.

### Coverage and confidence interpretation

Coverage is `fired / eligible` and must be reported independently for each arm and stratum. The calibrated arm may alter fused confidence or fused abstention behavior; that is an outcome to report, not a reason to change the floor or remove rows. No confidence threshold is tuned during or after this rerun.

Confidence bins must partition every scored fired confidence. If a value is outside `[0,1]`, or is missing on a fired row, stop. The `<0.50` bucket is required because the prior artifact contains fused confidence values below the raw signal floor; it prevents silent loss of rows while retaining the preregistered 0.50-and-above bins.

## Integrity, mutation, and determinism gates

Before scoring either arm, the harness must verify:

- exact 20-station list and exact two holdout windows;
- calibration receipt/source hash, cutoff, label count, explicit HIGH/LOW identity, and ambiguous-key policy;
- sealed event-key equality and key hash for both arms;
- no target-day settlement/label enters signal evaluation or calibration fitting;
- strictly prior METAR index for every eligible event;
- Arm U performs no fit; Arm C fits only explicit pre-cutoff `historical_kalshi` labels;
- fresh fuser/calibration state for each arm;
- identical evaluator/fusion/signal/fold/data geometry;
- one row per `(arm, station, target_date, market_direction)` with no duplicates;
- all eligible rows represented, including abstentions;
- no network/model/scheduler/trading/production calls;
- deterministic JSON serialization and stable row ordering.

The controlled run must be executed twice with the identical command and inputs. JSON bytes and SHA-256 must match. The receipt must include the exact commands, source/data/script/spec hashes, event counts and key hashes, fit counts, per-arm results, strata, stop-gate outcomes, and limitations.

A targeted mutation test must be available before execution:

- mutate a historical pre-cutoff label: only Arm C fit state/calibration output may change as expected;
- mutate a scored target/holdout settlement: predictions, confidence, firing, and fit state must not change; only scored correctness/label attachment may change;
- mutate target-day METAR: strictly prior feature snapshot and predictions must not change;
- rerun identical inputs: all rows, metrics, and hashes must be identical.

## Stop criteria

Stop immediately and report **BLOCKED**, with no result interpretation, if any of the following occurs:

1. explicit HIGH/LOW event identity cannot be reconstructed, or duplicate/conflicting event identities are resolved arbitrarily;
2. the two arms do not use exactly the same event universe, station/date windows, label, prior-settlement rule, prior-METAR snapshot, signal list, evaluator, fold, or fusion configuration;
3. any target-day/holdout settlement, label, or METAR row is used in feature evaluation or calibration fitting;
4. Arm C uses a label outside `2026-02-28`, overlaps holdout, lacks explicit `historical_kalshi` provenance, or silently falls back to disabled/settlement-derived fitting;
5. Arm U fits calibration or Arm C reuses Arm U state (or vice versa);
6. any required key, confidence, prediction, label, or rejection field is missing, duplicated, non-deterministic, or outside its declared domain;
7. CP95, Brier, ECE, coverage, or bin calculations cannot be reproduced from retained paired rows;
8. repeated identical runs differ in output bytes or SHA-256;
9. any network, model, scheduler, trading, paper/shadow/live, production, or configuration side effect occurs;
10. any attempt is made to tune a threshold, select stations/periods/strata, sweep signals/parameters, or promote based on the result.

A low accuracy, low coverage, CP95 failure, calibration non-improvement, sparse stratum, or ambiguous scientific result is **PARK**, not KILL, provided all integrity gates pass. An integrity, leakage, identity, or determinism failure is **KILL/BLOCKED** for this lane.

## Decision language

The only permitted conclusions are:

- **CALIBRATION RELIABILITY IMPROVED (research evidence):** Arm C improves the preregistered paired Brier/ECE endpoint without an integrity failure, while reporting all accuracy/coverage tradeoffs. This does not authorize production use.
- **NO RELIABILITY IMPROVEMENT / PARK:** Arm C does not improve the preregistered endpoint, is ambiguous, or has unacceptable coverage/accuracy tradeoffs despite valid execution.
- **KILL/BLOCKED:** any stop criterion fires.

The prior 55% CP95 lower-bound criterion remains descriptive context from the disabled-calibration reliability review. It must not be converted into a tuned gate for this comparison. No result can override the existing independent-label-source limitation or authorize production enablement.

## Accuracy-focused suggestion

Add a predeclared **direction-preservation and probability-resolution diagnostic**: for each common event key, retain the raw `pressure_delta` direction/confidence, calibrated confidence, and final fused direction/confidence, then report whether calibration changes probability resolution without changing raw directional evidence. This directly separates a calibration accuracy gain from a hidden direction/coverage change, and it can expose the prior quantized-confidence limitation without tuning thresholds or selecting a favorable subset.
