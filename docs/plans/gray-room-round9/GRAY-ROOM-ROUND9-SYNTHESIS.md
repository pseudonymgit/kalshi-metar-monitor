# Gray Room Round 9 — Synthesis

**Date:** 2026-07-22
**Panel:** 6 experts (Meteorology, Quant Finance, Architecture, Production Ops, Dashboard, Market Microstructure)
**Format:** 10+ errors, 5 improvements, 3 ideas, 2 elephants each — all with specs
**Status:** ✅ COMPLETE

---

## Expert Summaries

### Expert 1 — Meteorology (12 errors, 5 improvements, 3 ideas, 2 elephants)
- **P0:** NwpDirectSignal `evaluate()` is a no-op (returns None, 0.0)
- **P1:** Dewpoint Depression Modulator queries wrong table (`observations` vs `metar_observations`)
- **P1:** Pressure Delta signal has physically reversed direction mapping (rising pressure → warming, falling → cooling — wrong)
- **P1:** Seasonal window is ±45 days (data limitation) vs ±15 (spec), degrading analog quality
- **P2:** Wind Direction Shift assumes northerly=cooling — fails for Santa Ana (KLAX), upslope (KDEN)
- **P3:** Frontal Detector uses daily aggregates for 3-6 hour frontal phenomena — non-predictive
- **Elephant 1:** Signal independence not validated — reversion cluster dominates
- **Elephant 2:** No geographic/station-specific effects in any signal

### Expert 2 — Quant Finance (12 errors, 5 improvements, 3 ideas, 2 elephants)
- **P0:** Backtest engine simulates closing prices from settlement — P&L is fake
- **P0:** Kelly sizing uses variance of ALL trades, not just the asset's — formula is wrong
- **P0:** Risk controls not actually wired — `paper_trading_engine.py` doesn't import or call `risk_controls.py`
- **P0:** Position sizing has 3 conflicting systems (Kelly, fixed, risk-budget) — all can fire simultaneously
- **P1:** Fee rate set to 0.0 hides actual spread cost -> P&L overstatement
- **P1:** Strategy scoring — combinatorial search uses conflicting metrics without normalization
- **Elephant 1:** Backtest measures "did we call the direction right" not "did we make money"
- **Elephant 2:** Zero test infrastructure — no unit tests, no integration tests

### Expert 3 — Architecture (12 errors, 5 improvements, 3 ideas, 2 elephants)
- **P0:** 4 files = 24% of codebase in a single monolithic module — no module boundaries
- **P0:** 3 conflicting DB connection strategies — no connection pool, no context managers
- **P0:** Naive `datetime.now()` everywhere — 49+ calls, no timezone awareness
- **P0:** 18 bare `except:` clauses — swallows KeyboardInterrupt, SystemExit
- **P1:** Import cycle risk — `paper_trading_engine.py` imports signals, signals import config, config imports engine
- **P1:** Error handling inconsistent — 4 different patterns across 118 files
- **Elephant 1:** **No mode separation** — paper and live trading share the same code paths
- **Elephant 2:** **No test infrastructure** after 52,000 lines of production code

### Expert 4 — Production Ops (12 errors, 5 improvements, 3 ideas, 2 elephants)
- **P0:** Multiple cron jobs in ERROR state — no recovery mechanism
- **P0:** Alert pipeline has mismatched env var names (WEBHOOK_PROD vs DISCORD_WEBHOOK_PROD)
- **P0:** No structured logging — `print()` statements mixed with logging calls
- **P1:** .bashrc and load_webhooks.sh have inconsistent variable naming
- **P1:** Dashboards use Flask dev server (single-threaded, no WSGI)
- **P1:** No database backup automation for paper_trading.db or trade_journal.db
- **Elephant 1:** **No deployment pipeline** — manual Render deploy, no rollback, no staging
- **Elephant 2:** **No monitoring** — no uptime checks, error tracking, or performance metrics

### Expert 5 — Dashboard (13 errors, 6 improvements, 4 ideas, 2 elephants)
- **P0:** `market_prob` hardcoded to 0.5 everywhere — dashboard shows no real market data
- **P0:** Calibration dashboard silently generates random data via `random.uniform()` when DB unavailable
- **P0:** Two conflicting Flask apps on different ports — will crash on Render
- **P0:** No P&L or position data — dashboard completely disconnected from trading engine
- **P1:** Hardcoded absolute filesystem paths in calibration dashboard
- **P1:** Huge inline HTML template in Python file — zero maintainability
- **P1:** Conflicting Plotly CDN versions
- **Elephant 1:** Three separate dashboards with zero integration
- **Elephant 2:** No real-time data pipeline — dashboard is a static page

### Expert 6 — Market Microstructure (13 errors, 5 improvements, 3 ideas, 2 elephants)
- **P0:** Spread assumption hardcoded at 0.5¢ but actual mean spread is 3.1¢ — 6.2x underestimation
- **P0:** `fill_price = market_price` with no spread/slippage — no execution realism
- **P0:** Settlement price calculation uses wrong threshold (temperature > 50.0°F instead of strike price)
- **P0:** Cost model disagreement — FeeAwareEntryFilter uses 2¢, paper_trading_engine uses 0.5¢
- **P1:** LowLiquidityTrapFilter is a no-op — snapshot data never populated
- **P1:** Bid/ask spread data from Kalshi API is never used in execution decisions
- **Elephant 1:** **Entire cost model is wrong** — paper P&L is not real P&L (3.6-5.1x underestimation)
- **Elephant 2:** **Zero execution realism** — no slippage, no impact, no fill uncertainty

