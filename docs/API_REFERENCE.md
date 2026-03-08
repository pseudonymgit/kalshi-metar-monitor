# API Reference (Canonical Control Surface)

This document is the single source of truth for production HTTP endpoints.

All endpoint definitions below are derived from `app.py` Flask route decorators.

## Domain model mapping

- **Execution**: affects ingestion, transition production, or alert-emission flow.
- **Observability**: visibility-only reads from persisted runtime state, SQLite audit rows, and cached market data.
- **Simulation**: deterministic test injection paths.
- **Operations**: scheduler/process lifecycle controls.
- **Debug**: internal inspection and developer diagnostics.
- **Kalshi Integration**: explicit Kalshi API integration surface.

Observability guarantee:
- `/observability/*` endpoints are **observability-only** and MUST NOT trigger live Kalshi API calls.
- Endpoints exposing market ladder context rely on previously hydrated cached market data.

---

## Endpoint
`GET /`

### Domain
Operations

### Purpose
Service liveness health check.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Confirms service process availability before deeper checks.

### Safety Notes
No execution effect.

---

## Endpoint
`GET /kalshi/ping`

### Domain
Kalshi Integration

### Purpose
Checks Kalshi public API reachability.

### Execution Authority
read-only

### Data Source
Live Kalshi

### Trading Relevance
Validates upstream market connectivity for operators.

### Safety Notes
External call only; does not emit transitions/alerts by itself.

---

## Endpoint
`GET /kalshi/markets`

### Domain
Kalshi Integration

### Purpose
Returns public market listing payload (`limit` query parameter).

### Execution Authority
read-only

### Data Source
Live Kalshi

### Trading Relevance
Used to inspect currently listed market inventory.

### Safety Notes
External read-only call.

---

## Endpoint
`POST /kalshi/check`

### Domain
Kalshi Integration

### Purpose
Runs manual public market change-detection pass.

### Execution Authority
execution-affecting

### Data Source
Live Kalshi

### Trading Relevance
Lets operators force market-change detection when validating station activation and listing drift.

### Safety Notes
May update internal market-monitor baseline/check state.

---

## Endpoint
`GET /kalshi/snapshot`

### Domain
Kalshi Integration

### Purpose
Builds structured ladder snapshot for a station (`station`, optional `type=HIGH,LOW`).

### Execution Authority
read-only

### Data Source
Live Kalshi

### Trading Relevance
Used to inspect ladder state for active market evaluation.

### Safety Notes
Requires `station`; performs live market lookup.

---

## Endpoint
`POST /kalshi/composed`

### Domain
Kalshi Integration

### Purpose
Manually triggers composed weather-market alert flow for a station.

### Execution Authority
execution-affecting

### Data Source
Live Kalshi

### Trading Relevance
Operator control for manual composed alert path verification.

### Safety Notes
Can emit live delivery attempts depending on environment/webhook configuration.

---

## Endpoint
`GET /kalshi/health`

### Domain
Kalshi Integration

### Purpose
Reports active stations and recent Kalshi-composed/check summaries.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Quick view into active market-monitor control state.

### Safety Notes
No live call required.

---

## Endpoint
`GET /debug/version`

### Domain
Debug

### Purpose
Returns module/config diagnostics and monitor capability markers.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Debug-only; supports environment and deployment verification.

### Safety Notes
Internal diagnostics endpoint.

---

## Endpoint
`GET /debug/alerts`

### Domain
Debug

### Purpose
Returns recent alert audit rows (`limit` query parameter).

### Execution Authority
read-only

### Data Source
SQLite audit

### Trading Relevance
Used to audit emitted/suppressed alert records and investigate missed alerts.

### Safety Notes
Debug surface; no execution effect.

---

## Endpoint
`GET /debug/ladder-state`

### Domain
Debug

### Purpose
Returns in-memory ladder-state snapshot from Kalshi monitor internals.

### Execution Authority
read-only

### Data Source
Cached market data

### Trading Relevance
Supports diagnosis of ladder context memory and market-state alignment.

### Safety Notes
Snapshot is cache-backed internal state.

---

## Endpoint
`GET /metar/window`

### Domain
Execution

### Purpose
Performs strict-source window ingest for one station and returns latest-known observation plus ingest counts.

### Execution Authority
execution-affecting

### Data Source
Runtime state

### Trading Relevance
Operator tool to force deterministic ingest window processing for a station.

### Safety Notes
Requires `icao`; rejects non-positive `minutes`.

---

## Endpoint
`GET /metar/latest`

### Domain
Execution

