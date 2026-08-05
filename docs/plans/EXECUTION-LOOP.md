# Execution Loop — Critical Fixes, Validation, and Cleanup

**Design principle:** Single coherent loop, processed in order of dependency. Each stage gates the next. If a stage fails, loop stops and reports.

```
┌─────────────────────────────────────────────────────────────┐
│                    EXECUTION LOOP                           │
│                                                             │
│  STAGE 1: FOUNDATION  (no dependencies, can parallelize)    │
│  ├── 1a. Fix fee model (unify across all modules)           │
│  ├── 1b. Fix alert_formatter fee default (1 line)           │
│  ├── 1c. Set KALSHI_PUBLIC_BASE_URL env var                 │
│  ├── 1d. Delete empty GEFS DBs (gefs_operational, reforecast)│
│  ├── 1e. Archive experiment scripts → scripts/archive/      │
│  ├── 1f. Consolidate 3 instance_config → 1 canonical        │
│  └── 1g. Fix 3 ERROR'd crons (luna-price, db-snapshot,      │
│           restore-drill)                                     │
│        ↓ (all pass)                                          │
│                                                             │
│  STAGE 2: CODE CLEANUP  (no dependencies, can parallelize)  │
│  ├── 2a. DB connection context manager migration (60 → 0)   │
│  ├── 2b. Standardize imports (relative-only in core/)       │
│  ├── 2c. Add provenance comments to magic numbers            │
│  ├── 2d. Merge paper_trading*.db → 1 active + 1 backup      │
│  ├── 2e. Merge forecast_disagreement*.db → 1 DB             │
│  ├── 2f. Audit isd_lite_raw.db for METAR overlap            │
│  └── 2g. Add lint CI (ruff, pyflakes, isort)                │
│        ↓ (all pass)                                          │
│                                                             │
│  STAGE 3: DATA BACKFILL  (parallel, long-running)           │
│  ├── 3a. ECDS 2026 gap: 2026-01-10→2026-05-03 (114 dates)  │
│  │     → PREREQ: ECDS 2021-2023 batch completes             │
│  ├── 3b. GEFS Jul 31: investigate source or accept gap      │
│  ├── 3c. 43 extra METAR stations: backfill 2021→today       │
│  ├── 3d. IEM ASOS KNYC expansion: find more 1-min data      │
│  └── 3e. Nowcast 9 stations: complete if not already         │
│        ↓ (data available)                                    │
│                                                             │
│  STAGE 4: SIGNAL CONSOLIDATION  (sequential, depends on 3)  │
│  ├── 4a. Centralized signal registry (30+ modules, manifest) │
│  ├── 4b. Remove hardcoded thresholds → signal_config.py      │
│  ├── 4c. Threading/ concurrency model doc                    │
│  └── 4d. Move all config to config.py (remove duplicates)    │
│        ↓                                                    │
│                                                             │
│  STAGE 5: VALIDATION  (sequential, depends on 1-4)          │
│  ├── 5a. Walk-forward backtest (90+ days, corrected Kelly)  │
│  │     → Gate: fee model fixed, Kalshi API verified          │
│  ├── 5b. Per-station calibration curves (walk-forward)       │
│  │     → Gate: backtest data available                       │
│  ├── 5c. UHI bias correction (ALL cities, nowcast stations)  │
│  │     → Gate: calibration curves exist                      │
│  ├── 5d. Kalshi API price verification (1 live trade)        │
│  │     → Gate: fee model fixed, PEM verified                 │
│  └── 5e. Re-enable paper trading cron                        │
│        ↓ (validation passes ≥60% accuracy)                   │
│                                                             │
│  STAGE 6: REPORTING  (output)                               │
│  ├── 6a. Accuracy CI reporting (add to all output)           │
│  ├── 6b. Station tradability audit (per-station accuracy)    │
│  ├── 6c. Go/No-Go gate status (5 gates, all pass/fail)      │
│  └── 6d. Update master roadmap + app map                     │
└─────────────────────────────────────────────────────────────┘
```

## Execution Order (First Principles)

### Why Stage 1 first?
Fee model bugs affect every trade calculation. KALSHI_PUBLIC_BASE_URL affects every API call. The broken crons waste monitoring attention. These are the highest-leverage fixes — they make everything downstream correct.

### Why Stage 2 next?
DB connections, import style, config consolidation, and lint are infrastructure debt. They don't change results but they prevent the next bugs. High payoff for moderate effort.

### Why Stage 3 in parallel?
Backfills take hours to days. They should run while we do the validation work. No one should wait for a database to fill before fixing the fee model.

### Why Stage 4 after data?
The signal registry and threshold cleanup need to know which signals are actually live. If we're still backfilling ECMWF or GEFS, the signal count is unstable. Consolidate after data stabilizes.

### Why Stage 5 last?
Validation is the most expensive stage (90+ day backtest, per-station calibration). It should only run once, after everything below it is clean. Running it early means re-running when we fix a fee bug.

## Parallelization Strategy

| Stage | Items | Run in parallel? | Estimated time |
|-------|-------|-----------------|----------------|
| 1 | 7 items | ✅ Yes, all independent | ~2 hours |
| 2 | 7 items | ✅ Yes, all independent | ~4 hours |
| 3 | 5 items | ✅ Yes, all independent | ~27 hours (ECDS) |
| 4 | 4 items | ⚠️ Sequential (some overlap) | ~3 hours |
| 5 | 5 items | ⚠️ Sequential (dependencies) | ~4 hours |
| 6 | 4 items | ✅ Yes, all independent | ~1 hour |

**Total wall clock:** ~40 hours (dominated by ECDS backfill in Stage 3)
**Total active work:** ~18 hours (everything else runs in parallel or is waiting)

## What Happens If a Stage Fails

| Stage | Failure mode | Action |
|-------|-------------|--------|
| 1 | Fee model fix breaks something | Roll back, fix, retry |
| 2 | DB migration corrupts data | Restore from backup, retry |
| 3 | ECDS API dies again | Log checkpoint, retry on resume |
| 4 | Signal registry reveals missing module | Add to registry, don't block |
| 5 | Backtest <60% accuracy | Diagnose, fix, retry (don't proceed) |
| 6 | Report generation fails | Manual output, non-blocking |

## Dispatch Bucket

Once you approve this loop, I'll dispatch it as a single Gilfoyle task with the full spec. The subagent will execute each stage in order, reporting progress and surfacing any failures.

## Guardrails

- No AI/ML calls in the trading loop (per Gray Room gate)
- No live money until Stage 5 passes
- No deleting experiment scripts (archive, don't destroy)
- Each stage has a clear pass/fail gate before the next starts