# Gray Room Round 8 — Phase 16: Functionality Review & System Architecture

**Date:** 2026-07-22
**Panel:** 3 experts
**Moderator:** Donna Paulsen

---

## The Advisory Question

We have completed Phases 1-15 (119 files reviewed, 54 with bugs, 9 active signals at 72.3% accuracy). Phase 15 code review revealed architectural issues, orphan files, test methodology gaps, and a pending shift to short-duration trading. We need expert judgment on:

1. Codebase refactoring strategy (monolith, orphans, test methodology)
2. Short-duration trading build order (signals, entry/exit, data pipeline)
3. Production trading dashboard design (architecture, priorities, data flow)

---

## Expert Dispositions

| Expert | Domain | Output | Disposition |
|---|---|---|---|
| E1 | Software Architecture & Testing | 14 errors, 5 improvements, 4 ideas, 3 elephants | **ADVANCE** |
| E2 | Short-Duration Trading Pipeline | 12 errors, 7 improvements, 3 ideas, 3 elephants | **ADVANCE** with Phase 0 KILL GATE |
| E3 | Production Trading Dashboard | 12 errors, 5 improvements, 3 ideas, 2 elephants | **ADVANCE** |

**All 3 experts ADVANCE.** No KILLs. No PARKs.

---

## Key Findings

### 1. Agreement Threshold Default Mismatch (E1)
The code defaults to `AGREEMENT_THRESHOLD=2` but the changelog claims 3. The strategic trade-off (agree=2 → 66.2%, 9,977 trades vs agree=3 → 72.3%, 2,657 trades) is undocumented. **Fix:** Change default to "3", add startup log, document trade-off.

### 2. Dead Signal Still Registered (E1)
`nwp_analog` (49.2%) is still registered in `__init__.py` and `paper_trading_engine.py`. It degrades ensemble performance. **Fix:** Remove from registry and all imports. Keep file on disk for reference.

### 3. Live Signal Not Registered (E1)
`nwp_direct` (92.7% GFS direction) exists on disk but is NOT wired into the pipeline. The `evaluate()` method is a stub returning `(None, 0.0)`. **Fix:** Register in `__init__.py`, complete `evaluate()`, wire into pipeline.

### 4. Fee Rate Contradiction (E1, E3)
`position_sizing.py` dataclass defaults to 0.0 but `FeeAwareKellyPositionSizer` defaults to 0.05. 3+ other files still have 0.05. **Fix:** All fee rates to 0.0. Kalshi charges zero commission.

### 5. Phase 14 Tests Are Simulations, Not Backtests (E1, E2)
The Phase 14 "30-day unattended test" uses `random.gauss()` to simulate results. There is no real backtest that runs the actual pipeline against historical data. **Fix:** Build a real backtest runner that instantiates `PaperTrader` in replay mode.

### 6. No Sub-Daily Contracts on Kalshi (E2) — CRITICAL ELEPHANT
Kalshi offers only daily HIGH/LOW temperature contracts. There are no hourly, 3-hour, or intraday contracts. The entire short-duration premise must be validated. **Fix:** Phase 0 KILL GATE — verify contract types, run spread analysis. If round-trip cost > 5¢, KILL short-duration project.

### 7. No P&L Visibility on Any Dashboard (E3)
The three existing dashboards query METAR data, calibration metrics, and in-memory analytics — but NONE query `paper_trading.db` for P&L, positions, or trade history. **Fix:** Build a new trading dashboard with P&L as Priority 1.

### 8. Three Fragmented Dashboards Must Be Replaced, Not Extended (E3)
`dashboard.py`, `confidence_dashboard.py`, `calibration_dashboard.py` were built by different people for different purposes. None can be extended for trader workflows. **Fix:** Build `core/trading_dashboard/` as a package. Deprecate old dashboards.

### 9. System Has No Concept of Trade Duration (E2, E3)
No `duration_hours`, `entry_time_utc`, or `exit_time_utc` anywhere in the codebase. This blocks short-duration trading and distorts performance analytics. **Fix:** Add duration to trade records, position sizing, risk management.

