<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# P0.1 QC — Truth, Rule, Station Manifest and Holdout Custody

**Date:** 2026-08-19  
**Spec reviewed:** `VALIDATION-P0.1-TRUTH-RULE-STATION-HOLDOUT-SPEC-2026-08-19.md` v1.0.0  
**Protected snapshot:** `prototypes/weather-engine-source`  
**Inputs reviewed:** protected snapshot `ROADMAP.md`; `docs/plans/WEATHER-ENGINE-VALIDATION-TRACEABILITY-2026-08-19.jsonl`; Gray Room `technical-qc.md`; Gray Room `governance-qc.md`  
**Scope:** P0.1 specification only. No source/build changes made.

## Executive disposition

**REVISE — build handoff BLOCKED until the corrections below are incorporated and independently re-QC'd.**

The spec has the right boundary and a strong fail-closed direction: exact contract truth is separated from feature/corroboration sources, station/timezone fallbacks are prohibited, availability is treated causally, and a contaminated bounded-sweep holdout is not reused. Those points align with traceability findings F-002–F-006, F-008, E-001/E-002, S-001, and both reviewed QC documents.

It is not yet implementation-ready because several requirements are prose-only or internally incomplete: validation/data-lineage ownership is unassigned; source-manifest schema and availability evidence are absent; HIGH/LOW probability/bucket semantics are not exhaustive; local-day/DST and observation windows lack canonical outputs; status precedence is not representable by the declared row enums; and the receipt/holdout access chain is not machine-testable enough to prove custody or label isolation.

**No P0.1 PASS, and no downstream Track A/B/C or promotion work, should be inferred from this QC.**

## Gate-by-gate QC

