# VISIBILITY GAP CLOSURE SPEC (P0)

## Scope and Governance

This specification defines **design-only** visibility additions for the P0 silent-correctness zone:
**transition evaluation occurs but no transition is emitted**.

This spec is constrained to observability surfaces and persistence of visibility artifacts.
It does **not** modify execution semantics, transition logic, ingestion behavior,
alert triggering rules, scheduler cadence, or replay execution pathways.

Normative source alignment:
- Gap source: `docs/VISIBILITY_SURFACE_MAP.md` (Silent Zone #1).
- Determinism and non-causality constraints remain mandatory.

---

## 1) Non-Emission Event Model

### 1.1 Event Class

Define a deterministic observability event class:

- **`transition_non_emission_evaluated`**

This event is emitted by the visibility layer whenever transition evaluation is executed
and results in no emitted transition record.

### 1.2 Event Identity and Determinism

Each event MUST include deterministic identity attributes:

- `station_id` (string)
- `obs_timestamp_utc` (ISO8601) — timestamp of the observation being evaluated
- `evaluation_timestamp_utc` (ISO8601) — timestamp at evaluation completion
- `evaluation_seq` (monotonic per process/station/day visibility sequence)
- `day_utc` (YYYY-MM-DD)
- `source_path` (fixed enum; e.g., `emit_transition_if_changed`)

Identity uniqueness requirement:

- Unique key = (`station_id`, `obs_timestamp_utc`, `evaluation_context_key`, `source_path`)

### 1.2.1 Deterministic Event Identity Anchoring

Event identity is anchored to the **execution-evaluation instance** and MUST NOT derive from
runtime process lifecycle state.

Canonical deterministic identity tuple:

- `station_id`
- `obs_timestamp_utc`
- `evaluation_context_key`
- `source_path`

`evaluation_context_key` MUST be derived only from deterministic execution inputs that are
already replayable for the same station/day evaluation path.

Identity anchoring guarantees:

1. Process restart MUST NOT generate a new logical identity for the same evaluated instance.
2. Replay MUST reproduce the identical identity tuple for equivalent ordered inputs.
3. Sequence counters (including `evaluation_seq`) are ordering aids only and MUST NEVER be
   treated as identity authority.

Uniqueness enforcement MUST rely on the deterministic identity tuple above, not runtime
sequence counter values.

### 1.3 Decision Path Fields

The model MUST expose the full non-emission decision path deterministically:

- `transition_type_input` (nullable enum/string)
- `instant_changed` (bool)
- `settlement_changed` (bool)
- `prior_instant_bucket` (int|null)
- `candidate_instant_bucket` (int|null)
- `prior_settlement_bucket` (int|null)
- `candidate_settlement_bucket` (int|null)
- `decision_outcome` (enum, fixed to `non_emission` for this event class)

### 1.4 Explicit Reason Coding

Exactly one reason code MUST be present:

- `reason_code` (enum)
  - `NO_TRANSITION_TYPE`
  - `NO_EFFECTIVE_BUCKET_CHANGE`

Reason-code derivation rules:

1. If transition type is missing/null at evaluation exit -> `NO_TRANSITION_TYPE`.
2. Else if both effective change flags are false (`instant_changed=false` and `settlement_changed=false`) -> `NO_EFFECTIVE_BUCKET_CHANGE`.
3. No other reason codes are valid for P0.

### 1.5 Causality Guard Fields

To prove observability non-causality, event carries metadata:

- `execution_side_effects_applied` (bool, fixed `false`)
- `emitted_transition_count_delta` (int, fixed `0`)
- `alert_count_delta` (int, fixed `0`)

These fields are visibility assertions only; they do not control execution.

---

## 2) Visibility Surface Definition

### 2.1 Primary Surfaces

Define two deterministic read surfaces:

1. **Timeline surface**
   - Endpoint class: `/observability/transitions/non-emissions`
   - Function: append-only query stream of non-emission evaluations.
2. **Station summary surface**
   - Endpoint class: `/observability/station-summary`
   - Additive fields only:
     - `last_non_emission_reason_code`
     - `last_non_emission_eval_utc`
     - `non_emission_count_day`

### 2.2 Query Contract

Timeline surface query parameters (deterministic and bounded):

- required: `station_id`, `day_utc`
- optional: `from_ts`, `to_ts`, `limit` (default fixed; max fixed)

Ordering requirements:

- Primary sort: `evaluation_timestamp_utc` ascending
- Tie-breaker: `evaluation_seq` ascending

### 2.3 Operator Render Semantics

Each row MUST present:

- evaluation occurred (boolean rendered from presence)
- reason code
- decision path fields (`transition_type_input`, change flags, bucket comparisons)
- replay marker (`origin = live | replay`)

### 2.4 Backward Compatibility

Existing transition and alert observability endpoints remain unchanged.
This is additive visibility and must not alter existing schemas except
for additive optional fields in summary surfaces.

---

## 3) Persistence Requirements

### 3.1 Storage Class

Persist non-emission events in a dedicated append-only table/collection:

- logical name: `transition_non_emission_events`

### 3.2 Minimal Schema Requirements

Required columns/fields:

- Identity fields from §1.2
- Decision path fields from §1.3
- `reason_code` from §1.4
- `origin` enum: `live` or `replay`
- `recorded_at_utc` (write timestamp)

### 3.3 Integrity Constraints

- Enforce uniqueness on the identity key in §1.2.
- Reject writes with missing reason code.
- Reject writes where reason code conflicts with decision flags.

### 3.4 Retention and Auditability

- Retention window MUST be at least equal to transition event retention window.
- Event records are immutable after write (no updates, no deletes except policy expiry).
- Soft deletion is disallowed within retention horizon.

### 3.5 Failure Semantics

Visibility persistence failures MUST NOT block execution.
If persistence fails:

- execution outcome remains unchanged,
- failure is logged as observability-path error,
- no retry logic may alter execution timing semantics.

---

## 4) Replay Equivalence Guarantees

### 4.1 Replay Visibility Parity

For identical ordered input observations, replay MUST produce the same
non-emission reason sequence as live evaluation for the same station/day,
modulo `origin` and write timestamps.

### 4.2 Equivalence Definition

Define parity tuple per event:

- (`station_id`, `obs_timestamp_utc`, `reason_code`, `transition_type_input`,
  `instant_changed`, `settlement_changed`, `candidate_instant_bucket`,
  `candidate_settlement_bucket`)

Replay-equivalent visibility holds when parity tuple sequence is equal
between live and replay for same input ordering.

### 4.3 Deterministic Reconciliation Surface

Provide deterministic compare surface:

- Endpoint class: `/observability/replay-parity/non-emissions`
- Output:
  - `parity_status`: `MATCH` | `MISMATCH`
  - mismatch counts and first mismatch index
  - sample mismatch tuple pair (live vs replay)

### 4.4 Non-Interference Clause

Replay parity computation is read-only over persisted artifacts and replay output.
It must not mutate execution state, transition history, or alert surfaces.

---

## 5) Operator Awareness Latency Bounds

### 5.1 Latency SLO Definition

Define awareness latency for non-emission events:

- `awareness_latency_ms = first_surface_availability_ts - evaluation_timestamp_utc`

### 5.2 Bound Requirements

P0 deterministic bounds:

1. **Write bound**: non-emission event visible in persistence-backed timeline
   no later than one scheduler interval after evaluation completion.
2. **Summary bound**: station summary additive fields updated no later than
   one scheduler interval after timeline persistence.
3. **Replay bound**: replay-origin non-emission visibility available by replay
   completion response finalization.

### 5.3 Degradation Signaling

If bounds are exceeded, surface deterministic status token in observability:

- `visibility_latency_state`:
  - `WITHIN_BOUND`
  - `BOUND_EXCEEDED`

This token is informational only and non-causal.

---

## 6) Non-Causality Proof

### 6.1 Architectural Separation

Execution authority remains unchanged:

- transition emission decisions continue in execution domain,
- visibility layer subscribes to post-decision evaluation outputs,
- visibility writes occur after decision finalization.

Therefore visibility cannot influence whether a transition is emitted.

### 6.2 Dataflow Directionality

Allowed flow:

- execution decision -> visibility event materialization -> read surfaces

Disallowed flow:

- visibility state -> execution branching
- visibility latency/health -> transition/alert outcomes

### 6.3 Invariance Assertions

For every evaluation instance in P0 scope, the following invariants hold:

1. `emitted_transition_count_delta == 0`
2. `alert_count_delta == 0`
3. transition decision outcome equals baseline outcome absent visibility layer

These invariants certify that observability is truth-exposing, not truth-shaping.

### 6.4 Governance Check

This specification satisfies Phase 3 non-causality doctrine by making
non-emission explicit while preserving immutable execution semantics.

---

## Acceptance Criteria (Design-Level)

P0 visibility gap closure is complete when all are true:

1. Every transition evaluation resulting in non-emission yields one deterministic
   `transition_non_emission_evaluated` record with a valid reason code.
2. Operators can query per-station/day non-emission timelines and summaries
   without relying on absence inference.
3. Replay exposes equivalent non-emission reason sequences via canonical
   parity surface.
4. No execution outputs (transition emissions, alerts, ingestion acceptance,
   scheduler cadence) change under this design.
