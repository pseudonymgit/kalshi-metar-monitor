# SRRS Backfill v2 Resume Correction Specification

**Date:** 2026-08-15 UTC  
**Status:** approved process-control correction; provenance/QC only

## Incident and scope

The approved run invoked the v1 fetch process-control path. It produced 172 station/date attempts across the nine-date pilot (`2025-01-15` through `2025-01-23`) for the frozen 20-station contract. The v1 status decision treated ordinary intermediate and completed full-day CLI reports as an ambiguity: 170 rows were `blocked_ambiguous` and only two were `found`. That decision is invalid for this source because intermediate reports are provenance and a coherent full-day report is the settlement-facing descriptive candidate. The v1 raw bodies, scans, 94,762 attempts, manifest, and checkpoint are preserved and are never rewritten or used as v2 completion evidence.

The corrected driver is `scripts/backfill_srrs_cli_v2.py`. It computes status only from raw report semantics and `fetch_srrs_cli.py::select_lineage`; it never uses a v1 status as a v2 decision. V1 status is retained only as incident provenance. No calibration, evaluator, production, trading, sweep, or source-acceptance change is in scope.

## Artifact separation and no-loss migration

- V1 root: `data/raw/srrs_cli_backfill/2026-08-15/` (immutable incident evidence).
- V2 root: `data/raw/srrs_cli_backfill/2026-08-15-resume-v2/` (fresh checkpoint, append-only manifest, attempts, raw/scans, summaries).
- V2 migration reads cached v1 report references and raw bytes, re-parses each body, and applies `select_lineage`.
- Every source raw occurrence is copied or hash-verified into v2; identical bytes remain separate occurrence records and receive `duplicate_content` labels. Intermediate, full-day, revision, rejected/mismatched, and missing/conflict evidence is retained. V1 attempts/checkpoint remain in place and are hash-recorded, not collapsed into v2 statuses.
- A v2 checkpoint is atomically updated only after a terminal v2 manifest record is appended. Resume skips only a key with a validated v2 record and artifact references. It does not consult v1 `status` values.
- A migration fails closed if any expected source key/report/raw reference is absent, any immutable raw collision differs, any v2 lineage is unresolved, or any full-day settlement values conflict.

## Frozen range and contract

- Official source only: NCEI SRRS ANX `.scan` indexes plus exact HTTP byte ranges.
- Stations: exact frozen 20-station mapping in `fetch_srrs_cli.py`.
- Target dates: **2025-01-15 through 2026-02-28 inclusive**, 410 dates × 20 stations = **8,200 station/date keys**.
- Search dates per target: target −1, target, target +1 archive UTC dates; all listed hours.
- No whole-bundle requests, proxy substitution, neighboring-station substitution, carry-forward, or alternate product.

## Phase gates and stop conditions

1. **Phase 0:** this spec and v2 driver/options exist; v1 artifact hashes and source script/options are recorded.
2. **Phase 1:** reclassify all nine cached dates (180 keys) with v2 lineage; prove exactly one record per key, no report/raw occurrence loss, zero full-day conflicts, and deterministic rerun hashes. Only cached scans/raw are used for this proof unless an official SRRS gap is explicitly required.
3. **Phase 2:** after Phase 1 passes, resume remaining keys with v2, checkpointing every terminal key and preserving missing/conflict rows. Completion requires all 8,200 keys accounted for by v2 terminal records; v1 statuses do not count.
4. **Phase 3:** only after v2 completion, run the independent read-only audit and write `.meta/continuity/weather-engine/2026-08-15-srrs-cli-backfill-v2.md` with counts, bytes, lineage classes, missing/conflict rows, hashes, and deterministic proof.

Hard stop immediately if `select_lineage` cannot distinguish lineage, if any non-identical full-day settlement conflict is found, if checkpoint migration loses rows/references, if an immutable cached body changes, if a range response is not exact HTTP 206, or if any code path attempts to use a v1 status for a v2 decision. Missing and unresolved rows remain explicit; they are never substituted.

**Accuracy-focused suggestion (deferred):** after a separate source-acceptance decision, compare each selected full-day max/min with a delayed finalized independent official record and retain discrepancies as revision/provenance flags rather than overwriting raw evidence.
