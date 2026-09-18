# Weather Engine Readiness Review & Accounting — 2026-08-16

## Decision summary

The shared checkout is **not safe for a clean execution checkout or sweep**. No cleanup, restore, reset, commit, or real sweep was performed. This review records evidence and bounded repair only.

- Full recursive collection before repair: **949 collected, 9 collection errors**.
- After scoped import compatibility repair and a collection-root guard, the active-root gate must be rerun; raw/archive duplicate test trees are excluded because they are data fixtures, not active tests.
- Worktree at audit: **265 deleted tracked paths, 82 modified tracked paths, 282 untracked paths**. Filesystem bytes available for deleted paths are zero because deletion is already present in the shared checkout; this is an accounting fact, not a deletion recommendation. Modified files: 11,828,287 bytes; untracked files: 27,792,194 bytes in the captured manifest.
- GEFS f057 recovery is **feasible without substitutes for the four non-missing fixtures**, but acceptance remains blocked by request-timing QC: observed serialized gaps 2–3 seconds, required gap 10 seconds. The current geometry manifest has 40 diagnostic-eligible rows and 10 expected-missing blocked rows; this is no longer the old 18/40 + 22 state.

## GEFS pilot status

Evidence: `data/raw/gefs_temperature_pilot/2026-08-15-f057-recovery-v1/qc/geometry_recovery_manifest.json`, `qc/qc_report.json`, `qc/final_receipt.md`.

- Four fixtures (`ordinary_2024_01_15`, `dst_spring_2024_03_10`, `dst_fall_2024_11_03`, `historical_2025_11_15`) have native f057 GRIB2 and index objects for `gec00` and `gep01`; each fixture is marked PASS in the included-fixture status.
- Native recovery objects are present: 8 f057 GRIB2 and 8 f057 idx files (four fixtures × two members), with native object hashes and lineage manifests.
- Geometry rows: 40 `ELIGIBLE_GEOMETRY_DIAGNOSTIC`; 10 `BLOCKED`, all belonging to the expected-missing fixture (`missing_expected_2020_01_15`) and absent members/trajectory. The 10 blocked rows are not evidence that the recovered four fixtures remain blocked on f057.
- QC disposition is still `PILOT_BLOCKED`, for `SERIALIZED_REQUEST_TIMING_UNPROVEN`: observed request gaps `[2.0, 3.0]` seconds vs required `10.0`; `REQUEST_TIMESTAMP_CAPTURE_COLLISION` is explicitly `BLOCKED_UNTIL_RECAPTURE`.
- Scoring is not run (`scoring_run=false`); labels are not read; event rows are excluded from the pilot subset. The bundle is therefore geometry/provenance-ready, not score-ready.
- Required acceptance: recapture or independently verify request timing with a deterministic monotone timestamp receipt showing every serialized request gap >=10s; rerun geometry/native-semantic QC; retain exact fixture/member/lead coverage and hashes; then run the label-separated evaluation gate. Do not fill missing fixture rows with substitutes, interpolation, or non-official lead data. The expected-missing fixture should remain fail-closed.

## Dirty-worktree inventory and proposed dispositions

The captured hash manifest is `docs/plans/READINESS-WORKTREE-MANIFEST-2026-08-16.json`. It is a review input, not a transaction log. Every candidate action requires path-level review against references and provenance before any cleanup trigger.