### 10. Alert Pipeline Variable Name Mismatch (E1)
`.bashrc` sources `.env.webhooks` (sets `WEBHOOK_PROD`) but code expects `DISCORD_WEBHOOK_PROD`. Cron wrapper does remapping correctly; interactive sessions don't. **Fix:** Point `.bashrc` at `load_webhooks.sh`.

---

## Disposition: Errors, Improvements, Ideas, and Elephants

### ERRORS — All 38

| # | Error | Expert | Priority | Disposition |
|---|---|---|---|---|
| 1 | Agreement threshold default mismatch | E1 | P0 | **ADVANCE** — Fix default to "3" |
| 2 | Dead signal nwp_analog (49.2%) still registered | E1 | P0 | **ADVANCE** — Remove from registry |
| 3 | Live signal nwp_direct (92.7%) not registered | E1 | P0 | **ADVANCE** — Register and wire |
| 4 | Fee rate contradiction (0.05 vs 0.0) | E1 | P0 | **ADVANCE** — Fix all to 0.0 |
| 5 | Phase 14 tests are simulations, not backtests | E1/E2 | P0 | **ADVANCE** — Build real backtest runner |
| 6 | No sub-daily Kalshi contracts (KILL GATE) | E2 | P0 | **ADVANCE** — Phase 0 validation |
| 7 | No P&L visibility on any dashboard | E3 | P0 | **ADVANCE** — Priority 1 dashboard feature |
| 8 | Three fragmented dashboards must be replaced | E3 | P0 | **ADVANCE** — Build new, deprecate old |
| 9 | No trade duration concept anywhere | E2/E3 | P0 | **ADVANCE** — Add duration to all layers |
| 10 | Alert pipeline variable name mismatch | E1 | P1 | **ADVANCE** — Fix .bashrc |
| 11 | 18 bare except clauses swallow errors | E1 | P1 | **ADVANCE** — Replace with except Exception |
| 12 | No intraday spread validation | E2 | P1 | **ADVANCE** — Build spread validator |
| 13 | No sub-daily signal generation exists | E2 | P1 | **ADVANCE** — Build short-duration signals |
| 14 | METAR sub-hourly data discarded | E2 | P1 | **ADVANCE** — Add observations table |
| 15 | No HRRR data pipeline | E2 | P1 | **ADVANCE** — Build HRRR collector |
| 16 | Entry window too rigid for short-duration | E2 | P1 | **ADVANCE** — Condition-based entry |
| 17 | No intraday trade tracking in DB schema | E2 | P1 | **ADVANCE** — Add schema columns |
| 18 | Alert throttling cooldowns too long | E2 | P1 | **ADVANCE** — Duration-aware cooldown |
| 19 | No stop-loss/take-profit mechanism | E2 | P1 | **ADVANCE** — Add risk controls |
| 20 | No sub-daily backtest infrastructure | E2 | P1 | **ADVANCE** — Build short-duration backtest |
| 21 | Agreement gate not duration-aware | E2 | P1 | **ADVANCE** — Dual thresholds |
| 22 | NWP update schedule too infrequent | E2 | P1 | **ADVANCE** — 4x/day cron |
| 23 | Hardcoded market_prob = 0.5 on dashboard | E3 | P1 | **ADVANCE** — Real market prices |
| 24 | No position management | E3 | P1 | **ADVANCE** — Priority 2 dashboard feature |
| 25 | No alert feed on dashboard | E3 | P1 | **ADVANCE** — Priority 2 dashboard feature |
| 26 | ConfidenceTracker is in-memory only | E3 | P1 | **ADVANCE** — Persist to DB |
| 27 | No time range controls on dashboards | E3 | P2 | **ADVANCE** — Add date range filters |
| 28 | No portfolio-level statistics | E3 | P2 | **ADVANCE** — Priority 4 dashboard feature |
| 29 | No real-time refresh | E3 | P2 | **ADVANCE** — Poll-based refresh |
| 30 | No risk metrics visualization | E3 | P2 | **ADVANCE** — Priority 3 dashboard feature |
| 31 | No signal quality metrics on dashboard | E3 | P2 | **ADVANCE** — Priority 4 dashboard feature |
| 32 | Dashboard uses dead/non-existent signals | E3 | P2 | **ADVANCE** — Update signal references |
| 33 | position_sizing.py uses naive datetime.now() | E1 | P2 | **ADVANCE** — Fix timezone |
| 34 | No paper trading suppression for alerts | E1 | P2 | **ADVANCE** — Add paper mode flag |
| 35 | No safety interlock between paper/live | E1 | P2 | **ADVANCE** — Add mode flag + confirmation |
| 36 | No mode separation in DB | E1 | P2 | **ADVANCE** — Add mode column |
| 37 | 87+ div-by-zero risks remain | E1 | P3 | **ADVANCE** — Fix in batches |
| 38 | 49+ naive datetime.now() remain | E1 | P3 | **ADVANCE** — Fix in batches |

