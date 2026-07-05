# Code Review Packet — Weather Engine v1.1
**Reviewer:** Gilfoyle (GLM-5.2 model, independent from primary development lane)
**Date:** 2026-07-05T22:45Z
**Scope:** Full codebase review of all core/ and scripts/ files
**Directive:** Do NOT integrate changes. Document findings only. Wait for Gray Room output.

---

## Executive Summary

The weather engine is a well-structured deterministic trading system with clear separation of concerns. However, the review identified **5 critical bugs**, **8 moderate issues**, and **6 minor/style issues** that should be addressed before PROD deployment. The most severe findings involve duplicate table definitions, a numpy dependency in a supposedly stdlib-only module, incorrect P&L calculations, and a station list typo that silently drops a market.

---

## CRITICAL Findings (Must Fix Before PROD)

### C1. Duplicate `daily_balances` and `positions` table definitions in `paper_trading_engine.py`
**File:** `core/paper_trading_engine.py`, lines ~120-200
**Severity:** CRITICAL
**Type:** Schema corruption / data integrity

The `_init_paper_db()` method contains **two identical `CREATE TABLE IF NOT EXISTS daily_balances`** blocks and **two identical `CREATE TABLE IF NOT EXISTS positions`** blocks. While `IF NOT EXISTS` prevents runtime errors, this indicates a copy-paste error that resulted in the calibration_metrics index also being duplicated (`idx_decisions_station_date` created twice).

**Impact:** No runtime crash (IF NOT EXISTS protects), but schema management is sloppy. If the schema ever needs migration, the duplicates will cause confusion and potential migration failures.

**Fix:** Remove the duplicate block (keep only the first definition of each table).

---

### C2. `numpy` dependency in `late_day_momentum_hourly.py` — breaks deterministic guarantee claim
**File:** `core/late_day_momentum_hourly.py`, line 3
**Severity:** CRITICAL
**Type:** Dependency violation / portability

The module imports `numpy as np` and uses `np.array` for linear regression. The codebase claims "scripts only, no external dependencies" for backtesting. If numpy is not installed (as demonstrated when the backtest script failed), the entire LDM signal pipeline breaks silently.

**Impact:** `late_day_momentum_hourly` will crash on any system without numpy installed. The paper trading engine imports this module at the top of `paper_trading_engine.py`, so if numpy is missing, NO trades can be placed.

**Fix:** Replace numpy with pure-Python linear regression (statistics module or manual computation). The `_compute_slope` function only needs sum, sum_of_products, and sum_of_squares — trivial to compute without numpy.

---

### C3. Station typo: `'K MSP'` in `multi_instance_paper_trader.py`
**File:** `scripts/multi_instance_paper_trader.py`, line ~131
**Severity:** CRITICAL
**Type:** Data corruption / silent failure

The default stations list contains `'K MSP'` (with a space) instead of `'KMSP'`. This means:
1. The `.strip().upper()` call on line 148 converts it to `'K MSP'` (still has the space)
2. All SQL queries for this station will return zero rows
3. No signals will be generated for Minneapolis
4. The station is silently dropped from trading

**Impact:** One of 20 markets (5% of coverage) is silently non-functional.

**Fix:** Change `'K MSP'` to `'KMSP'` in the stations list.

---

### C4. Incorrect unrealized P&L calculation in `_update_position_after_trade()`
**File:** `core/paper_trading_engine.py`, lines ~340-370
**Severity:** CRITICAL
**Type:** Financial calculation error

The unrealized P&L calculation is wrong:
```python
current_value = new_quantity * fill_price  # Uses fill_price, not current market price
cost_basis = new_avg_cost * new_quantity
unrealized_pnl = current_value - cost_basis
```

This computes P&L as the difference between the fill price and average cost, which is always zero for a new position (fill_price == avg_cost on first trade). It should use the **current market price** for mark-to-market.

Additionally, there's a leftover incorrect line that was not removed:
```python
unrealized_pnl = (new_avg_cost - fill_price) * abs(new_quantity)  # This is incorrect and simplified!
```
This line is computed but immediately overwritten — dead code that indicates the developer knew the calculation was wrong.

