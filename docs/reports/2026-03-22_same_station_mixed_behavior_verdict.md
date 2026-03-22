# Same-Station Mixed Behavior Verdict — 2026-03-22

## Task Type
DIAGNOSTICS ONLY

## Verdict
REVISE

## Concurrency
NOT PARALLEL-SAFE

## REPO_STATE
- branch: `diagnostics/same-station-mixed-behavior-verdict`
- commit_at_diagnosis_start: `add16fbbad7049e486730a6255bea880f781ae39`
- base_url_targeted: `https://kalshi-metar-monitor.onrender.com`

## Operator Question
For one station that shows both a real ladder-backed alert and a ladder-missing / filtered-to-zero event in a short interval, what exact observability fields changed between the good evaluation and the bad evaluation?

## Decision
**Evidence still insufficient.**

I could verify the repository state, the current observability route semantics, the required diagnosis surfaces, and the production base URL from repository docs/code. I could **not** verify one same-station good timestamp plus one same-station bad timestamp from live production because this environment cannot reach the Render deployment over the available shell network path: both `curl` and `requests` fail with `CONNECT tunnel failed, response 403`. Because the operator question requires one exact station and two exact evaluation timestamps, any station/timestamp claim beyond that point would be assumption rather than verified target reality.

## What Was Verified

### 1. Required diagnosis surfaces and their intended authority
The repository already standardizes the exact primary surfaces requested in this task:
- `/observability/alert-decision-trace` is the primary stage-order proof surface.
- `/observability/alert-path-truth` is the primary send-alert outcome / non-emission reason surface.
- `/observability/market-coverage` is the primary ladder/series/eligibility surface.
- `/observability/runtime-authority-snapshot` is the primary supporting bundle for runtime authority, hydration, latest transitions, and latest alerts.  
These priorities are explicitly documented in the alert-path evidence crosswalk and manual fallback capture guide.  
RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/docs/reports/2026-03-21_alert_path_evidence_crosswalk.md  
RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/docs/reports/2026-03-21_manual_fallback_universe_capture_guide.md

### 2. Current live symptom statement in repo docs
The current repository state document explicitly says:
- real alerts are occurring again for some stations,
- ladder-missing alerts are still also occurring,
- the same city/station family can produce both behaviors.  
That confirms the prompt premise is plausible, but it does **not** identify a verified same-station same-window pair by itself.  
RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/docs/CURRENT_STATE.md

### 3. Exact field semantics from current route code
The current route code shows the exact observability fields that would differ between a successful ladder-backed alert path and each of the two bad-path families relevant to this task.

#### Good ladder-backed alert path
A successful emitted evaluation appears as:
- `alert-decision-trace.terminal_state = ALERTABLE`
- every `decision_chain` stage passes through `ALERT_EMISSION`
- `alert-path-truth.send_alert_entered = true`
- `alert-path-truth.blocking_stage = null`
- `alert-path-truth.suppression_or_non_emission_reason = null`
- `alert-path-truth.webhook_send_attempted = true`
- `alert-path-truth.webhook_send_succeeded = true`
- `alert-path-truth.webhook_send_failed = false`  
RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/app.py

#### Bad path family A: filtered-to-zero / no eligible market
A filtered-to-zero failure appears as:
- `market-coverage.eligible_market_count_after_filters = 0`
- `market-coverage.coverage_reason = no_eligible_markets_after_filters`
- `alert-decision-trace` blocks at `MARKET_MATCH`
- `alert-decision-trace.terminal_state = BLOCKED_NO_MARKET_MATCH`
- `alert-path-truth.send_alert_entered = true`
- `alert-path-truth.blocking_stage = market_match`
- `alert-path-truth.suppression_or_non_emission_reason = NO_ELIGIBLE_MARKET`
- `alert-path-truth.webhook_send_attempted = false`  
RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/app.py

#### Bad path family B: ladder missing / hydration-cache invalid
A ladder-missing or cache-invalid failure appears as:
- `market-coverage.discovered_series_ticker = null` **or** cache status degrades to `cache_missing` / `cache_stale`
- `market-coverage.coverage_reason = no_discovered_series | cache_not_yet_populated | cache_stale`
- `runtime-authority-snapshot.hydration_snapshot.stations.<station>.cache_present` drops false and/or hydration prerequisite fields show invalid cache
- `alert-decision-trace` blocks immediately at `INGESTION_ADMISSION`
- `alert-decision-trace.terminal_state = BLOCKED_INGESTION_ADMISSION`
- the first block reason becomes `station_not_admitted:<skip_reason>` (defaulting to `ladder_not_hydrated` in the route logic)
- `alert-path-truth.send_alert_entered = false` unless a persisted transition record says otherwise.  
RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/app.py

## Exact Station / Timestamp Pair
- station: `UNVERIFIED / UNAVAILABLE_FROM_THIS_ENVIRONMENT`
- good ladder-backed timestamp: `UNVERIFIED / UNAVAILABLE_FROM_THIS_ENVIRONMENT`
- bad ladder-missing-or-filtered-to-zero timestamp: `UNVERIFIED / UNAVAILABLE_FROM_THIS_ENVIRONMENT`

