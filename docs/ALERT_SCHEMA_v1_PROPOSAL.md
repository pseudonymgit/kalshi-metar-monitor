# ALERT SCHEMA v1.0 PROPOSAL (DESIGN ONLY)

Status: Design proposal only (no implementation changes).

## Goals
- Deterministic alert records.
- Replay-verifiable equivalence.
- Explicit suppression transparency.
- Execution-domain safety evidence.

## Canonical Payload Shape

```json
{
  "schema_version": "1.0.0",
  "station": "KDEN",
  "transition_type": "LADDER_BUCKET_TRANSITION",
  "decision_type": "alert_emitted",
  "pre_state_bucket": 71,
  "post_state_bucket": 72,
  "observation_timestamp": "2026-03-02T16:11:00+00:00",
  "observation_temperature": 72.1,
  "deterministic_cause_reference": {
    "source": "integer_cross",
    "source_event_id": "station-day-cycle deterministic key",
    "evaluation_trace_id": "deterministic runtime trace token"
  },
  "eligibility_evaluation_result": {
    "outcome": "ALERTABLE"
  },
  "suppression_reason": "none",
  "execution_domain_confirmation": {
    "domain": "production",
    "observability_path": false,
    "domain_guard_passed": true
  },
  "replay_equivalence_hash": "design: sha256(canonicalized_fields_v1)"
}
```

## Invariant-Conformant Decision Examples

```json
{
  "decision_type": "alert_emitted",
  "suppression_reason": "none"
}
```

```json
{
  "decision_type": "suppressed",
  "suppression_reason": "outside_alert_window"
}
```

## Required Fields

- `schema_version` (string; required)
- `station` (ICAO uppercase; required)
- `transition_type` (enum; required; ladder state transition class only)
- `decision_type` (enum; required; `alert_emitted` or `suppressed`)
- `pre_state_bucket` (int or null if initial state; required)
- `post_state_bucket` (int; required)
- `observation_timestamp` (ISO-8601 UTC; required)
- `observation_temperature` (number; required)
- `deterministic_cause_reference` (object; required)
- `eligibility_evaluation_result` (object; required)
- `suppression_reason` (string; required, explicit, never implicit; `none` when `decision_type=alert_emitted`)
- `execution_domain_confirmation` (object; required)
- `replay_equivalence_hash` (string; required)

## Deterministic Semantics

1. `transition_type` must encode only ladder transition class and never encode suppression outcomes.
2. `decision_type` records execution outcome: `alert_emitted` or `suppressed`.
3. Suppression is a decision outcome, not a transition type.
4. `suppression_reason` must always be present:
   - `none` for emitted alerts.
   - explicit reason token for suppressed decisions (e.g., `outside_alert_window`, `no_eligible_market`, `terminal_state`, `rate_limited`).
5. Hard consistency invariants:
   - If `decision_type == "alert_emitted"`, `suppression_reason` MUST equal `none`.
   - If `decision_type == "suppressed"`, `suppression_reason` MUST NOT equal `none`.
6. `deterministic_cause_reference` must bind to existing deterministic execution artifacts (no random UUID authority).
7. `replay_equivalence_hash` must be computed from a canonical field-order serialization excluding non-deterministic metadata.
8. Any field omitted by producer must fail schema validation.

## Transition Type Enum (proposed)

- `LADDER_BUCKET_TRANSITION`
- `LADDER_BUCKET_ENTRY_INITIAL`

## Decision Type Enum (proposed)

- `alert_emitted`
- `suppressed`

## Replay Equivalence Scope

- Replay equivalence is defined over transition + decision outcomes and deterministic observation context.
- Replay equivalence does NOT require parity of diagnostic breakdown fields (for example, rejection breakdown counts), which are informational.

## Replay Equivalence Hash Design (proposal)

`replay_equivalence_hash = SHA256(canonical_json({
schema_version,
station,
transition_type,
decision_type,
pre_state_bucket,
post_state_bucket,
observation_timestamp,
observation_temperature,
deterministic_cause_reference,
eligibility_evaluation_result.outcome,
suppression_reason
}))`

Notes:
- Canonical JSON requires deterministic key ordering and normalized numeric/string encoding.
- Excludes transport metadata, write timestamps, database row IDs.
- Excludes `execution_domain_confirmation.domain`; execution domain confirmation remains required metadata but is not an equivalence input.

## Compatibility + Versioning

- Version namespace: SemVer-like schema contract (`1.0.0`).
- Backward-compatible additive fields: minor bump.
- Required field changes or semantic reinterpretation: major bump.
- Producer and replay engine must publish accepted schema versions.
