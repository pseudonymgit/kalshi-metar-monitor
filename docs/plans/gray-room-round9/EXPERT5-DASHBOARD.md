# Gray Room Round 9 — Expert 5: Full-Stack Dashboard & UX Design

**Domain:** Full-Stack Development, Data Visualization, Trader-Facing UX
**Review Date:** 2026-07-22
**Status:** Independent analysis — no prior Gray Room findings referenced

---

## ERRORS (13)

### E1: `market_prob` hardcoded to 0.5 everywhere — dashboard shows no real market data
**What:** `dashboard.py` line 205: `market_prob = 0.5` and used as the universal default in `discrepancy_table()` line 257.
**Where:** `core/dashboard.py` lines 205, 257, 408
**Why wrong:** The dashboard's entire point is "Model PMF vs Market Odds." If market odds are always 0.5, the chart is meaningless. The discrepancy table, flagged rows, and all "model vs market" comparisons are an illusion. The dashboard says "auto-refreshed on load" but never refreshes market data.
**Spec to fix:** Wire `_get_market_price()` from `paper_trading_engine.py` into `DashboardApp`. The Kalshi price fetcher (`kalshi_price_fetcher.py`) already exists with thread-safe caching. Add a `_market_prob_cache` to `DashboardApp` that refreshes every 60s. Pass real market prices into `station_predictions()` and `discrepancy_table()`. Remove the `0.5` default; if no market data, return an explicit error state rather than a fake number.

### E2: No real-time data refresh — "auto-refreshed on load" is a lie
**What:** The dashboard HTML (line 143) says "auto-refreshed on load" but the page only fetches data once on `DOMContentLoaded`. There is no `setInterval`, no WebSocket, no SSE, no polling mechanism.
**Where:** `core/dashboard.py` lines 140-143 (HTML comment), lines 148-185 (JavaScript that runs once)
**Why wrong:** A trader dashboard that doesn't update is a screenshot. The user must manually refresh the browser tab. For a live trading engine, stale data is worse than no data — it can mislead decisions.
**Spec to fix:** Add a 30-second `setInterval` in the browser JS that re-fetches `/health` and `/api/discrepancies` and updates the DOM in-place. Add a visual "last updated" timer. For production, implement Server-Sent Events (SSE) endpoint at `/api/stream` that pushes updates on a cron tick.

### E3: `calibration_dashboard.py` silently generates random data when DB is unavailable
**What:** `fetch_paper_trading_metrics()` catches `Exception` and falls back to `random.gauss()`, `random.randint()`, `random.uniform()` to generate completely synthetic performance data.
**Where:** `core/calibration_dashboard.py` lines 123-157
**Why wrong:** The dashboard will show beautiful charts and metrics that are 100% fake with no warning banner. A trader could act on imaginary P&L data. The `print(f"Simulating with dummy data due to: {e}")` goes to stdout, not the UI.
**Spec to fix:** Remove the random fallback entirely. If the DB is unavailable, the dashboard should show a clear error state: "⚠️ Database connection failed — showing no data." Add a red banner at the top of the HTML. Return empty charts with an error annotation. Never silently simulate.

### E4: Hardcoded absolute filesystem paths in calibration dashboard
**What:** `PAPER_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/paper_trading.db"` and `METAR_DB_PATH = "/home/gaddams/.openclaw-next/..."` — these are hardcoded absolute paths tied to a specific dev environment.
**Where:** `core/calibration_dashboard.py` lines 19-20
**Why wrong:** This will crash on Render, on any other machine, or if the workspace moves. The `dashboard.py` version uses `REPO_ROOT / "data" / ...` correctly (relative to file location). The calibration dashboard uses two different hardcoded root paths that will break.
**Spec to fix:** Use `Path(__file__).resolve().parents[1]` like `dashboard.py` does. Remove the `/home/...` paths. Read DB paths from environment variables with fallback to project-relative defaults.

