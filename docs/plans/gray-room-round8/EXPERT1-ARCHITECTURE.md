# Gray Room Round 8 — Expert 1: Software Architecture, Code Quality & Testing Methodology

**Date:** 2026-07-22
**Domain:** Software Architecture, Code Quality, Testing
**Inputs:** Pre-read (GRAY-ROOM-ROUND8-PREREAD.md), Phase 15 Code Review (PHASE15-CODE-REVIEW-2026-07-21.md), live codebase inspection

---

## EXECUTIVE SUMMARY

The weather engine codebase has high signal accuracy (72.3% at agree=3) but runs on fragile architecture. 4 files = 24% of the codebase, 87+ div-by-zero risks, 49+ naive datetimes, 18 bare excepts, 5 hardcoded fee rates, a dead signal registered, a live signal unregistered, and test scripts that simulate rather than execute. The core problem is that the codebase grew by accretion (Phases 1-15) without a corresponding refactoring phase. The system works, but it will break unpredictably in production.

The refactoring strategy should be: **isolate fault domains first, then extract modules, then unify config.** Order of operations matters — do not refactor `metar_monitor.py` (5,007 lines) until its interfaces are stable, because it's the data backbone.

---

## ERRORS (14)

### Error 1: Agreement Threshold Default Mismatch
**Location:** `core/paper_trading_engine.py:L1102`
**What:** `os.getenv("AGREEMENT_THRESHOLD", "2")` defaults to 2, but the info map (from combinatorial search) recommends 3.
**Why wrong:** The changelog at L10 claims "Phase 8: Configure agreement threshold=3 (env var AGREEMENT_THRESHOLD), align with agreement_gate.py default" — but the actual code default is `"2"`, not `3`. The `agreement_gate.py` defaults to `n_required=3` but the production pipeline uses env var default `"2"`. This means without the env var set, the system runs at agree=2 (66.2% accuracy, 9,977 trades) silently, not agree=3 (72.3%, 2,657 trades). The changelog is misleading.
**Spec:** Change `os.getenv("AGREEMENT_THRESHOLD", "2")` to `os.getenv("AGREEMENT_THRESHOLD", "3")` on L1102. Add a startup log line that prints the effective agreement threshold. Document the trade-off (agree=2 = volume, agree=3 = accuracy) in a config comment. Verify that `agreement_gate.py` default is `n_required=3` (it is) and that the env var is referenced correctly.

### Error 2: Dead Signal Still Registered (nwp_analog, 49.2%)
**Location:** `core/signals/__init__.py:L25,L46` and `core/paper_trading_engine.py:L190,L937-L949`
**What:** `NwpAnalogSignal` (49.2% accuracy — worse than a coin flip) is still imported and registered in `SignalRegistry.signals['nwp_analog']`. The `paper_trading_engine.py` also directly imports and uses it at L190 and L937-L949.
**Why wrong:** This signal degrades ensemble performance. It's registered as a first-class signal but performs at 49.2% — actively harmful to the ensemble. The registry should not include signals that are known to be worse than random.
**Spec:** Remove the import of `NwpAnalogSignal` from `core/signals/__init__.py` and remove it from the registry dict. Remove the import and all references in `core/paper_trading_engine.py` (L190, L325, L346, L937-949, L1044, L1423, L2283). Add a deprecation notice in the changelog. Keep the file on disk for reference but exclude from all import paths.

### Error 3: Live Signal Not Registered (nwp_direct, 92.7%)
**Location:** `core/signals/__init__.py` (not imported) and `core/signals/nwp_direct_signal.py`
**What:** `NwpDirectSignal` exists on disk (92.7% GFS direction accuracy) but is NOT imported in `core/signals/__init__.py` and NOT registered in the `SignalRegistry`. The `evaluate()` method returns `(None, 0.0)` — it's a stub, not a working signal.
**Why wrong:** This is the highest-performing signal in the codebase (92.7% directional accuracy) and it's completely disconnected. It's not wired into the pipeline, not in the registry, and the `evaluate()` method is a no-op. The signal exists on disk but is dead code.
**Spec:** Import `NwpDirectSignal` in `core/signals/__init__.py`, register it in the dict. Complete the `evaluate()` method — it currently just returns `(None, 0.0)`. Wire it into `paper_trading_engine.py` alongside the other signals. This is a Phase 16 priority item.

