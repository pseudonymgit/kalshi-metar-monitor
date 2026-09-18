# Pressure-Delta Matched Comparison Specification — 2026-08-15

**Status:** preregistered research-only comparison; no promotion, deployment, shadow trading, paper-trading promotion, or live capital.

## Objective and population

Compare `pressure_delta` alone (Arm A) with the locked pair `pressure_delta + seasonal_regime` (Arm B) on the identical sealed 20-station holdout, station/date cell by station/date cell. Population is all canonical stations `KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO` and target dates inclusive **2026-05-15 through 2026-08-13** for which: (1) a deduplicated Kalshi settlement exists, (2) the prior chronological settlement exists, (3) the directional label is non-flat under epsilon 0.5°F, and (4) the same station's daily METAR aggregate exists. The eligible cell set is constructed once and is identical for both arms.

The label is `up` when today's settlement temperature minus yesterday's is >= 0.5°F, `down` when <= -0.5°F; cells inside (-0.5, 0.5) are excluded, not relabeled. Settlement data are labels only and are never supplied to feature evaluation.

## Signal configurations and evaluator geometry

Both arms use the same evaluator, same deduplicated settlement loader, same daily METAR loader, same 20 stations, same holdout, same epsilon (0.5°F), raw confidence floor (0.50), LLOP fusion, Dempster-Shafer conflict enabled, diversity modifier enabled, and isotonic calibration. Arm A signal list is exactly `[pressure_delta]`. Arm B is exactly `[pressure_delta, seasonal_regime]`. Configuration/version: `matched-comparison-v1`, evaluator `scripts/ws5_common.py::compute_fusion_accuracy` semantics, fold assignment `fold_4_holdout`, calibration cutoff `2026-02-28`; all training dates are <= cutoff and all scored dates are holdout dates. No signal selection, threshold tuning, or production-code/config changes are allowed.

Signal features are history-only. `pressure_delta` reads prior pressure observations; `seasonal_regime` uses the prior-day temperature/climatology path. Target-day settlement/label data may determine the evaluation label only, never features or calibration inputs for a scored row.

## Trade inclusion, matching, and abstentions

One output row is required for every eligible station/date **in each arm**, including an explicit `fired=false` abstention. A fired row has a direction prediction and confidence; an abstention has `prediction=null`, `confidence=null`, and a reason (`no_signal`, `fusion_none`, or equivalent deterministic reason). Matching is an inner join on the exact `(station, date)` identity, after asserting both arms have the same eligible-cell keys. No date/station may be dropped from one arm to improve comparability. Primary matched-population analysis includes only cells where both arms fired; cells where either arm abstained are reported as coverage context and excluded from the paired accuracy endpoint. No imputation is permitted.

Each row contains station, date, label, prediction, fired/abstain, confidence, signal/config version, and fold assignment, plus deterministic correctness/rejection fields. Duplicate station/date rows or differing evaluator/data/fold geometry are hard failures.

## Endpoints and statistical test

**Primary endpoint:** paired accuracy difference on the matched fired population, Arm B minus Arm A. Report matched n, Arm A correct, Arm B correct, accuracy for each, delta in percentage points, and McNemar discordant cells:
- `b`: Arm A correct / Arm B incorrect
- `c`: Arm A incorrect / Arm B correct

Use the exact two-sided McNemar binomial test with n=`b+c`, success probability 0.5; if `b+c=0`, p=1.0. This is one preregistered primary comparison. Report exact two-sided 95% Clopper–Pearson intervals for each matched accuracy and a deterministic paired bootstrap 95% CI for the delta (station/date rows sampled with replacement, seed 20260815, 10,000 replicates; if matched n=0, fail rather than fabricate a CI).

Also report all-eligible coverage for each arm (`fired / eligible`) and fired counts. Coverage is contextual and not a secondary promotion claim; the prior coverage confound is the reason for the matched endpoint.

## Multiple testing and promotion gate

There is exactly one primary comparison (Arm B versus Arm A), so no family-wide multiplicity adjustment is applied beyond declaring all other analyses descriptive. Do not scan additional signals, thresholds, folds, populations, or endpoints and select a favorable result. The **58% promotion gate** is descriptive and not authorization: promotion would require the preregistered next review to show the selected arm's matched accuracy >=58% and pass all existing safety/promotion controls. This run itself cannot promote anything.

## Stop conditions

Stop and report blocked if per-trade identity cannot be reconstructed for both arms on the same station/date cells; features access target-day settlement/label data; evaluator, configuration, fold, station list, data source, or holdout differs; required settlement/METAR data are missing in a way that prevents a shared eligible set; deterministic rerun differs; or any AI/model/network/live-trading call occurs. Stop before any production, configuration, shadow/paper promotion, deployment, or capital action.

## QC checklist (literal gates)

- [ ] **No label peek:** static/source and runtime audit show target-day settlement/label is read only to form `label`, never passed into feature evaluation or calibration fitting for holdout rows.
- [ ] **Same evaluator/data/stations/holdout:** both arms use the identical loader, evaluator geometry, DB paths, canonical 20 stations, date bounds, epsilon, calibration cutoff, fold assignment, and LLOP options.
- [ ] **Per-trade row identity:** exactly one row per eligible `(station,date,arm)`; arm key sets equal; duplicate keys fail.
- [ ] **Deterministic rerun:** same command, source/data inputs, seed, and JSON serialization produce byte-identical result content.
- [ ] **No AI calls in loop:** standalone script imports only local deterministic Python modules and makes no network, model, scheduler, trading, or production-config calls.
- [ ] **QC before build:** spec checker validates required sections, dates, stations, arms, gate, test, CI, stop conditions, and literal gates before script execution.
- [ ] **Artifact integrity:** output JSON and receipt are written only after tests and rerun verification; SHA-256 is recorded.

## Accuracy-focused improvement suggestion

Add a mechanical source/runtime guard to the signal validation path that rejects weather reads from `days[idx]` except immutable metadata (`date`/`station`), while allowing prior-index reads. This is accuracy-focused because it prevents future target-day contamination from silently inflating directional accuracy; it changes no current signal and is more durable than manual review alone.
