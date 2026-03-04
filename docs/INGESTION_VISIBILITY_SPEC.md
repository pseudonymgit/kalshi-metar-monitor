# INGESTION VISIBILITY SPEC

## Scope and Authority

This specification addresses **Silent Zone #2 (Rejected-ingestion class opacity)** identified in `docs/archive/planning/VISIBILITY_SURFACE_MAP.md`.

Design intent is restricted to **visibility only**. This spec does not change:

- ingestion acceptance/rejection criteria,
- parsing behavior,
- scheduler cadence,
- execution semantics,
- retry behavior,
- filtering behavior.

## 1) Rejected Observation Event Model

### 1.1 Event Definition

A **Rejected Observation Visibility Event** is an immutable, deterministic record emitted whenever an observation is evaluated by ingestion logic and is not accepted into authoritative ingestion state.

Event emission is observational (side-channel visibility) and must occur only after rejection/skip decision is finalized.

### 1.2 Canonical Event Shape

Each event must contain the following fields:

- `event_type` (constant): `metar_observation_rejected`
- `event_version` (integer): schema version for forward-compatible evolution
- `evaluation_timestamp_utc` (ISO-8601 UTC): timestamp when rejection decision is finalized
- `station_id` (string): station under evaluation
- `day_utc` (string `YYYY-MM-DD`): UTC day key used for operator partitioning and replay lookup
- `source_observation_timestamp_utc` (ISO-8601 UTC, nullable): parsed source timestamp when available
- `source_sequence_key` (string, nullable): deterministic ingest-order key if present in current pipeline
- `rejection_reason_code` (enum string): deterministic taxonomy class (Section 2)
- `rejection_reason_detail` (string, optional): constrained deterministic detail token (not free-form logs)
- `evaluation_mode` (enum): `live` or `replay`
- `correlation_id` (string, optional): deterministic trace key for joining with accepted/replay traces

### 1.3 Emission Invariants

1. Exactly one rejected visibility event per uniquely identified evaluated observation that is not accepted.
2. No event for observations never evaluated.
3. Event write failure must not alter ingestion outcomes.
4. Event ordering must preserve ingestion evaluation order within `(station_id, day_utc)`.
5. Event model must remain append-only; no mutation of prior rejection reason codes.

### 1.4 Deterministic Rejection Event Identity

Rejected-event identity MUST be derived from deterministic ingestion context and MUST be
independent of runtime timing.

Canonical rejection identity tuple:

- `station_id`
- `source_observation_timestamp_utc` (when present)
- `source_sequence_key` OR normalized observation hash (when sequence key is absent)
- `rejection_reason_code`

Identity requirements:

1. Idempotent write behavior MUST be keyed by the canonical rejection identity tuple.
2. Reprocessing (including ingest retry and partial batch recovery) MUST NOT create new
   logical rejection events for an already identified rejected observation.
3. Replay MUST reproduce identical rejection identity for equivalent ordered deterministic
   inputs.
4. Visibility persistence MUST tolerate duplicate write attempts safely via deterministic
   deduplication on identity.

Ordering relationship:

- Identity determines uniqueness and deduplication authority.
- Ordering fields (for example `evaluation_timestamp_utc`) determine presentation order only
  and MUST NOT be used as rejection identity components.

## 2) Deterministic Rejection Reason Taxonomy

Reason codes must represent deterministic rejection classes already implied by existing ingestion decisions. Taxonomy introduces no new decision branches.

### 2.1 Required Top-Level Classes

- `PARSE_INVALID`
  - Observation payload failed deterministic parse/normalization preconditions.
- `TIMESTAMP_INVALID`
  - Observation timestamp cannot be deterministically interpreted.
- `WINDOW_OUT_OF_SCOPE`
  - Observation lies outside configured ingest time window/day scope.
- `STALE_OBSERVATION`
  - Observation is older than accepted freshness/order threshold.
- `DUPLICATE_OBSERVATION`
  - Observation is equivalent to previously processed state under existing duplicate rules.
- `ORDERING_CONFLICT`
  - Observation violates monotonic/order acceptance constraints.
- `STATION_SCOPE_MISMATCH`
  - Observation does not belong to active station ingest scope under existing rules.
- `OTHER_DETERMINISTIC_REJECTION`
  - Reserved catch-all where legacy path exists but explicit class is not yet split.

### 2.2 Taxonomy Constraints

1. Codes are stable API tokens and must not be repurposed.
2. Mapping from existing rejection path to reason code must be total and deterministic.
3. If multiple rejection guards trigger, precedence order must be fixed and documented in implementation notes.
4. `rejection_reason_detail` may only use bounded enum-like tokens (e.g., `older_than_last_seen`) and must never require log inspection.

