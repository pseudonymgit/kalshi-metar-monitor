# Manual Fallback-Union Capture Guide

## Task Type
DIAGNOSTICS ONLY

## Target Reality
- Repository: `pseudonymgit/kalshi-metar-monitor`
- Diagnosis question: what manual evidence package can confirm or disprove the fallback-union hypothesis for mixed `real alert` + `ladder_missing` behavior when live production endpoints cannot be reached from this environment?
- Constraint: this guide is for manual operator capture against the live deployment only. Do not treat this document as proof by itself.

## Working Hypothesis
The suspected failure mode is:
1. the production runtime falls back from market-derived station authority into the deterministic fallback union,
2. the affected station remains present in the resolved live ingestion universe because it exists in config/state/watchlist/discovered-series keys,
3. that same station lacks usable discovered series and/or ladder cache data,
4. alert surfaces therefore show mixed behavior: at least one station still emits a real alert while another presents `ladder_missing` / no discovered ladder coverage.

Per current code reality, fallback-union resolution is exposed via `runtime_authority.fallback_branch_used`, while station coverage and ladder availability are exposed via `market-coverage`, `hydration-prerequisite-runtime`, `ladder_cache`, and alert-runtime surfaces. `pipeline-truth` is explicitly non-authoritative and should not be used as primary evidence.

## Capture Setup
Use two stations from the same live observation window:
- **Bad station** = the station showing `ladder_missing` or missing ladder coverage.
- **Good station** = a nearby/control station that produced a real alert or otherwise has normal ladder-backed evaluation during the same period.

If possible, capture all URLs within a single 2-5 minute window and save the full raw JSON responses verbatim.

## Exact Endpoint List
Assume production base URL:
- `https://<prod-host>`

Capture these exact URLs.

### Global / universe evidence
1. `https://<prod-host>/observability/runtime-authority-snapshot`
2. `https://<prod-host>/observability/market-coverage`
3. `https://<prod-host>/observability/alert-fire-audit`

### Bad station
4. `https://<prod-host>/observability/runtime-authority-snapshot?station=<BAD_STATION>`
5. `https://<prod-host>/observability/market-coverage?station=<BAD_STATION>`
6. `https://<prod-host>/observability/hydration-prerequisite-runtime?station=<BAD_STATION>`
7. `https://<prod-host>/observability/ladder_cache?station=<BAD_STATION>`
8. `https://<prod-host>/observability/internal-alert-runtime?station=<BAD_STATION>`
9. `https://<prod-host>/observability/alert-decision-trace?station=<BAD_STATION>`
10. `https://<prod-host>/observability/station-summary?station=<BAD_STATION>`
11. `https://<prod-host>/observability/trader-dashboard?station=<BAD_STATION>`
12. `https://<prod-host>/observability/market-eligibility-runtime?station=<BAD_STATION>`
13. `https://<prod-host>/observability/alert-preview?station=<BAD_STATION>&limit=10`

### Good station
14. `https://<prod-host>/observability/runtime-authority-snapshot?station=<GOOD_STATION>`
15. `https://<prod-host>/observability/market-coverage?station=<GOOD_STATION>`
16. `https://<prod-host>/observability/hydration-prerequisite-runtime?station=<GOOD_STATION>`
17. `https://<prod-host>/observability/ladder_cache?station=<GOOD_STATION>`
18. `https://<prod-host>/observability/internal-alert-runtime?station=<GOOD_STATION>`
19. `https://<prod-host>/observability/alert-decision-trace?station=<GOOD_STATION>`
20. `https://<prod-host>/observability/station-summary?station=<GOOD_STATION>`
21. `https://<prod-host>/observability/trader-dashboard?station=<GOOD_STATION>`
22. `https://<prod-host>/observability/market-eligibility-runtime?station=<GOOD_STATION>`
23. `https://<prod-host>/observability/alert-preview?station=<GOOD_STATION>&limit=10`

## Exact Fields To Extract
Record the following fields exactly.

### 1) Runtime authority / fallback evidence
From `runtime-authority-snapshot`:
- `runtime_authority.market_polling_stations_present`
- `runtime_authority.fallback_branch_used`
- `runtime_authority.configured_stations`
- `runtime_authority.state_stations`
- `runtime_authority.watchlist_stations`
- `runtime_authority.discovered_series_station_keys`
- `runtime_authority.market_polling_stations`
- `runtime_authority.final_resolved_stations`
- `system_health.hydration`
- `hydration_snapshot`
- `hydration_execution`
- `hydration_queue`
- `latest_alerts.rows[0:10]` for station/time correlation
- `latest_transitions.rows[0:10]` for station/time correlation

