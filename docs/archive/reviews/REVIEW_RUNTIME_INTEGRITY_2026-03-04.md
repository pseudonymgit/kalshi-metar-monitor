# Full Repository Code Review — Deterministic Runtime Integrity

Date: 2026-03-04  
Scope: Entire repository runtime path (ingestion, transition tracking, Kalshi hydration/discovery, alert emission, observability, scheduler)

## 1. Architecture Assessment

The repository is structured around a clear execution split:

- `core/metar_monitor.py` owns ingestion, scheduler loop, transition emission orchestration, and alert routing.
- `core/kalshi_monitor.py` owns market discovery, ladder hydration, market filtering, and composed alert construction.
- `core/transition_emitter.py` centralizes transition emission authority and epoch logging hook-in.
- `core/settlement_epoch_logger.py` persists epoch lifecycle state keyed by station/day.
- `core/observability.py` provides read-only query surfaces for persisted runtime state and immutable snapshots.
- `app.py` exposes runtime and observability HTTP endpoints.

Strengths:

- Explicit authority checks exist for transition emission and replay isolation.
- Replay path is separated via `execute_ordered_replay_stream(... allow_alert_delivery=False, persist_cache=False)`.
- Observability module itself uses SQLite read-only mode for query APIs and immutable public snapshots.

Primary architectural concern:

- `app.py` startup hooks (`before_first_request` / fallback `before_request`) can mutate runtime state and attempt discovery/scheduler startup regardless of endpoint class. This makes read-only behavior depend on request ordering and first-hit path.

## 2. Determinism Risks

### Finding D1 — Non-deterministic fallback timestamp in ISO parsing
`_parse_iso()` falls back to `datetime.utcnow()` on parse failure. Any bad timestamp can therefore inject wall-clock time into state transitions, reset logic, and lag calculations.

Impact:

- Replay divergence when malformed timestamps are encountered.
- Potential day-boundary misclassification in station-local logic.

### Finding D2 — Transition event timestamps are runtime-now, not observation-authoritative (diagnostic consistency)
`_log_transition_event()` stamps `created_utc`/`timestamp_utc` using `_now_utc_iso()` rather than observation time.

Impact:

- Replays of identical observation streams can produce different transition timestamps and downstream ordering in diagnostics.
- This affects diagnostic consistency and replay audit comparability, not core trading semantics.

### Finding D3 — Multiple runtime-now reads in deterministic diagnostics path (diagnostic consistency)
Health and runtime diagnostic builders repeatedly call `datetime.now(timezone.utc)` inside loops and helper calls.

Impact:

- Small internal skew across computed lags/status inside a single snapshot response.
- Not a core trading semantic break; this is a diagnostics consistency issue that reduces deterministic reproducibility for observability snapshots and automated replay validation.

## 3. Runtime Integrity Risks

### Finding R1 — Observability first-hit can mutate runtime (**highest priority**)
Autostart hooks can:

- start scheduler,
- run series discovery,
- merge discovered stations into watchlist.

If first inbound request is to `/observability/...`, runtime state can still mutate before domain is set to observability.

Impact:

- Violates strict read-only expectation for observability access patterns.
- Produces environment-dependent behavior (cold process + first endpoint hit).

### Finding R2 — Bounded in-memory transition retention requires explicit scope handling
`_TRANSITION_HISTORY` is intentionally capped at 500 entries and many runtime diagnostics depend on this bounded in-memory window.

Impact:

- In-memory views may appear incomplete relative to DB-backed history under sustained load.
- “No transition seen” style determinations can be false negatives unless endpoints clearly declare in-memory scope.

### Finding R3 — Health model does not expose a global precedence-evaluated status
`compute_system_health_snapshot()` computes component statuses (`ingestion`, `hydration`, `evaluation`) but does not return an aggregate with explicit `BLOCKED > DEGRADED > OK` precedence resolution.

Impact:

- Consumers can misinterpret composite health unless they replicate precedence logic externally.
- Cross-endpoint inconsistency risk if different clients apply different precedence rules.

## 4. Observability Safety Review

### Finding O1 — Core observability module is mostly read-only by construction
`core/observability.py` uses:

- immutable state snapshot readers,
- SQLite `mode=ro` URI queries,
- no mutation calls.

Assessment: **NO ISSUES DETECTED inside `core/observability.py` itself.**

### Finding O2 — Observability endpoint execution path can still trigger mutable side effects at app layer (**highest priority**)
`app.py` pre-request startup/autostart logic and watchlist merge can run before request domain scoping and before route handler execution.

Assessment: **Read-only guarantee is not end-to-end at HTTP boundary.**