### Error 4: Hardcoded Fee Rate Contradiction in position_sizing.py
**Location:** `core/position_sizing.py:L62` vs `L82`
**What:** The `PositionSizingConfig` dataclass defaults `fee_rate=0.0` (correct, L62), but the `FeeAwareKellyPositionSizer.__init__` defaults `fee_rate=0.05` (wrong, L82). The docstring says "Kalshi charges 0 commission."
**Why wrong:** Two conflicting defaults in the same file. The `FeeAwareKellyPositionSizer` is the production class (used by `paper_trading_engine.py`), so paper trades are incorrectly subtracting 5% fee from edge calculations. This artificially reduces position sizes and distorts the P&L. The CRITICAL fix from Phase 15 was only partially applied — the dataclass got fixed but the production class did not.
**Spec:** Change `core/position_sizing.py:L82` to `fee_rate: float = 0.0`. Also fix L176-192 where `ConfigPreset.PROD` uses `fee_rate=0.001` — change to `0.0`. Fix L203 where `TEST` preset uses `fee_rate=0.002` — change to `0.0`. Add a comment: "Kalshi charges zero commission — fee_rate is reserved for spread modeling only."

### Error 5: Phase 14 Test Scripts Are Monte Carlo Simulations, Not Backtests
**Location:** `scripts/phase14_unattended_test.py:L100-L229`
**What:** The `simulate_execution_of_best_combo()` function (L100) uses `random.gauss()` to generate fake daily accuracy numbers based on the expected accuracy from the combinatorial search. It does NOT execute signals against historical data.
**Why wrong:** This is a statistical simulation, not a backtest. The "test" reports 69.67% accuracy but this is a simulated value generated from random sampling, not an empirical result. There is no real deployment test that runs the actual pipeline against historical data and checks what the signals would have predicted. The results file `data/phase14_30day_test_results.json` is essentially a random number generator output.
**Spec:** Replace `scripts/phase14_unattended_test.py` with a real backtest script that:
1. Loads the actual signal pipeline (same as paper_trading_engine.py)
2. Iterates through 30 days of historical METAR data
3. Records what each signal would have predicted for each day
4. Compares against known settlement outcomes
5. Reports actual accuracy, trade count, confidence distribution
6. This is NOT a simulation — it's running the real code

### Error 6: Alert Pipeline Spams Every Trade During Paper Trading
**Location:** `core/paper_trading_engine.py:L2971-L2987`
**What:** The `build_paper_trade_alert()` method dispatches alerts for EVERY executed trade, regardless of paper/live mode.
**Why wrong:** During paper trading, the system fires alerts for every trade (potentially thousands per day at agree=2). This floods the alert channel, causes alert fatigue, and wastes webhook credits. The Phase 15 review flagged this but it was not fixed.
**Spec:** Add a `paper_mode` flag to the alert builder. When `paper_mode=True`, suppress all alerts OR aggregate them into a daily summary. Only dispatch individual alerts when `paper_mode=False` (live trading). The daily summary alert should include: total trades, win rate, P&L, largest win/loss, and any risk threshold breaches.

### Error 7: .bashrc Webhook Variable Name Mismatch
**Location:** `~/.bashrc:L116-117` (sources `.env.webhooks` directly) vs `core/alert_dispatcher.py` (expects `DISCORD_WEBHOOK_PROD`)
**What:** `.env.webhooks` exports `WEBHOOK_PROD` but the code reads `DISCORD_WEBHOOK_PROD`. The cron wrapper (`scripts/load_webhooks.sh`) does the remapping correctly, but interactive shells source `.bashrc` which does NOT.
**Why wrong:** Interactive development sessions have no Discord alerts. The discrepancy means developers testing locally don't see alert failures until they check cron logs. This has been broken since the webhook system was set up.
**Spec:** Update `.bashrc` to source `scripts/load_webhooks.sh` instead of sourcing `.env.webhooks` directly. This ensures the `WEBHOOK_PROD` → `DISCORD_WEBHOOK_PROD` remapping happens in all contexts.

