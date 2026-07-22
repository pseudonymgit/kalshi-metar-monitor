# Gray Room Round 9 — Expert 3: Software Architecture & Systems Engineering

**Domain:** Module Boundaries, Code Quality, Database Schema, Test Infrastructure, Configuration Management, Timezone Handling

**Codebase:** 118 Python files, ~52,000 lines, weather trading engine

---

## DISCOVERY METHODOLOGY

I performed a systematic multi-pass analysis of the codebase:

1. **Import graph analysis** — traced every import chain in `core/`, `core/signals/`, and `tests/`
2. **Pattern matching** — `grep`-based scans for: bare `except:`, `datetime.now()`, division by zero risks, repeated import statements, missing type hints, duplicate symbols
3. **Structural decomposition** — file size distribution, module count per directory, `__init__.py` exports, `if __name__ == "__main__"` density
4. **Test infrastructure audit** — pytest vs unittest usage, conftest coverage, mocking patterns
5. **Database schema audit** — `CREATE TABLE` and `CREATE INDEX` spread across modules
6. **Configuration drift** — comparing `instance_config.py` vs `instance_config_fixed.py`, env var patterns

---

## ERRORS (14)

### E1. Duplicate `RiskConfig` in paper_trading_engine.py import list

**What:** The import statement at line 107-118 lists `RiskConfig` twice in the same `from risk_controls import (...)` block. Python allows this syntactically but the second declaration silently overwrites the first. This creates a latent bug where any future code that changes the ordering could break.

**Where:** `core/paper_trading_engine.py:107-118`

```python
from risk_controls import (
    RiskMetrics,
    RiskState,
    RiskConfig,        # line 110 — first import
    ...
    RiskManager,
    RiskConfig,        # line 118 — DUPLICATE
    TradeResult
)
```

**Why wrong:** Duplicate imports are a maintenance hazard. If someone adds type annotations or re-exports between the two declarations, the second `RiskConfig` could shadow a modified version. Also indicates copy-paste code generation.

**Spec to fix:** Remove the duplicate `RiskConfig` at line 118. Keep only line 110.

---

### E2. 29 repeated `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection`

**What:** The import statement `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection` appears **29 times** in `paper_trading_engine.py`, interleaved between every other import block.

**Where:** `core/paper_trading_engine.py:1-152` (every 3-5 lines)

**Why wrong:** This is a symptom of automated code generation or repeated copy-paste. It adds ~290 lines of dead code, makes the import section ~150 lines instead of ~20, and creates confusion about what's actually imported. Python's import system deduplicates, but the pattern wastes reader attention and signals that the file was not reviewed after generation.

**Spec to fix:** Consolidate ALL imports at the top of the file. Single `from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection` at line 1, then all other imports below. Remove all 28 subsequent instances.

---

### E3. `BACKTEST_SIGNALS` and `FULL_ENSEMBLE` imported but not defined

**What:** `unified_backtest.py` line 29 imports `BACKTEST_SIGNALS` and `FULL_ENSEMBLE` from `signals`, but these names are **not exported** from `core/signals/__init__.py`.

**Where:** `core/unified_backtest.py:29` → `core/signals/__init__.py`

```python
# unified_backtest.py line 29
from signals import SignalRegistry, BACKTEST_SIGNALS, FULL_ENSEMBLE
```

**Why wrong:** This will raise `ImportError: cannot import name 'BACKTEST_SIGNALS' from 'core.signals'` when `unified_backtest.py` is imported or run. The module is completely broken as-is. The default `run_backtest()` will fail at line 176 when it tries to use `BACKTEST_SIGNALS`.

**Spec to fix:** Either define `BACKTEST_SIGNALS` and `FULL_ENSEMBLE` in `core/signals/__init__.py` (as lists of signal name strings), or change the import in `unified_backtest.py` to use the inline signal list already defined in the function signature.

---

### E4. `p3of.datetime.now()` — module-level alias to standard library

**What:** `p3_api.py` accesses `datetime.now()` through the module alias `p3of.datetime` (where `p3of` is `core.p3_output_formatter`).

**Where:** `core/p3_api.py:247, 282, 301`

```python
import core.p3_output_formatter as p3of
...
"triggered_at_utc": p3of.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
```