### Purpose
Returns latest METAR state for a station.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Used to check current observed temperature/state for a station.

### Safety Notes
Requires `icao`.

---

## Endpoint
`GET /metar/multi`

### Domain
Execution

### Purpose
Fetches current observations for multiple stations (`icaos` query parameter).

### Execution Authority
execution-affecting

### Data Source
Runtime state

### Trading Relevance
Operator shortcut to refresh/check a station set.

### Safety Notes
Requires at least one valid ICAO.

---

## Endpoint
`GET /metar/watchlist`

### Domain
Operations

### Purpose
Returns configured station watchlist.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Shows current operational station filter inputs.

### Safety Notes
Market-derived station authority still governs execution intent.

---

## Endpoint
`POST /metar/watchlist`

### Domain
Operations

### Purpose
Replaces configured watchlist via JSON payload.

### Execution Authority
execution-affecting

### Data Source
Synthetic input

### Trading Relevance
Allows operator control over watchlist filter set.

### Safety Notes
Watchlist mutation affects runtime ingest scope; invalid payload returns 400.

---

## Endpoint
`GET /metar/metrics`

### Domain
Observability

### Purpose
Returns polling/timeout monitoring counters.

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Confirms scheduler/ingestion heartbeat and timeout patterns.

### Safety Notes
No execution effect.

---

## Endpoint
`GET /metrics/retention`

### Domain
Observability

### Purpose
Returns alert-retention metrics.

### Execution Authority
observability-only

### Data Source
SQLite audit

### Trading Relevance
Shows audit-retention health for historical alert analysis.

### Safety Notes
Read-only retention inspection.

---

## Endpoint
`POST /metrics/prune`

### Domain
Operations

### Purpose
Triggers retention pruning of old alert records.

### Execution Authority
execution-affecting

### Data Source
SQLite audit

### Trading Relevance
Maintains alert-audit storage hygiene.

### Safety Notes
Destructively removes old retained rows per configured prune policy.

---

## Endpoint
`GET /observability/transitions`

### Domain
Observability

### Purpose
Returns transition history (`station`, `limit`).

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Primary operator endpoint to confirm `settlement_up` events and transition chronology.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/ingestion-health`

### Domain
Observability

### Purpose
Returns per-station freshness/staleness classification.

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Used to identify stalled or stale station ingestion before trust decisions.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/station-summary`

### Domain
Observability

### Purpose
Combines ingestion-health and current epoch fields into per-station summary rows.

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Operator summary endpoint to confirm settlement state and reversion markers per station.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/current-epochs`

### Domain
Observability

### Purpose
Returns current settlement epoch summaries with compact field set.

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Used to confirm `settlement_up`, `reversion_occurred`, and `first_reversion_timestamp_utc` for live structural-event tracking.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/day-structure`

### Domain
Observability

### Purpose
Returns station-day structural summaries (epoch counts, latest transition, reversion stats).

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Supports detection of `goldilocks_reversion` patterns and station-day structure verification.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/market-coverage`

### Domain
Observability

### Purpose
Returns market-coverage rows by station.

### Execution Authority
observability-only

### Data Source
Cached market data

### Trading Relevance
Used to verify which stations/market types currently have coverage.

### Safety Notes
Observability-only; relies on prior market-cache hydration.

---

## Endpoint
`GET /observability/trader-dashboard`

### Domain
Observability

### Purpose
Returns trader-oriented summary rows.

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Single endpoint for high-level station/epoch operational review.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/alert-preview`

### Domain
Observability

### Purpose
Returns recent alert preview rows with alert-context extraction (`station`, `limit`).

### Execution Authority
observability-only

### Data Source
SQLite audit

### Trading Relevance
Used to audit emitted alert context and identify potential missed/incorrect alert behavior.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/alert-diagnostics`

### Domain
Observability

### Purpose
Returns deterministic diagnostic rows about alert pipeline decisions.

### Execution Authority
observability-only

### Data Source
Runtime state

### Trading Relevance
Used to inspect suppression/no-fire reasons and diagnose missed alerts.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/alert-fire-audit`

### Domain
Observability

### Purpose
Returns alert fire-audit rows for operator review.

### Execution Authority
observability-only

### Data Source
SQLite audit

### Trading Relevance
Used to audit whether expected alert fires occurred for transitions.

### Safety Notes
Observability-only; no live Kalshi calls.

---

## Endpoint
`GET /observability/runtime-authority-snapshot`

### Domain
Observability

### Purpose
Returns a bounded, deterministic runtime authority snapshot for production diagnosis.

### Execution Authority
observability-only