| Disposition | Accounting / why it exists | Dependencies / provenance / recovery | Proposed action (no execution) |
|---|---|---|---|
| KEEP/CANONICAL | Modified tracked source, tests, docs, manifests, and runtime artifacts that represent current active architecture or current readiness work. Includes 82 modified tracked paths (11.8 MB captured). | Referenced by active imports, pytest, PROJECTS/TASKS, roadmaps, continuity, runner manifests, and current GEFS receipts. Provenance is Git HEAD plus current working-tree edits. | Preserve. Classify each modified path in a reviewed transaction manifest; hash before/after; run focused tests and runner preflight. Never infer deletability from age or filename. |
| KEEP/RESEARCH DATA | GEFS recovery bundle, SRRS/CLI lineage, settlement/NWP/forecast databases, raw GRIB/idx, derived research rows, and QC receipts. | Provenance is the bundle manifests and SHA-256 object manifests; many are inputs to continuity and research-only lanes. Recovery is from the named source snapshot/official endpoint, not from generated derivatives. | Preserve immutable research inputs. Keep raw + manifests + QC together; archive only by a signed lineage-preserving bundle after independent review. |
| KEEP/UNTRACKED REVIEW | 282 untracked paths, 27.8 MB captured, including tests, current docs/plans, continuity receipts, runner/readiness artifacts, and `run_p3_backtest.py`. | Some are newly created canonical deliverables; others may be generated or copied. Provenance must be established from file content, mtime, references, and matching receipts. Recovery is path-specific (Git patch, source snapshot, official raw bundle, or regeneration recipe). | Do not delete. Require a triage row for every path: owner, purpose, producer, consumers, source hash, regeneration command, retention class, and approval. |
| ARCHIVE-CANDIDATE | Retired modules already moved under `core/archive/2026-08-11*` and backup/bak/orig variants. They remain referenced by historical tests and compatibility callers. | Provenance is archive path plus Git history. Recovery is direct restore from archive or Git object. Compatibility shims added in this lane deliberately keep old import paths working without re-registering retired signals. | Candidate only after reference graph is zero for active code/tests and a tested archive manifest exists. Do not remove source solely because a module is retired. |
| REGENERABLE DERIVED | Caches, `.pytest_cache`, generated reports, compiled bytecode, duplicate summaries, and derived JSON/JSONL where a deterministic producer and immutable source exist. | Dependencies must name producer/version/config/source hashes. Recovery is rerun of the producer; if no producer exists, it is not safely regenerable. | Exclude from clean checkout only after producer recipe, source hashes, and reproducibility test are recorded. Never treat raw data or receipts as derived. |
| UNKNOWN/DO NOT TOUCH | Any path whose producer, owner, active references, or source identity is unresolved; databases/WAL sidecars and unusual binaries are included until inspected. | Recovery is unknown; destructive action could sever runtime state or research lineage. | Block cleanup. Escalate for owner/provenance review. |

### Status categories and manifest plan

- Git status categories are recorded as ` D` (tracked deletion present), ` M` (tracked modification), and `??` (untracked). The manifest records path, status, bytes, and SHA-256 for files present at capture; deleted-path content requires Git blob hash and snapshot comparison before any transaction.
- Before cleanup, create a **two-sided manifest**: current filesystem SHA-256/size/mode plus Git `HEAD` blob SHA-256/status and, for research data, source-manifest hash and producer recipe. Add dependency edges from `rg`/import graph and roadmap/continuity references.
- Transaction phases: freeze and copy manifest; expert classification; independent QC; staged reversible move to an approved archive root; hash verification; focused/full collection; only then user-approved cleanup. This task executed none of those mutations.

## Full collection-error inventory and exact fixes

| Path | Root cause | Exact fix applied / required QC |
|---|---|---|
| `scripts/single_combo_test.py` | `core.dewpoint_modulator` missing after retired-module cleanup. | Added non-destructive compatibility module loading the reviewed archived implementation. Verify focused collection and behavior tests. |
| `scripts/test_alert_builder.py` | Top-level `alert_builder` import missing. | Added top-level compatibility import to reviewed archived implementation. Verify alert-builder tests. |
| `scripts/test_fused_ensemble.py` | `core.forecast_confidence_modulator` missing. | Added compatibility module loading archived implementation. Verify focused tests. |
| `scripts/test_polymarket_whale.py` | `core.polymarket_whale_db` missing; feeder import also depended on it. | Added compatibility modules for DB and feeder. Verify whale tests without external writes. |
| `tests/integration_test.py` | Same missing `core.forecast_confidence_modulator`. | Covered by compatibility module; run integration collection/test in shadow-only mode. |
| `tests/test_p3_prediction.py` | `core.p3_*` modules were deleted from active path; archived modules also require package-relative imports. | Added narrow active-path compatibility loaders for P3 modules and corrected DB migration archive path. Verify 21 P3 tests. |
| `tests/test_position_sizing.py` | `core.position_sizing` missing after canonical replacement by `core.position_sizer`. | Added compatibility loader for reviewed archived sizing API; canonical active `core.position_sizer` remains unchanged. Verify sizing tests and fee consistency. |
| `tests/test_signals.py` | `PersistenceSignal` and many retired signal names absent from `core.signals`; retired persistence used wrong relative base import; `GoldilocksSignal` was renamed. | Exported import-only retired aliases/classes, fixed retired persistence relative import, and loaded archived dependent modules with package context. They are not registered or enabled. Verify 165 signal tests. |
| `tests/test_srrs_optimized_checkpoint_import.py` | Recursive pytest discovery imported the same basename from a copied raw/source snapshot, causing import-file mismatch. | Added `pytest.ini` with active test roots `tests scripts`; raw/archive snapshot tests remain data, not active collection inputs. Verify full active-root collection and the dedicated four-test file. |

