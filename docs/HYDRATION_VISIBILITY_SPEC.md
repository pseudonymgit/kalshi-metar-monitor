# HYDRATION VISIBILITY SPEC

## Scope and Authority

This specification addresses **Silent Zone #3 (Hydration causality ambiguity)** identified in `docs/archive/planning/VISIBILITY_SURFACE_MAP.md`.

Design intent is restricted to **visibility only**. This spec does not change:

- hydration decision logic,
- cache refresh strategy,
- ingestion sequencing,
- scheduler cadence,
- retry/fallback behavior,
- transition or alert execution semantics.

## 1) Hydration State Model

### 1.1 Purpose

Hydration visibility MUST allow operators to classify ladder readiness directly, per station and per poll cycle, without inference from skipped-evaluation side effects.

### 1.2 Canonical State Envelope

A **Hydration Visibility Event** is an immutable record representing the finalized outcome of one hydration evaluation attempt context for one station during one poll cycle.

Required fields:

- `event_type` (constant): `ladder_hydration_visibility`
- `event_version` (integer): schema version
- `station_id` (string)
- `poll_cycle_id` (string): deterministic identifier for the scheduler cycle/poll context
- `evaluation_timestamp_utc` (ISO-8601 UTC): when hydration status is finalized for this cycle
- `hydration_state` (enum): taxonomy defined in Section 2
- `state_reason_code` (enum string): deterministic reason class for current state
- `state_reason_detail` (string, optional): bounded token, never free-form log text
- `ladder_snapshot_timestamp_utc` (ISO-8601 UTC, nullable): source freshness timestamp when ladder payload exists
- `staleness_age_seconds` (integer, nullable): deterministic computed age at evaluation time
- `cache_entry_id` (string, nullable): deterministic cache key/version identifier if present
- `evaluation_mode` (enum): `live` or `replay_ignored` (Section 6)

### 1.3 Event Invariants

1. At most one terminal hydration visibility event per `(station_id, poll_cycle_id)`.
2. Event is emitted only after hydration evaluation concludes for that cycle context.
3. Event write/query failures MUST NOT change hydration logic or ingestion execution decisions.
4. Ordering within a station is by `evaluation_timestamp_utc` with deterministic tie-breaker.
5. Event model is append-only; historical records are immutable.

## 2) Deterministic Hydration Taxonomy

### 2.1 Required Operator-Distinguishable States

The taxonomy MUST map total hydration outcomes to exactly one of these canonical states:

- `HYDRATED_USABLE`
  - Ladder exists and is currently valid for execution prerequisites under existing rules.
- `MISSING`
  - Ladder is absent/unavailable in cache/payload for station at evaluation time.
- `STALE`
  - Ladder exists but violates existing freshness threshold.
- `ATTEMPT_FAILED`
  - Hydration attempt executed but ended in deterministic failure outcome (e.g., fetch/parse/validation failure path already present in logic).
- `NOT_ATTEMPTED`
  - No hydration attempt was executed for this station in the poll cycle under existing control flow.

These five states are mandatory and mutually exclusive.

### 2.2 Reason Codes

Each state MUST include a deterministic `state_reason_code` drawn from a bounded taxonomy. Minimum required codes:

- For `HYDRATED_USABLE`: `CACHE_VALID`
- For `MISSING`: `CACHE_ABSENT`
- For `STALE`: `CACHE_EXPIRED`
- For `ATTEMPT_FAILED`: `HYDRATION_EVALUATION_FAILURE`
- For `NOT_ATTEMPTED`: `HYDRATION_PATH_NOT_ENTERED`

Implementations may extend with additional bounded tokens but MUST preserve one-to-one deterministic mapping from existing hydration path outcomes.

### 2.3 Precedence and Totality

If multiple conditions are true in one cycle, precedence MUST be fixed and deterministic. Required precedence:

1. `NOT_ATTEMPTED`
2. `ATTEMPT_FAILED`
3. `MISSING`
4. `STALE`
5. `HYDRATED_USABLE`

Rationale: operator causality should prioritize whether execution reached hydration evaluation at all, then whether attempt completed, then resulting materialized cache quality.

## 3) Visibility Surfaces

### 3.1 Primary Surface: Hydration Timeline Feed

Expose an explicit hydration timeline queryable by station/time window.

Minimum capabilities:

- filters: `station_id`, `from_utc`, `to_utc`, optional `hydration_state`, optional `evaluation_mode`
- order: `evaluation_timestamp_utc` ascending/descending with deterministic tie-breaker
- stable pagination without sampling

### 3.2 Secondary Surface: Current Hydration Snapshot

Provide latest state per station for immediate readiness checks.

Required fields per station:

- `current_hydration_state`
- `current_state_reason_code`
- `last_evaluation_timestamp_utc`
- `ladder_snapshot_timestamp_utc`
- `staleness_age_seconds`

### 3.3 Tertiary Surface: Poll-Cycle Matrix