### Data Source
Runtime state + SQLite audit

### Trading Relevance
Provides execution-truth evidence (scheduler health, hydration cache state, transitions, alerts, DB path/existence) without granting mutation authority.

### Safety Notes
Read-only, bounded payload, and no execution mutation.

### Observability Contract

| field | type | description |
| --- | --- | --- |
| `hydration_queue.stations_in_backoff` | `integer` | number of stations currently under hydration backoff |
| `hydration_queue.next_backoff_expiry` | `number \| null` | earliest timestamp when any hydration backoff expires |
| `hydration_stall_signal` | `object` | hydration stall observability signal |
| `hydration_stall_signal.hydration_stall_condition` | `boolean` | indicates hydration stall condition |

Stall predicate (exact):

`hydration_cache_not_written`
`AND transitions_seen_today > 0`
`AND alerts_sent_today == 0`

---

## Endpoint
`GET /integrity/alert_pipeline`

### Domain
Observability

### Purpose
Returns deterministic integrity findings for transitions, evaluations, hydration state, and alert emission consistency.

### Execution Authority
observability-only

### Data Source
Runtime state + SQLite audit

### Trading Relevance
Used to verify that transitions, hydration readiness, and alert emission remain causally consistent.

### Safety Notes
Read-only integrity diagnostics; no worker execution, scheduler execution, or transition/alert mutation.

### Observability Contract

| field | type | description |
| --- | --- | --- |
| `hydration_queue.stations_in_backoff` | `integer` | number of stations currently under hydration backoff |
| `hydration_queue.next_backoff_expiry` | `number \| null` | earliest timestamp when any hydration backoff expires |
| `hydration_stall_signal` | `object` | hydration stall observability signal |
| `hydration_stall_signal.hydration_stall_condition` | `boolean` | indicates hydration stall condition |

Stall predicate (exact):

`hydration_cache_not_written`
`AND transitions_seen_today > 0`
`AND alerts_sent_today == 0`

---

## Endpoint
`GET /metar/status`

### Domain
Operations

### Purpose
Returns scheduler lifecycle and loop timestamps.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Confirms poll loop activity before diagnosing data/alert gaps.

### Safety Notes
No execution mutation.

---

## Endpoint
`POST /metar/simulate-ladder`

### Domain
Simulation

### Purpose
Injects synthetic station temperature for deterministic ladder-transition testing.

### Execution Authority
simulation-only

### Data Source
Synthetic input

### Trading Relevance
Operator testing path to validate transition and alert logic behavior.

### Safety Notes
Bypasses station-local alert window for simulation; optional `deliver=true` attempts webhook delivery.

---

## Endpoint
`POST /metar/start`

### Domain
Operations

### Purpose
Starts METAR scheduler.

### Execution Authority
execution-affecting

### Data Source
Runtime state

### Trading Relevance
Operator control to initiate polling execution.

### Safety Notes
Lifecycle control endpoint.

---

## Endpoint
`POST /metar/stop`

### Domain
Operations

### Purpose
Stops METAR scheduler.

### Execution Authority
execution-affecting

### Data Source
Runtime state

### Trading Relevance
Operator control to pause polling execution.

### Safety Notes
Lifecycle control endpoint.

---

## Endpoint
`POST /metar/test-alert`

### Domain
Simulation

### Purpose
Injects synthetic temperature event and allows alert delivery attempt.

### Execution Authority
simulation-only

### Data Source
Synthetic input

### Trading Relevance
Used to validate webhook delivery plumbing and alert payload format.

### Safety Notes
Testing endpoint; can emit delivery attempts.

---

## Endpoint
`POST /metar/force-poll`

### Domain
Execution

### Purpose
Forces one immediate poll cycle and returns before/after counters.

### Execution Authority
execution-affecting

### Data Source
Runtime state

### Trading Relevance
Manual operator trigger to process ingestion immediately.

### Safety Notes
Bypasses normal cadence by initiating immediate poll pass.

---

## Endpoint
`POST /debug/replay`

### Domain
Debug

### Purpose
Runs deterministic replay for `station` + `date_local`.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Used to validate replay determinism against live-produced transitions.

### Safety Notes
Debug/replay endpoint; requires valid `YYYY-MM-DD`.

---

## Endpoint
`GET /debug/state`

### Domain
Debug

### Purpose
Returns current in-memory monitor state snapshot.

### Execution Authority
read-only

### Data Source
Runtime state

### Trading Relevance
Low-level inspection during incident/debug workflows.

### Safety Notes
Debug-only internal visibility endpoint.
