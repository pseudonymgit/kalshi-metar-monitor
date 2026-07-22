# Phase A.3 — Risk Controls Wiring + NwpDirectSignal Fix + Test Fixes

**Date:** 2026-07-22  
**Scope:** 4 small fixes across `risk_controls.py`, `nwp_direct_signal.py`, `dual_polarity_signal.py`, and `test_edge_cases.py`.

---

## 1. Risk Controls Wiring — Connecting `risk_controls.py` to the Engine

**File:** `core/risk_controls.py` + `core/paper_trading_engine.py`

### Background
`core/risk_controls.py` defines a complete `RiskManager` class with:
- `check_daily_loss()` — default 5% of account per day
- `check_drawdown()` — default 15% max drawdown
- `check_consecutive_losses()` — default 3 max consecutive losses
- `update_after_trade()`, `evaluate()`, `risk_report()`, `format_risk_alert()` — all implemented

However, the `PaperTrader` engine in `core/paper_trading_engine.py` imports these but the wiring is broken or incomplete in **four** places:

### 1a. Missing `check_kill_switches()` function

**File:** `core/risk_controls.py`  
**Issue:** The `PaperTrader.check_kill_switches()` method (line 212-215) does:
```python
from .risk_controls import check_kill_switches as _check_kill_switches
return _check_kill_switches(self.db_path, self.risk_config)
```

This raises `ImportError` because **no function named `check_kill_switches` exists** in `risk_controls.py`.

**Fix:** Add a module-level function `check_kill_switches(db_path, risk_config)` to `risk_controls.py`. It should:
- Accept `db_path: str` and `risk_config: RiskConfig` (or `risk_config: dict`)
- Return `(should_halt: bool, reasons: List[str])`
- Check global kill-switch state (e.g., a file-based flag, environment variable, or DB row)
- Return `(False, [])` when no kill switch is active
- Return `(True, ["reason string"])` when a kill switch is triggered

### 1b. `risk_report()` caller/callee signature mismatch

**File:** `core/paper_trading_engine.py`, line 192-193  
**Current code:**
```python
def risk_report(self) -> dict:
    return risk_report(self.db_path, RISK_CONFIG)
```

**File:** `core/risk_controls.py`, line 374  
**Current signature:**
```python
def risk_report(risk_manager: RiskManager) -> Dict[str, Any]:
```

**Issue:** The caller passes `(self.db_path, RISK_CONFIG)` but the callee expects `(risk_manager)`.

**Fix (option A — preferred):** Store a `RiskManager` instance on `PaperTrader` (e.g. `self._risk_manager = RiskManager(config=RiskConfig())`) and call `risk_report(self._risk_manager)`.  
**Fix (option B):** Re-define the module-level `risk_report()` to accept `(db_path, risk_config)` and internally create a `RiskManager`. Option A is preferred to preserve risk state across calls.

### 1c. `format_risk_alert()` caller/callee signature mismatch

**File:** `core/paper_trading_engine.py`, line 195-196  
**Current code:**
```python
def format_risk_alert(self) -> str:
    return format_risk_alert()
```

**File:** `core/risk_controls.py`, line 399  
**Current signature:**
```python
def format_risk_alert(risk_manager: RiskManager) -> str:
```

**Issue:** The caller passes no args but the callee requires `risk_manager`.

**Fix:** Change to `return format_risk_alert(self._risk_manager)` (same `_risk_manager` instance from fix 1b).

### 1d. `RiskManager` never instantiated in `PaperTrader.__init__`

**File:** `core/paper_trading_engine.py`, `__init__` method (around line 98)  
**Issue:** The module-level `risk_report()` and `format_risk_alert()` are called without a `RiskManager` instance. Risk state tracking (`daily_pnl`, `consecutive_losses`, `peak_capital`) is lost between calls because no stateful `RiskManager` persists.

