# ROLLING DEVELOPMENT TODO

Canonical rolling execution plan for immediate trading reliability and deterministic learning expansion.

## Governing Principle

Primary priority: maintain a system that reliably informs immediate trading decisions while expanding deterministic learning capability.

All expansion must directly improve one or more of:
- alert reliability
- epoch generation
- replay integrity
- actionable decision visibility

Avoid scope creep.

## Priority Stack (Reconciled)

### P0 — PRODUCTION RELIABILITY + DISCOVERY INTEGRITY (ACTIVE)

Purpose: preserve alert-path determinism while closing station/market visibility gaps.

- [ ] **Market discovery mismatch investigation** (new)
- [ ] Audit missing alert emissions across stations
- [ ] Verify ingestion parity across all active cities
- [ ] Detect stalled station ingestion automatically
- [x] Add ingestion health visibility endpoint *(completed: `/observability/ingestion-health` present)*
- [ ] Confirm integer-cross detection consistency
- [ ] Validate scheduler execution per station
- [x] Detect silent execution-domain guard rejection *(completed: execution-domain observability + guardrails present)*
- [~] Add transition emission verification logging *(partially complete via transition/runtime observability; needs explicit acceptance criteria closure)*
- [ ] **Milestone tagging requirement** (new)


### P0 — REGRESSION ALERT GOVERNANCE

Purpose: detect invariant violations and structural drift independent of trading alerts.

Tasks:
- [ ] Define regression-class taxonomy (hydration, discovery, eligibility, scheduler, execution-domain)
- [ ] Define invariant violation triggers vs informational telemetry
- [ ] Separate regression notifications from trading alerts
- [ ] Add replay-based regression verification hook
- [ ] Define suppression transparency for regression-class events
- [ ] Production runtime discovery snapshot validation

### P0 — ALERT GOVERNANCE HARDENING (NEW)

Purpose: freeze deterministic alert contracts before broader expansion.

- [ ] **Alert Schema v1.0 design freeze** (new)
- [ ] **Event log canonicalization format** (new)
- [ ] **Schema versioning policy** (new)
- [ ] **Alert distribution topology decision (Discord per-city vs unified)** (new)

### P1 — STRUCTURAL EDGE PRESERVATION

Purpose: preserve deterministic execution edge from structural-event awareness.

- [ ] Goldilocks alert surfacing
- [ ] Replay verification for Goldilocks transitions
- [ ] Detection auditability for advance-and-revert sequences
- [ ] Station-day visibility for structural events
- [ ] Missed-event detection with deterministic root-cause traceability

### P1 — EPOCH BACKFILL BOOTSTRAP

Purpose: increase usable historical learning immediately.

- [ ] Historical METAR acquisition utility
- [ ] Deterministic replay ingestion runner
- [ ] Backfill >=90 historical days per city
- [ ] Accept reduced-resolution snapshots when necessary
- [ ] Persist reconstructed settlement epochs
- [ ] Record backfill provenance metadata
- [ ] Validate replay equivalence after backfill

Rule: a partial historical day is preferable to missing epoch history.

### P2 — EXECUTION/OBSERVABILITY DOMAIN SEPARATION MAINTENANCE

Purpose: ensure read-only observability boundaries remain explicit and testable.

- [x] Confirm observability endpoints do not perform live Kalshi calls *(completed by domain guards and endpoint tests)*
- [~] Hydration state surfacing consistency across endpoints *(partially complete; continue reconciliation of endpoint-level semantics)*
- [ ] Runtime authority snapshot adoption as primary operator diagnosis surface

### Infrastructure Reliability

#### Deterministic Hydration Queue

Status: Hardened in production path (poll + alert queue-only execution).

Purpose:
- Prevent API rate-limit storms and ensure ladder cache stability.
- Route all hydration triggers through deterministic queue scheduling.
- Preserve replay and signal equivalence while decoupling network timing from poll execution.

