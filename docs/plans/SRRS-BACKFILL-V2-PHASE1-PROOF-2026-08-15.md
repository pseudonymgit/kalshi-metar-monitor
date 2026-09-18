# SRRS backfill v2 Phase 1 proof — 2026-08-15

## Disposition

**Phase 1 PASS for the cached nine-date reclassification; Phase 2 NOT STARTED.** The stale v1 acquisition process was found still running against the v1 root and was terminated at 2026-08-15T08:56:19Z before v2 evidence was frozen. No v2 command made HTTP requests. The v1 root remains preserved as incident evidence and is not used for v2 status decisions.

## V2 artifacts

- Driver: `scripts/backfill_srrs_cli_v2.py`
- Policy: `scripts/fetch_srrs_cli.py::select_lineage`
- V1 source root: `data/raw/srrs_cli_backfill/2026-08-15/`
- V2 proof root: `data/raw/srrs_cli_backfill/2026-08-15-resume-v2-proof-final/`
- Deterministic rerun root: `data/raw/srrs_cli_backfill/2026-08-15-resume-v2-proof-final-rerun/`

The proof copied all 583 cached raw occurrences whose target-date filename falls in 2025-01-15..2025-01-23, including rejected/internal-date-mismatch evidence, and copied cached scan/listing files. It reparsed accepted report bodies and classified them only through v2 lineage.

## Counts

| measure | count |
|---|---:|
| station/date records | 180 |
| unique station/date keys | 180 |
| v2 status `accepted_full_day` | 180 |
| selected full-day reports | 180 |
| retained full-day report occurrences | 184 |
| retained intermediate report occurrences | 260 |
| revision reports | 1 |
| copied raw body occurrences | 583 |
| copied raw bytes | 2,388,367 |
| unique raw bodies | 444 |
| duplicate raw-content occurrences | 139 |
| missing rows | 0 |
| unresolved lineage rows | 0 |
| conflicting full-day rows | 0 |

The v1 provenance status distribution remains visible only as incident evidence: 178 `blocked_ambiguous` and 2 `found` in the frozen nine-date subset. The v2 status distribution is independent: 180 `accepted_full_day`.

## Deterministic and no-loss gates

- Focused lineage tests: `4 passed`.
- Python compilation: passed for fetcher, reclassifier, and v2 driver.
- V2 manifest hash: `69813fc822581c9527527cf236be6a6f6e792773e66f8db4078cedb8ce1ff158`.
- V2 run-summary hash: `0e583c0fba258cc10820a6a31ac183a18422b1a4b6835e1226abbbeea7236309`.
- Deterministic rerun reproduced the same manifest and run-summary hashes and identical counts/raw SHA occurrence map.
- The v2 migration has an explicit raw-occurrence set equality guard and immutable-byte collision guard.
- No calibration, evaluator, production, trading, sweep, or proxy action occurred.

## Phase 2 gate

**STOP / NOT STARTED.** The v2 proof is valid, but the full continuation is not declared complete. A subsequent run must use the v2 driver with a fresh v2 checkpoint and official SRRS-only acquisition for unmaterialized keys. It must account for all 8,200 keys before Phase 3 audit. Never use the v1 status field for completion.