---

## Disposition

All 6 experts **ADVANCE**. Total findings: 74 errors, 32 improvements, 20 ideas, 12 elephants.

### P0 — Must fix before going live
| # | Item | Expert | Effort |
|---|------|--------|--------|
| 1 | NwpDirectSignal evaluate() is a no-op | E1 | 1h |
| 2 | Pressure Delta signal has reversed direction mapping | E1 | 1h |
| 3 | Backtest engine simulates P&L from settlement — fake P&L | E2 | 1w |
| 4 | Kelly sizing uses wrong variance formula | E2 | 2h |
| 5 | Risk controls not wired into paper_trading_engine.py | E2 | 1h |
| 6 | 3 conflicting position sizing systems can fire simultaneously | E2 | 2h |
| 7 | 18 bare except clauses | E3 | 0.5h |
| 8 | 49+ naive datetime.now() calls | E3 | 2h |
| 9 | 3 conflicting DB connection strategies | E3 | 4h |
| 10 | No mode separation (paper vs live) | E3 | 1w |
| 11 | Cron jobs in ERROR state | E4 | 2h |
| 12 | Alert pipeline env var mismatch | E4 | 0.5h |
| 13 | No structured logging | E4 | 4h |
| 14 | market_prob hardcoded to 0.5 | E5 | 1h |
| 15 | Calibration dashboard generates random data | E5 | 2h |
| 16 | Spread assumption 6.2x off (0.5¢ vs 3.1¢) | E6 | 1h |
| 17 | fill_price = market_price (no spread) | E6 | 0.5h |
| 18 | Settlement price wrong threshold | E6 | 1h |
| 19 | FeeAwareEntryFilter and paper_trading_engine disagree on spread | E6 | 1h |

### P1 — Should fix before Phase 17
| # | Item | Expert | Effort |
|---|------|--------|--------|
| 1 | Dewpoint Depression queries wrong table | E1 | 1h |
| 2 | Seasonal window ±45 vs ±15 | E1 | 1h |
| 3 | Signal independence not validated | E1 | 1w |
| 4 | Fee rate 0.0 hides spread cost (separate issue from E6's cost model) | E2 | 2h |
| 5 | Combinatorial search uses conflicting metrics | E2 | 2h |
| 6 | Import cycle risk | E3 | 2h |
| 7 | Error handling inconsistent (4 patterns) | E3 | 4h |
| 8 | No deployment pipeline | E4 | 1w |
| 9 | No monitoring/uptime checks | E4 | 3d |
| 10 | No database backup automation | E4 | 1h |
| 11 | Conflicting Flask apps on different ports | E5 | 2h |
| 12 | No P&L or position data in dashboard | E5 | 1w |
| 13 | LowLiquidityTrapFilter is a no-op | E6 | 1h |
| 14 | Bid/ask spread never used | E6 | 3h |

### P2 — Nice to have
| # | Item | Expert | Effort |
|---|------|--------|--------|
| 1 | Frontal Detector uses daily aggregates | E1 | 3d |
| 2 | No geographic effects in any signal | E1 | 2w |
| 3 | No unit tests/integration tests | E2/E3 | 2w |
| 4 | No contract selection logic | E6 | 3d |
| 5 | Slippage not market-aware | E6 | 2h |
| 6 | Intraday trading loop | E6 | 2w |

---

## Roadmap Impact

### P0 Fixes (priority order):
1. Fix settlement price calculation (wrong threshold)
2. Fix spread assumption (0.5¢ → 3.1¢) and wire bid/ask from API
3. Fix fill_price to include spread/slippage
4. Wire risk_controls.py into paper_trading_engine.py
5. Fix all 3 conflicting position sizing systems
6. Fix Kelly formula (wrong variance)
7. Remove fake P&L from backtest
8. Fix 18 bare excepts
9. Fix 49+ naive datetime.now() calls
10. Fix NwpDirectSignal evaluate() (no-op)
11. Fix Pressure Delta direction mapping
12. Fix market_prob hardcoded to 0.5
13. Stop random data generation in calibration dashboard
14. Fix cron jobs in ERROR state
15. Fix alert pipeline env vars
16. Add structured logging

### Phase 17 (Dashboard) — blocked on:
- P0 items 1-3, 12-13 (cost model and market data)
- P0 items 4-6 (position sizing)
- Unification of 3 dashboards into one

### Short-duration trading — blocked on:
- P0 items 1-3 (cost model must be right first)
- P1 items 13-14 (liquidity filter and bid/ask)
- Proper intraday trading loop (P2, 2 weeks)

---

## Files

| File | Lines | Size |
|------|-------|------|
| `docs/plans/gray-room-round9/GRAY-ROOM-ROUND9-PREREAD.md` | — | 3.6KB |
| `docs/plans/gray-room-round9/EXPERT1-METEOROLOGY.md` | 519 | 33.8KB |
| `docs/plans/gray-room-round9/EXPERT2-QUANT-FINANCE.md` | 575 | 31.9KB |
| `docs/plans/gray-room-round9/EXPERT3-ARCHITECTURE.md` | 666 | 36.1KB |
| `docs/plans/gray-room-round9/EXPERT4-OPS.md` | 500+ | 28.0KB |
| `docs/plans/gray-room-round9/EXPERT5-DASHBOARD.md` | 289 | 29.6KB |
| `docs/plans/gray-room-round9/EXPERT6-MARKET-MICRO.md` | 350+ | 16.5KB |
| `docs/plans/gray-room-round9/GRAY-ROOM-ROUND9-SYNTHESIS.md` | — | This file |