Completed hardening:
- Queue worker is the sole live hydration execution path for poll/alert/simulate flows.
- Poll and alert paths only enqueue hydration work (no inline ladder hydration calls).
- Alert/send market evaluation now uses cache-only ladder reads; cache misses trigger enqueue-only refresh.
- 429 response handling is centralized to queue backoff + deterministic retry eligibility.
- Runtime authority and hydration prerequisite observability now expose queue depth, queued stations, and active backoff stations.

Future improvements:
- Global hydration metrics
- ladder_cache_age telemetry
- hydration health monitoring

### P2 — MARKET EXPANSION (MERGED WORKSTREAM)

LOW + HIGH Symmetric Market Enablement + City Optimization

- [~] Enable LOW markets across supported cities *(stale priority: runtime supports LOW; deployment/config parity remains)*
- [x] Preserve HIGH/LOW symmetric transition monitoring guarantees *(completed structurally; keep as regression guard)*
- [ ] Replace static ICAO→Kalshi mapping with formulaic resolver
- [~] Auto-detect newly listed Kalshi temperature markets *(partially complete via series discovery; validate failure modes)*
- [ ] Emit alert when unknown market detected
- [ ] Optimize city selection for diversified weather regimes
- [ ] Expand ingestion simultaneously with LOW activation

Goal: accelerate epoch density growth without increasing architectural complexity.

### P3 — ENVIRONMENTAL SIGNAL LOGGING

Logging only. No prediction logic introduced.

Add deterministic logging for:
- [ ] dewpoint
- [ ] pressure
- [ ] wind speed
- [ ] cloud coverage
- [ ] precipitation presence

- [ ] Store alongside settlement epochs

Purpose: future structural correlation analysis.

### P4 — SEARCHABLE EPOCH STORE + DECISION SUPPORT VISIBILITY

- [ ] Transition hashing
- [ ] Epoch indexing
- [ ] Fast deterministic lookup
- [ ] Replay validation fingerprints
- [ ] Queryable epoch search interface
- [ ] High-signal condition highlighting for manual review
- [ ] Market vs structural state comparison views
- [ ] Actionable report generation
- [ ] Manual trader decision support only

Constraint: no execution authority impact.


## Signal Engine Evolution (Deterministic Backbone Preserved)

This section captures the agreed signal-engine architecture while preserving deterministic core behavior.

Non-negotiable constraints:
- Phase 1 ladder semantics are immutable.
- Execution-domain guard remains strict.
- Replay equivalence must not be violated.
- The system does **NOT** perform live trading.

### Implemented

#### Phase 1 — Nearest Strike Only (Directional Purity)

Status: Implemented.

- `build_structured_snapshot()` now filters to only the nearest relevant strike:
  - `HIGH` -> next strike above observed value
  - `LOW` -> next strike below observed value
- Deep ladder rungs are no longer evaluated.
- Eliminates multi-rung spam.

### In progress

#### Phase 2 — Proximity Regime Engine

Status: Regime tracking implemented. Emission logic not yet fully integrated.

- `classify_proximity(distance_f)`:
  - `<= 0.25` -> `CRITICAL`
  - `<= 0.5` -> `NEAR`
  - `<= 0.8` -> `APPROACHING`
  - else -> `FAR`
- `_LAST_PROXIMITY_REGIME` tracked per `(station, direction)`.
- Emit only when regime rank increases:
  - `{"FAR":0,"APPROACHING":1,"NEAR":2,"CRITICAL":3}`

Goal:
- Prevent oscillation spam.
- Escalation-only proximity signals.

### Designed but not implemented

#### Phase 3 — Price Suppression Logic

Status: Designed.

Policy:
- If regime in `{APPROACHING, NEAR, CRITICAL}`
- AND `yes_price >= 90`
- AND `distance > 0.15`
- -> suppress.

Override:
- If `distance <= 0.15` -> never suppress.

