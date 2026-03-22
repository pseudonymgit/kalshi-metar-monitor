# Alert Path Evidence Crosswalk

## Task Type
DIAGNOSTICS ONLY

## Target Reality
- Repository: `pseudonymgit/kalshi-metar-monitor`
- Verified branch at diagnosis time: `docs/alert-path-evidence-crosswalk`
- Verified commit at diagnosis start: `2af6321ccd44156087bf44133b913a320e93b5e4`

## Operator Question
Which current observability endpoint fields prove each stage of the alert path for station-scoped diagnosis, especially admission, ladder availability, evaluation, suppression, and emission?

## Recommended Surface Order
1. **Primary:** `/observability/alert-decision-trace?station=<ICAO>`
2. **Primary:** `/observability/alert-path-truth?station=<ICAO>`
3. **Primary:** `/observability/market-coverage?station=<ICAO>`
4. **Supporting:** `/observability/hydration-prerequisite-runtime?station=<ICAO>`
5. **Supporting:** `/observability/runtime-authority-snapshot?station=<ICAO>`
6. **Supporting summary only:** `/observability/internal-alert-runtime?station=<ICAO>`

## Stage-to-Field Crosswalk

| Diagnosis question | Primary evidence | Supporting evidence | Interpretation |
|---|---|---|---|
| **Was the station admitted to execution?** | `/observability/alert-decision-trace`: `decision_chain[0].stage == "INGESTION_ADMISSION"`; `decision_chain[0].status`; `decision_chain[0].reason`; `terminal_state` | `/observability/runtime-authority-snapshot`: `hydration_snapshot.stations.<ICAO>.ingestion_admission.admitted_to_fetch`, `hydration_snapshot.stations.<ICAO>.ingestion_admission.skip_reason`; `/observability/internal-alert-runtime`: `ingestion_admission`, `first_blocking_stage` | `PASS` at `INGESTION_ADMISSION` proves admission. `BLOCK` plus `station_not_admitted:<reason>` proves the station never entered downstream evaluation. |
| **Is ladder / series availability missing?** | `/observability/market-coverage`: per market-type row `discovered_series_ticker`, `series_discovered`, `series_market_cache_status`, `coverage_status`, `coverage_reason`, `eligible_market_count_after_filters` | `/observability/hydration-prerequisite-runtime`: top-level `status`, `reason`, `cache_valid`, `markets_cached`; nested `live_cache_probe.value.raw_market_count`, `live_cache_probe.value.cache_present`; `/observability/runtime-authority-snapshot`: `hydration_snapshot.stations.<ICAO>.hydration_prerequisite.*`, `hydration_snapshot.stations.<ICAO>.cache_present` | `coverage_reason == no_discovered_series` proves series missing. `series_market_cache_status == cache_missing` or hydration `reason in {cache_missing, cache_empty}` proves ladder/cache unavailable. `eligible_market_count_after_filters == 0` with discovered series means series exists but usable ladder rows did not survive filters. |
| **Did market evaluation run?** | `/observability/alert-decision-trace`: presence of `MARKET_MATCH`, `SUPPRESSION_GATE`, or `ALERT_EMISSION` stages in `decision_chain`; `/observability/alert-path-truth`: `send_alert_entered` | `/observability/runtime-authority-snapshot`: `latest_transitions.rows[0].metadata_json`-derived fields are summarized as `latest_alerts` / `latest_transitions`; `/observability/internal-alert-runtime`: `latest_market_outcome`; `/observability/market-coverage`: `evaluation_possible` is only readiness, not proof of a completed evaluation | Reaching `MARKET_MATCH` or beyond proves the path got past admission/observation/transition checks. `send_alert_entered == true` proves `_send_alert` was entered for the persisted/latest transition. |
| **Was the path blocked because there was no settlement transition?** | `/observability/alert-decision-trace`: `decision_chain` contains `SETTLEMENT_TRANSITION` with `status == BLOCK`; `terminal_state == BLOCKED_NO_SETTLEMENT_TRANSITION` | `/observability/internal-alert-runtime`: `first_blocking_stage == SETTLEMENT_TRANSITION`, `diagnostic_class == TRANSITION`; `/observability/runtime-authority-snapshot`: `latest_transitions.rows` | This is the authoritative “no transition” proof. Do not infer it from missing alerts alone. |
| **Was the path blocked because no eligible market matched?** | `/observability/alert-decision-trace`: `MARKET_MATCH` stage with `status == BLOCK`; `terminal_state == BLOCKED_NO_MARKET_MATCH` | `/observability/alert-path-truth`: `blocking_stage == market_match`, `suppression_or_non_emission_reason == NO_ELIGIBLE_MARKET`; `/observability/market-coverage`: `eligible_market_count_after_filters == 0`, `coverage_reason == no_eligible_markets_after_filters` | Decision trace is the primary path-stage proof; market coverage explains why a market could not match. |
| **Was the path suppressed after evaluation?** | `/observability/alert-path-truth`: `blocking_stage == suppression_gate`, `suppression_or_non_emission_reason`; `/observability/alert-decision-trace`: `SUPPRESSION_GATE` stage with `status == BLOCK`, `terminal_state == BLOCKED_SUPPRESSED` | `/observability/internal-alert-runtime`: `suppression_reason`, `latest_market_outcome`, `first_blocking_stage == SUPPRESSION_GATE`; `/observability/runtime-authority-snapshot`: latest transition metadata via `latest_transitions.rows` | Use `alert-path-truth` for the exact persisted/non-emission reason and whether the path entered send-alert handling. |
| **Was the path terminal-state blocked?** | `/observability/alert-path-truth`: `blocking_stage == suppression_gate`, `suppression_or_non_emission_reason == TERMINAL_STATE` | `/observability/internal-alert-runtime`: `latest_market_outcome == TERMINAL_STATE` or suppression summary; `/observability/alert-decision-trace`: it will appear as suppression-gate block rather than a distinct terminal-state stage | Terminal-state is represented as a suppression-gate outcome, not a separate top-level decision-trace stage. |
| **Was an alert actually sent?** | `/observability/alert-path-truth`: `send_alert_entered == true`, `webhook_send_attempted == true`, `webhook_send_succeeded == true`, `webhook_send_failed == false`; `/observability/alert-decision-trace`: `terminal_state == ALERTABLE` and all stages `PASS` | `/observability/internal-alert-runtime`: `latest_market_outcome == ALERT_SENT`, `alerts_emitted_today`; `/observability/runtime-authority-snapshot`: `latest_alerts.rows` filtered by station | `alert-path-truth` is the emission authority. Decision trace shows the stage path, but alert-path-truth proves the delivery attempt/success outcome. |
| **Was the path blocked during emission/webhook delivery?** | `/observability/alert-path-truth`: `blocking_stage == webhook_delivery` or `blocking_stage == alert_emission`; inspect `suppression_or_non_emission_reason`, `webhook_send_attempted`, `webhook_send_succeeded`, `webhook_send_failed` | `/observability/internal-alert-runtime`: `first_blocking_stage == ALERT_EMISSION`, `diagnostic_class == EMISSION` | This is distinct from suppression. Use alert-path-truth to separate “decision suppressed” from “delivery attempted but failed.” |

