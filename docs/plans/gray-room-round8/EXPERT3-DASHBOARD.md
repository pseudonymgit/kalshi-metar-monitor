# Gray Room Round 8 — Expert 3: Production Trading Dashboard

**Domain:** Production Trading Dashboard — Design, Architecture, and Priorities  
**System:** Weather Engine (Kalshi temperature market paper trader)  
**Evaluated Files:** `core/dashboard.py`, `core/confidence_dashboard.py`, `core/calibration_dashboard.py`, `core/paper_trading_engine.py`, `core/trade_journal.py`, `core/alert_builder.py`, `core/alert_dispatcher.py`  
**Date:** 2026-07-22  
**Reviewer:** Expert 3 — Production Trading Dashboard

---

## Executive Summary

The weather engine currently has **three separate dashboard files** (`dashboard.py`, `confidence_dashboard.py`, `calibration_dashboard.py`) serving three different Flask apps, none of which provide a unified trader-facing view. There is:

- **No P&L visibility** on any dashboard (zero trade journal queries)
- **No position management** (no open positions, no MTM)
- **No alert feed** (alerts go exclusively to Discord)
- **No portfolio-level aggregation** across 20 cities
- **No real-time or near-real-time stream** from the trading engine
- **Hardcoded market_prob=0.5** on the main dashboard

The production trading dashboard must be built from the ground up. Extending any existing dashboard is the wrong approach — they are all oriented toward system health/calibration, not trader workflow.

---

## ERRORS

### Error 1: No P&L Visibility Anywhere

**Location:** `core/dashboard.py` (entire file), `core/confidence_dashboard.py` (standalone analytics), `core/calibration_dashboard.py` (calibration only)

**What:** The main Flask dashboard (`dashboard.py`) queries `daily_stats` from the METAR database. It shows signal counts, station predictions, and model-vs-market discrepancies. **It never queries the paper trading database (`paper_trading.db`)** for P&L, open positions, or trade history.

The `confidence_dashboard.py` has a `ConfidenceTracker` class that computes P&L, Sharpe, drawdown, etc. — but it is an **in-memory class** with no Flask integration and no DB persistence bridge. The `calibration_dashboard.py` queries `daily_balances` but only shows calibration metrics, not live trading P&L.

**Why wrong:** A trader cannot assess whether the strategy is working without P&L data. The engine tracks P&L through:
- `paper_trading_engine.py` → `trades` table (realized_pnl per settlement)
- `trade_journal.py` → `trade_journal` table (outcome tracking)
- `daily_balances` table (EOD snapshots)

None of these are surfaced on a dashboard.

**Spec:**
1. Add a `/api/pnl/summary` endpoint that queries `paper_trading.db`:
   ```sql
   SELECT COALESCE(SUM(realized_pnl), 0.0) as total_pnl,
          COUNT(*) as total_trades,
          SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
          MIN(settlement_date_utc) as first_trade,
          MAX(settlement_date_utc) as last_trade
   FROM trades WHERE status = 'closed'
   ```
2. Add a `/api/pnl/timeseries?days=30` endpoint returning daily P&L from `daily_balances`.
3. Add a `/api/pnl/by_city` endpoint aggregating P&L by station.
4. Display on dashboard: total P&L (with green/red color), P&L by city as a bar chart, P&L over time as a line chart.

---

### Error 2: Three Separate Flask Dashboards — No Unification

**Location:** `core/dashboard.py` (port 5005), `core/confidence_dashboard.py` (standalone class), `core/calibration_dashboard.py` (separate Flask app)

**What:** There are three separate dashboard implementations:
- `dashboard.py` — Flask app on :5005, technical health, station prediction discrepancies
- `confidence_dashboard.py` — pure analytics class, no Flask, self-contained `ConfidenceTracker`
- `calibration_dashboard.py` — separate Flask app with hardcoded paths, calibration-only

None of these are composable into a single view. A trader would have to run three separate servers and flip between them.

**Why wrong:** Fragmented dashboards create context-switching overhead. A trader needs to see P&L, open positions, risk, and alerts on ONE screen. Three separate apps also means three maintenance burdens, three potential port conflicts, and three deployment configs.

**Spec:**
1. Create a new `core/trading_dashboard.py` as the single trader-facing Flask app. It imports data access from existing modules but serves all routes on one port.
2. Deprecate old dashboards: add a redirect from `/old/dashboard` to the new unified view.
3. Blueprint architecture: `/api/health` (from dashboard.py), `/api/trading/pnl`, `/api/trading/positions`, `/api/trading/alerts`, `/api/risk/summary`.

---

### Error 3: Hardcoded `market_prob = 0.5` — No Real Market Data

**Location:** `core/dashboard.py`, line ~195 (`station_predictions`) and line ~255 (`discrepancy_table`)

**What:** Both the single-station predictions endpoint and the discrepancy table set `market_prob = 0.5` as a hardcoded constant:
```python
# Market probability: default 0.5 (no live market data in demo)
market_prob = 0.5
```