**Fix:** In `PaperTrader.__init__()`:
```python
self._risk_manager = RiskManager(config=RiskConfig())
```
Then wire all risk method calls through this instance.

### 1e. Pre-trade risk checks not performed

**File:** `core/paper_trading_engine.py`, trade execution path (around `place_paper_trade` and `daily_paper_run`)  
**Issue:** The `RiskManager` checks (`check_daily_loss`, `check_drawdown`, `check_consecutive_losses`) are not called **before** executing a trade. They're only called during reconciliation (after the fact).

**Fix:** In `place_paper_trade()` or equivalent entry point:
```python
risk_state = self._risk_manager.evaluate()
if not risk_state.passed:
    _LOGGER.warning(f"Trade blocked by risk controls: {risk_state.halt_reason}")
    return None  # or raise RiskBlockedError
```

---

## 2. NwpDirectSignal `evaluate()` No-Op Fix

**File:** `core/signals/nwp_direct_signal.py`, lines 57-60

### Current behavior
```python
def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
    """Standard evaluate interface - opens DB connection and evaluates."""
    # This method is called by the backtest engine, but we need station context.
    # The base class evaluate_for_station calls this after loading days data.
    # Since NwpDirectSignal doesn't use historical days (it queries NWP DB),
    # this is a placeholder. Real callers should use evaluate_for_station().
    return None, 0.0
```

Always returns `(None, 0.0)` — a silent no-op.

### The fix
`evaluate()` should delegate to `evaluate_for_station()` using the date from `days[idx]`. The station context is missing from the standard `evaluate(idx, days)` interface, so the fix requires one of:

**Option A (preferred):** Extract the station from a known attribute or pass it through a thread-local / instance attribute. Modify `evaluate()` to:
1. Get `target_date` from `days[idx]['date']`
2. Get `station` from `self._station` (set externally, e.g. by the backtest engine before calling evaluate)
3. Get `market_type` from context (default 'HIGH')
4. Call `return self.evaluate_for_station(station, target_date, market_type)`

**Option B:** Infer station from `days[idx]` if the caller has embedded it as a key (e.g. `days[idx]['station']`). If present, use it; otherwise fall back to an instance-level default.

**Option C:** Leave `evaluate()` as a pass-through that raises `NotImplementedError` with a clear message — safer than silent no-op.

**Recommendation:** Option A with a `_station` instance attribute set by the backtest loop. This requires a small change in the backtest engine to set `signal._station = station` before calling `evaluate()`.

---

## 3. Four Skipped Test Fixes in `test_edge_cases.py`

**File:** `tests/test_edge_cases.py`

Four tests in `TestAlertDispatchEdgeCases` call non-existent functions or use wrong signatures. The real function names and signatures are:

| Real function | Location | Signature |
|---|---|---|
| `build_paper_trade_alert` | `core/alert_builder.py:432` | `(trade_result, station, market_type, direction, ...)` |
| `format_alert` | `core/alert_formatter.py:205` | `(station, market_type, direction, event_ticker, position_size, conviction_details, balance, ...)` |
| `dispatch_alert` | `core/alert_dispatcher.py:62` | `(alert_data, discord_payload, ...)` — first arg is `alert_data`, not `message` |

### Fix 1 — `test_alert_builder_empty_payload`

**File:** `tests/test_edge_cases.py`, class `TestAlertDispatchEdgeCases`  
**Current (line ~158):**
```python
from core.alert_builder import build_alert
result = build_alert(station=None, market_type=None, direction=None)
```
**Problem:** `build_alert` does not exist. The real function is `build_paper_trade_alert`.  
**Fix:** Change the import and call to:
```python
from core.alert_builder import build_paper_trade_alert
result = build_paper_trade_alert(
    trade_result={}, station=None, market_type=None, direction=None
)
```
Wrap in `try/except (ImportError, TypeError)` for graceful skip.

### Fix 2 — `test_alert_dispatcher_missing_webhook`

