# Filtered-to-Zero Diagnosis Crosswalk

## Task Type
DIAGNOSTICS ONLY

## Target Reality
- Repository: `pseudonymgit/kalshi-metar-monitor`
- Verified local branch at report time: `work`
- Verified local commit at report time: `fab958f5e28c3f81a884c9701b26be1013854efd`
- Report artifact status before this task: missing and created fresh from current checkout reality.

## Operator Question
On current repo reality, what exactly does `filtered_to_zero` mean on the observability surfaces, and which fields should operators inspect first to distinguish cache/series problems from post-filter candidate collapse?

## Executive Answer
`filtered_to_zero` means the station had cached market rows to consider, but zero markets survived the final directional/strike eligibility filter in the live cache probe used by `GET /observability/market-eligibility-runtime`.

It does **not** mean:
- series discovery failed,
- the ladder cache is missing,
- the ladder cache is empty,
- the ladder cache is stale,
- or that a later suppression/delivery gate blocked an otherwise eligible market.

For current code reality, operators should treat `GET /observability/market-eligibility-runtime?station=<ICAO>` as the **primary authority** for the exact `filtered_to_zero` classification, then use `GET /observability/market-coverage?station=<ICAO>` and `GET /observability/hydration-prerequisite-runtime?station=<ICAO>` as the first supporting surfaces to rule out series/cache defects before escalating to alert-path suppression surfaces.

## Current Code Reality

### 1) Exact meaning of `filtered_to_zero`
In `GET /observability/market-eligibility-runtime`, the route builds a live `current_cache_probe` from cached structured markets. If the cache probe's `empty_reason` is the stale legacy string `no_directional_ladder_match`, the route normalizes that to `filtered_to_zero`. The route then classifies the live evaluation as:
- `NO_MARKETS_CACHED` with `latest_suppression_reason=cache_empty` when `markets_considered_count == 0`
- `NO_ELIGIBLE_MARKET` with `latest_suppression_reason=filtered_to_zero` when `markets_considered_count > 0` but `eligible_markets_count == 0`
- `ELIGIBLE_MARKET_PRESENT` otherwise

So the current meaning is:
> **markets were present in cache, but none survived the post-directional eligibility filter stack.**

This is a **post-cache, post-series, post-market-load collapse** classification.

### 2) Where operators will see equivalent semantics on other surfaces
The equivalent operator-facing idea appears on other surfaces with different wording:
- `GET /observability/market-coverage` reports `coverage_reason=no_eligible_markets_after_filters` when the station has discovered series and non-stale cache, but `eligible_market_count_after_filters == 0`.
- `GET /observability/trader-dashboard` can bubble that up as `attention_reason=no_eligible_markets` if both HIGH and LOW rows collapse after filters.
- `GET /observability/alert-decision-trace` reports `BLOCKED_NO_MARKET_MATCH` when the latest runtime outcome is `NO_ELIGIBLE_MARKET`.
- `GET /observability/internal-alert-runtime` can report `latest_market_outcome=NO_ELIGIBLE_MARKET`, but that surface is already downstream of hydration/admission/transition context and is not the best first proof that the collapse happened in the candidate filter stage.

## Primary vs Supporting Evidence

### Primary authority surface
Use `GET /observability/market-eligibility-runtime?station=<ICAO>` first.

Inspect these fields in this order:
1. `markets_considered_count`
2. `eligible_markets_count`
3. `latest_evaluation_outcome`
4. `latest_suppression_reason`
5. `current_cache_probe.empty_reason`
6. `current_cache_probe.pre_directional_market_count`
7. `current_cache_probe.post_directional_market_count`
8. `rejection_breakdown.directional_strike_rejected`
9. `latest_persisted_evaluation.market_eligibility_runtime.*` only as supporting retained context

### First supporting surfaces
Use these immediately after the primary surface.

#### A. `GET /observability/market-coverage?station=<ICAO>`
Inspect, for both HIGH and LOW rows:
1. `discovered_series_ticker`
2. `series_discovered`
3. `series_market_cache_status`
4. `discovered_market_count`
5. `eligible_market_count_after_filters`
6. `coverage_reason`
7. `filtered_out_market_counts_by_reason`
8. top-level `series_discovery_source`

Why second: this is the fastest surface for separating `no_discovered_series`, `cache_not_yet_populated`, `cache_stale`, and `no_eligible_markets_after_filters`.

#### B. `GET /observability/hydration-prerequisite-runtime?station=<ICAO>`
Inspect:
1. `derived_runtime_assessment.cache_valid`
2. `derived_runtime_assessment.markets_cached`
3. `derived_runtime_assessment.reason`
4. `retained_prerequisite_snapshot.value.series_discovered`
5. `retained_prerequisite_snapshot.value.markets_cached`
6. `retained_prerequisite_snapshot.value.cache_valid`
7. `live_cache_probe.value.raw_market_count`
8. `live_cache_probe.value.cache_present`

Why third: this surface is authoritative for whether the process currently sees usable ladder cache at all, but it does **not** by itself classify post-filter candidate collapse.

### Supporting but later-stage alert-path surfaces
Only after the three surfaces above, use:
- `GET /observability/internal-alert-runtime?station=<ICAO>`
- `GET /observability/alert-decision-trace?station=<ICAO>`

Inspect these later-stage fields:
- `latest_market_outcome`
- `first_blocking_stage`
- `diagnostic_class`
- `terminal_state`
- `decision_chain`
- `suppression_reason`
- `cooldown_state`

Why later: these surfaces are best for distinguishing **post-evaluation suppression/blocking** from market-match failure, not for proving whether the root cause was missing series/cache versus filter collapse.

## Classification Crosswalk

