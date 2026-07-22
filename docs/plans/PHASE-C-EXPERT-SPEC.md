# Phase C: Expert Spec — Alert Pipeline & Production Hardening

**Date:** 2026-07-22  
**Author:** Gilfoyle (Technical Lead)  
**Status:** Draft — spec for implementation

---

## Table of Contents

1. [Flask App Consolidation](#1-flask-app-consolidation)
2. [DB Connection Standardization](#2-db-connection-standardization)
3. [Structured Logging](#3-structured-logging)
4. [Alert Pipeline](#4-alert-pipeline)
5. [Live Trading Pipeline](#5-live-trading-pipeline)
6. [Kalshi API Execution](#6-kalshi-api-execution)
7. [Real P&L Tracking](#7-real-pnl-tracking)
8. [Cron Cleanup](#8-cron-cleanup)

---

## 1. Flask App Consolidation

### Current State

There are **three conflicting Flask applications** running in the same process:

| App | File | Port | Purpose |
|---|---|---|---|
| Main App | `app.py` | 10000 (gunicorn) | Production HTTP API — METAR, Kalshi, debug, trading dashboard |
| Old Dashboard | `core/dashboard.py` | 5005 (self-contained) | Standalone Flask app with `/health`, `/api/predictions`, `/dashboard` |
| Confidence Tracker | `core/confidence_dashboard.py` | None (pure analytics) | No Flask dependency — analytics library only |

The old dashboard (`core/dashboard.py`) is a **self-contained Flask app** with its own `DashboardApp` class that creates a Flask app in `create_app()`. It listens on port 5005 and serves:
- `GET /health`
- `GET /api/predictions/<station>`
- `GET /api/discrepancies`
- `GET /dashboard` (HTML + Plotly)

The main `app.py` registers the **Trading Dashboard Blueprint** at `/trading` and explicitly marks the old dashboards as deprecated. The old dashboard routes are **not mounted** in the main app.

### Problem

- Two separate Flask apps fighting for the same import namespace
- `core/dashboard.py` has its own `logging.basicConfig()` call that conflicts with the structured logger
- `core/dashboard.py` directly imports `core/kalshi_price_fetcher.py` with a try/except ImportError pattern — fragile
- `core/dashboard.py` uses `print()` for output instead of structured logging
- Station prediction data comes from `daily_stats` table, which may not be populated depending on the data collection pipeline

### Recommendation

**DO NOT** merge the old dashboard into the main app. Instead:

1. **Deprecate `core/dashboard.py` entirely** — the Trading Dashboard Blueprint at `/trading` replaces all its functionality
2. **Remove the `DashboardApp` class** — it's a standalone app that should never have been part of the core module
3. **Keep `core/confidence_dashboard.py`** — it's a pure analytics library with no Flask dependency, useful for P&L tracking
4. **Remove the `app.py` import of `DashboardApp`** (if any) — it's not imported in the current codebase
5. **Archive the file** or add a `# DEPRECATED — use /trading dashboard instead` header

### Migration Path

| Step | Action | Risk |
|---|---|---|
| 1 | Confirm Trading Dashboard covers all old dashboard endpoints | Low |
| 2 | Remove `core/dashboard.py` from the file tree | Low |
| 3 | Update any references in scripts or docs | Low |
| 4 | Remove the `DashboardApp` run configuration from `render.yaml` (port 5005) | None (not in render.yaml) |

---

## 2. DB Connection Standardization

### Current State

The codebase has **15+ independent `CREATE TABLE IF NOT EXISTS` statements** scattered across modules. Each module manages its own SQLite connection with its own PRAGMA settings.

**Phase 20.3 introduced `core/sqlite_utils.py`** which provides:
- `get_sqlite_connection()` — standard connection with `WAL`, `busy_timeout=5000`, `foreign_keys=ON`
- `get_readonly_sqlite_connection()` — read-only URI connection
- `get_pooled_connection()` / `close_pooled_connection()` — connection pool
- `ensure_schema()` — centralized schema creation via `core/db_schema.py`
- `run_migration_test()` — automated migration testing

**`core/db_schema.py`** (version 1.0.0) provides:
- `TABLES` dict — all CREATE TABLE statements by name
- `ensure_all_tables()` — creates all registered tables
- `get_table_names()` — returns registered table names

### Audit: Modules still using direct SQLite

| Module | Connection Pattern | Uses sqlite_utils? |
|---|---|---|
| `core/paper_trader.py` | `from .sqlite_utils import get_sqlite_connection` | ✅ Yes |
| `core/live_trader.py` | `from .sqlite_utils import get_sqlite_connection` | ✅ Yes |
| `app.py` | Direct `sqlite3.connect()` with manual PRAGMAs | ❌ No |
| `core/dashboard.py` | Direct `sqlite3.connect()` in `DashboardApp` | ❌ No (deprecated) |
| `core/metar_monitor.py` | Direct `sqlite3.connect()` | ❌ No |
| `core/kalshi_monitor.py` | Various connection patterns | ❌ No |
| `core/alert_dispatcher.py` | No DB usage | ✅ N/A |
| `core/alert_builder.py` | No DB usage | ✅ N/A |
| `core/structured_logger.py` | No DB usage | ✅ N/A |
| `core/db_health_monitor.py` | Direct `sqlite3.connect()` | ❌ No |
| `core/heartbeat_monitor.py` | No DB usage | ✅ N/A |

### Recommendation

1. **Migrate `app.py`** to use `get_sqlite_connection()` and `ensure_schema()` instead of hardcoded `CREATE TABLE IF NOT EXISTS`
2. **Migrate `core/metar_monitor.py`** to use `get_sqlite_connection()` for all database operations
3. **Migrate `core/kalshi_monitor.py`** sub-modules to use `get_sqlite_connection()` 
4. **Migrate `core/db_health_monitor.py`** to use `get_sqlite_connection()` when running health checks (it has its own `DatabaseHealthCheck` class — this should delegate to sqlite_utils)
5. **Register all table schemas** in `core/db_schema.py` — audit the codebase for any `CREATE TABLE` statements not in the schema registry
6. **Remove** all standalone `CREATE TABLE IF NOT EXISTS` calls after migration is complete

### Schema Registry Status

Currently registered in `db_schema.py`:
- `trades` — trade recording
- `decision_output_log` — decision logging

Missing (need to add):
- `daily_stats` — used by old dashboard
- `positions` — used by live trader
- All alert-related tables (alerts, transition_events, settlement_epochs)
- METAR observation tables
- Paper trading tables

---

## 3. Structured Logging

### Current State

**`core/structured_logger.py`** provides:
- `StructuredFormatter` — JSON-line formatter
- `configure_root_logger()` — one-time root logger configuration
- `get_logger(name)` — module-level logger factory
- `LogContext` — context manager for adding structured fields
- `log_print()` — print() replacement for migration

### Modules using structured logging

| Module | Uses `get_logger()`? | Still uses `print()`? |
|---|---|---|
| `core/structured_logger.py` | ✅ Yes | ❌ No |
| `core/db_health_monitor.py` | ✅ Yes | ❌ No |
| `core/heartbeat_monitor.py` | ✅ Yes | ❌ No |
| `core/alert_dispatcher.py` | ❌ No — uses `logging.getLogger()` | ❌ No |
| `core/alert_builder.py` | ❌ No — uses `logging.getLogger()` | ❌ No |
| `core/paper_trader.py` | ❌ No — uses `logging.getLogger()` | ❌ No |
| `core/live_trader.py` | ❌ No — uses `logging.getLogger()` | ❌ No |
| `app.py` | ❌ No — uses `app.logger` | ⚠️ Uses `print()` for startup messages |
| `core/dashboard.py` | ❌ No | ⚠️ Uses `print()` in self-tests |
| `core/confidence_dashboard.py` | ❌ No | ❌ No |

### Recommendation

1. **Replace all `logging.getLogger()` with `get_logger()`** in:
   - `core/alert_dispatcher.py`
   - `core/alert_builder.py`
   - `core/paper_trader.py`
   - `core/live_trader.py`
   - `core/confidence_dashboard.py`
   - `core/kalshi_monitor.py` and sub-modules

2. **Replace `app.logger` calls** in `app.py` with `get_logger("app")` — but keep the `app.logger` for Flask-specific route logging (Werkzeug handles that)

3. **Replace `print()` calls** in `app.py` with `log_print()` or `get_logger("app").info()`:
   - `app.py` has `print()` calls in `detect_illegal_cross_layer_imports()`, `_refresh_metar_if_stale()`, `_start_periodic_refresh()`, and security boot messages
   - These should use `get_logger("app.startup")` or `log_print()`

4. **Standardize event_id naming**:
   - Use `snake_case` for all event_ids
   - Prefix with module name: `metar_poll`, `kalshi_hydrate`, `alert_dispatch`, `trade_execute`
   - Document standard event_ids in a shared constant file

---

## 4. Alert Pipeline

### Current State

#### Webhook Configuration (Fixed in Phase C)

**Root cause identified:** The Discord webhook env vars were never loaded in production because:
- `.env.webhooks` exists with valid `WEBHOOK_PROD`, `WEBHOOK_DEV`, `WEBHOOK_SBOX` URLs
- `~/.bashrc` sources `.env.webhooks` and exports them — but only in interactive shells
- Render's gunicorn process runs in a non-interactive shell

**Fix applied:** Added to `scripts/bootstrap.sh` (runs before gunicorn):
```bash
if [ -f .env.webhooks ]; then
  . .env.webhooks
  export DISCORD_WEBHOOK_DEV="$WEBHOOK_DEV"
  export DISCORD_WEBHOOK_PROD="$WEBHOOK_PROD"
  export DISCORD_WEBHOOK_SBOX="$WEBHOOK_SBOX"
fi
```

#### Alert Pipeline Architecture

```
METAR Polling → settlement_up transition → market evaluation → alert_builder → alert_dispatcher → Discord
```

| Component | File | Role |
|---|---|---|
| `_poll_once` / `fetch_window` | `core/metar_monitor.py` | Detects bucket changes → settlement_up |
| Market evaluation | `core/kalshi_monitor.py` | Maps station+market to eligible markets |
| `build_paper_trade_alert()` | `core/alert_builder.py` | Builds Discord embed, applies cooldown, hard filters, opportunity grade |
| `format_alert_for_discord()` | `core/alert_builder.py` | Wraps embed into Discord payload |
| `dispatch_alert()` | `core/alert_dispatcher.py` | HTTP POST to Discord webhook with retry |
| `AlertCooldown` | `core/alert_builder.py` | Per-station, per-lane frequency throttle (4h/8h/12h) |

#### Alert Filtering (Hard Filters)

`check_alert_filter()` blocks emission if:
- Edge < 0 (negative edge)
- Edge < +10 percentage points
- Trade confidence < 50%
- Opportunity Grade is D or F

#### Webhook URLs

The `dispatch_alert()` function resolves webhook URLs via:
```python
WEBHOOK_ENV_VARS = {
    "PROD": "DISCORD_WEBHOOK_PROD",
    "DEV": "DISCORD_WEBHOOK_DEV",
    "SBOX": "DISCORD_WEBHOOK_SBOX",
}
```

Instance is auto-detected from `PAPER_TRADING_INSTANCE` env var (defaults to `DEV`).

### Recommendation

1. **Fix is deployed** — webhook env vars now sourced in `bootstrap.sh` ✅
2. **Add validation** — the bootstrap.sh webhook sourcing should print a warning if `.env.webhooks` is missing but `DISCORD_WEBHOOK_*` env vars are not set by Render secrets
3. **Add `DISCORD_WEBHOOK_*` to Render secret env vars** as a fallback (in case `.env.webhooks` is not present in the deployment)
4. **Consider consolidating** `WEBHOOK_BASE_URL` (used by heartbeat) and `DISCORD_WEBHOOK_*` (used by alerts) — they serve different purposes but share the same underlying webhook infrastructure
5. **Alert retry queue** — `dispatch_alert()` handles 429 rate limits but doesn't retry. Add a simple retry with exponential backoff (max 3 retries, 1s/2s/4s delays)

---

## 5. Live Trading Pipeline

### Current State

#### Execution Loop

The live trading pipeline is defined in `core/live_trader.py`:

```python
class LiveTrader:
    def execute_real_trade(self, station, market_type, direction, quantity, price):
        # 1. Check kill switches
        kill_switch_active, reasons = self._check_kill_switches()
        # 2. Check risk limits
        risk_ok, risk_msg = self._check_risk_limits(station, market_type, quantity, price)
        # 3. Place real trade via Kalshi API
        from .order_manager import process_ladder_transition
        return process_ladder_transition(station, direction, quantity, price)
```

#### Risk Controls

| Check | Where | What it does |
|---|---|---|
| Kill switch | `_check_kill_switches()` → `risk_controls.check_kill_switches()` | Blocks all trading if P&L exceeds threshold, drawdown too high, or manual kill switch set |
| Position limits | `_check_risk_limits()` → `position_sizing.validate_trade_size()` | Validates trade size against max position, max exposure per station, max total exposure |
| Stop loss | Implicit in kill switch | Not implemented as automatic stop-loss — only periodic kill switch check |

#### Trade Execution

`process_ladder_transition()` in `core/order_manager.py` handles:
- Ladder transition logic (buy/sell based on bucket changes)
- Position sizing
- Order placement via Kalshi API

### Gaps

1. **No automatic position liquidation** — if a trade goes against the system, no stop-loss closes it
2. **No order confirmation** — `execute_real_trade()` assumes the order fills immediately. Kalshi's auction-based markets may have delayed fills
3. **No trade journaling** — the live trader doesn't record trades to a journal DB (only paper trader does)
4. **No pre-trade price validation** — the `price` parameter is used directly; no check that the price is within the current market spread

### Recommendation

1. **Add trade journaling** to `execute_real_trade()` — record every live trade to `trade_journal.db` with:
   - `trade_uuid`, `station`, `market_type`, `direction`, `entry_price`, `quantity`, `fill_price`, `status`, `timestamp`
   - Use `get_sqlite_connection()` from `sqlite_utils.py`

2. **Add order confirmation loop** — after placing an order, poll the Kalshi API for fill status (up to 30s, every 2s). If the order doesn't fill, mark as `pending` and alert

3. **Add automatic stop-loss** — configure a max loss per trade (e.g., 10% of position value). If the market moves against the position by more than the stop-loss, submit a market order to close it

4. **Add pre-trade price validation** — check that the `price` is within 5% of the current mid-market price. If not, reject the trade and log a warning

5. **Add `DISCORD_WEBHOOK_*` alerting** on:
   - Trade execution (filled)
   - Trade rejection (risk control)
   - Stop-loss triggered
   - Kill switch engaged/disengaged

---

## 6. Kalshi API Execution

### Current State

The Kalshi API integration is handled by `core/kalshi_monitor.py` (facade) which delegates to:

| Sub-module | File | Role |
|---|---|---|
| `market_monitor.py` | `core/market_monitor.py` | Market discovery, station mapping, series discovery, hydration |
| `price_fetcher.py` | `core/price_fetcher.py` | Kalshi API connectivity, rate limiting, public GET methods |
| `order_manager.py` | `core/order_manager.py` | Execution domain, ladder transitions, composed alerts, signal/market cache |

#### Execution Domain

The execution domain is controlled by `_kalshi_execution_domain`:
- `"observability"` — read-only, no trades
- `"paper"` — simulated trades (no API calls)
- `"live"` — real API calls, real money

Set via `KALSHI_LIVE_DOMAIN` env var:
- Production: not set → defaults to `observability` (or `paper` for paper trading)
- Staging: `"staging"` — no real money

#### Real Order Placement

`process_ladder_transition()` in `order_manager.py` handles:
1. Ladder state evaluation
2. Price determination from ladder cache
3. Position sizing from config
4. Order dispatch (via Kalshi API or paper simulation)

### Gaps

1. **No `KALSHI_LIVE_DOMAIN=live` configuration** in `render.yaml` — live trading is not enabled in production
2. **No API retry logic** — Kalshi API calls may fail due to rate limiting or network issues
3. **No API key validation** — the Kalshi API key/secret are set as Render secret env vars but never validated at startup
4. **KALSHI_API_KEY and KALSHI_API_SECRET** are `sync: false` in `render.yaml` — meaning they must be set manually in the Render dashboard

### Recommendation

1. **Add Kalshi API connectivity validation** to `scripts/bootstrap.sh`:
   ```bash
   if [ "${KALSHI_LIVE_DOMAIN:-}" = "live" ]; then
     python3 -c "
   import os, sys
   sys.path.insert(0, '.')
   from core.price_fetcher import _kalshi_public_get
   try:
       result = _kalshi_public_get('/v1/markets')
       print(f'Kalshi API: OK ({len(result.get(\"markets\", []))} markets)')
   except Exception as e:
       print(f'Kalshi API: FAILED ({e})')
       sys.exit(1)
   "
   fi
   ```

2. **Add API retry with exponential backoff** to `price_fetcher.py`:
   - 3 retries
   - 1s / 2s / 4s delays
   - Only retry on 429 (rate limit) and 5xx (server errors)
   - Do NOT retry on 401 (auth failure) or 400 (bad request)

3. **Add `KALSHI_LIVE_DOMAIN` to `render.yaml`** with `sync: false` for production:
   ```yaml
   - key: KALSHI_LIVE_DOMAIN
     sync: false
   ```

4. **Add startup order validation** — if `KALSHI_LIVE_DOMAIN=live`, validate that the API key can place a small test order (e.g., buy 1 contract at market price, immediately cancel). If the order fails, block trading and alert.

---

## 7. Real P&L Tracking

### Current State

#### Paper Trading P&L

`core/paper_trader.py` delegates to `core/pnl_tracking.py`:
- `process_settlements_for_date()` — settlement processing
- `daily_reconciliation()` — daily P&L recalc
- `calculate_calibration_metrics_for_date()` — calibration metrics
- `get_current_balance()` — current balance
- `get_version_performance()` — version-tagged performance
- `generate_calibration_report()` — full report
- `compute_sharpe()` — Sharpe ratio

#### Live Trading P&L

`core/live_trader.py` does **not** have P&L tracking — it delegates to `order_manager.process_ladder_transition()` for trade execution but doesn't record trades to a journal.

The Trading Dashboard (`core/trading_dashboard/routes.py`) reads from:
- `PAPER_DB_PATH = data/paper_trading.db`
- `JOURNAL_DB_PATH = data/trade_journal.db`

#### Confidence Tracker

`core/confidence_dashboard.py` provides `ConfidenceTracker`:
- Running p-value (binomial test vs 50%)
- Cumulative P&L
- Rolling Sharpe ratio
- Rolling win rate
- Current and maximum drawdown
- Monte Carlo simulation
- Alert threshold checking

### Gaps

1. **Live trades are not recorded** to `trade_journal.db` — no P&L tracking for live trading
2. **No settlement reconciliation** for live trades — `process_settlements_for_date()` is only used by paper trader
3. **No trade journal DB schema** in `db_schema.py` — the journal DB is created by the dashboard module's own SQL
4. **No P&L tracking for composed alerts** — alerts that fire but don't result in trades are not tracked

### Recommendation

1. **Add trade journal schema** to `core/db_schema.py`:
   ```python
   TABLES["trade_journal"] = """
   CREATE TABLE IF NOT EXISTS trade_journal (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       trade_uuid TEXT UNIQUE NOT NULL,
       station TEXT NOT NULL,
       market_type TEXT NOT NULL,
       direction TEXT NOT NULL,
       entry_price REAL NOT NULL,
       quantity INTEGER NOT NULL,
       fill_price REAL,
       exit_price REAL,
       pnl REAL,
       trade_type TEXT NOT NULL,       -- 'paper', 'live', 'composed_alert'
       trade_version TEXT NOT NULL,
       instance TEXT NOT NULL,
       trade_date TEXT NOT NULL,
       settled_at TEXT,
       created_at TEXT DEFAULT (datetime('now')),
       updated_at TEXT DEFAULT (datetime('now'))
   )
   """
   ```

2. **Record every live trade** in `core/live_trader.py`:
   - After `execute_real_trade()` completes, insert a row into `trade_journal.db`
   - Use `get_sqlite_connection()` from `sqlite_utils.py`

3. **Add settlement reconciliation** for live trades:
   - Every 15 minutes, reconcile open positions against Kalshi settlement prices
   - Update `pnl` and `exit_price` for settled positions
   - Log settlement events to structured logger

4. **Wire `ConfidenceTracker`** into the Trading Dashboard:
   - Initialize `ConfidenceTracker` with trade history from `trade_journal.db`
   - Expose `get_snapshot()` via `/trading/api/confidence` endpoint
   - Display drawdown, Sharpe, p-value, and Monte Carlo on the dashboard

5. **Add P&L-based kill switch**:
   - If cumulative P&L drops below configurable threshold (e.g., -20% of initial balance), automatically engage kill switch
   - Alert via Discord webhook
   - Log to structured logger

---

## 8. Cron Cleanup

### Current State

#### OpenClaw Cron Jobs (from DEPLOYMENT.md)

| Job | Schedule | Model | Status |
|---|---|---|---|
| NWP Forecast Collection | 0 6 * * * UTC | `qwen/qwen3-coder-plus` | OK |
| Forecast Disagreement | 0 7 * * * ET | `qwen/qwen3-coder-plus` | Retry on transient |

#### Application-Level Scheduled Tasks

| Task | Schedule | Description |
|---|---|---|
| METAR collection | Every 30 min | Live weather observations |
| NWP collection | Daily 06:00 UTC | GFS model forecasts |
| Kalshi polling | Every 15 min | Market price discovery |
| Forecast disagreement | Daily 07:00 ET | GFS vs NWS divergence |

#### Production Monitoring

- **Health check endpoint:** `/healthz` (polled by Render every 5s)
- **Bootstrap checks:** Python version, dependencies, env vars, data dirs, SQLite, syntax, station registry, webhooks
- **Background health monitor:** `core/db_health_monitor.py` — 5 min interval, PRAGMA integrity_check, auto-recovery
- **Background heartbeat monitor:** `core/heartbeat_monitor.py` — 5 min interval, self-health + webhook delivery test

### Gaps

1. **No alert pipeline monitoring cron** — no cron job verifies that alerts are being delivered
2. **No P&L monitoring cron** — no cron job checks P&L thresholds and alerts
3. **No trade execution cron** — the live trading pipeline has no scheduled execution loop
4. **No log rotation** — structured JSON logs grow unbounded in the Render log stream
5. **No disk usage monitoring** — SQLite databases grow unbounded; no VACUUM schedule

### Recommendation

1. **Add alert pipeline health check cron** (hourly):
   - Check that `DISCORD_WEBHOOK_*` env vars are set
   - Check that `dispatch_alert()` returns success for a test payload
   - Alert if webhook delivery fails
   - Model: `qwen/qwen3-coder-plus` (cheap, good enough)

2. **Add P&L monitoring cron** (daily at 23:00 UTC):
   - Check cumulative P&L against kill switch threshold
   - Check rolling Sharpe > 0.5
   - Check drawdown < 8%
   - Alert via Discord if any threshold breached
   - Model: `qwen/qwen3-coder-plus`

3. **Add trade execution cron** (every 15 min during market hours):
   - Evaluate all stations for trading opportunities
   - Execute trades via `LiveTrader.execute_real_trade()`
   - Record to trade journal
   - Model: `qwen/qwen3-coder-plus`

4. **Add SQLite maintenance cron** (weekly, Sun 04:00 UTC):
   - `VACUUM` all databases
   - `REINDEX` all indexes
   - Check file sizes and log growth
   - Model: none (pure shell script)

5. **Add station registry validation cron** (daily at 05:00 UTC):
   - Validate station registry
   - Check for stale stations (no observation in 24h)
   - Log missing stations
   - Model: `qwen/qwen3-coder-plus`

6. **Remove unused cron jobs** from OpenClaw schedule:
   - The old `NWP Forecast Collection` and `Forecast Disagreement` jobs use `qwen/qwen3-coder-plus` — verify they are still needed or consolidate into the new cron structure

### Cron Configuration Template

```yaml
# Template for new cron jobs
# Add via openclaw cron add

# --- Alert Pipeline Health Check ---
# Schedule: hourly
# Payload: curl -s https://kalshi-metar-monitor.onrender.com/healthz | grep -q '"healthy":true'
# Delivery: announce on failure

# --- P&L Monitoring ---
# Schedule: 0 23 * * *
# Payload: python3 scripts/check_pnl.py --threshold -0.20
# Delivery: announce on breach

# --- Trade Execution ---
# Schedule: */15 9-16 * * 1-5
# Payload: python3 scripts/execute_trades.py
# Delivery: announce on trade

# --- SQLite Maintenance ---
# Schedule: 0 4 * * 0
# Payload: python3 scripts/vacuum_dbs.py
# Delivery: announce on failure
```

---

## Appendix: Implementation Priority

| Priority | Area | Effort | Impact | Risk |
|---|---|---|---|---|
| **P0** | Alert Pipeline fix (DONE) | 5 min | Critical — alerts now work in prod | None |
| **P1** | Add trade journal schema + live P&L tracking | 1 day | High — enables real P&L visibility | Low |
| **P1** | Kalshi API validation in bootstrap | 2 hours | High — fails fast on bad config | Low |
| **P2** | Structured logging migration | 1 day | Medium — better observability | Low |
| **P2** | DB connection standardization | 2 days | Medium — reduces connection bugs | Medium |
| **P3** | Flask app consolidation (deprecate old dashboard) | 2 hours | Low — no functional impact | Low |
| **P3** | Cron cleanup | 1 day | Medium — automates monitoring | Low |
| **P3** | Alert retry queue | 4 hours | Medium — handles rate limits | Low |
| **P4** | Live trading pipeline automation | 3 days | High — enables real trading | High |
| **P4** | Automatic stop-loss | 1 day | High — risk management | Medium |

---

*End of Phase C Expert Spec. This document should be read alongside the codebase and the Phase C implementation plan.*