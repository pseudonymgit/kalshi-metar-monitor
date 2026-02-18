# Phase 1 — Alert Engine Specification
Version: 1.0
Status: Frozen (Controlled Evolution Only)

This document defines the authoritative alert semantics for Phase 1.

Any change to alert behavior requires:
- Version increment
- Explicit documentation of semantic change
- PR description clearly stating the change
- ChatGPT merge gate approval

If behavior changes without version increment, it is considered a defect.




# ⚠️ PHASE 1 IS FROZEN

Frozen as of: 2026-02-17
Git tag: phase1-final

Behavior is locked:
- NWS source
- Integer floor transition alerts (up/down)
- Station-local 11:00–19:00 window
- Station-local daily reset
- last_observed_floor persistence

No behavior changes allowed.
Only critical bug fixes permitted.




# Phase 1 (Hardened) — Current Production Behavior

This document is the source of truth for **current Phase 1 behavior**.

## Scope

Phase 1 monitors METAR temperatures for a configured station list and sends Discord/webhook alerts when integer temperature floors change.

## Data source

- **Primary/expected source:** `nws` (api.weather.gov station observations).
- Source is controlled by `METAR_DEFAULT_SOURCE` (default `nws`) and strict fetch behavior.
- In production, Phase 1 is operated with NWS as the chosen source.

## Alert semantics (current)

Alerts are emitted only when all of the following are true:

1. A new observation is ingested for a station.
2. The integer floor changes between previous and current temperatures (`floor(prev_temp_f) != floor(temp_f)`).
   - This triggers for **upward or downward** crosses.
3. Observation time is inside the station-local alert window: **11:00 <= local time < 19:00**.
4. The same target integer floor has not already been alerted for that station since the current local-day reset.

## Daily reset semantics

- Reset key: **station-local calendar date**.
- At first ingestion for a new local date, per-station integer-cross memory is reset.
- Reset marker is persisted as `last_reset_date_local[ICAO] = "YYYY-MM-DD"`.

## Persisted state (cache)

Cache file (`METAR_CACHE_FILE`) persists:

- `last_obs`
- `last_seen_iso`
- `last_alert_floor`
- `last_reset_date_local`

This ensures integer-cross dedupe/reset state survives restarts.

## Required environment variables

Required for production operation:

- `ALERT_WEBHOOK_URL` — destination webhook for alerts.
- `AWC_FROM_EMAIL` — value used for `From` header on NWS requests.
- `AWC_USER_AGENT` — value used for `User-Agent` header on NWS requests.

Common optional knobs:

- `METAR_STATIONS_JSON`
- `METAR_POLL_SECONDS`
- `METAR_DEFAULT_SOURCE` (default `nws`)
- `METAR_STRICT` (default `true`)
- `METAR_LOOKBACK_MIN`
- `METAR_CACHE_FILE`
- `IEM_LOOKBACK_HOURS`
- `TEMP_ALERT_DELTA_F` (retained for compatibility; integer-cross logic is authoritative)