### E5: Two standalone Flask apps with no unified routing — will crash on Render
**What:** `dashboard.py` runs on `:5005` and `calibration_dashboard.py` runs on `:8086`. Each creates its own `Flask(__name__)` with overlapping routes (`/` in both).
**Where:** `core/dashboard.py` line 264, `core/calibration_dashboard.py` line 49
**Why wrong:** Render (and any production deployment) serves a single process on a single port. These two apps cannot run simultaneously. The `/` route is defined in both — if merged, one would overwrite the other silently. The `app.py` file at the repo root is the intended entry point but doesn't import either dashboard.
**Spec to fix:** Create a unified Flask app factory at `app.py` that registers both dashboards as Blueprints. `DashboardApp` should be refactored to a Flask Blueprint. `calibration_dashboard.py` should be refactored identically. Both should register under prefix paths (`/model`, `/calibration`). The factory should be the single WSGI entry point for gunicorn.

### E6: Dashboard has zero P&L or position data — completely disconnected from trading engine
**What:** `dashboard.py` has no routes for P&L, positions, trade history, or account balance. The `/api/predictions/<station>` endpoint shows weather predictions only. The `/api/discrepancies` endpoint shows model-vs-market deltas but never what the engine actually did.
**Where:** `core/dashboard.py` — entire file, no P&L data anywhere
**Why wrong:** This is a "weather dashboard," not a "trading dashboard." The trader needs to see: what positions are open, what's the P&L, what's the risk exposure, what trades executed today. Without this, the dashboard is an academic exercise.
**Spec to fix:** Add API endpoints:
- `GET /api/positions` — open positions with mark-to-market P&L
- `GET /api/pnl` — daily, cumulative, and per-station P&L
- `GET /api/trades/recent` — last 50 trades from `trade_journal`
- `GET /api/account` — balance, drawdown, exposure
- `GET /api/risk` — risk state, kill switch status, alerts
Wire these into the dashboard HTML with dedicated panels.

### E7: Huge inline HTML template in Python file — zero maintainability
**What:** ~200 lines of HTML/CSS/JS defined as a triple-quoted string inside `dashboard.py` (lines 50-245). Same anti-pattern in `calibration_dashboard.py` (~200 lines inline).
**Where:** `core/dashboard.py` lines 50-245, `core/calibration_dashboard.py` lines 337-506
**Why wrong:** Inline HTML means: no syntax highlighting, no linting, no template inheritance, no frontend tooling, no way to split into partials. Every dashboard change requires editing a Python string. This is a maintenance nightmare.
**Spec to fix:** Move all HTML templates to `templates/` directory. Use Jinja2 template inheritance. Create a base layout (`templates/base.html`) with header, sidebar, footer, and script loading. Each dashboard page extends the base. Use `render_template()` instead of `render_template_string()`.

### E8: No loading states, empty states, or error states in the UI
**What:** The dashboard HTML shows "Loading health..." for the health bar and has no `<tbody>` content until JS fetches data. If the fetch fails, the page stays on "Loading..." forever or shows a blank table.
**Where:** `core/dashboard.py` lines 148-185 (JavaScript)
**Why wrong:** Silent failures are the worst UX. If the API is down, the trader sees a blank page with no indication of what's wrong. There's no error handling on any `fetch()` call — no `.catch()`, no timeout, no retry.
**Spec to fix:** Add to every fetch:
- Loading spinner (CSS-only, no extra dependencies)
- Error state: show red banner with error message
- Empty state: show "No data available" message
- Timeout: abort after 10s and show timeout error
- Retry: exponential backoff up to 3 retries
Wrap all `fetch()` calls in a reusable `safeFetch()` function with these behaviors.

### E9: No authentication or authorization — any dashboard is fully public
**What:** Every dashboard endpoint is `methods=["GET"]` with no auth middleware. No API keys, no session tokens, no IP allowlisting.
**Where:** All three dashboard files, all routes
**Why wrong:** If deployed to Render (even on a private URL), the dashboard exposes: live P&L, position details, trade history, and system health. This is sensitive financial data. A single URL leak exposes everything.
**Spec to fix:** Add a `before_request` handler that checks for a configurable API key via header (`X-API-Key`) or query param (`?key=...`). Use `os.getenv("DASHBOARD_API_KEY")` for the key. For development, allow a `DASHBOARD_INSECURE=true` env var to bypass auth. Add rate limiting (100 req/min per IP).

