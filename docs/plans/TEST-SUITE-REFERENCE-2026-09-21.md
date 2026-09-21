# Test Suite Reference — `scripts/ultimate_sweep.py`

**Author:** Donna Paulsen (Chief of Staff)
**Date:** 2026-09-21
**Supersedes:** `ULTIMATE-SUITE-2026-09-18.md` (stale: referenced `ultimate_suite.py`, the old 1,537-label truth set, and predates parallelization + the connected fusion).
**Entry point:** `scripts/ultimate_sweep.py` (canonical harness, R/T-1)
**Type:** READ-ONLY. No signal re-enable/wire-in/revival. Dan holds wire-in (R/T-5).

---

## What it is

One deterministic script that sweeps **everything** against the **correct ground truth** (strike-crossing axis). It is the single source of pass/fail + MCC + accuracy + Brier/ECE + Sharpe for every signal, lane, gate, fusion mode, calibration mode, and reusable experiment function. R/T-2..4 consume its output; R/R review + Dan's wire-in depend on it.

It **reuses** existing code as core (does not rebuild):
- `core/settlement_outcome.py` — `strike_crossing_direction` (single source of truth for the label)
- `core/unified_backtest.py` — Brier/ECE/Sharpe/drawdown helpers
- `core/calibrate_all_signals.py` — per-signal calibration driver
- `scripts/sweep_engine.py` — signal registry + lane/gate enumeration

---

## Ground truth (the axis everything is scored on)

- **Correct axis:** `settlement_temp_f > strike_price` (strike-crossing), NOT the old `settlement_bucket > prior_settlement_bucket` (day-over-day).
- **Real strikes:** 14,305 rows (10,139 greater + 4,112 less + 54 between) via `/historical` backfill, persisted in `settlement_truth.strike_price`. Usable labels ~14,020.
- **Date-tiered temp source ladder:** GHCN → Kalshi finalized → WeatherAPI.
- **Contemporaneous-only (no lookahead):** NWP gated via `core/nwp_lookahead_gate`, GHCN `date <= target_date`, ERA5 removed from training.
- **Honest metric = MCC, not accuracy.** The label set is market-type-conditional (HIGH resolves "up" ~10%, LOW ~95%), so raw accuracy is base-rate-inflated. Every number carries axis + provenance.

---

## The ten test layers (Dan's explicit list)

| # | Layer | What it sweeps | Underlying sweep |
|---|---|---|---|
| 1 | `directional` | signal direction vs strike-crossing label (MCC + acc + Sharpe) | `signal` |
| 2 | `intraday` | intraday signal surfaces (frontal_passage_intraday, spike_reversion, goldilocks, fogr) | `signal` |
| 3 | `predictions` | probability quality — Brier + ECE per signal | `calibration` |
| 4 | `trajectory` | L4 Kalman / trajectory lane + gate | `lane_gate` |
| 5 | `fusion` | majority / weighted / UWC cascade modes | `lane_gate` |
| 6 | `calibration` | Platt / BMA / EMOS + per-signal fit | `calibration` |
| 7 | `entry_timing` | spread / settlement-window / execution gates | `lane_gate` |
| 8 | `exit_logic` | stop-loss / loss-limiter / drawdown gates | `lane_gate` |
| 9 | `risk_sizing` | Kelly / variance-weighted / continuous-Kelly | `lane_gate` |
| 10 | `crossfeed` | cross-model divergence / agreement consumers | `lane_gate` |
| — | `experiment` | reusable experiment functions (DTW, Gaussian fusion, Kalman, trajectory replay, NWP-analog kNN) | `experiment` |

Layer → sweep mapping is explicit in `LAYER_SWEEPS`. `--layer` restricts to any subset.

---

## What it enumerates

- **Signals:** every registered signal — active + killed + recovered + orphaned (~43 names), built via `build_signal_registry()` with explicit `import_status` tracking (`ok` / `missing_cls` / `instantiate_failed` / `missing_func`). A signal that fails to instantiate is recorded as a status, not silently dropped.
- **Lanes + gates:** all 6 lanes, 9 gates, 4 fusion modes, 4 calibration modes — enumerated + import-smoke-tested as `available`/`unavailable`.
- **Experiments:** all reusable experiment functions resolve.
- **Calibration:** Platt / BMA / EMOS + per-signal fit, per-target.

---

## The four sweep functions

| Function | What it scores |
|---|---|
| `sweep_signals` / `sweep_signals_parallel` | per-signal direction vs strike-crossing label → acc, MCC, Sharpe, n |
| `sweep_lanes_gates` | lane/gate/fusion/calibration-mode enumeration + reachability |
| `sweep_calibration` / `sweep_calibration_parallel` | per-signal Brier + ECE (prediction quality) |
| `sweep_experiments` | reusable experiment-function resolution |

---

## Parallelization (R/T-8g, 9/21)

- `--workers N` flag (default 1 = serial). Worker-per-signal `ProcessPoolExecutor`.
- **Deterministic:** tasks submitted in `sorted(registry)` order, `chunksize=1`, results keyed by name, fixed `seed=42` in config generation. No RNG in the scoring loop.
- **Byte-identical output:** parallel == serial (verified: 3.45x speedup, 0 diffs across 43/43 keys). Output schema unchanged.

---

## Usage

```bash
python3 scripts/ultimate_sweep.py                         # full suite (all layers)
python3 scripts/ultimate_sweep.py --dry-run               # enumerate only, no scoring
python3 scripts/ultimate_sweep.py --layer directional     # restrict to layer(s)
python3 scripts/ultimate_sweep.py --workers 8             # parallel (worker-per-signal)
python3 scripts/ultimate_sweep.py --no-emit               # don't write report files
```

Outputs: `reports/ultimate_sweep_<ts>.json` (structured manifest) + `reports/ultimate_sweep_<ts>.md` (markdown).

---

## Boundaries

- ✅ READ-ONLY — no signal re-enable/re-register/taxonomy-flip/wire-in.
- ✅ No DB mutation.
- ✅ Deterministic — fixed ordering, no RNG in scoring loop.
- ✅ Git-clean — no untracked artifacts left behind.
- ✅ Correct axis throughout — `core/settlement_outcome` is the single label seam.

---

## Current known state (9/21)

- **Directional verdict:** connected LIOP fusion has NO genuine directional edge (global MCC −0.0165). Every slice (station/regime/season/up-down/pairs) tracks the base rate.
- **UWC is degenerate** (predicts "up" 100% by construction) — latent pool-of-pools beta-routing bug, not the default (`liop` is).
- **Frame note:** the suite scores *directional* skill (MCC vs null). A separate *forecast-accuracy* pass (MAE / RMSE / Brier / skill-vs-climatology) is the weather-reframe follow-on — not yet run by this harness.
