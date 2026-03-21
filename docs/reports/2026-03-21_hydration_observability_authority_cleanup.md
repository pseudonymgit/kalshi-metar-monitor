# Hydration Observability Authority Cleanup Report

## Target Reality
- Repository: `pseudonymgit/kalshi-metar-monitor`
- Branch at implementation time: `fix/hydration-observability-authority-cleanup`
- Base reality verified from local checkout before editing.

## Scope
Reporting/schema/docs cleanup only. No hydration execution, discovery, cache population, or alert-path behavior was intentionally changed.

## Change Summary
- Removed the misleading `hydration_state` alias from `GET /observability/hydration-prerequisite-runtime`.
- Split the hydration prerequisite runtime response into explicit authority blocks:
  - `retained_prerequisite_snapshot`
  - `live_cache_probe`
  - `derived_runtime_assessment`
  - `live_queue_snapshot`
- Added explicit `source_authority` metadata to hydration-related blocks in `GET /observability/runtime-authority-snapshot` so operators can distinguish retained snapshots from live telemetry and derived judgments.
- Updated narrowly scoped tests to lock the new reporting contract.
- Updated directly related docs to reflect the new authority semantics while keeping `/observability/runtime-authority-snapshot` as the primary diagnosis surface.

## Route Contract Notes
### `/observability/hydration-prerequisite-runtime`
This route still returns the same derived top-level booleans/status values for backward compatibility (`status`, `reason`, `cache_valid`, `markets_cached`), but the hydration-specific detail now carries explicit authority labeling instead of a mixed alias.

### `/observability/runtime-authority-snapshot`
Hydration-related response blocks now include `source_authority` metadata:
- `hydration_snapshot.source_authority`
- `hydration_execution.source_authority`
- `hydration_queue.source_authority`
- `hydration_stall_signal.source_authority`

## Behavioral Risk Assessment
- No write paths were added.
- No hydration workers are triggered by these changes.
- No queue mutation behavior changed.
- No cache population logic changed.
- No discovery or alert logic changed.

## Verification
- `python -m pytest tests/test_observability_hydration_runtime.py tests/test_runtime_authority_snapshot_endpoint.py -q`

## Operator Guidance
Use `GET /observability/runtime-authority-snapshot` as the primary diagnosis surface. Use `GET /observability/hydration-prerequisite-runtime?station=<ICAO>` as a supporting, station-scoped explainer when you specifically need to compare retained prerequisite state against the live cache probe and queue telemetry in one payload.
