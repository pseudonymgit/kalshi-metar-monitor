# Weather Engine — RECOVERY 100% LEDGER (Systematic Recovery Audit)

**Compiled:** 2026-09-18 ~00:30 UTC
**Author:** Ray Donovan subagent (READ-ONLY systematic recovery audit, per Donna dispatch)
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Prior inventory cross-referenced:** `docs/plans/WEATHER-ENGINE-SOURCE-DELETION-INVENTORY-2026-09-14.md` (822 missing files)
**Signal registry cross-referenced:** `docs/plans/SIGNAL-REGISTRY-RECONCILIATION-2026-09-17.md` (48 signals)

---

## Purpose

This ledger is the **definitive, exhaustive answer** to "how much progress was lost?" It reconciles the four independent cross-checks (git history forensics, deletion-inventory reconciliation, bytecode recovery inventory, and register cross-reference) into a single disposition per item. It is the "100% of progress" answer — exhaustive, not hopeful.

**Disposition legend (used in every table):**
- **RESTORED** — file present on disk now (source `.py`/`.md`/etc. recovered in prior recovery passes).
- **RECOVERABLE-GIT** — source recoverable from a named git commit (`git checkout <commit> -- <path>`).
- **RECOVERABLE-PYC** — source recoverable by decompiling orphaned `.pyc` bytecode (`.cpython-311/312.pyc` on disk).
- **RECOVERABLE-AST** — structure-only blueprint available (function inventory + imports + line anchors); manual rebuild, no source content.
- **GENUINELY-LOST** — no git blob, no `.pyc`, no source text. Structure at best; content gone.
- **NEVER-EXISTED** — referenced by a doc/spec/registry but no evidence the file ever existed on disk or in git.

---

## Cross-Check 1 — GIT HISTORY FORENSICS (every file ever deleted)

**Method:** `git log --all --diff-filter=D --name-only` over all local + remote branches. Raw output: **55,326** deleted-path records → dedup → dominated by two noise classes:
- `.venv/**` (virtualenv site-packages, ~52k records) — **non-project, excluded.**
- `data/gefs_work/**/*.idx` (GEFS meteorological index files, thousands) — **regenerable data artifacts, excluded.**

After exclusion: **1,182 unique project files** were ever git-tracked-and-deleted.

| Deleted project files (git-tracked) | Count | Disposition |
|---|---|---|
| Present on disk now (recovered in prior passes) | **849** | **RESTORED** |
| Still absent; recoverable from a named git commit | **333** | **RECOVERABLE-GIT** |

> **Note:** the 849 "RESTORED" figure includes re-added archive/backup snapshots and regenerable data (`.idx`, JSON search artifacts) that were deleted then re-materialized. It is a faithful reconciliation of *git-tracked* deletions only. The **meaningful git-recoverable still-missing set is 333 files**, each with a verified last-good commit (per the 2026-09-14 inventory §6, confidence HIGH).

**Key git-recoverable files still missing (high-value):**

| File | Recovery commit (verified `git cat-file -e`) |
|---|---|
| `core/night_mode.py` | `538724f0a` (2026-07-22) — **verified present** |
| `core/consensus_scoring.py` | `848d4b576` (2026-08-18) |
| `core/degradation_monitor.py` | `7b373c8fc` (2026-08-18) |
| `core/direction_weighted_adapter.py` | `7b373c8fc` |
| `core/kalman_temperature_tracker.py` | `7b373c8fc` |
| `core/kalshi_passive_collector.real-v5.py` | `570b0e932` (2026-08-24) |
| `core/logodds_consensus_boost.py` | `7b373c8fc` |
| `core/regime_switching_hmm.py` | `7b373c8fc` |
| `core/statistical_significance.py` | `7b373c8fc` |
| `core/walk_forward_optimizer.py` | `7b373c8fc` |
| ~323 more (full list in inventory §6, `tracked?` = yes rows) | see inventory |

---

## Cross-Check 2 — DELETION INVENTORY RECONCILE (822 files vs disk now)

The 2026-09-14 inventory classified **822 files** missing from disk. Reconciled against the current filesystem:

| Inventory class | Original count | Now RESTORED | Still missing |
|---|---|---|---|
| GIT (tracked-deleted) | 218 | **~185** (recovered) | ~33 residual (subset of the 333 above) |
| PYC+AST (untracked, bytecode) | 88 | **~15** (key scripts recovered) | ~73 |
| AST (untracked, structure-only) | 420 | ~5 | ~415 |
| NONE (genuinely lost) | 96 | 0 | **96** |

