# MARKET DISCOVERY INTEGRITY AUDIT

Date: 2026-03-02
Scope: inventory-only audit of market discovery, hydration prerequisites, and station/market mapping.

## Method
- Static code-path audit of discovery + eligibility/hydration logic.
- Runtime probe attempted via `core.kalshi_monitor.ensure_series_discovery_loaded()` and per-station snapshot hydration.
- Runtime probe failed due network connectivity to `api.elections.kalshi.com` (`[Errno 101] Network is unreachable`).

## 1) Configured station inventory

Configured station sources:
1. `METAR_STATIONS_JSON` default list in `core.metar_monitor.get_default_config()`.
2. Kalshi station token map in `core.kalshi_monitor._STATION_CITY_TOKEN_MAP`.
3. Optional `KALSHI_ACTIVE_STATIONS` allowlist in `core.kalshi_monitor._get_active_stations()`.
4. Runtime union/fallback in `app._canonical_live_station_universe()` via config + state + watchlist + discovered series.

Static default station set (7):
- KDEN, KLAX, KNYC, KPHL, KMDW, KMIA, KAUS

## 2) Dynamic discovery inventory

Dynamic series discovery source:
- `GET /series?tags=Daily temperature` in `_discover_series_for_stations()`.
- Filter constraints: `frequency == daily`, title contains `highest`, ticker starts with `KXHIGH`.
- Discovery result shape: `station -> series_ticker` (`_SERIES_BY_STATION`).

Dynamic market discovery source:
- `GET /markets?series_ticker=<series>` in `build_structured_snapshot()`.
- Filter constraints in `_filter_structured_markets()`:
  - status is active (or missing status)
  - ticker contains station city token
  - ticker contains station-local Kalshi day token
  - ticker contains enabled market type(s)
- Additional eligibility gate in app coverage path:
  - `_market_has_supported_strike()` must be true.

## 3) Required count fields

Because dynamic calls were unreachable during this audit, only static/deterministic values are confirmed:

- configured_station_count: **7** (default config + station token map overlap)
- discovered_series_count: **unavailable at audit time** (network error)
- discovered_market_count: **unavailable at audit time** (depends on series hydration)
- eligible_market_count: **unavailable at audit time** (post-filter + supported-strike only)
- active_market_count: **not a canonical persisted metric in code**

## 4) Station → series → markets → eligibility mapping (audit-state)

| station | city_token | discovered_series | discovered_markets | eligibility_state |
|---|---|---|---|---|
| KDEN | DEN | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |
| KLAX | LAX | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |
| KNYC | NY | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |
| KPHL | PHIL | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |
| KMDW | CHI | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |
| KMIA | MIA | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |
| KAUS | AUS | unavailable (runtime fetch failed) | unavailable | unknown (no runtime discovery data) |

## 5) Discrepancy inventory

### Unmapped configured stations
- Cannot be confirmed from live discovery due failed series fetch.
- Structural risk area: `_discover_series_for_stations()` accepts only series title containing `highest` and ticker prefix `KXHIGH`; stations with only LOW-side discoverability or naming drift may appear unmapped.

### Discovered markets not mapped to a station
- Mapping authority is `city_token` substring check from `_STATION_CITY_TOKEN_MAP` in `_filter_structured_markets()`.
- Any market lacking mapped city token is rejected as station mismatch.
- Live count unavailable without market payload access.

### Hydration failures per station
- Hydration execution telemetry exists in `_LAST_HYDRATION_EXECUTION` and `get_last_hydration_execution_snapshot()`.
- This audit run could not populate live hydration rows due upstream connectivity failure.

### Eligibility exclusions per station
- Exclusion counters are recorded in `rejection_counts` (hydration) and endpoint-level `filtered_out_market_counts_by_reason`.
- Station-level live exclusion counts unavailable in this run.

## 6) Dynamic scrape filter correctness assessment

No direct evidence of incorrect filtering was observed in static logic review.

However, there are deterministic filter choke-points that can reduce discovered/eligible counts even when markets exist:
1. Series discovery hard-requires `highest` title token + `KXHIGH` prefix.
2. Market eligibility requires station-local date token match.
3. Market status must be `active` (if present).
4. Market type must match enabled config (`KALSHI_TARGET_MARKET_TYPE`, default HIGH only).
5. Strike extraction must succeed (`between/less/greater` fields or ticker `B<digits>` fallback).

Given runtime network failure, this audit cannot assert whether current production mismatch is from data unavailability vs filter logic.