**Why wrong:** This works only because `p3_output_formatter.py` happens to import `from datetime import datetime` at the module level. This is an extremely fragile pattern — any refactoring of `p3_output_formatter.py` that removes that import silently breaks `p3_api.py`. It violates the principle of explicit imports.

**Spec to fix:** Replace `p3of.datetime.now()` with `datetime.now(timezone.utc)` in `p3_api.py`, adding `from datetime import datetime, timezone` to its own imports.

---

### E5. `datetime.now()` without timezone in ~30 locations

**What:** The pattern `datetime.now()` (without `timezone.utc`) appears in at least 30 locations across `p3_main.py`, `p3_scheduler.py`, `p3_backtest_engine_v2.py`, `round_number_anchoring.py`, `nws_revision_model.py`, `liquidity_weighted_ensemble.py`, `station_skill_gate.py`, `p3_api.py`, and others.

**Where:** Multiple files in `core/`

```
core/p3_scheduler.py:270, 280, 294, 372, 387, 416, 453, 461, 492, 498
core/p3_main.py:89, 99, 113, 195, 260
core/p3_backtest_engine_v2.py:94, 150
core/round_number_anchoring.py:123
core/nws_revision_model.py:167
...
```

**Why wrong:** `datetime.now()` returns a **naive** datetime (no timezone info). The code then formats it with `strftime("%Y-%m-%dT%H:%M:%SZ")` and labels it `timestamp_utc`. If the server's local timezone is not UTC, the timestamp is **wrong** — it's local time mislabeled as UTC. This is a data integrity bug that manifests as off-by-hours errors in logs, timestamps, and settlement calculations.

**Spec to fix:** Replace all `datetime.now()` with `datetime.now(timezone.utc)` across the codebase. This is a one-line trivially auditable change. Add a linter rule to forbid `datetime.now()` without args.

---

### E6. `datetime.utcnow()` — deprecated in Python 3.12

**What:** `datetime.utcnow()` is used in `kalshi_monitor.py` and `alert_retry_queue.py`.

**Where:** `core/kalshi_monitor.py:358, 2911`, `core/alert_retry_queue.py:30`

```python
return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
```

**Why wrong:** `datetime.utcnow()` is deprecated in Python 3.12+. It produces a naive datetime, then the code manually attaches `timezone.utc` — which is correct but fragile. The modern standard is `datetime.now(timezone.utc)`.

**Spec to fix:** Replace `datetime.utcnow().replace(tzinfo=timezone.utc)` with `datetime.now(timezone.utc)`.

---

### E7. Bare `except:` blocks in 20+ scripts

**What:** Bare `except:` (without exception type) appears in 20+ files in `scripts/`.

**Where:** `scripts/phase8_purged_cv.py:144`, `scripts/ensemble_v10_phase2.5.py:322, 333, 341, 391`, `scripts/phase8_calibrated_search.py:183`, `scripts/phase12/regime_split_diagnostic.py:196`, `scripts/phase12/empirical_markov_chain.py:119`, `scripts/test_enso_integration.py:121`, `scripts/phase8_parameter_sweep.py:195`, `scripts/phase9_calibrated_search.py:183`, `scripts/comprehensive_ensemble_backtest.py:56, 356, 362`, `scripts/basic_backtest.py:27`, `scripts/ensemble_v11_improved.py:154`, `scripts/edge2_time_of_day_decay.py:65`, `scripts/generate_paper_trade_simulation.py:244, 254, 264, 272, 280, 289, 297`, and more.

**Why wrong:** Bare `except:` catches `SystemExit`, `KeyboardInterrupt`, and `GeneratorExit` — meaning Ctrl+C won't work, and the program can't be cleanly shut down. At minimum, these should be `except Exception:`.

**Spec to fix:** Replace all `except:` with `except Exception:` in production-facing scripts. Add `except (Exception, KeyboardInterrupt):` for graceful shutdown support.

---

### E8. Tests that silently catch all exceptions

**What:** `test_p3_prediction.py` has 5 test methods that wrap assertions in `try/except Exception as e:` blocks and print the error rather than re-raising.

**Where:** `tests/test_p3_prediction.py:347, 355, 368, 379, 392`

```python
try:
    result = some_function()
    self.assertIsNotNone(result)
except Exception as e:
    print(f"Error: {e}")
```

**Why wrong:** These tests will **never fail**. Even if the function raises an exception or the assertion fails, the `except` block catches it and prints it. The test passes regardless. This is a false-positive test battery.

