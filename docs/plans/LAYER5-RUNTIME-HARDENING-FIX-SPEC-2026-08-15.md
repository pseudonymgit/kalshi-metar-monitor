# Layer 5 Runtime Hardening Fix Spec — 2026-08-15

## Scope
Bounded remediation of the six currently failing Layer 5 runtime tests only. No production deployment, trading, or unrelated refactors.

## Canonical contracts
- **Execution domain:** canonical spelling is `production` (not `prod`). Non-production domains remain guarded and must not be weakened.
- **Time/logging mocking:** runtime code should use the module's injectable/patchable clock boundary (tests patch `time.time` where applicable); assertions should inspect structured snapshot fields rather than fragile log text. Backoff timestamps are absolute epoch seconds; `retry_after` is derived from current clock and rounded down to an integer.
- **Discovery snapshots:** failed discovery records `last_discovery_failure`; successful series discovery records the success metadata and cache contents. State must be reset/isolated per test.

## Failure classification and intended action
1. `test_connectivity_snapshot_records_last_discovery_failure` — **source defect or stale fixture pending inspection**: snapshot must retain failure metadata after a discovery exception; fix source if state is dropped, otherwise update stale expectation only.
2. `test_connectivity_snapshot_records_series_discovery_success` — **source defect or stale fixture pending inspection**: successful series discovery must publish success metadata; fix source if absent, otherwise update stale expectation only.
3. `test_429_sets_backoff_and_prevents_immediate_retry` — **source defect** if 429 stores a relative delay where snapshot expects absolute expiry or clock use is unpatchable; preserve retry guard and fix state calculation minimally.
4. `test_worker_processes_one_request_per_interval` — **source defect** if worker drains more than one item or uses non-injectable timing; fix worker cadence minimally.
5. `test_execution_domain_defaults_to_production` — **stale test only if it expects `prod`; canonical source truth is `production`; never weaken domain guard.
6. `test_hydrate_snapshot_reads_all_series_cache_entries` — **test-isolation defect** if context manager/domain mismatch prevents the cache-path assertion; otherwise source defect. Preserve production-only hydration guard.

## QC gate before implementation
Inspect exact test assertions and implementation paths; run the six tests unchanged and capture traceback. Verify no production trust-boundary regression and confirm domain spelling from source/tests. Stop/escalate if canonical behavior is ambiguous.

## Acceptance
Apply smallest justified source/test edits, run six tests, then focused Layer 5 suite. Record exact commands/counts/residual failures in continuity receipt. Accuracy suggestion must target runtime observability or test determinism without changing trading behavior.
