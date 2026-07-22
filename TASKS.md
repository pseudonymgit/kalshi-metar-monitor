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

## Phase 17: Production Trading Dashboard (2026-07-22)
**Status:** 🔄 PENDING (revised per Gray Room Round 8)

### Build Order (per Expert 3):
- [ ] P1: P&L + Positions — /api/trading/pnl, /api/trading/positions, /api/trading/portfolio
- [ ] P2: Alert Feed + Position Management — alert feed from trade_journal.db, open positions view
- [ ] P3: Risk Dashboard — drawdown, daily loss, kill switch state
- [ ] P4: Performance Analytics — signal accuracy, Sharpe, win rate by station/signal
- [ ] P5: Short-Duration Mode — faster refresh, micro-P&L, close buttons

### Architecture Decision:
- Build NEW core/trading_dashboard/ package (don't extend existing)
- Deprecate old dashboards (dashboard.py, confidence_dashboard.py, calibration_dashboard.py)
- In-memory event buffer for real-time data
- SQLite for persistence

### P0 Fixes (from Gray Room Round 9, must be done before/during Phase 17):
- [ ] Fix settlement price calculation (wrong threshold)
- [ ] Fix spread assumption (0.5¢ → 3.1¢) and wire bid/ask from API
- [ ] Fix fill_price to include spread/slippage
- [ ] Wire risk_controls.py into paper_trading_engine.py
- [ ] Fix all 3 conflicting position sizing systems
- [ ] Fix Kelly formula (wrong variance)
- [ ] Remove fake P&L from backtest
- [ ] Fix 18 bare excepts
- [ ] Fix 49+ naive datetime.now() calls
- [ ] Fix NwpDirectSignal evaluate() (no-op)
- [ ] Fix Pressure Delta direction mapping
- [ ] Fix market_prob hardcoded to 0.5
- [ ] Stop random data generation in calibration dashboard
- [ ] Fix cron jobs in ERROR state
- [ ] Fix alert pipeline env vars
- [ ] Add structured logging