A poll-centric surface keyed by `poll_cycle_id` containing one row per active station, enabling deterministic detection of station coverage gaps within a cycle.

Required row-level guarantee: exactly one of the five canonical states for every station expected in the cycle scope.

### 3.4 Dashboard Semantics

Operator UI/status summaries MUST derive hydration readiness from hydration surfaces directly (state token + timestamp), not from skipped ingestion or alert suppression symptoms.

## 4) Persistence Requirements

### 4.1 Durability and Retention

Hydration visibility records must be durably persisted with retention at least equal to market-coverage and transition audit retention used for operational diagnosis.

### 4.2 Idempotency Key

Canonical event identity key:

`(station_id, poll_cycle_id, hydration_evaluation_context_key)`

Requirements:

1. Duplicate write attempts for same identity are idempotent.
2. Reprocessing/recomputation within a poll cycle must not create duplicate logical visibility events.
3. Identity is deterministic and independent of wall-clock write timing.

### 4.3 Deterministic Hydration Event Identity Anchoring

Hydration event identity MUST anchor to the evaluation instance, not to the evaluated outcome.

Canonical identity tuple:

- `station_id`
- `poll_cycle_id`
- `hydration_evaluation_context_key`

`hydration_evaluation_context_key` MUST be derived only from deterministic, replayable scheduler/hydration inputs already present in execution context, such as:

- `poll_cycle_id`
- hydration invocation boundary
- cache evaluation markers already present in execution context

Identity invariants:

1. Recomputation of the same hydration evaluation instance MUST NOT create a new logical hydration visibility event.
2. Process restart MUST reproduce the same canonical identity tuple for the same evaluation instance.
3. Replay of equivalent deterministic inputs MUST reproduce the same canonical identity tuple.
4. `hydration_state` and `state_reason_code` are outcome attributes and MUST NOT be identity authority.
5. Persistence MUST deduplicate writes using the deterministic identity tuple.
6. Identity authority and ordering timestamps are distinct: identity determines uniqueness/deduplication, while `evaluation_timestamp_utc` determines presentation order only.

### 4.4 Query Indexing

Minimum indexed access patterns:

- `(station_id, evaluation_timestamp_utc)`
- `(poll_cycle_id, station_id)`
- `(hydration_state, evaluation_timestamp_utc)` for fleet-level diagnosis

### 4.5 Failure Isolation

Hydration visibility persistence degradation MUST NOT block scheduler progress, hydration decisioning, ingestion, transitions, or alerts. Visibility remains non-causal.

## 5) Awareness Latency Bounds

Define deterministic latency terms measured from hydration outcome finalization:

- `T_persist_hydration`: outcome finalized → durable event write visible in storage
- `T_surface_hydration`: durable write → API/query surface visibility
- `T_awareness_hydration = T_persist_hydration + T_surface_hydration`

Operational bound:

- `T_awareness_hydration` MUST be strictly less than one poll interval for current-state surface.
- For timeline feeds, bounded eventual consistency is acceptable if explicit; bound must remain below stale-threshold inference window so `STALE` and `ATTEMPT_FAILED` are visible before operators infer root cause from unrelated symptoms.

## 6) Replay Interaction Rules

Replay is not hydration-authoritative for live ladder readiness.

Rules:

1. Replay execution MUST NOT mutate live hydration readiness state.
2. Hydration visibility timelines for live operations MUST remain tagged `evaluation_mode=live`.
3. If replay emits hydration-adjacent diagnostics, they MUST be tagged `evaluation_mode=replay_ignored` and excluded from live readiness summaries by default.
4. Operator surfaces comparing live vs replay must never allow replay records to satisfy live hydration readiness checks.
5. Absence of replay hydration records must not be interpreted as live hydration failure.

## 7) Non-Causality Proof

### 7.1 Causal Separation

- Hydration execution authority remains in existing hydration/cache logic.
- Visibility emission occurs after outcome determination.
- No hydration visibility field is consumed by hydration decision logic, ingestion acceptance, transition emission, alert gating, or scheduler cadence.

### 7.2 Failure-Mode Argument

If hydration visibility write or query fails:

- hydration attempt behavior remains unchanged,
- cache refresh/invalidity behavior remains unchanged,
- ingestion and transition behavior remains unchanged,
- scheduler timing remains unchanged.

Therefore hydration visibility cannot create false hydrated/missing/stale outcomes and cannot alter market-facing execution behavior.

### 7.3 Deterministic Operator Causality Guarantee

Given this model, for every station in scope operators can directly read one of exactly five canonical outcomes (`HYDRATED_USABLE`, `MISSING`, `STALE`, `ATTEMPT_FAILED`, `NOT_ATTEMPTED`) with deterministic timestamp and reason token, eliminating causality ambiguity from secondary symptom inference.

---

## Acceptance Statement

When implemented, this specification ensures hydration readiness is first-class and directly observable per station and poll cycle, allowing operators to distinguish usable, missing, stale, failed-attempt, and not-attempted states without relying on inferred side effects.
