# Runtime Authority Diagnosis — 2026-03-02 (KNYC, KPHL)

Authoritative source used: `GET /observability/runtime-authority-snapshot` only.

## KNYC

- scheduler_state
  - `scheduler_running: true`
  - `last_poll_utc: null`
  - station row: `status: "stale"`, `status_reason: "no_accepted_observation"`, `latest_accepted_observation_utc: null`, `latest_poll_utc: null`
  - ingestion health classification: **stale / no_accepted_observation**
- hydration_state
  - `cache_present: false`
  - `state_key_count: 0`, `state_keys: []`
  - hydration readiness: **not ready**
- transitions_present
  - `latest_transitions.count: 0`
  - `latest_transitions.rows: []`
  - `settlement_up` present: **no**
  - bucket progression for 2026-03-02: **not present in bounded runtime snapshot rows**
- alerts_present
  - `latest_alerts.count: 0`
  - `latest_alerts.rows: []`
  - `composed_alert_sent` exists: **no evidence in runtime snapshot rows**
- FIRST deterministic stopping stage
  - **Stage 1 — Observation acceptance**
  - exact blocking condition:
    - expected condition true: scheduler loop is running (`scheduler_running: true`)
    - downstream condition false: no accepted observation exists (`latest_accepted_observation_utc: null`, `status_reason: "no_accepted_observation"`)

## KPHL

- scheduler_state
  - `scheduler_running: true`
  - `last_poll_utc: null`
  - station row: `status: "stale"`, `status_reason: "no_accepted_observation"`, `latest_accepted_observation_utc: null`, `latest_poll_utc: null`
  - ingestion health classification: **stale / no_accepted_observation**
- hydration_state
  - `cache_present: false`
  - `state_key_count: 0`, `state_keys: []`
  - hydration readiness: **not ready**
- transitions_present
  - `latest_transitions.count: 0`
  - `latest_transitions.rows: []`
  - `settlement_up` present: **no**
  - bucket progression for 2026-03-02: **not present in bounded runtime snapshot rows**
- alerts_present
  - `latest_alerts.count: 0`
  - `latest_alerts.rows: []`
  - `composed_alert_sent` exists: **no evidence in runtime snapshot rows**
- FIRST deterministic stopping stage
  - **Stage 1 — Observation acceptance**
  - exact blocking condition:
    - expected condition true: scheduler loop is running (`scheduler_running: true`)
    - downstream condition false: no accepted observation exists (`latest_accepted_observation_utc: null`, `status_reason: "no_accepted_observation"`)

## Shared supporting runtime fields

- `scheduler_health_snapshot.scheduler_running: true`
- `scheduler_health_snapshot.last_poll_utc: null`
- `scheduler_health_snapshot.stations[0].latest_accepted_observation_utc: null`
- `scheduler_health_snapshot.stations[0].status: "stale"`
- `scheduler_health_snapshot.stations[0].status_reason: "no_accepted_observation"`
- `hydration_snapshot.stations.<station>.cache_present: false`
- `latest_transitions.count: 0`
- `latest_alerts.count: 0`
- `db.exists: false`
