# SRRS Backfill Optimization — bounded live benchmark specification

Date: 2026-08-15 UTC  
Candidate: `scripts/backfill_srrs_cli_v2_optimized.py`  
Policy: `scripts/fetch_srrs_cli.py` (v2 lineage/CCA policy)  
Benchmark root: `data/raw/srrs_cli_backfill/2026-08-15-srrs-optimization-benchmark/`

## Scope and isolation

Run exactly one bounded live performance benchmark, not a backfill. The sample is the disjoint 300-key block immediately after the existing fixed equivalence sample:

- stations: the frozen 20-station set from `fetch_srrs_cli.py`;
- target dates: `2025-01-30` through `2025-02-13` inclusive;
- keys: date-major then frozen station order, 15 × 20 = 300;
- sample/worklist SHA-256: computed from the runner's canonical worklist and recorded in the benchmark artifacts;
- candidate-QC disjointness: the existing `2026-08-15-srrs-optimization-qc` sample ends at `2025-01-29`; the benchmark sample has no raw/scan evidence in that candidate-QC root at preflight.

The benchmark root must be fresh and is the only data root written by the live candidate. Do not write approved roots, the existing fixed QC root, calibration/evaluator/production/trading/sweep artifacts, or the source runner/policy. Do not launch the remaining range or switch the live process.

Before and after the run, record stat/hash guards for the candidate script, frozen policy, approved baseline roots, and existing fixed-QC root. Any source or approved-root change is a BLOCK.

## Command and execution contract

Run once, exactly:

```text
python3 scripts/backfill_srrs_cli_v2_optimized.py acquire \
  --output data/raw/srrs_cli_backfill/2026-08-15-srrs-optimization-benchmark \
  --start 2025-01-30 --end 2025-02-13 \
  --workers 4 --delay 0.30 --timeout 60 --retries 5
```

Required implementation settings: four bounded workers; one global minimum inter-request delay of 0.30 seconds; persistent `requests.Session` per worker; shared URL/cache identities; exact-range cache; atomic cache, manifest, and checkpoint writes; unchanged v2 lineage and CCA/review semantics. No proxy, endpoint, station mapping, search-window, parser, or policy changes.

A run stops immediately on a rate-limit response that exhausts retries, any request/network failure that exhausts retries, invalid range status/Content-Range/length, cache identity collision, manifest/checkpoint inconsistency, non-terminal key, selected-output/provenance mismatch discovered during verification, or any attempted write outside the isolated benchmark root plus the explicitly required spec/continuity documents.

## Metrics and hashes to capture

Capture UTC start/finish wall clock and elapsed seconds from the run summary/process wrapper. Compute keys/minute as 300 divided by elapsed minutes; do not infer an ETA until this measured result exists. Record:

- sample count, scope, worklist/sample SHA, command, workers, limiter, timeout/retries, runner/policy hashes;
- terminal keys/status counts and deterministic manifest ordering;
- total attempt records, logical unique request URLs, request kinds;
- HTTP status/cache-hit/cache-miss counts, cache-hit bytes, network bytes;
- HTTP 429, other HTTP errors, network errors, retries/backoff records;
- raw occurrence count, raw unique-body count, duplicate-content occurrences, raw bytes;
- scan/cache body bytes and total benchmark-root storage bytes, with raw/scan/cache breakdown;
- SHA-256 of manifest, checkpoint, attempts, run summary, and the benchmark root file inventory/metric payload.

Attempt records must retain URL, request kind, target key, attempt number, status/error, bytes, Content-Range, cache hit/miss, timestamp, and response SHA when present.

## Exact output equivalence gate

After the live run, create an independent baseline subset under the benchmark root from the benchmark's immutable evidence and recompute lineage with the frozen policy in a separate comparator (no network). Compare the candidate and independent baseline for all 300 keys. Require exact equality of:

- selected report identity, selected raw SHA-256, raw URL, exact range start/length, scan URL and scan SHA;
- full-day max/min;
- accepted report count, complete accepted-report identities, semantic classes, revision and duplicate labels, lineage counts/status, and selected marker;
- key/station/date, search window, candidate-row count, status/failure records, and manifest semantics;
- deterministic ordering, complete key set, and sample/worklist hash.

Any difference, missing evidence, or inability to independently reconstruct the baseline is BLOCKED. Exact comparison must be read-only with respect to approved roots and must issue zero network requests.

## Storage and network stop conditions

Stop and mark BLOCKED if the candidate root is not fresh, any source/approved guard changes, the sample is not exactly 300 disjoint keys, a request returns an invalid exact range, cache metadata/body identity is inconsistent, any HTTP 429/retry exhaustion/error remains, any key is non-terminal, raw evidence is missing, or checkpoint/manifest hashes are not durable and internally consistent. Do not silently convert failures to missing keys.

## Switch criteria

A PASS means only that this bounded measured sample completed and passed exact equivalence. It does **not** authorize a full optimized run or a live switch. Switching requires separate parent/Dan authorization, a fresh preflight, a new isolated output root, a second manifest/checkpoint review, and a preserved rollback path to the approved runner. A full-run ETA may be projected only from the measured benchmark throughput, clearly labeled as a projection with sample limitations; it is not an approval.

Accuracy-focused follow-up: before switching, add a second disjoint QC sample deliberately oversampling revised, duplicate-content, mapping-mismatch, and conflict-prone station-dates, including a reviewed KHOU-like CCA case, and require exact raw SHA/range/max-min/lineage equality again.
