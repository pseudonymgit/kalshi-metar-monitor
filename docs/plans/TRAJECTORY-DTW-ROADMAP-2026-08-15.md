# Original DTW Trajectory Experiment Roadmap
Date: 2026-08-15 UTC
Status: parallel research track; no production/trading integration

## Objective
Test the original trajectory concept: dynamic time warping (DTW) over epoch sequences of observed weather, matched to historical trajectories, producing bucket-level predictive recommendations that update sequentially through the day.

## Non-negotiable method
- DTW epoch-sequence matching.
- Not k-NN.
- Not Kalman filtering.
- No substitution of a different "trajectory" implementation.

## Inventory correction
- Standalone DTW research code exists in `scripts/research_trajectory.py`, `a3_dtw_matching.py`, and `a5_dtw_scaletest.py` with historical artifacts.
- Current live modules `core/trajectory_confirmation_gate.py` and `core/trajectory_lane.py` are single-epoch weighted Euclidean matching, not the original DTW trajectory method.
- Roadmap-compliant sequential DTW replay, bucket scoring, and rolling-CRPS evaluation are not yet implemented.
- Therefore current live trajectory behavior is not evidence for this roadmap and remains parked/non-equivalent.

## Experiment sequence

### T0 — Data and epoch contract
Freeze settlement epochs, observation timestamps, station-local time, target bucket identity, and eligible historical analog dates. Verify no future epoch or settlement enters a query trajectory.

### T1 — Feature ablation
Predeclare and compare:
1. temperature only;
2. temperature + humidity;
3. temperature + humidity + pressure;
4. approved additions only if justified by QC.
Feature scaling and DTW channel weights are fixed within each rolling fold.

### T2 — DTW matching/tuning
Tune only on rolling-origin training/validation windows:
- DTW window/band;
- distance normalization;
- analog count K;
- recency weighting;
- minimum analog similarity/quality floor.
Primary tuning objective: rolling-origin CRPS, not test accuracy.

### T3 — Bucket recommendations
For each query epoch, emit a probability distribution over settlement buckets, top bucket(s), expected range, confidence, and abstention when diffuse. Score bucket log loss, CRPS, Brier/ECE where applicable, top-1/top-k bucket accuracy, and coverage.

### T4 — Sequential updates
Replay the same event at successive intraday epochs. Require monotonic timestamp identity, no future observations, probability update trace, and stability/accuracy as the day advances.

### T5 — Integration decision
Only after standalone out-of-sample bucket quality passes review may trajectory become an informant/modifier. It must not override the primary directional engine without a separate integration spec and gate.

## Hard gates
- Rolling-origin temporal separation.
- No k-NN/Kalman substitution.
- No test-peeking during CRPS tuning.
- No target-day settlement/late epoch leakage.
- Deterministic artifact/hash.
- Per-station/season/lead-time strata.
- ADVANCE/PARK/KILL disposition before integration.