### Error 8: 18 Bare Except Clauses Swallow Errors
**Locations:** `core/heartbeat.py` (L127, L217, L253, L286, L305, L446), `core/climatology_pillar.py` (L83, L101, L123, L145), `core/alert_reconciliation.py` (L181), `core/p3_backtest_engine.py` (L627), `core/p3_backtest_engine_v2.py` (L717), `core/late_day_momentum.py` (L331), `core/multi_model_ensemble.py` (L274), `core/paper_trading_engine.py` (L1170), `core/rdae_mos.py` (L62), `core/signals/frontal_passage_detector.py` (L107, L237)
**What:** `except:` catches ALL exceptions including `SystemExit`, `KeyboardInterrupt`, and `GeneratorExit`.
**Why wrong:** These are not "noise" — they are dangerous. A bare `except:` will silently swallow `KeyboardInterrupt` (making Ctrl+C not work), `SystemExit` (masking shutdown signals), and `MemoryError` (letting the process continue in an undefined state). The `heartbeat.py` has 6 of these, making it impossible to cleanly shut down the heartbeat process. The `climatology_pillar.py` has 4 bare excepts that could mask data corruption.
**Spec:** Replace every `except:` with `except Exception:` across all 18 locations. This is a one-line change per location. For `heartbeat.py`, add specific exception handling for expected failures (e.g., `except (ConnectionError, TimeoutError):`) and let `KeyboardInterrupt` propagate. For `climatology_pillar.py`, log the exception before continuing.

### Error 9: position_sizing.py Uses Naive datetime.now() for Date Arithmetic
**Location:** `core/position_sizing.py:L95, L97, L110, L366`
**What:** `datetime.now()` is called without timezone. The `add_win_result()` method (L95-97) compares a passed date string with `datetime.now()` — but if the caller passes a timezone-aware datetime, the comparison will raise `TypeError: can't compare offset-naive and offset-aware datetimes`.
**Why wrong:** This is a latent bug that will manifest as soon as any caller passes a timezone-aware datetime. The `_get_rolling_win_rate()` method silently uses naive datetime arithmetic, which means the 30-day rolling window is in local time, not UTC. This can cause off-by-one errors in trade history filtering.
**Spec:** Replace `datetime.now()` with `datetime.now(timezone.utc)` in all 4 locations. Update the `add_win_result()` method to handle both naive and aware datetime inputs by converting to UTC.

### Error 10: No Alert Suppression for Paper Trading Mode
**Location:** `core/paper_trading_engine.py` — no paper/live mode flag exists
**What:** The `PaperTrader` class has no `mode` flag. It always dispatches alerts and always records trades as if they're real.
**Why wrong:** When running in paper mode, the system should not dispatch alerts to Discord (alert fatigue), should not hit Kalshi API rate limits, and should clearly label all data as "paper." Currently there is no distinction between paper and live execution paths.
**Spec:** Add a `mode: str = "paper"` parameter to `PaperTrader.__init__()`. When `mode="paper"`: suppress Discord alerts, add `[PAPER]` tag to all log entries, and skip Kalshi API calls. When `mode="live"`: full alert dispatch, real API calls. This is a single flag that gates multiple behaviors.

### Error 11: Changelog Headers Claim Fixes That Were Not Applied
**Location:** `core/paper_trading_engine.py:L10` (changelog entry)
**What:** The changelog at L10 says "Phase 8: Configure agreement threshold=3 (env var AGREEMENT_THRESHOLD), align with agreement_gate.py default" — but the default is `"2"` at L1102.
**Why wrong:** The changelog is a lie. It claims the threshold was set to 3, but the code has `"2"`. This is either a documentation error (the changelog was written before the code was committed) or a partial fix. Either way, it's misleading for anyone reading the code.
**Spec:** Either fix the code (change default to 3) OR fix the changelog to say "2." The correct fix is to change the default to 3 and update the changelog appropriately.

### Error 12: `phase14_unattended_test.py` Hardcodes 72.3% Target
**Location:** `scripts/phase14_unattended_test.py` (the `identify_best_combo()` function)
**What:** The script hardcodes the combinatorial search target of 72.3% as the expected accuracy for the 30-day test.
**Why wrong:** The 72.3% figure is from the combinatorial search (Phase 9-11), which is a historical optimization result, not a real deployment test. The actual Phase 14 result was 69.67% at agree=1. The script conflates the two figures and reports the combinatorial search target as if it's the test result.
**Spec:** Remove the hardcoded 72.3% target. The test script should report actual accuracy from running the pipeline, not compare against a static target. Add a `--agreement-threshold` CLI flag to allow running the test at different agreement levels.