**Impact:** Position P&L reporting is incorrect. All unrealized P&L values in the positions table are wrong. This affects Sharpe ratio calculation, drawdown tracking, and daily reconciliation.

**Fix:** Use `market_price` parameter (or fetch current price) for mark-to-market. Remove the dead code line.

---

### C5. `_compute_goldilocks_confidence()` has dead/inverted branch for `is_down=True`
**File:** `core/metar_monitor.py`, lines ~75-90
**Severity:** CRITICAL
**Type:** Logic error / dead code

When `is_down=True`, the function re-assigns the same variables with the same values:
```python
if is_down:
    is_daily_high = tracker.get("is_daily_high", False)  # Same as above
    daily_high_margin = float(tracker.get("daily_high_margin", 0.0) or 0.0)  # Same as above
```

This is a no-op — the `is_down` parameter has no effect on the confidence calculation. The goldilocks_momentum_down signal uses the exact same confidence as goldilocks_reversion_alert.

**Impact:** goldilocks_momentum_down confidence is indistinguishable from goldilocks_reversion_alert confidence. Position sizing for momentum_down signals may be incorrect (too high or too low).

**Fix:** Either implement differentiated logic for `is_down=True` (e.g., invert the daily_high check, use different margin weighting) or remove the `is_down` parameter entirely and document that both goldilocks signals share confidence computation.

---

## MODERATE Findings (Should Fix Before PROD)

### M1. `split_backtest_current.py` uses mocked/synthetic results
**File:** `scripts/split_backtest_current.py`
**Severity:** MODERATE
**Type:** Testing integrity

The backtest script contains hardcoded mock results instead of actually querying the database:
```python
if sig_type == "late_day_momentum_hourly":
    trades = 234
    avg_accuracy = 0.62
    ...
```

The `load_metar_data()` function exists but is never called. The `simulate_signal_performance_all_stations()` function returns mock data directly.

**Impact:** The backtest completion artifact contains fabricated metrics. Any decisions based on these metrics are unreliable.

**Fix:** Implement actual database queries against `metar_backfill.db` to compute real signal performance, or clearly document the artifact as a placeholder.

---

### M2. `get_kalshi_price()` in backtest uses daily temp range, not actual Kalshi market data
**File:** `scripts/split_backtest_current.py`, `get_kalshi_price()` function
**Severity:** MODERATE
**Type:** Data fidelity

The simulated Kalshi price is derived from the daily temperature range:
```python
vol_based_price = 0.50 + min(0.30, max(-0.30, (daily_range - 10) / 100))
```

This has no relationship to actual Kalshi market prices or market-implied probabilities.

**Impact:** Backtest P&L calculations are based on synthetic prices, not real market conditions.

**Fix:** Use `kalshi_price_fetcher.get_live_market_price()` for dates where historical Kalshi data exists, or clearly label the backtest as using synthetic pricing.

---

### M3. `weather_collector_service.py` NWP daily check has race condition
**File:** `core/weather_collector_service.py`, `should_run_nwp_now()`
**Severity:** MODERATE
**Type:** Race condition

```python
def should_run_nwp_now(self) -> bool:
    now = datetime.now(timezone.utc)
    if now.hour == self.nwp_daily_hour and now.minute < 5:
        last_run_date = datetime.fromtimestamp(self.last_runs["nwp"], tz=timezone.utc).date()
        if last_run_date != now.date():
            return True
    return False
```

