# Execution Snapshot Reconciliation Specification — 2026-08-15

## Purpose and non-goals

This specification defines how to make a reviewable execution snapshot from the
currently dirty weather-engine repository without weakening the unattended runner.
It is a documentation and review gate only. It does **not** authorize a sweep,
deployment, paper/live trading, staging, commit, reset, clean, deletion, or source
change. The runner remains fail-closed on dirty state.

The snapshot must preserve the corrected evaluator, its safety tests/receipts, the
unattended runner, and the GHCN/Kalshi equivalence work as a coherent review unit.

## Classification taxonomy

Every path reported by `git status --porcelain=v1 --untracked-files=all` is assigned
exactly one primary category and a disposition (`include`, `exclude`, or `review`).
Ignored paths are inventoried separately and are not silently treated as clean.

1. **Intended source changes** — source/configuration changes required by the
   corrected evaluator, its dependency path, the unattended runner, or the
   equivalence audit. Includes relevant tracked modifications and intentional
   tracked deletions, but only after a reviewer confirms each deletion.
2. **Required new tests/specs/receipts** — focused tests, audit scripts, plans,
   manifests, and continuity receipts that prove the intended source change and
   its execution boundary. These are included when directly linked to the current
   work item.
3. **Generated artifacts** — sweep outputs, graphify reports/cache, logs, result
   databases, `.json`/`.csv` outputs, archives, signatures, and other products of
   execution. Exclude by default; include only if a spec names the artifact as a
   frozen evidence input and its hash/lineage is recorded.
4. **Archived modules** — backup/retired modules, historical snapshots, `.orig`,
   `.review`, dated copies, or modules outside the canonical import path. Exclude
   from execution unless the review explicitly requires an immutable reference.
5. **Temporary/cache files** — bytecode, `__pycache__`, pytest/graphify caches,
   editor files, temporary databases, and transient logs. Exclude; never use them
   to infer source correctness.
6. **Unrelated work** — paths not required by the corrected evaluator, runner,
   or equivalence sprint. Exclude from this snapshot and leave untouched for the
   owning workstream.

## No-delete/no-loss policy

No path may be deleted, cleaned, reset, overwritten, or discarded as part of
reconciliation. A tracked `D` is evidence of a proposed deletion, not permission
to remove anything. Every dirty path remains recoverable from the source tree and
is listed or represented by a manifest before any future snapshot operation.
Review must distinguish an intentional retirement from accidental loss. If a path
cannot be classified with confidence, disposition is `review` and execution is
blocked.

## Snapshot inclusion and exclusion rules

The snapshot is a narrow execution unit, not a copy of the whole dirty tree.
Include only:

- the corrected evaluator and its imported runtime path;
- `scripts/run_unattended_sweep.py` and its canonical evaluator/validation
  dependencies;
- focused evaluator/runner/pre-sweep tests;
- the associated dated specs, audit scripts, manifests, and continuity receipts;
- configuration/data files explicitly named as frozen inputs by the accepted spec;
- tracked deletions only when review confirms they are part of the same change and
  the deletion is represented in the patch.

Exclude by default:

- generated graphify trees, sweep outputs, logs, databases, bytecode, caches,
  archives, and experimental outputs;
- archived/retired modules and unrelated source or documentation;
- secrets (`.env*`), credentials, local virtual environments, and machine-local
  state;
- any file whose provenance, ownership, or relation to the work item is unknown.

An excluded path is not deleted or cleaned. It remains in the original shared
workspace and remains a dirty-state blocker there.

## Freeze rules

Before review, capture UTC time, `HEAD`, complete porcelain status, and SHA-256
manifests. During any eventual execution snapshot:

- **Source freeze:** `app.py`, `core/`, and the approved evaluator/runner scripts
  cannot change after the manifest is accepted.
- **Config freeze:** approved `config/` files and all evaluator parameters are
  immutable; no implicit defaults or generated config is allowed.
- **Data freeze:** approved input data is read-only and hash-pinned. Outputs are
  written outside the frozen input set. No network acquisition, backfill, or
  mutation of databases is permitted.
- **Execution freeze:** the unattended runner's dirty-tree gate, canonical
  command, integrity checks, single-child policy, timeout, and before/after status
  comparison remain unchanged. No `--no-integrity` or dirty-path exception may
  cover evaluator/source/config/data.

A changed hash or status after review invalidates the snapshot and requires a new
review. A snapshot is not authorization to execute.

## Approval gate

Two-person review (or explicit designated-operator review recorded in the
continuity receipt) must approve:

1. the path manifest and every `include`/`exclude`/`review` decision;
2. all tracked modifications and deletions, with diff/stat inspection;
3. source/config/data hashes and input provenance;
4. focused tests and full-suite acceptance criteria;
5. the exact canonical command and the fact that no sweep/trade/deploy occurs in
   this reconciliation step.

Any unresolved `review` item, missing required test/receipt, dirty source/config/data
outside an explicitly approved snapshot, hash drift, or test collection failure is
`BLOCKED`. The approval gate must not be implemented by weakening the runner's
fail-closed dirty-tree policy.

## Recommended snapshot mechanism

Because all workspace changes are shared and the source worktree is highly dirty,
choose **Option B: an isolated clone/worktree snapshot assembled from an approved
manifest**, after review. Do not stage or commit the shared worktree merely to make
the runner pass.

Review-only command sequence (not executed by this reconciliation):

```sh
cd /home/node/.openclaw/workspace/prototypes
# Create an isolated destination from the current committed baseline.
git clone --no-local weather-engine-source weather-engine-execution-snapshot-20260815
cd weather-engine-execution-snapshot-20260815
# Export the reviewed tracked patch from the source (run from source directory).
(cd ../weather-engine-source && git diff --binary HEAD -- . ':!docs/plans/EXECUTION-SNAPSHOT-RECONCILIATION-SPEC-2026-08-15.md') > /tmp/weather-engine-reviewed.patch
# Apply only after the manifest and reviewer approve the patch.
git apply --check /tmp/weather-engine-reviewed.patch && git apply /tmp/weather-engine-reviewed.patch
# Copy only approved untracked files listed in the signed/reviewed manifest.
(cd ../weather-engine-source && rsync -a --files-from=/path/to/approved-files.manifest ./ ../weather-engine-execution-snapshot-20260815/)
# Verify the destination, then run only the documented QC/preflight; do not sweep.
git -C ../weather-engine-execution-snapshot-20260815 status --short
```

The source worktree remains untouched. The destination must still be checked for
secrets, generated files, missing dependencies, hash drift, and accidental path
inclusion before any future operator considers a preflight.