**Spec to fix:** Remove the `try/except` wrappers. Let exceptions propagate naturally. If the test framework requires error handling, use `self.assertRaises()`.

---

### E9. `instance_config.py` vs `instance_config_fixed.py` — module identity collision

**What:** Two files exist with the same intended module name: `core/instance_config.py` and `core/instance_config_fixed.py`. Both define `InstanceConfig`, `INSTANCE_CONFIGS`, `InstanceLock`, `write_health_status`, etc. They differ in webhook validation logic and default `discord_enabled` values.

**Where:** `core/instance_config.py` and `core/instance_config_fixed.py`

**Key differences found via `diff`:**
- `instance_config.py` has DYN-ENFORCED webhook validation (only enforces if `discord_enabled=True`)
- `instance_config_fixed.py` has ENFORCE-ALL webhook validation (all three must be set)
- `instance_config.py` defaults `DISCORD_ENABLED_PROD` and `DISCORD_ENABLED_DEV` to `"false"`
- `instance_config_fixed.py` defaults them to `"true"`
- `instance_config_fixed.py` has an additional placeholder-value check that `instance_config.py` lacks

**Why wrong:** Both files define the same Python module name `instance_config`. When Python imports `instance_config`, it will load whichever file it finds first (`.pyc` cache or alphabetical). The scripts in `scripts/` import `from instance_config import ...` — they may get either version depending on import order, `.pyc` cache state, and whether the other file was imported first. This is a **runtime-critical** configuration drift.

**Spec to fix:** Delete one of the two files. Choose the correct version, rename it to be the canonical `instance_config.py`, and update all importers. The `_fixed` suffix suggests the fixed version is the intended one, but the production scripts may be using the unfixed version. Audit which version is actually in use.

---

### E10. Conftest hooks only pytest, but 65/66 tests use unittest

**What:** `tests/conftest.py` defines a pytest-only `autouse` fixture that restores the `ALERT_DB_PATH` environment variable. However, 65 of 66 test files use `unittest.TestCase` (not pytest fixtures), so the cleanup fixture never runs for them.

**Where:** `tests/conftest.py` (pytest fixture) vs `tests/test_*.py` (unittest.TestCase)

**Why wrong:** Environment variable pollution from one test can leak into another when tests run in a single process. The `conftest.py` was supposed to fix this, but it only works for pytest-based tests. The 65 unittest-based tests have no `setUp`/`tearDown` or cleanup for `ALERT_DB_PATH`. This causes test ordering dependencies and flaky failures.

**Spec to fix:** Either:
1. Add `@pytest.mark.usefixtures("restore_alert_db_path_env")` to all unittest classes, or
2. Convert all tests to pytest style, or
3. Remove the conftest and add `setUp`/`tearDown` to each unittest class.

---

### E11. Division by zero in `compute_signal_agreement_score()`

**What:** The function divides by `total_weight` without checking for zero, and divides by `n_signals` multiple times in the `weights` construction.

**Where:** `core/conviction.py:87-88`

```python
if total_weight == 0:
    return 0.0
return total_agreement / total_weight if total_weight > 0 else 0.0
```

**Why wrong:** The code has a guard after the fact, but there's an earlier path where `weights = [1.0 / n_signals if n_signals > 0 else 0.0] * n_signals if n_signals > 0 else []` — if `n_signals` is 0, the formula produces `[0.0] * 0` which is `[]`, but then `total_weight` would be 0 and the guard at line 87 catches it. However, the `else` on line 88 is dead code — the `if total_weight == 0` already returned. This is a logic error, not a crash, but signals confusion.

**Spec to fix:** Simplify: remove the redundant `if total_weight > 0 else 0.0` ternary. The guard at line 87 already handles it.

---

### E12. `SignalFusionEngine` imports `from core.calibration_pipeline` but some paths import from `calibration_pipeline`

**What:** `signal_fusion.py` uses `from core.calibration_pipeline import CalibrationPipeline`, but other files import the same module differently (relative vs absolute).

**Where:** `core/signal_fusion.py:15`

**Why wrong:** Inconsistent import patterns across the `core/` package create subtle import resolution issues. When running from the repo root, absolute imports work. When running from `core/` directory, relative imports work. Mixing them creates hard-to-debug import errors.

