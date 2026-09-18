# E9 + E10 — Sigma Test Authoring Receipt

**Date:** 2026-09-17
**Branch:** `temp-sprint-branch`
**Author:** Gilfoyle (subagent)
**Status:** COMPLETE — 61 tests passing

## Scope

Sigma test authoring for the trading business-logic layer (E9) and the
calibration + data-layer modules (E10). E7 (sigma test conversion) and E8
(P0 core tests) were already complete as of 2026-09-16.

## Deliverables

| File | Sigma refs | Test count |
|------|-----------|-----------|
| `tests/test_sigma_business_logic.py` | Σ.1f (RiskManager), Σ.1g (StopLossMonitor), Σ.1h (intraday_daemon), Σ.1i (intraday_runner) | 32 |
| `tests/test_sigma_calibration_datalayer.py` | Σ.2a (NWS revision correction), Σ.2b (intraday hourly calibration), Σ.2c (db_connection) | 29 |

**Total: 61 tests** (`pytest` on both files → `61 passed in 0.27s`).

## Sigma taxonomy (this sprint)

- **E7/E8 (done 09-16):** Σ.1a–Σ.1e — EntryTimingEngine, HourlyPipeline,
  ContinuousRecalibrationLoop, HourlySignalEvaluator, kalshi_price_fetcher.
- **E9 (this task):** Σ.1f–Σ.1i — business-logic layer.
- **E10 (this task):** Σ.2a–Σ.2c — calibration + data-layer.

## What is covered

### E9 — business logic (`test_sigma_business_logic.py`)
- **RiskManager** (Σ.1f): default config thresholds, initial pass state,
  profitable trade passes, consecutive-loss halt + win reset, daily-loss
  percentage halt, drawdown-from-peak halt, daily reset on new day,
  `RiskState` metric aggregation, `TradeResult.from_dict`.
- **StopLossMonitor** (Σ.1g): no-stop on winners, primary loss limit,
  day-limit elapsed + disabled flag, drawdown stop, win-rate stop,
  Sharpe stop (rolling 50 with win-rate kept ≥52%), recovery protocol
  (cooldown → paper trades → reactivate), paper-trade failure reset,
  state snapshot, empty drawdown.
- **intraday_daemon** (Σ.1h): PID write/remove, state save, no-op remove,
  `_check_pid` writes fresh PID. The `run()` loop is **not** executed
  (touches DBs/network/sleep).
- **intraday_runner** (Σ.1i): trading-hours boundaries, 20-station universe,
  risk default sanity, current-hour validity, freshness warnings (missing +
  fresh-file cases). `run_intraday_cycle` is **not** executed.

### E10 — calibration + data-layer (`test_sigma_calibration_datalayer.py`)
- **NWS revision correction** (Σ.2a): known-station upward shift, unknown
  station passthrough, upper/lower clamp, case-insensitivity, positive-bias
  invariant, missing-curves → empty dict.
- **Intraday hourly calibration** (Σ.2b): identity fallback, output clamping,
  per-station curve (≥30 samples), insufficient-samples → global, neighbor-hour
  fallback, global monotonicity, `_safe_logit` symmetry, `_safe_expit` clamp +
  midpoint, factory + alias.
- **db_connection** (Σ.2c): registry keys, config lookup + shortcut alias +
  unknown-key raise, path resolution, no-side-effect path lookup, empty debug
  status, absolute-path passthrough, `close_all` no-op, shortcut validity,
  read-only connection against a temp DB (redirected `DATA_DIR`).

## Production code

**Untouched.** `git diff --stat -- core/ scripts/` is empty. No fixture or
import fix was required — all target modules import cleanly and their existing
behavior was pinned as-is.

## Notable production behavior observed (NOT changed)

- `core.intraday.calibration._safe_expit()` raises `OverflowError` for
  `log_odds ≤ ~-745` (e.g. `_safe_expit(-1000.0)`), because `math.exp(-x)`
  overflows before the `np.clip` runs. This is a latent edge-case bug in
  production code. Per write scope ("do not modify production code"), it was
  **not** fixed; the test asserts the documented clamp behavior at
  non-overflowing magnitudes (`±50.0`) instead. Flagging here for a future
  defensive-hardening task if desired.

## Stop conditions honored

- No backtests, no live trading, no daemon restart, no DB writes.
- All DB-touching tests use an isolated temp `DATA_DIR` / temp files.

## Verification

```
$ python3 -m pytest tests/test_sigma_business_logic.py tests/test_sigma_calibration_datalayer.py
61 passed in 0.27s
```
