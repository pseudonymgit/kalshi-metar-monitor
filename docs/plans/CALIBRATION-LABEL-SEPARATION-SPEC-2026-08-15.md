# Calibration-label separation — 2026-08-15

## Status

Layer 2D prerequisite / evaluator gate. Calibration is **disabled in this evaluator** until a separately supplied, immutable historical label source exists. This is deliberate: do not replace `was_correct` with a silently different target.

## Contract

Calibration may fit only history-only feature/signal outputs paired with a fixed training label for a legitimate prediction target. Features must be generated at the prior-METAR cutoff for the historical prediction date. A target-day METAR row is never eligible. A holdout label is never consumed.

For the Layer 2D evaluator, `settlements[station][date]` is also the scored-date label used to derive `actual_dir` and `was_correct`. There is no independent, immutable label stream in this API. Therefore `was_correct = (pred_dir == actual_dir)` is a valid *accuracy outcome* for scoring, but it is not an acceptable calibration-training label for this evaluator: its source is the same mutable settlement table used by the scored evaluation. Replacing it with a proxy would change the statistical meaning and is forbidden.

## Fold geometry and cutoff

The Layer 2D gate uses the exact calibration cutoff **2026-02-28**. Training dates would be dates `<= 2026-02-28`, with features from strictly prior METAR observations; the evaluation/holdout dates are disjoint and target-day settlement values are used only for scored correctness. Holdout labels are never available to calibration. Other callers may pass their fold cutoffs, but this evaluator does not fit until an explicit safe label source is implemented.

## Required invariants

Mutating any settlement, including a date at or before the nominal calibration cutoff, must not change calibration history, fitted calibration state, raw/calibrated predictions, confidence, firing, rejection, or fusion output. Settlement mutation may change only scored correctness/accuracy metrics for rows whose outcome is being scored. Target-day METAR mutation must likewise not affect a prior-METAR snapshot.

## Implementation decision

`compute_fusion_accuracy` accepts `calibration_label_source`, defaulting to `"disabled"`. The only currently supported value is `"disabled"`; any attempted settlement-derived calibration source fails closed rather than fabricating a target. The existing `fit_calibration` API remains unchanged for callers that own a separately validated history, but this evaluator does not invoke it from settlements.

## Tests / gate

- full-fusion settlement mutation QC (calibration-date mutation leaves fit/prediction state unchanged)
- evaluator label-safety tests (strict prior METAR and disabled calibration path)
- existing calibration pipeline and Platt tests
- `py_compile` for changed Python files
- deterministic audit twice, comparing counts and hashes

Accuracy remains the calibration-adjacent suggestion: obtain an independently versioned historical temperature-outcome label table, then validate temperature-direction accuracy and calibration Brier/ECE separately before enabling any fit. This preserves the meaning of the target and avoids P&L/settlement leakage.
