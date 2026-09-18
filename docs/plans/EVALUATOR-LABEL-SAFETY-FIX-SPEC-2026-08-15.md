# Evaluator Label-Safety Fix Specification — 2026-08-15

## Scope and non-goals
This gate fixes evaluator look-ahead only. It does not build Layer 2D UWC/LLOP comparison, alter production/deployment/paper/shadow/live-trading paths, tune signals, or change output schema.

## Locked geometry
- Scored target date `D`: fold_4 holdout, inclusive `2026-05-15..2026-08-13`.
- Calibration training cutoff: `2026-02-28` inclusive; only training dates `<= cutoff`.
- Existing matched-comparison assumptions remain unchanged.

## Evaluator contract
1. **Prediction cutoff.** For each station/date key `(S,D)`, the feature snapshot is formed at the last METAR daily record with `record.date < D` (prior chronological available METAR index). The target-day METAR row is never passed to `signal.evaluate`. If no prior record exists or `min_lookback` is not met, the cell is ineligible.
2. **Eligible row identity.** One deduplicated settlement per `(station,target_date)` (existing source priority), one prior chronological settlement for the label, non-flat epsilon `0.5°F`, and a prior-METAR feature snapshot. No duplicate station/date rows are allowed.
3. **Calibration/holdout separation.** Calibration fitting uses only labels for dates `D <= 2026-02-28` and history-only features under the same prior-index rule. Holdout dates and labels/features are never used to fit calibration. Scoring excludes calibration dates when calibration is active and applies the requested period bounds.
4. **Prior-METAR indexing.** Resolve `bisect_left`/ordered lookup for the first METAR date `>= D`, then select the immediately preceding index. Exact target date is forbidden even when present. Feature history passed to a signal remains the complete chronological series, but its index is strictly `< D`.
5. **Settlement label-only rule.** `settlements[S][D]` and the prior settlement are read only to compute `actual_dir`/correctness. Settlement values are never passed to signal, calibration, fusion, threshold, or rejection logic. Target-day observed weather is likewise label-forbidden.
6. **Forbidden target-day accesses.** No `metar_date_to_idx[D]`, no `metar_days[idx where date == D]`, no target-day settlement in feature/calibration input, and no holdout label in calibration. Immutable `(station,D)` metadata is permitted.

## Mutation tests and gates
- Mutate target-day METAR fields (temperature, pressure, wind, dewpoint) while preserving prior rows: feature snapshots, raw predictions, fused predictions, and calibration fit must remain byte/equality-identical; labels/correctness may change.
- Mutate target-day settlement: predictions/features remain identical; label/correctness may change.
- Assert calibration dates and scored dates are disjoint; assert all calibration dates `<= cutoff`; assert holdout rows are within the inclusive window; assert unique `(station,date)` keys.
- Static/runtime audit must enumerate every evaluator signal call and prove the passed index maps to a date `< D`.

## Rollback and stop conditions
Stop and escalate if prior-day data cannot be reconstructed; any target-day mutation changes predictions; calibration consumes holdout labels/features; production-path semantics are ambiguous; or broad refactoring is required. Roll back only the files introduced by this gate and preserve all pre-existing artifacts. Layer 2D remains blocked until this gate passes.

## Accuracy-focused follow-up suggestion
After this gate, measure accuracy by signal and station on the honest holdout, then prioritize calibration reliability (Brier/ECE and confidence-floor coverage) before fusion tuning. This attacks overconfident same-day artifacts without changing the locked comparison population.
