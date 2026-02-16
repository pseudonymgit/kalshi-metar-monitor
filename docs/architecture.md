# Architecture

## Overview
A lightweight Flask web service deployed to Render.

### Components
- `app.py`
  - HTTP endpoints (health, METAR endpoints, ops helpers)
  - Starts/stops poll scheduler

- `core/metar_monitor.py`
  - Fetches observations from a configured source (Phase 1: NWS)
  - Maintains in-memory + cached state
  - Dedupe + ingestion pipeline
  - Alert emission via webhook (Discord supported)

## State
In-memory state `_STATE`:
- `stations` (watchlist)
- `last_obs` per station
- `last_seen_iso` per station (dedupe)
- `poll_count`, `last_poll_utc`
- `last_alert` (optional)

Persistent cache file (Render disk best-effort):
- `/opt/render/project/src/data/metar_state.json`

## Polling
- Background thread (daemon) started via `POST /metar/start`
- Every `METAR_POLL_SECONDS`:
  - For each station, fetch a short window and ingest new observations
  - Emit alerts if crossings occur

## Alerts
- Webhook POST
- If Discord webhook URL, formats a user-friendly message and embed.

## Timezones
- Convert timestamps for display to ET (`America/New_York`) in metrics.
- Phase 1 adds station-local timezone gating for alerts.