| Area | Status | Exact correction required |
|---|---|---|
| Scope and claim boundary | **PASS** | Retain the explicit out-of-scope boundary and the statement that P0.1 PASS proves manifest/custody integrity only, never forecast skill. Add the same non-edge wording to `SPEC-RECEIPT.json`, `QC-RECEIPT.json`, and `BUILD-RECEIPT.json` schemas. |
| Canonical event identity | **REVISE** | Add `decision_ts_utc` (or an explicit decision-window ID) to the canonical identity/manifest, and state whether the key is `(event_id, market_id, target_date_local, settlement_station_id, market_type, decision_policy_version, decision_ts_utc)` or a versioned equivalent. Define a deterministic duplicate rule and a separate overlap/purge rule for repeated snapshots and overlapping target windows; “duplicate or overlapping event identity” is currently too vague. Preserve all rejected rows with reason codes. |
| Manifest schema | **REVISE** | Split required schemas for `market_rule_station_manifest.jsonl` and `source_manifest.jsonl`. The current row schema does not contain the required source availability ledger. Add source ID/type, valid interval, issued/publication timestamp, first-known/available timestamp or upper bound, retrieval timestamp, revision/version, cycle/lead/member, timezone, payload/file hash, and `availability_evidence_ref`. Enforce `first_known_or_available_ts <= feature_cutoff_ts`; `retrieval_ts` and a hash alone must never satisfy this gate. |
| HIGH/LOW and bucket semantics | **REVISE** | Define a versioned, exhaustive, mutually exclusive bucket schema for each market type. Specify integer/decimal degree representation, unit conversion/rounding, unbounded endpoints, equality behavior, and whether HIGH and LOW use the same or distinct bucket IDs. Add a canonical expected probability-vector/label fixture, not only scalar `bucket_lower`/`bucket_upper`; reject missing, overlapping, or non-exhaustive bucket sets. Directional HIGH/LOW movement must not be treated as the contract target. |
| Truth hierarchy and labels | **PASS with required test addition** | The hierarchy is correctly conservative and matches F-003. Add a machine-checkable source-role enum (`CONTRACT_RULE`, `SETTLEMENT_SOURCE`, `CORROBORATION`, `FEATURE`, `RETROSPECTIVE`) and require an immutable raw settlement receipt plus contract-rule payload/hash. Define whether `REVISED` labels are ever eligible; default must be no. Require reconciliation evidence before `CONFLICT` can leave `BLOCKED`. |
| KDAL/KDFW/KHOU and aliases | **REVISE** | Add a frozen mapping fixture with at least KDAL, KDFW, KHOU, NYC, Chicago, and alias/conflict/missing cases. Each expected result must include contract evidence reference, station ID, rule version, and timezone. Acceptance must assert that ticker-only, nearest-city, alias, or default mapping produces `BLOCKED` rather than merely checking a manually populated status. Do not encode a universal city mapping where the contract rule differs by market vintage. |
| Local day, DST, and observation window | **REVISE** | Add `local_day_start_ts_utc`, `local_day_end_ts_utc`, observation-window start/end, DST transition/fold/gap policy, and climate-day rule version to the schema (or an explicitly linked rule artifact). Supply golden cases for spring-forward, fall-back, UTC-date crossover, leap/day boundary, and station-specific settlement windows. Acceptance must compare byte-stable expected timestamps; “represented in the manifest and golden fixture” is insufficient without exact outputs. |
| Source vintage/cutoff | **REVISE** | Make the cutoff field explicit in the manifest (`feature_cutoff_ts_utc` and, if different, `label_cutoff_ts_utc`). Define the admissible evidence for historical availability and a terminal status when it is absent (recommended `BLOCKED`/`NON_CAUSAL`, never silently eligible). Add negative fixtures for post-cutoff publication, late retrieval with no publication proof, revised source, wrong cycle/lead/member, and cached future data. |
| Holdout custody and one-read rule | **REVISE** | Add a custody schema requiring custodian identity, independent validation-owner identity, boundary selection rationale, seal commit/tree hash, seal timestamp, immutable storage reference, manifest hash, ACL/access-log hash, pipeline version, read count, and post-read disposition. Define the one-read operation as a read-only scored artifact with no row-level label/feature exposure to builders; an access log must record actor, purpose, timestamp, artifact, and result. Any holdout-informed change or second read must force `INVALIDATED` and a new seal. A hash without ACL/access evidence remains insufficient. |
| Ownership and receipt chain | **BLOCK** | `validation owner` and `data/lineage owner` are “to be assigned,” and the build owner is deferred. Before any build handoff, name independent validation/custody, data/lineage, and decision owners in the spec receipt; require no builder/tuner to self-certify holdout or label truth. Add parent spec hash, reviewed input hashes, owner/approver IDs, artifact hashes, and predecessor/successor receipt IDs to all four receipt schemas. |
| Status semantics | **REVISE** | The precedence `BLOCKED > FAIL > INSUFFICIENT_SAMPLE > NOT_APPLICABLE > PASS` is good and agrees with technical/governance QC, but `row_status` only permits `ELIGIBLE|BLOCKED|INVALID|MISSING`; it cannot represent the declared terminal statuses. Either add `FAIL|INSUFFICIENT_SAMPLE|NOT_APPLICABLE|PASS` to the typed status enum or clearly separate row validity from package terminal status with two required fields. Define conflict resolution when multiple reasons apply: highest-precedence status wins and all reason codes remain. |
| Acceptance tests | **REVISE** | Add production-path assertions with independently frozen expected outputs, mutation tests, and fail-closed negative cases. Required additions: non-exhaustive/overlapping buckets; equality at every endpoint; wrong station/rule/timezone; DST fold/gap; post-cutoff availability; revised/conflicting settlement; duplicate and overlapping target windows; missing manifest rows; hash canonicalization mutation; holdout second-read/ACL violation; and final-label access from feature construction. A small fixture may prove wiring only and must be forced to `INSUFFICIENT_SAMPLE`/`BLOCKED`, never forecast `PASS`. |
| Stop conditions | **REVISE** | Preserve current stops, but explicitly add: missing owner/custodian; non-byte-stable canonicalization; non-exhaustive bucket universe; absent availability evidence; any label exposure to pre-decision code; failed negative control; missing denominator/omitted invalid rows; holdout access-log/ACL failure; and receipt-chain hash mismatch. State that “PARK/KILL” downstream dispositions do not override P0.1 `BLOCKED`. |
| Roadmap integration | **REVISE** | Protected `ROADMAP.md` still describes historical signal/DEV/trading completion and does not itself show the P0.1 truth-spine gate. The validation remediation and traceability matrix correctly require P0.1 before Track A/B/C. Add an explicit dependency: P0.1 spec → QC → build/QC → confirm → only then Track A; no existing backtest, DEV result, or software-suite pass can satisfy it. |

## Accuracy improvement (required)

**Make exact contract-bucket probability vectors the primary truth test, not merely bucket metadata.** For every eligible event, require an exhaustive vector keyed by the versioned bucket IDs, with the finalized settlement label attached only in the scoring phase. Score against matched persistence/climatology baselines using Brier/log loss/ECE; retain directional HIGH/LOW accuracy as secondary. This directly corrects the known target mismatch (F-002): a model can predict temperature movement while being wrong about the exact Kalshi HIGH/LOW bucket. It also catches adjacent-bucket and overconfidence errors that scalar direction accuracy misses.

## Required acceptance/receipt additions

Before build, the revised spec must freeze:

1. schema versions and canonical JSON serialization/hash scope;
2. market/contract/bucket universe and HIGH/LOW mapping evidence;
3. local-day/DST/window golden outputs;
4. cutoff and availability-evidence policy;
5. duplicate/overlap/purge semantics and retained-row accounting;
6. source-role and label-status eligibility policy;
7. named independent owners/custodian and holdout ACL/access-log procedure;
8. package-vs-row status fields and precedence;
9. production-path fixture, mutation/leakage cases, and expected artifacts;
10. receipt predecessor/successor links and exact owner/approver chain.

## Final decision

- **P0.1 specification:** **REVISE**.
- **P0.1 implementation/build handoff:** **BLOCKED** pending the owner assignment, schema/status corrections, executable custody contract, and golden/negative acceptance tests above.
- **Forecast edge, calibration, execution, paper/live promotion:** **BLOCKED**; outside P0.1 and prohibited by the reviewed governance/technical QC.
- **Source changes:** none made.