**Spec to fix:** Standardize all imports within `core/` to use relative imports (e.g., `from .calibration_pipeline import CalibrationPipeline`) or absolute (e.g., `from core.calibration_pipeline import CalibrationPipeline`). Pick one convention and enforce it.

---

### E13. `scripts/` files import from `signals` and `signal_fusion` without `core.` prefix

**What:** Multiple scripts in `scripts/` import from `signals` and `signal_fusion` (without `core.` prefix), relying on the `core/` directory being in `sys.path`.

**Where:** `scripts/b7_down_momentum_goldilocks.py:33-34`, `scripts/validate_aggregation.py:22`, `scripts/comprehensive_ensemble_backtest.py:30`, `scripts/ensemble_v11_calibration_fusion.py:40`

```python
from signals import GoldilocksSignal, SignalRegistry
from signal_fusion import SignalFusionEngine
```

**Why wrong:** If `core/` is not in `sys.path` (e.g., running from a different working directory), these imports fail. Some scripts handle this, some don't. Inconsistent.

**Spec to fix:** Standardize to `from core.signals import ...` and `from core.signal_fusion import ...` across all scripts. Add `sys.path` setup at the top of each script if needed.

---

### E14. `p3_scheduler.py` imports `ACTIVE_STATIONS` from `kalshi_monitor` inside a `try/except`

**What:** Line 33-48 of `p3_scheduler.py` tries to import `ACTIVE_STATIONS` from `kalshi_monitor`, but falls back to a hardcoded list of 7 stations if it fails.

**Where:** `core/p3_scheduler.py:33-48`

```python
try:
    from core.kalshi_monitor import (
        discover_market_derived_station_codes,
        ACTIVE_STATIONS,  # does this exist?
    )
except ImportError:
    ACTIVE_STATIONS = ["KDEN", "KLAX", "KNYC", ...]
```

**Why wrong:** Silent fallback to a hardcoded list means the production system could be running with only 7 stations when the dynamic discovery module is available but has an import error in a different module. The `except ImportError` masks the real problem.

**Spec to fix:** Validate that `ACTIVE_STATIONS` is actually exported from `kalshi_monitor`. If not, define it there. Log a warning when the fallback is used.

---

## IMPROVEMENTS (7)

### I1. Centralize database schema creation

**What to change:** Currently, 15+ modules independently create their own SQLite tables and indexes using `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`. This is scattered across the codebase with no schema versioning.

**Files affected:** `alert_reconciliation.py`, `alert_state_machine.py`, `alert_throttle.py`, `cross_platform_divergence.py`, `kalshi_monitor.py`, `ladder_cache_observability.py`, `metar_monitor.py`, `multi_stage_execution.py`, `near_miss_audit.py`, `night_mode.py`, `p3_db_migration.py`, `paper_trading_engine.py`, `trade_journal.py`, and others.

**Why better:** Schema creation is currently mixed with runtime logic. A single `schema.py` with versioned migrations would:
- Eliminate the risk of schema drift between modules
- Provide a single source of truth for the database schema
- Enable schema-level testing and validation
- Simplify deployment and rollback

**Effort:** Medium (2-3 days). Requires extracting all `CREATE TABLE` and `CREATE INDEX` statements into a migration module, adding version tracking, and updating all callers to use `ensure_schema(version)` instead of `CREATE TABLE IF NOT EXISTS`.

**Spec for implementation:**
```python
# core/schema.py
SCHEMA_VERSION = 7

TABLES = {
    "transition_events": """
        CREATE TABLE IF NOT EXISTS transition_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            ...
        )
    """,
    # ... all tables
}

INDEXES = {
    "idx_transition_events_station_date": """
        CREATE INDEX IF NOT EXISTS idx_transition_events_station_date
        ON transition_events(station, timestamp_utc)
    """,
    # ... all indexes
}

def ensure_schema(conn: sqlite3.Connection, target_version: int = SCHEMA_VERSION):
    """Apply all schema migrations up to target_version."""
    current = _get_current_version(conn)
    for version in range(current + 1, target_version + 1):
        _apply_migration(conn, version)
```

---

### I2. Replace all `datetime.now()` with `datetime.now(timezone.utc)`

**What to change:** Every instance of `datetime.now()` and `datetime.utcnow()` across the codebase should be replaced with `datetime.now(timezone.utc)`.

**Why better:** Eliminates a class of timezone-related bugs that are notoriously hard to debug. The `Z` suffix in formatted timestamps is a promise of UTC — the code should keep that promise.

