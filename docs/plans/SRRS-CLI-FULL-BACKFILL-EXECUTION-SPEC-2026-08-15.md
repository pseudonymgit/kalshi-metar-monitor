# NCEI SRRS CLI Full Backfill — Execution Specification

**Date:** 2026-08-15 UTC  
**Status:** approved execution plan; provenance/QC only  
**Authority:** `docs/plans/SRRS-CLI-FULL-BACKFILL-SPEC-2026-08-15.md` plus corrected lineage policy `docs/plans/SRRS-CLI-REPORT-LINEAGE-QC-2026-08-15.md` (`2026-08-15-lineage-qc-v2`).  
**Explicit approval:** Dan explicitly approved execution of the full NCEI SRRS CLI backfill in the requester instruction on 2026-08-15 UTC. This approval authorizes only the bounded archive acquisition and read-only QC/audit described here; it does not authorize evaluator/calibration enablement, sweeps, deployment, or trading changes.

## Scope

- Official host/path only: `https://www.ncei.noaa.gov/data/service-records-retention-system/access/anx/`.
- Exactly the frozen 20 mapped stations in the full-backfill spec; no mapping changes, city/airport substitutions, or proxies.
- Target dates exactly **2025-01-15 through 2026-02-28 inclusive**: 410 dates × 20 stations = 8,200 station/date keys.
- Search bounded archive UTC dates target date −1, target date, target date +1 and every listed hour.
- `.scan` indexes and exact HTTP byte ranges only. Whole ANX bundles are prohibited; no unbounded GET/fallback.

## Corrected report-lineage policy (v2)

- Preserve every raw intermediate, full-day, revision, rejected/mismatched, and duplicate occurrence with immutable bytes, provenance, and SHA-256.
- Classify from raw report semantics. `full_day` is preferred when coherent; intermediate is retained as provenance and is never used as a settlement value.
- Byte-identical repeated occurrences are `duplicate_content` provenance, not conflicts.
- Non-identical full-day bodies that disagree on settlement values block as `blocked_conflicting_full_day_revisions`; unclassifiable lineage blocks; missing full-day blocks rather than substituting an intermediate.
- Internal summary date and exact station/PIL/WMO identity control key assignment. No relabeling by archive or issuance date.

## Rate limits and retries

Requests are globally serialized with default 0.20 s inter-request delay (never below 0.05 s for controlled QC). User-Agent identifies the official SRRS backfill. Retry only transient network/HTTP 408, 425, 429, 500, 502, 503, 504 responses up to five retries with exponential backoff capped at 120 s; honor numeric/HTTP-date `Retry-After`. Stop on archive/rate-limit/source-integrity failure; never substitute a proxy.

## Checkpoint/resume

Output root: `data/raw/srrs_cli_backfill/2026-08-15/`. `checkpoint.json` is atomically updated after each station/date terminal record. `manifest.jsonl` and `attempts.jsonl` are append-only. Resume skips only keys with validated terminal records/artifacts; interrupted/failed work is retried. Existing v1/v2 artifacts remain preserved and are not rewritten or deleted.

## Storage and artifacts

- `scans/`: cached directory listings and `.scan` bytes only.
- `raw/`: immutable exact range bodies addressed by station/date/ANX/start/length/hash.
- `reports/`: parsed/read-only reports and lineage outputs.
- `failures/`: missing, mismatch, conflict, and blocked evidence.
- `manifest.jsonl`, `attempts.jsonl`, `checkpoint.json`, `run_summary.json`.
- Final continuity receipt: `.meta/continuity/weather-engine/2026-08-15-srrs-cli-backfill.md`.

## Preflight and verification gates

Before resuming, run the focused v2 lineage tests and deterministic KATL proof, then run the bounded 20-station/2025-01-15 v2 reclassification against preserved v1 artifacts. Verify v1 hashes remain unchanged. After acquisition, run deterministic manifest/reclassification verification twice, hash manifests/checkpoint/raw/report artifacts, and report exact counts, dates, bytes, attempts, missing/conflicts, and runtime.

## Stop conditions and disposition

Stop immediately on archive integrity failure (invalid HTTP range/content range/body size, immutable-byte collision, corrupted cache), sustained rate-limit/access failure, or source-integrity failure. Missing and unresolved/conflicting records remain explicit; they are not filled from other products. This is descriptive provenance/QC only: no evaluator/calibration enablement, sweep, deployment, or trading changes.

**Accuracy-focused suggestion:** after a separate source-acceptance decision, compare each selected coherent full-day CLI value against a delayed finalized independent official record, retaining any discrepancy as an explicit revision/provenance flag rather than overwriting raw evidence.