### IMPROVEMENTS — All 17

| # | Improvement | Expert | Effort | Disposition |
|---|---|---|---|---|
| 1 | Extract signal pipeline from paper_trading_engine.py | E1 | Medium | **ADVANCE** — Phase 1 of monolith breakup |
| 2 | Extract alert building from paper_trading_engine.py | E1 | Medium | **ADVANCE** — Phase 2 |
| 3 | Extract position sizing into position_calculator.py | E1 | Medium | **ADVANCE** — Phase 3 |
| 4 | Extract DB access into trade_repository.py | E1 | Medium | **ADVANCE** — Phase 4 |
| 5 | Switch to structured logging | E1 | Low | **ADVANCE** — Low effort, high value |
| 6 | FOGR reversion signal | E2 | Medium | **ADVANCE** — Priority short-duration signal |
| 7 | METAR dT/dt trend signal | E2 | Low | **ADVANCE** — Quick win |
| 8 | Pressure tendency leading indicator | E2 | Low | **ADVANCE** — Quick win |
| 9 | Sequential trigger entry/exit architecture | E2 | High | **ADVANCE** — Core architecture change |
| 10 | Horizon-specific confidence thresholds | E2 | Low | **ADVANCE** — Easy config change |
| 11 | Duration-aware position sizing | E2 | Medium | **ADVANCE** — Protects against over-trading |
| 12 | Intraday signal decay model | E2 | Low | **ADVANCE** — Quick win |
| 13 | Build dedicated trading dashboard | E3 | High | **ADVANCE** — Core Phase 17 deliverable |
| 14 | Real market price integration | E3 | Medium | **ADVANCE** — P1 dashboard feature |
| 15 | Auto-refresh with configurable interval | E3 | Low | **ADVANCE** — Easy UX improvement |
| 16 | Alert feed inbox on dashboard | E3 | Medium | **ADVANCE** — P2 dashboard feature |
| 17 | Short-duration trading mode on dashboard | E3 | High | **ADVANCE** — P5, after foundation |

### IDEAS — All 10

| # | Idea | Expert | Disposition |
|---|---|---|---|
| 1 | Monitoring/observability sidecar process | E1 | **PARK** — Valuable but not urgent |
| 2 | Deterministic replay framework for backtesting | E1 | **ADVANCE** — Replaces simulation-based tests |
| 3 | Feature flag system for gradual signal rollout | E1 | **ADVANCE** — Enables canary testing |
| 4 | Containerize pipeline for reproducible deploys | E1 | **PARK** — Valuable but not urgent |
| 5 | Intraday spread momentum as confirming signal | E2 | **ADVANCE** — Only market-based signal |
| 6 | No-trade zone classification | E2 | **ADVANCE** — Reduce bad trades |
| 7 | Phase-dependent signal weighting | E2 | **ADVANCE** — Optimize by horizon |
| 8 | WebSocket-powered live price feed | E3 | **PARK** — Future, not MVP |
| 9 | ML-powered daily trade summary | E3 | **PARK** — After dashboard is stable |
| 10 | "Glass-box" explainability view | E3 | **ADVANCE** — Builds trust |

### ELEPHANTS — All 8