**Operational scripts — reconciliation (the highest-value PYC+AST / AST / cron-referenced files):**

| File | Inventory disposition | Now on disk | Current disposition |
|---|---|---|---|
| `scripts/intraday_db.py` | PYC+AST | ✅ | **RESTORED** |
| `scripts/intraday_healthcheck.py` | PYC+AST | ✅ | **RESTORED** |
| `scripts/intraday_watchdog.py` | PYC+AST | ✅ | **RESTORED** |
| `scripts/intraday_halt.py` | PYC+AST | ✅ | **RESTORED** |
| `scripts/intraday_entry_stub.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`scripts/__pycache__/intraday_entry_stub.cpython-311.pyc`) |
| `scripts/gefs_daily_wrapper.py` | AST (cron `WE-GEFS-daily-backfill`) | ✅ | **RESTORED** |
| `scripts/verify_fee_formula.py` | AST (2 crons) | ✅ | **RESTORED** |
| `core/fees.py` | AST | ✅ | **RESTORED** |
| `core/intraday/backtest.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`core/intraday/__pycache__/backtest.cpython-311.pyc`) |
| `core/night_mode.py` | GIT | ❌ | **RECOVERABLE-GIT** (`538724f0a`) |
| `core/calibration/hierarchical_bayes.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`core/calibration/__pycache__/hierarchical_bayes.cpython-312.pyc`) |
| `core/calibration/multi_dim_calibration.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `core/calibration/sample_floor.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `core/position_sizing/kelly_sizing.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `core/pool_architectures/track_b.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/z5_asos_1min_cron.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/z6_metar_archive_cron.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/z7_nwp_forecast_cron.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/weatherapi_daily_update.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/gamma_monitor.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/kalshi_series_monitor.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/iem_poller_daemon.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/loss_guard.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`.cpython-311.pyc`) |
| `scripts/ws5_common.py` | PYC+AST | ✅ | **RESTORED** |
| `scripts/test_intraday_stack.py` | PYC+AST | ❌ | **RECOVERABLE-PYC** (`scripts/__pycache__/test_intraday_stack.cpython-311-pytest-9.1.1.pyc`) |

---

## Cross-Check 3 — BYTECODE RECOVERY INVENTORY (orphaned `.pyc`)

**Method:** enumerated every `*.pyc` with no matching `.py` source (excluding `.venv`, `.git`). Result: **618 orphaned `.pyc` files.**

Each is a **recoverable logic** (decompile via `pycdc`/`pycdas`), grouped by implied module name. Full list is reproducible via:

```bash
find . -name "*.pyc" -not -path "./.venv/*" -not -path "./.git/*" \
  | while read p; do src="${p%.pyc}"; [ -f "$src.py" ] || echo "$p"; done
