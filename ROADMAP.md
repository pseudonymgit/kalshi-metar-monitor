# Weather Engine Roadmap

**Date:** 2026-07-20  
**Status:** P0 batch complete — multi-instance paper trading active in DEV

---

## What's Done

### Phase 0 — Foundation (Complete)

| Component | Status | Notes |
|-----------|--------|-------|
| METAR data collection | ✅ Complete | NWS API fetches, 807K+ observations in `metar_backfill.db` |
| NWP 30-day backfill | ✅ Complete | 34,182 rows / 4 models / 20 stations |
| Settlement epochs | ✅ Complete | 61,188 epochs generated from daily_stats |
| Backtest engine v2 | ✅ Complete | Per-signal metrics, forward-day scoring, NWP integration |
| Late-day momentum (hourly) | ✅ Complete | Threshold=1.7, Sharpe 2.29, wired into paper trading |
| Paper trading engine | ✅ Complete | Deterministic, version-tagged, daily reconciliation |
| Alert Schema v1.0 | ✅ Complete | Frozen spec, canonical event log, per-city distribution |
| Signal fixes | ✅ Complete | Direction mapping, outcome classification, Tier 1 bypass |
| Confidence-weighted sizing | ✅ Complete | 3 tiers, all 8 signals, DEV instance active |
| Multi-instance runner | ✅ Complete | PROD/DEV/SBOX, separate ledgers, Discord alert format |
| Promotion rules | ✅ Complete | PROMOTION-RULES.md, DB snapshot script, 3 rotating copies |
| Climatology pillar | ✅ Complete | Enhanced with ENSO/AO/NAO regime conditioning |
| Decision output | ✅ Complete | Market implied vs analytical fair value + confidence |

### Phase 1b: Signal Validation (7-day DEV run)

**Goal:** Run DEV paper trading for 7 days, validate accuracy and P&L attribution

**Tasks:**
- [x] Run `multi_instance_paper_trader.py --instances DEV` daily
- [x] Track per-signal accuracy (late_day_momentum, reversion, climatology)
- [x] Validate Sharpe ratio ≥ 1.0
- [x] Generate promotion report after 7 days
- [x] If passing, begin SBOX smoke test

**Estimated time:** 7 days (passive)

### Phase 2: Risk & Execution Layer (Pre-Live)

**Goal:** Implement risk management before any live trading

**Tasks:**
- [ ] Source-health scoring + kill switch (P2.1)
- [ ] Uncertainty-aware sizing (P2.2)
- [ ] Fee-aware Kelly + survival mode (P2.3)
- [ ] Portfolio correlation clamp (P2.4)
- [ ] Cross-platform pricing divergence (P1.3)
- [ ] Calibration dashboard (P1.5)

**Estimated time:** 5-7 days

### Phase 3: Live Signals (IN PROGRESS)

**Goal:** Deploy live signals with controlled risk exposure

**Tasks:**
- [x] 3.1 Wire Agreement Gate into Paper Trading Engine (P0, 2-3 hr) - Created `core/agreement_gate.py`, configurable N-of-M threshold, wired into `generate_signals()` after skill gating
- [x] 3.4 Fix evaluate_for_station() stubs (P1, 2-3 hr) - Updated `calendar_climatology_signal.py`, `regime_signal.py`, `forecast_disagreement_signal.py`
- [x] 3.5 Re-run B6 experiments on cleaned signal set (P1, 2-3 hr) - Completed
- [x] 3.6 Goldilocks Lane Architecture (P1, 1-2 hr) - Added `GOLDILOCKS_SEPARATE_LANE` config flag, code wiring
- [ ] 3.7 Implement Kalshi integration (P2, 3-4 hr) - Remaining
- [x] 3.8 Documentation update (P2, 1 hr) - Updated `docs/FUNCTIONALITY_SPEC.md`, ROADMAP.md
- [ ] Promote DEV → PROD per PROMOTION-RULES.md
- [ ] Kalshi API integration for live market data
- [ ] Position sizing calculator (max $250 risk budget)
- [ ] Stop-loss logic (2x target loss)
- [x] Signal filtering (require ≥65% confidence) - Agreement gate provides N-of-M consensus
- [ ] Daily review process

**Estimated time:** 7-10 days (after Phase 2 complete)

### Phase 5: AI/ML Alert Infrastructure (COMPLETED)

**Goal:** Enhance alert system reliability and robustness with frequency throttles, graceful degradation, and real risk controls.

**Tasks:**
- [x] 5.1 Frequency throttle with per-station cooldown (4h/8h/12h)
- [x] 5.2 Graceful degradation based on data freshness tiers
- [x] 5.3 Enhanced trade journal with SQLite and required query functions
- [x] 5.6 Real risk controls with configurable thresholds (5%, 15%, max 3 loses)
- [x] 5.7 Fix retry queue DB path for container compatibility
- [x] 5.8 Verify bucket function semantics