### Error 13: `nwp_direct_signal.py` evaluate() Returns No-Op
**Location:** `core/signals/nwp_direct_signal.py:L48-L50`
**What:** The `evaluate()` method simply returns `(None, 0.0)` — it does nothing.
**Why wrong:** The signal exists, is not registered, and even if it were registered, it would never produce a prediction. This is dead code pretending to be a signal. The `evaluate_for_station()` method (L52) appears to be the real implementation, but it's disconnected from the standard signal interface.
**Spec:** Rename `evaluate_for_station()` to `evaluate()` and update the signature to match the standard `(idx, days)` interface. Or better, add a bridge method that maps `evaluate(idx, days)` to `evaluate_for_station(station, date)`. Register the signal in `__init__.py`. This is the highest-leverage signal in the codebase (92.7% directional accuracy).

### Error 14: `alert_formatter.py` Depends on DEAD `conviction.py`
**Location:** `core/alert_formatter.py:L33` — imports `ConvictionScorer` from `core.conviction`
**What:** `alert_formatter.py` (restored from git, STALE) imports `ConvictionScorer` from `core.conviction`, which is also a restored DEAD file that duplicates `signal_fusion.py`.
**Why wrong:** If both files are restored and present, the `alert_formatter.py` will use the old `conviction.py` scoring logic instead of the current `signal_fusion.py` scoring. This means alerts will show incorrect conviction scores. The import chain is: `alert_formatter.py` → `conviction.py` (DEAD) → wrong scoring.
**Spec:** Either (a) delete both `alert_formatter.py` and `conviction.py` (they are STALE/DEAD), or (b) rewrite `alert_formatter.py` to import from `signal_fusion.py` instead of `conviction.py`. Option (a) is cleaner — the alert formatting logic is already in `paper_trading_engine.py`'s `build_paper_trade_alert()` method.

---

## IMPROVEMENTS (7)

### Improvement 1: Extract Signal Pipeline from paper_trading_engine.py
**What:** Extract the signal generation and fusion logic (lines 888-1200, the `generate_signals()` method and all signal-specific methods) into a separate `signal_pipeline.py` module.
**Why better:** `paper_trading_engine.py` (3,104 lines) has signal extraction, position sizing, journaling, alerting, and P&L calculation all in one class. The signal generation logic is the most domain-specific and most likely to change independently. Extracting it makes the signal pipeline testable in isolation, reusable by the backtest engine, and easier to maintain.
**Effort:** Medium (2-3 days)
**Spec:**
1. Create `core/signal_pipeline.py` with a `SignalPipeline` class
2. Move `generate_signals()`, `_analyze_late_day_momentum_signals()`, `calculate_temperature_trend()`, `_get_prior_day_reversion()`, `_get_calendar_climatology_direction()`, `_get_prev_day_high_temperature()`, `_get_analytical_probability()` from `paper_trading_engine.py` into the new class
3. Have `PaperTrader` accept a `SignalPipeline` instance via dependency injection
4. Move all signal-specific imports (nwp_analog, etc.) to the new module
5. Update all callers of these methods to use the new class
6. **Do not change any interfaces** — this is a pure extraction refactoring

### Improvement 2: Centralize Configuration in a Single Config Module
**What:** Create `core/config.py` that reads from environment variables with sensible defaults, and replace all hardcoded constants across the codebase.
**Why better:** Currently, configuration is scattered across 50+ files with hardcoded values. The agreement threshold, fee rates, database paths, webhook URLs, and signal weights are each defined in multiple places. A single config module ensures that changing one value changes it everywhere.
**Effort:** Medium (2-3 days)
**Spec:**
1. Create `core/config.py` with a `@dataclass` or `pydantic.BaseSettings` class
2. Include: `AGREEMENT_THRESHOLD`, `FEE_RATE`, `DB_PATH`, `DISCORD_WEBHOOK_PROD`, `MAX_POSITION_SIZE`, `MIN_CONFIDENCE`, `CALIBRATION_REPORT_PATH`, `MAX_DAILY_TRADES`, `MAX_DRAWDOWN_PERCENT`
3. All values should be read from env vars with sensible defaults
4. Replace all scattered `os.getenv("AGREEMENT_THRESHOLD", "2")` calls with `config.agreement_threshold`
5. Replace all hardcoded `0.05` fee rates with `config.fee_rate`
6. Add a startup log that prints all config values