### 2) Market coverage / ladder presence evidence
From `market-coverage` for both `HIGH` and `LOW` rows:
- `station`
- `market_type`
- `station_in_live_ingestion_universe`
- `station_active_for_processing`
- `station_ingestion_configured`
- `market_type_enabled_by_config`
- `discovered_series_ticker`
- `series_discovered`
- `discovered_market_count`
- `series_market_cache_status`
- `series_market_cache_metadata.hydrated_at_utc`
- `series_market_cache_metadata.station_local_day`
- `eligible_market_count_after_filters`
- `evaluation_possible`
- `alerting_possible`
- `coverage_status`
- `coverage_reason`
- `eligible_market_tickers`
- `filtered_out_market_count`
- `filtered_out_market_counts_by_reason`
- top-level `series_discovery_source`
- top-level `series_discovery_error` if present

### 3) Hydration prerequisite evidence
From `hydration-prerequisite-runtime`:
- `retained_prerequisite_snapshot.source_authority`
- `retained_prerequisite_snapshot.value.attempted`
- `retained_prerequisite_snapshot.value.series_discovered`
- `retained_prerequisite_snapshot.value.markets_cached`
- `retained_prerequisite_snapshot.value.cache_valid`
- `retained_prerequisite_snapshot.value.status`
- `retained_prerequisite_snapshot.value.reason`
- `retained_prerequisite_snapshot.value.evaluated_at_utc`
- `live_cache_probe.value`
- `derived_runtime_assessment.cache_valid`
- `derived_runtime_assessment.markets_cached`
- `derived_runtime_assessment.reason`
- `live_queue_snapshot.value`

### 4) Ladder cache evidence
From `ladder_cache` for the target station row:
- `stations[].station`
- `stations[].hydration_state.cache_valid`
- `stations[].hydration_state.series_discovered`
- any per-station cache timestamp/day fields returned in that row

### 5) Alert-runtime evidence
From `internal-alert-runtime`:
- `station`
- `hydration_state`
- `ingestion_admission`
- `latest_observation.observed_at_utc`
- `latest_transition`
- `latest_market_outcome`
- `signal_type`
- `suppression_reason`
- `cooldown_state`
- `alerts_emitted_today`
- `first_blocking_stage`
- `diagnostic_class`

### 6) Alert decision evidence
From `alert-decision-trace`:
- `execution_mode`
- `station`
- `decision_chain`
- `terminal_state`
- `signal_type`
- `suppression_reason`
- `cooldown_state`

### 7) Station structural context
From `station-summary`:
- `station`
- `ingestion_status`
- `ingestion_status_reason`
- `latest_accepted_observation_utc`
- `freshness_lag_seconds`
- `current_epoch_selection_source`
- `epoch_status`
- `settlement_bucket`
- `prior_settlement_bucket`
- `settlement_timestamp_utc`
- `reversion_occurred`
- `first_reversion_timestamp_utc`
- `max_excursion_above_settlement`
- `terminal_state_reached`
- `last_transition_timestamp_utc`
- `last_transition_temp_f`

### 8) Trader summary context
From `trader-dashboard`:
- `station`
- `high_coverage_status`
- `high_coverage_reason`
- `low_coverage_status`
- `low_coverage_reason`
- `latest_evaluation_outcome`
- `attention_status`
- `attention_reason`
- `open_epoch_present`
- `alerts_sent_today`

### 9) Eligibility evidence
From `market-eligibility-runtime`:
- `markets_considered_count`
- `eligible_markets_count`
- `rejected_markets_count`
- `rejection_breakdown`
- `latest_evaluation_outcome`
- `latest_suppression_reason`
- `current_cache_probe`
- `latest_persisted_evaluation`

### 10) Alert evidence
From `alert-preview` and `alert-fire-audit`:
- `alert_type`
- `created_utc`
- `station`
- `market_type`
- `event_ticker`
- `direction`
- `reason`
- `attention_phrase`
- `payload_preview`
- `alerts_sent_today`
- any row showing `ladder_missing`
- any row showing a real emitted market alert for the good station in the same window

## Bad Station / Good Station Comparison Template
Copy this table into the incident ticket and fill it with exact values.

