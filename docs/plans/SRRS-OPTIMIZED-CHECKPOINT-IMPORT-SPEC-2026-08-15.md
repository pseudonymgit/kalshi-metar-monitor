# SRRS optimized checkpoint import/resume specification — 2026-08-15

## Authority and scope

This is a bounded safety change to the QC-passed candidate
`scripts/backfill_srrs_cli_v2_optimized.py`. It adds `acquire --resume-from`
without changing the frozen `fetch_srrs_cli.py` policy, selected outputs,
calibration, evaluator, production, trading, or sweep behavior. It does **not**
authorize or launch the full optimized acquisition.

Source input is the approved, immutable checkpoint root
`data/raw/srrs_cli_backfill/2026-08-15-srrs-cli-backfill-final/` (318 completed
keys from the deterministic 8,200-key worklist) plus the reviewed KHOU v3
lineage referenced by that checkpoint. The destination must be a new sibling
(or otherwise independent) optimized output root.

## Compatibility contract

Import is accepted only when all of these checks pass before destination
initialization:

1. **Exact worklist:** requested `--start`, `--end`, and frozen station order
   match the source checkpoint scope; the canonical generated worklist SHA and
   count must equal source `worklist_sha256`/`worklist_count`. Every source
   manifest row must be the exact deterministic worklist item at its recorded
   checkpoint `manifest_index`, with no duplicate, missing, or out-of-scope key.
2. **Policy/schema identity:** source checkpoint schema is v2, every imported
   row is schema v2, and the policy path is exactly
   `scripts/fetch_srrs_cli.py`. The imported runner computes and records the
   frozen policy SHA-256; it never uses a source status as a new disposition.
3. **Lineage identity:** any source v3 lineage hashes are verified against the
   sibling KHOU v3 `checkpoint.json`, `manifest.jsonl`, `review.json`, and
   `run_summary.json` files before import. The explicit CCA-reviewed selected
   report remains the source record; no raw-policy re-selection or v1 status
   reinterpretation occurs.
4. **Source hash verification:** manifest and checkpoint bytes are SHA-256
   hashed before and after validation/copy. Every referenced raw body must
   exist, remain inside the source root, have the declared byte count and
   SHA-256, and copy byte-for-byte to the destination. A stale historical
   `run_summary.json` is not used as the source of truth; direct manifest,
   checkpoint, row, and raw-reference checks are authoritative.
5. **Completed-key validity:** only source rows with status
   `accepted_full_day` are importable. Missing, blocked, failure, incomplete,
   or unknown statuses fail closed; they are never copied into the destination
   checkpoint. Each imported row must have a selected report whose identity is
   retained exactly and whose accepted/raw references validate. Source
   statuses are not duplicated or translated into optimized statuses.
6. **Read-only source:** source and destination may not resolve to the same
   root, and destination may not be inside the source tree. No source file is
   opened for writing.

## Destination initialization and continuation

Validation occurs before any destination directory is created. The importer
builds a temporary sibling staging root, creates `raw/` and `cache/`, copies
only raw files referenced by imported terminal records (accepted reports and
their retained failure/provenance references), writes the preserved manifest,
and writes an optimized v2 checkpoint with imported keys and the full remaining
worklist identity. A same-directory atomic rename publishes the new root.

The normal optimized coordinator then skips only the validated imported keys
and acquires all remaining worklist keys in deterministic order. It retains the
existing candidate's persistent sessions, shared cache, exact-range checks,
retry policy, ordered manifest writes, and atomic per-key checkpoints. Imported
records are not re-fetched, reclassified, or overwritten.

## Rollback / failure behavior

Any incompatibility, source mutation, missing/mismatched raw body, unsafe path,
copy collision, destination pre-existence, or failed atomic publication aborts
before publishing the destination. The temporary staging root is removed; the
source and any pre-existing destination are untouched. Once published, rollback
is ordinary artifact quarantine/removal of the new destination only; the
approved source remains the rollback baseline. No full acquisition is started
by import tests.

## Required focused gates

- incompatible worklist and policy/schema/lineage inputs fail closed;
- mutation of source manifest, checkpoint, or referenced raw body fails closed;
- imported row/key and raw-reference counts are exact;
- repeated import into fresh destinations has deterministic manifest/checkpoint
  initialization (apart from explicitly recorded import timestamp, if any);
- fixed 300-key exact equivalence remains zero-difference and offline;
- four frozen lineage tests and Python compilation pass.

Accuracy-focused suggestion: before any bounded optimized benchmark, add a
second disjoint sample oversampling duplicate/revision/CCA-prone keys and
require exact selected raw SHA, max/min, and lineage provenance equality.