### Improvement 3: Add Guardian Wrapper for All Division Operations
**What:** Create a utility function `safe_divide(numerator, denominator, default=0.0)` and use it across all 87+ div-by-zero locations.
**Why better:** 87+ division by zero risks is not a maintenance burden — it's a structural problem. Individual fixes are error-prone and will be missed. A centralized guard function makes the intent explicit and the fix auditable.
**Effort:** Medium (1-2 days)
**Spec:**
1. Add to `core/cost_utils.py` (or a new `core/safe_math.py`):
   ```python
   def safe_divide(numerator, denominator, default=0.0, log_warning=True):
       if denominator == 0 or denominator is None:
           if log_warning:
               _logger.warning("Division by zero prevented", stack_info=True)
           return default
       return numerator / denominator
   ```
2. Use `sed` or `rg` to find all `/` operations where denominator could be zero
3. Replace each with `safe_divide(numerator, denominator, default=0.0)`
4. This is a mechanical transformation — every `/` that could receive a zero denominator gets wrapped

### Improvement 4: Add Timezone-Aware Datetime Utility
**What:** Create a `now_utc()` function and replace all 49+ `datetime.now()` calls with `now_utc()`.
**Why better:** 49+ naive datetime calls across the codebase is a time bomb. Every comparison between naive and aware datetimes will raise `TypeError`. A centralized utility makes the codebase resilient to timezone issues.
**Effort:** Low (0.5-1 day)
**Spec:**
1. Add to `core/cost_utils.py`:
   ```python
   def now_utc() -> datetime:
       return datetime.now(timezone.utc)
   ```
2. Replace all `datetime.now()` calls with `now_utc()` across the codebase
3. For files where `datetime.now(timezone.utc)` is already used, replace with `now_utc()` for consistency
4. This is a mechanical search-and-replace

### Improvement 5: Add Paper/Live Mode Flag to PaperTrader
**What:** Add a `mode` parameter to `PaperTrader.__init__()` that controls alert dispatch, Kalshi API access, and logging.
**Why better:** Currently paper and live execution are indistinguishable. Adding a mode flag is a one-line change with downstream behavioral changes that makes the system safe for production.
**Effort:** Low (0.5 day)
**Spec:**
1. Add parameter: `mode: Literal["paper", "live"] = "paper"` to `PaperTrader.__init__()`
2. In alert dispatch: `if self.mode == "paper": return` (skip alert)
3. In Kalshi API calls: `if self.mode == "paper": return simulated_price` (skip real API)
4. In logging: prepend `[PAPER]` or `[LIVE]` to all log entries
5. In `__main__` block (L2852): change to `PaperTrader(initial_balance=10000.0, fee_rate=0.0, mode="paper")`

### Improvement 6: Add Signal Registry Validation Test
**What:** Add a unit test that validates every signal in the `SignalRegistry` is functional — i.e., `evaluate()` returns a non-None direction and a valid confidence.
**Why better:** The current system has a dead signal (nwp_analog, registered but harmful) and a stub signal (nwp_direct, exists but not registered). A registry validation test would catch these issues automatically on every deployment.
**Effort:** Low (0.5 day)
**Spec:**
1. Create `tests/test_signal_registry.py`
2. Test: `from core.signals import SignalRegistry; registry = SignalRegistry(":memory:")`
3. For each signal, call `evaluate(0, [mock_day])` and assert the result is not `(None, 0.0)`
4. Assert that no signal returns confidence exactly 0.5 (random guess)
5. Assert that no signal returns accuracy < 50% (if known from historical data)
6. Run this test as part of CI/CD pipeline

### Improvement 7: Add Structured Logging Throughout
**What:** Replace all `print()` calls and bare `logging.info()` with structured JSON logging that includes timestamps, module names, and correlation IDs.
**Why better:** The codebase has a mix of `print()` (in scripts, signal files, and some core modules) and `logging` calls. When the system runs as a cron job, `print()` output goes to stdout but isn't structured for log aggregation. Structured logging enables log-based monitoring, alerting, and debugging.
**Effort:** Medium (2-3 days)
**Spec:**
1. Define a `get_logger(module_name)` function that returns a JSON-structured logger
2. Replace all `import logging` with `import structlog` or add a JSON formatter
3. Each log entry should include: timestamp (UTC ISO 8601), module, function, line number, severity, message, and any structured data (e.g., `{station: "KBOS", accuracy: 0.72}`)
4. Replace `print()` calls with `logger.info()`
5. Configure log level via env var `LOG_LEVEL`

