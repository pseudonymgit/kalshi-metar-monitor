# Replay Parity Validation

## Purpose

Replay parity validation verifies deterministic equivalence between:

1. Runtime transition history for a station and trading day.
2. Replay-produced transitions for the same station and trading day.

The validator answers:

> Does replay produce the exact same transition sequence that runtime produced?

## Safety Guarantees

`validate_replay_parity(station, trading_day)` is diagnostics-only and designed to avoid authoritative mutations.

Safety constraints:

- Replay runs in replay domain (`allow_alert_delivery=False`, `persist_cache=False`).
- Alert delivery is disabled.
- Cache persistence is disabled.
- Replay transition emission persistence is monkeypatched to an in-memory collector.
- Settlement epoch logging during parity validation is disabled in-memory.

This preserves runtime behavior while preventing replay-parity checks from emitting alerts or writing transition artifacts.

## Parity Rules

Parity is `true` only when all rules pass:

1. Transition count matches.
2. Transition ordering matches.
3. Transition type and structural fields match per index.
4. Timestamp ordering is non-decreasing in both sequences.

Timestamp string equality is not required; ordering integrity is required.

## Divergence Interpretation

When mismatch is found, `parity=false` and `divergence` is populated.

`divergence.type` values:

- `TIMESTAMP_ORDER_MISMATCH`: at least one sequence has non-monotonic timestamp ordering.
- `TRANSITION_COUNT_MISMATCH`: transition counts differ.
- `TRANSITION_MISMATCH`: first non-equivalent transition payload differs at the same index.

`first_divergence_index` identifies first mismatch position in chronological sequence.

`runtime_transition` and `replay_transition` include the compared transition objects at that divergence point when available.

## HTTP Endpoint

`GET /integrity/replay_parity?station=<ICAO>&day=<YYYY-MM-DD>`

Response:

```json
{
  "ok": true,
  "result": {
    "station": "KDEN",
    "trading_day": "2026-03-04",
    "runtime_transition_count": 12,
    "replay_transition_count": 12,
    "parity": true,
    "divergence": null
  }
}
```
