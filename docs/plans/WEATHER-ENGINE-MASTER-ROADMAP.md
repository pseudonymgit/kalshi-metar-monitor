# Weather Engine — Comprehensive Roadmap
**Updated:** 2026-08-05 01:20 UTC
**Prioritized by:** Need × Impact × Ease (highest first)

---

## P0 — CRITICAL FIXES (Fix now, while running)

| # | What | Why | Effort | Status |
|---|------|-----|--------|--------|
| 1 | **Unify fee model across all modules** | Kalshi fee = `ceil(0.07 × P × (1-P) × 100) / 100` per side. `alert_formatter.py` uses 0.1% (wrong). `market_cost_model.py` uses 2.05¢ spread approximation (close but not exact). `kalshi_fee()` in cron has the right formula. Need one source of truth. | 2h | 🔴 |
| 2 | **Fix alert_formatter.py fee default** | Line 49: `fee_rate = 0.001` → should be actual Kalshi fee formula | 0.1h | 🔴 |
| 3 | **DB connection context manager migration** | ~60 `sqlite3.connect()` calls without `with` blocks. Can leak connections under load. | 4h | 🟡 |
| 4 | **Delete empty GEFS DBs** | `gefs_operational.db`, `gefs_reforecast.db` — 0 bytes, nothing to lose | 0.1h | 🟡 |

## P1 — PIPELINE VALIDATION (Before trusting any results)

| # | What | Why | Effort | Status |
|---|------|-----|--------|--------|
| 5 | **Walk-forward backtest (90+ days)** | 68% on 266 trades is directional skill, but Kelly P&L and Sharpe need real Kalshi prices to validate. | 4h | 🟡 |
| 6 | **Per-station calibration curves** | Gray Room E5: confidence formula is uncalibrated. Walk-forward, not whole-window. | 4h | 🟡 |
| 7 | **UHI bias correction — ALL cities** | Not just KNYC/KLAX/KPHX/KDFW. Every city has an urban heat island effect. Nowcast stations provide the regional reference. | 3h | 🟡 |
| 8 | **Kalshi API price verification** | Gray Room E6: run 1 live Kalshi trade to verify the fee formula, price feed, and API auth all work. | 0.5h | 🟡 |
| 9 | **Paper trading re-enable** | After PEM fix, fee fix, and Kalshi verification. Cron disabled until clean. | 0.1h | 🔴 |

## P2 — SIGNAL EXPANSION (Build the edge)

| # | What | Why | Effort | Status |
|---|------|-----|--------|--------|
| 10 | **82-member signal design** | GEFS 31 + ECMWF 50 = 81/82. Log-odds Bayesian fusion from member divergence. First-principles, not ML. | 4h | 🟡 |
| 11 | **Edge 20 multi-model ensemble** | NWP data is already backfilled (2,045 dates). GFS, ECMWF, ICON, GEM deterministic. Wire it. | 4h | 🟡 |
| 12 | **Nowcast backfill complete** | 9 stations → Aug 4. 43 extra stations → 2021→today. | 4h | 🔄 |
| 13 | **ECDS backfill (2021-2023)** | 758 dates ECMWF 50-member. Then 2026 gap (114 dates). Running now. | ~26h | 🔄 |
| 14 | **ECMWF shadow test** | 30 days parallel, wire only if adds >2pp over GEFS-alone. | 4h | 🟡 |

## P3 — NEW LANES (Trajectory + Goldilocks)

| # | What | Why | Effort | Status |
|---|------|-----|--------|--------|
| 15 | **Gray Room → Trajectory lane** | Heavy informant, not a gate. Uses epoch-based analog matching. | 4h | 🟡 |
| 16 | **Gray Room → Goldilocks lane** | Fleeting temp-tick microstructure alerts at bucket boundaries. | 4h | 🟡 |
| 17 | **IEM ASOS 1-min data expansion** | KNYC has only 16 days of 1-min data. Need to expand coverage for goldilocks to work. | 6h | 🟡 |

## P4 — INFRASTRUCTURE (Make it maintainable)

