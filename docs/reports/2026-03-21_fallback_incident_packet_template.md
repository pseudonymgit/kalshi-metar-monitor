# Fallback Incident Packet Template

## Task Type
DIAGNOSTICS ONLY

## REPO_STATE
- branch: `work`
- commit: `2af6321ccd44156087bf44133b913a320e93b5e4`

## Question
What is the cleanest operator-ready incident packet structure for collecting and pasting one bad-station / one good-station production evidence set for the fallback-universe alert issue?

## Verdict
Use one **minimal three-block incident packet**:
1. **Shared / global context** to prove process-wide fallback state and capture-time coherence.
2. **Bad station block** to prove the ladder-missing failure path.
3. **Good station block** to prove the system was still capable of normal ladder-backed evaluation in the same window.

This is the smallest structure that still preserves verdict power for **confirmed fallback re-entry**, **weakened hypothesis**, and **disproven hypothesis** classification.

## Why this is the cleanest packet
The current manual capture guide is strong for completeness, but it is too large for fast operator paste/review because it spans 23 endpoint captures and many fields. The decisive classification logic only needs:
- one process-wide fallback proof,
- one bad-station ladder-failure proof,
- one good-station normal-behavior proof,
- one shared time window proving both station observations are contemporaneous.

Everything else is supporting context.

## Essential vs Optional Evidence

### Required fields
These are the minimum fields that preserve verdict power.

#### A. Shared / global context
Required source surfaces:
- `GET /observability/runtime-authority-snapshot`
- `GET /observability/alert-fire-audit`

Required fields:
- capture timestamp in UTC
- bad station ICAO
- good station ICAO
- `runtime_authority.fallback_branch_used`
- `runtime_authority.market_polling_stations_present`
- `runtime_authority.final_resolved_stations` membership for both stations
- `runtime_authority.market_polling_stations` membership for both stations
- fallback-source membership for the bad station from:
  - `runtime_authority.configured_stations`
  - `runtime_authority.state_stations`
  - `runtime_authority.watchlist_stations`
  - `runtime_authority.discovered_series_station_keys`
- one same-window alert audit row proving a real emitted alert for the good station, if available globally

Why required:
- Without these fields, the packet cannot prove that fallback was active, cannot show whether the bad station was fallback-admitted, and cannot prove mixed behavior happened in the same process window.

#### B. Bad station block
Required source surfaces:
- `GET /observability/market-coverage?station=<BAD_STATION>`
- `GET /observability/hydration-prerequisite-runtime?station=<BAD_STATION>`
- `GET /observability/internal-alert-runtime?station=<BAD_STATION>`
- `GET /observability/alert-decision-trace?station=<BAD_STATION>`
- `GET /observability/alert-preview?station=<BAD_STATION>&limit=10`

Required fields:
- `station_in_live_ingestion_universe`
- `discovered_series_ticker`
- `series_discovered`
- `series_market_cache_status`
- `eligible_market_count_after_filters`
- `coverage_reason`
- `retained_prerequisite_snapshot.value.series_discovered`
- `retained_prerequisite_snapshot.value.markets_cached`
- `retained_prerequisite_snapshot.value.cache_valid`
- `derived_runtime_assessment.cache_valid`
- `derived_runtime_assessment.reason`
- `latest_market_outcome`
- `first_blocking_stage`
- `diagnostic_class`
- `terminal_state`
- first blocking reason in `decision_chain`
- one alert-preview row showing `ladder_missing` or equivalent bad-path symptom in the same window

Why required:
- This set is the minimum needed to prove the bad station remained live in station scope but lacked usable ladder/discovery prerequisites and failed at a specific alert stage.

#### C. Good station block
Required source surfaces:
- `GET /observability/market-coverage?station=<GOOD_STATION>`
- `GET /observability/internal-alert-runtime?station=<GOOD_STATION>`
- `GET /observability/alert-preview?station=<GOOD_STATION>&limit=10`
- optionally `GET /observability/alert-fire-audit` if the good-station emitted row is easier to capture there than in preview

Required fields:
- `series_discovered`
- `series_market_cache_status`
- `eligible_market_count_after_filters`
- `coverage_reason`
- `latest_market_outcome`
- `diagnostic_class`
- one same-window row showing a real alert or otherwise normal ladder-backed evaluation for the good station

Why required:
- The good station is only a control. It does not need the full deep-dive packet. It only needs enough evidence to prove the system was not globally dark and that mixed behavior was real.

### Optional but useful supporting fields
Include these only when the required packet leaves ambiguity.

