# Canonical Operational Architecture

## System Purpose

Kalshi METAR Monitor is a deterministic observation system.

Its purpose is deterministic execution visibility and deterministic transition emission under immutable Phase 1 behavioral semantics.

This architecture document defines structural authority boundaries and deterministic containment only.

## Deterministic Authority Flow

External Reality
→ Execution Domain
→ Transition History
→ Replay Domain
→ Observability Domain
→ Scoring Domain

## Domain Model

### Execution Domain

- Produces deterministic state progression from canonical observations under Phase 1 semantics.
- Holds transition authority.
- Transition authority transfers atomically at the moment of emission creation inside the authoritative evaluation cycle.
- No intermediate mutable transition state may exist outside that cycle.

### Historical Domain

- Stores canonical historical observations and committed transition history.
- Historical observations are replay reconstruction authority.
- Committed transition history is immutable.

### Replay Domain

- Reconstructs deterministic system state from historically valid deterministic system state produced under Phase 1 semantics.
- External initialization values are prohibited.
- Stored transition history may be used for validation comparison.
- Stored transition history is not required for replay reconstruction authority.

### Observability Domain

- Provides deterministic execution visibility and audit integrity.
- Is strictly epistemic.
- Must not participate in execution causality.
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
- Stored transition history may validate replay but is not replay reconstruction authority.

## Observability Constraints

- Observability is strictly epistemic.
- Observability must not participate in execution causality.
- Observability may expose deterministic artifacts from downstream deterministic derivation layers within architectural constraints.
- Observability shall not assume or define signal-layer authority.

## Scoring Containment

- Scoring is strictly post-transitional.
- Scoring possesses zero execution authority.
- Scoring classification derives exclusively from emitted transition history.
- Scoring normalization must not depend on wall-clock timestamps or execution duration.

## Security Boundaries

- Security protects deterministic legitimacy, authority protection, and boundary integrity.
- Security does not participate in execution behavior.
- Execution is the sole transition-authoritative domain.
- Historical observations are replay reconstruction authority.
- Transition history can validate replay but cannot replace canonical historical observation authority for reconstruction.

## Contributor Guardrails

- Preserve Phase 1 behavioral semantics as immutable baseline.
- Do not alter transition authority placement; transition authority remains in Execution.
- Do not introduce execution causality into Replay, Observability, Scoring, or Security domains.
- Do not use external initialization values for Replay.
- Do not treat transition history as replay reconstruction authority.
- Keep scoring normalization independent of wall-clock timestamps and execution duration.
- Any proposed Phase 1 behavioral modification requires explicit versioning and explicit documentation updates.