### E10: The calibration dashboard plots are fake — confidence calibration uses `random.uniform()`
**What:** `create_confidence_calibration_plot()` generates calibration data from `random.uniform()` with no real data input. The confidence calibration plot is a complete fabrication.
**Where:** `core/calibration_dashboard.py` lines 175-214
**Why wrong:** The confidence calibration plot is one of the most important diagnostics for a trading system. Showing fake data is worse than showing nothing — it gives a false sense of model reliability. The comment says "For now, create a simulated calibration plot" — this is production code, not a prototype.
**Spec to fix:** Remove the fake data generation. Query actual trades from `paper_trading.db` or `trade_journal.db` to compute real calibration. Group trades by confidence decile (0.0-0.1, 0.1-0.2, ..., 0.9-1.0), compute actual accuracy in each bucket, and plot those. If insufficient data, show an empty chart with "Need N trades for calibration" annotation.

### E11: Dashboard uses conflicting Plotly CDN versions
**What:** `dashboard.py` loads Plotly 2.35.2 from CDN. `calibration_dashboard.py` loads Plotly 2.24.1 from CDN. When these are merged into a single page, the user will get whichever loads second.
**Where:** `core/dashboard.py` line 53, `core/calibration_dashboard.py` line 347
**Why wrong:** Different minor versions may have different APIs. The calibration dashboard uses `import plotly.graph_objects as go` server-side for rendering, then also loads the client-side library — this is redundant because the charts are rendered server-side as HTML. The client-side CDN load is unnecessary.
**Spec to fix:** Use a single, pinned version (2.35.2 is fine). Remove the client-side CDN from `calibration_dashboard.py` since charts are server-rendered. For `dashboard.py` where client-side `Plotly.newPlot()` is used, keep the CDN. Pin to exact version in both.

### E12: Confidence dashboard has no Flask integration — it's a standalone analytics module with no web endpoint
**What:** `confidence_dashboard.py` (ConfidenceTracker) is a pure analytics class with no Flask routes, no API endpoints, and no web rendering. It's never imported by any dashboard.
**Where:** `core/confidence_dashboard.py` — entire file, no Flask integration
**Why wrong:** The `ConfidenceTracker` has valuable metrics (p-value, Sharpe, drawdown, Monte Carlo) that are exactly what a trader dashboard needs. But it's completely disconnected. The data exists in `trade_journal.db` but no dashboard reads it.
**Spec to fix:** Create a Flask Blueprint at `/api/confidence` that:
- `GET /api/confidence/snapshot` — returns `ConfidenceTracker.get_snapshot()`
- `GET /api/confidence/p-value` — running p-value
- `GET /api/confidence/monte-carlo` — Monte Carlo results
- `GET /api/confidence/alerts` — alert thresholds
Add a "Confidence & Risk" panel to the dashboard HTML that displays these stats.

### E13: `calibration_dashboard.py` creates duplicate `daily_balances` and `positions` tables
**What:** The `_init_paper_db()` method in `paper_trading_engine.py` already creates `daily_balances` and `positions` tables. `calibration_dashboard.py` does not create tables directly, but it duplicates table creation logic across the codebase.
**Where:** `core/paper_trading_engine.py` (multiple `CREATE TABLE IF NOT EXISTS` statements), `core/calibration_dashboard.py` (queries `daily_balances`)
**Why wrong:** The `calibration_dashboard.py` has duplicate `CREATE INDEX` statements (lines 120-121 and 127-128 are exact duplicates). The schema is duplicated across multiple files with no single source of truth. If the schema changes in one place, the other breaks.
**Spec to fix:** Create a single `schema.py` module with all table definitions. Both `paper_trading_engine.py` and `calibration_dashboard.py` should import from there. Remove inline SQL DDL from both files.