Structural signals ignore suppression entirely.

#### Phase 4 — Time Weighting (Soft Suppression)

Status: Designed.

- High window: `11-19` local
- Low window: `3-10` local

Outside window:
- Suppress Tier 2 (proximity)
- Allow Tier 1 (structural)

No hard blocking of ladder events.

#### Phase 5 — Forced Rehydration on Critical Events

Status: Designed.

Trigger recache when:
- `settlement_bucket_changed`
- OR regime == `CRITICAL`

Even if TTL not expired.

Purpose:
- Ensure fresh strikes/prices during decisive events.

Implementation note:
- Trigger paths enqueue deterministic hydration requests instead of issuing direct poll-loop Kalshi calls.

#### Phase 6 — Signal Classification Layer

Status: Scaffold planned.

`signal_class`:
- `STRUCTURAL`
- `PROXIMITY`
- `PREDICTIVE` (not enabled)

Emission rule:
- `STRUCTURAL` -> always emit
- `PROXIMITY` -> emit if not suppressed
- `PREDICTIVE` -> future

#### Phase 7 — Goldilocks Priority

Status: Design confirmed.

Goldilocks (`reversion_after_settlement`):
- Tier 1
- Never suppressed
- Force recache
- Payload includes:
  - `current_temp`
  - `high_of_day`
  - `settlement`
  - `nearest_strike`
  - `YES price`

#### Phase 8 — Alert Payload Upgrade

Status: Planned.

All alerts will include:
- `current_temp`
- `high_of_day`
- `settlement_bucket`
- `nearest_strike`
- `distance_to_strike`
- `yes_price`
- `spread`
- `time_remaining`
- `signal_class`

Goal:
- Alerts become structured state reports, not stimuli.

### Future roadmap

#### Expected Behavior Change

Before:
- Multi-rung spam
- Oscillation noise
- Late-day expansion garbage

After:
- `0-2` proximity alerts per meaningful move
- Rare Tier 1 structural events
- No deep ladder nonsense
- Strike-relevant only
- Informative, not stimulating

#### Strategic roadmap

- Support all Kalshi cities dynamically (remove static station map).
- Dynamic city discovery and market onboarding must enqueue deterministic hydration work for newly activated stations.
- Detect newly listed series automatically.
- Auto-discover new market structures.
- AI interpretation layer (non-authoritative, read-only).
- Unified signal engine (deterministic backbone + signal evaluation layer).
- Maintain strict execution-domain guard integrity.

#### Important architectural note

Hydration gating is policy-level only.
Ingest execution-domain flags must never be used for market-phase suppression.

Hydration queueing is infrastructure-level only.
Signal transitions and replay semantics remain unchanged.

Execution domain symmetry:
- `(True, True)` = production
- `(False, False)` = replay
- Mixed states forbidden.

## Reconciliation Notes

- Completed items were retained and marked instead of removed.
- Obsolete framing consolidated: prior standalone "decision support" section moved into P4 to reduce priority ambiguity.
- Bootstrap scope remains active but deprioritized behind production discovery/alert governance closure.
- Historical intent is preserved; ordering now reflects current operational risk.

## Historical Continuity (Preserved Intent)

Carry-forward items from prior backlog organization remain represented in the priorities above:
- Integer bucket crossing alert behavior and reduced-noise transition-only emission intent
- Station-local gating, per-station reset behavior, and station time visibility/debugging intent
- Kalshi read-only market monitoring and watchlist expansion intent
- Goldilocks structural-event handling as live reliability concern (now explicit priority)

No completed historical items were removed.

## Governance Constraints

DO NOT:
- modify execution logic
- introduce ML behavior
- introduce probabilistic execution paths
- smooth temperature transitions
- suppress rapid reversions
- reinterpret Goldilocks events statistically
- alter Phase-1 semantics
- grant execution authority outside Execution domain