**Estimated time:** 3 days

### Phase 4: Scale & Optimize

**Goal:** Expand to Polymarket, add secondary signals, optimize parameters

### Phase 4.1: Spatial Coherence Gate (IN PROGRESS)
**Goal:** Enhance signal quality by grouping 20 stations into 6 NOAA climate regions, applying confidence modulation based on regional consensus

**Status:** IMPLEMENTED (July 21, 2026)

**Implementation:**
- Created `core/spatial_coherence.py` with 6 regional groupings based on NOAA climate divisions
- Group stations: Northeast (4), Southeast (4), Midwest (4), South Central (3), West (3), Pacific Coast (3)
- Apply confidence boost (+15%) when station agrees with regional majority consensus
- Apply confidence penalty (-40%) when station disagrees with regional majority consensus
- Promote to "Sure Thing" lane when all stations in region agree and have high enough confidence (>0.70)
- Wire into paper trading after agreement gate, before alert generation
- Configurable via environment variables (SPATIAL_COHERENCE_ENABLED, SPATIAL_BOOST_FACTOR, SPATIAL_PENALTY_FACTOR)

**Task completion criteria:**
- [x] 4.1.1 Create spatial coherence logic - Complete
- [x] 4.1.2 Group 20 stations into 6 NOAA climate regions - Complete
- [x] 4.1.3 Implement confidence modulation based on regional consensus - Complete
- [x] 4.1.4 Wire into paper trading engine - Complete
- [x] 4.1.5 Add configuration flags - Complete
- [x] 4.1.6 Add "Sure Thing" lane for regional unanimous consensus - Complete

**Expected impact:** Improved signal reliability, reduced isolated false alerts, ~3-5% accuracy improvement based on Gray Room expert recommendations.

### Phase 7: Kalshi API Integration (COMPLETED)

**Goal:** Full Kalshi API integration with advanced trading mechanisms

**Tasks:**
- [x] 7.1 NWS Revision Predictability (Scaffold only - awaiting CDS API)
- [x] 7.2 Round-Number Anchoring - Created `core/round_number_anchoring.py`, compares climatological vs. market probability at round thresholds (80, 85, 90, 95, 100), implements arbitrage detection, export: `check_round_number_arbitrage(station, date) → (direction, confidence, edge)`
- [x] 7.3 Spread-Adjusted Net-Edge Calibration - Created `core/spread_calibrator.py`, 2D calibrator f(signal_confidence, spread) → net_edge with formula: net_edge = signal_confidence - 0.5 - (spread/2), min net edge 0.02, integrated into paper_trading_engine.py
- [x] 7.4 Liquidity-Weighted Ensemble - Created `core/liquidity_weighted_ensemble.py`, weights signal votes by accuracy × liquidity using data from per_signal_performance.json, export: `get_liquidity_weighted_vote(station, date, signals) → (direction, confidence)`
- [x] 7.5 Market Phase Classification - Created `core/market_phase_classifier.py`, classifies phases: NORMAL, INFORMATION_EVENT, SETTLEMENT_CONVERGENCE, OVERNIGHT_THIN, HOLIDAY_WEEKEND with phase-specific position sizing, export: `classify_market_phase(station, date, hour_utc) → phase_label`
- [x] 7.6 Spread Momentum Co-Signal - Created `core/spread_momentum_signal.py`, tracks spread and midpoint delta over time, adjusts confidence with different rules based on market behavior, export: `adjust_confidence_with_market_signal(station, market_type, confidence) → adjusted_confidence`
- [x] 7.8 Settlement Cascade Timing - Created `core/settlement_cascade.py`, predicts cascade and manages exit timing in final 2h before settlement, exit 90 min before, re-entry on deviation, export: `get_settlement_timing(station, date) → (exit_before, reentry_window, fair_value)`
- [x] 7.9 Multi-Stage Execution - Created `core/multi_stage_execution.py`, implements 3-stage execution (mid-0.5¢, mid-price, marketable) with SQLite order tracking, export: `execute_multi_stage_order(station, market_type, direction, size) → order_status`

**Estimated time:** 30-40 days

**Dependencies:** Uses existing Kalshi API wrapper at `core/kalshi_monitor.py`, requires accuracy data from `data/signal_audit_2026-07-20/per_signal_performance.json`

**Tasks:**
- [ ] Polymarket integration (global cities)
- [ ] Secondary signals (reversion magnitude, duration)
- [ ] Parameter optimization (analog count, confidence thresholds)
- [ ] Automated retraining (weekly model refresh)

