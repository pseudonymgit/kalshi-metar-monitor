# EXECUTION VISIBILITY STANDARD

This document defines mandatory execution visibility
requirements for production operation of the system.

All future architectural or operational changes MUST
preserve visibility guarantees defined here.

## Execution Visibility Audit

Fully visible (operator-observable via endpoint and/or deterministic audit tables):

- Settlement movement: `settlement_up` transitions are persisted to `transition_events`, exposed in `/observability/transitions`, and reflected in current/day epoch endpoints.
- Goldilocks structural progression: `reversion_occurred` and `first_reversion_timestamp_utc` are exposed by `/observability/current-epochs`, `/observability/day-structure`, and `/observability/station-summary`.
- Missed-alert triage signals: `/observability/alert-diagnostics` and `/observability/alert-fire-audit` provide deterministic blocked-classification and `TRANSITION_WITHOUT_ALERT` integrity checks.
- Ladder hydration gating signals: poll skips log `poll_skipped_reason=ladder_not_hydrated`; ingestion-health/station-summary expose downstream freshness degradation.
- Station ingestion freshness/stall visibility: `/observability/ingestion-health` classifies healthy/stale with lag metrics.
- Scheduler heartbeat visibility: `/metar/status` and observability boundary markers expose `scheduler_running`, `poll_count`, `last_poll_utc`, `last_loop_utc`, and timeout counters.
- Observability boundary controls: request execution-domain routing plus observability read-only guards prevent observability endpoints from becoming causal.

## Silent Failure Risks

Potentially silent or weakly surfaced defects (visibility gap):

1. Transition emission suppression reason is not explicitly surfaced when `emit_transition_if_changed` returns early due to no transition or no effective bucket change; operators see absence, not cause.
2. Goldilocks-specific prioritization is weak: `goldilocks_reversion` is persisted as transition type, but there is no dedicated high-priority health/summary surface signaling “Goldilocks just occurred”.
3. Ladder evaluation failures inside `_send_alert` are caught and printed as generic errors; no deterministic operator endpoint summarizes evaluation exceptions by station/day.
4. Market hydration failures are warning-logged and poll-skipped, but there is no first-class endpoint aggregating hydration status (`cache_missing`, `cache_stale`, `cache_valid`) per station.
5. Scheduler degradation lacks explicit thresholded health state; operators must infer degradation by reading timestamps/counters rather than a deterministic `degraded` status.
6. Cache staleness affecting decisions is only day-key validated; within-day stale cache age is not explicitly surfaced as a risk indicator.
7. Replay-vs-live divergence is not continuously surfaced: replay endpoint runs deterministically, but there is no operator-facing parity dashboard/flag indicating mismatch against committed transition history.

## Goldilocks Visibility Assessment

- Detectable: **Partially yes**. Goldilocks transitions are emitted (`goldilocks_reversion`) and reversion fields are available on epoch endpoints.
- Distinguishable: **Moderate**. Distinguishable in transition/event detail, but not elevated in top-level operational health summaries.
- Actionable: **Moderate**. Operators can query reversion metrics, but no dedicated “recent Goldilocks events” summary/feed exists for fast response.
- Prioritized appropriately: **No**. Goldilocks is architecturally critical, but operational surfacing is not prioritized above generic transition visibility.

Conclusion: If operator workflows do not continuously inspect transition/day-structure endpoints, Goldilocks events can be missed in practice.

## Observability Boundary Validation

Validated: observability remains non-causal.

- Observability endpoints are explicitly defined as read-only and Kalshi-call-prohibited.
- Execution domain tokenization routes `/observability/*` away from production execution domain.
- Security boundary checks enforce read-only snapshot constraints and cross-layer import restrictions.

No execution-causality leakage was identified from observability into transition/alert decisions.

## Minimal Deterministic Improvements

Visibility-only additions (no execution changes):

1. Add `/observability/hydration-health` returning per-station ladder hydration status/reason (`cache_valid/cache_missing/cache_stale`, hydrated timestamp, station-local day).
2. Add `/observability/goldilocks-feed` returning recent `goldilocks_reversion` transitions with station, timestamps, settlement bucket, and delta-seconds metadata.
3. Add deterministic counters in `/metar/status` for consecutive poll loops with zero ingestions and zero hydrated stations.
4. Add explicit `transition_suppressed_reason` logging/audit row when emission is skipped for `NO_TRANSITION_TYPE` or `NO_EFFECTIVE_CHANGE`.
5. Add `/observability/replay-parity` summary endpoint comparing latest replay run (station/day) against committed transition IDs/count parity.
6. Add scheduler health classification field (`healthy/degraded/stalled`) derived deterministically from `last_loop_utc` lag vs poll interval.

## Enforcement Rule

Any change that introduces a new execution-domain event
or failure mode MUST ensure operator visibility through:

- observability endpoint
- deterministic audit record
- or explicit health classification

Silent execution-domain behavior is prohibited.

## Operational Confidence Assessment

**Readiness: MODERATE**

Deterministic basis:

- Core execution events are persisted and queryable.
- Ingestion and scheduler heartbeat telemetry exists.
- Alert diagnostics provide deterministic block reasoning.
- But multiple critical failure classes still require inference from raw logs/timestamps.
- Goldilocks events are detectable but insufficiently prioritized for operator immediacy.

Per critical rule: because a Goldilocks event could be missed without active deep endpoint monitoring, the system is **not production-stable** at current visibility posture.

This document is normative.
Failure to maintain these visibility guarantees
invalidates production-stable classification.