---

## IDEAS (4)

### Idea 1: Extract Monitoring and Observability into a Sidecar Process
**What:** Separate the live dashboard (`dashboard.py`, `confidence_dashboard.py`) and alert pipeline (`alert_dispatcher.py`, `alert_builder.py`, etc.) into a standalone monitoring process that reads from the shared SQLite database rather than being embedded in the paper trading pipeline.
**Expected benefit:** The paper trading engine currently handles trading, alerting, and dashboard data generation. Separating observability into a sidecar process would:
- Reduce the attack surface of the trading engine
- Allow the dashboard to show historical data even when the trading engine is down
- Enable the alert pipeline to be updated without touching the trading code
- Allow independent scaling (e.g., the dashboard could be polled every 5s without affecting trade execution)
**Risk/uncertainty:** The shared SQLite database could become a contention point. Need to ensure read-only access for the sidecar. The alert pipeline needs to be real-time — a sidecar polling approach would add latency.
**Spec for validation:**
1. Create a `monitor_sidecar/` directory with `__init__.py`, `dashboard_server.py`, `alert_consumer.py`
2. The sidecar reads from `data/metar_backfill.db` and `data/paper_trading.db` (read-only)
3. The trading engine writes to a `alerts_outbox` table in the DB
4. The sidecar polls the `alerts_outbox` table every 1s and dispatches alerts
5. Run a 1-week validation: sidecar runs alongside the current embedded system, compare dashboard data and alert timing
6. If validation passes, remove the embedded alert/dashboard code from the trading engine

### Idea 2: Build a Deterministic Replay Framework for Backtesting
**What:** Create a replay engine that can replay any date range through the current signal pipeline and produce a deterministic prediction output. This is different from the existing backtests (which are Monte Carlo simulations) — it's a record-and-replay system.
**Expected benefit:** With a deterministic replay framework, you can:
- Test a code change against the last 30 days of data in < 1 minute
- Compare "before" and "after" prediction outputs for regression testing
- Run the replay as a CI/CD gate before deployment
- Generate a "prediction diff" that shows exactly what changed
**Risk/uncertainty:** The replay engine must produce identical output to the live pipeline for the same inputs. This requires careful handling of random number generators, database state, and timestamp-dependent calculations. Any non-determinism in the signal pipeline would break the replay guarantee.
**Spec for validation:**
1. Create `core/replay_engine.py` with a `ReplayEngine` class
2. Accept a date range and a list of signals to evaluate
3. For each day, load the METAR data that was available at that time (not future data)
4. Run the signal pipeline and record predictions
5. Compare against known settlement outcomes to compute accuracy
6. Add a `--replay` flag to `paper_trading_engine.py` that runs in replay mode
7. Run a validation: replay the 30-day Phase 14 window and compare against the published 69.67% result

### Idea 3: Add a Feature Flag System for Gradual Signal Rollout
**What:** Implement a simple feature flag system (Toggled by env vars or a config file) that allows enabling/disabling individual signals, agreement thresholds, and confidence models without code changes.
**Expected benefit:** Currently, deploying a new signal requires a code change to `__init__.py`, `paper_trading_engine.py`, and potentially the config. With feature flags:
- New signals can be deployed to production but disabled
- Canary testing: enable for 1 station, then 5, then all 20
- Instant rollback: just flip the flag back
- A/B test signal configurations in production
**Risk/uncertainty:** Feature flags add complexity. If not cleaned up, they accumulate and create technical debt. Need a process for removing flags after a signal is validated.
**Spec for validation:**
1. Create `core/feature_flags.py` with a simple dict-based flag system
2. Flags are read from env vars: `SIGNAL_ENABLE_NWP_DIRECT=true`, `AGREEMENT_THRESHOLD=3`
3. Create a `with_flag(flag_name, enabled_fn, disabled_fn)` context manager
4. Add flag validation at startup: log all enabled/disabled flags
5. Run a 1-week trial: deploy NWP Direct with flag disabled, enable it for 2 stations, compare with full rollout
6. After validation, add a `--list-flags` CLI option to the trading engine