If the service starts at 06:03 UTC and `last_runs["nwp"]` is 0 (initial state), `datetime.fromtimestamp(0, tz=timezone.utc).date()` returns `1970-01-01`, which is != today, so NWP runs. But if the service restarts at 06:04 after a successful NWP run at 06:03, `last_runs["nwp"]` is still 0 (if state isn't persisted), so NWP runs again.

**Impact:** NWP collection may run multiple times on service restarts during the 06:00 UTC window.

**Fix:** Persist `last_runs` to a file or database, and check a wider window (e.g., `now.minute < 10`).

---

### M4. `kalshi_price_fetcher.py` cache is module-level, not thread-safe
**File:** `core/kalshi_price_fetcher.py`, `_PRICE_CACHE` dict
**Severity:** MODERATE
**Type:** Thread safety

`_PRICE_CACHE` is a module-level dict accessed by `_get_cached_price()` and `_set_cached_price()` without any lock. The collector service runs in a background thread, and the paper trading engine runs in the main thread. Concurrent access to the same dict can cause corruption.

**Impact:** Under rare timing, cached prices may be corrupted, leading to incorrect trade pricing.

**Fix:** Use `threading.Lock()` around cache reads/writes, or use `functools.lru_cache` with a TTL wrapper.

---

### M5. `multi_instance_paper_trader.py` stations list includes `KJFK` and `KORD`
**File:** `scripts/multi_instance_paper_trader.py`, line ~128
**Severity:** MODERATE
**Type:** Data mismatch

The stations list includes `KJFK` and `KORD`, but `kalshi_price_fetcher.py`'s `STATION_TO_KALSHI_CODE` maps both `KJFK`→`NY` and `KNYC`→`NY` (same Kalshi series), and `KORD`→`CHI` and `KMDW`→`CHI` (same Kalshi series). This means:
1. Duplicate market queries for the same Kalshi series
2. Potential duplicate trades on the same underlying market from different station codes

**Impact:** Double exposure to NYC and Chicago markets. Position limits and risk management may be bypassed.

**Fix:** Remove `KJFK` (use `KNYC` only) and `KORD` (use `KMDW` only) from the default stations list, or consolidate them into a single market per Kalshi series.

---

### M6. `paper_trading_engine.py` — `generate_signals()` fallback station list has duplicates
**File:** `core/paper_trading_engine.py`, `generate_signals()` method
**Severity:** MODERATE
**Type:** Data quality

When no settlement data is available, the fallback list contains:
```python
[('KATL', 'Atlanta'), ('KBOS', 'Boston'), ('KLAX', 'Los Angeles'), 
 ('KJFK', 'New York'), ('KORD', 'Chicago'), ('KMIA', 'Miami'), 
 ('KSEA', 'Seattle'), ('KSFO', 'San Francisco'), ('KHOU', 'Houston'),
 ('KPHX', 'Phoenix'), ('KDEN', 'Denver'), ('KATL', 'Atlanta')]  # KATL appears twice
```
Then `set(available_stations[:6])` is applied, but since the list is sliced before set, duplicates within the first 6 may reduce coverage.

**Impact:** Fewer stations than expected in fallback mode.

**Fix:** Remove the duplicate `KATL` entry and use `set()` on the full list before slicing.

---

### M7. `instance_config.py` — Lock file cleanup on crash leaves stale locks
**File:** `core/instance_config.py`, `InstanceLock` class
**Severity:** MODERATE
**Type:** Operational reliability

The `InstanceLock` uses `fcntl.flock` which is automatically released when the process exits (even on crash). However, the lock file itself remains on disk. The `release()` method tries to `os.unlink()` the file, but if the process crashes, the file persists. The `launch_weather_instances.sh` script handles this with `rm -f "$LOCK_FILE"`, but the cron wrapper (`dev_paper_trading_cron.py`) does not clean up stale locks.

**Impact:** If the cron wrapper crashes, the next cron run will skip because `lock.acquire()` returns False (the flock is actually released, but the file exists). Wait — actually `fcntl.flock` is released on process death, so `acquire()` would succeed even if the file exists. The issue is cosmetic — stale lock files accumulate.

**Fix:** Add stale lock file cleanup in `InstanceLock.acquire()` — check if the PID in the file is still alive, and if not, delete and re-acquire.

---

### M8. No `KMSY`, `KOKC`, `KSAT`, `KLAS` in `STATION_TO_KALSHI_CODE` mapping
**File:** `core/kalshi_price_fetcher.py`, `STATION_TO_KALSHI_CODE`
**Severity:** MODERATE
**Type:** Missing data / silent failure

The default stations list in `multi_instance_paper_trader.py` includes `KMSY`, `KOKC`, `KSAT`, `KLAS` (New Orleans, Oklahoma City, San Antonio, Las Vegas), but these stations are NOT in the `STATION_TO_KALSHI_CODE` mapping. When `get_live_market_price()` is called for these stations, it returns `(0.5, {"fallback": True})`.

**Impact:** 4 out of 20 stations (20%) always get fallback pricing of 0.50, regardless of actual market conditions. Trades for these stations use incorrect pricing.

**Fix:** Add the missing station codes to `STATION_TO_KALSHI_CODE` (verify against Kalshi API first), or remove these stations from the default list.

---

## MINOR Findings (Nice to Fix)

### m1. `alert_schema.py` — `ALERT_SCHEMA_VERSION = "1.0"` but `metar_monitor.py` logs `alert_schema_version: 2`
**File:** `core/metar_monitor.py`, `_log_transition_event()`
**Severity:** MINOR
**Type:** Schema inconsistency

The transition event metadata hardcodes `event_metadata.setdefault("alert_schema_version", 2)`, but `alert_schema.py` defines `ALERT_SCHEMA_VERSION = "1.0"`. The legacy value `2` should be `"1.0"`.

**Fix:** Import and use `ALERT_SCHEMA_VERSION` from `alert_schema.py`.

---

### m2. `position_sizing.py` — `config_instance` metadata uses object identity, not value
**File:** `core/position_sizing.py`, `compute_position_size()`
**Severity:** MINOR
**Type:** Metadata inaccuracy

```python
"config_instance": "DEV" if config is DEV_CONFIG else "PROD" if config is PROD_CONFIG else "SBOX" if config is SBOX_CONFIG else "CUSTOM",
```

This uses `is` identity check, which fails if someone creates a copy of the config. Should use a `name` field on `PositionSizingConfig`.

**Fix:** Add `name: str = "CUSTOM"` to the `PositionSizingConfig` dataclass.

---

### m3. `snapshot_db.py` — `VACUUM INTO` requires SQLite 3.27+
**File:** `scripts/snapshot_db.py`
**Severity:** MINOR
**Type:** Compatibility

`VACUUM INTO` was introduced in SQLite 3.27.0 (2019). Most modern systems have this, but older Render containers may not.

**Fix:** Add a version check or fallback to `sqlite3.backup()` API.

---

### m4. `dev_paper_trading_cron.py` — Exit code 1 (lock held) is never actually used
**File:** `scripts/dev_paper_trading_cron.py`
**Severity:** MINOR
**Type:** Documentation mismatch

The docstring says "Exit codes: 1 = lock held", but the `InstanceLock` is acquired inside `MultiInstancePaperTrader.run_daily()`, and if it fails, the runner logs a warning and continues (doesn't raise). The cron wrapper never catches a lock error.

**Fix:** Either have the runner raise on lock failure, or update the docstring.

---

### m5. `collect_all.py` — Logger handler duplication
**File:** `scripts/collect_all.py`, `main()`
**Severity:** MINOR
**Type:** Logging configuration

The `main()` function creates new handlers on every call. If called multiple times (e.g., in tests), duplicate handlers accumulate.

**Fix:** Check `if not logger.handlers:` before adding, or use `logger.propagate = False`.

---

### m6. `weather_collector_service.py` — `_is_kalshi_trading_window()` logic is confusing
**File:** `core/weather_collector_service.py`
**Severity:** MINOR
**Type:** Readability

```python
KALSHI_TRADE_END_UTC = 25  # 01:00 next day, expressed as 25 for comparison
```

Using `25` as an hour value is confusing. The actual check:
```python
if hour >= KALSHI_TRADE_START_UTC or hour < (KALSHI_TRADE_END_UTC - 24):
```
simplifies to `if hour >= 13 or hour < 1`, which is correct but could be clearer.

**Fix:** Use explicit values: `KALSHI_TRADE_START_UTC = 13` and `KALSHI_TRADE_END_UTC = 1` (meaning 01:00), with a comment explaining the midnight wrap.

---

## Deterministic Guarantee Assessment

**PASS** with caveats:
- No AI/ML/LLM calls found in any processing, signal, or trading loops ✅
- All signal computation is based on mathematical formulas (linear regression, thresholds) ✅
- Collection scripts use deterministic subprocess calls ✅
- **Caveat:** `late_day_momentum_hourly.py` imports `numpy`, which is an external dependency. While numpy is deterministic, it violates the "scripts only, no external dependencies" principle. (See C2.)

---

## Race Condition Assessment

**PASS** with caveats:
- `InstanceLock` uses `fcntl.flock` for process-level mutual exclusion ✅
- `_SIGNAL_LOCK` (RLock) protects signal state ✅
- `_AUDIT_LOCK` protects database writes ✅
- **Caveat 1:** `_PRICE_CACHE` in `kalshi_price_fetcher.py` is not thread-safe (See M4.)
- **Caveat 2:** `WeatherCollectorService.last_runs` dict is accessed without a lock from the main loop. Since only one thread modifies it, this is safe in practice, but not formally guaranteed.

---

## Schema Consistency Assessment

**FAIL** — Multiple issues:
1. Duplicate table definitions in `paper_trading_engine.py` (C1)
2. Alert schema version mismatch between `alert_schema.py` ("1.0") and `metar_monitor.py` (2) (m1)
3. `daily_balances` table uses `INSERT OR REPLACE` which is correct, but the duplicate definition means the second definition's columns are silently ignored

---

## Data Freshness Assessment

**PASS** with caveats:
- Dynamic METAR intervals (3-5 min) are appropriate for trading hours ✅
- Kalshi 5-min intervals during trading window are adequate ✅
- NWP daily at 06:00 UTC is standard ✅
- **Caveat:** No staleness detection or alerting if collection fails. The health JSON file is written, but nothing monitors it. A failed collector could go unnoticed for hours.

---

## Edge Cases Identified

1. **Zero division in Sharpe calculation** — `_compute_sharpe()` in `multi_instance_paper_trader.py` guards against `std_pnl == 0` ✅
2. **Zero division in position sizing** — `compute_position_size()` guards against `current_balance < min_size_usd` ✅
3. **Zero market price** — `paper_trading_engine.py` guards `market_price > 0.001` before computing quantity ✅
4. **Empty METAR data** — `late_day_momentum_hourly.py` returns `None` if `len(obs) < MIN_OBS` ✅
5. **Missing station in Kalshi mapping** — Falls back to 0.5 price (See M8)
6. **Concurrent instance runs** — `InstanceLock` prevents this ✅
7. **Daily reset across timezones** — `_maybe_daily_reset_local()` handles this correctly ✅

---

## Recommended Fix Priority (Pre-PROD)

1. **C3** (station typo `K MSP`) — Trivial fix, immediate
2. **C2** (numpy dependency) — Replace with stdlib, immediate
3. **C4** (P&L calculation) — Fix mark-to-market logic, high priority
4. **C1** (duplicate tables) — Remove duplicates, high priority
5. **C5** (goldilocks dead code) — Fix or document, medium priority
6. **M5** (duplicate Kalshi series) — Remove KJFK/KORD, medium priority
7. **M8** (missing station codes) — Add or remove stations, medium priority
8. **M4** (thread-unsafe cache) — Add lock, medium priority

---

## Conclusion

The system is architecturally sound with clear separation between collection, signal generation, and trading layers. The deterministic guarantee holds (no AI in loops). However, several critical bugs (station typo, numpy dependency, P&L miscalculation) must be fixed before PROD deployment. The duplicate table definitions and schema version mismatch indicate the codebase grew quickly and would benefit from a cleanup pass.

**Recommendation:** Fix C1-C5 and M4-M8 before GitHub push / Render auto-deploy. Integrate Gray Room expert panel feedback alongside these fixes before PROD promotion.

---

**Review complete. No changes integrated. Awaiting Gray Room output and Dan's authorization.**
