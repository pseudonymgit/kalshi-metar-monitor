# Kalshi METAR Monitor

Production Flask service that ingests METAR observations, detects integer temperature crossings, and emits composed Kalshi weather ladder alerts when structured markets are available.

## Canonical Documentation
- Architecture specification: `docs/ARCHITECTURE.md`
- Execution governance rules: `docs/CODEX_MASTER_TEMPLATE.md`

## Deterministic Runtime Guardrails
- 60-second polling cadence.
- Event-gated Kalshi calls (only after integer-cross detection).
- 5-second station throttle.
- Deterministic-only behavior.
- No automated trading.

## Project Scope

### Phase 1 — METAR Monitoring (Frozen v1.0 semantics)
Phase 1 alert semantics remain fixed:
- Integer floor-cross detection.
- Station-local alert window enforcement (11:00–19:00 local time) for live polling.
- Station-local daily reset of alert memory.
- Scheduler lifecycle idempotency and thread isolation.

### Phase 2 — Kalshi Weather Ladder Composition
Phase 2 enriches temperature-cross events with current Kalshi weather ladder context:
- Structured snapshot by station + market type (`HIGH`, `LOW`).
- Ladder transition detection and composed webhook alert formatting.
- Event-scoped context memory to infer direction and adjacent rung distance.

## Alert Routing Policy (Production)

Within station-local alert window (11:00–19:00):

| Condition | Result |
|---|---|
| Ladder exists + transition fires | Composed ladder alert |
| Ladder exists + no transition | Nothing |
| Ladder missing + enabled | Ladder-missing alert |
| Outside window | Nothing |

Policy notes:
- Raw integer-cross (temp-only) alerts are permanently removed.
- Composed alerts are the only alert type.

## Weather Ladder Alert Architecture

Flow:
1. METAR ingest updates station state and detects integer floor crosses.
2. On crossing, the alert pipeline fetches Kalshi ladder snapshots for active market types.
3. Ladder transition logic checks bucket entry/transition.
4. If transition alert criteria are met, a composed ladder alert is sent.

## Architecture Overview (Alert Flow)

`Integer cross detected -> Window check -> Ladder evaluation ->`
- `Transition? -> Composed alert -> Audit row`
- `Missing? -> Missing alert -> Audit row`
- `Otherwise -> No alert`

## SQLite Durable Audit

- Environment variable: `ALERT_DB_PATH=/var/data/alerts.db`
- Default fallback path: `/var/data/alerts.db`
- Deployment requirement: persistent disk is required.
- Scope: single-instance SQLite only.

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

Audit event writes:
- `ladder_transition`
- `ladder_missing`
- `composed_alert_sent`

## Structured Logging Contract

Allowed high-signal log events:
- `EVENT integer_cross`
- `EVAL ladder_check`
- `WARN ladder_missing`
- `EVENT ladder_transition`
- `SEND composed_alert`

Contract:
- No per-poll logging.
- No debug prints.
- High-signal logs only.

Bucket detection rules:
- `less`: match when `temp <= cap`.
- `between`: match when `floor <= temp < cap`.
- `greater`: match when `temp >= floor`.

Direction resolution order:
1. `transition_reason` hint (`up` / `down`) from ladder transition logic.
2. Previous bucket index from event-scoped memory.
3. Previous observed temperature fallback.
4. Default upward arrow if no prior context exists.

Distance calculation:
- Uses the adjacent bucket in the resolved direction.
- Upward movement: boundary from next higher rung.
- Downward movement: boundary from next lower rung.
- Edge cases emit `MAX REACHED` (top) or `MIN REACHED` (bottom).

Event-scoped memory:
- Context key is scoped by `station + market_type + event_ticker`.
- Prevents cross-event contamination when markets roll to a new event ticker.

## Scheduler Lifecycle

Autostart and lifecycle behavior:
- `METAR_AUTOSTART=true` (default): scheduler start is attempted once via Flask `before_first_request` when available.
- Fallback path: one-time guarded `before_request` hook for environments lacking `before_first_request`.
- Start endpoint: `POST /metar/start`.
- Stop endpoint: `POST /metar/stop`.
- Status endpoint: `GET /metar/status` returns:
  - `scheduler_running`
  - `poll_count`
  - `last_poll_utc`
  - `last_loop_utc`
  - `timeout_count`
  - `last_timeout_station`
  - `last_timeout_utc`

## API Surface