### Idea 4: Containerize the Pipeline for Reproducible Deployments
**What:** Create a Dockerfile and docker-compose.yml that packages the weather engine, its dependencies, and the dashboard into a reproducible container.
**Expected benefit:** The current deployment relies on Render auto-deploy from `main`. This works, but:
- Dependencies are resolved at deploy time (pip install every time)
- Environment variables are configured in the Render dashboard (not version-controlled)
- Reproducing the exact environment locally is difficult
- The cron job environment may differ from the interactive shell environment
**Risk/uncertainty:** Containerization adds operational complexity. The SQLite database must be persisted outside the container. The cron job model needs to be rethought (cron inside container vs. host cron triggering container).
**Spec for validation:**
1. Create `Dockerfile` with Python 3.11+ slim image, requirements installed
2. Create `docker-compose.yml` with services: `engine` (cron-driven), `dashboard` (Flask on :5005), `nwp-collector`
3. Docker volumes for: `data/` (SQLite databases), `logs/` (structured logs)
4. Environment variables in `.env` file (not in the Render dashboard)
5. Run a 1-week validation: compare containerized vs. bare-metal accuracy and uptime
6. If validation passes, update the Render deployment to use the Dockerfile

---

## ELEPHANTS (3)

### Elephant 1: The Codebase Monolith — 4 Files = 24% of the Codebase
**What:** `metar_monitor.py` (5,007 lines), `paper_trading_engine.py` (3,104 lines), `kalshi_monitor.py` (3,062 lines), and `signal_fusion.py` (1,268 lines) account for 12,441 lines = 24% of the codebase. Each file does the work of 3-5 modules.
**Why it matters:**
- **Maintenance risk:** A single merge conflict in `paper_trading_engine.py` blocks the entire pipeline
- **Testing difficulty:** You cannot unit test the signal pipeline without instantiating the entire `PaperTrader` class with its DB connections, alert pipeline, and position sizing
- **Cognitive load:** A new developer needs to understand 3,000+ lines of code to change a single signal
- **Deployment risk:** A bug in position sizing (L1995-2050) delays a fix in signal extraction (L888-1200)
- **Code review bottleneck:** Every PR touches `paper_trading_engine.py` because it's the only integration point
**What happens if we ignore it:** The monolith will continue to grow. Each new signal adds 50-100 lines to `paper_trading_engine.py`. Each new feature (HRRR integration, short-duration trading, portfolio management) adds more. The file will reach 5,000+ lines within 6 months. At that point, no single person will understand the full file, and refactoring will be impossible without a rewrite.
**Spec for resolution:**
1. **Phase 1 (Immediate):** Extract the signal pipeline into `core/signal_pipeline.py` (see Improvement 1). This is 800-1,000 lines of `paper_trading_engine.py` that can be cleanly extracted.
2. **Phase 2 (Week 2):** Extract the alert building into `core/alert_builder.py` (replace the STALE `alert_formatter.py`). The `build_paper_trade_alert()` and `build_paper_trade_alert_dev()` methods (L2649-2670) and the alert dispatch logic (L2971-2987) move out.
3. **Phase 3 (Week 3):** Extract the position sizing and P&L calculation into `core/position_calculator.py`. The `_compute_round_trip_cost()`, `mark_positions_to_market()`, `daily_reconciliation()` methods move out.
4. **Phase 4 (Week 4):** Extract the database access layer into `core/trade_repository.py`. All SQL queries move to a repository pattern.
5. After all 4 phases, `paper_trading_engine.py` should be 800-1,000 lines of orchestration logic, not 3,104 lines of everything.
6. **Order matters:** Do signal extraction first (it's the most independent), then alerts, then P&L, then DB. Test after each phase.

### Elephant 2: No Real Backtest Pipeline — All Tests Are Simulations
**What:** The Phase 14 "30-day unattended test" is a Monte Carlo simulation, not a real backtest. The `scripts/phase14/` directory has a `production_setup.py` but no actual backtest runner. The `scripts/phase14_unattended_test.py` uses `random.gauss()` to generate fake results. The `p3_backtest_engine.py` and `p3_backtest_engine_v2.py` exist but are not wired into the test pipeline.
**Why it matters:**
- **Unknown actual accuracy:** The 72.3% and 69.67% figures are from combinatorial search and simulation, respectively. We don't know what the actual pipeline would produce on historical data.
- **No regression detection:** A code change that degrades accuracy by 5% would go undetected because there's no test that runs the real pipeline against historical data.
- **No confidence in deployment:** Every deployment is a leap of faith. The system "works" on the current day's data, but we don't know if it was better or worse than yesterday.
- **False sense of security:** The Phase 14 "test" passed, but it was a simulation. The team believes the system is tested, but it's not.
**What happens if we ignore it:** The team will continue to deploy code changes without knowing if they improve or degrade accuracy. A regression will eventually slip through. When the system goes live and loses money, the team won't know if it's a market condition change or a code regression. The backtest infrastructure will be built in a panic after a loss event.
**Spec for resolution:**
1. **Immediate (0.5 day):** Write a script that runs the actual `PaperTrader` pipeline against 30 days of historical data and records predictions. This is not a new backtest engine — it's the existing code running in replay mode.
2. **Week 1:** Create `scripts/backtest_runner.py` that:
   - Accepts a date range and signal configuration
   - Instantiates the real `PaperTrader` with `mode="backtest"`
   - Iterates through each day, calls `generate_signals()`, `place_paper_trade()`, `process_settlements_for_date()`
   - Records all predictions, trades, and P&L
   - Outputs a JSON report with accuracy, Sharpe, max drawdown, trade count
3. **Week 2:** Wire the backtest runner into CI:
   - Run on every PR against the last 7 days of data
   - Fail the PR if accuracy drops > 2% from the baseline
   - Record the baseline in a `backtest_baseline.json` file
4. **Week 3:** Add a nightly cron job that runs the 30-day backtest and reports results to the dashboard

### Elephant 3: No Separation Between Paper Trading and Live Trading Concerns
**What:** The `PaperTrader` class handles both paper trading (simulation) and the infrastructure for live trading (alert dispatch, Kalshi API interactions, position sizing). There is no `mode` flag, no canary deployment strategy, and no safety interlock between paper and live.
**Why it matters:**
- **Accidental live trading:** If the env vars for Kalshi API keys are set, the paper trading engine could accidentally execute real trades. There is no "are you sure?" gate.
- **No risk isolation:** A bug in the alert pipeline can crash the trading engine. A bug in the position sizing can send a 100% balance trade. There is no circuit breaker.
- **No audit trail:** All trades are recorded in the same DB schema regardless of mode. You can't distinguish paper from live in the database without parsing log messages.
- **Can't test Kalshi integration:** If you want to test Kalshi API connectivity, you need to run the full paper trading engine. There's no integration test harness.
**What happens if we ignore it:** The first time someone adds Kalshi API keys to the environment and runs `python3 paper_trading_engine.py`, the system will execute real trades. If the position sizing has a bug (which it does — see Error 4), the system could place a 100% balance trade on a real market. The lack of a safety interlock between paper and live trading is a material financial risk.
**Spec for resolution:**
1. **Immediate (0.5 day):** Add a `mode` flag to `PaperTrader.__init__()` (see Improvement 5). Default to `"paper"`. When `mode="paper"`, skip all Kalshi API calls and suppress all Discord alerts.
2. **Immediate (0.5 day):** Add a startup safety check: if `mode="live"`, require explicit confirmation via stdin or a separate `--confirm-live` CLI flag. Print a warning message: "⚠️ LIVE TRADING MODE — This will execute real trades on Kalshi. Press Ctrl+C to abort." Wait 5 seconds for confirmation.
3. **Week 1:** Add a `mode` column to the trades table in the SQLite database. All trade records must include the mode. Add a database trigger that prevents trades from being recorded without a mode tag.
4. **Week 2:** Create a separate `core/live_trading_engine.py` that is a thin wrapper around `PaperTrader` with:
   - Kalshi API client (real API calls, not simulated)
   - Risk circuit breakers (max position size, max daily loss, max drawdown)
   - Audit logging to a separate `live_trades.db` database
   - Alert dispatch to a separate `[LIVE]` Discord channel
5. **Week 3:** Add a canary deployment strategy: the first live trade should be a $1 position. If it settles correctly, increase to $10. Then $100. This is automated by the `live_trading_engine.py`.