Reason:
- `curl -sS -m 20 https://kalshi-metar-monitor.onrender.com/...` failed with `CONNECT tunnel failed, response 403`
- `requests.get("https://kalshi-metar-monitor.onrender.com/...")` failed with the same proxy-tunnel `403 Forbidden`
- no committed local artifact in this repository contains the required same-station same-window production payload pair for 2026-03-22.

## Field-Level Delta Matrix To Use Once the Same-Station Pair Is Reachable

### Comparison A — good ladder-backed alert vs bad filtered-to-zero evaluation
| Surface | Good evaluation | Bad evaluation | Meaning |
|---|---|---|---|
| `market-coverage` | `eligible_market_count_after_filters > 0` | `eligible_market_count_after_filters = 0` | ladder exists, but eligible rung selection collapsed to zero |
| `market-coverage` | `coverage_reason = eligible_but_runtime_transition_terminal_rate_limit_gates_apply` or `webhook_missing` / other non-zero-eligibility reason | `coverage_reason = no_eligible_markets_after_filters` | ladder presence remained, market eligibility changed |
| `alert-decision-trace` | `MARKET_MATCH = PASS` | `MARKET_MATCH = BLOCK` with reason `no_eligible_market_after_filters` | blocking stage changed at market match |
| `alert-decision-trace` | `terminal_state = ALERTABLE` | `terminal_state = BLOCKED_NO_MARKET_MATCH` | emitted vs blocked-before-emission |
| `alert-path-truth` | `blocking_stage = null` | `blocking_stage = market_match` | send-alert path reached vs stopped at market match |
| `alert-path-truth` | `suppression_or_non_emission_reason = null` | `suppression_or_non_emission_reason = NO_ELIGIBLE_MARKET` | explicit no-market outcome recorded |
| `alert-path-truth` | `webhook_send_attempted = true`, `webhook_send_succeeded = true` | both false | no delivery attempt because no market survived |
| `runtime-authority-snapshot` | supporting `latest_alerts.rows` contains a real alert row near the transition | no same-window alert row for the bad evaluation | proves emitted vs non-emitted outcome |

**Interpretation if the same station shows this exact delta:** **candidate-selection drift confirmed**.

### Comparison B — good ladder-backed alert vs bad ladder-missing / cache-invalid evaluation
| Surface | Good evaluation | Bad evaluation | Meaning |
|---|---|---|---|
| `market-coverage` | `discovered_series_ticker` present | `discovered_series_ticker = null` or cache becomes stale/missing | discovery or hydration prerequisite changed |
| `market-coverage` | `series_market_cache_status = cache_valid` | `cache_missing` or `cache_stale` | ladder cache health changed |
| `market-coverage` | positive or otherwise usable coverage row | `coverage_reason = no_discovered_series | cache_not_yet_populated | cache_stale` | failure happened before market selection |
| `runtime-authority-snapshot` | `hydration_snapshot.stations.<station>.cache_present = true` and usable hydration prerequisite fields | cache/hydration prerequisite fields degrade false | runtime authority shows hydration/cache drift |
| `alert-decision-trace` | `INGESTION_ADMISSION = PASS` | `INGESTION_ADMISSION = BLOCK` with `station_not_admitted:<skip_reason>` | path blocked before downstream evaluation |
| `alert-decision-trace` | `terminal_state = ALERTABLE` | `terminal_state = BLOCKED_INGESTION_ADMISSION` | emitted vs blocked-at-admission |
| `alert-path-truth` | `send_alert_entered = true` | `send_alert_entered = false` | send-alert path never entered on the bad cycle |
| `alert-preview` / `alert-fire-audit` | real alert row | `ladder_missing` row | explicit operator-visible symptom split |

**Interpretation if the same station shows this exact delta:** **hydration/cache drift confirmed**.

## Why I Am Not Claiming One Of The Stronger Verdicts Yet
I do **not** have one verified same-station pair proving either Comparison A or Comparison B from live production. The repository documents the right fields and the current code defines the exact meaning of those fields, but this environment does not have direct shell reachability to the production deployment, and the repository does not contain a committed evidence artifact with the required station+timestamp pair.

Therefore I cannot honestly claim:
- `candidate-selection drift confirmed`,
- `hydration/cache drift confirmed`, or
- `alert-path blocking stage changed`  
from verified same-station live evidence.

## Conclusion
**evidence still insufficient**

The repository reality is consistent with **two distinct bad-path shapes**:
1. a **candidate-selection / filtered-to-zero** path where ladder discovery and cache remain present but eligibility falls to zero, and
2. a **hydration/cache / ladder-missing** path where admission is blocked before downstream evaluation.  
Without one verified same-station good timestamp and one verified same-station bad timestamp from production, this diagnosis cannot truthfully collapse those two possibilities into a single confirmed verdict.

## Single Recommended Next Task
Capture **one same-station production evidence pair** through a network path that can actually reach `https://kalshi-metar-monitor.onrender.com`, using exactly these primary surfaces for the same station within one short window:
- `/observability/runtime-authority-snapshot?station=<ICAO>`
- `/observability/market-coverage?station=<ICAO>`
- `/observability/alert-decision-trace?station=<ICAO>`
- `/observability/alert-path-truth?station=<ICAO>`
- `/observability/alert-preview?station=<ICAO>&limit=10`  
Then classify the station using the delta matrix above and only then open the alert-fix implementation task.