**Why wrong:** With `market_prob = 0.5` for every station, the discrepancy table shows every station as having a discrepancy. The z-score computation across stations becomes meaningless because it's comparing model_prob to a flat 0.5. In reality, Kalshi markets have different prices for different cities and strike levels. The `kalshi_price_fetcher.py` module exists and is already used by `paper_trading_engine.py` via `_get_market_price`, but the dashboard never calls it.

**Spec:**
1. Import `kalshi_price_fetcher.get_live_market_price` in `dashboard.py`.
2. Add a `_fetch_market_prob(station, market_type, date)` method that:
   - Tries `get_live_market_price(station, market_type, date)` first
   - Falls back to historical heuristic (from `_get_market_price` in paper_trading_engine)
   - Falls back to 0.5 only if both fail
3. Pass `market_type='HIGH'` and today's date to the fetcher.
4. Cache prices with a 60-second TTL to avoid rate limits.

---

### Error 4: No Position Management (Open Positions, MTM, P&L by Position)

**Location:** No file implements this. The `positions` table exists in `paper_trading.db` but no dashboard querys it.

**What:** The engine has a full `positions` table schema:
```sql
CREATE TABLE positions (
    station TEXT, market_type TEXT, market_side TEXT,
    average_cost REAL, quantity INTEGER,
    mark_to_market_value REAL, unrealized_pnl REAL, ...
)
```
The `paper_trading_engine.py` has `_fetch_current_market_price()` for MTM. But no endpoint exposes open positions, current MTM value, or unrealized P&L.

**Why wrong:** A trader cannot manage positions without knowing what's open, what the current value is, and whether to exit or hold. For short-duration trading (minutes-to-hours), position management is the **primary** dashboard function — not technical health metrics.

**Spec:**
1. Add `/api/trading/positions` endpoint:
   ```sql
   SELECT station, market_type, market_side, average_cost, quantity,
          initial_value_usd, unrealized_pnl, realized_pnl, total_pnl,
          opened_date_utc, DATEDIFF('hours', opened_date_utc, NOW()) as hours_open
   FROM positions WHERE status = 'active'
   ```
2. For each open position, call `_fetch_current_market_price()` to update `mark_to_market_value` before returning.
3. Display on dashboard as a table with: station, direction, entry price, current price, size, unrealized P&L (colored green/red), hours open. Sort by largest unrealized P&L magnitude.

---

### Error 5: No Alert Feed on Any Dashboard

**Location:** `core/alert_builder.py`, `core/alert_dispatcher.py` (Discord-only delivery), `core/trade_journal.py` (DB recording)

**What:** The alert pipeline (`alert_builder.py` → `alert_dispatcher.py` → Discord webhook) is fully operational. Alerts are sent to Discord only. The `trade_journal.py` records every decision (EXECUTED, SKIPPED_*, ERROR) in a SQLite table. But no dashboard ever queries `trade_journal.db`.

**Why wrong:** When a trader wants to review recent decisions, they must either scroll Discord chat or query the DB manually. Discord messages scroll off screen. A web dashboard with a searchable, filterable alert feed is essential for:
- Reviewing skipped opportunities (why was this station skipped?)
- Auditing executed trades
- Diagnosing error conditions
- Backtesting awareness of what the system was thinking

**Spec:**
1. Add `/api/trading/alerts?limit=50&station=KATL&outcome=EXECUTED` endpoint:
   ```sql
   SELECT timestamp_utc, station, market, direction, confidence, edge,
          market_prob, lane, outcome, alert_id, failure_mode
   FROM trade_journal
   ORDER BY timestamp_utc DESC
   LIMIT ?
   ```
2. Support filters: `station` (ICAO), `outcome` (EXECUTED/SKIPPED_*/ERROR), `lane` (Regular/Sure_Thing/Goldilocks), `min_edge`, `date_from`/`date_to`.
3. Display on dashboard as a scrollable, reverse-chronological feed with:
   - Color-coded outcome badges (green=EXECUTED, yellow=SKIPPED, red=ERROR)
   - Station, direction (up/down arrow), confidence (bar), edge, lane
   - Click-to-expand for full context

---

### Error 6: ConfidenceTracker Uses In-Memory State — Can't Survive Restarts

**Location:** `core/confidence_dashboard.py`, `ConfidenceTracker` class (entire file)

**What:** The `ConfidenceTracker` class stores trades in an in-memory list (`self._trades: List[TradeRecord]`). Trades are added via `record_trade()` but there is no mechanism to hydrate from `paper_trading.db` or `trade_journal.db` on startup.

**Why wrong:** If the server restarts, all historical analytics (running p-value, rolling Sharpe, max drawdown, Monte Carlo results) are wiped. The metrics only start accumulating from the next trade. This means:
- The dashboard shows "no data" for up to 50 trades (rolling Sharpe window)
- Alert thresholds won't trigger until enough trades accumulate
- Monte Carlo simulation produces meaningless results on <2 trades