---

## IMPROVEMENTS (6)

### I1: Unified Flask app factory with Blueprint architecture
**What to change:** Refactor both dashboards into Flask Blueprints registered on a single app instance. Create `app/dashboards/model_blueprint.py` (from `dashboard.py`) and `app/dashboards/calibration_blueprint.py` (from `calibration_dashboard.py`). The `app.py` entry point imports and registers both.
**Why better:** Single port, single WSGI entry point, shared middleware (auth, CORS, rate limiting), shared template context. No more "which port do I open?" confusion. Gunicorn can serve everything on one process.
**Effort:** 2-3 days. Mostly refactoring — no new logic.
**Spec:** Create `app/dashboards/__init__.py` with a `register_dashboards(app)` function. Each dashboard module exposes a `create_blueprint()` function. `app.py` calls `register_dashboards(app)` during initialization.

### I2: Add real-time data refresh via Server-Sent Events (SSE)
**What to change:** Add an SSE endpoint at `/api/stream` that pushes dashboard updates every 30 seconds. The browser JS uses `EventSource` to listen for updates and patches the DOM. Fall back to polling `setInterval` if SSE is unavailable.
**Why better:** 30-second polling is wasteful (full HTTP request/response every time). SSE gives a persistent connection with incremental updates. The server controls refresh cadence. The browser can update individual chart traces without re-rendering the entire page.
**Effort:** 1-2 days. Requires Flask SSE support (simple generator route), client-side `EventSource` handler, and Plotly `Plotly.react()` for incremental chart updates.
**Spec:**
- Backend: `@app.route('/api/stream')` returns a streaming response with `text/event-stream` content type. Yields JSON patches every 30s from a background data refresh thread.
- Frontend: `new EventSource('/api/stream')` listens for `message` events. Updates the health bar, discrepancy table, and chart traces in-place using `Plotly.react()`.
- Fallback: `setInterval` polling every 60s if SSE fails.

### I3: Add tradable P&L dashboard with position management
**What to change:** Add a new dashboard panel showing:
- Account summary: balance, P&L today, P&L all-time, max drawdown, Sharpe
- Open positions: station, market, direction, size, entry price, current price, unrealized P&L
- Recent trades: last 50 trades with outcome, P&L, confidence
- Risk state: kill switch status, daily loss count, consecutive losses
**Why better:** This is the information a trader actually needs. The current dashboard shows weather predictions but nothing about the trading results. The data already exists in `paper_trading.db` and `trade_journal.db` — it just needs queries and visualization.
**Effort:** 2-3 days. Write SQL queries, add API endpoints, build HTML panels and Plotly charts.
**Spec:**
- `GET /api/account` — queries `daily_balances` for latest balance, sums P&L
- `GET /api/positions` — queries `positions` table with `status = 'active'`
- `GET /api/trades/latest` — queries `trade_journal` for last 50 entries
- `GET /api/risk` — consumes `RiskManager.get_summary()` from `risk_controls.py`
- Dashboard panel: 4-column metric cards at top, positions table in middle, trades table below, risk gauge on the right

### I4: Move templates to filesystem with Jinja2 inheritance
**What to change:** Create `templates/base.html` with Bootstrap 5 (or minimal custom CSS) layout, dark theme, nav bar, and footer. Move `dashboard.html` and `calibration_dashboard.html` as separate page templates that extend the base. Use `render_template()`, not `render_template_string()`.
**Why better:** Separates frontend from backend. Enables syntax highlighting, linting, and HTML validation. Base template means consistent header/footer/nav across all pages. Adding a new dashboard page is a one-file change.
**Effort:** 1 day. Pure refactoring — no behavioral change.
**Spec:**
- `templates/base.html` — doctype, head with CDN deps, body with nav bar, content block, footer
- `templates/dashboard.html` — extends base, overrides content block with current dashboard HTML
- `templates/calibration.html` — extends base, overrides content block
- `templates/account.html` — new page for P&L and positions
- Update `dashboard.py` to use `render_template('dashboard.html', ...)`
- Add `template_folder` to Flask app init

