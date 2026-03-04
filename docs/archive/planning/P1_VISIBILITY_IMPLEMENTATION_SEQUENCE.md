# P1 Visibility Implementation Sequence (Phase 3 Planning)

## Purpose

This plan defines a deterministic, dependency-safe rollout order for implementing accepted visibility specifications while preserving execution-domain invariants:

- transition emission unchanged,
- ingestion acceptance unchanged,
- scheduler cadence unchanged,
- replay/live equivalence unchanged.

Planning scope only: no execution-path redesign, no visibility-domain merging, no behavior changes.

## Input Specs Covered

- `docs/VISIBILITY_GAP_CLOSURE_SPEC.md`
- `docs/INGESTION_VISIBILITY_SPEC.md`
- `docs/HYDRATION_VISIBILITY_SPEC.md`
- `docs/SCHEDULER_VISIBILITY_SPEC.md`
- `docs/REPLAY_PARITY_VISIBILITY_SPEC.md`

## Execution-Proximity Classification

| Spec | Primary Proximity Class | Why |
|---|---|---|
| `VISIBILITY_GAP_CLOSURE_SPEC` | transition-adjacent | Hooks at transition evaluation non-emission boundary and reason-path capture. |
| `INGESTION_VISIBILITY_SPEC` | ingestion-adjacent | Hooks at ingestion reject/skip finalization boundary and rejection taxonomy mapping. |
| `HYDRATION_VISIBILITY_SPEC` | cache/hydration-adjacent | Tracks hydration outcome per station/poll cycle, independent of transition/acceptance authority. |
| `SCHEDULER_VISIBILITY_SPEC` | scheduler-adjacent | Derived from loop/cadence signals and exposed as health classification state. |
| `REPLAY_PARITY_VISIBILITY_SPEC` | replay/post-execution | Consumes persisted live/replay artifacts for read-only parity judgments. |

## Dependency-Safe Global Order

Deterministic implementation order across all specs:

1. **Scheduler visibility primitives** (`SCHEDULER_VISIBILITY_SPEC`)
2. **Hydration visibility primitives** (`HYDRATION_VISIBILITY_SPEC`)
3. **Ingestion rejection visibility primitives** (`INGESTION_VISIBILITY_SPEC`)
4. **Transition non-emission visibility primitives** (`VISIBILITY_GAP_CLOSURE_SPEC`)
5. **Replay parity visibility primitives** (`REPLAY_PARITY_VISIBILITY_SPEC`)

Rationale for this order:

- Start with most read-derived, low execution-coupling domains (scheduler/hydration).
- Introduce ingestion/transition-adjacent capture only after observational contracts are stabilized.
- Build replay parity last, because it depends on stable persisted artifacts and identity discipline from earlier domains.

## Rollout Phases

### Phase A — Zero-risk observational hooks

#### Implementation scope

- Add non-causal, post-decision hook points only (no persistence writes yet).
- Emit deterministic in-memory/ephemeral envelopes for:
  - scheduler classification snapshot events,
  - hydration outcome envelopes,
  - ingestion rejection envelopes,
  - transition non-emission envelopes,
  - parity comparison envelope skeleton (without storage).
- Validate deterministic field derivation and taxonomy mapping with fixed fixtures.

#### Why safe at this stage

- No durable write-path changes.
- No API/schema exposure yet.
- Hook execution is side-channel only and can be no-op disabled.

#### Failure containment boundary

- Boundary: hook-adapter layer only.
- Any failure degrades to "no visibility artifact emitted" while preserving existing control flow and timing.
- No downstream consumer depends on Phase A outputs.

#### Replay-risk analysis

- Replay risk is minimal because there is no persisted cross-run state.
- Guard against nondeterministic fields (wall-clock/random ordering keys) before advancing.

---

### Phase B — Persistence introduction

#### Implementation scope

- Introduce append-only persistence for each domain with deterministic identity keys and idempotent writes.
- Enforce domain-specific uniqueness/dedup constraints:
  - scheduler transition identity,
  - hydration `(station_id, poll_cycle_id, hydration_evaluation_context_key)`,
  - ingestion rejection canonical tuple,
  - transition non-emission canonical tuple,
  - parity comparison identity tuple.
- Add failure-isolation behavior: persistence failures must not block execution loops.

#### Why safe at this stage

