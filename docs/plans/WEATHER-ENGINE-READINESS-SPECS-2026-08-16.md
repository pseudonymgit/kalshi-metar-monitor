# Bounded Readiness Specs — 2026-08-16

## 1. Cleanup accounting / transaction plan (accounting only)

**Scope:** shared checkout inventory only; no deletion, restore, reset, clean, move, commit, or sweep in this task.

**Inputs:** `READINESS-WORKTREE-MANIFEST-2026-08-16.json`, Git status, HEAD blob hashes, import/reference graph, PROJECTS/TASKS/roadmaps, continuity receipts.

**Procedure:**
1. Freeze status and filesystem hash manifest.
2. Reconcile each path to one of six dispositions with owner, purpose, provenance, consumers, recovery path, and regeneration recipe.
3. QC the classification using a second reviewer and path-level reference scan.
4. Prepare a reversible transaction manifest; separate canonical source, research raw, receipts, derived output, and unknowns.
5. Only after explicit cleanup authorization: stage archive moves, verify hashes, rerun collection/tests, then report transaction receipts.

**Acceptance:** every candidate has a non-filename rationale; zero UNKNOWN paths in the proposed transaction; SHA-256 and byte totals reconcile before/after; no source/raw lineage loss.

## 2. Collection-error repair spec

**Scope:** restore import compatibility without re-enabling retired signals or changing canonical behavior.

**Implementation:** compatibility loaders in `core/_archive_compat.py`, active-path shims for deleted modules, import-only retired signal aliases, and `pytest.ini` active test roots to prevent copied raw snapshots from becoming active tests.

**Acceptance:** imports resolve; retired classes absent from `REGISTERED_SIGNAL_NAMES` and `SignalRegistry.signals`; targeted collection is zero-error; focused behavior tests pass; full active-root collection receipt records count/errors/warnings.

## 3. GEFS f057 recovery/validation spec

**Scope:** four non-missing fixtures and two native members (`gec00`, `gep01`); expected-missing fixture remains fail-closed.

**Required QC:** native GRIB/idx hash and semantic TMP validation; complete required lead set per member; local-day window and timezone mapping; deterministic request timing with every serialized request gap >=10s; fixture/member/lead missingness ledger; no labels before cutoff-safe evaluation; separate geometry-ready vs score-ready dispositions.

**Acceptance:** `PILOT_BLOCKED` clears only after timing receipt and all required native geometry checks pass. No interpolation/substitute/alternate source is allowed for missing official boundary leads.

## 4. Other readiness fixes

- Keep runner fail-closed and source-immutable; rerun preflight only against a clean reviewed snapshot.
- Add duplicate test-tree exclusion to collection config, while retaining dedicated tests for production code.
- Reconcile stale ledger numbers only after fresh receipts; preserve historical receipts and link the superseding state.

## QC / receipts

Expert receipt: this document and `WEATHER-ENGINE-READINESS-ACCOUNTING-2026-08-16.md`.
QC receipt: compatibility imports and focused collection checks were run; full active-root collection and behavior suite remain required after final process completion.
Accuracy improvement: require the boundary-lead completeness metric in every GEFS receipt so partial native trajectories cannot be promoted to score-ready status.
