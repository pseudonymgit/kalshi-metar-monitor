# Phase 1 — METAR Monitor (Current Scope)

## Goal
Run a small public web service that:
- Pulls METAR observations for a watchlist of ICAO stations (e.g., KDEN, KLAX, KMDW).
- Detects **integer-degree temperature crossings** (both up and down).
- Sends **Discord webhook alerts** when a station crosses into a new integer °F bucket.
- Restricts alerts to a **local-time window per station** (default: 11:00–19:00 station-local).
- Resets “daily” state overnight so alerts are fresh each new day.

This service is intentionally narrow: **no backfills, no forecasting, no Kalshi trading yet**.

---

## What it does today
- Flask app exposes endpoints:
  - `GET /` health
  - `GET /metar/latest?icao=KDEN&source=nws`
  - `GET /metar/window?icao=KDEN&minutes=3&source=nws`
  - `GET /metar/multi?icaos=KDEN,KLAX,KMDW&source=nws`
  - `GET|POST /metar/watchlist`
  - `GET /metar/metrics`
  - `POST /metar/start`
  - `POST /metar/stop`
  - `POST /metar/test-alert`
  - `POST /metar/force-poll`
  - (debug) `GET /debug/version`, `GET /debug/state`

- `core/metar_monitor.py`:
  - Fetches observations from **NWS** (api.weather.gov).
  - Uses a small rolling window with overlap, ingests observations in time order.
  - Maintains `last_seen_iso` to dedupe.
  - Emits alerts via Discord webhook.

---

## Alert behavior (Phase 1 requirements)
### 1) Integer crossing alerts only
We alert when the *integer bucket* changes:
- Example: 70.2 → 70.8 : **no alert**
- 70.8 → 71.0 : **alert (UP)**
- 71.0 → 70.9 : still 70 bucket? depends on bucket rule.
**Rule for Phase 1:** floor/“bucket” by truncation is NOT allowed.  
Instead: bucket = `int(round(temp_f))` is NOT allowed either.

**Use this rule:**
- Bucket = `math.floor(temp_f)` for positive temps
- Bucket = `math.ceil(temp_f)` for negative temps (so -0.2 stays 0? decide explicitly)

**Recommended for METAR temps:** use `math.floor(temp_f)` always (temps are usually >= -40F and this matches “crossing into next whole degree”).

We alert on any change in bucket:
- Up-cross: 70.x → 71.x
- Down-cross: 71.x → 70.x

### 2) Local time alert window per station
Only emit alerts between **11:00 and 19:00** in the station’s local timezone.

Implementation notes:
- Maintain a mapping `ICAO -> IANA timezone` (static config in Phase 1).
- Convert observation timestamp to station-local time.
- Gate alerts by local hour.

### 3) Overnight reset
At “local midnight” for each station, reset the day’s state so:
- The first crossing in the morning can alert cleanly.
- We don’t carry prior-day bucket across days.

Implementation notes:
- Track `last_bucket` and `last_bucket_date` per station.
- If `obs_local_date != last_bucket_date`, reset station bucket state.

---

## Phase 1 is complete when
- Service runs reliably on Render.
- Scheduler polling is stable.
- Discord alerts are correct:
  - integer bucket change only
  - both directions
  - gated to 11:00–19:00 local
  - resets by local day

---

## Out of scope (explicitly)
- Backfills (IEM/VisualCrossing/etc.)
- Bias modeling
- Forecast history tracking
- Any Kalshi trading or automated orders

Those belong in later phases.
