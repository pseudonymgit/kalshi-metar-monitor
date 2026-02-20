# METAR Monitor (Phase 1)

Small, standalone Flask service that polls NOAA AWC (ADDS) for METARs, tracks last observed temperature per station, 
and sends an alert when temperature changes by `TEMP_ALERT_DELTA_F` or more.

## Endpoints

- `GET /health` – liveness check
- `GET /state` – current in-memory + file-backed state (last temps, last timestamps)
- `GET /metar/now?stations=KDEN,KLAX` – on-demand fetch without mutating state
- `POST /monitor/start` – starts the background poller (if not autostarted)
- `POST /monitor/stop` – stops the background poller

## Environment Variables

| Var | Default | Notes |
|-----|---------|-------|
| `METAR_STATIONS_JSON` | `["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]` | JSON list of ICAO codes |
| `METAR_POLL_SECONDS` | `60` | Polling cadence |
| `TEMP_ALERT_DELTA_F` | `1.0` | Alert on changes >= this |
| `ALERT_WEBHOOK_URL` | _empty_ | Slack/Discord webhook URL (optional) |
| `ALERT_INGEST_SECRET` | _empty_ | Optional secret to send along |
| `METAR_CACHE_FILE` | `/opt/render/project/src/data/metar_state.json` | Where state is persisted |
| `METAR_AUTOSTART` | `true` | Start poller at boot |

## Deploy (Render)

- Start Command: `gunicorn app:app -t 180`
- Add the env vars above (as “Secret”).
- Ensure outbound network is allowed.

## Notes

- Source: NOAA AWC ADDS Data Server (no API key)
  - Example: https://aviationweather.gov/adds/dataserver_current/httpparam?dataSource=metars&requestType=retrieve&format=JSON&stationString=KDEN&hoursBeforeNow=2&mostRecent=true
- We only use latest observation per station.
- All temperatures converted to °F.

Auth pipeline verification test.
