# Sprint B — Receipt (R3 verification · Signal taxonomy · E7 sigma conversion)

**Date:** 2026-10-04 · **Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Branch:** `main` · **Base:** `8431da3e5`
**Scope:** Sprint B, three parallel items (P1).

---

## Item 1 — R3 verification (three consecutive z8 cycles)

**Verdict: R3 is DONE-EVIDENCED via the live daemon log — with a data-source caveat.**

The task names the `intraday_daemon_cycles` table, but that table is **stale**:
- `data/signal_cache.db::intraday_daemon_cycles` — **161 rows, all dated 2026-09-07**
  (ts range `05:36:02Z → 21:05:14Z`), every status `ok`. No rows since.
- No code path writes that table. Its only consumers are `scripts/intraday_healthcheck.py`
  and `scripts/deadman_switch.py` (reads `MAX(ts_utc)`; the 12h staleness bound is exceeded).

The live proof is in `logs/intraday_daemon.log` (daemon PID 902970, started 2026-10-03
16:32:15Z; the daemon was restarted onto post-M1 code):

| Marker | Count | First | Last | Span | Gaps > 400s |
|---|---:|---|---|---:|---:|
| `Fusion complete` | **306** | 2026-10-03 16:32:15Z | 2026-10-04 17:57:15Z | **25.42 h** | **0** |
| `Calibration complete` | 306 | 2026-10-03 16:32:15Z | 2026-10-04 17:57:15Z | 25.42 h | 0 |

- Longest consecutive run (≤10 min spacing): **306 cycles, both markers**.
- Cadence is ~5 min/cycle → far exceeds the "3 consecutive cycles" bar.

**Caveat (must be stated with the number):** `Calibration complete` did **not** fire with a
nonzero station count until recently. Station-count distribution across the 306 events:
`0 stations → 276`, `1 station → 24`, `3 stations → 6`. The first `3 stations calibrated`
event is **2026-10-04 17:32:15Z** (last 6 cycles). So the *pipeline* completed every cycle,
but *calibration output* was zero-station until the final ~30 min. Per the M4 findings
(`docs/plans/M4-M7-CALIBRATION-FINDINGS-2026-10-03.md`) the zero-station condition is a
fusion-gate starvation, not a crash.

**Recipe for downstream:** if R3 is defined against the table, the table is dead and must be
re-wired (a writer is missing). If R3 is defined against cycle completion, it is satisfied
now (306 cycles ≥ 3, 25.42 h). **Recommendation:** treat R3 as DONE-EVIDENCED on the log, and
file a separate defect for the stale `intraday_daemon_cycles` table.

---

## Item 2 — Signal taxonomy separation — **DONE**, commit `c86683d9e`

Implements `docs/plans/SIGNAL-TAXONOMY-SPEC-2026-10-03.md`.

- `scripts/sweep_engine.py`: added `SIGNAL_CLASSES` (37/37 registry keys classified:
  **27 predictor / 5 modulator / 5 gate**), `signal_class_of()`,
  `count_zero_trade_by_class()`, `VALID_SIGNAL_CLASSES`.
- `scripts/big_sweep.py`: report now splits the bucket — headline is
  **"Predictors with 0 trades"** (actionable); modulators/gates reported separately as
  "wired? never expected to trade". New artifact `data/sweep_taxonomy_split.json`.
- `tests/test_signal_taxonomy.py`: **10 tests** — every registry key has a non-null valid
  class; no modulator/gate can be counted in `predictors_zero_trade`; unknown signals fall to
  `unclassified`; the spec's 10 non-directional signals are pinned.

**Acceptance proven** against the real 2026-09-30 sweep CSV
(`data/sweep_signal_summary.csv`): `predictors_zero_trade == 13` (was 23), matching the
spec's expected 13. No signal module logic changed.

---

## Item 3 — E7 sigma test conversion (95→40) — **BLOCKED (not present in this worktree)**

**Finding: the E7 sigma suite does not exist on `main`.** It lives only on the **unmerged**
branch `temp-sprint-branch`, and the branch version does not run against `main`'s code.

Evidence:
- No file matching `test_sigma*` or the E7 target modules exists on `main`
  (`git ls-tree HEAD tests/ | grep -i sigma` → empty; `grep -rln EntryTimingEngine
  HourlyPipeline ContinuousRecalibrationLoop HourlySignalEvaluator kalshi_price_fetcher
  tests/` → only `test_timing_overlay_gate.py`, unrelated to E7).
- The sigma suites were authored on `temp-sprint-branch`
  (`docs/plans/E9-E10-SIGMA-TESTS-RECEIPT-2026-09-17.md`, commit `026a95265`, recount of the
  E9/E10 files = 32 + 29 = 61 tests; E7 = Σ.1a–Σ.1e, E8 = P0 core tests).
- `temp-sprint-branch` is a large unmerged refactor (77 test files added/modified vs main)
  and is **not an ancestor of `main`** (commit `026a95265` is contained only in
  `temp-sprint-branch`).
- Extracting the 7 sigma files from the branch and running them against `main`'s code
  collects/fails: `test_intraday_pipeline.py` → `ImportError: cannot import name
  '_cloud_cover_fraction'`; `test_intraday_signal_evaluator.py` →
  `TypeError: HourlySignalEvaluator.__init__() got an unexpected keyword argument
  'confidence_map_path'`; plus 8 more signature-level failures. The tests target
  branch-era APIs absent from `main`.

**Why blocked:** the ~95-test suite named by E7 is not in the main worktree, and no active
tree contains an E7-shaped ~95-test suite (largest single suites on main:
`test_signal_evaluation` 57, `test_platt_calibration` 52, `test_architecture_decomposition`
41, `test_structural_hardening`/`test_edge_cases` 40 each). Removing/consolidating to ~40
cannot be done "safely, without deleting tests that cover real behavior" when the source set
is absent.

**Decision needed (Dan/Gilfoyle):**
1. Land `temp-sprint-branch` first (then E7 conversion runs on the real suite), **or**
2. Re-point E7 at a specific `main` suite (name it), **or**
3. Drop E7 from the sprint as a stale register item.

---

## Commits

| Item | Result | Commit |
|---|---|---|
| 1 — R3 verification | DONE-EVIDENCED (log) + stale-table defect flagged | (evidence only — this doc) |
| 2 — Signal taxonomy | DONE + guard test green (10/10) | **`c86683d9e`** |
| 3 — E7 sigma conversion | BLOCKED — suite absent from main | — |

## Verification commands

```bash
# Item 1
grep -c "Fusion complete" logs/intraday_daemon.log          # 306
grep -c "Calibration complete" logs/intraday_daemon.log     # 306
# Item 2
.venv-gefs/bin/python -m pytest tests/test_signal_taxonomy.py -q   # 10 passed
# Item 3
git ls-tree HEAD tests/ | grep -i sigma                     # (empty)
```