### I5: Add trade history and alert feed to the dashboard
**What to change:** Add a "Recent Activity" panel to the dashboard that shows the last 50 journal entries from `trade_journal.db` with color-coded outcomes (green for executed, red for skipped, yellow for error). Add a "Live Alerts" section that displays the last 10 Discord alerts that were dispatched.
**Why better:** Traders need to see what the engine is doing in real time. The `trade_journal` and `alert_dispatcher` already record everything — it just needs a UI. This turns the dashboard from a static weather display into an operational command center.
**Effort:** 1 day. Add a `GET /api/activity` endpoint that queries `trade_journal.get_recent_trades()`. Add a `GET /api/alerts/recent` endpoint that queries the alert history. Build a scrolling table UI.
**Spec:**
- `GET /api/activity` — returns last 50 entries from `trade_journal.get_recent_trades()`
- `GET /api/alerts/recent` — returns last 10 alerts from `alert_retry_queue` or re-query
- UI: scrolling table with color-coded rows (green=executed, red=skipped, yellow=filtered)
- Click row to expand and show full decision context (edge, confidence, market_prob, reasons)

### I6: Add automated recovery dashboard with system health timeline
**What to change:** Add a "System Health" panel showing:
- Uptime tracker (hours since last restart)
- Last N health checks with timestamps
- Database connection status (with last failure time)
- Signal generation status (last successful run, last error)
- Alert delivery success rate (last 100 attempts)
**Why better:** When the trading engine goes silent, the operator needs to know why. A health timeline shows exactly when things broke, what the error was, and whether it recovered. This is critical for a 24/7 automated system.
**Effort:** 1-2 days. Add health check logging to `heartbeat.py` or a new `health_history.db`. Query and display.
**Spec:**
- `GET /api/health/history` — returns last 50 health check results with timestamps
- `GET /api/health/uptime` — returns process uptime, last restart time
- `GET /api/health/alerts` — returns alert delivery success/failure stats
- UI: timeline chart showing health state over time (green=ok, yellow=warning, red=error)

---

## IDEAS (4)

### Idea 1: Trader Position Management UI — close positions from the dashboard
**The idea:** Add a "Close Position" button next to each open position in the positions table. Clicking it sends a `POST /api/positions/{uuid}/close` request that triggers the paper trading engine to close the position and record the P&L. Include a confirmation dialog showing the expected P&L at current market price.
**Expected benefit:** Turns the dashboard from read-only to actionable. The trader can intervene when the engine is making bad bets. Enables manual override of automated decisions.
**Risk:** Accidental clicks could close profitable positions. Need confirmation dialog, undo window (30s), and audit logging. Should be gated behind a "manual override enabled" config flag.
**Validation spec:**
- Add `POST /api/positions/{uuid}/close` endpoint
- Add confirmation modal: "Close KATL HIGH UP? Est. P&L: -$23.50 (loss)"
- Add 30-second undo window with "Undo" button
- Log all manual closes to `trade_journal` with outcome `MANUAL_CLOSE`
- Add `MANUAL_OVERRIDE_ENABLED` env var (default: `False`)
- Test: open 5 positions, close 2, verify P&L recorded correctly

### Idea 2: Correlation and Concentration Risk Heatmap
**The idea:** Build a visual heatmap showing exposure across stations, regions, and clusters. Use a grid where rows are stations and columns are clusters (from `station_registry`). Color intensity represents total USD exposure. Red cells indicate over-concentration (near cluster budget cap). Show city pair exposure as a separate bar chart.
**Expected benefit:** Traders can see at a glance where risk is concentrated. Prevents accidentally exceeding cluster budgets. The `CLUSTER_BUDGET_USD` and `CITY_PAIR_CAP_USD` constants exist but have no visualization.
**Risk:** Heatmap could be misleading if mark-to-market prices are stale. Need to ensure market prices are recent (< 5 min old).
**Validation spec:**
- Build a `GET /api/exposure/heatmap` endpoint returning a 2D array of {station, cluster, exposure_usd, budget_usd, pct_used}
- Render as Plotly heatmap with color scale: green (<50%), yellow (50-80%), red (>80%)
- Add city pair exposure as a horizontal bar chart below the heatmap
- Auto-refresh every 60s
- Test: simulate cluster budget violations and verify red highlighting

