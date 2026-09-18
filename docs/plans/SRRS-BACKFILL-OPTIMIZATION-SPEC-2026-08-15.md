# SRRS CLI Backfill Optimization Spec — 2026-08-15

## Scope and hard stop

This is an isolated performance candidate for the approved NCEI SRRS CLI historical backfill. It does **not** change calibration, evaluator, production, trading, sweep, or the live approved process/output. The candidate is profile-only plus a bounded fixed-sample equivalence QC. No optimized full-range acquisition is authorized by this spec; switching requires Dan's separate approval after QC.

Baseline/current artifacts are read-only:

- active approved resume output: `data/raw/srrs_cli_backfill/2026-08-15-srrs-cli-backfill-final/`
- preserved v2 acquisition: `data/raw/srrs_cli_backfill/2026-08-15-v2-full/`
- frozen policy: `scripts/fetch_srrs_cli.py`
- current runner: `scripts/backfill_srrs_cli_v2_phase2.py` and `scripts/resume_srrs_after_khou_cca.py`

## Baseline measurements

Profile before implementation from the existing `manifest.jsonl`, `attempts.jsonl`, raw tree, scan tree, checkpoint, and process state. Record UTC timestamp, file hashes/stat-before/stat-after, completed key count, worklist scope/hash, keys/minute, wall-clock/active elapsed time when available, HTTP requests per key, request kinds, scan-index bytes, range bytes, raw bytes, retries/errors/rate limits, duplicate-content counts, station/date distribution, and projected remaining ETA. A profile is invalid if a file changes while being read; retry or mark the measurement blocked.

The approved worklist is deterministic: 20 frozen stations, one key per station/date, `2025-01-15..2026-02-28` inclusive (8,200 keys), with target order date then station. Existing acquired evidence is never interpreted as a disposition without applying the frozen lineage policy.

## Permitted optimizations

1. Reuse one persistent `requests.Session` per worker thread (connection pooling only; same official NCEI origin and user agent class).
2. Deduplicate listing and `.scan` acquisition by URL and cache identity; index a given archive day/hour/scan once for all station/date consumers.
3. Cache exact response bodies and metadata; reuse only when URL, canonical request headers (including `Range`), status/Content-Range, byte count, and SHA-256 match.
4. Cache exact range bodies by `(ANX URL, canonical Range header)`; never substitute a full ANX body or a different range.
5. Bounded worker pool, default four workers and configurable; no unbounded async/forking and no per-station process fan-out.
6. Deterministic coordinator ordering: workers may finish out of order, but manifest selection/checkpoint commits are ordered by the frozen worklist.
7. Keep all existing report/range/scan provenance and lineage behavior unchanged.

No proxy substitution, endpoint change, altered station mapping, broader date search, content parsing shortcut, or policy/calibration/evaluator change is permitted.

## Rate limits, retries, and errors

A global limiter applies a minimum inter-request delay (default 0.30 seconds; configurable but never below 0.05 seconds) across workers. HTTP 408/425/429/500/502/503/504 and network exceptions use bounded exponential backoff, honoring a valid `Retry-After`, with at most five retries by default. Every attempt is logged with URL, request kind, target key, attempt number, status/error, bytes, Content-Range, cache hit/miss, timestamp, and response SHA when present. A 429 or retry exhaustion is visible and stops/blocks the affected work; it must not become silent missing data. Range responses must be 206 with an exact matching Content-Range and byte length.

## Provenance and cache identity

Every selected/retained report keeps station/date, issued-by/PIL/WMO, archive scan URL and scan SHA, scan row, ANX URL, exact start/length, raw SHA/bytes, internal summary date, classification, revision and lineage labels, and selection marker. Cache metadata is written atomically beside the body. Cache identity is SHA-256 of a canonical tuple containing URL, sorted request headers, request kind, and schema/policy version. A cache hit is itself an attempt/provenance event; an old body without matching metadata is unusable.

## Checkpoint atomicity and deterministic output

A terminal record is committed only after its raw evidence and request provenance are durable. Manifest and checkpoint are replaced with `os.replace` from same-directory temporary files; no in-place truncation. Checkpoint includes schema, policy/runner identity, scope/worklist SHA, completed key map, and source hashes. On resume, scope/worklist/policy/checkpoint/manifest key sets must match exactly. A crash may leave the old complete pair or the new complete pair, never a partially written pair.

Final output is sorted by `(target_date, station)` and selected through the unchanged `select_lineage` policy. Full-day conflicts remain blocked; duplicate raw content remains retained and labeled, not collapsed out of provenance. Full-day max/min, selected report identity, report lineage classes/labels, exact raw SHA/range, and manifest semantics are deterministic.

## Equivalence QC gate

Before any full optimized acquisition, replay already acquired evidence for a fixed sample of 20 stations × `2025-01-15..2025-01-29` (300 station/date keys) into a separate output. Compare baseline vs candidate for every key:

- selected report identity and exact raw SHA, ANX URL, range start/length, scan URL/SHA;
- full-day max/min;
- accepted report count, semantic classes, revision/duplicate labels, lineage counts/status, and selected marker;
- key/station/date/search window/candidate-row/status/failure and manifest semantics;
- deterministic output ordering and worklist/sample hash.

Any difference is a **BLOCK**. The QC must not read or write the live output except read-only source reads, and it must not issue network requests. Candidate hashes, sample counts, source stat/hash guards, and pass/block reason go into the continuity receipt.

## Rollback and switching

The candidate writes only its own script, spec, QC output, and continuity receipt. Rollback is deleting/quarantining the candidate output/script or reverting the isolated change; the approved runner/output/checkpoint remains untouched. Do not point the live process at the candidate, stop/restart it, or delete/rename any approved artifact. A later switch requires: Dan's explicit approval, a fresh preflight, a new isolated output root, a second checkpoint/manifest review, and preserved rollback path to the approved runner.

## Acceptance

Accepted only if py_compile and focused lineage tests pass, the fixed sample has exact selected-output equivalence with zero differences, invariants above are evidenced, and the receipt records the result. This document intentionally does not authorize a full optimized run.
