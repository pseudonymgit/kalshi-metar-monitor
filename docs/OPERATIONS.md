# Operations

## Runtime notes (Render)

- Start command: `gunicorn app:app -t 180`
- No import-time scheduler side effects.
- Scheduler lifecycle is request-hook safe and idempotent.

## Production Authority Model

- Markets are the source of truth for active stations.
- METAR ingestion follows market availability for those stations.
- Watchlist/config controls are operational filters and cannot supersede market-derived station authority.
- Default runtime monitoring targets `HIGH` markets only.
- Symmetric `HIGH` and `LOW` monitoring is supported when `KALSHI_TARGET_MARKET_TYPE=HIGH,LOW`.

## Deterministic Execution Notes

- Settlement bucket progression is monotonic per station-day.
- Alerts are transition-driven, never threshold/window heuristics.
- Rapid advancement/reversion sequences (Goldilocks structural events) are preserved and surfaced.
- Replay must reproduce live transitions exactly.

## Observability Isolation Model

- Observability endpoints read persisted runtime state and cached market data. Persisted-vs-live authority is still mixed in some payloads/endpoints, so operators must treat endpoint semantics as surface-specific rather than globally reconciled.
- Observability endpoints MUST NOT trigger live Kalshi API calls.
- Flask startup lifecycle hooks may still run on a first-request path (for example, `before_first_request` can trigger scheduler/bootstrap discovery before handler execution).
- The guarded fallback startup path in `before_request` explicitly excludes `/observability/*` requests.
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


## Common Pipeline Failure Modes

| Symptom | Diagnostic Endpoint | Fields to Inspect | Interpretation |
|---|---|---|---|
| `ladder_not_hydrated` | `GET /observability/hydration-prerequisite-runtime?station=...` | `hydration_state.cache_valid`, `hydration_state.series_discovered`, `hydration_state.markets_cached` | Hydration prerequisites are not usable for market evaluation yet. |
| `SUPPRESSED_NO_TRANSITION` | `GET /observability/internal-alert-runtime?station=...` | `latest_market_outcome`, `latest_transition`, `diagnostic_class` | Alert path evaluated, but no qualifying runtime transition reached emission criteria. |
| `no eligible markets` | `GET /observability/market-eligibility-runtime?station=...` | `eligible_markets_count`, `rejected_markets_count`, `rejection_breakdown` | Market evaluation ran but deterministic filters left zero eligible ladders. Ignore `NO_DIRECTIONAL_LADDER_MATCH` if it appears in older notes; that branch is effectively unreachable in current route logic. |
| `missing_webhook` | `GET /observability/alert-decision-trace?station=...` | `terminal_state`, `decision_chain`, `execution_mode` | Alert decision path is blocked before delivery because webhook configuration is absent. |
| `webhook_failed` | `GET /observability/internal-alert-runtime?station=...` | `latest_market_outcome`, `alerts_emitted_today`, `diagnostic_class` | Alert decision reached delivery stage, but downstream webhook attempt failed. |

## Signal-layer troubleshooting

### near_boundary_momentum_up
- Confirm `hydration_state.cache_valid=true` and `eligible_markets_count>0`.
- Check `/observability/internal-alert-runtime` for `signal_type`, `suppression_reason`, and `cooldown_state`.
- Common suppressions: `NO_ELIGIBLE_MARKETS`, `STATION_COOLDOWN_ACTIVE`, `HYDRATION_CACHE_INVALID`.

### goldilocks_reversion_alert
- Verify a `settlement_up` occurred first in the current epoch.
- Verify the epoch saw both a spike (`>= settlement + 1.2°F`) and reversion (`<= settlement - 0.2°F`).
- Use `/observability/alert-decision-trace` plus `/observability/runtime-authority-snapshot` as the primary diagnosis surfaces. `/observability/pipeline-truth` remains partial/logic-stubbed and is not authoritative operator evidence.
