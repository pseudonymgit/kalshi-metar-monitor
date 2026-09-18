# Temperature Model / Target-Geometry Tournament
Date: 2026-08-15 UTC
Status: research-only; expert/QC/inventory dispatched

## Objective
Test which forecast representation works best for daily NWS settlement HIGH and LOW outcomes, allowing specialization by station, direction, lead, and regime only when selected out of sample.

## Candidate families
1. Point forecast at fixed valid step.
2. Valid-path maximum for HIGH / minimum for LOW.
3. Ensemble mean/extrema.
4. Ensemble quantiles and predictive intervals.
5. Member fraction/probability over candidate buckets.
6. Distribution/calibration-based bucket probabilities.
7. DTW trajectory-derived extrema/distribution.
8. ECMWF or GEFS+ECMWF only after provenance/vintage alignment passes.

## Specialization hypotheses
- HIGH and LOW may require different geometry/models.
- Station-specific models may be justified only with minimum support and nested selection.
- Lead/cutoff and seasonal/regime strata may differ.
- Specialization is selected on rolling tune folds and evaluated on locked holdout; no post-hoc winner selection.

## Controls
- Candidate registry frozen before scoring.
- Common station/date/event universe per comparison.
- Exact issue/availability and local-day valid-window geometry.
- Temporal train/tune/locked-shadow separation.
- Family-level multiple-testing correction.
- Minimum support, shrinkage/pooled fallback, and abstention rules predeclared.
- Deterministic row artifacts, hashes, and source lineage.

## Primary metrics
Paired key-matched MAE/RMSE and block-bootstrap CI for forecast values; CRPS for distributions; directional accuracy; Brier/ECE; interval coverage; bucket top-k accuracy. P&L is downstream only.

## Disposition
Each family and specialization arm receives ADVANCE, PARK, or KILL. No candidate enters SignalRegistry, trajectory integration, shadow trading, or sweep without a separate gate.
