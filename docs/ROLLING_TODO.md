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

## Priority Stack

### P0 — LIVE TRADING RELIABILITY (ACTIVE)

Purpose: system must reliably inform immediate trading decisions.

- [ ] Audit missing alert emissions across stations
- [ ] Verify ingestion parity across all active cities
- [ ] Detect stalled station ingestion automatically
- [ ] Add ingestion health visibility endpoint
- [ ] Confirm integer-cross detection consistency
- [ ] Validate scheduler execution per station
- [ ] Detect silent execution-domain guard rejection
- [ ] Add transition emission verification logging

### P0 — EPOCH BACKFILL BOOTSTRAP

Purpose: increase usable historical learning immediately.

- [ ] Historical METAR acquisition utility
- [ ] Deterministic replay ingestion runner
- [ ] Backfill >=90 historical days per city
- [ ] Accept reduced-resolution snapshots when necessary
- [ ] Persist reconstructed settlement epochs
- [ ] Record backfill provenance metadata
- [ ] Validate replay equivalence after backfill

Rule: a partial historical day is preferable to missing epoch history.

### P1 — MARKET EXPANSION (MERGED WORKSTREAM)

LOW Market Enablement + City Optimization

- [ ] Enable LOW markets across supported cities
- [ ] Replace static ICAO→Kalshi mapping with formulaic resolver
- [ ] Auto-detect newly listed Kalshi temperature markets
- [ ] Emit alert when unknown market detected
- [ ] Optimize city selection for diversified weather regimes
- [ ] Expand ingestion simultaneously with LOW activation

Goal: accelerate epoch density growth without increasing architectural complexity.

### P1 — ENVIRONMENTAL SIGNAL LOGGING

Logging only. No prediction logic introduced.

Add deterministic logging for:
- [ ] dewpoint
- [ ] pressure
- [ ] wind speed
- [ ] cloud coverage
- [ ] precipitation presence

- [ ] Store alongside settlement epochs

Purpose: future structural correlation analysis.

### P2 — SEARCHABLE EPOCH STORE

- [ ] Transition hashing
- [ ] Epoch indexing
- [ ] Fast deterministic lookup
- [ ] Replay validation fingerprints
- [ ] Queryable epoch search interface

Constraint: no execution authority impact.

### P3 — DECISION SUPPORT VISIBILITY (HUMAN-IN-THE-LOOP)

- [ ] High-probability / high-payout condition highlighting
- [ ] Market vs structural probability comparison views
- [ ] Actionable report generation
- [ ] Manual trader decision support only

Constraint: no automated execution.

## Historical Continuity (Preserved Intent)

Carry-forward items from prior backlog organization remain represented in the priorities above:
- Integer bucket crossing alert behavior and reduced-noise transition-only emission intent
- Station-local gating, per-station reset behavior, and station time visibility/debugging intent
- Kalshi read-only market monitoring and watchlist expansion intent

No completed historical items were removed.

## Governance Constraints

DO NOT:
- modify execution logic
- introduce ML behavior
- introduce probabilistic execution paths
- alter Phase-1 semantics
- grant execution authority outside Execution domain
