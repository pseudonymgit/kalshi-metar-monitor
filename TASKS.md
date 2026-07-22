# TASKS

## Phase 15: Code Review & File Changelogs Redux (2026-07-21 23:05 UTC)
**Status:** ✅ COMPLETE

### Completed:
- [x] 119 files with real changelog headers from git history
- [x] Real code review with file:line specifics (54 files with bugs found)
- [x] 4 orphaned files restored from git commit 0f99ac6
- [x] Phase 14 result VERIFIED: 69.67% (correct)
- [x] Alert pipeline diagnosed: .bashrc variable name mismatch
- [x] Code review doc: docs/plans/PHASE15-CODE-REVIEW-2026-07-21.md
- [x] Continuity handoff updated

## Phase 16: Bug Fixes & Pipeline Corrections (2026-07-21 — ROLLED BACK 2026-07-22)
**Status:** ⚠️ ROLLED BACK — subagent corrupted 87 files with syntax errors. Only safe fixes retained.

### Retained Fixes (verified clean):
- [x] Agreement threshold: changed default to "3" in paper_trading_engine.py
- [x] Signal registry: removed nwp_analog, registered nwp_direct
- [x] Fee rates: position_sizing.py defaults changed to 0.0
- [x] Fee rates: instance config presets set to 0.0

### Rolled Back (deferred — redo manually or via targeted patches):
- [ ] ~17 datetime.now() → timezone.utc replacements (22+ files) — need careful manual review
- [ ] ~17 bare except → except Exception replacements — need careful manual review
- [ ] ~16 div-by-zero guard additions — need careful manual review
- [ ] Syntax fixes in signal files (alert_state_machine.py L115 pre-existing)
- [ ] .bashrc/webhook variable name mismatch (already functionally correct via remapping)
- [ ] test_alert_dispatcher.py

## Phase 16: Gray Room Functionality Review (2026-07-22)
**Status:** ✅ COMPLETE

### Round 8 (REJECTED by Dan — wrong format, wrong premise, bugs not fixed)
Dispatched 3 experts. Rejected because:
- Wrong format (3 experts instead of 5-7)
- Expert 2 ran with wrong premise (Kalshi sub-daily contracts) — irrelevant to intraday signal goal
- Bugs not fixed before dispatch
- Dan's verdict: "this wasn't the standard gray room format"

### Round 9 (6 experts, independent analysis from scratch)
Fixes applied before dispatch: agreement threshold, signal registry, fee rates. Delivered 74 errors, 32 improvements, 20 ideas, 12 elephants.

#### Expert 1 — Meteorology (12 errors, 5 improvements, 3 ideas, 2 elephants)
- P0: NwpDirectSignal evaluate() is a no-op (returns None, 0.0)
- P1: Pressure Delta signal has reversed direction mapping
- P1: Dewpoint Depression queries wrong table
- P1: Seasonal window ±45 vs ±15
- Elephant: Signal independence not validated; no geographic effects

#### Expert 2 — Quant Finance (12 errors, 5 improvements, 3 ideas, 2 elephants)
- P0: Backtest simulates P&L from settlement — fake
- P0: Kelly sizing uses wrong variance formula
- P0: Risk controls not wired into engine
- P0: 3 conflicting position sizing systems can fire simultaneously
- Elephant: Backtest measures direction accuracy, not profit; zero tests

#### Expert 3 — Architecture (12 errors, 5 improvements, 3 ideas, 2 elephants)
- P0: 4 files = 24% monolithic; 18 bare excepts; 49+ naive datetime
- P0: 3 conflicting DB strategies; import cycle risk
- Elephant: No mode separation (paper/live share code); zero test infrastructure

#### Expert 4 — Production Ops (12 errors, 5 improvements, 3 ideas, 2 elephants)
- P0: Cron jobs in ERROR state; alert pipeline env var mismatch
- P0: No structured logging; no deployment pipeline
- Elephant: No monitoring/uptime checks; no rollback

#### Expert 5 — Dashboard (13 errors, 6 improvements, 4 ideas, 2 elephants)
- P0: market_prob hardcoded to 0.5; random data in calibration dashboard
- P0: Conflicting Flask apps; zero P&L data
- Elephant: 3 fragmented dashboards; no real-time data pipeline

#### Expert 6 — Market Microstructure (13 errors, 5 improvements, 3 ideas, 2 elephants)
- P0: Spread 6.2x off (0.5¢ vs 3.1¢); fill_price has no spread
- P0: Settlement price wrong threshold; 4x spread discrepancy between modules
- Elephant: Entire cost model wrong — paper P&L not real; zero execution realism