## 3) Visibility Surfaces

Rejected observation visibility must be exposed on first-class operator surfaces, not logs alone.

### 3.1 Primary Surface: Rejection Timeline Feed

A station/day queryable feed for rejected events:

- Filter keys: `station_id`, `day_utc`, optional reason code, optional mode (`live`/`replay`)
- Sort key: `evaluation_timestamp_utc` then deterministic tie-breaker
- Guarantees: stable ordering, no hidden sampling, bounded pagination

### 3.2 Secondary Surface: Ingestion Health Summary Extension

Current ingestion-health surface must add deterministic counters for selected window:

- `rejected_count_total`
- `rejected_count_by_reason_code`
- `last_rejected_evaluation_timestamp_utc`
- `last_rejected_reason_code`

This enables immediate distinction between:

- no observation evaluated,
- observations evaluated and accepted,
- observations evaluated and rejected.

### 3.3 Operator Dashboard Semantics

For each station/day status panel, include an explicit tri-state:

- `NO_OBSERVATION_EVALUATED`
- `OBSERVATION_ACCEPTED`
- `OBSERVATION_REJECTED`

Tri-state must be computable from persisted visibility events + accepted observation state only.

## 4) Persistence Requirements

### 4.1 Durability

Rejected events must persist in durable storage with retention at least equal to accepted observation audit retention for the same operational period.

### 4.2 Indexing and Queryability

Minimum query support:

- by `(station_id, day_utc)` ordered by evaluation timestamp,
- by reason code within station/day,
- by mode (`live` vs `replay`) for parity audits.

### 4.3 Idempotency and Deduplication

Persistence must support deterministic idempotent writes under reprocessing/replay restarts, using a stable event identity key derived from existing deterministic ingestion context.

### 4.4 Failure Isolation

Any persistence/serving degradation in rejected-event surfaces must not block scheduler loop or ingestion acceptance path. Visibility is non-causal and best-effort for liveness, but correctness target is eventual durable capture of all deterministic rejected outcomes.

## 5) Replay Equivalence Guarantees

### 5.1 Same Input, Same Rejection Visibility

For identical ordered replay input, replay must emit the same sequence of rejection reason codes as live deterministic ingestion logic, modulo mode tag (`replay` instead of `live`) and timestamps representing replay evaluation time.

### 5.2 Canonical Parity Comparison

Parity check key:

`(station_id, day_utc, source_observation_timestamp_utc/source_sequence_key, rejection_reason_code)`

Operator-facing parity status per station/day:

- `PARITY_MATCH`
- `PARITY_MISMATCH`
- `PARITY_INCOMPLETE` (insufficient retained data)

### 5.3 Divergence Diagnostics

When parity mismatch occurs, surface deterministic mismatch counts by reason code and first mismatch point; do not require log for first-order diagnosis.

## 6) Operator Awareness Latency Bounds

Define deterministic awareness SLO bounds from rejection decision completion:

1. **Persistence latency bound (`T_persist`)**: max allowed delay from decision to durable rejected-event record.
2. **API visibility latency bound (`T_surface`)**: max allowed delay from durability to query visibility on primary/summary surfaces.
3. **Total awareness bound (`T_awareness`)**:

`T_awareness = T_persist + T_surface`

Operational target: `T_awareness` must be bounded and strictly less than stale-ingestion alert threshold window so rejected activity becomes visible before operators infer silent outage.

## 7) Non-Causality Proof

Rejected-ingestion visibility is observational and cannot affect execution outcomes.

### 7.1 Causal Separation

- Decision authority remains in existing ingestion acceptance logic.
- Visibility event emission is downstream of decision finalization.
- No visibility field is consumed by acceptance, transition, alert, scheduler, or market evaluation logic.

### 7.2 Failure-Mode Argument

If rejection visibility write/query path fails:

- acceptance/rejection decision remains unchanged,
- scheduler cadence remains unchanged,
- parsing and ordering behavior remain unchanged,
- alert/transition semantics remain unchanged.

Therefore visibility cannot introduce false accepts, false rejects, or behavioral drift.

### 7.3 Replay Determinism Preservation

Replay must invoke identical rejection classification mapping used by live ingestion path; visibility mode flag is metadata only and does not branch acceptance logic.

---

## Acceptance Statement

When this specification is implemented, operators can deterministically distinguish:

- **No observation occurred/evaluated**, versus
- **Observation occurred and was rejected with explicit reason class**,

without inference and without log inspection.