### Finding O3 — Observability path protection for live Kalshi calls is present but can degrade endpoint reliability
`_kalshi_public_get()` blocks forbidden domains including observability. This prevents accidental live calls from observability routes.

Residual risk:

- Some observability builders call discovery helpers that may raise when domain guard blocks live access and cache is cold, causing 5xx instead of deterministic stale/read-only response.

## 5. Alert Logic Edge Cases

### Finding A1 — Transition can be recorded without market evaluation annotation
`_annotate_transition_history_market_eval()` requires transition correlation. If missing, annotation is skipped with warning.

Impact:

- Transition exists but no downstream evaluation context (appears as unevaluated).
- Can confound diagnostics that assume every transition has market evaluation metadata.

### Finding A2 — Rate-limit suppression can hide legitimate rapid transitions
`_send_alert()` applies station-level call throttling (`_KALSHI_CALL_THROTTLE_SECONDS`), returning early before evaluation if too soon.

Impact:

- Closely spaced transitions may never reach market evaluation/alert emission path.
- Integrity dashboards may show fewer evaluations than transitions without explicit causal linkage.

### Finding A3 — Terminal-state suppression behavior is explicit but can be conflated with generic suppression
`process_ladder_transition` emits `terminal_state_blocked`; higher-level outcome normalization can map many non-alert outcomes to `SUPPRESSED_*`.

Impact:

- Diagnostic clarity is reduced when terminal and non-terminal suppressions are aggregated in high-level summaries.

## 6. Hydration / Discovery Risks

### Finding H1 — Cache rollover grace admits prior-day cache as valid
Hydration prerequisite accepts prior station-local day cache under rollover grace.

Impact:

- Stale ladder can be treated as `cache_valid` during rollover window.
- Potential mismatch with current-day listed markets if market structure changed overnight.

### Finding H2 — Partial hydration state can pass admission while market phase is disabled
Polling admission marks `admitted_to_fetch=True` even when hydration is not cache-valid, while setting `market_phase_enabled` separately.

Impact:

- Ingestion continues (good) but alerting/evaluation semantics become split-phase and easier to misread operationally.
- If hydration repeatedly fails, system may appear healthy on ingestion while silently non-alerting.

### Finding H3 — Discovery failure on cold cache can cascade into observability gaps
When discovery is unavailable and no cached map exists, observability market coverage/runtime paths can degrade or fail depending on route handling.

Impact:

- Harder to distinguish true “no eligible market” from “discovery unavailable” without per-route explicit fallback envelopes.

## 7. Concurrency or State Mutation Risks

### Finding C1 — Shared mutable structures span locks with mixed granularity
State is protected by several locks (`_STATE_LOCK`, `_TRANSITION_LOCK`, `_SERIES_LOCK`, others), but cross-structure snapshots often compose data from separately locked reads.

Impact:

- Minor self-inconsistency windows in composite diagnostics (e.g., transition list + latest eval context + ingestion runtime captured at slightly different moments).

### Finding C2 — Scheduler lifecycle is mostly safe, but request-triggered startup adds race-like operational behavior
Scheduler start/stop is lock-guarded and idempotent enough for normal operation. However, first-request autostart path introduces endpoint-order-dependent startup timing.

Impact:

- Operationally surprising behavior in multi-worker deployment where first traffic shape determines runtime initialization side effects.

### Finding C3 — Transition persistence and in-memory append are not atomic as a unit
DB insert then in-memory append is best-effort; failures can produce DB-only entries or memory-only visibility gaps.

Impact:

- Different observability views (DB-backed vs in-memory-backed) may disagree transiently or after exceptions.

## 8. Recommended Hardening Improvements

1. Replace `_parse_iso()` fallback-to-now with explicit failure propagation (or deterministic sentinel) so malformed timestamps cannot inject wall-clock nondeterminism.
2. Stamp transition events from observation-time authority (or dual-field with explicit `observed_at_utc` + `recorded_at_utc`) and use observation-time for replay parity comparisons; if dual fields are added, timestamp handling should improve auditability without changing alert/trading semantics.
3. Enforce app-layer observability purity:
   - bypass autostart/discovery/watchlist mutation when path is `/observability/*`.
4. Return explicit aggregate health status from `compute_system_health_snapshot()` using strict precedence (`BLOCKED > DEGRADED > OK`) to remove client-side ambiguity.
5. Make observability routes resilient under cold cache + forbidden domain by returning structured degraded payloads instead of raising.
6. Add explicit “evaluation skipped due to rate-limit throttle” annotation so transition-to-evaluation gaps are diagnosable.
7. Provide DB-backed fallback for transition runtime queries where completeness matters (or clearly label in-memory scope/retention limits).
8. Strengthen hydration freshness policy around day rollover (e.g., bounded grace with explicit stale classification surfaced to health).
