# Spec: Paper Trading Engine Import Fixes

## Problem

The paper trading engine at `core/paper_trading_engine.py` (3408 lines) has three categories of import issues that prevent clean loading and violate code review standards:

1. **Archived module import** — `late_day_momentum_hourly` (killed July 6, 48.31% directional accuracy) is still imported at line 72 via `sys.path.insert(0, ...)` + direct import. The module was archived to `core/archive/2026-08-11/`. The import will fail if the archive directory is not on the path.

2. **Duplicate imports** — `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection` appears 30+ times, interleaved between every other import. This is flagged as HIGH debt in the v3 code review.

3. **Position sizing import verification** — The `position_sizing` import was previously broken and was fixed to `position_sizer`. Need to verify both import paths (line 80 and line 144) are correct.

## Current State

### Issue 1: `late_day_momentum_hourly` (lines 70-72)

```python
# Import the hourly late-day momentum signal
sys.path.insert(0, str(REPO_ROOT / "core"))
from late_day_momentum_hourly import late_day_momentum_hourly as _ldm_hourly_signal
```

The module is used in three locations:
- **Line 1033**: `ldm_direction, ldm_conf, ldm_prob = _ldm_hourly_signal(station, date, metar_conn)` — signal generation in `_get_trading_signals()`
- **Line 1586**: Same call in a different signal path (`generate_paper_trade_simulation()` or similar)
- **Line 3307**: Trade version tagging: `trade_ver = "v2.1_ldm_hourly"`

The module lives at `core/archive/2026-08-11/late_day_momentum_hourly.py`. It returns `(direction: str | None, confidence: float, probability: float)` where direction is `"up"` or `"down"` or `None`.

### Issue 2: Duplicate imports (lines 1-165)

Every substantive import is preceded/followed by `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection`. This repeats ~30 times, making the file hard to read, maintain, and diff.

### Issue 3: Position sizer imports (lines 80-84, 144)

- **Line 80**: `from core.position_sizer import (compute_position_size as ..., ...)` — CORRECT. Uses the canonical `position_sizer.py` module which exists and contains all the exported symbols.
- **Line 144**: `from core.position_sizer import PositionSizer` — CORRECT. The `PositionSizer` class exists in `position_sizer.py` at line 174.

Both imports are already fixed from the old `position_sizing` name. No changes needed here.

## Design

### Fix 1: No-op stub for `late_day_momentum_hourly`

Replace the archived module import with a no-op stub matching the same pattern used for `adaptive_thresholds` (lines 57-64 of paper_trading_engine.py). The stub must match the return signature: `(direction, confidence, probability)`.

Strategy: **Remove all dependent code** rather than stub the import. The signal was killed at 48.31% directional accuracy (below coin flip). Keeping a no-op stub that returns `(None, 0.0, 0.0)` is cleaner, but the cleanest approach is to:

1. Remove the `sys.path.insert(0, ...)` and `from late_day_momentum_hourly import ...` line
2. Remove the three usages (lines 1033-1035, 1586-1587, 3307)
3. The trade version tag `v2.1_ldm_hourly` at line 3307 will never be reached after removal — clean it up

This is safer than a stub because it eliminates dead code paths entirely. The `adaptive_thresholds` stub remains because that module may be restored; `late_day_momentum_hourly` was permanently killed.

### Fix 2: Consolidate duplicate imports

Move the single `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection` to the top of the import block (after the shebang and docstring, before any other imports). Remove all 29+ interleaved duplicates.

### Fix 3: Verification only — imports already correct

Confirm both `position_sizer` imports are correct. No changes needed.

## Implementation Steps

### Step 1: Remove `late_day_momentum_hourly` import and usage

File: `core/paper_trading_engine.py`

