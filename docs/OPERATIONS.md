# Operations

## Runtime notes (Render)

- Start command: `gunicorn app:app -t 180`
- No import-time scheduler side effects.
- Scheduler lifecycle is request-hook safe and idempotent.

## Production Authority Model

- Markets are the source of truth for active stations.
- METAR ingestion follows market availability for those stations.
- Watchlist/config controls are operational filters and cannot supersede market-derived station authority.
- `HIGH` and `LOW` monitoring are symmetric production paths.

## Deterministic Execution Notes

- Settlement bucket progression is monotonic per station-day.
- Alerts are transition-driven, never threshold/window heuristics.
- Rapid advancement/reversion sequences (Goldilocks structural events) are preserved and surfaced.
- Replay must reproduce live transitions exactly.

## Observability Isolation Model

- Observability endpoints read persisted runtime state and cached market data.
- Observability endpoints MUST NOT trigger live Kalshi API calls.
- Cache hydration is performed by ingestion/execution cycles.
- Operations runbooks must ensure cache hydration before treating observability snapshots as complete.


## Bootstrap Window Constants (Canonical)

Values below are execution constants from `core/metar_monitor.py` and are authoritative for bootstrap/lookback behavior.

| Constant | Value | Purpose |
|---|---:|---|
| `BOOTSTRAP_LOOKBACK_MINUTES` | `60` | First-contact bootstrap lookback window when no prior station timestamp exists. |
| `OVERLAP_SECONDS` | `120` | Deterministic overlap for subsequent windows to avoid late-arrival misses. |
| `FIRST_RUN_CUSHION_SEC` | `300` | First-contact cushion to absorb initial fetch edge timing. |
| `PUBLICATION_LAG_BUFFER_SECONDS` | `90` | Excludes most-recent publication edge to avoid premature reads. |
| `METAR_ACCEPTANCE_GRACE_SECONDS` | `600` (`min(900, OVERLAP_SECONDS * 5)`) | Bounded acceptance grace for delayed observations. |

## Scheduler lifecycle

Autostart:
- `METAR_AUTOSTART=true` attempts one scheduler startup on first request lifecycle pass.
- Uses `before_first_request` when available.
- Falls back to a guarded one-time `before_request` path.

Manual control:
- `POST /metar/start`
- `POST /metar/stop`

Status:
- `GET /metar/status` returns:
  - `scheduler_running`
  - `poll_count`
  - `last_poll_utc`
  - `last_loop_utc`
  - `timeout_count`
  - `last_timeout_station`
  - `last_timeout_utc`

## Endpoint reference

See `docs/API_REFERENCE.md` for canonical endpoint definitions.

Do not duplicate endpoint inventories in operational runbooks.
