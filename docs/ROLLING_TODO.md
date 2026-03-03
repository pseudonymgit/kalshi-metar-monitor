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
