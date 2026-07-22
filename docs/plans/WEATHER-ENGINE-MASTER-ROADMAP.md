# Weather Engine Master Roadmap v5.0

**Date:** 2026-07-22
**Previous:** v4.0 (Gray Room R5, 2026-07-18)
**Source:** Gray Room R9 (6 experts, 74 errors, 32 improvements, 20 ideas, 12 elephants) + Gray Room R8 (3 experts, 38 errors, 17 improvements, 10 ideas, 8 elephants) + Phase 15 code review

---

## Completed Phases (1-15)

### Phase 1: Foundation ✅
- Baseline signals, Render METAR integration, station registry

### Phase 2: Signal Expansion ✅
- Temperature advection, audit fixes, NWP analog v1.0

### Phase 3: Risk & Position Sizing ✅
- Kelly sizing, risk budget allocator, scaling ladder, stop-loss, dynamic station discovery

### Phase 4: Dashboard & Enhancements ✅
- Spatial coherence, dashboard MVP, dewpoint modulator, confidence thresholds, ensemble diversity, frontal passage detector, intraday METAR confirmation, confidence tracker

### Phase 5: Alert Infrastructure ✅
- Alert dispatcher, Kalshi API integration, Discord webhook

### Phase 6: Combinatorial Search ✅
- 7-signal search, calibrated search, parameter sweep, calibration v3 validation

### Phase 7: Production Readiness ✅
- Agreement gate, SBOX/PROD config, 30-day test plan

### Phase 8: Gray Room R5 Bug Fixes ✅
- All 9 CRITICAL bugs fixed (Goldilocks look-ahead, Kelly formula, 3 conflicting sizing systems, confidence squaring, calibration data leakage, XGBoost, fee model, lane thresholds, SQLite concurrency)

### Phase 9: NWP Backfill & Full Search ✅
- NWP data (217K+ rows), NWP analog v2.0, 11-signal search, purged CV

### Phase 10: Full Combinatorial Search ✅
- 9 clean signals, 1,479 combos × 3 agreement levels, best: pd+fd+cc at 72.30% (agree=3)
- NWP Direct Signal discovered: 92.7% GFS direction accuracy