### METAR (`/metar/*`)
- `GET /metar/window` — ingest strict-source window for one station and return latest-known observation.
- `GET /metar/latest` — latest observation for one station.
- `GET /metar/multi` — on-demand fetch for multiple stations.
- `GET /metar/watchlist` — read active watchlist.
- `POST /metar/watchlist` — replace active watchlist.
- `GET /metar/metrics` — poll/timeout counters and monitoring metrics.
- `GET /metar/status` — scheduler and loop lifecycle status.
- `POST /metar/start` — start METAR scheduler.
- `POST /metar/stop` — stop METAR scheduler.
- `POST /metar/test-alert` — emit synthetic webhook payload.
- `POST /metar/force-poll` — run one immediate poll cycle.
- `POST /metar/simulate-ladder` — simulate temp ingestion for ladder transition testing.

### Kalshi (`/kalshi/*`)
- `GET /kalshi/ping` — checks public Kalshi reachability.
- `GET /kalshi/markets` — fetches public markets (`limit` query param).
- `POST /kalshi/check` — manual change-detection pass against baseline state.
- `GET /kalshi/snapshot` — structured station snapshot for market ladders.
- `POST /kalshi/composed` — manual composed weather-ladder alert trigger.
- `GET /kalshi/health` — active station set and composed send metadata.

## `/metar/simulate-ladder` Workflow

Request JSON:
```json
{
  "icao": "KJFK",
  "temp_f": 72.1,
  "deliver": false
}
```

Behavior:
- Required fields: `icao`, `temp_f`.
- Optional `deliver` flag (`true/1` values accepted):
  - `false` (default): computes crossing + ladder logic without webhook delivery.
  - `true`: attempts live alert delivery when a crossing is generated.
- Simulation bypasses station-local alert window (`window_bypassed: true`).

Recommended baseline → crossing test sequence:
1. Seed baseline (no crossing expected):
   - `temp_f` stays in current integer.
2. Submit crossing value (integer change):
   - `temp_f` crosses next integer.
3. Re-run with `deliver=true` only when ready to emit to webhook.

## Ladder Alert Formatting

Composed ladder alert payload includes:
- Header with station, market type, and directional arrow (`⬆️` or `⬇️`).
- Current temperature and entered rung label.
- Event ticker and direct market URL (`https://kalshi.com/markets/<event_ticker>`).
- Monospaced ladder table rows:
  - Rung label (`X or below`, `X–Y`, `X or higher`)
  - `YES` and `NO` prices in cents
  - `▶` prefix and `← CURRENT` marker for active rung
- Footer line: `Next rung: <distance | MAX REACHED | MIN REACHED>`.

## Environment Variables

### Core alerting and scheduler
- `ALERT_WEBHOOK_URL` — Webhook destination for composed ladder alerts.
- `METAR_AUTOSTART` — `true/false`; one-time scheduler autostart gate.
- `METAR_POLL_SECONDS` — Poll interval for scheduler loop.
- `ALERT_DB_PATH` — Durable SQLite audit DB path (default `/var/data/alerts.db`; persistent disk required).

### Alert policy controls
- `ALERT_ON_MISSING_LADDER` — Enables ladder-missing alert emission when a ladder is unavailable.
- `SUPPRESS_TEMP_ONLY_ALERTS` — Legacy compatibility only; raw temp-only alerts are removed in production.

### Kalshi ladder targeting
- `KALSHI_TARGET_STATION` — Restrict ladder monitoring to one ICAO’s city event set.
- `KALSHI_TARGET_MARKET_TYPE` — Comma-separated `HIGH` and/or `LOW` filter (`HIGH,LOW` enables symmetric dual-side monitoring).
- `KALSHI_ALERT_TICKERS` — Optional comma-separated alert emission allowlist.

## LOW Market Support

- `HIGH` and `LOW` share symmetric ladder-evaluation and transition behavior.
- Enable both with: `KALSHI_TARGET_MARKET_TYPE=HIGH,LOW`.

### Additional operational vars
- `METAR_STATIONS_JSON`, `METAR_CACHE_FILE`, `METAR_DEFAULT_SOURCE`, `METAR_STRICT`, `METAR_LOOKBACK_MIN`, `IEM_LOOKBACK_HOURS`, `ALERT_INGEST_SECRET`, `AWC_FROM_EMAIL`, `AWC_USER_AGENT`, `HTTP_FROM_EMAIL`, `HTTP_USER_AGENT`, `KALSHI_PUBLIC_BASE_URL`.

## Deployment (Render / Gunicorn)

- Production URL: `https://kalshi-metar-monitor.onrender.com`
- Start command: `gunicorn app:app -t 180`
- Scheduler autostart is request-lifecycle safe:
  - No import-time scheduler startup.
  - One-time guarded startup in Flask request hooks.

## Governance

- Branch + PR workflow only; no direct commits to protected branch names.
- `docs/CODEX_MASTER_TEMPLATE.md` process rules are mandatory.
- `docs/ARCHITECTURE.md` is the canonical deterministic architecture specification.
- Merge sign-off phrase remains: **“Phase 1 semantics preserved.”**