**Spec:**
1. Add a `hydrate_from_db(self, paper_db_path: str)` method that reads from `trades` table:
   ```sql
   SELECT trade_date_utc, signal_direction, forecast_prob,
          CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END as was_correct,
          realized_pnl
   FROM trades
   WHERE status = 'closed'
   ORDER BY trade_date_utc ASC
   ```
2. Call `hydrate_from_db()` at startup in the dashboard's `__init__`.
3. Add a last-hydrated timestamp to the snapshot response.

---

### Error 7: No Time Range Controls on Any Dashboard

**Location:** `core/dashboard.py`, `core/calibration_dashboard.py` (fixed-window queries)

**What:** `dashboard.py` hardcodes a 30-day window:
```python
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
```
`calibration_dashboard.py` hardcodes a 90-day window. Neither supports query parameters for custom ranges.

**Why wrong:** Traders need to compare different time windows: "What happened in the last 7 days vs the 30 days before that?" Especially for short-duration trading, a 30-day window with trades settling every 5 hours means 144+ data points — comparing hourly performance requires flexible windowing.

**Spec:**
1. Add query parameter support to all list/timeseries endpoints: `?days=7&days=30&days=90`.
2. For the `/api/pnl/timeseries` endpoint, accept `?date_from=2026-07-01&date_to=2026-07-21`.
3. On the HTML dashboard, add a row of preset buttons: `[7D] [30D] [90D] [YTD] [Custom]` and a date range picker.

---

### Error 8: No Portfolio-Level Statistics

**Location:** All endpoints are per-station. No aggregation exists.

**What:** The discrepancy table iterates over stations individually. The predictions endpoint is single-station. There is no endpoint for:
- Total open exposure across all cities
- Exposure by region
- Correlation between station positions
- Concentration risk (too much in one cluster)