#### Shared / global optional fields
- `system_health.hydration`
- `hydration_snapshot`
- `hydration_execution`
- `hydration_queue`
- `latest_alerts.rows[0:10]`
- `latest_transitions.rows[0:10]`

Use when:
- the time window is disputed,
- operators need additional process-wide correlation,
- a reviewer wants to see whether hydration instability was broader than the two stations.

#### Bad station optional fields
- `series_discovery_source`
- `series_discovery_error`
- `live_cache_probe.value`
- `live_queue_snapshot.value`
- `ladder_cache.stations[].hydration_state.cache_valid`
- `ladder_cache.stations[].hydration_state.series_discovered`
- ladder cache timestamp/day metadata
- `hydration_state`
- `ingestion_admission`
- `signal_type`
- `suppression_reason`
- `cooldown_state`
- `station-summary` structural timing fields
- `trader-dashboard` coverage and attention fields
- `market-eligibility-runtime` rejection breakdown
- one sanitized log excerpt around the same timestamp

Use when:
- `coverage_reason` and hydration fields disagree,
- `cache_only_fallback` weakens confidence,
- the blocking stage needs secondary confirmation,
- reviewers need a faster path to the non-fallback alternate explanation.

#### Good station optional fields
- `discovered_series_ticker`
- `first_blocking_stage`
- `terminal_state`
- `trader-dashboard.latest_evaluation_outcome`
- `market-eligibility-runtime` counts

Use when:
- the “good” station did not emit but still appears healthy,
- the reviewer needs proof that “good” means ladder-backed evaluation rather than merely absence of obvious failure.

## Recommended operator-facing packet structure
Paste exactly one packet per incident, using this shape.

### 1. Shared / global context
- Capture window UTC:
- Prod host:
- Bad station:
- Good station:
- `fallback_branch_used`:
- `market_polling_stations_present`:
- Bad station in `final_resolved_stations`:
- Good station in `final_resolved_stations`:
- Bad station in `market_polling_stations`:
- Good station in `market_polling_stations`:
- Bad station fallback-source membership:
  - configured:
  - state:
  - watchlist:
  - discovered-series keys:
- Same-window real alert evidence for good station:

### 2. Bad station block
- Station:
- `station_in_live_ingestion_universe`:
- `discovered_series_ticker`:
- `series_discovered`:
- `series_market_cache_status`:
- `eligible_market_count_after_filters`:
- `coverage_reason`:
- `retained_prerequisite_snapshot.value.series_discovered`:
- `retained_prerequisite_snapshot.value.markets_cached`:
- `retained_prerequisite_snapshot.value.cache_valid`:
- `derived_runtime_assessment.cache_valid`:
- `derived_runtime_assessment.reason`:
- `latest_market_outcome`:
- `first_blocking_stage`:
- `diagnostic_class`:
- `terminal_state`:
- First block reason from `decision_chain`:
- Same-window `ladder_missing` evidence:

### 3. Good station block
- Station:
- `series_discovered`:
- `series_market_cache_status`:
- `eligible_market_count_after_filters`:
- `coverage_reason`:
- `latest_market_outcome`:
- `diagnostic_class`:
- Same-window real alert or normal ladder-backed evaluation evidence:

### 4. Classification
Choose exactly one:
- **confirmed fallback re-entry**
- **weakened hypothesis**
- **disproven hypothesis**

### 5. One-line rationale
- <why the chosen classification follows from the packet>

## Minimal review rubric
A reviewer should be able to answer these five questions in under a minute:
1. Was fallback active process-wide?
2. Was the bad station still in the resolved universe?
3. Was the bad station absent from live market-polling authority but present via fallback sources?
4. Did the bad station lack usable ladder/discovery prerequisites?
5. Did the good station show contemporaneous normal behavior or a real alert?

If any answer is missing, the packet is too thin.

## What to leave out by default
Do not require these in the first operator paste unless ambiguity remains:
- full raw payload dumps in the ticket body
- both HIGH and LOW rows when one market type already proves the defect
- full station-summary chronology
- full trader-dashboard summary
- full market-eligibility rejection breakdown
- ladder-cache detail when hydration-prerequisite-runtime already proves cache invalidity
- pipeline-truth output

These surfaces are useful, but they are not first-pass requirements for fallback-universe verdicting.

## Single recommended next action
Replace the current large manual capture checklist in the operator workflow for this issue class with the three-block incident packet above, and escalate to optional/supporting fields only when the first-pass packet lands in **weakened hypothesis**.
