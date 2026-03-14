# SCHEDULER VISIBILITY SPEC

## Purpose and Scope

This specification defines deterministic **operator visibility design** for scheduler health in production.
It resolves the scheduler interpretation gap identified in `docs/archive/planning/VISIBILITY_SURFACE_MAP.md` Silent Zone #4.

This document is design-only and explicitly non-causal:

- No scheduler cadence changes
- No polling logic changes
- No retry/throttle policy changes
- No ingestion/transition behavior changes
- No execution timing changes

## 1) Scheduler State Classification Model

### 1.1 Inputs (Read-Only Signals)

The classification model consumes only existing or visibility-layer-derived scheduler signals:

- `last_loop_utc` (timestamp of latest completed scheduler loop)
- `poll_interval_seconds` (configured cadence target)
- `loop_counter` (monotonic loop completion count)
- `last_obs_accepted_utc` (latest accepted observation timestamp for station or global scope)
- `classification_evaluated_utc` (timestamp of health computation)

No control-plane signal is written back to scheduler execution.

### 1.2 Derived Durations

At classification time:

- `loop_age_s = now_utc - last_loop_utc`
- `obs_age_s = now_utc - last_obs_accepted_utc` (if available)
- `cadence_ratio = loop_age_s / poll_interval_seconds`

### 1.3 Deterministic State Selection Order

To avoid overlap ambiguity, states are selected in strict priority order:

1. `STALLED_EXECUTION`
2. `DEGRADED_CADENCE`
3. `DELAYED_POLLING`
4. `HEALTHY_POLLING`
5. `OBSERVATION_STARVATION` (orthogonal overlay; see taxonomy)

Primary scheduler state is one of the first four.
`OBSERVATION_STARVATION` is computed independently and emitted as an additive condition.

## 2) Deterministic Health State Taxonomy

### 2.1 Primary Scheduler States

| State | Deterministic Rule | Operator Meaning |
|---|---|---|
| `HEALTHY_POLLING` | `cadence_ratio <= 1.25` and loop counter advances between consecutive evaluations | Scheduler running at expected cadence |
| `DELAYED_POLLING` | `1.25 < cadence_ratio <= 2.5` and loop counter advances eventually | Scheduler active, but behind target interval |
| `DEGRADED_CADENCE` | `2.5 < cadence_ratio <= 4.0` and loop counter still advances (non-zero progress) | Scheduler materially behind; degraded service quality |
| `STALLED_EXECUTION` | `cadence_ratio > 4.0` **or** loop counter unchanged for `> 4 * poll_interval_seconds` | Scheduler loop execution appears stopped |

Thresholds are design constants for visibility interpretation, not execution control values.

### 2.2 Overlay Condition: Observation Starvation

| Overlay | Deterministic Rule | Operator Meaning |
|---|---|---|
| `OBSERVATION_STARVATION=true` | Primary state is not `STALLED_EXECUTION` **and** `obs_age_s > starvation_threshold_s` where `starvation_threshold_s = max(3 * poll_interval_seconds, 300)` | Scheduler appears to run, but no accepted observations are arriving |

This overlay separates "nothing happening" from "cannot observe new data".

### 2.3 Deterministic Output Schema (Design Contract)

Each visibility record must include:

- `primary_state` (enum)
- `observation_starvation` (boolean)
- `loop_age_s`
- `obs_age_s` (nullable)
- `poll_interval_seconds`
- `cadence_ratio`
- `evaluated_utc`
- `state_reason_code` (stable token, e.g., `RATIO_GT_2P5_LE_4P0`)

## 3) Visibility Surfaces

### 3.1 Required Surfaces

Design requires first-class scheduler-health exposure in:

1. `/metar/status` (single-shot current state)
2. `/observability/ingestion-health` (state plus starvation overlay)
3. `/observability/scheduler-health` (new canonical scheduler visibility surface)
4. Trader-facing summary surface (read-only condensation of canonical state)

### 3.2 Surface Semantics

- `/observability/scheduler-health` is canonical and authoritative for classification.
- Other surfaces may mirror or summarize but must preserve identical enum/value semantics.
- No surface may infer alternate ad hoc states from raw timestamps.

