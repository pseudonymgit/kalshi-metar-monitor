<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# P0.1 Specification Re-QC — Truth, Rule, Station Manifest and Holdout Custody

**Date:** 2026-08-19  
**Spec reviewed:** `VALIDATION-P0.1-TRUTH-RULE-STATION-HOLDOUT-SPEC-2026-08-19.md` v1.1.0  
**Prior QC reviewed:** `VALIDATION-P0.1-TRUTH-RULE-STATION-HOLDOUT-QC-2026-08-19.md`  
**Additional inputs reviewed:** Gray Room `technical-qc.md`, `governance-qc.md`, and `artifacts/validation/p0.1/DECISION-RECEIPT-2026-08-19.json`  
**Scope:** Independent re-QC of the P0.1 specification only. No code or source changes made.

## Decision

**REVISE — build handoff remains BLOCKED.**

The v1.1.0 revision incorporates the focused QC corrections substantially and correctly. In particular, it now explicitly names Gerri Kellman as independent validation owner and holdout custodian and Bertram Gilfoyle as data/lineage owner; defines chronological, access-controlled, one-read holdout custody with resealing; freezes the exact HIGH/LOW bucket Brier score versus matched station/season climatology as the primary Track A endpoint; adds deterministic event identity, duplicate/overlap handling, source availability fields and cutoff gating, exhaustive probability-vector requirements, mapping/time/DST fields, status precedence, fail-closed acceptance tests, and an explicit non-edge claim boundary. The decision receipt independently confirms `build_authorized: false` and `promotion_authorized: false`.

The remaining gaps are narrow but material to the requested implementation-readiness standard: several machine contracts are still described in prose rather than frozen schemas/artifact references, and package status/receipt semantics are not fully representable. These must be corrected before QC PASS or build handoff.

## Re-QC findings

### 1. Manifest and custody schemas are not fully frozen — REVISE

The two JSONL examples are useful field inventories, but neither declares an explicit schema version or canonical serialization/hash scope. The holdout section lists required custody fields but does not provide a machine-readable custody record schema; the required `holdout_access_log.jsonl` artifact has no declared field contract or status/disposition enum. The receipt list also names receipt contents without defining receipt schemas.

Add, before build:

- `schema_version` (or an explicitly versioned external schema reference) to each manifest, bucket-universe, holdout-custody/access-log, and receipt contract;
- canonical JSON serialization rules, including key ordering, number/date/null handling, newline/record framing, and the exact byte range covered by each hash;
- a typed holdout custody schema containing all listed fields, including `disposition` values such as `SEALED`, `SCORED`, `INVALIDATED`, and `RESEALED`;
- an access-log schema requiring actor, purpose, UTC timestamp, artifact/ref, operation, result, read number, and whether row-level labels/features were exposed;
- typed receipt schemas for `SPEC-RECEIPT`, `QC-RECEIPT`, `BUILD-RECEIPT`, `POST-BUILD-QC-RECEIPT`, and `CONFIRM-RECEIPT`, including their package status and claim boundary.

### 2. Package terminal status is stated but not representable — REVISE

The specification correctly separates `row_status` from `package_status` and defines precedence, but `package_status` does not appear in any declared schema or required artifact. Add a required package-level status field to the receipt/summary contract, constrained to `BLOCKED|FAIL|INSUFFICIENT_SAMPLE|NOT_APPLICABLE|PASS`, with deterministic aggregation using the stated precedence. Preserve all row statuses and reason codes; do not infer package status solely from an omitted/empty row set.

### 3. Raw settlement receipt and immutable source linkage need an explicit artifact contract — REVISE

The truth hierarchy is correct, and the manifest includes `contract_rule_sha256`, `contract_payload_sha256`, `label_source`, and source references. However, the spec does not explicitly require an immutable raw finalized settlement receipt/payload artifact or define its required fields and hash linkage. Add the raw settlement receipt (or a versioned source artifact contract) to the required artifacts and require its hash, contract-rule reference/version, event/market identity, finalization/revision metadata, and reconciliation/block evidence where applicable. A source ID or hash reference alone must not permit an unverifiable final label.

### 4. Frozen golden/mutation fixture artifacts are not named — REVISE

The acceptance text requires exact station mappings, DST/local-day/window golden outputs, mutations, negative controls, and fail-closed behavior, but the required-artifact list does not identify the immutable fixture inputs and independently computed expected outputs. Add explicit versioned artifact references for at least:

- KDAL/KDFW/KHOU/NYC/Chicago and alias/conflict/missing mapping fixtures;
- spring-forward, fall-back, UTC crossover, leap/day-boundary, and station-window timestamp fixtures;
- bucket endpoint/equality, non-exhaustive/overlap, duplicate/overlap, late-source/post-cutoff, revised/conflicting-label, label-isolation, and holdout second-read/ACL mutation fixtures;
- independently computed expected outputs and their hashes.

State that production-path tests compare byte-stable outputs to these frozen expected artifacts and that fixture runs can only yield `INSUFFICIENT_SAMPLE` or `BLOCKED`, never edge `PASS`.

## Corrections verified as incorporated

- Independent validation/data-lineage ownership is explicit: Gerri Kellman and Bertram Gilfoyle.
- Holdout custodian, future chronological boundary, access control, one read, access logging, and reseal-after-change policy are explicit.
- Primary endpoint is exact HIGH/LOW bucket Brier score versus matched station/season climatology; secondary metrics are enumerated.
- Build owner is unassigned pending QC; build handoff and promotion remain blocked.
- Canonical identity includes decision policy and decision timestamp/window semantics.
- Repeated snapshots are diagnostic and clustered/purged; duplicate rows are blocked and retained with reason codes; overlap groups are explicit.
- Separate market and source manifest inventories include availability/first-known timestamps, cutoff, vintage, revision, and causal eligibility.
- Exhaustive, mutually exclusive, versioned bucket universes and normalized probability vectors are required; finalized labels are attached only during scoring.
- Truth hierarchy, final-label eligibility, source roles, mapping fail-closed behavior, local timezone/day/DST/window fields, and exact status precedence are explicit.
- Acceptance and stop conditions cover negative controls, leakage, availability, canonical hashes, custody, denominator/receipt gaps, and non-edge claim boundaries.
- The decision receipt confirms approval is only for the spec/next independent re-QC gate, not build or promotion authorization.

## Final disposition

**P0.1 specification: REVISE.** Complete the four exact corrections above, then obtain independent technical and governance QC PASS. Until then, implementation/build handoff, Track A/B/C evaluation, forecast-edge claims, promotion, and trading remain **BLOCKED**.
