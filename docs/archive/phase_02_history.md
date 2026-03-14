ARCHIVED — superseded; not current requirements.

# Phase 2 — Observation History + Reporting (Proposed)

## Goal
Capture and retain the full observation stream so we can reconstruct “every tick” events and daily highs/lows reliably.

## In Scope
- Store observations to durable storage:
  - Minimal schema: station, obs_time_utc, temp_f (raw + integer), source, payload hash
- Endpoints:
  - GET /history/recent?icao=KDEN&minutes=60
  - GET /history/day?icao=KDEN&date=YYYY-MM-DD (local date)
  - GET /history/highs?date=YYYY-MM-DD (local date per station)
- High-of-day tracking derived from stored observations
- Operational controls:
  - retry/backoff and basic circuit breaker when NWS errors
  - logging improvements (structured logs)

## Out of Scope
- Forecasting, bias, backfills, Kalshi trading

## Definition of Done
- We can prove we captured short-lived spikes (e.g., 70→71 for 1 minute)
- Daily high/low computed from stored obs matches what we observed in alerts/logs
