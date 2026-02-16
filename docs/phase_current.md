# Phase 1 — METAR Live Monitor (Current)

## Goal
Run a small web service that fetches near-live station observations (METAR via NWS station observations),
detects integer temperature threshold crossings (up/down), and sends Discord alerts during allowed hours.

## In Scope
- NWS-based observation fetch for configured ICAOs (no multi-source fallback unless explicitly enabled)
- Endpoints:
  - GET / (health)
  - GET /metar/latest?icao=KDEN[&source=nws]
  - GET /metar/window?icao=KDEN&minutes=N[&source=nws]
  - GET /metar/multi?icaos=KDEN,KLAX
  - GET/POST /metar/watchlist
  - POST /metar/start, POST /metar/stop
  - GET /metar/metrics
  - POST /metar/test-alert, POST /metar/force-poll
  - /debug/version, /debug/state (if present)
- Polling scheduler (Render web service) that:
  - polls on an interval
  - ingests new observations
  - triggers alerts on integer temperature boundary crossings
- Alerts:
  - Discord webhook integration
  - Only send alerts inside each station’s **local time window**
- Daily reset logic:
  - midnight local time: reset per-station “last alerted integer” baseline for the new day

## Out of Scope (Do NOT implement in Phase 1)
- Backfills, bias estimation, confidence intervals, forecast aggregation
- Kalshi trading, market selection, order placement
- Storing full historical time series in Sheets/DB (beyond minimal state/cache needed for operation)
- Complex UI/dashboard
- Multi-source blending / fallbacks (unless a Phase 1 bug forces an emergency workaround)

## Acceptance Criteria (Definition of Done)
- Service deploys on Render and stays up (no boot/import errors)
- /metar/latest returns a valid observation for a known station when NWS has data
- Polling updates metrics: poll_count increments; last_poll_utc and last_poll_et populate
- Discord alert fires when integer temperature crosses up or down (e.g., 70→71, 71→70)
- Alerts are suppressed outside the configured local-time window per station
- State resets overnight so the next day’s alerts behave as “fresh”

## Configuration (Env Vars)
- METAR_STATIONS_JSON: JSON list of ICAOs
- METAR_POLL_SECONDS: scheduler interval (seconds)
- METAR_LOOKBACK_MIN: window size (minutes)
- METAR_DEFAULT_SOURCE: "nws"
- METAR_STRICT: true
- AWC_FROM_EMAIL, AWC_USER_AGENT: headers for api.weather.gov etiquette
- ALERT_WEBHOOK_URL: Discord webhook URL
- TEMP_ALERT_DELTA_F: (legacy) should not control integer crossing logic once implemented
- METAR_CACHE_FILE: state file path

## Notes / Guardrails
- Prefer correctness + reliability over adding features.
- Any “nice to have” goes into docs/backlog.md.
