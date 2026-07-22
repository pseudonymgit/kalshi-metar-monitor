# Alert Subsystem Diagnosis — P2

## Date
2026-07-22

## Diagnosis Result
**There is NO circular import issue in the alert subsystem.** All five alert modules (`alert_builder`, `alert_dispatcher`, `alert_formatter`, `alert_retry_queue`, `alert_state_machine`) import cleanly in isolation and when the `core` package is loaded first. The four skipped tests are failing due to **function name mismatches** between the test file and the actual module APIs.

---

## Per-test Analysis

### 1. `test_alert_builder_empty_payload` — SKIPPED: "alert_builder module not available"

**Root cause:** The test tries:
```python
from core.alert_builder import build_alert   # Does not exist
```

The actual function is named `build_paper_trade_alert` (line 432 of `core/alert_builder.py`).

**Fix:** Change the import to `from core.alert_builder import build_paper_trade_alert` and update the call signature to match.

---

### 2. `test_alert_formatter_none_values` — SKIPPED: "alert_formatter module not available"

**Root cause:** The test tries:
```python
from core.alert_formatter import format_alert_message   # Does not exist
```

The actual function is named `format_alert` (line 205 of `core/alert_formatter.py`). There is also `format_detailed_alert` (line 227).

**Fix:** Change the import to `from core.alert_formatter import format_alert` and update the call signature to match.

---

### 3. `test_alert_dispatcher_missing_webhook` — SKIPPED: "dispatch_alert raised: dispatch_alert() got an unexpected keyword argument 'message'"

**Root cause:** The import succeeds, but the call fails:
```python
dispatch_alert(message="test", webhook_url=None)
```

The actual function signature is:
```python
def dispatch_alert(alert_data: Dict[str, Any],
                   discord_payload: Dict[str, Any],
                   instance: Optional[str] = None,
                   webhook_url: Optional[str] = None,
                   timeout: int = 10) -> Dict[str, Any]:
```

`message` is not a valid keyword argument — the first positional parameter is `alert_data`.

**Fix:** Change the call to `dispatch_alert(alert_data={"message": "test"}, webhook_url=None)` or use an appropriate alert data dict.

---

### 4. `test_alert_dispatcher_malformed_payload` — SKIPPED: "dispatch_alert raised: dispatch_alert() got an unexpected keyword argument 'message'"

**Root cause:** Same as #3 — `message` is not a parameter of `dispatch_alert`.

**Fix:** Same as #3 — pass `alert_data` as a dict instead of using `message=`.

---

## Import Dependency Graph (verified)

```
alert_builder.py         →  stdlib only (enum, os, time, logging, re)
                              → lazy import of .station_registry inside generate_market_url()
                              → NO circular import

alert_dispatcher.py      →  stdlib + requests + typing
                              → NO internal project imports
                              → NO circular import

alert_formatter.py       →  stdlib + from core.conviction import ConvictionScorer
                              → core/conviction.py → stdlib only (math, statistics, datetime)
                              → NO circular import

alert_retry_queue.py     →  stdlib only (os, json, sqlite3, threading, logging, datetime, pathlib)
                              → NO circular import

alert_state_machine.py   →  stdlib only (sqlite3, json, os, logging, datetime, typing, pathlib, enum)
                              → lazy imports (inside functions):
                                 - from core.alert_builder import format_alert_for_discord
                                 - from core.alert_retry_queue import _queue_alert_for_delivery
                              → NO circular import (lazy imports are deferred)
```

No module in the alert subsystem imports another alert module at the top level. All cross-references (e.g., `alert_state_machine.py` importing `alert_builder` and `alert_retry_queue`) are inside function bodies, which are deferred until runtime.

---

## Recommended Fix

1. **`test_edge_cases.py` lines 271-272** — Change `build_alert` → `build_paper_trade_alert` and update call signature
2. **`test_edge_cases.py` lines 278-281** — Change `format_alert_message` → `format_alert` and update call signature
3. **`test_edge_cases.py` lines 299-300, 316-317** — Change `dispatch_alert(message=..., ...)` → `dispatch_alert(alert_data=..., ...)`

The tests are testing the right concepts (empty payloads, missing webhooks, malformed payloads, None values) but referencing the wrong function names.