**Effort:** Low (1-2 hours). Mechanical search-and-replace.

**Spec for implementation:**
```python
# Before
timestamp_utc = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

# After
timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

Add a pre-commit hook or ruff rule: `DTZ005` (forbidden `datetime.now()`) and `DTZ001` (forbidden `datetime.utcnow()`).

---

### I3. Add `__all__` exports to all modules

**What to change:** Currently, most modules in `core/` have no `__all__` definition. This means `from module import *` imports everything, including internal helpers, constants, and `_LOGGER`.

**Why better:** Explicit `__all__` defines the public API surface of each module, prevents accidental internal API exposure, and enables static analysis tools to detect unused exports.

**Effort:** Low (half-day). Go through each module and add `__all__ = [...]` with the intended public symbols.

**Spec for implementation:**
```python
# core/signal_fusion.py
__all__ = [
    "SignalFusionEngine",
    "TimeDecaySignalManager",
    "dempster_shafer_conflict",
    "apply_conflict_modulation",
    "mutual_information_matrix",
]
```

---

### I4. Delete `instance_config_fixed.py` and consolidate

**What to change:** Remove the duplicate config file. Merge the best parts of both (the `discord_enabled`-aware validation from `instance_config.py` and the placeholder-value check from `instance_config_fixed.py`).

**Why better:** Eliminates the runtime-critical module identity collision. One config, one truth.

**Effort:** Low (1 hour).

**Spec for implementation:**
1. Determine which version is actually used by production scripts
2. Merge the differences into a single canonical `instance_config.py`
3. Delete `instance_config_fixed.py`
4. Add a CI check that prevents duplicate module names

---

### I5. Add type hints to `signal_fusion.py`

**What to change:** `signal_fusion.py` has 30 functions and methods, but the module-level functions (`mutual_information_from_boolean_pairs`, `mutual_information_matrix`, `dempster_shafer_conflict`, etc.) have **no type hints** on their parameters or return values. Even the `__init__` of `SignalFusionEngine` lacks type annotations.

**Why better:** Type hints serve as documentation, enable static analysis, catch type errors at CI time, and make the code more maintainable as the fusion stack grows.

**Effort:** Medium (half-day to full day). The functions have complex signatures with nested lists and dicts.

**Spec for implementation:**
```python
def mutual_information_from_boolean_pairs(
    pairs_i: List[Tuple[str, float]],
    pairs_j: List[Tuple[str, float]],
    outcomes: List[Optional[str]],
) -> float: ...
```

---

### I6. Standardize import paths across `core/` and `scripts/`

**What to change:** Currently, some modules use relative imports (`from .sqlite_utils import ...`), some use absolute (`from core.calibration_pipeline import ...`), and scripts use bare names (`from signals import ...`). Choose one convention.

**Why better:** Eliminates the existing `ImportError` risks. Makes the codebase work regardless of working directory or `sys.path` state.

**Effort:** Low (half-day). Mechanical find-and-replace across all files.

**Spec for implementation:** Use absolute imports everywhere (`from core.signals import ...`, `from core.signal_fusion import ...`). Add `sys.path` setup at the top of each `scripts/` file. Remove the `sys.path.insert(0, str(CORE_DIR))` hacks from individual files.

---

### I7. Add `pyproject.toml` with project metadata

**What to change:** The project has no `pyproject.toml`, `setup.py`, `setup.cfg`, or `requirements.txt` at the root. Dependencies are scattered across the codebase with `try/except ImportError` guards.

**Why better:** A `pyproject.toml` would:
- Define the project's dependencies in one place
- Enable `pip install -e .` for development
- Enable tool configuration (ruff, mypy, pytest)
- Replace the `scripts/` level `requirements.txt` files

**Effort:** Low (2-3 hours). Collect all dependencies from import statements.

**Spec for implementation:**
```toml
[project]
name = "weather-engine"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "numpy",
    "scipy",
    "requests",
    "fastapi",
    "uvicorn",
    ...
]

