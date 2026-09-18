# Intraday Path Calibration — Receipt

**Date:** 2026-09-17 UTC
**Author:** Ray Donovan (subagent — script execution)
**Repo:** `prototypes/weather-engine-source`, branch `temp-sprint-branch`
**Type:** SCRIPT EXECUTION / ENTRY-POINT IDENTIFICATION ONLY. No evaluation logic written, modified, or re-derived. No data mutation. No commit. No paper/live trading. No daemon restart.

---

## 0. Verdict: **BLOCKED**

The intraday hourly directional accuracy (h-6 absolute, and h-6 vs h-24 edge)
**cannot be produced against clean settlement ground truth** with the current
repo state, because the canonical intraday measurement entry point exists only
as an **orphaned compiled bytecode artifact** whose source was never committed,
and it is **API-incompatible** with the current committed intraday stack.

No number is fabricated. The precise blocker is stated in §3.

---

## 1. What the task asked

Measure whether the INTRADAY hourly loop (the actual product) clears the
≥58% Gray Room directional gate against the now-clean Kalshi settlement ground
truth, specifically:

- absolute h-6 directional accuracy (and any other lead),
- the h-6 vs h-24 directional edge (pp),
- compare to the ≥58% gate.

---

## 2. Canonical entry-point identification (what exists)

| Candidate | Location | What it actually does | Intraday hourly scoring? |
|---|---|---|---|
| `HourlyBacktest` | `core/intraday/backtest.py` (**only `.pyc` present**) | Hourly backtest & validation framework. `run(station, start, end, market_type, settlement_hour_utc, simulate_market, eval_all_hours)` → `HourlyBacktestResult` with `per_hour_accuracy` (dict keyed by `hour_before_settlement`), `trade_accuracy`, `per_hour_trades`. **This is the canonical h-1..h-24 directional-accuracy scorer.** | ✅ YES (but see §3 blocker) |
| `scripts/split_backtest_hourly.py` | committed | "Split-backtest harness" — scores **daily** direction (settlement_bucket vs prior_settlement_bucket), NOT hourly lead. Name is misleading; no `hour_before_settlement` lead scoring. | ❌ No — daily settlement-vs-prior only |
| `scripts/intraday_runner.py` | committed | Production live runner. Produces per-station calibrated probability + entry decisions for the *current* hour. No backtest/scoring mode. | ❌ No — produces predictions, never scores vs settlement |
| `core/intraday/signal_evaluator.py` | committed | `HourlySignalEvaluator.evaluate_hour()` → returns `(direction, confidence)` for a single hour. No settlement scoring. | ❌ No — signal eval only, no accuracy |
| `scripts/intraday_healthcheck.py` | committed | Daemon/DB/data health monitor (PID, log freshness, DB integrity, NWP recency, signal-fire count). No accuracy. | ❌ No — infra health only |

**Conclusion:** The only entry point that measures *intraday hourly directional
accuracy against settlement* is `core/intraday/backtest.py::HourlyBacktest`. It
exists as `core/intraday/__pycache__/backtest.cpython-311.pyc` (compiled
2026-08-31, embedded source size 24,243 bytes) — but the `.py` source is
**absent from the working tree AND from all git history** (`git log --all --
core/intraday/backtest.py` is empty; `git ls-files core/intraday/` does not list it).

---

## 3. Precise blocker (do-not-fabricate)

### 3a. Source is missing (orphaned `.pyc` only)

- `core/intraday/backtest.py` → **does not exist** (`ls`: No such file).
- `core/intraday/__pycache__/backtest.cpython-311.pyc` → exists (orphaned).
- `git ls-files core/intraday/` lists only: `__init__.py`, `calibration.py`,
  `entry_engine.py`, `exit_logic.py`, `fusion.py`, `pipeline.py`,
  `recalibration_loop.py`, `signal_evaluator.py`. **No `backtest.py`.**
- `git log --all --oneline -- core/intraday/backtest.py` → empty. The source was
  never committed; only a local `.pyc` remains from a prior session.

### 3b. The `.pyc` is API-incompatible with the committed stack

The `.pyc` loads successfully (Python 3.11.2, same interpreter tag) and its
public surface is recoverable:

```
HourlyBacktest.__init__(metar_db='/data/metar_backfill.db',
                        settlements_db='/data/metar_backfill.db',
                        entry_engine=None, fusion_engine=None,
                        calibration=None, recalibration=None)
HourlyBacktest.run(station, start_date, end_date,
                   market_type='HIGH', settlement_hour_utc=18,
                   simulate_market=True, eval_all_hours=True)
HourlyBacktestResult fields: station, date_range, total_hours, total_trades,
    correct_trades, trade_accuracy, total_pnl, sharpe_ratio, max_drawdown,
    profit_factor, per_hour_accuracy, per_hour_trades, per_hour_pnl, entries
HourlyEntry fields: utc_hour, hour_before_settlement, our_probability,
    market_price, divergence, did_trade, trade_direction, position_size,
    actual_settlement, was_correct, pnl
```

