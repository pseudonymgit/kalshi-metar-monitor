# Weather Engine Roadmap

**Date:** 2026-07-05  
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

### Data Sources

| Source | Path | Date Range |
|--------|------|------------|
| metar_backfill.db | data/metar_backfill.db | 2021-01-01 → 2026-07-04 |
| NWP data | data/nwp_forecasts.db | 2026-06-04 → 2026-07-04 |
| alerts-prod.db | backup-2026-06-17/alerts-prod.db | 2026-03-01 → 2026-06-16 |
| paper_trading_dev.db | data/paper_trading_dev.db | Active (DEV instance) |
| DB snapshots | data/snapshots/ | Weekly rotation (3 copies) |

---

## What's Next

### Phase 1b: Signal Validation (7-day DEV run)

**Goal:** Run DEV paper trading for 7 days, validate accuracy and P&L attribution

**Tasks:**
- [ ] Run `multi_instance_paper_trader.py --instances DEV` daily
- [ ] Track per-signal accuracy (late_day_momentum, reversion, climatology)
- [ ] Validate Sharpe ratio ≥ 1.0
- [ ] Generate promotion report after 7 days
- [ ] If passing, begin SBOX smoke test

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

### Phase 3: Live Signals

**Goal:** Deploy live signals with controlled risk exposure

**Tasks:**
- [ ] Promote DEV → PROD per PROMOTION-RULES.md
- [ ] Kalshi API integration for live market data
- [ ] Position sizing calculator (max $250 risk budget)
- [ ] Stop-loss logic (2x target loss)
- [ ] Signal filtering (require ≥65% confidence)
- [ ] Daily review process

**Estimated time:** 7-10 days (after Phase 2 complete)

### Phase 4: Scale & Optimize

**Goal:** Expand to Polymarket, add secondary signals, optimize parameters

**Tasks:**
- [ ] Polymarket integration (global cities)
- [ ] Secondary signals (reversion magnitude, duration)
- [ ] Parameter optimization (analog count, confidence thresholds)
- [ ] Automated retraining (weekly model refresh)

**Estimated time:** 10-14 days (after Phase 3 stable)

---

## Phase 5: AI/ML (Parked)

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

**Risk budget:** $250 (25 trades at $10 each, max loss threshold)

---

**Last updated:** 2026-07-05 (P0 batch complete, all 7 items delivered)