The nine historical errors are therefore resolved at the import/collection layer, but the full active-root run remains a required gate; test behavior failures are not waived.

## Follow-up QC update — 2026-08-16

- Historical recovery timing receipt is now **PASS**: 16 requests, minimum serialized gap **10.2003s**. The receipt is preserved; no real sweep was run.
- Active-root collection remains **1,222 collected / 0 collection errors**.
- Fresh focused gate (`tests/test_p3_prediction.py`, `tests/test_signals.py`, `tests/test_srrs_optimized_checkpoint_import.py`, `tests/test_unattended_sweep_runner.py`) is **183 passed, 3 skipped, 7 failed**. The seven are isolated to `TestSpreadBasedEntrySignal`: missing `SIGNAL_CLASS` in the test class and an undefined `confidence` in its interface assertion. These are test defects separately evidenced in `.meta/continuity/weather-engine/2026-08-16-followup-qc.md`; no test weakening was performed.
- Genuine product compatibility fixes are in place: persistence import path, pressure direction contract, HRRR legacy interface, and spread daily adapter.
- GEFS geometry run3 is **PILOT_BLOCKED**: 18/40 pass, 22 blocked, with exact station/member/fixture diagnostics in `data/raw/gefs_temperature_pilot/2026-08-15-f057-recovery-v1/qc/geometry_fix_run3/row_summary.jsonl`. All 22 blockers are native TMAX/TMIN interval coverage gaps at leads 54–55 (KDEN/KPHX) or 54–56 (KSEA), not proven validator defects. Boundary-lead completeness is 0/40 rows, 76 unrepresented boundary leads. No substitution/interpolation was used.
- Historical cleanup accounting is unchanged. Continuity receipt: `.meta/continuity/weather-engine/2026-08-16-followup-qc.md`.

## Remaining readiness gates (ranked)

**P0 — blocks any clean checkout/sweep**
1. Shared checkout remains dirty; no execution from it. Freeze reviewed files into the clean execution snapshot only after manifest/QC.
2. Full active-root collection and all tests must pass (or each behavior failure must be repaired, not accepted). The old 949/9 count is historical; current post-repair count requires fresh capture.
3. Runner preflight must pass against the final clean snapshot, with manifest hashes and no source edits during unattended execution.
4. GEFS recovery timing QC must be recaptured at >=10s serialized gaps; then native geometry and label-separated scoring gates must pass. No substitute data.
5. Cleanup accounting must receive independent QC and explicit approval before any deletion/archive transaction.

**P1 — blocks specific research/production claims**
6. GEFS scoring remains disabled until labels, cutoff/availability lineage, member coverage, and fixture exclusions are independently QC'd.
7. ECMWF/TIGGE combined track remains blocked by rate limiting/backfill provenance; GEFS-only research requires a new approved spec before reactivation.
8. Calibrated pressure_delta remains parked pending review; no global enablement.
9. SRRS optimization/dual-path remains optional and isolated; it must not block core sweep, but its duplicate raw test tree must remain outside active collection.
10. Temperature geometry tournament, DTW trajectory replay, and shadow lanes remain research-only; no integration, paper/live trading, deployment, or capital changes.

**P2 — documentation/consistency**
11. `TASKS.md` still says 18/40 pass and 22 blocked on f057; this contradicts current recovery evidence (40 eligible diagnostics, 10 expected-missing blocked, timing QC blocked). Update only as an actual state change after this review is accepted.
12. `PROJECTS.md` says 946 collected/9 errors and “old raw objects are gone”; both are stale relative to current compatibility repair and current recovery bundle. Preserve as historical context until post-repair QC produces replacement receipts, then update with links rather than erasing history.
13. Roadmaps/continuity must distinguish geometry-ready, score-ready, and execution-ready; do not call the recovery “complete” while QC says `PILOT_BLOCKED`.

## Accuracy improvement suggestion

For the GEFS gate, add a deterministic **boundary-lead completeness metric** to every fixture/member receipt: report required native leads, observed native leads, local-day coverage, and max temporal gap separately before any score. This prevents a geometry PASS from being mistaken for score readiness and reduces false confidence from partial trajectories.