1. Delete lines 70-72 (the `sys.path.insert` and `from late_day_momentum_hourly import ...` lines). The `sys.path.insert` is only used for this one import.
2. Delete or comment out lines 1033-1035 (the `_ldm_hourly_signal(station, date, metar_conn)` call in `_get_trading_signals()`).
3. Delete or comment out lines 1586-1593 (the `_ldm_hourly_signal(station, date, metar_conn)` call in the second signal path, and the `late_day_momentum_prob`/`late_day_momentum_confidence` metadata setting).
4. Delete or update line 3307 (`trade_ver = "v2.1_ldm_hourly"`). Since this code path becomes unreachable, the `if functionality == "late_day_momentum_hourly"` block at line 3306 will never match — consider removing the conditional or keeping it for future signal versioning patterns.

### Step 2: Consolidate duplicate imports

File: `core/paper_trading_engine.py`

1. Place `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection` at line 14 (after docstring, before `import sqlite3`).
2. Remove all subsequent instances of this import line (29+ occurrences scattered from lines 3-165).

Use a tool that can do this safely — the instances are identical and interleaved with other imports. A sed command or Python script matching the exact string will work:

```
re.sub(r'^from \.sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection\n', '', content)
```

But keep the FIRST occurrence (move it to the top).

### Step 3: Verify position sizer imports (no changes needed)

Confirm line 80 and line 144 both reference `position_sizer` (not `position_sizing`) and that the module exists.

## Changed Files

| File | Change Type | Description |
|---|---|---|
| `core/paper_trading_engine.py` | DELETE | Remove `late_day_momentum_hourly` import (lines 70-72) |
| `core/paper_trading_engine.py` | DELETE | Remove `_ldm_hourly_signal()` call in `_get_trading_signals()` (lines 1033-1035) |
| `core/paper_trading_engine.py` | DELETE | Remove `_ldm_hourly_signal()` call in second signal path (lines 1586-1593) |
| `core/paper_trading_engine.py` | DELETE | Remove `trade_ver = "v2.1_ldm_hourly"` branch or keep stub (line 3307) |
| `core/paper_trading_engine.py` | REFACTOR | Consolidate 30+ duplicate `sqlite_utils` imports into one at top |
| No change | VERIFY | `position_sizer` imports at lines 80 and 144 confirmed correct |

## Verification Steps

```bash
# 1. Verify clean import
python3 -c "from core.paper_trading_engine import PaperTradingEngine; print('OK')"

# 2. Verify the module still instantiates and runs
python3 -c "
from core.paper_trading_engine import PaperTradingEngine
trader = PaperTradingEngine()
print(f'PaperTradingEngine instantiated: {trader.trader_id}')
print('OK')
"

# 3. Verify no leftover references to _ldm_hourly_signal
grep -n "_ldm_hourly_signal" core/paper_trading_engine.py && echo "WARNING: residual references found" || echo "CLEAN: no residual references"

# 4. Verify single sqlite_utils import
grep -c "from .sqlite_utils import" core/paper_trading_engine.py | xargs -I{} sh -c 'test {} -eq 1 && echo "CLEAN: single import (found {})" || echo "WARNING: {} imports found"'

# 5. Run existing tests if any
python3 -m pytest tests/test_paper_trading.py -v 2>/dev/null || echo "No paper trading tests found"
```

## Rollback Plan

1. If `late_day_momentum_hourly` is needed again: revert the three delete blocks in Step 1. The archive module at `core/archive/2026-08-11/` remains intact.
2. If imports break: the duplicate import consolidation is a mechanical change. A broken consolidation means a missed duplicate — check the bottom of the file for any late-in-file imports that were missed. Use `grep -n "from .sqlite_utils import"` to audit.
3. If anything else breaks: `git checkout -- core/paper_trading_engine.py` from the last commit.

## Out of Scope

- Refactoring the 3408-line file into smaller modules (that's a separate project)
- Fixing the `late_day_momentum.py` (daily version, not hourly) — it still exists in `core/` and may be in use
- Adding type hints or improving code style
- Changing any business logic or signal behavior
- The `_ldm_hourly_signal` module itself — it's archived; this spec only removes its usage