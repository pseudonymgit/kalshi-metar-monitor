# Environment Variables

## Required (Phase 1)
- `ALERT_WEBHOOK_URL`  
  Discord webhook URL to receive alerts.

- `AWC_FROM_EMAIL`  
  Used as `From` header for api.weather.gov.

- `AWC_USER_AGENT`  
  Used as `User-Agent` header for api.weather.gov.

## Optional (Phase 1)
- `METAR_DEFAULT_SOURCE` (default: `nws`)
- `METAR_STRICT` (default: `true`)
- `METAR_POLL_SECONDS` (default: `60`)
- `TEMP_ALERT_DELTA_F` (default: `1.0`)  
  **Will be replaced by integer-bucket crossing logic.**

- `METAR_LOOKBACK_MIN` (default: `3`)
- `METAR_STATIONS_JSON` (default list in code)
- `METAR_CACHE_FILE` (default: `/opt/render/project/src/data/metar_state.json`)

## Future (Phase 2+)
- Kalshi read-only keys (not used in Phase 1)
