# Doctrine Cross-Reference Audit — Consistency Review

Date: 2026-03-04  
Scope: Doctrine and integrity-review corpus consistency audit

## Reviewed Corpus

Primary targets from task scope:
- `docs/DOCUMENTATION_INDEX.md`
- `docs/EXECUTION_VISIBILITY_STANDARD.md`
- `docs/REVIEW_RUNTIME_INTEGRITY_2026-03-04.md`
- `docs/REVIEW_CAUSAL_CHAIN_INTEGRITY_2026-03-04.md`

Additional architecture/doctrine files referenced by `DOCUMENTATION_INDEX.md` and included in this audit:
- `docs/ARCHITECTURE.md`
- `docs/OPERATING_MODE.md`
- `docs/VISIBILITY_HOOK_CONTRACT.md`
- `docs/API_REFERENCE.md` (authoritative endpoint boundary mapping)
- `docs/REPLAY_PARITY_VISIBILITY_SPEC.md` (replay visibility doctrine)

---

## Executive Verdict

Doctrine alignment is **mostly strong but not fully consistent** across the corpus.

- Core architecture documents consistently preserve deterministic boundaries.
- Integrity-review documents correctly identify real runtime caveats, but several caveats are not reflected as explicit doctrine exceptions in the normative docs.
- The largest cross-document inconsistency is between the normative claim that observability endpoints are read-only/non-causal and the runtime-integrity finding that HTTP-layer startup hooks can mutate runtime state on first observability request.
- Replay semantics are directionally aligned (deterministic parity objective), but wording diverges between “exact live equivalence” and “replay delivery/evaluation branches intentionally disabled.”

---

## Consistency Check 1 — Transition Emission Authority

### Requirement
All docs should describe `transition_emitter` as single transition authority.

### Findings

**Consistent in review docs and runtime evidence framing:**
- Causal-chain review explicitly states `core.transition_emitter.emit_transition_if_changed(...)` is the single authority with boundary enforcement.  
- Runtime integrity review states `core/transition_emitter.py` centralizes transition emission authority.

**Doctrine wording gap (not contradiction, but missing explicitness):**
- `ARCHITECTURE.md` states that the Execution Domain “holds transition authority,” but does not explicitly name `transition_emitter` as the concrete module-level authority.

### Assessment
**Status: PARTIAL CONSISTENCY**  
No direct contradiction detected; explicit module-level authority is missing in doctrine-level architecture text.

### Recommended small edits
1. In `ARCHITECTURE.md` Execution Domain section, add a one-line normative anchor: “Concrete transition authority is implemented via `core.transition_emitter.emit_transition_if_changed(...)`.”
2. In `EXECUTION_VISIBILITY_STANDARD.md`, add a short “authority anchor” sentence referencing `transition_emitter` so visibility doctrine and causal-chain review use identical authority language.

---

## Consistency Check 2 — Deterministic Event Flow

### Requirement
All docs should align on:

`observation → transition → evaluation → suppression → alert`

### Findings

**Strong alignment in causal review:**
- Causal-chain review uses equivalent path wording: `METAR ingestion → ladder transition emission → market evaluation → suppression logic → alert emission`.

**Partial alignment in architecture doctrine:**
- `ARCHITECTURE.md` describes transition-driven alerts and deterministic domain sequencing, but does not present the full five-stage chain as a single canonical sentence.

**Material caveat documented in reviews:**
- Causal-chain review records a manual bypass (`/kalshi/composed`) outside transition correlation/evaluation annotation flow.
- Causal-chain review also records throttle early-return paths that can skip evaluation annotation lineage.

### Assessment
**Status: PARTIAL CONSISTENCY WITH EXPLICIT EXCEPTIONS**  
Core ingestion path is described consistently, but doctrine docs under-specify documented bypass/short-circuit exceptions.

### Recommended small edits
1. Add one canonical causal-chain line (exact 5-step sequence) to `ARCHITECTURE.md` and `EXECUTION_VISIBILITY_STANDARD.md`.
2. Add a compact “known non-canonical paths” note in doctrine docs referencing:
   - manual `/kalshi/composed` endpoint (diagnostic/manual),
   - throttle short-circuit lineage gap.

---

## Consistency Check 3 — Replay Boundaries

### Requirement
Replay scope limitations should be consistently documented: no alert delivery, no runtime mutation.

### Findings

**Consistent evidence in integrity docs:**
- Runtime integrity review cites replay path with `allow_alert_delivery=False, persist_cache=False`.
- Causal-chain review explicitly confirms replay does not execute alert/evaluation delivery branch when alert delivery is disabled.

**Doctrinal wording tension (soft contradiction):**
- `ARCHITECTURE.md` says replay must reproduce live transitions exactly and includes “alert outcomes” in equivalence language.
- Integrity reviews explicitly describe replay branch limitation that excludes alert-delivery behavior and may diverge in evaluation lineage/timestamps.

### Assessment
**Status: MINOR DOCTRINAL DIVERGENCE**  
Replay doctrine and review findings are directionally compatible but use wording that can be read as contradictory without clarification.

