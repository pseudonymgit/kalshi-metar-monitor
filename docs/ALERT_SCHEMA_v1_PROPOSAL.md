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
    "outcome": "ALERTABLE",
    "markets_considered_count": 18,
    "eligible_markets_count": 3,
    "rejected_markets_count": 15,
    "rejection_breakdown": {
      "outside_price_band": 5,
      "wrong_series": 2,
      "expired_market": 4,
      "settlement_mismatch": 3,
      "unknown_reason": 1
    }
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

## Required Fields

- `schema_version` (string; required)
- `station` (ICAO uppercase; required)
- `transition_type` (enum; required)
- `pre_state_bucket` (int or null if initial state; required)
- `post_state_bucket` (int; required)
- `observation_timestamp` (ISO-8601 UTC; required)
- `observation_temperature` (number; required)
- `deterministic_cause_reference` (object; required)
- `eligibility_evaluation_result` (object; required)
- `suppression_reason` (string; required, explicit, never implicit)
- `execution_domain_confirmation` (object; required)
- `replay_equivalence_hash` (string; required)

## Deterministic Semantics

1. `suppression_reason` must always be present:
   - `none` for emitted alerts.
   - explicit reason token for suppressed decisions (e.g., `outside_alert_window`, `no_eligible_market`, `terminal_state`, `rate_limited`).
2. `deterministic_cause_reference` must bind to existing deterministic execution artifacts (no random UUID authority).
3. `replay_equivalence_hash` must be computed from a canonical field-order serialization excluding non-deterministic metadata.
4. Any field omitted by producer must fail schema validation.

## Transition Type Enum (proposed)

- `LADDER_BUCKET_TRANSITION`
- `LADDER_BUCKET_ENTRY_INITIAL`
- `NO_ELIGIBLE_MARKET_SUPPRESSION`
- `HYDRATION_UNAVAILABLE_SUPPRESSION`
- `WINDOW_GATED_SUPPRESSION`
- `TERMINAL_STATE_SUPPRESSION`

## Replay Equivalence Hash Design (proposal)

`replay_equivalence_hash = SHA256(canonical_json({
schema_version,
station,
transition_type,
pre_state_bucket,
post_state_bucket,
observation_timestamp,
observation_temperature,
deterministic_cause_reference,
eligibility_evaluation_result,
suppression_reason,
execution_domain_confirmation.domain
}))`

Notes:
- Canonical JSON requires deterministic key ordering and normalized numeric/string encoding.
- Excludes transport metadata, write timestamps, database row IDs.

## Compatibility + Versioning

- Version namespace: SemVer-like schema contract (`1.0.0`).
- Backward-compatible additive fields: minor bump.
- Required field changes or semantic reinterpretation: major bump.
- Producer and replay engine must publish accepted schema versions.