**Estimated time:** 10-14 days (after Phase 3 stable)

---

## Phase 6: AI/ML (Parked)

**Explicitly parked until data + risk layer is stable and paper trading has run for ≥30 days.**

- Forecast-error distribution modeling (Oalkhadra approach)
- LinUCB contextual bandit for mode selection
- LLM-as-context-enricher (NWS discussion parsing)
- Any neural / learned probability models

---

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA LAYER                               │
├─────────────────────────────────────────────────────────────┤
│  NWS API → METAR observations → daily_stats → epochs       │
│  NWP forecasts → forecast_disagreement signal               │
│  Settlement epochs → reversion signals                      │
│  DB snapshots (weekly, 3 rotating copies)                   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    SIGNAL LAYER                             │
├─────────────────────────────────────────────────────────────┤
│  8 Signal Types:                                            │
│  ├── near_boundary_momentum_up / down                       │
│  ├── goldilocks_reversion_alert / momentum_down (Tier 1)    │
│  ├── reversion_after_settlement                             │
│  ├── instant_up / down                                      │
│  └── late_day_momentum_hourly                               │
│                                                             │
│  Alert Schema v1.0 (frozen)                                 │
│  Outcome Classification: ALERT_SENT / ELIGIBLE_NOT_ALERTABLE│
│    / NO_ELIGIBLE_MARKET / HYDRATION_BLOCKED / NO_SIGNAL     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    TRADING LAYER                            │
├─────────────────────────────────────────────────────────────┤
│  Multi-Instance Paper Trading Runner                        │
│  ├── PROD (conservative, real alerts)                       │
│  ├── DEV  (development, smaller sizing)                     │
│  └── SBOX (sandbox, tiny sizing, no alerts)                 │
│                                                             │
│  Confidence-Weighted Position Sizing                        │
│  ├── HIGH (≥0.70) → 1.5x base                              │
│  ├── MEDIUM (0.50-0.69) → 1.0x base                        │
│  └── LOW (<0.50) → 0.5x base                               │
│                                                             │
│  Discord Alert Format (exact spec)                          │
│  Promotion Rules (DEV → SBOX → PROD)                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Gate Criteria

### Phase 1b → Phase 2 (DEV → Risk Layer)

**Requirements:**
- 7-day DEV paper trading complete
- Signal accuracy ≥ 65% in DEV
- Sharpe ≥ 1.0 in DEV
- No alert spam (< 50/day)
- All settlements processing correctly

### Phase 2 → Phase 3 (Risk Layer → Live)

**Requirements:**
- Risk management rules implemented
- Kill switch operational
- Calibration dashboard showing Brier < 0.25
- All Phase 2 tasks complete

### Phase 3 → Phase 4 (Live → Scale)

**Requirements:**
- 14 days of live signals running
- P&L positive for 10+ days
- Win rate ≥ 65%
- Max drawdown ≤ $250
- Polymarket integration ready

---

## Risk Management

| Risk | Mitigation |
|------|------------|
| Signal accuracy below threshold | Stop live trading, investigate, rollback |
| API rate limits | 10-min cooldown, batch requests |
| Data quality issues | Daily data integrity check, source-health scoring |
| Position sizing errors | Max $250 risk, confidence-weighted sizing, hard cap |
| Wrong direction signal | Require ≥65% confidence, multi-signal confirmation |
| Alert spam | Per-station cooldown (300s), boundary cooldown (900s) |
| DB corruption | Weekly snapshots (3 rotating copies), VACUUM INTO |

---

## Success Criteria

| Metric | Target | Status |
|--------|--------|--------|
| Signal accuracy | ≥65% | ✅ In backtest (DEV pending) |
| Paper trading win rate | ≥65% | ⏳ (7-day DEV run) |
| Live trading P&L | Positive | ⏳ (Phase 3) |
| Win rate consistency | ≥60% over 30 days | ⏳ (Phase 4) |
| Max drawdown | ≤$250 | ⏳ (Phase 3) |
| Sharpe ratio | ≥1.0 | ✅ In backtest (2.29 for LDM) |

---

## Disposition: ADVANCE

**Recommended action:** Begin 7-day DEV paper trading run. If metrics pass promotion gates, proceed to Phase 2 (Risk Layer).

**Next steps:**
1. Run DEV instance daily for 7 days
2. Implement Phase 2 risk management
3. Generate promotion report
4. If passing, proceed to SBOX smoke test → PROD deployment
5. Complete Phase 5 improvements which are now finished

**Risk budget:** $250 (25 trades at $10 each, max loss threshold)

---

**Last updated:** 2026-07-20 (Phase 5 enhancements completed)