| Diagnosis bucket | Primary evidence | First supporting evidence | What it means operationally |
|---|---|---|---|
| **Series missing** | `market-coverage.series_discovered=false`, `coverage_reason=no_discovered_series` | `hydration-prerequisite-runtime.retained_prerequisite_snapshot.value.series_discovered=false` | The station never got a usable discovered series, so `filtered_to_zero` is the wrong interpretation. |
| **Cache missing / empty** | `hydration-prerequisite-runtime.derived_runtime_assessment.cache_valid=false`, `markets_cached=false`, `reason=cache_missing` or `cache_empty` | `market-coverage.series_market_cache_status=cache_missing`; `market-eligibility-runtime.markets_considered_count=0` | Markets were not available in cache for evaluation. This is not post-filter collapse. |
| **Cache stale** | `market-coverage.series_market_cache_status=cache_stale` | `series_market_cache_metadata.station_local_day` mismatch; system hydration reason may show stale cache | Cached series exists but should not be trusted as current-day evaluation input. This is not `filtered_to_zero`. |
| **Candidates existed, then collapsed after filters** | `market-eligibility-runtime.markets_considered_count>0`, `eligible_markets_count=0`, `latest_evaluation_outcome=NO_ELIGIBLE_MARKET`, `latest_suppression_reason=filtered_to_zero` | `market-coverage.coverage_reason=no_eligible_markets_after_filters`; `eligible_market_count_after_filters=0`; nonzero filtered counts | This is the true current meaning of `filtered_to_zero`. |
| **Later suppression / blocking after evaluation** | `market-eligibility-runtime.eligible_markets_count>0` | `internal-alert-runtime.latest_market_outcome=SUPPRESSED_*` or `TERMINAL_STATE`; `alert-decision-trace` shows `SUPPRESSION_GATE` or `ALERT_EMISSION` block | Eligible markets existed; failure happened after market match. Do not blame `filtered_to_zero`. |

## Recommended Triage Order
1. **Start with `market-eligibility-runtime`.**
   - If `markets_considered_count == 0`, stop calling it `filtered_to_zero`; this is cache missing/empty.
   - If `markets_considered_count > 0` and `eligible_markets_count == 0`, continue the `filtered_to_zero` branch.
2. **Check `market-coverage`.**
   - If `coverage_reason=no_discovered_series`, downgrade immediately to series-discovery failure.
   - If `series_market_cache_status=cache_missing` or `cache_stale`, downgrade immediately to cache/hydration issue.
   - If `coverage_reason=no_eligible_markets_after_filters`, keep the `filtered_to_zero` diagnosis.
3. **Check `hydration-prerequisite-runtime`.**
   - If `derived_runtime_assessment.cache_valid=false` or `markets_cached=false`, this is cache-side, not post-filter collapse.
   - If cache is valid and markets cached, keep the post-filter collapse diagnosis.
4. **Only then check alert-path surfaces.**
   - If they show `SUPPRESSED_*`, cooldown, webhook, or terminal-state blocks while eligibility remained positive, classify as later suppression/blocking.

## Primary Fields Operators Should Inspect First
When the operator only has time for one tight pass, inspect these exact fields in order:

1. `market-eligibility-runtime.markets_considered_count`
2. `market-eligibility-runtime.eligible_markets_count`
3. `market-eligibility-runtime.latest_suppression_reason`
4. `market-eligibility-runtime.current_cache_probe.empty_reason`
5. `market-coverage.series_discovered`
6. `market-coverage.series_market_cache_status`
7. `market-coverage.coverage_reason`
8. `hydration-prerequisite-runtime.derived_runtime_assessment.cache_valid`
9. `hydration-prerequisite-runtime.derived_runtime_assessment.markets_cached`
10. `internal-alert-runtime.first_blocking_stage` only after the first nine fields are understood

## Misleading or Non-Authoritative Surfaces to Avoid as First Proof
- **Do not start with `GET /observability/pipeline-truth`.** Current docs explicitly mark it as partial / logic-stubbed and non-authoritative.
- **Do not start with `internal-alert-runtime` if the question is specifically what `filtered_to_zero` means.** It is a later-stage runtime summary and can conflate market-match failure with downstream suppression context.
- **Do not start with `trader-dashboard` for root cause.** It is a compact operator summary that intentionally compresses underlying coverage reasons.
- **Do not use retained persisted evaluation alone as proof of current live cache reality.** `latest_persisted_evaluation` is useful context, but the reportable live meaning of `filtered_to_zero` comes from the route's current cache probe.

## Bottom-Line Interpretation Rule
Use `filtered_to_zero` only when all of the following are true on current observability reality:
1. the station has discovered series,
2. the station has cached markets available for consideration,
3. the cache is not being classified as missing/empty/stale for the diagnosis,
4. `markets_considered_count > 0`, and
5. `eligible_markets_count == 0` after directional/strike filtering.

If any of 1-3 fail, operators should classify the issue as **series/cache/hydration** rather than **post-filter candidate collapse**.

## Surfaces Examined
- `app.py`
- `docs/API_REFERENCE.md`
- `docs/OPERATIONS.md`
- `docs/reports/2026-03-21_hydration_observability_authority_cleanup.md`
- `docs/reports/2026-03-21_manual_fallback_universe_capture_guide.md`

## Next Best Task
Capture one live station example and validate this crosswalk against real production JSON from:
1. `GET /observability/market-eligibility-runtime?station=<ICAO>`
2. `GET /observability/market-coverage?station=<ICAO>`
3. `GET /observability/hydration-prerequisite-runtime?station=<ICAO>`
4. `GET /observability/internal-alert-runtime?station=<ICAO>`
5. `GET /observability/alert-decision-trace?station=<ICAO>`
