# Honest Signal-Level Diagnosis Specification — 2026-08-15

## Objective and non-goals

Diagnose why the frozen `pressure_delta + seasonal_regime` pair loses under the corrected prior-METAR evaluator with calibration disabled. This is descriptive research only. `pressure_delta` is the baseline; `seasonal_regime` is diagnostic only. This run cannot select, tune, authorize, promote, deploy, shadow, paper-trade, or live-trade any candidate.

## Locked population and evaluator

Compare exactly three locked signal sets: `[pressure_delta]` (baseline), `[seasonal_regime]` (diagnostic), and `[pressure_delta, seasonal_regime]` (frozen pair). Use canonical stations `KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO`. Analyze current holdout `2026-05-15..2026-08-13` and predeclared extension `2026-03-01..2026-05-14`, inclusive.

The eligible station/date universe is built once per period from the same deduplicated settlement source, a prior chronological settlement, non-flat directional label (epsilon `0.5°F`), and the existence of a strictly prior METAR snapshot. It is independent of arm firing. Every arm must emit exactly one row for every eligible key. The evaluator is `scripts/ws5_common.py::compute_fusion_accuracy`, LLOP fusion, confidence floor `0.50`, fold `4`, same data/stations/period bounds, and `calibration_label_source='disabled'`. No calibration fit occurs and no target-day label/settlement is passed to features, fusion, or calibration.

## Required arm and comparison outputs

For each period and arm, report all-eligible coverage (`eligible_n`, `fired_n`, `abstain_n`, `coverage`), fired accuracy and exact 95% Clopper–Pearson interval, prediction direction counts, confidence summaries, and one deterministic row for each `(station,date)` including label, prediction, fired/abstain, confidence, correctness, rejection reason, signal set, and fold/config identity.

Report exact matched intersections for each pair of arms, including matched fired n, both-arm accuracy, accuracy deltas, and discordance table (`A correct/B incorrect`, `A incorrect/B correct`, both correct, both incorrect). The primary diagnostic comparison is pair versus pressure baseline on the matched fired population; seasonal-alone comparisons are descriptive only. No promotion threshold or selection rule is applied.

## Stratification

Report station strata and calendar seasonal strata (`DJF`, `MAM`, `JJA`, `SON`) for every arm: eligible, fired, coverage, fired correct, and fired accuracy. Also report a deterministic regime/discordance table using the calendar seasonal stratum, and matched-pair discordance by station and season. These strata describe where the pair loses; they do not authorize inference beyond observed cells.

## Fixed interpretation criteria

1. If the pair has lower fired accuracy than pressure baseline on the matched intersection, describe the loss as observed disagreement/selection behavior; do not claim causality.
2. If pair coverage differs, report it as a separate coverage fact; never substitute coverage for accuracy or impute abstentions.
3. If seasonal-alone is weak or its discordant cells concentrate in a station/season, report that concentration descriptively; do not tune or remove strata.
4. Treat small or zero strata as insufficient evidence, not as a signal of quality.
5. An integrity failure (key mismatch, target-day access, calibration fit, inconsistent geometry, missing required data, nondeterministic rerun, or forbidden external/trading call) blocks interpretation. Do not infer from a blocked artifact.

## QC gates and execution contract

Before execution, QC must verify this spec, locked dates/stations/arms, prior-METAR strictness, calibration disabled, no selection/tuning, and read-only/no-network intent. Run one deterministic standalone script twice with the same command. The JSON artifact must be byte-identical; record SHA-256, source/spec/QC hashes, input DB hashes, exact commands, and result summaries in the receipt. No production/deployment/paper/shadow/live trading changes are permitted.

## Accuracy-focused suggestion

If the diagnosis is intact, the only permitted suggestion is accuracy-focused and descriptive: prioritize a predeclared, independent reliability/accuracy review of the seasonal component's station-season discordance before any future candidate decision. This run itself does not authorize that future work or a new candidate.
