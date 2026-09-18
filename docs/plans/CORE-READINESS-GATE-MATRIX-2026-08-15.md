# Core Readiness Gate Matrix — 2026-08-15

## Scope and safety boundary

Autonomous lane D bounded cleanup only. No calibration was globally enabled, no
sweep was run, and no paper/shadow/live trading or production configuration was
touched. The shared worktree remains dirty; the unattended runner therefore
remains fail-closed.

| Gate | Evidence / result | Disposition |
|---|---|---|
| Calibrated `pressure_delta` reliability | Controlled paired rerun receipt: calibrated arm improved aggregate Brier/ECE but did not improve paired accuracy and reduced coverage. `status=research_only`; global calibration remains disabled. | **PARK** |
| Full-suite collection | `python3 -m pytest --collect-only -q`: 946 tests collected, 9 collection errors. Eight stale import/API paths plus the newly identified SRRS optimized-checkpoint collection error in the full invocation; the SRRS test alone collects 4 tests successfully. | **BLOCKED** |
| Paper-runtime datetime traceback | Removed the function-local `from datetime import datetime, timezone` in `daily_paper_run`'s ISO fallback. This prevents local-name shadowing and the recorded `UnboundLocalError`; no paper run was executed. | **FIXED / syntax-gated** |
| Unattended runner preflight | Runner remains no-execute by default, requires `--execute`, preserves dirty-tree fail-closed policy, and records manifests/receipts. Fixed manifest hashing to include only frozen evaluator inputs (`kalshi_settlements.db`, `metar_backfill.db`) rather than volatile SQLite sidecars and unrelated data artifacts. | **FOCUSED PASS; overall BLOCKED by dirty tree/full suite** |
| Clean execution snapshot | Reconciliation receipt identifies mixed dirty tree, tracked deletions, generated artifacts, and missing path-level approval. Isolated snapshot strategy is specified; no shared-tree staging/reset/clean performed. | **BLOCKED / REVIEW REQUIRED** |

## Focused verification

- `python3 -m py_compile scripts/run_unattended_sweep.py scripts/run_sweep.py scripts/sweep/config.py core/paper_trading_engine.py` — **PASS**.
- AST check confirms `daily_paper_run` has no function-local datetime import — **PASS**.
- `pytest -q tests/test_unattended_sweep_runner.py tests/test_pre_sweep_build.py tests/test_qc_check.py` — **18 passed**.
- `pytest --collect-only -q tests/test_srrs_optimized_checkpoint_import.py` — **4 collected**.
- Runner invocation without `--execute` produced **BLOCKED** due current dirty tree; no child sweep launched.

## Known stale full-suite collection paths

`scripts/single_combo_test.py`, `scripts/test_alert_builder.py`,
`scripts/test_fused_ensemble.py`, `scripts/test_polymarket_whale.py`,
`tests/integration_test.py`, `tests/test_p3_prediction.py`,
`tests/test_position_sizing.py`, and `tests/test_signals.py` retain imports/API
expectations for retired or renamed modules. These were not recreated or
silently excluded; they require a separately approved inventory/retirement
pass. The full invocation also reports `tests/test_srrs_optimized_checkpoint_import.py`
as a collection error despite its isolated collection passing, so that
environment-sensitive discrepancy remains unresolved.

## Next safe action

Review and approve an isolated clean execution snapshot manifest, then rerun the
runner's normal preflight there. Do not weaken dirty-state gates, enable
calibration globally, execute a sweep, or enter paper/live trading from this
lane.