| Surface | Field | Bad station | Good station | Interpretation note |
|---|---|---:|---:|---|
| runtime-authority-snapshot | `runtime_authority.fallback_branch_used` |  |  | Must usually match process-wide reality |
| runtime-authority-snapshot | `runtime_authority.market_polling_stations_present` |  |  | `false` supports fallback branch being active |
| runtime-authority-snapshot | `runtime_authority.final_resolved_stations contains station` |  |  | Confirms inclusion in resolved universe |
| runtime-authority-snapshot | `runtime_authority.market_polling_stations contains station` |  |  | Empty/missing with resolved inclusion suggests fallback-only presence |
| runtime-authority-snapshot | `configured_stations/state_stations/watchlist_stations/discovered_series_station_keys` membership |  |  | Identify which fallback source pulled the station in |
| market-coverage | `station_in_live_ingestion_universe` |  |  | Confirms the route considers the station live |
| market-coverage | `discovered_series_ticker` |  |  | Missing on bad + present on good is strong evidence |
| market-coverage | `series_discovered` |  |  |  |
| market-coverage | `series_market_cache_status` |  |  | `cache_missing` or `cache_stale` matters |
| market-coverage | `eligible_market_count_after_filters` |  |  |  |
| market-coverage | `coverage_reason` |  |  | Expected bad values: `no_discovered_series`, `cache_not_yet_populated`, `cache_stale` |
| market-coverage | `series_discovery_source` |  |  | `cache_only_fallback` weakens confidence in live discovery visibility |
| hydration-prerequisite-runtime | `retained_prerequisite_snapshot.value.series_discovered` |  |  |  |
| hydration-prerequisite-runtime | `retained_prerequisite_snapshot.value.markets_cached` |  |  |  |
| hydration-prerequisite-runtime | `retained_prerequisite_snapshot.value.cache_valid` |  |  |  |
| hydration-prerequisite-runtime | `derived_runtime_assessment.reason` |  |  |  |
| ladder_cache | `hydration_state.cache_valid` |  |  |  |
| ladder_cache | `hydration_state.series_discovered` |  |  |  |
| internal-alert-runtime | `latest_market_outcome` |  |  |  |
| internal-alert-runtime | `first_blocking_stage` |  |  |  |
| internal-alert-runtime | `diagnostic_class` |  |  |  |
| alert-decision-trace | `terminal_state` |  |  |  |
| alert-decision-trace | first `BLOCK` reason |  |  |  |
| trader-dashboard | `latest_evaluation_outcome` |  |  |  |
| trader-dashboard | `high_coverage_reason` / `low_coverage_reason` |  |  |  |
| alert-preview / fire-audit | recent real alert present in same window |  |  | Confirms mixed behavior actually happened |
| alert-preview / fire-audit | recent `ladder_missing` present |  |  | Confirms the bad-path symptom |

## Interpretation Rules

### Confirmed fallback re-entry
Classify as **confirmed fallback re-entry** when all of the following are true:
1. `runtime_authority.fallback_branch_used=true`.
2. `runtime_authority.market_polling_stations_present=false`.
3. The bad station is still present in `runtime_authority.final_resolved_stations`.
4. The bad station is explainable by fallback inputs (`configured_stations`, `state_stations`, `watchlist_stations`, and/or `discovered_series_station_keys`) rather than live `market_polling_stations` membership.
5. The bad station shows ladder absence on at least one ladder surface, for example:
   - `market-coverage.coverage_reason=no_discovered_series`, or
   - `market-coverage.series_market_cache_status=cache_missing`, or
   - `hydration-prerequisite-runtime.derived_runtime_assessment.cache_valid=false`, or
   - `ladder_cache.hydration_state.cache_valid=false`.
6. The good station, captured in the same time window, shows normal ladder-backed evaluation or a real alert (`alert-preview` / `alert-fire-audit`), proving the system was not globally dark.

This combination means the process re-entered the fallback union and kept the bad station live enough to appear in observability/ingestion scope, while ladder prerequisites for that station were missing or unusable.

### Weakened hypothesis
Classify as **weakened hypothesis** when fallback evidence exists but causality is incomplete or mixed, for example:
- `fallback_branch_used=true`, but the bad station also has `series_discovered=true`, valid cache, and eligible markets.
- Both bad and good stations lack cache/series, suggesting a broader hydration outage rather than station-specific fallback-union behavior.
- Mixed alerts cannot be confirmed in the same observation window.
- `series_discovery_source=cache_only_fallback`, making observability itself partially degraded and reducing confidence in whether the bad station is missing because of fallback-union inclusion or because the cache snapshot is stale/incomplete.

In this case the hypothesis remains plausible, but manual evidence does not isolate fallback re-entry as the decisive cause.

### Disproven hypothesis
Classify as **disproven hypothesis** when any of the following hold:
- `runtime_authority.fallback_branch_used=false` and `market_polling_stations_present=true` during the capture window.
- The bad station is present because it is in `market_polling_stations`, not merely because of fallback-union sources.
- The bad station has discovered series, valid cache, eligible markets, and no hydration/coverage defect; the symptom is instead explained by suppression, cooldown, terminal state, or no transition.
- No mixed behavior is actually present: there is no contemporaneous real alert for the control station and/or no contemporaneous `ladder_missing` evidence for the bad station.

That outcome means the operator should stop blaming fallback-union re-entry and instead pivot to the specific blocking stage shown in `internal-alert-runtime` / `alert-decision-trace`.

## Practical Capture Notes
- Save full JSON, not screenshots alone.
- Record the exact capture timestamp in UTC.
- If a station-specific endpoint returns `400`, record that as evidence rather than retrying with mutations.
- Do **not** use `GET /observability/pipeline-truth` as primary proof; repository docs mark it as non-authoritative for this diagnosis class.
- If available, include one sanitized log excerpt around the same timestamp showing the station code and either `ladder_missing`, `cache_missing`, or emitted alert evidence. This is supporting evidence only, not the primary classification source.

## Recommended Next Action After Manual Capture
If the package confirms fallback re-entry, open a follow-up implementation task to add one operator-facing observability field that explicitly states **which fallback source(s)** admitted each final resolved station into the universe, so production diagnosis no longer depends on manual set-difference reconstruction.
