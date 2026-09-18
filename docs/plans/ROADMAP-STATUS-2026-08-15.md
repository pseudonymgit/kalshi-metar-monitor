# Weather Engine Roadmap Status
Date: 2026-08-15 UTC
Owner: Donna / Gilfoyle / Gerri

## Current decision gates
Global calibration enablement and real sweep execution are conditionally approved by Dan, but execute only after the gates below pass. No live capital or deployment is implied.

## Active workstreams

### A. Core research path — calibrated pressure_delta
Status: RUNNING / highest priority.

- Explicit `historical_kalshi` labels with strict temporal separation.
- Same event universe, prior-METAR evaluator, current and extension windows.
- Artifact: `data/pressure_delta_calibrated_reliability_review_20260815.json`.
- Verified result: calibration materially improves Brier/ECE but does not improve aggregate accuracy; disposition PARK for this candidate pending final review.
- Next: independent review, then decide whether calibration is globally enableable for the sweep or remains research-only.

### B. SRRS audit/replay path — dual baseline/optimized
Status: RUNNING IN PARALLEL / optional.

- Purpose: historical NWS CLI provenance, revision/replay audit, and comparison against GHCN/Kalshi.
- Not a prerequisite for core calibration or sweep execution.
- Baseline/optimized outputs remain isolated.
- Optimized runner passed offline 300/300 equivalence but the first live benchmark was terminated at 167/300 after source mutation; no speed result was accepted.
- A new source-frozen dual-path benchmark/backfill lane is active. It must complete 300/300 exact equivalence before optional full audit backfill.

### C. Runtime/test certification
Status: ACTIVE.

- Targeted Layer 5: 49/49 pass.
- Adjacent runtime: 69/69 pass.
- Full collection: 912 collected with 8 stale import/API collection errors; paper-trading datetime traceback remains unresolved.
- Next: classify/fix or explicitly accept residuals before execution snapshot preflight.

### D. Clean execution snapshot / unattended runner
Status: READY BASELINE / INCOMPLETE INCLUSION.

- Clean snapshot: `prototypes/weather-engine-execution-snapshot-20260815/` at `48587df...`.
- Runner: `scripts/run_unattended_sweep.py`, tests 3/3, fail-closed dirty-tree preflight.
- Next: apply only reviewed evaluator/calibration/runtime files, freeze hashes, and rerun preflight. Never run from shared dirty worktree.

### E. GEFS / ECMWF legacy lane
Status: SECONDARY / NEEDS LEDGER RECONCILIATION.

- GEFS historical data is available for an honest GEFS-only research sweep, subject to current SignalRegistry roadmap review.
- ECMWF 51-member historical backfill is blocked by ECDS rate limits; do not make it a prerequisite for GEFS-only work.
- The old GEFS-era roadmap entries are historical unless explicitly reactivated by a new spec and approval.

## Completed/rejected milestones

- Layers 0–4 SignalRegistry research/integrity work complete.
- UWC/LLOP Layer 2D killed for current promotion path due non-comparable semantics.
- Seasonal_regime rejected diagnostically; pair rejected.
- Pressure_delta reliability review parked under corrected no-calibration path.
- Calibration temporal-separation correction passed focused gates and is ready for controlled research; global default remains disabled.
- SRRS official route verified; KHOU CCA lineage policy resolved; full audit backfill is optional.

## Execution sequence
1. Complete/verify calibrated pressure_delta research result.
2. Complete independent SRRS/QC and optional dual-path benchmark in parallel.
3. Resolve full-suite/runtime residuals or document explicit acceptance.
4. Assemble clean execution snapshot with reviewed files and hashes.
5. Run unattended runner preflight.
6. Final gate review: calibration disposition, test status, snapshot/hash status, artifact/ledger consistency.
7. Enable calibration globally only if final gate passes.
8. Execute one unattended sweep through the fail-closed runner.
9. Review sweep artifact before any downstream shadow/deployment decision.

## Hard boundaries
- No sweep from shared dirty worktree.
- No source edits during an unattended run.
- No silent proxy substitution.
- No threshold tuning during validation.
- No production/deployment/trading change before final gate.