**Why wrong:** A portfolio manager (or the engine's own risk controls) needs to see aggregate exposure. `station_registry.py` has cluster definitions and budget caps (`CLUSTER_BUDGET_USD`, `CITY_PAIR_CAP_USD`), but no dashboard visualizes whether these are being hit.

**Spec:**
1. Add `/api/trading/portfolio` endpoint that returns:
   ```json
   {
     "total_exposure": 4500.00,
     "station_count": 12,
     "exposure_by_cluster": {
       "northeast": 1200.00,
       "southeast": 800.00,
       "west_coast": 2500.00
     },
     "cluster_utilization": {
       "northeast": { "exposure": 1200.00, "budget": 5000.00, "utilization_pct": 24.0 }
     },
     "largest_positions": [
       {"station": "KLAX", "exposure": 1000.00, "market_type": "HIGH", "direction": "UP"}
     ]
   }
   ```
2. Query from `positions` table, grouped by cluster (using `station_registry.py`).
3. Display on dashboard as a portfolio summary panel with gauge charts for cluster utilization.

---

### Error 9: No Real-Time or Near-Real-Time Refresh

**Location:** `core/dashboard.py`, HTML template — full page load only

**What:** The HTML dashboard loads once with `<script>` blocks that fetch `/health` and `/api/discrepancies`. There is no auto-refresh, no WebSocket, no Server-Sent Events, and no polling interval. The trader must manually refresh the browser.

**Why wrong:** In short-duration trading (minutes-to-hours), price changes happen every few seconds. Position values change. Alerts fire. A static dashboard is useless for active trading — it's a report, not a trading tool.

**Spec:**
1. Add `setInterval` polling to the dashboard HTML: refetch P&L data every 30s, position data every 15s, alert feed every 10s.
2. Add a visual "last updated X seconds ago" indicator.
3. Add a WebSocket endpoint for push updates (lower priority, but ideal for position/price changes).
4. Add a manual "Refresh Now" button.

---

### Error 10: No Risk Metrics Visualization

**Location:** `core/dashboard.py` (no risk), `core/confidence_dashboard.py` (has risk but not exposed), `core/risk_controls.py` (has data but unused by dashboards)

**What:** `risk_controls.py` provides `RiskMetrics`, `RiskConfig`, `evaluate_risk_state()`, and `risk_report()`. The `ConfidenceTracker` has `check_alert_thresholds()` that checks drawdown, win rate, and Sharpe. But no dashboard renders these visually.

**Why wrong:** Risk is the most important thing to monitor during active trading. The trader needs to know at a glance:
- Current drawdown (compared to threshold)
- Daily loss remaining before kill switch
- Consecutive losses counter
- Whether the risk state is OK / WARNING / KILL_SWITCH

**Spec:**
1. Add `/api/risk/summary` endpoint:
   - Fetch current_drawdown and max_drawdown from `ConfidenceTracker.hydrate_from_db()`.
   - Fetch daily P&L from `daily_balances`.
   - Get risk state from `RiskManager.evaluate_risk_state()`.
   - Return: risk_state, current_drawdown, max_drawdown, daily_pnl, consecutive_losses, daily_loss_limit_remaining.
2. Display on dashboard as a "Risk Panel" with:
   - Risk state badge (green=OK, yellow=WARNING, red=KILL_SWITCH)
   - Drawdown gauge (0% → 10%+)
   - Daily loss thermometer (how much of daily limit used)
   - Consecutive losses counter

---

### Error 11: No Signal Quality Metrics on Dashboard

**Location:** `core/dashboard.py`, `core/confidence_dashboard.py`, `core/paper_trading_engine.py`

**What:** The dashboard shows which signals fired but not how accurate they were historically. `dashboard.py` computes 3 signals (mean_reversion, volatility_regime, momentum) but has no accuracy tracking. The actual trading engine uses 7+ signals (calendar climatology, late_day_momentum, NWP analog, multi_model_ensemble, temperature_advection, goldilocks, intraday_metar_confirmation) but there's no way to see which signals are winning and which are losing.

**Why wrong:** Without signal-level accuracy metrics, the trader can't tell if the system is improving or if a new signal is hurting performance. If `temperature_advection` has a 35% accuracy while `late_day_momentum` has 68%, the trader needs that information to override or adjust.

**Spec:**
1. Add `/api/trading/signal_quality?days=30` endpoint:
   ```sql
   SELECT signal_type, 
          COUNT(*) as total_fires,
          SUM(CASE WHEN settled_win THEN 1 ELSE 0 END) as wins,
          ROUND(AVG(CASE WHEN settled_win THEN 1.0 ELSE 0.0 END), 4) as accuracy,
          AVG(confidence) as avg_confidence,
          AVG(edge) as avg_edge
   FROM signal_log  -- (requires adding signal_log table or extracting from trade_journal)
   GROUP BY signal_type
   ```
2. If no signal_log table exists, parse `alert_id` or `failure_mode` fields from `trade_journal` to extract signal provenance.
3. Display as a table with: signal name, fires, accuracy (colored), avg confidence, avg edge. Sort by accuracy descending.

---

### Error 12: Dashboard Uses Dead/Non-Existent Signals

**Location:** `core/dashboard.py`, `_compute_signals()` method (~line 150)

**What:** The dashboard's `_compute_signals()` creates 3 synthetic signals (mean_reversion, volatility_regime, momentum) that **do not exist** in the actual trading engine. The real engine uses different signals (calendar_climatology, late_day_momentum_hourly, nwp_analog, multi_model_ensemble, temperature_advection, goldilocks, intraday_metar_confirmation).

**Why wrong:** The dashboard is showing a "signal breakdown" that has nothing to do with the actual trading decisions. A trader looking at the dashboard for signal analysis would get completely misleading information. Moreover, the signals produce a `model_prob` that doesn't match the engine's actual probability computation.

**Spec:**
1. Replace `_compute_signals()` with a call to the actual engine's `_get_analytical_probability()` or the `AgreementGate` output.
2. Alternatively, query the actual signal outputs from `decision_output_log` table in `paper_trading.db`.
3. If neither is available, show a clear disclaimer: "Simulated signals — actual signal analysis requires engine integration."

---

## IMPROVEMENTS

### Improvement 1: Build a Dedicated Trading Dashboard (Don't Extend Existing)

**What to change:** Create a new `core/trading_dashboard.py` as a standalone Flask app for trader-facing views. Don't extend `dashboard.py`, `confidence_dashboard.py`, or `calibration_dashboard.py`.

**Why better:** The existing dashboards are oriented toward:
- `dashboard.py` → System health / station prediction discrepancies (DevOps view)
- `confidence_dashboard.py` → Post-trade analytics (Quant view)
- `calibration_dashboard.py` → Model calibration (ML Engineer view)

A trader dashboard needs completely different primitives: real-time P&L, open positions, order entry, alert feed, risk gauges. Mixing these into the existing files would create a tangle of unrelated concerns. A clean new file with its own blueprints is easier to maintain, test, and deploy.

**Effort:** Medium (new file, but can reuse data access patterns from existing files)

**Spec:**
```
core/trading_dashboard.py  (new)
├── Blueprint: api_trading
│   ├── GET /api/trading/pnl           (from trades + daily_balances)
│   ├── GET /api/trading/positions      (from positions table)
│   ├── GET /api/trading/alerts         (from trade_journal)
│   ├── GET /api/trading/portfolio      (aggregated exposure)
│   ├── GET /api/trading/signal_quality (historic signal accuracy)
│   └── GET /api/trading/order_history  (full trade log)
├── Blueprint: api_risk
│   ├── GET /api/risk/summary           (drawdown, daily loss, kill switch state)
│   └── GET /api/risk/alerts            (risk-related alerts only)
├── Routes: HTML
│   ├── /trading                        (main trading dashboard page)
│   ├── /trading/positions              (position management view)
│   └── /trading/alerts                 (full alert history view)
└── Static: JS polling, WebSocket client (optional)
```

---

### Improvement 2: Implement Real Market Price Integration

**What to change:** Connect the dashboard to `kalshi_price_fetcher.py` for live market prices instead of hardcoded 0.5. Add a price cache with TTL.

**Why better:** The entire discrepancy analysis (model vs market) is only useful if market prices are real. With hardcoded 0.5, the dashboard is showing noise. Real prices enable:
- Meaningful discrepancy detection (actual mispricings)
- Accurate MTM for open positions
- Edge calculation visualization
- Real-time awareness of market conditions

**Effort:** Low (kalshi_price_fetcher already exists; just wire it in)

**Spec:**
1. Create `core/trading_dashboard/price_integration.py` (or extend the dashboard):
   ```python
   class MarketPriceProvider:
       def __init__(self, cache_ttl_seconds=60):
           self._cache = {}
           self._ttl = cache_ttl_seconds
       
       def get_price(self, station, market_type, date):
           # Check cache first
           key = f"{station}:{market_type}:{date}"
           cached = self._cache.get(key)
           if cached and (time.time() - cached['ts']) < self._ttl:
               return cached['price']
           # Fetch from live API
           try:
               price, meta = get_live_market_price(station, market_type, date)
               if price and 0.01 <= price <= 0.99:
                   self._cache[key] = {'price': price, 'ts': time.time()}
                   return price
           except Exception:
               pass
           # Fallback to heuristic (from paper_trading_engine)
           return self._heuristic_price(station, market_type, date)
   ```
2. Wire into `/api/trading/positions` for MTM updates.
3. Wire into `/api/trading/pnl` for current position value calculations.

---

### Improvement 3: Add Auto-Refresh with Configurable Poll Interval

**What to change:** Add JavaScript-based auto-refresh (30s default) with user-configurable interval (5s, 15s, 30s, 60s, manual).

**Why better:** Static pages force manual refresh, which is unacceptable for active trading. Auto-refresh with visual freshness indicator is table stakes for any trading dashboard.

**Effort:** Low (client-side JS only, no server changes needed)

**Spec:**
```javascript
// In trading_dashboard HTML template
class DashboardAutoRefresh {
    constructor(defaultIntervalMs = 30000) {
        this.interval = defaultIntervalMs;
        this.timer = null;
        this.lastUpdated = null;
        this.paused = false;
    }

    start() {
        this.refresh();
        this.timer = setInterval(() => this.refresh(), this.interval);
    }

    async refresh() {
        if (this.paused) return;
        // Fetch all API endpoints in parallel
        await Promise.all([
            this.fetchAndUpdate('/api/trading/pnl', this.updatePnlPanel),
            this.fetchAndUpdate('/api/trading/positions', this.updatePositionsTable),
            this.fetchAndUpdate('/api/trading/alerts?limit=10', this.updateAlertFeed),
            this.fetchAndUpdate('/api/risk/summary', this.updateRiskPanel),
        ]);
        this.lastUpdated = new Date();
        document.getElementById('last-updated').textContent =
            `Updated ${this.lastUpdated.toLocaleTimeString()}`;
    }
}

// Controls: dropdown for interval, pause/resume button, manual refresh button
```

---

### Improvement 4: Add Alert Feed Inbox to Dashboard

**What to change:** Render a real-time alert feed on the dashboard that queries `trade_journal.db` instead of (or in addition to) Discord.

**Why better:** The alert pipeline already records every decision to `trade_journal.db`. Surfacing this on the dashboard provides:
- Single-pane visibility for all system decisions
- Searchable history (Discord scrolls off)
- Filtered views (show only EXECUTED, show only ERROR, show for specific station)
- Click-to-diagnose (click an alert to see full decision context)

**Effort:** Low (data exists in trade_journal.db; just need query endpoint + UI)

**Spec:**
1. Alert feed endpoint: `GET /api/trading/alerts?limit=50&station=KATL&outcome=EXECUTED`
2. Alert detail endpoint: `GET /api/trading/alerts/{alert_id}`
3. Dashboard component:
   - Top panel: last 5 alerts (compact, color-coded badges)
   - Expandable full feed: filterable table with all columns
   - Count badges: "3 new alerts since last view"
   - Different visual treatment for EXECUTED vs SKIPPED vs ERROR

---

### Improvement 5: Add Short-Duration Trading Mode

**What to change:** Add a "Short Duration" toggle/preset to the dashboard that changes default views for intraday trading (minutes-to-hours holding periods).

**Why better:** The engine is shifting to minutes-to-hours trading (as stated in the brief). Short-duration trading has fundamentally different dashboard needs:
- Every second of latency matters (refresh interval should drop to 5-10s)
- Position micro-management replaces portfolio strategy
- Individual trade P&L replaces aggregate daily P&L
- Fast exits require prominent close/exit buttons
- Alert frequency increases dramatically (need better noise filtering)

**Effort:** High (requires new UI paradigm, not just metrics)

**Spec:**
1. Add a mode toggle in the header: `[Standard Trading] [Short Duration]`
2. In Short Duration mode:
   - Refresh interval: 5 seconds (vs 30s in standard mode)
   - Position panel becomes the primary (largest) view
   - Add "Close Position" button per row
   - Show trade P&L as scrolling ticker
   - Alert feed shows only last 60 seconds
   - Add audio/visual alert for major P&L moves (>$50 in one refresh)
   - Add "Time to settlement" countdown per position
   - Portfolio exposure refresh every refresh cycle (not cached)

---

## IDEAS

### Idea 1: WebSocket-Powered Live Price Feed

**Idea:** Replace HTTP polling with a WebSocket connection for live price updates from the dashboard. The server subscribes to Kalshi price changes and pushes them to all connected dashboard clients.

**Expected benefit:** Sub-second updates without polling overhead. Clients don't need to poll every 5 seconds; they receive updates as they happen. This is critical for short-duration trading where 5 seconds of latency can mean the difference between profit and loss.

**Risk/Uncertainty:**
- Kalshi doesn't have a push API — prices must still be polled server-side
- WebSocket adds deployment complexity (sticky sessions, connection management)
- Multiple connected clients multiply server-side polling load
- Fallback mechanism needed for browsers that block WebSockets

**Spec for validation:**
1. Implement a `PriceBroadcaster` class on the server that:
   - Polls Kalshi API for every active station every 15 seconds (configurable)
   - Compares against last-known price; pushes only on change
   - Uses Flask-SocketIO or plain `asyncio` websockets
   - Maintains a dict of connected clients with their subscribed stations
2. Client-side: single persistent WebSocket connection, updates DOM directly on message.
3. Validation metric: measure average latency from price change to dashboard update; compare to 15s polling baseline.

---

### Idea 2: ML-Powered Trade Summary (Natural Language Daily Brief)

**Idea:** Generate a daily natural-language summary of trading activity: "KLAX had strong late-day momentum signals confirming 3 of 4 trades. KATL showed temperature advection divergence — 2 of 3 UP signals fired but prices moved DOWN. Overall win rate 62%, P&L +$143. Recommendation: review KATL temperature advection calibration."

**Expected benefit:** Reduces the time a trader spends scanning dashboards. A 2-paragraph summary can communicate what happened, what to watch, and what to fix — especially useful for the non-technical trader (Dan/Gerri).

**Risk/Uncertainty:**
- Quality depends on prompt engineering and signal extraction
- Could produce misleading summaries if signal data is noisy
- Adds a dependency on an LLM call (not deterministic)
- May create false confidence in the system

**Spec for validation:**
1. Add a `/api/trading/daily_brief` endpoint that compiles structured data:
   - Total P&L and win rate for the day
   - Best and worst performing stations
   - Signal-level accuracy for the day
   - Alert count by outcome
   - Unusual events (divergence, errors, kill switch activations)
2. Feed the structured data into a deterministic template engine (not an LLM) for v1:
   - "P&L: ${total_pnl} (${win_rate}% win rate across ${trade_count} trades)"
   - "Best station: ${best_station} (+${best_pnl})"
   - "Worst station: ${worst_station} (${worst_pnl})"
   - "Alerts: ${executed_count} executed, ${skipped_count} skipped, ${error_count} errors"
3. For v2, feed structured data to an LLM call with a strict format prompt.
4. Validation: compare automated summary against a human-written summary for 10 trading days; measure factuality and completeness.

---

### Idea 3: "Glass-Box" Mode — Show Every Signal and Gate Decision

**Idea:** Add an "explainability" view that shows the complete decision chain for any trade or proposed trade: which signals fired, what their confidences were, what the agreement gate decided, what adaptive thresholds filtered, what spatial coherence modulated, what diversity penalty applied, what risk controls approved.

**Expected benefit:** Full transparency into every trade decision. When the system loses money, the trader can trace exactly why: "Ah, the spatial coherence gate reduced KSEA confidence from 0.72 to 0.55 because the Pacific Northwest cluster was split." This builds trust and enables targeted improvements.

**Risk/Uncertainty:**
- Requires a `decision_trace` table or logging to capture all intermediate states
- Could overwhelm the UI with too much information
- Performance overhead of recording all intermediate states per trade
- May expose internal complexity that confuses non-technical traders

**Spec for validation:**
1. Create a `decision_trace` table:
   ```sql
   CREATE TABLE decision_trace (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       trade_uuid TEXT NOT NULL,
       station TEXT,
       decision_step TEXT,  -- 'signal_gen', 'agreement_gate', 'adaptive_threshold', 'spatial_coherence', 'diversity_penalty', 'risk_control'
       input_value TEXT,     -- JSON of input to this step
       output_value TEXT,    -- JSON of output from this step
       passed_gate BOOLEAN,
       step_order INTEGER,
       timestamp_utc TEXT
   )
   ```
2. Instrument the signal pipeline in `paper_trading_engine.py` to log every gate/step.
3. Dashboard view: timeline of decision steps per trade_uuid, with expand/collapse for each step's input/output JSON.
4. Validation: test on 100 trades, verify that 100% of decision steps are captured, and that the timeline is human-readable.

---

## ELEPHANTS

### Elephant 1: The Dashboard Architecture Must Be Redesigned, Not Extended

**What the elephant is:** The current three-dashboard architecture (`dashboard.py`, `confidence_dashboard.py`, `calibration_dashboard.py`) is fragmented, inconsistent, and trader-unfriendly. None of them were designed with the trader workflow in mind. They were built by different people at different times for different purposes:
- `dashboard.py` — DevOps health dashboard (Edge 11)
- `confidence_dashboard.py` — Quant analytics class (Edge 19)
- `calibration_dashboard.py` — ML calibration reporting (Phase 3)

**Why it matters:** Adding trader features to any one of these would be a mistake. They have different:
- Data sources (METAR DB vs paper_trading DB vs in-memory)
- Refresh patterns (static pages vs computed analytics)
- Output formats (HTML+Plotly vs JSON dicts vs pure Flask)
- Error handling patterns (self-tests vs try/except vs hardcoded paths)

Attempting to "add trading to dashboard.py" would create a 2000+ line file with conflicting concerns.

**What happens if we ignore it:** The codebase continues to accrete dashboard files. A fourth dashboard gets added for trading. Then a fifth for risk. Configuration becomes impossible (which port runs which dashboard?). The trader has 4 browser tabs open. Documentation can't keep up. Bugs get fixed in one dashboard but not another.

**Spec for resolution:**
1. **Immediate (next sprint):** Create `core/trading_dashboard/` as a package with:
   ```
   core/trading_dashboard/
   ├── __init__.py       # Flask app factory, blueprint registration
   ├── routes.py         # All HTTP endpoints
   ├── market_data.py    # Price fetching + caching
   ├── pnl_provider.py   # P&L queries from paper_trading.db
   ├── position_provider.py  # Position queries + MTM
   ├── alert_provider.py # Alert feed from trade_journal.db
   ├── risk_provider.py  # Risk metrics from risk_controls.py
   └── templates/
       └── trading.html  # Single-page trader dashboard
   ```
2. **Short term (next 2 sprints):** Migrate the existing dashboard endpoints that traders actually need:
   - `/health` → keep, but add `/trading/health`
   - `/api/predictions/<station>` → migrate to `/api/trading/predictions/<station>` with real engine data
   - `/api/discrepancies` → migrate to `/api/trading/discrepancies` with real market prices
3. **Medium term (next 4 sprints):** Deprecate old dashboards:
   - Add `/redirect/old-dashboard/v1` pointing to new unified dashboard
   - Remove old dashboards after 2-week migration window
   - Add `WARNING: This dashboard is deprecated. Use /trading instead.` header to old dashboards
   - Delete old dashboard files after confirming no one is using them

---

### Elephant 2: The Data Pipeline from Engine to Dashboard is Unreliable for Real-Time

**What the elephant is:** The trading engine writes to SQLite databases (`paper_trading.db`, `trade_journal.db`). The dashboard reads from the same databases. There is no message queue, no event bus, no streaming layer. The timing is:

1. Engine runs → writes trade to `paper_trading.db`
2. Engine runs → writes alert decision to `trade_journal.db`
3. Dashboard polls → queries `paper_trading.db`
4. Dashboard polls → queries `trade_journal.db`

This works for daily reconciliation but breaks under short-duration trading:
- SQLite WAL mode can handle concurrent reads/writes, but contention increases with frequency
- A trade executed at 10:00:03 may not appear in the dashboard until 10:00:30 (next poll)
- If the engine and dashboard are in the same process, both block on DB I/O
- If the engine and dashboard are in separate processes, SQLite file locking becomes a bottleneck

**Why it matters:** For minutes-to-hours trading, data latency issues compound:
- A 30-second dashboard refresh means a position could lose 5% of its value before the trader sees it
- If the engine crashes mid-trade, the dashboard never sees the partial write
- SQLite's single-writer concurrency model means the dashboard's read queries compete with the engine's write queries

**What happens if we ignore it:** The dashboard becomes increasingly unreliable as trade frequency increases. Position snapshots show stale data. Alert feed misses trades. The trader loses trust in the dashboard and switches to manual Discord monitoring, defeating the dashboard's purpose.

**Spec for resolution:**
1. **Near-term (next sprint):** Implement a lightweight in-memory event buffer:
   ```python
   class TradeEventBuffer:
       """In-memory ring buffer for recent trading events.
       Writes are non-blocking. Reads are instant.
       SQLite is the persistence layer; this is the real-time layer."""
       def __init__(self, max_events=1000):
           self._events = []  # list of dicts
           self._lock = threading.Lock()
           self._max_events = max_events
       
       def push(self, event: dict):
           with self._lock:
               self._events.append(event)
               if len(self._events) > self._max_events:
                   self._events.pop(0)
       
       def recent(self, limit=50, event_type=None, station=None):
           with self._lock:
               filtered = self._events
               if event_type:
                   filtered = [e for e in filtered if e.get('type') == event_type]
               if station:
                   filtered = [e for e in filtered if e.get('station') == station]
               return filtered[-limit:]
   ```
2. **Medium-term:** Instrument `paper_trading_engine.py` to push events to the buffer on every trade execution, settlement, and MTM update:
   ```python
   self._event_buffer.push({
       'type': 'trade_executed',
       'station': station,
       'direction': direction.value,
       'size': position_size,
       'price': market_price,
       'timestamp': datetime.now(timezone.utc).isoformat()
   })
   ```
3. **Long-term:** Extract the event buffer into a standalone `EventBus` class that supports:
   - Multiple subscribers (dashboard, alert dispatcher, logger)
   - Async delivery (background thread, not in the trading hot path)
   - Dead-letter queue for failed deliveries

---

## Build Order: Priority and Rationale

### Priority 1: P&L + Position Visibility (Week 1)

**What:** `/api/trading/pnl`, `/api/trading/positions`, `/api/trading/portfolio`

**Why first:** Without P&L and positions, the dashboard has no value. Everything else is secondary. A trader must know: "Am I making money? What's open?"

**Deliverable:** A single-page dashboard showing:
- Total P&L (large, colored green/red)
- P&L over time (line chart, last 7 days)
- Open positions table (station, direction, size, entry price, current price, unrealized P&L)
- Portfolio summary (total exposure, by cluster)

### Priority 2: Alert Feed Integration (Week 2)

**What:** `/api/trading/alerts` from `trade_journal.db`

**Why second:** The alert pipeline already records everything. Surfacing it on the dashboard is low effort and provides immediate value. The trader can see what the system was thinking, not just what it did.

**Deliverable:** Alert feed panel on the dashboard with:
- Reverse-chronological list of last 50 alerts
- Color-coded outcome badges
- Filter controls (by station, outcome, lane)
- Click-to-expand for full decision context

### Priority 3: Risk Dashboard (Week 3)

**What:** `/api/risk/summary` with drawdown, daily loss, kill switch state

**Why third:** Risk monitoring is critical but depends on P&L and position data (which Priority 1 provides). The `ConfidenceTracker` and `risk_controls.py` already compute the metrics; just need to expose them.

**Deliverable:** Risk panel with:
- Risk state badge (OK/WARNING/KILL_SWITCH)
- Drawdown gauge
- Daily loss thermometer
- Consecutive losses counter

### Priority 4: Performance Analytics (Week 4)

**What:** Signal quality metrics, Sharpe curves, win rate by station/signal, calibration drift

**Why fourth:** Performance analytics require accumulated trade history. The dashboard needs at least 2-3 weeks of trading data to show meaningful metrics. This is the "why" layer — why is the strategy working or not?

**Deliverable:** Analytics tab with:
- Signal accuracy table (by signal type, last 30 days)
- Win rate by station (bar chart)
- Rolling Sharpe (line chart, last 50 trades)
- Monte Carlo simulation results (from ConfidenceTracker)

### Priority 5: Short-Duration Mode (Week 5-6)

**What:** Mode toggle, faster refresh, micro-P&L, close buttons, settlement countdown

**Why last:** Short-duration mode only matters when the engine is actively trading short-duration. It requires the foundation (P&L, positions, alerts, risk) to be stable first. If the engine is still in daily/settlement-based trading, this is premature.

**Deliverable:** Mode toggle in header, configurable refresh interval, position-level close buttons, settlement countdown timers, audio alerts for major P&L moves.

---

## Summary: Key Requirements for the Trading Dashboard

| Priority | Feature | Data Source | Effort | Dependencies |
|----------|---------|-------------|--------|--------------|
| P1 | P&L + Positions | `paper_trading.db` (trades, positions, daily_balances) | Medium | None |
| P2 | Alert Feed | `trade_journal.db` (trade_journal) | Low | None |
| P3 | Risk Dashboard | `risk_controls.py`, `ConfidenceTracker` | Medium | P1 (P&L data) |
| P4 | Performance Analytics | `paper_trading.db`, `trade_journal.db` | Medium | P1 (accumulated history) |
| P5 | Short-Duration Mode | All above + WebSocket | High | P1-P4 |

**Architecture decision: Build new, don't extend.** Create `core/trading_dashboard/` as a package with separate blueprints. Deprecate old dashboards after migration. Use an in-memory event buffer for real-time data. Wire real market prices from `kalshi_price_fetcher.py`. Poll for now, plan WebSocket for future.

**The single most important fix: Get P&L and positions on screen.** Everything else follows from that.