## Endpoint Authority Notes

### Primary surfaces
- **`/observability/alert-decision-trace`** is the primary stage-order surface for: admission → observation present → settlement transition → market match → suppression gate → alert emission.
- **`/observability/alert-path-truth`** is the primary send-alert outcome surface for: entered send-alert, exact blocking stage, suppression/non-emission reason, webhook attempted/succeeded/failed.
- **`/observability/market-coverage`** is the primary ladder/series availability and market-match readiness surface.

### Supporting surfaces
- **`/observability/hydration-prerequisite-runtime`** explains whether the live cache probe says the station has a usable cached ladder right now, and whether the retained prerequisite snapshot disagrees.
- **`/observability/runtime-authority-snapshot`** is the best one-stop supporting bundle when you need surrounding authority context: universe resolution, hydration admission, queue state, latest transitions, latest alerts.
- **`/observability/internal-alert-runtime`** is a fast summary surface for operators, but it collapses multiple deeper sources into `first_blocking_stage` and `diagnostic_class` and should not be treated as the sole authority.

### Non-authoritative surfaces to avoid for root-cause proof
- **Avoid using `/observability/internal-alert-runtime` alone** to prove exact ladder or delivery cause; it is a derived summary.
- **Avoid using `/observability/trader-dashboard` or `/observability/alert-diagnostics` for root-cause proof**; they are operator summaries that compress multiple sources into attention/diagnostic classes.
- **Avoid using `/observability/alert-preview` for proof of suppression or admission**; it shows recent alert examples, not the authoritative blocked-path reason chain.

## Fast Triage Pattern
1. Open `/observability/alert-decision-trace?station=<ICAO>` to find the first blocked stage.
2. If blocked at `INGESTION_ADMISSION`, open `/observability/market-coverage` and `/observability/hydration-prerequisite-runtime` for series/cache proof.
3. If blocked at `MARKET_MATCH`, use `/observability/market-coverage` for eligible-market evidence.
4. If blocked at `SUPPRESSION_GATE` or `ALERT_EMISSION`, open `/observability/alert-path-truth` for the exact persisted send-alert outcome.
5. Use `/observability/runtime-authority-snapshot` only to confirm surrounding context, not as the first station-stage proof surface.

## Single Recommended Next Action
Standardize operator runbooks on this sequence: **`alert-decision-trace` first, then `alert-path-truth`, then `market-coverage`**.