### Phase 11: NWP Direct Signal + Fusion ✅
- NwpDirectSignal built, combinatorial search with NWP (redundant — doesn't improve ensemble), Bayesian log-odds fusion spec

### Phase 12: Regime & Markov ✅
- SKIPPED — insufficient signal quality, insufficient classification rate

### Phase 13: Probabilistic Trajectory ✅
- GFS forecast magnitude IS the trajectory (correlation 0.936)

### Phase 14: 30-Day Test ✅
- 68.67% cumulative accuracy (300 trades, 16 days, agree=1) — GREEN

### Phase 15: Code Review & Changelogs ✅
- 119 files, 54 with bugs, 4 restored orphans, 87+ div-by-zero, 49+ naive datetime, 18 bare excepts, 5 fee rate files

---

## Phase 16: Gray Room Functionality Review ✅

### Round 8 (REJECTED — wrong format, bugs not fixed)
- 3 experts (Architecture, Trading Pipeline, Dashboard)
- 38 errors, 17 improvements, 10 ideas, 8 elephants
- Replaced by Round 9

### Round 9 (6 experts, independent analysis)
- Fixes applied before dispatch: agreement threshold, signal registry, fee rates
- 74 errors, 32 improvements, 20 ideas, 12 elephants — all ADVANCE
- 19 P0, 14 P1, 6 P2 items

---

## Phase 0: P0 Bug Fixes (MUST DO BEFORE ANYTHING ELSE)

**Priority order — highest impact first:**

### Cost & Execution (from E6 — Market Micro)
| # | Item | Effort | Impact |
|---|------|--------|--------|
| 1 | Fix settlement price calculation — compares temp > 50.0°F instead of strike price | 1h | P&L completely wrong |
| 2 | Fix spread assumption: 0.5¢ → 3.1¢ (6.2x underestimation) | 1h | Paper P&L overstates real by 3-5x |
| 3 | Fix fill_price to include spread/slippage (currently = market_price) | 0.5h | No execution realism |
| 4 | Centralize cost model (FeeAwareEntryFilter uses 2¢, engine uses 0.5¢ — 4x discrepancy) | 1h | Inconsistent cost assumptions |

### Position Sizing & Risk (from E2 — Quant Finance)
| # | Item | Effort | Impact |
|---|------|--------|--------|
| 5 | Wire risk_controls.py into paper_trading_engine.py (currently not imported) | 1h | Paper trading runs unguarded |
| 6 | Fix 3 conflicting position sizing systems (Kelly, fixed, risk-budget — all fire simultaneously) | 2h | Positions sized unpredictably |
| 7 | Fix Kelly formula — uses variance of ALL trades, not specific asset's variance | 2h | Sizing formula is wrong |
| 8 | Remove fake P&L from backtest (simulates closing prices from settlement) | 1w | Backtest results are not real |

### Signal Pipeline (from E1 — Meteorology)
| # | Item | Effort | Impact |
|---|------|--------|--------|
| 9 | Fix NwpDirectSignal evaluate() — currently returns (None, 0.0) always | 1h | Signal registered but dead |
| 10 | Fix Pressure Delta direction mapping (rising pressure → warming is wrong) | 1h | Signal predicts opposite |

### Dashboard (from E5 — Dashboard)
| # | Item | Effort | Impact |
|---|------|--------|--------|
| 11 | Fix market_prob hardcoded to 0.5 everywhere — wire real Kalshi prices | 1h | Dashboard shows no real data |
| 12 | Stop random.uniform() data generation in calibration dashboard | 2h | Fake charts mislead |
| 13 | Fix conflicting Flask apps on different ports — will crash on Render | 2h | Dashboard won't deploy |

### Code Quality (from E3 — Architecture)
| # | Item | Effort | Impact |
|---|------|--------|--------|
| 14 | Fix 18 bare except clauses → except Exception | 0.5h | Swallows Ctrl+C, SystemExit |
| 15 | Fix 49+ naive datetime.now() → datetime.now(timezone.utc) | 2h | Timezone chaos |
| 16 | Fix 3 conflicting DB connection strategies | 4h | Inconsistent DB access |

### Operations (from E4 — Production Ops)
| # | Item | Effort | Impact |
|---|------|--------|--------|
| 17 | Fix cron jobs in ERROR state | 2h | Data collection may be broken |
| 18 | Fix alert pipeline env var mismatches (WEBHOOK_PROD vs DISCORD_WEBHOOK_PROD) | 0.5h | Alerts may not fire |
| 19 | Add structured logging (replace print() + logging mix) | 4h | Can't debug production issues |

---

## Phase 17: Production Trading Dashboard

**Prerequisite:** All Phase 0 items must be complete (especially cost model fixes, market_prob, and random data fixes)

**Architecture Decision:** Build NEW `core/trading_dashboard/` package. Deprecate old dashboards (`dashboard.py`, `confidence_dashboard.py`, `calibration_dashboard.py`).

### 17.1 — P&L + Positions (P1, Week 1)
- `/api/trading/pnl` — total P&L, by city, over time (with REAL cost model)
- `/api/trading/positions` — open positions, entry price, current price, unrealized P&L
- `/api/trading/portfolio` — total exposure, by cluster
- Frontend: single-page dashboard, P&L as large colored number
- Wire real Kalshi market prices (not 0.5 default)

### 17.2 — Alert Feed + Position Management (P2, Week 2)
- Alert feed from `trade_journal.db` — reverse-chronological, filterable by station/signal/outcome
- Position management — open/close controls, execution status
- Color-coded outcomes (green=executed, red=skipped, yellow=error)
- Live alerts from alert_dispatcher

### 17.3 — Risk Dashboard (P3, Week 3)
- Risk state badge (OK/WARNING/KILL_SWITCH) — wired from risk_controls.py
- Drawdown gauge, daily loss thermometer, consecutive losses counter
- Correlation heatmap — exposure by station, cluster, city pair
- System health timeline

### 17.4 — Performance Analytics (P4, Week 4)
- Signal accuracy table (by type, last 30 days)
- Win rate by station (bar chart)
- Rolling Sharpe (line chart, last 50 trades)
- Monte Carlo simulation results (P&L range: 5th, 50th, 95th percentile)

### 17.5 — Short-Duration Mode (P5, Week 5-6)
- Mode toggle (daily/short-duration)
- Faster refresh intervals (SSE, 30s)
- Micro-P&L display
- Close buttons per position (POST /api/positions/{uuid}/close)
- Settlement countdown timers

### 17.6 — Expert I5 Dashboard Improvements
- I1: Unified Flask app with Blueprint architecture (replace 2 conflicting apps)
- I2: Real-time data via SSE (replace polling)
- I3: Tradable P&L with position management
- I4: Filesystem Jinja2 templates (replace inline HTML)
- I5: Trade history + alert feed from trade_journal.db
- I6: System health timeline panel

### 17.7 — Expert I5 Dashboard Ideas (optional)
- Idea 1: Close position buttons from dashboard
- Idea 2: Correlation risk heatmap
- Idea 3: Historical trade replay mode
- Idea 4: Self-diagnosing anomaly detection

---

## Phase 18: Short-Duration Trading (Intraday Signals)

**Prerequisite:** Phase 17 dashboard complete (to see intraday data)

### 18.0 — Contract Selection Optimizer (E6 I5)
- Select best available contract (D+1 vs D+2) based on spread/edge ratio
- Prefer D+1 when spread is tight, D+2 when D+1 spread is excessive
- Effort: 3 days

### 18.1 — Phase A Signals (from Gray Room R7)
Build 3 signals that work with existing METAR data (no HRRR needed):
- **FOGR Reversion** — Frost Occurrence Guidance Reversion: when overnight dewpoint depression is near 0, expect rapid morning warming
- **METAR dT/dt** — 3-hour temperature change rate from raw METAR observations (not daily aggregates)
- **Pressure Tendency** — 3-hour pressure change from METAR (already in METAR data, not daily aggregated)

### 18.2 — Phase B: HRRR Pipeline
- Add HRRR collection endpoint to nwp_collect.py (Open-Meteo /v1/hrrr, 3km, hourly)
- 0-48h forecasts, 20 stations
- Expected: 80-85% directional accuracy at 1-3h horizon
- Scope doc: `docs/plans/HRRR-COLLECTION-SCOPE.md`

### 18.3 — Phase C: Advanced Intraday Signals
- **ESDR** (Ensemble Spread Divergence Rate) — from HGEFS 31-member ensemble
- **NWP Trajectory + METAR dT/dt fusion** — GFS direction + METAR rate of change
- **Lagrangian trajectory** — trace 850-mb air parcels backward 12h (E1 Idea 1)

### 18.4 — Phase D: Entry/Exit Rules
- Sequential trigger architecture (A→B→C→D), not majority-vote ensemble
- Tiered confirmation: Stage 1 at 50% confidence, Stage 2 after METAR confirmation
- Time-decay + confidence decay dual exit
- Spread-based entry signal (E6 Idea 1): widening spreads → avoid or increase edge requirement

### 18.5 — Intraday Trading Loop (E6 I3)
- Separate intraday trading mode, runs hourly
- Evaluates same daily contracts at multiple points during the day
- Enters when edge/spread ratio is favorable
- Refines prediction with more recent METAR data
- Effort: 2 weeks

---

## Phase 19: Cost Model & Execution Realism

**Prerequisite:** Phase 0 items 1-4 complete (these are the quick fixes)

### 19.1 — Centralized Cost Model (E6 I1)
- Create `core/market_cost_model.py` — single source of truth for spread, slippage, commission
- Wire bid/ask from Kalshi API into execution cost (E6 I2)
- Build market depth analysis for position sizing (E6 I4)
- Replace hardcoded 0.5¢ spread with dynamic real spread from API

### 19.2 — Execution Simulator
- Realistic execution model: random fill price within spread, partial fill probability based on volume
- Market impact: position_size / market_depth
- Monte Carlo simulation: 1000 scenarios per trade
- Report expected P&L range (5th, 50th, 95th percentile instead of single point)

### 19.3 — Market Micro Ideas (E6)
- Idea 1: Spread-based entry signal — widening spreads → informed traders pulling liquidity
- Idea 2: Volume-based momentum — volume spikes confirm or contradict signal direction
- Idea 3: Settlement-time arbitrage — last hour, contracts may misprice vs known METAR temp

### 19.4 — Contract Selection Optimizer (E6 I5)
- When multiple contracts available, pick the best risk-adjusted edge
- Prefer D+1 when spread is tight, D+2 when D+1 spread is excessive
- Effort: 3 days

---

## Phase 20: Architecture Decomposition

**Prerequisite:** Phase 0 items 14-16 complete (bare excepts, datetime, DB strategies)

### 20.1 — Monolith Extraction (E3 Elephant 1)
Extract 4 files accounting for 24% of codebase:
- `paper_trading_engine.py` (3,105 lines) → signal pipeline, trade execution, P&L tracking, settlement processing
- `metar_monitor.py` (5,015 lines) → data collection, data processing, health monitoring
- `kalshi_monitor.py` (3,062 lines) → market monitoring, price fetching, order management
- `signal_fusion.py` (1,268 lines) → fusion logic, compatibility checks

### 20.2 — Mode Separation (E3 Elephant 1)
- Separate paper trading from live trading code paths
- Paper: simulation mode, fake fills, no real money
- Live: real fills, risk controls active, kill switch, alerting
- Shared: signal pipeline, DB schemas, utility functions

### 20.3 — DB Connection Standardization (E3 I1)
- Centralized connection pool/factory replacing 15+ independent `CREATE TABLE IF NOT EXISTS`
- Schema registry with version tracking
- Automated migration testing

### 20.4 — Import Path Standardization (E3 I6)
- Choose one convention: relative imports for core modules, absolute for top-level
- Add `__all__` exports to all modules
- Add `pyproject.toml` with project metadata

### 20.5 — Architecture Ideas (E3)
- Idea 1: Dependency injection for DB connections
- Idea 2: In-process event bus for decoupling
- Idea 3: Schema registry with automated migration testing
- Idea 4: Plugin-based backtest framework

---

## Phase 21: Test Infrastructure

**Prerequisite:** Phase 0 complete (can't test broken code)

### 21.1 — Unit Tests (E2/E3 Elephant)
- Test signal evaluation for each of 9 signals
- Test agreement gate logic
- Test position sizing calculation
- Test trade journal recording
- Target: 80% coverage on core modules

### 21.2 — Integration Tests
- Test end-to-end pipeline: signal → agreement → position sizing → trade execution
- Test database schema creation and migration
- Test alert dispatch and webhook delivery
- Test Kalshi API wrapper (with mock)

### 21.3 — Real Backtest Runner (Phase 0 item 8)
- Replace Monte Carlo simulation-based Phase 14 tests
- Historical walk-forward backtest using real settlement data
- Report: directional accuracy, P&L, Sharpe, drawdown, max consecutive losses
- Separate P&L calculation from signal accuracy (fix Elephant 2)

### 21.4 — Test Automation
- CI pipeline (GitHub Actions or Render native)
- Run on every PR to main
- Fail on: test failures, syntax errors, coverage drops

---

## Phase 22: Production Operations

**Prerequisite:** Phase 0 items 17-19 complete (cron, alerting, logging)

### 22.1 — Deployment Pipeline (E4 Elephant 1)
- `render.yaml` for infrastructure-as-code
- Pre-deploy health bootstrap script (`scripts/bootstrap.sh`)
- `make deploy-check` target: syntax check, env vars, DB connectivity, webhook dry-run
- Staging environment (in addition to PROD)
- Rollback procedure documented

### 22.2 — Monitoring & Observability (E4 Elephant 2)
- Proper `/healthz` endpoint returning 503 when DB is disconnected
- Structured JSON logging (timestamp, level, module, event_id, structured message)
- Synthetic heartbeat monitoring (test webhook every 5 min)
- Database health monitor (PRAGMA integrity_check, auto-recovery)
- Uptime tracking, error tracking
- Log aggregation (Render logs or external)

### 22.3 — Ops Ideas (E4)
- Idea A: Unified AlertPipeline class (6 files with overlapping delivery logic → 1)
- Idea B: Synthetic heartbeat monitoring
- Idea C: Database health monitor with auto-recovery

---

## Phase 23: Signal Improvements

**Prerequisite:** Phase 0 items 9-10 complete (NwpDirectSignal, Pressure Delta)

### 23.1 — Station-Specific Effects (E1 Elephant 2)
- Build station-specific wind direction → temperature mappings (E1 I2)
- Santa Ana winds at KLAX, upslope effects at KDEN, sea breeze at KSEA
- Use 24 months of METAR data, regress wind direction on ΔT

### 23.2 — Signal Independence Validation (E1 Elephant 1)
- Measure pairwise signal independence for all 9 active signals
- Identify reversion cluster dominance (if signals are all finding the same thing)
- Add Ensemble Diversity Score (exists but not validated)

### 23.3 — Frontal Detection Fix (E1 I3)
- Convert from daily aggregate comparison to 3-6 hour window analysis
- Use raw METAR observations from `metar_observations` table

### 23.4 — Seasonal Diurnal Curve Model (E1 I5)
- Model expected diurnal temperature curve for each station
- Use as baseline for anomaly detection
- Cloud cover as confidence modulator (E1 Idea 3)

### 23.5 — Dual-Polarity Signal Framework (E1 Idea 2)
- Warm-season / cool-season regime gating
- Many signals reverse direction between seasons
- Pressure Delta fix is the first use case

### 23.6 — Meteorology Ideas (E1)
- Idea 1: Lagrangian trajectory — trace 850-mb air parcels backward 12h
- Idea 2: Dual-polarity signal framework
- Idea 3: Cloud cover as DTR confidence modulator

---

## Phase 24: Future Data Sources

### 24.1 — HRRR Integration
- 3km resolution, hourly updates, 0-48h forecasts
- Available via Open-Meteo (same API as GFS)
- Build after Phase 18 (short-duration trading)
- Scope doc: `docs/plans/HRRR-COLLECTION-SCOPE.md`

### 24.2 — ECMWF Backfill
- Current: 264 predictions vs 2,010 GFS — invalid comparison
- Backfill to match GFS coverage
- Expected: 1-2% improvement over GFS

### 24.3 — AI Model Exploration
- AIGFS, GraphCast, GenCast, Pangu-Weather, AIFS
- All open source / available via Open-Meteo
- Blocked on: AI/ML gate (Gray Room decision — CLOSED for steps 1-4)
- Consider after short-duration trading is live

---

## Goldilocks: Intraday Arbitrage (PARKED)

**Separate lane — not a forecasting signal.**

Concept: Watch live METAR feed for brief temperature spikes (single-tick jumps). Those spikes set the daily high, but most traders don't catch them. Buy the market at a discount before it re-prices.

**Current status:** GoldilocksSignal in registry (0.11% standalone). Separate lane flag exists but disabled. Needs dedicated intraday test harness, expert consultation, and pricing model.

**Trigger:** Re-visit after Phase 18 (short-duration trading) is live and stable.

---

## Summary: All Action Items by Priority

### P0 (19 items — must do before anything else)
1. Fix settlement price calculation
2. Fix spread assumption (0.5¢ → 3.1¢)
3. Fix fill_price to include spread
4. Centralize cost model
5. Wire risk_controls.py into engine
6. Fix 3 conflicting position sizing systems
7. Fix Kelly formula
8. Remove fake P&L from backtest
9. Fix NwpDirectSignal evaluate()
10. Fix Pressure Delta direction mapping
11. Fix market_prob hardcoded to 0.5
12. Stop random data in calibration dashboard
13. Fix conflicting Flask apps
14. Fix 18 bare excepts
15. Fix 49+ naive datetime.now()
16. Fix 3 conflicting DB strategies
17. Fix cron jobs in ERROR state
18. Fix alert pipeline env vars
19. Add structured logging

### P1 (14 items — should fix before Phase 17)
1. Signal independence validation
2. Station-specific wind→temp mappings
3. Frontal detection sub-hourly fix
4. ROGR reversion signal (Phase A)
5. METAR dT/dt signal (Phase A)
6. Pressure tendency signal (Phase A)
7. Unified dashboard (Phase 17.1-17.4)
8. Real backtest runner
9. Mode separation (paper/live)
10. Import path standardization
11. DB schema registry
12. Deployment pipeline
13. Monitoring/observability
14. Unit tests (core modules)

### P2 (6 items — nice to have)
1. HRRR collection (Phase B)
2. ESDR signal (Phase C)
3. Intraday trading loop (Phase D)
4. Contract selection optimizer
5. Slippage market-aware model
6. Execution simulator (Monte Carlo)

### P3 (future)
1. Lagrangian trajectory
2. Dual-polarity signal framework
3. Cloud cover confidence modulator
4. Goldilocks intraday arb
5. ECMWF backfill
6. AI model exploration
7. Settlement-time arbitrage
8. Volume-based momentum signal

---

## Key Artifacts
- Gray Room R9 synthesis: `docs/plans/gray-room-round9/GRAY-ROOM-ROUND9-SYNTHESIS.md`
- Gray Room R9 files: `docs/plans/gray-room-round9/EXPERT[1-6]-*.md`
- Gray Room R8 files: `docs/plans/gray-room-round8/`
- Gray Room R7 (short-duration): `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS-2026-07-21.md`
- Gray Room R6 (fusion): `docs/plans/WEATHER-ENGINE-GRAY-ROOM-ROUND6-SYNTHESIS.md`
- Phase 15 code review: `docs/plans/PHASE15-CODE-REVIEW-2026-07-21.md`
- NWP Implementation spec: `docs/plans/NWP-DIRECT-IMPLEMENTATION-SPEC.md`
- HRRR scope: `docs/plans/HRRR-COLLECTION-SCOPE.md`
- TASKS: `prototypes/weather-engine-source/TASKS.md`
- ACCOMPLISHMENTS: `prototypes/weather-engine-source/ACCOMPLISHMENTS.md`