- Persistence added after hook payload stability is proven in Phase A.
- Write paths remain downstream of finalized execution decisions.
- Idempotency controls prevent restart/reprocess duplication drift.

#### Failure containment boundary

- Boundary: persistence adapters and queue/write workers.
- On failure: log observability-path error, drop/defer visibility write only.
- Execution authorities (ingestion, transition, scheduler, replay engines) remain independent.

#### Replay-risk analysis

- Primary risk: identity-key mistakes causing duplicate/missing records and false mismatch signals.
- Mitigation: deterministic identity conformance tests + replay rerun idempotency tests before enabling full write throughput.

---

### Phase C — Surface exposure

#### Implementation scope

- Expose read-only operator surfaces using persisted artifacts:
  - scheduler health canonical surface + mirrored summaries,
  - hydration timeline/snapshot/poll-cycle matrix,
  - ingestion rejection timeline + ingestion-health additive counters,
  - transition non-emission timeline + station summary additive fields,
  - parity summary/detail/timeline/attention surfaces.
- Enforce deterministic ordering, bounded pagination, and explicit taxonomy tokens.

#### Why safe at this stage

- APIs are read-only projections over immutable records.
- No new write-back/control endpoints are introduced.
- Uses already-validated storage contracts from Phase B.

#### Failure containment boundary

- Boundary: query/read models and API serialization.
- Read-surface outage only impacts operator visibility, not execution cadence or outcomes.

#### Replay-risk analysis

- Risk limited to presentation errors (ordering/filter correctness), not execution mutation.
- Must verify live/replay mode partitioning to prevent accidental blending in operator views.

---

### Phase D — Cross-surface parity linkage

#### Implementation scope

- Link surfaces with deterministic references only:
  - parity records reference live transition/replay artifacts,
  - station/day drill-down links from scheduler/hydration/ingestion/transition surfaces,
  - consistency checks for latest status propagation (without inferred state overrides).
- Add cross-surface "stale/latency-state" indicators where specs require them.

#### Why safe at this stage

- Occurs after each domain is independently stable.
- Linkage is read-model composition only; no control-plane integration.
- Avoids early coupling that could blur failure origin.

#### Failure containment boundary

- Boundary: aggregation/link resolver layer.
- If linkage fails, each canonical domain surface remains independently queryable.
- No fallback may synthesize execution-affecting states.

#### Replay-risk analysis

- Highest in this phase for semantic drift (incorrect joins causing false parity narratives).
- Mitigate via deterministic join keys, explicit live/replay scoping, and first-divergence reproducibility checks.

## Highest Determinism Risk Step

❗ **Highest determinism risk step:** Phase B introduction of canonical identity + dedup logic for ingestion rejection and transition non-emission persistence.

Why this is highest risk:

- These domains are closest to hot execution paths.
- Incorrect identity anchoring can silently create duplicates/omissions.
- Downstream parity and operator conclusions depend on these records being exactly-once logical artifacts under reprocessing/replay.

## Required Guardrails Before Proceeding (Gate to Phase B and beyond)

1. **Identity conformance suite (blocking):** prove all identity tuples are derived only from deterministic replayable inputs.
2. **Non-causality assertions (blocking):** verify no visibility component is read by ingestion acceptance, transition emission, scheduler control, or replay execution branches.
3. **Failure-injection tests (blocking):** simulate persistence unavailability; assert zero change in execution outputs/cadence.
4. **Replay reproducibility checks (blocking):** repeated replay with identical inputs yields identical logical visibility identities and reason-code sequences.
5. **Ordering invariance checks (blocking):** enforce stable per-surface ordering and tie-break rules.
6. **Live/replay isolation checks (blocking):** replay-tagged artifacts cannot overwrite or satisfy live-operational readiness/health states.
7. **Latency-state correctness checks (non-blocking for execution, blocking for visibility release):** verify bound tokens (`WITHIN_BOUND`/`BOUND_EXCEEDED`, stale markers) are informational only.

## Safe Incremental Delivery Checklist

- Complete each phase for all five domains before enabling next phase globally.
- Use canary enablement by read-surface audience, never by execution-path branching.
- Preserve additive-only schema evolution on existing surfaces.
- Require explicit sign-off statement at each phase gate:
  - "Deterministic execution semantics preserved."