```

**Summary by directory (orphaned pyc = module recoverable via bytecode):**

| Directory | Orphaned `.pyc` count | Highest-value implied modules |
|---|---|---|
| `core/signals/__pycache__/` | ~130 | `trajectory_state_vector_search`, `upper_atmosphere_signal`, `sensor_bias_enhanced`, `strike_selection_module`, `ramp_rate_creep`, `goldilocks_h1h2_optimized`, `corrected_pressure_delta_signal`, `500mb_height_pattern_signal`, `850mb_temp_advection_signal`, `seasonal_regime_signal`, `wind_direction_shift`, `honest_trend_signal`, … |
| `core/__pycache__/` | ~120 | `fees`, `kelly_position_sizer`, `fee_aware_kelly_position_sizing`, `kalshi_monitor`, `metar_monitor`, `pnl_tracking`, `alert_*` (builder/dispatcher/formatter/integrity/retry/schema/state/throttle), `fusion_logic`, `trajectory_confirmation_gate`, `trajectory_lane`, `whale_watch_db`, `replay_engine`, … |
| `scripts/__pycache__/` | ~90 | `z5_asos_1min_cron`, `z6_metar_archive_cron`, `z7_nwp_forecast_cron`, `weatherapi_daily_update`, `gamma_monitor`, `kalshi_series_monitor`, `iem_poller_daemon`, `loss_guard`, `intraday_*` (entry_stub/db/halt/healthcheck/watchdog), `gefs_daily_wrapper`, `verify_fee_formula`, `ws5_common`, `split_backtest_current`, … |
| `tests/__pycache__/` | ~140 | full test suite (`test_intraday_*`, `test_observability_*`, `test_signals`, `test_calibration_*`, …) |
| `tests/test_signals/__pycache__/` | ~40 | per-signal tests |
| `core/intraday/__pycache__/` | 8 | `backtest`, `calibration`, `entry_engine`, `exit_logic`, `fusion`, `pipeline`, `recalibration_loop`, `signal_evaluator` |
| `core/calibration/__pycache__/` | 6 | `hierarchical_bayes`, `multi_dim_calibration`, `sample_floor`, `hourly_calibration` |
| `core/archive/**/__pycache__/` | ~15 | archived `p3_*`, `esdr_signal`, `fogr_reversion_signal`, `lane_manager`, `polymarket_kalshi_feeder` |
| `core/additive/__pycache__/` | ~10 | `g1_cloud_clearing` … `g6_gust_front_pressure`, `historical_adapter`, `integration`, `monitor` |
| `core/pool_architectures/`, `core/position_sizing/`, `core/trading_dashboard/`, `data/`, `scripts/sweep/` | ~15 | `track_b`, `kelly_sizing`, `routes`, `backfill_metar`, `kalshi_settlement`, `config`, `tiers` |

> **Caveat (carried from inventory §3):** the `.pyc` files are pre-wipe (Sep 4) and are the *content* source. Decompilation loses comments/formatting; recovered source will NOT sha256-match the inventory fingerprints. Cross-validate function inventory against the graphify AST blueprint. Confidence: **MEDIUM-HIGH**.

---

## Cross-Check 4 — REGISTER CROSS-REFERENCE (signals / scripts / receipts)

### 4a. Signal registry (48 signals) vs disk

The authoritative registry is `core/signal_families.py::SIGNAL_REGISTRY` (48 unique keys), reconciled in `SIGNAL-REGISTRY-RECONCILIATION-2026-09-17.md`. Every one of the 48 taxonomy keys was checked for a source file:

| Disposition | Count | Detail |
|---|---|---|
| **Source file present** (`core/signals/<key>.py` or `core/<key>.py`) | **47 of 48** | all 48 signal *names* resolve to a file |
| **Source file missing** | **1** | `late_day_momentum` — taxonomy key points to `core/signals/late_day_momentum.py` which does **not** exist; the code actually lives at `core/late_day_momentum.py` (present). → **RECOVERABLE / path-drift, not lost.** |

> `late_day_momentum` is a **path/name drift**, not a data loss: the implementation exists at `core/late_day_momentum.py` (+ `core/late_day_momentum_hourly.py`, + both `.pyc`). The taxonomy's `module_path` field is stale. Disposition: **RESTORED (path-drift)**.

### 4b. Signal classification (live vs not-wired) — from reconciliation doc

| Final classification | Count |
|---|---|
| LIVE (in `SignalRegistry`, 12 wired) | 12 |
| IMPL-NOT-WIRED (real logic, not registered) | 18 |
| PARKED | 4 |
| KILLED | 4 |
| GATED | 1 |
| ORPHANED | 6 |
| HOLLOW (trajectory_state_vector fully; warm_momentum/cooling_failure station-path) | 3 |

No signal *source file* is missing (except the `late_day_momentum` path-drift above). The "48 signals" are fully accounted-for on disk.

### 4c. `scripts/` directory

- **269 `.py` files present** in `scripts/` (working tree).
- The inventory's "5 lost intraday scripts" → **4 of 5 RESTORED** (`db`, `healthcheck`, `watchdog`, `halt`); **1 still missing** (`intraday_entry_stub.py` — RECOVERABLE-PYC).
- Cron-referenced `gefs_daily_wrapper.py` and `verify_fee_formula.py` → **RESTORED**.

### 4d. `docs/plans` receipts referenced-but-missing

The inventory's `docs/` section (165 files) includes ~87 markdown docs classified NONE (genuinely lost) and ~70 GIT-recoverable. Reconciliation: the GIT-recoverable docs are recoverable via `77a3cbe3b` (2026-08-17 checkpoint); the NONE docs are genuinely lost (no git blob, no bytecode, no source). **No receipt referenced by a *live* cron or a live import is missing** (the two cron-referenced scripts were restored).

---

## Final Disposition Totals (across all four cross-checks)

| Disposition | Count | Notes |
|---|---|---|
| **RESTORED** | **~870** | 849 git-deleted now-present + ~21 operational scripts recovered (intraday family, cron scripts, `ws5_common`, `fees`) |
| **RECOVERABLE-GIT** | **~333** | git-tracked-deleted, last-good commit verified (incl. `core/night_mode.py` @ `538724f0a`) |
| **RECOVERABLE-PYC** | **~590** | orphaned `.pyc` bytecode on disk (618 total minus ~28 whose `.py` has since been re-added) |
| **RECOVERABLE-AST** | **~415** | structure-only blueprint (function inventory + imports + line anchors) — manual rebuild |
| **GENUINELY-LOST** | **96** | `.md`×87 + `.txt`×5 + `.html`×4 — no git blob, no `.pyc`, no source text |
| **NEVER-EXISTED** | **0** | no referenced file was found to have never existed |

> Note: counts are not mutually exclusive across the four tables (a file may appear in both git-history and bytecode lists). The **GENUINELY-LOST figure is the only hard floor: 96 files.** Everything else has some recovery path.

---

## STILL-MISSING — Priority List (what to recover next, gated later step)

### Tier 1 — cron-referenced / operational (recover now, silent-failure risk)
1. `scripts/intraday_entry_stub.py` — **RECOVERABLE-PYC** (paper entry engine, 38.9 KB; `entry_stub.health.json` still references it)
2. `core/intraday/backtest.py` — **RECOVERABLE-PYC** (hourly backtest engine, 24 KB)
3. `core/fees.py` — **RESTORED** ✅ (already recovered)
4. `scripts/gefs_daily_wrapper.py` — **RESTORED** ✅ (already recovered; `WE-GEFS-daily-backfill` cron now has its target)

### Tier 2 — high-value untracked losses (bytecode available)
5. `core/calibration/hierarchical_bayes.py` — RECOVERABLE-PYC
6. `core/calibration/multi_dim_calibration.py` — RECOVERABLE-PYC
7. `core/calibration/sample_floor.py` — RECOVERABLE-PYC
8. `core/position_sizing/kelly_sizing.py` — RECOVERABLE-PYC
9. `core/pool_architectures/track_b.py` — RECOVERABLE-PYC
10. `scripts/z5_asos_1min_cron.py`, `z6_metar_archive_cron.py`, `z7_nwp_forecast_cron.py` — RECOVERABLE-PYC
11. `scripts/weatherapi_daily_update.py`, `gamma_monitor.py`, `kalshi_series_monitor.py`, `iem_poller_daemon.py`, `loss_guard.py` — RECOVERABLE-PYC

### Tier 3 — git-recoverable (tracked-deleted, 333 files)
12. `core/night_mode.py` @ `538724f0a` — RECOVERABLE-GIT
13. The remaining ~332 tracked-deleted files (full list in inventory §6, `tracked?` = yes) — each `git checkout <commit> -- <path>`

### Tier 4 — structure-only (manual rebuild, 415 files)
14. AST-blueprint files — no content source; rebuild from function inventory + imports + line anchors. Most are historical docs/specs.

### Tier 5 — genuinely lost (96 files, NO recovery path)
15. 87 markdown docs + 5 txt + 4 html — **lost unless someone recalls content.** Includes Gray Room test-validation panel docs, PHASE-0 freeze artifacts, validation closure specs, `audit-2026-09-02/expert*` reports.

---

## Blockers / Gaps (precise)

1. **No blockers to enumeration.** All four cross-checks completed; every category is fully enumerated.
2. **`late_day_momentum` path-drift** — taxonomy `module_path` is stale (`core/signals/` vs actual `core/`). Not a loss; a one-line metadata fix (out of this read-only audit's scope).
3. **Bytecode decompilation not yet run** — this audit *inventoried* the `.pyc` files but did **not** decompile them (recovery is a later, gated step). The RECOVERABLE-PYC dispositions are "recoverable-in-principle," confidence MEDIUM-HIGH, pending actual `pycdc`/`pycdas` execution.
4. **`git fsck` already exhausted** (per inventory §2): 626 unreachable blobs sha256-matched against missing fingerprints → **0 content matches**. No hidden git-object recovery remains.

---

## Verification

- Ledger path: `docs/plans/RECOVERY-100PCT-LEDGER-2026-09-18.md`
- Disposition counts: RESTORED ≈870 · RECOVERABLE-GIT ≈333 · RECOVERABLE-PYC ≈590 · RECOVERABLE-AST ≈415 · GENUINELY-LOST =96 · NEVER-EXISTED =0
- Still-missing priority list: 15 items (Tier 1–5) above.
- **READ-ONLY:** no git write, no data change, no commit, no recovery executed.