### Idea 3: Trade Replay Mode — replay historical trading days
**The idea:** Add a "Replay" mode where the user selects a historical date and the dashboard loads the state of the engine at that time: what signals fired, what trades were placed, what was the P&L, what was the confidence. Step through the day hour by hour or jump to specific events.
**Expected benefit:** Debugging and post-mortem analysis. "Why did we lose $500 on July 15?" — load July 15, see exactly what happened. Essential for improving the engine.
**Risk:** Storing full engine state snapshots is expensive. Need to decide what "state" means and how much to store. Could be limited to the data already in `trade_journal` and `daily_balances`.
**Validation spec:**
- Add `GET /api/replay/dates` — returns list of dates with trading activity
- Add `GET /api/replay/state?date=2026-07-15` — returns: signals generated, trades placed, P&L, positions, risk state
- Add date picker to dashboard UI
- Load historical chart data as a "snapshot" overlay — big red line vs current P&L
- Performance: queries must complete in < 500ms. Index `trade_journal.timestamp_utc` and `daily_balances.date_utc`.

### Idea 4: Anomaly Detection Alert Feed — self-diagnosing dashboard
**The idea:** Add a self-diagnosing alert feed that highlights anomalies in the dashboard data itself: "P&L dropped 5% in 10 minutes — 3σ event", "Discrepancy table has 0 flagged rows — possible data staleness", "Win rate dropped from 65% to 40% — regime change detected". These are statistical alerts on dashboard metrics, not trading signals.
**Expected benefit:** Reduces the need for manual dashboard monitoring. The dashboard tells you when something is wrong with itself. Catches stale data, broken pipelines, and regime changes earlier.
**Risk:** Alert fatigue. Need to tune thresholds carefully. Could generate false positives during normal volatility.
**Validation spec:**
- Add a background thread that checks dashboard metrics every 5 minutes
- Triggers: P&L change > 3σ, discrepancy count = 0 for > 1 hour, data freshness > 5 min stale, API error rate > 10%
- Display as a scrolling alert feed at the top of the dashboard with severity badge
- Send critical anomalies to Discord via `dispatch_alert()`
- Thresholds configurable via env vars: `ANOMALY_PNL_SIGMA=3`, `ANOMALY_STALE_MINUTES=5`

---

## ELEPHANTS (2)

### Elephant 1: Three separate dashboards with zero integration
**What:** The codebase has three dashboard systems:
1. `core/dashboard.py` — Flask app on :5005, shows weather predictions vs market odds
2. `core/calibration_dashboard.py` — Flask app on :8086, shows paper trading metrics
3. `core/confidence_dashboard.py` — pure analytics, no Flask, no web UI

None of them import, reference, or communicate with each other. `confidence_dashboard.py` has the most valuable trader metrics (Sharpe, drawdown, p-value, Monte Carlo) but it's never served by any web endpoint. The `calibration_dashboard.py` generates fake calibration data. The `dashboard.py` shows predictions but no trading results.

**Why matters:** A trader needs a single pane of glass. Currently they would need three browser tabs, two of which are on different ports, and none of them show the full picture. The `ConfidenceTracker` class has the analytics the dashboard needs, but nobody wired it up. The data is in the databases but the dashboards don't query it.

**What happens if ignored:** The trader dashboard will remain a toy. Every review cycle will find the same issues. New team members will add yet another dashboard file instead of consolidating. The system will accumulate more dashboard fragments until it's unmanageable.

