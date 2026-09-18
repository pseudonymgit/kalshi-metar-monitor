# Calibration temporal-separation correction — 2026-08-15

## Status and scope

Research-only correction. No sweep, deployment, trading, pressure-delta reliability review, GHCN/CLI acquisition, or production enablement is authorized. The end state is **READY FOR CONTROLLED RESEARCH RERUN** only if the focused gates pass; this document does not authorize that rerun.

## Target contract

The official settlement product is the NWS station daily high/low outcome, resolved on the following day from the overnight report. A calibration label represents the directional change for one station, local target date, and market direction (`HIGH` or `LOW`): compare that event's settled value with the strictly prior settled target date for the same station and event direction. `flat` is excluded from directional calibration.

Kalshi settlement outcomes are valid historical labels when they are strictly before the prediction/holdout date and are not sourced from the current holdout. The scored target-day settlement remains an evaluation outcome only; it must never enter fitting for that prediction date.

## Fold and cutoff geometry

- Fixed calibration cutoff: `2026-02-28` (inclusive for historical training labels).
- For every scored prediction date `d`, calibration labels must have target date `< d`; the implementation enforces this by fitting only `<= cutoff` and scoring only dates after the cutoff when calibration is active.
- Holdout labels are never fit. If a holdout interval overlaps the cutoff, the overlap is excluded from fitting and scoring remains disjoint.
- Signal features are evaluated using the latest METAR day strictly earlier than the target date. A target-day METAR row is not a feature input.
- Calibration folds are expanding/history-only: fit on the fixed historical prefix, then apply unchanged to the disjoint evaluation/holdout window. Any future controlled rerun must retain this geometry per fold.

## Identity and duplicate policy

Calibration labels are keyed by `(station, target_date, market_direction)`, not by station/date alone. `HIGH` and `LOW` rows on the same station/date are separate events and must not be collapsed. The source loader derives identity only from explicit HIGH/LOW series names, preserves event ticker identity, and rejects an identity when duplicate rows disagree or represent multiple event tickers. Exact repeated copies of the same event/value may be coalesced deterministically. No arbitrary first/latest row selection is permitted for calibration labels.

## Mutation invariants

1. Changing a historical training settlement changes the fitted calibration only through the corresponding historical label and only when that label is otherwise valid; it must not alter fold membership or target identity.
2. Changing a target-day or holdout settlement cannot change fitted calibration history, calibrator state, raw prediction, calibrated confidence, firing, rejection, or fusion output. It may change only the scored correctness/accuracy attached to that target.
3. Changing target-day METAR data cannot change the strictly prior METAR snapshot used by the prediction.
4. Training and scored/holdout label keys are disjoint; no target-day or holdout key is accepted by the fit path.
5. Repeated runs with identical inputs produce identical label counts, fit history, predictions, and artifact hashes.

## Implementation plan

1. Audit `scripts/ws5_common.py`, the calibration/fuser contracts, settlement loader, and the prior fail-closed path.
2. Add an explicit historical-only calibration-label loader/audit keyed by station/date/HIGH|LOW, with the `2026-02-28` cutoff guard and ambiguity exclusion.
3. Add an opt-in evaluator argument for those prevalidated labels. Keep the default `calibration_label_source="disabled"`; do not enable globally.
4. Keep the output schema unchanged and leave GHCN/CLI as optional audit evidence, not a calibration prerequisite.
5. Run focused calibration, evaluator label-safety, full-fusion mutation, settlement identity, and Brier/ECE tests; run deterministic audit twice.

## Acceptance criteria

- The exact prior defect is identified: the old evaluator's settlement-derived training path could pair a current scored settlement with signal output in the same evaluator universe; the fail-closed branch prevented safe historical use altogether. The corrected path proves that supplied historical labels only fit on the pre-cutoff prefix.
- Historical Kalshi labels can be passed to `fit_calibration` only through the explicit opt-in path; default callers still perform no calibration fit.
- No HIGH/LOW identity collapse or arbitrary duplicate choice occurs.
- Fold/cutoff and strict-prior-METAR assertions pass.
- Historical-label mutation changes fit state only as expected; target-day/holdout mutation leaves prediction-side state invariant.
- Brier/ECE and full-fusion mutation gates pass, with no production or trading side effects.
- Continuity evidence records exact commands, counts, changed files, hashes, limitations, and one accuracy-focused suggestion.
