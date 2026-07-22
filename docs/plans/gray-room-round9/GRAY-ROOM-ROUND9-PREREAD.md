# Gray Room Round 9 — Codebase & Architecture Vetting

**Date:** 2026-07-22
**Pre-read for:** All experts
**Context:** Weather engine has completed Phases 1-15. We need an independent expert panel to vet the codebase, architecture, and trading pipeline.

---

## System Overview

A deterministic weather trading engine that predicts daily HIGH/LOW temperature settlement for 20 US cities. It runs paper trades on Kalshi temperature markets with ~72% accuracy using 9 METAR-based signals.

### Key Metrics
- **Best ensemble:** `pressure_delta + forecast_disagreement + calendar_climatology` at agree=3
- **Accuracy:** 72.30% (2,657 trades, 20 stations)
- **At agree=2:** 66.20% (9,977 trades)
- **NWP signal:** NwpDirectSignal (92.7% GFS direction) — registered but not wired into main pipeline
- **Phase 14 test:** 69.67% (300 trades, 16 days, agree=1)

### Architecture
- **118 Python files** in `core/`, ~52,000 lines total
- **4 largest files:** metar_monitor.py (5,007), paper_trading_engine.py (3,104), kalshi_monitor.py (3,062), signal_fusion.py (1,268)
- **Pipeline:** METAR data ingestion → signal evaluation → agreement gate → position sizing → trade execution → P&L tracking → alert dispatch
- **Database:** SQLite (metar_backfill.db, paper_trading.db, trade_journal.db, nwp_forecasts.db, hgefs_ensemble.db)
- **Alert pipeline:** alert_builder.py → alert_dispatcher.py → Discord webhook
- **Existing dashboards:** dashboard.py (Flask/Plotly, health), confidence_dashboard.py (in-memory analytics), calibration_dashboard.py (calibration metrics)

### Active Signals (9 registered)
1. calendar_climatology — 68.9% standalone
2. forecast_disagreement — 62.9%
3. frontal_detector — active
4. gaussian — 66.9%
5. gaussian_v2 — 64.0%
6. persistence — active
7. pressure_delta — 58.9%
8. regime — active (weak)
9. wind_direction_shift — active
10. nwp_direct — 92.7% GFS (registered but evaluate() returns (None, 0.0))
11. goldilocks — intraday arb confidence modulator (0.11% standalone)
12. intraday_metar_confirmation — active
13. temperature_advection — active

### Data Sources
- **METAR:** NOAA NWS API, 5-15 min polling, 20 stations, ~13 months backfill
- **NWP:** GFS, ECMWF, ICON, GEM, ERA5 — 217K+ rows, daily collection at 06:00 UTC
- **HGEFS:** 31 ensemble members, daily max/min, 420+ records
- **HRRR:** Not collected (scoped only)

### Kalshi Integration
- 396 active markets, 3.1¢ mean spread
- API: `https://api.elections.kalshi.com/trade-api/v2`
- Paper trading only (no live execution)

### Code Quality Snapshot
- 1 pre-existing syntax error (alert_state_machine.py:L115)
- 87+ division by zero risks
- 49+ naive datetime.now() calls
- 18 bare except clauses
- 4 restored orphan files (STALE/DEAD)
- Timezone hygiene: mixed UTC and naive local times

### Recent Work
- Phase 10: Full combinatorial search (1,479 combos, 9 signals)
- Phase 11: NWP direct signal + fusion (NWP doesn't improve ensemble)
- Phase 12: Regime/Markov (SKIPPED — insufficient signal quality)
- Phase 13: Probabilistic trajectory (built from GFS forecast values)
- Phase 14: 30-day deployment test (simulation-based, not real backtest)
- Phase 15: Code review (119 files, 54 with bugs, 4 restored orphans)
- Phase 15 fixes applied: agreement threshold default to 3, signal registry updated (nwp_analog removed, nwp_direct registered), fee rates to 0.0

### Files
- Roadmap: `docs/plans/WEATHER-ENGINE-MASTER-ROADMAP.md`
- Phase 15 review: `docs/plans/PHASE15-CODE-REVIEW-2026-07-21.md`
- Gray Room R7 (short-duration signals): `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS-2026-07-21.md`