**Spec for resolution:**
1. **Unify architecture:** Create a `dashboards/` package with a single Flask Blueprint-based app. Each dashboard becomes a Blueprint registered under a prefix (`/model`, `/performance`, `/risk`).
2. **Create a DataService layer:** A single `DashboardDataService` class that queries all databases (paper_trading.db, trade_journal.db, metar_backfill.db) and returns unified data objects. Each Blueprint calls this service instead of its own DB queries.
3. **Wire ConfidenceTracker:** Create a `/api/risk` Blueprint that instantiates `ConfidenceTracker` from `trade_journal.db` data and serves `get_snapshot()` as JSON.
4. **Remove fake data:** The `calibration_dashboard.py` random fallback must be removed. If no data, show empty states.
5. **Single WSGI entry point:** `app.py` imports and registers all Blueprints. Gunicorn serves one process on one port.

**Timeline:** 1 week for full consolidation. Merge the three dashboards into one navigable SPA-lite with tabs.

### Elephant 2: No real-time data pipeline — dashboard is a static page
**What:** The dashboard has zero real-time capabilities. There is no:
- WebSocket or SSE connection for live updates
- Push mechanism from the trading engine to the dashboard
- Background thread that refreshes dashboard data
- Cron job that updates the dashboard state
- Caching layer for dashboard queries

The only "refresh" is the browser's `window.location.reload()`. The `calibration_dashboard.py` has a `/refresh` endpoint that returns JSON, but the frontend never calls it. The `dashboard.py` fetches data once on page load and never again.

**Why matters:** The trading engine runs continuously in the background, making decisions and placing trades. The dashboard should reflect this in near-real-time. A static dashboard means:
- The trader cannot react to market movements
- Risk alerts go unnoticed until the next manual refresh
- The "last updated" timestamp diverges from reality
- The dashboard becomes a historical record, not a live tool

**What happens if ignored:** The dashboard will be used for post-hoc analysis only, not for live trading. The team will build a separate "real-time" system (probably a different UI) that duplicates the dashboard's logic. The dashboard becomes tech debt.

**Spec for resolution:**
1. **SSE endpoint:** Add `GET /api/stream` that yields JSON events every 30 seconds. Each event contains: timestamp, health status, latest P&L, open positions (count + value), flagged discrepancies (count), recent trades (last 5).
2. **Background refresh thread:** In the unified Flask app, start a background thread that runs every 30 seconds and caches dashboard data in a module-level `dashboard_cache` dict. API endpoints read from the cache instead of hitting the database. The SSE endpoint reads from the same cache.
3. **Event classification:** Each SSE event has a `type` field: `heartbeat` (normal), `alert` (new flag/risk), `trade` (new trade executed). The browser can filter or highlight based on type.
4. **Browser EventSource:** The frontend connects to `/api/stream` on page load. Listens for events. Updates the relevant DOM sections without full page re-render. Uses `Plotly.react()` for chart updates (efficient partial re-render).
5. **Fallback polling:** If SSE fails (proxy issues, browser compatibility), fall back to `setInterval` fetch every 60s. This should be transparent to the user.
6. **Cache invalidation:** The cache is invalidated on every cron tick, every trade execution, and every alert dispatch. This ensures the dashboard is always up-to-date with the engine's latest state.

**Timeline:** 3-4 days for a complete SSE pipeline with fallback. This is the single highest-impact change for the dashboard.

---

## Summary

| Category | Count | Key Action |
|---|---|---|
| ERRORS | 13 | Fix hardcoded market_prob=0.5, remove fake data, unify Flask apps |
| IMPROVEMENTS | 6 | Blueprint architecture, SSE real-time, P&L panel, Jinja2 templates |
| IDEAS | 4 | Position management UI, risk heatmap, trade replay, anomaly detection |
| ELEPHANTS | 2 | Consolidate 3 dashboards into 1, add real-time data pipeline |

The two elephants are interdependent — you cannot properly consolidate the dashboards without a real-time pipeline, and you cannot build a real-time pipeline without consolidating the data sources. Recommend tackling both simultaneously: build the unified `DashboardDataService` first, then add SSE on top.