#### Synthesis: 74 errors, 32 improvements, 20 ideas, 12 elephants — all ADVANCE
- 19 P0 items, 14 P1 items, 6 P2 items
- Files: docs/plans/gray-room-round9/GRAY-ROOM-ROUND9-SYNTHESIS.md

## Phase 0: P0 Bug Fixes (2026-07-22)
**Status:** ✅ COMPLETE (8 commits, all verified)

### Applied Fixes:
- [x] Settlement price: directional compare (today vs yesterday temp)
- [x] Spread assumption: centralized in market_cost_model.py (3.1¢ mean)
- [x] fill_price includes spread from market_cost_model.py
- [x] Risk controls wired: check_kill_switches() called before every trade
- [x] Position sizing consolidated: only FeeAwareKellyPositionSizer used
- [x] Kelly formula fixed: per-signal variance, not global
- [x] NwpDirectSignal wired: replaces NwpAnalogSignal in pipeline
- [x] 18 bare excepts → except Exception (with logging)
- [x] 49+ naive datetime.now() → datetime.now(timezone.utc)
- [x] market_prob no longer hardcoded to 0.5 — wired to live Kalshi price
- [x] Random.uniform() removed from calibration dashboard
- [x] Market_cost_model.py created — single source of truth for spread/fees

### Not Yet Fixed (deferred):
- [ ] Remove fake P&L from backtest engine (Phase 21.3 — new backtest runner)
- [ ] Fix Pressure Delta direction mapping (Phase 23.5 — dual-polarity framework)
- [ ] Conflicting Flask apps consolidated (Phase 17 replaced old dashboards)
- [ ] Cron jobs in ERROR state (independent — needs diagnosis)
- [ ] Alert pipeline env var mismatch (already functionally correct)

## Phase 17: Production Trading Dashboard (2026-07-22)
**Status:** ✅ COMPLETE

### Built:
- [x] `core/trading_dashboard/` package (6 files, 2511 lines)
- [x] `/trading/` — Main P&L/Positions dashboard page
- [x] `/trading/positions` — Full positions + risk + analytics page
- [x] `/trading/api/pnl` — Total P&L, by city, daily time series
- [x] `/trading/api/positions` — Open positions with MTM, unrealized P&L
- [x] `/trading/api/portfolio` — Exposure by cluster and station
- [x] `/trading/api/alerts` — Last 50 journal entries, color-coded, filterable
- [x] `/trading/api/risk` — Risk state (OK/WARNING/KILL_SWITCH), drawdown gauge
- [x] `/trading/api/stats` — Signal accuracy, win rate by station, rolling Sharpe
- [x] `/trading/api/stream` — SSE endpoint, 30s real-time push
- [x] Dark-themed Jinja2 templates (base.html, index.html, positions.html)
- [x] Plotly.js charts (P&L cumulative, city breakdown, portfolio pie, win rate, Sharpe)
- [x] Blueprint registered in app.py at /trading prefix
- [x] Uses market_cost_model.py (3.1¢ spread), station_registry.py (cluster mapping)
- [x] Old dashboards deprecated (dashboard.py, confidence_dashboard.py, calibration_dashboard.py)
- [x] 0 new syntax errors

## Phase 18: Short-Duration Trading (Intraday Signals) 🔄 IN PROGRESS

### Phase A — METAR-based signals (existing data, no HRRR needed)
- [ ] **FOGR Reversion** — Frost Occurrence Guidance Reversion: when overnight dewpoint depression is near 0, expect rapid morning warming
- [ ] **METAR dT/dt** — 3-hour temperature change rate from raw METAR observations
- [ ] **Pressure Tendency** — 3-hour pressure change from METAR

### Phase B — HRRR Pipeline
- [ ] Add HRRR collection endpoint to nwp_collect.py (Open-Meteo /v1/hrrr, 3km, hourly)
- [ ] 0-48h forecasts, 20 stations

### Phase C — Advanced Intraday Signals
- [ ] **ESDR** (Ensemble Spread Divergence Rate) — from HGEFS 31-member ensemble
- [ ] **NWP Trajectory + METAR dT/dt fusion** — GFS direction + METAR rate of change
- [ ] **Lagrangian trajectory** — trace 850-mb air parcels backward 12h

### Phase D — Entry/Exit Rules
- [ ] Sequential trigger architecture (A→B→C→D), not majority-vote ensemble
- [ ] Tiered confirmation: Stage 1 at 50% confidence, Stage 2 after METAR confirmation
- [ ] Time-decay + confidence decay dual exit
- [ ] Spread-based entry signal: widening spreads → avoid or increase edge requirement

### Phase E — Intraday Trading Loop
- [ ] Separate intraday trading mode, runs hourly
- [ ] Evaluates daily contracts at multiple points during the day
- [ ] Enters when edge/spread ratio is favorable
- [ ] Refines prediction with more recent METAR data
