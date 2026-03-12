# Hydration Health Classification

`classify_hydration_health(ladder_cache_snapshot)` produces machine-readable
health signals from `build_ladder_cache_snapshot()` output.

## Signals

Per station, classifier emits one `status`:

- `DISCOVERY_MISSING`
  - `hydration_state.series_discovered` is false.
- `CACHE_MISSING`
  - discovery exists but `hydration.cache_present` is false.
- `CACHE_STALE`
  - cache exists, but `ladder_cache_age_seconds` is missing or exceeds
    `HYDRATION_LADDER_CACHE_STALE_THRESHOLD_SECONDS` (default `1800`).
- `PARTIAL_HYDRATION`
  - cache exists and is fresh, but `hydration_state.cache_valid` is false.
- `HEALTHY`
  - discovery exists, cache exists, cache age is within threshold, and
    cache is valid.

## Response Shape

```json
{
  "healthy": true,
  "station_health": [
    {
      "station": "KJFK",
      "status": "HEALTHY",
      "cache_age_seconds": 12
    }
  ]
}
```

- `healthy` is `true` when all stations are `HEALTHY` (or no station rows exist).
- `station_health` is sorted by station code.

## System Health Integration

`compute_system_health_snapshot()` attaches this payload under:

- `hydration.health`

This allows dashboards and alert pipelines to consume hydration drift signals
without additional Kalshi API calls.


## Queue Observability

Hydration execution is queue-driven for live poll/alert flows.
Operational queue state is surfaced in existing observability payloads:

- `/observability/runtime-authority-snapshot`
  - `hydration_queue.queue_depth`
  - `hydration_queue.queued_stations`
  - `hydration_queue.stations_in_backoff`
  - `hydration_queue.next_backoff_expiry`
  - `hydration_queue.last_hydration_request_ts`
  - `hydration_queue.backoff_stations`
  - `hydration_stall_signal.hydration_stall_condition`
  - `hydration_stall_signal.hydration_reason`
  - `hydration_stall_signal.hydration_cache_not_written`
  - `hydration_stall_signal.transitions_seen_today`
  - `hydration_stall_signal.alerts_sent_today`
- `/observability/hydration-prerequisite-runtime`
  - `hydration_queue` snapshot for the same process epoch.
- `/integrity/alert_pipeline`
  - `hydration_queue` snapshot including `stations_in_backoff` and `next_backoff_expiry`.
  - `hydration_stall_signal` snapshot and `HYDRATION_STALL_CONDITION` finding when the derived condition is true.

These fields allow operators to distinguish cache-policy usability issues from
queue transport/backoff pressure without introducing new write-capable surfaces.
Queue observability is read-only telemetry and not a hydration control plane.