| # | What | Why | Effort | Status |
|---|------|-----|--------|--------|
| 18 | **Centralized signal registry** | 30+ signal modules, no manifest. Track which are wired, which are experimental, which are dead. | 2h | 🟡 |
| 19 | **Experiment archive** | 8+ root-level experiment scripts → `scripts/archive/` with date stamps. | 0.5h | 🟡 |
| 20 | **Backup directory cleanup** | `core.backup.YYYY_phase3_discovery/` → archive or remove. | 0.5h | 🟡 |
| 21 | **DB consolidation** | Merge paper_trading*.db → 1 active + 1 backup. Merge forecast_disagreement*.db → 1 DB. | 2h | 🟡 |
| 22 | **Graphify map auto-update cron** | Set up weekly cron to regenerate the knowledge graph. | 0.5h | 🟡 |

## P5 — ADVANCED FEATURES (After core validated)

| # | What | Effort | Gate |
|---|------|--------|------|
| 23 | Portfolio correlation matrix | 12h | After calibration curves |
| 24 | Epoch-based Kelly schedule | 2h | After portfolio correlation |
| 25 | Spatial coherence wiring | 2h | After Edge 20 |
| 26 | Regime-based confidence modulation | 4h | After calibration |
| 27 | Dewpoint ceiling cap | 1h | After calibration |
| 28 | Low liquidity traps wiring | 2h | Before real money |

## P6 — PRODUCTION READINESS (Phase B)

| # | What | Items | Status |
|---|------|-------|--------|
| 29 | Production reliability | 10 items (R1-R10): market discovery, alert emissions, ingestion parity, stalled detection, integer-cross, scheduler validation, milestone tagging, emission logging, health endpoint, domain guard | 3/10 done |
| 30 | Regression alert governance | 6 items (R11-R16): taxonomy, triggers, notification separation, replay hook, suppression transparency, snapshot replacement | 0/6 done |
| 31 | Alert governance hardening | 4 items (R17-R20): schema freeze, event log format, versioning policy, distribution topology | 0/4 done |
| 32 | Signal engine phases 2-8 | Proximity regime, price suppression, time weighting, rehydration, classification, Goldilocks priority, payload upgrade | 0 done |

## P7 — PARK (For later)

| # | What | Gate |
|---|------|------|
| 33 | 30-day paper test | All P0-P4 deployed |
| 34 | Real-money go decision | All 5 gates met |
| 35 | Fee verification (1 live trade) | When actually trading |
| 36 | ECMWF shadow test | Backfill complete |
| 37 | 3 ERROR'd crons (luna-price, db-snapshot, restore-drill) | Not weather engine |
| 38 | Dead code tagging | Convenience |
| 39 | Phase 6.2 search scripts | When relevant |

## GO/NO-GO GATES (Before real money)

| Gate | Status | Detail |
|------|--------|--------|
| 30 days paper testing | ❌ | Requires P0-P4 deployed |
| ≥60% directional accuracy | ❓ 68% on 266 trades | Needs walk-forward validation |
| ≥0.30 Sharpe | ❌ | Not computed with corrected Kelly + real prices |
| No >10% single-day drawdown | ❌ | Untested at $175 max position |
| Settlement-confirmed accuracy | ❌ | 266 < 1,000 needed for significance |

**Verdict: All 5 gates fail. Not close to real money.**

## COMPLETE INVENTORY

| Tier | Items | DONE | IN PROGRESS | TODO |
|------|-------|------|-------------|------|
| P0 — Critical fixes | 4 | 0 | 0 | 4 |
| P1 — Pipeline validation | 5 | 0 | 0 | 5 |
| P2 — Signal expansion | 5 | 0 | 2 | 3 |
| P3 — New lanes | 3 | 0 | 0 | 3 |
| P4 — Infrastructure | 5 | 0 | 0 | 5 |
| P5 — Advanced features | 6 | 0 | 0 | 6 |
| P6 — Production readiness | 4 | 0 | 0 | 4 |
| P7 — Park | 7 | 0 | 0 | 7 |
| Go/No-Go gates | 5 | 0 | 0 | 5 |
| **Total** | **44** | **0** | **2** | **42** |

*Note: Does not count the 18 items already completed (6 Gray Room errors, 1 idea, 2 improvements, etc.)*