### 3.3 Operator Read Model

Every surface exposing scheduler health must answer directly:

- "Scheduler running or stalled?" via `primary_state`
- "Running but not seeing data?" via `observation_starvation`
- "How late?" via `loop_age_s` and `cadence_ratio`

## 4) Persistence Requirements

### 4.1 Snapshot Persistence

Persist latest scheduler visibility snapshot with:

- full deterministic output schema
- monotonic `version`
- write timestamp

### 4.2 Timeline Persistence

Persist state transitions for audit/replay-adjacent operations:

- prior state
- new state
- reason code
- classification boundary timestamp
- source markers used for computation (`last_loop_utc`, `loop_counter`, `last_obs_accepted_utc`)
- ordering/presentation timestamps (non-identity)

### Deterministic Scheduler State Transition Identity

Canonical logical transition identity is the tuple:

- `prior_state`
- `new_state`
- `classification_boundary_timestamp`
- `poll_interval_seconds`
- `classification_context_key`

`classification_context_key` is derived only from deterministic replayable scheduler inputs:

- `last_loop_utc`
- `loop_counter`
- `poll_interval_seconds`

Deterministic identity requirements:

- recomputation MUST NOT create new logical transitions when the identity tuple is unchanged
- process restart MUST reproduce identical identity for the same logical transition
- replay MUST generate identical transition identity for equivalent scheduler inputs
- timestamps used only for presentation/order are NOT identity authority

Persistence layer MUST deduplicate scheduler transition rows using the full identity tuple.
Identity authority is the tuple above; ordering timestamps are for chronology/display only.

### 4.3 Retention

- High-resolution state timeline retained for minimum 7 days
- Roll-up summaries retained for minimum 30 days

Retention is visibility-only and must not gate execution.

## 5) Awareness Latency Bounds

### 5.1 Computation Cadence

Visibility recomputation target: every scheduler loop completion and every health-query read.

### 5.2 Maximum Awareness Latency

For each state class, worst-case awareness latency from underlying condition onset:

- `HEALTHY_POLLING`: <= `1 * poll_interval_seconds + query_latency`
- `DELAYED_POLLING`: <= `1 * poll_interval_seconds + query_latency`
- `DEGRADED_CADENCE`: <= `1 * poll_interval_seconds + query_latency`
- `STALLED_EXECUTION`: <= `4 * poll_interval_seconds + query_latency`
- `OBSERVATION_STARVATION`: <= `starvation_threshold_s + query_latency`

### 5.3 Determinism Requirement

Given identical input timestamps/counters and poll interval, classification output must be identical.

## 6) Replay Interaction Rules

### 6.1 Isolation Rule

Replay operations must not mutate live scheduler-health classification history.
Replay-visible computations are labeled `context=replay` and isolated from live surfaces.

### 6.2 Live Surface Integrity

During replay windows:

- live scheduler primary state continues to reflect live loop signals only
- replay status cannot overwrite live `primary_state` or starvation overlay

### 6.3 Comparative Visibility

If replay produces scheduler-health records, they are queryable only through replay-scoped endpoints or replay-tagged filters.
No implicit merging with live timeline.

## 7) Non-Causality Proof

This design is non-causal by construction:

1. **Input-only dependency**: classification consumes timestamps/counters already produced by execution paths; it does not write scheduler control inputs.
2. **Read-only publication**: visibility surfaces expose computed states but provide no mutation endpoint for cadence/polling decisions.
3. **No feedback edges**: scheduler, ingestion, and transition engines do not condition behavior on visibility enums or reason codes.
4. **Replay isolation**: replay-classified records are context-scoped and cannot alter live scheduler state interpretation.

Therefore, this specification cannot alter execution behavior and only improves operator interpretability.

## Acceptance Criteria

This spec is satisfied when operators can answer, from visibility surfaces alone and without logs:

- whether scheduler is healthy/delayed/degraded/stalled
- whether scheduler is active but observations are starved
- when each state was entered and why (reason code)

This directly resolves the interpretation question:

> "Is nothing happening, or is the system unable to observe?"
