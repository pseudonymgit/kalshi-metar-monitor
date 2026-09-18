# Unattended Deterministic Weather-Engine Sweep Runner

**Date:** 2026-08-15  
**Disposition:** implementation specification; real execution is not authorized.

## Purpose and authority

`python3 scripts/run_unattended_sweep.py` is the sole unattended wrapper. Without
`--execute` it performs preflight and parks. A real sweep is one non-interactive
child invocation of the current canonical evaluator:

```text
python3 scripts/run_sweep.py --folds 4 --n-configs 20
```

The wrapper does not tune signals, edit source/configuration, deploy, trade, call
AI/models, or make network requests. Real execution requires separate explicit Dan
go-ahead after a PASS preflight; a PASS is not authorization.

## Preflight gates (all required)

1. Repository root and canonical script exist.
2. Required configuration (`config/sweep-optimal.json`) and frozen input databases
   (`data/kalshi_settlements.db`, `data/metar_backfill.db`) exist.
3. Git HEAD, source/config/data SHA-256 hashes, and complete porcelain status are
   captured. Any dirty path blocks by default. `--allow-dirty-path` is an explicit,
   recorded path-prefix exception; it must never cover evaluator/source/config/data.
4. Station list is non-empty and every configured station is present in the station
   registry. Data/schema checks are delegated to canonical QC and the evaluator's
   integrity gate; missing/invalid data blocks.
5. Exact QC commands run in order (short-circuit on failure):

```text
python3 -m py_compile scripts/run_unattended_sweep.py scripts/run_sweep.py scripts/sweep/config.py
python3 -m pytest -q tests/test_pre_sweep_build.py tests/test_qc_check.py
python3 -m pytest -q
```

The full suite is collection-complete: collection errors, test failures, and
timeouts are BLOCKED. No partial suite is accepted. Imports are exercised by the
suite and station-registry import gate.

## Frozen execution and process policy

On PASS the runner freezes HEAD and all files under `app.py`, `core/`, `scripts/`,
`config/`, and `data/` (excluding bytecode and `.git`). It starts exactly one child,
with `cwd` at repository root, inherited environment, stdin connected to
`DEVNULL`, and bounded timeout. stdout/stderr are redirected to files. No shell,
interactive prompt, subprocess loop, AI/model call, or network operation is used
by the wrapper. The canonical evaluator's own integrity gate remains enabled;
`--no-integrity` is not supplied. A timeout or non-zero child exit blocks.

Git status is recorded before and after. Any unexpected status difference, including
source edits made by the child, blocks. The wrapper never modifies production or
runtime configuration. The existing repository dirty tree is intentionally a
preflight blocker; do not weaken this safety rule.

## Run artifacts

Each run is an immutable timestamped directory under
`.meta/unattended-sweep-runs/<UTC timestamp>/` containing (the run directory is
excluded from its own before/after source-status comparison):

- `preflight.json` — gates, versions, station checks, QC evidence and disposition
- `manifest.json` — canonical command, HEAD, frozen source/config/data hashes
- `stdout.log`, `stderr.log` — complete child streams (empty for preflight-only)
- `result.json` — timing, exit code, timeout, pre/post status and disposition
- `sha256-manifest.json` — hashes of run artifacts
- `continuity-receipt.json` — run identity and authorization boundary

Exit codes: `0` PASS/PARK, `2` blocked preflight or source-change, `3` QC/child
failure, `4` timeout. PASS means the requested child completed successfully and
post-run invariants held; PARK means preflight passed but `--execute` was absent.
BLOCKED means no real sweep occurred unless a running child subsequently violated
an invariant. Failures are fail-closed; no retry or manual intervention occurs.

## Authorization and acceptance

After test-suite acceptance, one silent automated batch may be run only by Dan or
a designated operator, with separate explicit Dan authorization recorded in the
continuity receipt. Any change to source, config, data, canonical command, station
universe, or QC suite requires a new preflight and review. This runner itself must
not be used to authorize trading or production deployment.
