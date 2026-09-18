# SRRS Dual-Path Backfill / Audit Replay Specification — 2026-08-15

## Status and lane boundary

SRRS is an **optional audit/replay path**. It is not a calibration prerequisite, evaluator prerequisite, trading prerequisite, or sweep prerequisite. This work must run in an isolated output root and must not modify the core calibration lane, production data, evaluator inputs, trading artifacts, proxy sources, or sweep state.

The baseline and optimized paths are separate experiments with separate source snapshots, manifests, checkpoints, attempts, caches, raw evidence, logs, and receipts. Existing SRRS artifacts are immutable rollback evidence and must not be reset, deleted, or redirected.

## Paths

- **Baseline:** the approved v2/v3 lineage policy and preserved baseline artifacts under `data/raw/srrs_cli_backfill/2026-08-15-srrs-cli-backfill-final/`; baseline acquisition uses the conservative single-path driver and official NCEI exact ranges.
- **Optimized:** an immutable copied source snapshot of the optimized runner plus frozen policy/config under an isolated snapshot directory. It uses bounded concurrency (default 4 workers), a global 0.30-second limiter, persistent worker sessions, shared URL/cache identities, exact byte-range validation, and deterministic coordinator-order checkpoints.
- Neither path writes into the other path's root. A benchmark failure never authorizes a switch.

## Immutable source snapshot and hash guard

Before execution, copy the optimized runner, frozen `fetch_srrs_cli.py` policy, tests/config used by the runner, and a machine-readable snapshot manifest into a new timestamped snapshot directory. Hash every source/config file with SHA-256. The runner is executed from that snapshot and is not edited while it is executing. Capture source hashes before import, before acquisition, after acquisition, and after comparison. Any source/config hash change, missing file, changed policy hash, or terminated process is **BLOCKED**; no speed or ETA inference is permitted.

The imported baseline checkpoint is also guarded by SHA-256 of its checkpoint, manifest, policy, worklist, and reviewed lineage artifacts. If a source manifest/checkpoint/raw body changes during validation or execution, fail closed. Candidate output manifests and checkpoints record explicit `source_hashes`/`config_hashes`, not only a path name.

## Imported 318-key prefix

The approved current prefix contains exactly 318 terminal `accepted_full_day` station/date keys from the deterministic 8,200-key worklist (`2025-01-15..2026-02-28`, 20 frozen stations), with 7,882 remaining keys. Import is allowed only after validating exact scope, policy/schema, worklist hash, contiguous manifest indices, selected-report identity, every accepted raw SHA/byte count/path, and the reviewed KHOU v3 artifacts. No v1 status is consulted or translated. Import copies accepted and retained failure/provenance raw references into a fresh optimized root using staging plus atomic rename; the baseline root is untouched.

## v3 KHOU CCA policy

`KHOU|2025-01-26` uses the reviewed v3 official-CCA correction disposition. The selected corrected issue is `CDUS44 KHGX 271523 CCA`, selected raw SHA `3fe90b212b37027b8d868dfbc9176f09ffedcbb51d6f7368b58910dc31af2662`, values max/min 70/55 F; the superseded issue and raw SHA remain retained as lineage. The v3 checkpoint/manifest/review/summary hashes are checked before import. The optimized path must preserve this explicit reviewed overlay and must not infer or overwrite it from a body classifier.

## Exact benchmark sample and dates

The primary live benchmark is a complete disjoint 300-key sample: all 20 frozen stations for each date **2025-01-30 through 2025-02-13 inclusive** (15 dates × 20 stations). Worklist SHA-256 is `72eeb2100140970bdc674f31fe4b245aa2001ceb28a862035b268b407ad84634`. Default runtime configuration is 4 workers, 0.30-second global limiter, 60-second request timeout, and 5 bounded retries.

Run baseline and optimized outputs separately. The baseline output is the immutable reference for the same 300 keys; the optimized output is the candidate under test. A complete run requires terminal manifest coverage **300/300**, a final run summary, and valid atomic checkpoint/manifest hashes. A partial or terminated run is not a benchmark pass.

## Output equivalence gate

For every one of the 300 keys, require exact equality of selected report identity, selected values (max/min), lineage and revision/duplicate labels, raw SHA-256 and byte range/URL, station, target date, and final status. Require zero differences and preserve all raw/provenance evidence. Source hashes must be unchanged pre/post. Only a complete 300/300 run with zero differences and valid hashes is PASS. Any source mutation, termination, missing key, mismatch, cache collision, range mismatch, 429/error/retry anomaly that violates policy, or checkpoint loss is BLOCKED/FAIL; do not infer speed.

## Metrics and accounting

Capture wall-clock start/end and elapsed seconds, terminal keys, keys/minute, attempts, logical URLs, cache hits/misses, HTTP 429, errors, retries, response/request bytes, raw file count/bytes, cache file count/bytes, source hash pre/post, policy/config hashes, manifest hash, checkpoint hash, attempts hash, and equivalence result hash. Report metrics independently for baseline and optimized; do not compare unlike partial prefixes. For the optional full run, final accounting is 8,200 total keys = imported 318 + newly acquired 7,882, with no silent loss and resumable checkpoint indices.

## Conditional backfill

Only after the benchmark is PASS and source remains frozen may the optimized remaining 7,882-key backfill be prepared. Preparation is not an implicit launch: record the command, destination, imported-prefix hashes, expected remaining count, and rollback plan first. If the explicit direction to test both ways is treated as approval for this optional audit path, launch only after the PASS gate, into a new isolated output root. Baseline artifacts remain untouched. The full run is resumable and must finish with exactly 8,200 accounted keys.

## Checkpoint atomicity

Only the deterministic coordinator writes manifest/checkpoint state. Each update writes a same-directory temporary file, fsync/replace semantics where supported, and updates the checkpoint only after the corresponding manifest row is staged. A checkpoint is valid only when completed keys, contiguous manifest indices, statuses, worklist hash, source/config hashes, and manifest hash agree. A crash leaves the prior committed pair usable; a partial temp file is ignored/quarantined.

## Rollback

Rollback means stop the optional audit path, preserve its partial root and receipt as evidence, and continue using the untouched baseline artifacts if separately authorized. Never delete or mutate the baseline, v3 CCA artifacts, calibration inputs, evaluator inputs, trading outputs, or sweep state. A rejected optimized snapshot/output is quarantined as an isolated candidate; no production enablement or source substitution is allowed.
