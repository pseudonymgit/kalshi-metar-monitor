# Phase 1 — METAR Monitor + Discord Alerts (NWS)

## Goal
Monitor near-live station observations via NWS (`api.weather.gov`) and send Discord alerts when the **integer temperature changes** (up OR down), during a **station-local alert window**, resetting the alert memory daily.

This phase is considered “done” when:
- The service polls and/or supports on-demand reads successfully.
- Alerts post reliably to Discord.
- Alerts trigger only on integer boundary crossings (e.g., 70.x → 71.0+ or 71.x → 70.x).
- Alerts only fire during the configured local time window.
- The alert boundary memory resets daily per station (local date).

---

## What the service does

### Data source
- **NWS / api.weather.gov** is the default source.
- Endpoints used:
  - Range: `/stations/{ICAO}/observations?start=...&end=...&limit=...`
  - Fallback for single: `/stations/{ICAO}/observations/latest` (only if range window returns empty)

### Polling + windowing
- The monitor polls on `METAR_POLL_SECONDS` (default 60s).
- Each poll uses a rolling window:
  - If we have seen the station before: start at `(last_seen - OVERLAP_SECONDS)`
  - If first run: start at `(now - lookback_min - FIRST_RUN_CUSHION_SEC)`

### Dedupe
- Observations are ingested in chronological order.
- Obs with `obs_time <= last_seen_iso[ICAO]` are ignored.

---

## Alert logic (Phase 1)
### Integer-cross alerts
An alert is triggered when the **integer floor** changes between observations:
- `floor(prev_temp_f) != floor(curr_temp_f)`

This catches:
- Up-cross: 70.9 → 71.0
- Down-cross: 71.0 → 70.9

### Alert window (station local time)
Alerts only fire when the station’s local time is:
- **11:00 to 19:00**, inclusive start, exclusive end (`11 <= hour < 19`)

### Daily reset (station local date)
Once per station-local day:
- Clear `last_alert_floor[ICAO]`
- Record `last_reset_date_local[ICAO] = YYYY-MM-DD`

This ensures the “integer-cross memory” starts fresh each day.

---

## Time zones
- Service returns ET in some responses (metrics, window debug), but:
- **Alert gating + daily reset are based on station local time.**

The station → tz mapping lives in `core/metar_monitor.py` as `_ICAO_TZ`.
If an ICAO isn’t in the map, it defaults to ET.

---

## Config (env vars)
Required (for NWS etiquette + Discord alerts):
- `AWC_FROM_EMAIL` (or `HTTP_FROM_EMAIL`)
- `AWC_USER_AGENT` (or `HTTP_USER_AGENT`)
- `ALERT_WEBHOOK_URL` (Discord webhook URL)

Common:
- `METAR_STATIONS_JSON` (default includes KDEN, KLAX, KNYC, KPHL, KMDW, KMIA, KAUS)
- `METAR_POLL_SECONDS` (default 60)
- `METAR_DEFAULT_SOURCE` (default `nws`)
- `METAR_LOOKBACK_MIN` (default 3)
- `METAR_CACHE_FILE` (default `/opt/render/project/src/data/metar_state.json`)

Notes:
- `TEMP_ALERT_DELTA_F` exists but is not used for integer-cross alerting.

---

## API endpoints
- `GET /` → health
- `GET /metar/latest?icao=KDEN&source=nws`
- `GET /metar/window?icao=KDEN&minutes=3&source=nws`
- `GET /metar/multi?icaos=KDEN,KLAX&source=nws`
- `POST /metar/start` → start scheduler
- `POST /metar/stop` → stop scheduler
- `GET /metar/metrics`
- `POST /metar/test-alert` → synthetic Discord alert
- `POST /metar/force-poll` → run one poll immediately
- `GET /debug/state`
- `GET /debug/version`

---

## Testing (Linux)
```bash
# health
curl -sS https://kalshi-metar-monitor.onrender.com/ | jq .

# single latest
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/latest?icao=KDEN&source=nws" | jq .

# window read
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/window?icao=KDEN&minutes=10&source=nws" | jq .

# start scheduler
curl -sS -X POST "https://kalshi-metar-monitor.onrender.com/metar/start" | jq .

# metrics
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/metrics" | jq .

# send test alert to Discord
curl -sS -X POST "https://kalshi-metar-monitor.onrender.com/metar/test-alert" | jq .