### Recommended small edits
1. In `ARCHITECTURE.md`, clarify that “exact transition parity” is mandatory while alert delivery side effects are intentionally out-of-scope for replay execution.
2. In `REPLAY_PARITY_VISIBILITY_SPEC.md`, add a brief cross-reference sentence tying replay parity object to transition/epoch artifacts rather than live outbound delivery behavior.

---

## Consistency Check 4 — Observability Safety Guarantees

### Requirement
Observability endpoints must be consistently described as read-only.

### Findings

**Normative docs are consistent:**
- `ARCHITECTURE.md`, `API_REFERENCE.md`, and `EXECUTION_VISIBILITY_STANDARD.md` consistently assert observability non-causality and no live Kalshi calls.
- `VISIBILITY_HOOK_CONTRACT.md` also enforces one-way `execution -> visibility` and forbids visibility mutation/control-flow influence.

**Integrity review contradiction against end-to-end behavior:**
- Runtime integrity review reports first-hit observability requests can trigger mutable startup/autostart behaviors at app layer before domain scoping, breaking strict end-to-end read-only guarantee.

### Assessment
**Status: DIRECT CONTRADICTION (DOC CLAIM VS REVIEWED RUNTIME BEHAVIOR)**

### Recommended small edits
1. Add an explicit doctrine caveat note: observability route handlers are read-only by contract, but app bootstrap/first-hit lifecycle must also be constrained to preserve end-to-end purity.
2. Add a pointer in `EXECUTION_VISIBILITY_STANDARD.md` to runtime-integrity finding for startup-order side effects until hardening is implemented.

---

## Consistency Check 5 — Alert Eligibility Gates

### Requirement
Alerts should be described as only occurring after evaluation and eligibility checks.

### Findings

**Runtime causal path is consistent:**
- Causal-chain review confirms ingestion path gates alert sending on evaluation results (`should_alert` gate after `process_ladder_transition(...)`).

**Cross-document exception present:**
- Causal-chain review and API reference acknowledge `/kalshi/composed` manual endpoint can trigger alert flow outside normal transition/evaluation lineage.
- This exception is not clearly called out in doctrine-level architecture language.

### Assessment
**Status: PARTIAL CONSISTENCY WITH DOCUMENTED BYPASS**

### Recommended small edits
1. In doctrine files (`ARCHITECTURE.md` and/or `OPERATING_MODE.md`), add one sentence distinguishing:
   - canonical runtime alert path (post-evaluation eligibility only), vs
   - manual diagnostic endpoint behavior (non-canonical lineage).
2. In `API_REFERENCE.md`, add explicit “non-canonical causal lineage” language under `/kalshi/composed` safety notes.

---

## Contradictions Identified

1. **Observability read-only doctrine vs first-hit mutation behavior**
   - Doctrine: observability is read-only/non-causal.
   - Runtime integrity review: app-layer startup hooks can mutate runtime state even when first request is observability.

2. **Replay “exact live behavior” phrasing vs replay branch limitations**
   - Doctrine language can imply full live-path equivalence including alert/evaluation outcomes.
   - Integrity reviews document replay disables alert delivery branch and can diverge in evaluation lineage/timestamp surfaces.

---

## Missing Doctrine References

1. Missing explicit module-level authority reference to `transition_emitter` in core architecture doctrine.
2. Missing canonical five-stage causal-chain sentence in doctrine docs outside the causal-chain review.
3. Missing doctrine-level mention of manual `/kalshi/composed` bypass as non-canonical causal path.
4. Missing explicit doctrine caveat connecting observability endpoint purity claims to app bootstrap lifecycle behavior.
5. Missing replay doctrine precision separating transition parity requirements from outbound alert-delivery side effects.

---

## Recommended Wording Normalization (Small, Non-Structural)

1. **Authority sentence normalization**
   - Reuse identical phrase across doctrine/reviews: “`core.transition_emitter.emit_transition_if_changed(...)` is the single transition emission authority.”

2. **Canonical chain normalization**
   - Reuse exact chain tokenization across docs:  
     `observation → transition → evaluation → suppression → alert`.

3. **Replay boundary normalization**
   - Standard sentence: “Replay enforces transition parity and deterministic state reconstruction; replay does not perform live alert delivery side effects.”

4. **Observability boundary normalization**
   - Standard sentence: “Observability handlers are read-only by contract; end-to-end read-only guarantee also requires bootstrap/autostart isolation from observability request paths.”

5. **Eligibility-gate normalization**
   - Standard sentence: “In canonical ingestion runtime, alert delivery is permitted only after deterministic evaluation and eligibility gating; manual endpoints are explicitly non-canonical.”

---

## Final Conclusion

The corpus is close to doctrinally unified, but currently contains **two material doctrine-vs-review tensions** (observability end-to-end purity and replay equivalence wording) plus several missing explicit cross-references. Addressing the small wording edits above should bring architecture doctrine and integrity-review outputs into a single unambiguous contract without changing runtime behavior.
