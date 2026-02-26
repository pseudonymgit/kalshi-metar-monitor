# Operations

## Runtime notes (Render)

- Start command: `gunicorn app:app -t 180`
- No import-time scheduler side effects.
- Scheduler lifecycle is request-hook safe and idempotent.

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

### Health / Debug

- `GET /` → `{"status":"ok"}`
- `GET /debug/version` → module/config diagnostics
- `GET /debug/state` → in-memory state snapshot

### METAR operations

- `GET /metar/window?icao=KDEN&minutes=3&source=nws`
- `GET /metar/latest?icao=KDEN&source=nws`
- `GET /metar/multi?icaos=KDEN,KLAX&source=nws`
- `GET /metar/watchlist`
- `POST /metar/watchlist` with `{"icaos":["KDEN","KLAX"]}`
- `GET /metar/metrics`
- `GET /metar/status`
- `POST /metar/start`
- `POST /metar/stop`
- `POST /metar/force-poll`
- `POST /metar/test-alert`
- `POST /metar/simulate-ladder`

### Kalshi operations

- `GET /kalshi/ping`
- `GET /kalshi/markets?limit=5`
- `POST /kalshi/check?limit=5`
- `GET /kalshi/snapshot?station=KJFK&type=HIGH,LOW`
- `POST /kalshi/composed?station=KJFK&type=HIGH`
- `GET /kalshi/health`

## `/metar/simulate-ladder` runbook

Request:
```bash
curl -s -X POST "$BASE/metar/simulate-ladder" \
  -H 'Content-Type: application/json' \
  -d '{"icao":"KJFK","temp_f":72.1,"deliver":false}'
```

JSON rules:
- Required: `icao`, `temp_f`.
- Optional: `deliver` (`true`/`1` to allow webhook delivery attempt).

Behavior:
- Window bypass is always true for simulation (`window_bypassed: true`).
- Crossing is determined from previous integer vs current integer.
- `delivery_attempted` is true only when crossing occurs and `deliver=true`.

Baseline → crossing test sequence:
1. Baseline seed (no crossing):
```bash
curl -s -X POST "$BASE/metar/simulate-ladder" \
  -H 'Content-Type: application/json' \
  -d '{"icao":"KJFK","temp_f":72.2,"deliver":false}'
```
2. Crossing trigger:
```bash
curl -s -X POST "$BASE/metar/simulate-ladder" \
  -H 'Content-Type: application/json' \
  -d '{"icao":"KJFK","temp_f":73.1,"deliver":false}'
```
3. Delivery validation (only when webhook target is ready):
```bash
curl -s -X POST "$BASE/metar/simulate-ladder" \
  -H 'Content-Type: application/json' \
  -d '{"icao":"KJFK","temp_f":74.1,"deliver":true}'
```

## Composed ladder alert interpretation

Message layout:
- Header: `<emoji> <station> <market_type> — Ladder Cross <arrow>`
- Body:
  - Current temp and entered bucket label.
  - `Event: <event_ticker>`.
  - Direct market link: `https://kalshi.com/markets/<event_ticker>`.
  - Ladder table with YES/NO prices.
- Footer: `Next rung: <distance | MAX REACHED | MIN REACHED>`.

Table conventions:
- `▶` marks the active rung row.
- `← CURRENT` labels the active bucket.
- Arrow `⬆️` means upward transition context.
- Arrow `⬇️` means downward transition context.

## Environment configuration checklist

Required:
- `ALERT_WEBHOOK_URL`
- `AWC_FROM_EMAIL`
- `AWC_USER_AGENT`

Common production:
- `METAR_AUTOSTART`
- `METAR_POLL_SECONDS`
- `METAR_STATIONS_JSON`
- `METAR_CACHE_FILE`
- `KALSHI_TARGET_STATION`
- `KALSHI_TARGET_MARKET_TYPE`
- `KALSHI_ALERT_TICKERS`