| # | Elephant | Expert | Severity | Disposition |
|---|---|---|---|---|
| 1 | 4 files = 24% of codebase (monolith) | E1 | CRITICAL | **ADVANCE** — 4-phase extraction plan |
| 2 | No real backtest pipeline (simulations only) | E1 | CRITICAL | **ADVANCE** — Build replay engine |
| 3 | No paper/live separation | E1 | HIGH | **ADVANCE** — Mode flag + safety interlock |
| 4 | Kalshi has no sub-daily contracts | E2 | CRITICAL | **ADVANCE** — Phase 0 KILL GATE |
| 5 | System has no concept of trade duration | E2 | CRITICAL | **ADVANCE** — Add duration to all layers |
| 6 | Real edge is market microstructure, not meteorology | E2 | HIGH | **ADVANCE** — Analyze accuracy vs P&L |
| 7 | Dashboard architecture must be redesigned, not extended | E3 | CRITICAL | **ADVANCE** — Build new, deprecate old |
| 8 | Data pipeline unreliable for real-time | E3 | HIGH | **ADVANCE** — In-memory event buffer |

---

## Build Order & Roadmap Impact

### Phase 16 (Gray Room): COMPLETE ✅
Synthesis written. 3 expert files at `docs/plans/gray-room-round8/`.

### Phase 17 (Production Dashboard): REVISED
Priority order per Expert 3:
1. **P1: P&L + Positions** — Build `/api/trading/pnl`, `/api/trading/positions`, `/api/trading/portfolio`
2. **P2: Alert Feed + Position Management** — Alert feed from trade_journal.db, open positions view
3. **P3: Risk Dashboard** — Drawdown, daily loss, kill switch state
4. **P4: Performance Analytics** — Signal accuracy, Sharpe, win rate by station/signal
5. **P5: Short-Duration Mode** — Faster refresh, micro-P&L, close buttons
6. **Architecture:** Build new `core/trading_dashboard/` package. Don't extend existing.

### Post-Phase 17: Revised
**Phase 0 — Validate the Premise (before any short-duration work)**
- Verify Kalshi contract offerings for sub-daily instruments
- Run spread analysis: round-trip cost for intraday entry/exit
- **KILL GATE:** If round-trip > 5¢ for all markets, abandon short-duration
- If ADVANCE: build order per Expert 2's 6-phase plan

**Phase A — Data Infrastructure**
- Raw METAR observations table
- HRRR collection pipeline
- NWP collection 4x/day

**Phase B — Short-Duration Signals**
- FOGR, dT/dt, pressure tendency
- Sequential trigger engine
- Duration-aware sizing and confidence

**Phase C — Market Microstructure (if Elephant 6 confirmed)**
- Spread momentum signal
- Execution quality analytics
- Latency monitoring

---

## Expert Files

| File | Expert | Lines | Key Findings |
|---|---|---|---|
| `docs/plans/gray-room-round8/EXPERT1-ARCHITECTURE.md` | E1 — Architecture | 332 | 14 errors, 5 improvements, 4 ideas, 3 elephants |
| `docs/plans/gray-room-round8/EXPERT2-TRADING-PIPELINE.md` | E2 — Trading Pipeline | 419 | 12 errors, 7 improvements, 3 ideas, 3 elephants |
| `docs/plans/gray-room-round8/EXPERT3-DASHBOARD.md` | E3 — Dashboard | 766 | 12 errors, 5 improvements, 3 ideas, 2 elephants |

---

## P0 Action Items (Immediate)

1. Agreement threshold: change default to "3", document trade-off
2. Signal registry: remove nwp_analog, register nwp_direct, complete evaluate()
3. Fee rates: fix all 5+ files to 0.0
4. Build real backtest runner (replace simulation-based tests)
5. Phase 0: Validate Kalshi sub-daily contracts (KILL GATE)
6. Build trading dashboard with P&L as Priority 1
7. Fix alert pipeline .bashrc variable name mismatch
8. Replace 18 bare except clauses with except Exception
9. Add trade duration to database schema

## Next Steps

1. Apply P0 fixes (agreement threshold, signal registry, fee rates)
2. Dispatch Phase 17 (Production Dashboard) to Gilfoyle
3. Run Phase 0 validation (Kalshi contract types)
4. Start building real backtest runner
5. Update roadmap and tracking files