`per_hour_accuracy` is exactly the h-1..h-24 directional-accuracy map the task
needs. **But** `_evaluate_day()` calls:

```python
pipeline.build_for_station_date(station, date_str, settlement_hour_utc=settlement_hour_utc)
```

whereas the current committed `core/intraday/pipeline.py` signature is:

```python
def build_for_station_date(self, station: str, date_str: str) -> List[HourlyVector]:
```

— **no `settlement_hour_utc` parameter**. Running the canonical framework
against clean settlement data fails immediately:

```
TypeError: HourlyPipeline.build_for_station_date() got an unexpected keyword
argument 'settlement_hour_utc'
```

The `.pyc` (compiled 2026-08-31) predates a pipeline API change; the two have
drifted. Fixing this would require writing/re-deriving evaluation glue — which
the task explicitly forbids ("Do NOT write new evaluation logic").

### 3c. No committed fallback computes the number

`split_backtest_hourly.py` (committed) scores *daily* settlement-vs-prior
direction, not hourly lead. `intraday_runner.py` (committed) is production-only
with no scoring path. Neither produces h-6 absolute accuracy nor the h-6 vs h-24
edge. There is no committed intraday hourly accuracy scorer.

---

## 4. Commands actually run (exact)

```bash
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source

# (1) Identify entry points
git status --porcelain
git ls-files core/intraday/
git log --all --oneline -- core/intraday/backtest.py   # empty → never committed
ls -la core/intraday/backtest.py                        # No such file

# (2) Confirm the .pyc loads and recover its public surface (read-only, no mutation)
python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location('backtest','core/intraday/__pycache__/backtest.cpython-311.pyc'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print([n for n in dir(m.HourlyBacktest) if not n.startswith('_')])"

# (3) Attempt the canonical hourly backtest against clean settlement (FAILS — see §3b)
python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('backtest', 'core/intraday/__pycache__/backtest.cpython-311.pyc')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
DB = '/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db'
bt = mod.HourlyBacktest(metar_db=DB, settlements_db=DB)
r = bt.run('KNYC', '2026-01-01', '2026-09-17', market_type='HIGH',
           settlement_hour_utc=18, simulate_market=True, eval_all_hours=True)
"
# → TypeError: HourlyPipeline.build_for_station_date() got an unexpected keyword
#   argument 'settlement_hour_utc'
```

---

## 5. Related context (for the record, NOT fabricated for this task)

The daily-anchor calibration (`M-CALIBRATION-EXECUTION-2026-09-17.md`) already
established honest settlement-validated directional skill on the clean data:
aggregate **40.91%**, best signal `metar_trend` **53.14%** over 37,797 trades —
**below** the ≥58% gate. That is a *daily* result, not intraday hourly.

The "+12.1pp directional edge over h-24" referenced in the task description does
**not** appear in the repo as an intraday directional edge. The only "12.1%"
figure found (`docs/weather-engine/accuracy-numbers-cycles4-6.md`) is a
**fee-adjusted return** for the daily ensemble scan (`core/unified_backtest.py`,
cycle 4.5, 2026-08-01), explicitly labeled "PAPER ACCURACY — unconfirmed against
settlement data." No committed artifact documents a settlement-validated
"+12.1pp h-6-over-h-24" intraday directional edge.

---

## 6. Write-scope / side-effects disclosure

| Item | State |
|---|---|
| Evaluation/backtest code | NOT written or modified |
| Source / data (DB) | NOT mutated |
| `.pyc` | read-only (loaded in-process; not altered) |
| This receipt doc | written (untracked `docs/plans/`) |
| Git commit | none |

---

## 7. Unblock conditions (what would clear this to a real number)

1. **Recover/commit `core/intraday/backtest.py` source** (the canonical
   `HourlyBacktest` framework) — or reconstruct it from the `.pyc` + git history
   of `core/intraday/pipeline.py`'s older signature.
2. **Re-sync the API** — restore `build_for_station_date(..., settlement_hour_utc=...)`
   compatibility (or adapt the backtest to the current two-arg signature).
3. Then run `HourlyBacktest.run(..., eval_all_hours=True)` against
   `data/metar_backfill.db` (clean settlement, rewrite confirmed 2026-09-17) and
   read `per_hour_accuracy[6]`, `per_hour_accuracy[24]`, and the h-6-vs-h-24 edge.

None of these are within this task's write scope (script execution only, no new
evaluation logic).

---

Receipt: `commit=n/a` (identification + failed run + doc only; no tracked changes).
