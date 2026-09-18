# Layer 2D Full-Fusion Mutation QC — 2026-08-15

**Disposition: BLOCKED (Phase 1); Phase 2/3 must not run.**

## Scope and fixture

The QC fixture is a deterministic station/date table with the locked signal set
`[pressure_delta, seasonal_regime]`, one prior METAR snapshot per scored key,
settlement temperatures for the current and prior settlement dates, calibration
cutoff `2026-02-28`, holdout `2026-05-15..2026-08-13`, and fold
`fold_4_holdout`. It exercises both raw signal evaluation and the complete
`compute_fusion_accuracy` evaluator/fuser path. Keys are `(station, date)` and
must be unique, identical across mutations, and separated into calibration and
holdout populations.

## Mutations and expected invariants

For every eligible key, run the unmutated fixture and the following mutations:

1. Change every target-day METAR aggregate field (`high`, `low`, `temp`,
   `dewpoint`, `wind_dir`, `wind_speed`, `pressure`) while preserving station
   and date. **Expected:** raw signal direction/confidence, fused direction and
   confidence, fired/abstain, and rejection reason are byte-identical.
2. Change target-day settlement temperature. **Expected:** only label and the
   derived correctness field may change; features, raw predictions, fused
   predictions/confidence, fired/abstain, and rejection reason must not change.
3. Repeat mutations for calibration records and holdout records separately.

Additional invariants: calibration records are strictly `date <= cutoff`,
holdout records are strictly within the inclusive holdout interval, no target-day
weather value is read, station/date keys are identical, and no duplicate keys
exist.

## Stop conditions

Stop with BLOCKED if any mutation changes a forbidden output, if calibration or
holdout rows overlap, if key sets differ/duplicate, or if any target-day METAR or
settlement is passed to signal evaluation, calibration, fusion, thresholding, or
rejection. Do not run UWC/LLOP adapter QC or comparison after a Phase 1 stop.

## Execution evidence

`tests/test_layer2d_full_fusion_mutation_qc.py` contains the focused audit. The
current evaluator's calibration pass derives `was_correct` from target-day
settlements and supplies those records to `fuser.fit_calibration`; therefore a
calibration-date settlement mutation changes the fitted calibration state. This
violates the required settlement invariant and is an intentional blocking
finding, not an acceptable comparison result.
