# Canonical Operational Architecture

## System Purpose

Kalshi METAR Monitor is a deterministic live-trading reliability system.

Its production purpose is to:
- derive active station authority from currently tradable Kalshi weather markets,
- ingest METAR observations for those market-authoritative stations,
- emit deterministic transition-driven alerts for `HIGH` and `LOW` ladders,
- preserve exact replay equivalence with live execution,
- detect Goldilocks structural events that can create temporary trader-awareness asymmetry.

Goldilocks structural-event detection is part of live trading reliability, not optional analytics.

## Deterministic Authority Flow

Market Availability (Kalshi listings)
→ Station Authority Set
→ Execution Domain
→ Transition History
→ Replay Domain
→ Observability Domain
→ Scoring Domain

## Canonical Control Surface

HTTP endpoint definitions are canonicalized in `docs/API_REFERENCE.md`.

That reference is authoritative for endpoint domain assignment, execution authority, safety boundaries, and data-source mapping.

## Domain Model

### Market Authority Domain

- Markets are the source of truth for active stations.
- Station configuration can scope or filter monitoring, but cannot override market authority.
- METAR ingestion follows market availability so ingestion and settlement interpretation stay aligned.

### Execution Domain

- Produces deterministic state progression from canonical observations under frozen Phase 1 semantics.
- Settlement bucket progression is monotonic for each station-day.
- Alerts are transition-driven and emitted from authoritative execution transitions only.
- Holds transition authority.
- Transition authority transfers atomically at emission creation inside the authoritative evaluation cycle.
- No intermediate mutable transition state may exist outside that cycle.

### Historical Domain

- Stores canonical historical observations and committed transition history.
- Historical observations are replay reconstruction authority.
- Committed transition history is immutable.

### Replay Domain

- Reconstructs deterministic system state from historically valid deterministic system state produced under Phase 1 semantics.
- Must reproduce live transitions exactly (station, direction, settlement bucket, alert outcomes).
- External initialization values are prohibited.
- Stored transition history may be used for validation comparison.
- Stored transition history is not replay reconstruction authority.

### Observability Domain

- Provides deterministic execution visibility and audit integrity.
- Is strictly epistemic.
- Must not participate in execution causality.
- Must not initiate live Kalshi API calls.
- Operates from persisted runtime state and cached market data snapshots.
- Cache hydration is owned by execution/ingestion pathways, not observability endpoints.
- May expose deterministic artifacts produced by downstream deterministic derivation layers operating within architectural constraints.
- Shall not assume or define signal-layer authority.

### Scoring Domain

- Defines deterministic scoring classification derived exclusively from emitted transition history.
- Is strictly post-transitional.
- Possesses zero execution authority.
- Time normalization is derived exclusively from deterministic transition ordering or epoch-relative positional metrics.
- Must not depend on wall-clock timestamps or execution duration.

### Security Domain

- Protects deterministic legitimacy, authority protection, and boundary integrity.
- Does not participate in execution behavior.
- Replay may validate against committed transition history.
- Replay reconstruction authority derives exclusively from canonical historical observations governed under Phase 1 semantics.

## Replay Guarantees

- Replay execution remains behaviorally identical to production execution under Phase 1 semantics and deterministic architecture constraints.
- Replay initialization state is derived exclusively from historically valid deterministic system state.
- External initialization values are prohibited.
- Replay acceptance requires exact transition parity with live history, not approximate similarity.
- Stored transition history may validate replay but is not replay reconstruction authority.

## Observability Constraints

- Observability is strictly epistemic.
- Observability must not participate in execution causality.
- Observability endpoints must not call Kalshi.
- Observability reads persisted runtime state plus cached market data only.
- Cache hydration expectations must be explicit in operations docs and runbooks.
- Observability may expose deterministic artifacts from downstream deterministic derivation layers within architectural constraints.
- Observability shall not assume or define signal-layer authority.

## Governance Invariants

DO NOT:
- introduce ML execution
- introduce probabilistic execution paths
- smooth temperature transitions
- suppress rapid reversions
- reinterpret Goldilocks events statistically
- shift execution authority outside Execution domain

## Contributor Guardrails

- Preserve Phase 1 behavioral semantics as immutable baseline.
- Preserve market-derived station authority.
- Preserve symmetric `LOW` + `HIGH` monitoring behavior.
- Keep alerts transition-driven only.
- Do not introduce execution causality into Replay, Observability, Scoring, or Security domains.
- Do not use external initialization values for Replay.
- Do not treat transition history as replay reconstruction authority.
- Keep scoring normalization independent of wall-clock timestamps and execution duration.
- Any proposed Phase 1 behavioral modification requires explicit versioning and explicit documentation updates.
