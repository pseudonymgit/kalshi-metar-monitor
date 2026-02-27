# Architecture

## Overview
Flask service on Render with two integrated runtime tracks:
- METAR ingestion and integer-cross detection.
- Kalshi weather ladder transition composition for alert output.

## Runtime Components
- `app.py`
  - HTTP routes.
  - Scheduler lifecycle hooks (autostart + manual control).

- `core/metar_monitor.py`
  - Polling scheduler, dedupe, station-local window/reset behavior.
  - Integer floor-cross detection.
  - Alert emission pipeline entrypoint.

- `core/kalshi_monitor.py`
  - Structured market snapshot building.
  - Ladder bucket transition detection.
  - Composed ladder alert rendering and dispatch.

## Scheduler Lifecycle
- Autostart gate: `METAR_AUTOSTART`.
- Startup path:
  - Preferred: `before_first_request` one-time start.
  - Fallback: guarded one-time `before_request` when first hook is unavailable.
- Idempotent start path via `ensure_scheduler_started()` / `start_scheduler()` under lock.
- Stop path joins worker thread with timeout.
- Status visibility includes `last_loop_utc` in addition to poll counters.

## Weather Ladder Alert Architecture

### Routing matrix (within station-local 11:00–19:00)

| Condition | Result |
|---|---|
| Ladder exists + transition fires | Composed ladder alert |
| Ladder exists + no transition | Nothing |
| Ladder missing + enabled | Ladder-missing alert |
| Outside window | Nothing |

Raw integer-cross temp-only alerts are removed. Composed alert pathways are the only production alert type.

### Flow
1. METAR observation ingested and station state updated.
2. Integer crossing detected against station integer memory.
3. For active station and each market type (`HIGH`, `LOW`), structured snapshot is queried.
4. Ladder transition evaluator returns `should_alert` + reason.
5. Composed ladder alert sends formatted ladder message with event link.

Concise flow:
`Integer cross detected -> Window check -> Ladder evaluation ->`
- `Transition? -> Composed alert -> Audit row`
- `Missing? -> Missing alert -> Audit row`
- `Otherwise -> No alert`

## Durable alert audit (SQLite)

- `ALERT_DB_PATH` controls audit DB location.
- Default fallback path: `/var/data/alerts.db`.
- Single-instance SQLite only.
- Persistent disk required.

Table: `alerts`
- `id INTEGER PRIMARY KEY`
- `created_utc TEXT`
- `station TEXT`
- `market_type TEXT`
- `event_ticker TEXT`
- `alert_type TEXT`
- `direction TEXT`
- `temp_f REAL`
- `bucket_index INTEGER`
- `metadata_json TEXT`

Written events:
- `ladder_transition`
- `ladder_missing`
- `composed_alert_sent`

## Structured logging contract

Allowed events:
- `EVENT integer_cross`
- `EVAL ladder_check`
- `WARN ladder_missing`
- `EVENT ladder_transition`
- `SEND composed_alert`

Logging rules:
- No per-poll logging.
- No debug prints.
- High-signal logs only.

### Bucket detection
- `less`: `temp <= cap`
- `between`: `floor <= temp < cap`
- `greater`: `temp >= floor`

### Direction resolution order
1. Transition reason (`up` / `down`).
2. Prior bucket index from scoped context memory.
3. Prior observed temperature fallback.
4. Upward default when no prior context exists.

### Next-rung distance
- Uses adjacent bucket boundary in resolved direction.
- Upward: compare against next higher rung boundary.
- Downward: compare against next lower rung boundary.
- Edge labels: `MAX REACHED` or `MIN REACHED`.

### Memory scoping
- Transition and direction context are scoped per:
  - `station`
  - `market_type`
  - `event_ticker`
- Event rollover resets effective context for new ticker to prevent stale direction memory.
- Context memory is maintained in-process and resets on application restart. It is not persisted to disk.

## State
In-memory METAR state tracks watchlist, latest obs, dedupe cursor, poll counters, timeout metrics, and last loop timestamp.

Kalshi ladder context tracks prior bucket/temperature per scoped event key for direction/distance rendering continuity.

## Deployment behavior
- Gunicorn process boot does not start polling at import time.
- Scheduler starts lazily through request lifecycle hooks when autostart is enabled.
- Manual API start/stop remains available for explicit operator control.