**File:** `tests/test_edge_cases.py`, class `TestAlertDispatchEdgeCases`  
**Current (line ~170):**
```python
from core.alert_dispatcher import dispatch_alert
result = dispatch_alert(message="test", webhook_url=None)
```
**Problem:** `dispatch_alert` first positional arg is `alert_data` (a dict), not `message`.  
**Fix:** Change the call to:
```python
result = dispatch_alert(
    alert_data={"test": True},
    discord_payload={"content": "test"},
    webhook_url=None,
)
```

### Fix 3 — `test_alert_dispatcher_malformed_payload`

**File:** `tests/test_edge_cases.py`, class `TestAlertDispatchEdgeCases`  
**Current (line ~180):**
```python
result = dispatch_alert(message=None, webhook_url="https://hooks.example.com/alert")
```
**Problem:** Same as Fix 2 — `message` is not a parameter of `dispatch_alert`.  
**Fix:** Change to:
```python
result = dispatch_alert(
    alert_data={},
    discord_payload={},
    webhook_url="https://hooks.example.com/alert",
)
```

### Fix 4 — `test_alert_formatter_none_values`

**File:** `tests/test_edge_cases.py`, class `TestAlertDispatchEdgeCases`  
**Current (line ~196):**
```python
from core.alert_formatter import format_alert_message
result = format_alert_message(
    station=None, direction=None, confidence=None, message=None,
)
```
**Problem:** `format_alert_message` does not exist. The real function is `format_alert` with a different signature.  
**Fix:** Change import to `from core.alert_formatter import format_alert`, and call:
```python
result = format_alert(
    station=None, market_type=None, direction=None,
    event_ticker=None, position_size=0.0,
    conviction_details={}, balance=0.0,
)
```

---

## 4. `dual_polarity_signal.py` Relative Import Fix

**File:** `core/signals/dual_polarity_signal.py`, line 22

### Current code
```python
from ..station_effects import get_wind_delta_t, is_warming_wind
```

### Problem
`core/signals/` is a subpackage under `core/`. The relative import `..station_effects` resolves to `core.station_effects` in theory, but relative imports fail when the module is executed directly (`python -m core.signals.dual_polarity_signal`) or when the package structure creates import ambiguity. Additionally, `dual_polarity_signal.py` runs `test_framework()` under `if __name__ == '__main__':` which breaks with relative imports.

### Fix (line 22)
Change to an absolute import:
```python
from core.station_effects import get_wind_delta_t, is_warming_wind
```

This ensures the module can be imported from any context (direct execution, `-m` invocation, pytest collection, etc.) without `ImportError`.

---

## Implementation Order

| # | Item | File(s) | Risk | Effort |
|---|---|---|---|---|
| 1e | Add `check_kill_switches()` function | `core/risk_controls.py` | HIGH — imported but missing | Small |
| 1b | Fix `risk_report()` wiring | `core/paper_trading_engine.py` + `core/risk_controls.py` | MEDIUM — broken calls | Small |
| 1c | Fix `format_risk_alert()` wiring | `core/paper_trading_engine.py` | MEDIUM — broken calls | Trivial |
| 1a | Instantiate `RiskManager` in `PaperTrader.__init__` | `core/paper_trading_engine.py` | MEDIUM — enables stateful risk | Small |
| 1d | Add pre-trade risk gate | `core/paper_trading_engine.py` | MEDIUM — prevents over-trading | Small |
| 2 | Fix `NwpDirectSignal.evaluate()` no-op | `core/signals/nwp_direct_signal.py` | HIGH — silent failure | Small |
| 3 | Fix 4 test function/signature mismatches | `tests/test_edge_cases.py` | LOW — tests currently skipped by `pytest.skip` | Trivial |
| 4 | Fix relative import in dual_polarity_signal.py | `core/signals/dual_polarity_signal.py` | MEDIUM — import error on direct run | Trivial |

---

**File version:** 1.0  
**Review status:** Spec complete, pending implementation.