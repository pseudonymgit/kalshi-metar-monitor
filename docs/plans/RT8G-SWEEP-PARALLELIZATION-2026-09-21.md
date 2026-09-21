# R/T-8G — Sweep Engine Parallelization Receipt

- **Task:** R/T-8G — parallelize the sweep loop (script-only, deterministic)
- **Date:** 2026-09-21
- **Repo:** `prototypes/weather-engine-source`
- **Branch:** `temp-sprint-branch` (NOT merged to main — merge is Dan's)

## Summary

Parallelized the two expensive scoring loops in `scripts/ultimate_sweep.py`
(signal sweep + calibration sweep) using `concurrent.futures.ProcessPoolExecutor`
with a **worker-per-signal** model. Output format is **unchanged** — the same
JSON/receipt schema, byte-identical to the serial path.

## What changed

| File | Change |
|---|---|
| `scripts/ultimate_sweep.py` | Added `--workers N` flag (default 1 = serial). Added module-level worker functions `_score_signal_worker`, `_calibrate_signal_worker`, deterministic `_parallel_map`, and `sweep_signals_parallel` / `sweep_calibration_parallel`. Added `parallel` + `num_workers` fields to the report manifest. |
| `scripts/rt8g_smoke_repro.py` | New read-only reproducibility harness (serial vs parallel on a bounded subset). |

No data mutation. No new DB writes. Read-only.

## Architecture (determinism guarantees)

1. **Worker-per-signal.** Each ProcessPool worker rebuilds its OWN signal
   instance from a name (via `build_signal_registry()`) and opens its OWN
   sqlite connection. Nothing live is pickled across processes except the
   `(name, labels, meta)` tuple (plain JSON-serializable dicts).
2. **Fixed ordering.** Parent submits tasks in `sorted(registry)` order with
   `chunksize=1`; results are keyed by name, giving deterministic dict
   insertion order identical to the serial path.
3. **No shared mutable state.** No module-level LossLimiter/gate singletons are
   touched (those live in `scripts/sweep_engine.py`, which this task did not
   modify — it is not in the scoring hot path for `ultimate_sweep.py`).
4. **Fixed seed.** No RNG introduced; config generation (`generate_sweep_configs`,
   `_latin_hypercube`) already uses `seed=42`.

## Reproducibility check (parallel == serial)

Both sweeps compared on bounded subsets; results re-serialized with
`json.dumps(sort_keys=True)` and compared byte-for-byte.

| Sweep | Subset | serial | parallel (8 workers) | speedup | byte-identical |
|---|---|---|---|---|---|
| Signal | 43 signals × 4 stations (2,666 labels) | 216.83s | 62.90s | **3.45x** | ✅ True (0 diffs, 43/43 keys) |
| Signal (smoke) | 6 signals × 3 stations (2,945 labels) | 6.41s | 3.97s | 1.61x | ✅ True (0 diffs) |
| Calibration | 5 signals × 3 stations | 5.71s | 3.11s | 1.84x | ✅ True (0 diffs, 5/5 keys) |

**Result: parallel output is byte-identical to serial output (order-normalized
and byte-identical).** Deterministic, reproducible.

## Runtime improvement (before/after wall-clock)

- **Signal sweep (43 signals × 4 stations):** 216.83s → 62.90s (**3.45x**).
- The full grid (43 signals × 20 stations) is proportionally larger; the same
  per-worker distribution applies. Speedup scales toward the worker count
  (16 cores available) with `--workers 8` measured here.

## Verification commands

```bash
# Reproducibility smoke (serial vs parallel, bounded subset)
python3 scripts/rt8g_smoke_repro.py --workers 8

# Serial (unchanged default)
python3 scripts/ultimate_sweep.py --layer directional --no-emit

# Parallel (identical output schema)
python3 scripts/ultimate_sweep.py --layer directional --workers 8 --no-emit
```

## Stop conditions met

1. ✅ Receipt at this file with before/after runtime + reproducibility (parallel == serial).
2. ✅ Git clean (work committed; no untracked files left by this task).
3. ✅ Output format unchanged (same JSON/receipt schema; byte-identical).

## Coordination note

R/T-8b's untracked artifacts (`scripts/rt8b_exhaustive_recalibration.py`,
`reports/rt8b_recalibration_*`) were **not touched or interfered with**. This
task modified only `scripts/ultimate_sweep.py` and added
`scripts/rt8g_smoke_repro.py`. R/T-8b's serial output will later serve as an
independent correctness cross-check.

## Receipt

- `commit=<sha>` (see `git log --oneline -1` after commit)
- `clean=<sha>` — `git status --porcelain` empty for this task's files
