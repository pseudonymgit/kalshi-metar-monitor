# Ladder Cache Observability

## Purpose

`GET /observability/ladder_cache` provides an operator-facing, read-only view of ladder hydration state and market cache status per station.

This endpoint is observability-only:

- it does not mutate runtime state
- it does not trigger ladder hydration
- it does not call Kalshi APIs

## Response Shape

```json
{
  "generated_utc": "...",
  "station_count": 0,
  "stations": [
    {
      "station": "...",
      "series_ticker": "...",
      "market_count": 0,
      "ladder_cache_age_seconds": 0,
      "hydration": {
        "cache_present": true,
        "series_discovered": true,
        "cache_valid": true
      },
      "last_hydration_execution_utc": "..."
    }
  ]
}
```

## Field Notes

- `generated_utc`: UTC timestamp when the snapshot was generated.
- `station_count`: number of station rows in `stations`.
- `stations[]`: per-station ladder cache observability row.
  - `station`: ICAO station identifier.
  - `series_ticker`: currently known Kalshi series ticker from last hydration execution snapshot, if present.
  - `market_count`: number of cached markets for the station's known series ticker.
  - `ladder_cache_age_seconds`: age of the cached ladder (`generated_utc - hydrated_at_utc`) in whole seconds; `null` when cache metadata is unavailable.
  - `hydration.cache_present`: whether a cache entry exists for the known series ticker.
  - `hydration.series_discovered`: whether a series ticker is known from runtime hydration execution snapshot.
  - `hydration.cache_valid`: latest hydration prerequisite validity bit.
  - `last_hydration_execution_utc`: last hydration evaluation timestamp for station, if available.

## Data Sources

The endpoint is built strictly from in-memory runtime snapshots:

- `get_last_hydration_execution_snapshot()`
- `get_hydration_prerequisite_state_snapshot()`
- `get_cached_series_markets()`

## Operator Debugging Usage

Use this endpoint to quickly answer:

- Which stations appear hydrated (`cache_present=true`, `cache_valid=true`)?
- Which stations have known series but missing cache (`series_discovered=true`, `cache_present=false`)?
- Which stations have stale or absent prerequisite validation (`cache_valid=false`)?
- How old each station ladder cache is (`ladder_cache_age_seconds`)?
- When hydration last executed (`last_hydration_execution_utc`)?

This allows targeted diagnosis of partial hydration or incomplete cache population without invoking live Kalshi calls.