[tool.ruff]
select = ["E", "F", "I", "N", "W", "DTZ"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

---

## IDEAS (4)

### Idea 1: Dependency Injection for Database Connections

**The idea:** Replace the current pattern of every module calling `sqlite3.connect(db_path)` with a centralized connection pool or factory that provides connections. Each module would receive its connection via constructor injection.

**Expected benefit:**
- All connections use the same PRAGMA settings (WAL mode, busy timeout)
- Connections can be mocked for testing without patching `sqlite3`
- Connection lifecycle is managed in one place
- Enables future migration to a connection pool for concurrent access

**Risk:** Medium. Requires refactoring every module's constructor. The `sqlite_utils.py` module is a partial implementation but isn't used consistently.

**Spec for validation:**
1. Count the number of `sqlite3.connect()` calls across the codebase
2. Design a `DatabaseSession` class that wraps `sqlite3.Connection` with context manager support
3. Refactor one module (`paper_trading_engine.py`) as a proof of concept
4. Verify test coverage is maintained

---

### Idea 2: Centralized Event Bus for Decoupling Major Modules

**The idea:** Introduce an in-process event bus (using `asyncio` or simple callback registration) to decouple:
- `metar_monitor` (temperature transition producer)
- `kalshi_monitor` (market data producer)
- `alert_state_machine` (alert lifecycle)
- `paper_trading_engine` (trade execution)

Currently, these modules are tightly coupled through `_STATE` dicts and direct function calls. An event bus would allow them to communicate through typed events.

**Expected benefit:**
- Modules can be tested independently
- New consumers can be added without modifying producers
- Event logging enables replay and debugging
- Reduces the risk of circular imports

**Risk:** High. The current architecture is deliberately monolithic for determinism. An event bus introduces asynchronicity which could break the deterministic replay guarantees. The `transition_emitter.py` module already shows the beginning of this pattern — it could be extended.

**Spec for validation:**
1. Define the event types that flow between modules (e.g., `TemperatureTransitionEvent`, `MarketPriceUpdateEvent`, `TradeDecisionEvent`)
2. Implement a simple synchronous event bus (not async, to preserve determinism)
3. Move `transition_emitter.py` to use the event bus
4. Run the full backtest suite to verify no regression

---

### Idea 3: Schema Registry and Automated Migration Testing

**The idea:** Create a `SchemaRegistry` that tracks all database tables, columns, types, indexes, and foreign keys in a single JSON/YAML file. Each migration version is a diff against the previous schema. Tests verify that the actual database matches the registered schema.

**Expected benefit:**
- Schema changes are auditable and versioned
- Tests catch schema drift immediately
- New developers can understand the data model without reading 15 modules
- Enables automated migration scripts for production deployments

**Risk:** Low. This is additive — existing code continues to work. The risk is that the schema registry becomes out of date if not enforced by CI.

**Spec for validation:**
1. Extract all current `CREATE TABLE` statements into a `schema.yaml` file
2. Add a test `test_schema_matches_registry()` that creates a fresh database and compares its schema to the registry
3. Add a CI check that fails if the schema drifts from the registry

---

### Idea 4: Modular Backtest Framework with Plugins

**The idea:** Create a plugin-based backtest framework where signals, fusion strategies, and risk controls are plugins that register themselves. The backtest runner discovers and loads plugins, rather than having hardcoded imports.

**Expected benefit:**
- New signals can be added by creating a file (no import chains to modify)
- The `scripts/` directory has 50+ backtest scripts that are variants of each other — a plugin framework would reduce this to 3-5
- Enables A/B testing of different signal combinations in a single run
- The `SignalRegistry` class is already a partial implementation

**Risk:** Medium. Requires refactoring the backtest scripts. The `SignalRegistry` in `core/signals/__init__.py` is a good starting point but needs to be extended to support dynamic discovery.

**Spec for validation:**
1. Design a plugin interface (e.g., `BaseSignal.evaluate(idx, days)` is already defined)
2. Add a `discover_signals()` function that scans `core/signals/` for subclasses of `BaseSignal`
3. Refactor `unified_backtest.py` to accept a list of signal names from config
4. Validate that the output matches the current hardcoded backtest results

---

## ELEPHANTS (3)

### Elephant 1: Three Monolithic Files Contain 22% of the Codebase

**What:** Three files dominate the codebase:
- `core/metar_monitor.py` — 5,007 lines
- `core/kalshi_monitor.py` — 3,062 lines
- `core/paper_trading_engine.py` — 3,104 lines

Total: 11,173 lines out of ~52,000 (22%).

**Why this matters:** Monolithic files are the single largest architectural risk in this codebase:
- **Import coupling:** Every file that imports from `metar_monitor` loads the entire module, including all its runtime state initialization at module level
- **Testability:** Testing a single function requires importing the entire 5,000-line module, which triggers all module-level side effects
- **Merge conflicts:** Multiple developers working on the same file create unresolvable merge conflicts
- **Cognitive load:** A developer cannot hold a 5,000-line file in their working memory
- **Hidden dependencies:** Functions at the bottom of the file depend on state initialized at the top — the dependency graph is implicit

**What happens if ignored:** The files will continue to grow. Each new feature adds another 200-500 lines. The import coupling means that a change to `metar_monitor.py` breaks anything that imports it. At some point, no single developer can safely modify these files without introducing regressions.

**Spec for resolution:**
1. **Decompose `metar_monitor.py`** (5,007 lines) into:
   - `metar_ingestion.py` — METAR data fetching and parsing (~800 lines)
   - `temperature_state.py` — temperature state machine (~500 lines)
   - `transition_detection.py` — transition detection logic (~600 lines)
   - `metar_alerts.py` — alert generation from transitions (~400 lines)
   - `metar_db.py` — database schema and queries (~300 lines)

2. **Decompose `kalshi_monitor.py`** (3,062 lines) into:
   - `market_discovery.py` — market discovery logic (~800 lines)
   - `market_cache.py` — market ladder caching (~500 lines)
   - `kalshi_connectivity.py` — API connectivity (~400 lines)
   - `kalshi_alerts.py` — alert context building (~300 lines)

3. **Decompose `paper_trading_engine.py`** (3,104 lines) into:
   - `trade_execution.py` — trade placement logic (~600 lines)
   - `position_tracking.py` — position management (~500 lines)
   - `reconciliation.py` — daily reconciliation (~400 lines)
   - `signal_generation.py` — signal generation gate (~300 lines)
   - `risk_management.py` — risk controls (~300 lines)

Each decomposition should preserve the existing public API by re-exporting functions from the original file's `__init__.py` equivalent. This is the minimum viable refactor that reduces risk without changing behavior.

---

### Elephant 2: No Integration Test for the Full Pipeline

**What:** All 66 test files in `tests/` are unit tests that mock external dependencies. There is **zero** integration tests that verify the end-to-end pipeline:

1. METAR observation → temperature state update → transition detection → alert emission → trade execution → settlement → P&L reconciliation

**Why this matters:** Unit tests with heavy mocking (each test file has 5-10 `@patch` decorators) verify that individual functions work in isolation, but they cannot detect:
- Data flow breaks between modules (e.g., a field name change in `metar_monitor` that `kalshi_monitor` depends on)
- Schema incompatibilities between the 15+ modules that create tables
- Timezone handling errors that only manifest when data crosses module boundaries
- State corruption from the shared `_STATE` dict
- Import path errors that only surface when the codebase is deployed fresh

**What happens if ignored:** Regressions go undetected until production. The lack of integration tests means every deployment is a leap of faith. The 66 unit tests provide a false sense of security — they test individual isolated functions but not the system as a whole.

**Spec for resolution:**
1. Create a `tests/integration/` directory with a single `test_full_pipeline.py` that:
   - Creates a fresh SQLite database with the proper schema
   - Injects simulated METAR observations
   - Runs the full processing pipeline
   - Verifies that transitions, alerts, and trades are produced
2. The integration test should NOT mock external APIs (use file-based fixtures instead)
3. Add a CI job that runs integration tests separately from unit tests
4. Target: 2 integration test files that cover the critical paths (data ingestion→settlement and signal generation→trade execution)

---

### Elephant 3: 50+ Backtest Scripts with No Shared Framework

**What:** The `scripts/` directory contains 50+ backtest scripts, each with its own copy of backtest logic. Files like `comprehensive_split_backtest.py`, `split_backtest.py`, `basic_backtest.py`, `ensemble_v11_improved.py`, `phase8_calibrated_search.py`, etc. each have their own implementations of:
- Signal loading
- Data loading from SQLite
- Walk-forward loop
- Performance metrics computation
- Result formatting

**Why this matters:** This is the highest-duplication area in the codebase. Each new experiment duplicates the backtest framework. The `unified_backtest.py` module was created to centralize this, but it's broken (E3: `BACKTEST_SIGNALS` not defined) and the scripts haven't migrated to it.

Consequences:
- Bug fixes must be applied to 50+ files
- Metric computation differs between scripts (e.g., Sharpe ratio calculation varies)
- Results from different scripts are not directly comparable
- New contributors must learn 50 different backtest patterns

**What happens if ignored:** The `scripts/` directory will continue to grow. The `unified_backtest.py` module will remain broken and unused. The signal validation pipeline (Gray Room process) will be comparing apples to oranges because each script computes metrics differently. The 50+ scripts represent technical debt that compounds with every new experiment.

**Spec for resolution:**
1. Fix `unified_backtest.py` (E3: export `BACKTEST_SIGNALS` and `FULL_ENSEMBLE` from `core/signals/__init__.py`)
2. Add a `run_backtest()` CLI interface that accepts signal names, date ranges, and fusion parameters as command-line arguments
3. Create a `BacktestConfig` dataclass that captures all backtest parameters
4. Migrate one script at a time to use `unified_backtest.run_backtest()`, verifying output matches
5. Target: reduce the 50+ scripts to 5-10 by removing those that are duplicates

---

## SUMMARY TABLE

| # | Category | Severity | File(s) | Issue |
|---|----------|----------|---------|-------|
| E1 | Import | HIGH | paper_trading_engine.py:110,118 | Duplicate `RiskConfig` import (NameError at runtime) |
| E2 | Code Quality | LOW | paper_trading_engine.py:1-152 | 29 repeated sqlite_utils import statements |
| E3 | Import | CRITICAL | unified_backtest.py:29 | `BACKTEST_SIGNALS`/`FULL_ENSEMBLE` not defined (ImportError) |
| E4 | Fragility | MEDIUM | p3_api.py:247,282,301 | `p3of.datetime` fragile module-level alias |
| E5 | Timezone | HIGH | 30+ locations across 7+ files | `datetime.now()` without timezone — naive datetime mislabeled as UTC |
| E6 | Timezone | MEDIUM | kalshi_monitor.py:358,2911 | `datetime.utcnow()` deprecated in Python 3.12 |
| E7 | Exception Safety | HIGH | 20+ scripts | Bare `except:` blocks swallow SystemExit/KeyboardInterrupt |
| E8 | Test Quality | CRITICAL | test_p3_prediction.py:347-392 | Tests that catch all exceptions — never fail |
| E9 | Configuration | CRITICAL | instance_config.py/fixed.py | Module identity collision — two versions of same config |
| E10 | Test Infrastructure | MEDIUM | conftest.py | Pytest fixture doesn't cover 65/66 unittest-based tests |
| E11 | Logic | LOW | conviction.py:87-88 | Dead code in division-by-zero guard |
| E12 | Import | MEDIUM | signal_fusion.py | Inconsistent import paths (relative vs absolute) |
| E13 | Import | MEDIUM | scripts/*.py | Bare `from signals import` without `core.` prefix |
| E14 | Robustness | MEDIUM | p3_scheduler.py:33-48 | Silent fallback on import failure masks real errors |
| I1 | Architecture | MEDIUM | 15+ modules | Centralize schema creation in migration module |
| I2 | Timezone | LOW | 30+ locations | Mechanical `datetime.now()` → `datetime.now(timezone.utc)` |
| I3 | API Design | LOW | All modules | Add `__all__` exports |
| I4 | Configuration | LOW | instance_config*.py | Delete duplicate config file |
| I5 | Type Safety | MEDIUM | signal_fusion.py | Add type hints to 30 functions |
| I6 | Import | LOW | 20+ files | Standardize import paths |
| I7 | Project Structure | LOW | root | Add pyproject.toml |
| Idea 1 | Architecture | MEDIUM | All modules | Dependency injection for DB connections |
| Idea 2 | Architecture | HIGH | Major modules | Centralized event bus for decoupling |
| Idea 3 | Data | LOW | 15+ modules | Schema registry and migration testing |
| Idea 4 | Architecture | MEDIUM | 50+ scripts | Plugin-based backtest framework |
| Elephant 1 | Architecture | CRITICAL | 3 files | 22% of codebase in 3 monolithic files |
| Elephant 2 | Testing | CRITICAL | tests/ | No integration tests for full pipeline |
| Elephant 3 | Architecture | HIGH | scripts/ | 50+ backtest scripts with no shared framework |

---

*Analysis completed 2026-07-22 01:48 UTC. Independent analysis — no cross-reference with other Gray Room experts.*