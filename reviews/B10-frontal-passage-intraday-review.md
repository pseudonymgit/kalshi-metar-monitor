# REVIEW: B10 — frontal_passage_intraday

**Verdict: REVISE**

---

## Issues Found

### 🔴 P1: `evaluate()` always returns `(None, 0.0)` — signal is dead through the standard interface

`FrontalPassageIntradaySignal.evaluate()` delegates to `FrontalDetectorSignal.evaluate()`, which is **deprecated and always returns `(None, 0.0)`** (see `frontal_detector_signal.py:204`). The sweep and backtest harness call `signal_obj.evaluate(idx, days)` (big_sweep.py:468), so the signal never fires in daily backtest runs.

The signal only works via `evaluate_for_station()`, which is not called by the standard sweep/backtest pipeline. This means the signal is **effectively always-neutral** through the primary interface it's supposed to satisfy.

**Fix:** Either implement a real `evaluate()` that uses intraday METAR data, or remove the `evaluate()` fallback entirely and surface this as a station-only signal.

### 🔴 P1: `_load_intraday_obs` ignores the `hours` parameter — SQL query loads full day

The SQL query in `_load_intraday_obs` filters only by `station` and `date_utc`, with **no time-window filter**:

```python
cur.execute("""
    SELECT ... FROM metar_observations
    WHERE station = ? AND date_utc = ?
    ...
""", (station, date))
```

The `hours` parameter (and `self.lookback_hours = 3`) are **never used in the query**. A call with `hours=3` and a call with `hours=24` return identical results. The "3-hour lookback window" documented in the class docstring, the `LOOKBACK_HOURS` constant, and `self.lookback_hours` instance variable are all effectively dead code.

**Impact:** The condition checks (`_check_wind_shift`, `_check_pressure_tendency`, `_check_temp_change`) operate on **all observations for the entire day**, not a 3-hour window. "Intraday" precision is misleading — this is a daily-level detector in practice.

**Fix:** Add a `timestamp_utc >= ? AND timestamp_utc <= ?` filter using the `hours` parameter to constrain the window relative to the latest observation or a reference time.

### 🟡 P2: `evaluate_for_station` can return `(None, confidence > 0)` — invalid tuple

If `conditions_met >= 2` but `_determine_direction` returns `None` (e.g., wind shift signals cold front, temp change signals warm front, and pressure change wasn't detected so no tiebreaker), the function returns `(None, 0.55)` or `(None, 0.80)`. This violates the invariant that `(None, 0.0)` means "no signal" and `(direction, confidence)` means "signal firing."

The `@validate_signal` decorator that catches this is only on `evaluate()`, **not on `evaluate_for_station()`**.

**Fix:** Guard with `if direction is None: return None, 0.0` after determining direction, and/or add `@validate_signal` to `evaluate_for_station`.

### 🟡 P2: `evaluate_for_station` ignores the `conn` parameter — breaks caller transaction boundaries

The `conn` parameter is accepted but documented as "unused — opens own connection." This means:
- Callers with temporary databases or uncommitted writes will have inconsistent state.
- The connection is opened/closed per call, which is wasteful for batch processing.

**Fix:** Use the passed `conn` if provided, or accept the station-only nature and remove the `conn` parameter from the signature.

### 🟡 P2: Connection leak in `_load_intraday_obs` exception path

If `get_sqlite_connection` succeeds but `cur.execute()` raises an exception, the `except` block returns `[]` without calling `conn.close()`. The connection is leaked.

**Fix:** Use a context manager (`with`) or `try/finally` to ensure `conn.close()` is always called.

### 🟢 P3: Wind direction bearing heuristic is overly crude

`_check_wind_shift` computes `late_mean < 180` as 'northerly' and `late_mean >= 180` as 'southerly'. A late mean of 100° (easterly) is mislabeled 'northerly'; a late mean of 190° (180° = due south, 190° = S by 10°W) is mislabeled 'southerly'. The code also computes `diff` (for clockwise/counterclockwise detection) but **never uses it** — it's dead code.

**Fix:** Either use the computed `diff` for accurate veering/backing detection, or document the bearing heuristic more precisely and use cardinal quadrants (e.g., 315°–45° = N, 45°–135° = E, etc.).

### 🟢 P3: `min_lookback` inconsistency

`min_lookback` returns `2` with the docstring "Need at least 2 observations in the lookback window", but `evaluate_for_station` requires `len(obs) >= 4` (checks for `< 4` then tries a 24-hour fallback, also requiring `< 4`). The `evaluate()` path uses `min_lookback=2` for the deprecated fallback. These are inconsistent.

### 🟢 P3: `_mean_wind_direction` returns 0.0 on vector cancellation

If all wind vectors cancel out (e.g., 0° and 180°), `atan2(0, 0)` approach returns 0.0, which could be misread as a valid direction. Consider returning `None` in this case.

---

## What's Good

- Self-test suite passes and covers all condition checks, direction determination, and helper methods.
- `BaseSignal` interface (`name`, `min_lookback`, `evaluate()` signature) is formally satisfied.
- Signal is registered in `SignalRegistry` (`__init__.py:87`) and `build_signal_registry()` (`big_sweep.py:126`).
- `evaluate_for_station` has a real, working intraday detection path for station-level queries.
- `@validate_signal` decorator is applied to `evaluate()`.
- Circular wind difference math is correct.
- `_determine_direction` uses a reasonable voting-plus-tiebreaker approach.
- Clean separation of condition checks into individual methods.

---

## Summary

| Check | Status |
|---|---|
| `evaluate()` returns `(direction, confidence)` tuple | ✅ (but always `(None, 0.0)`) |
| BaseSignal interface satisfied | ✅ |
| Registered in `SignalRegistry` | ✅ |
| Registered in `build_signal_registry()` | ✅ |
| Signal produces non-neutral output | ❌ via `evaluate()` — always `(None, 0.0)` |
| Signal produces non-neutral output | ⚠️ via `evaluate_for_station()` — yes, but has bugs |

**Recommendation:** Fix the P1 issues (dead `evaluate()` and ignored `hours` parameter) before this signal can be relied upon in the sweep. The `evaluate_for_station()` path is salvageable but needs the `(None, confidence>0)` guard and connection leak fix.