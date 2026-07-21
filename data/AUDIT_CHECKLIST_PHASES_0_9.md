# Audit Checklist: Phases 0-9

Cross-reference every roadmap deliverable against git log + filesystem. For each item, determine: DONE, PARTIAL, or MISSING.

---

## Phase 1: Foundation
- [ ] 1.1: Baseline signals (reversion, regime, DTR trend, pressure_regime_interaction)
- [ ] 1.1: Dead signals removed (from registry + engine)
- [ ] 1.1: Risk guardrails (Marty's spec)
- [ ] 1.2: Render METAR auto-refresh on startup + periodic
- [ ] 1.2: Station registry dedup (KJFK/KNYC, KORD/KMDW)

## Phase 2: Signal Expansion
- [ ] 2.1: Temperature advection signal wired into engine
- [ ] 2.2: Goldilocks NameError fix
- [ ] 2.2: Wind Direction Shift look-ahead fix
- [ ] 2.2: Signal hygiene pass
- [ ] 2.3: NWP Analog Signal v1.0 (initial k-NN)

## Phase 3: Risk & Position Sizing
- [ ] 3.1: Kelly sizing formula `f*=(p-c)/(1-c)` with spread-based cost
- [ ] 3.1: 1/4 Kelly conservative scaling
- [ ] 3.2: Risk budget allocator (per-trade cap, portfolio allocation)
- [ ] 3.3: Scaling ladder (multi-tier, confidence-based)
- [ ] 3.4: Stop-loss monitor (kill switch, daily loss, drawdown, consecutive)
- [ ] 3.5: Dynamic station discovery (20-city coverage)
- [ ] 3.5: Dynamic market types

## Phase 4: Dashboard & Enhancements
- [ ] 4.1: Spatial coherence gate (6 regions, consensus modulation)
- [ ] 4.2: Dashboard MVP (Flask, health, predictions, Plotly)
- [ ] 4.3: Dewpoint depression modulator (humidity confidence boost/penalty)
- [ ] 4.4: Adaptive confidence thresholds (rolling 30d adjustment)
- [ ] 4.5: Ensemble diversity score (penalize redundant signals)
- [ ] 4.6: Frontal passage detector (4-condition event detection)
- [ ] 4.7: Intraday METAR confirmation signal
- [ ] 4.8: Confidence tracker (Monte Carlo, rolling Sharpe/win rate, p-value, drawdown)

## Phase 5: Alert Infrastructure
- [ ] 5.1: Alert dispatcher (Discord webhook)
- [ ] 5.2: Kalshi API integration (396 markets, 3.1¢ mean spread)
- [ ] 5.2: Market phase classification
- [ ] 5.2: Spread calibration

## Phase 6: Combinatorial Search Iterations
- [ ] 6.1: Initial 7-signal search (127 combos, 20 stations)
- [ ] 6.2: Calibrated search (Phase 6)
- [ ] 6.3: Parameter sweep (optimized windows, thresholds)
- [ ] 6.4: Calibration v3 validation (5 combos)

## Phase 7: Production Readiness
- [ ] 7.1: Agreement gate (configurable env var)
- [ ] 7.2: SBOX/PROD deployment configs
- [ ] 7.3: 30-day test plan (initial draft)

## Phase 8: Bug Fixes (Gray Room Round 5)
- [ ] 8.1: Goldilocks look-ahead fix
- [ ] 8.2: Kelly formula fix
- [ ] 8.3: 3 conflicting sizing systems consolidated
- [ ] 8.4: Confidence squaring removed
- [ ] 8.5: Calibration data leakage fixed
- [ ] 8.6: XGBoost removed from NWP path
- [ ] 8.7: Fee model fixed (zero commissions)
- [ ] 8.8: Lane thresholds aligned
- [ ] 8.9: SQLite concurrency fixed (WAL, busy_timeout)

## Phase 9: NWP Backfill & 11-Signal Search
- [ ] 9.1: NWP data backfill (18+ months, 5 models, 20 stations, 21 variables)
- [ ] 9.1: ERA5 backfill fixes
- [ ] 9.2: NWP Analog Signal v2.0 (deterministic k-NN)
- [ ] 9.3: Full 11-signal combinatorial search
- [ ] 9.4: Purged CV (Phase 9 version)

## Gray Room Rounds (Cross-Cutting)
- [ ] GR1: Initial advisory exists
- [ ] GR2: Meteorological experts exist
- [ ] GR3: 6-expert panel synthesis exists
- [ ] GR4: Regime + market micro analysis exists
- [ ] GR5: 9 bugs fixed, 19 ideas documented, roadmap v4.0
- [ ] GR6: 6 experts, synthesis exists

## Additional Checks
- [ ] Late-day momentum signal (should be KILLED/DISABLED)
- [ ] Goldilocks removed from signal list in engine config
- [ ] Regime signal registered in __init__.py
- [ ] Phase 10.3 (calibration-integrated search) — not done, mark as PENDING
- [ ] Phase 10.4 (purged CV) — not done, mark as PENDING
- [ ] Phase 10.5 (NWP standalone analysis) — not done, mark as PENDING
- [ ] Phase 10.6 (temp advection / intraday METAR investigation) — not done, mark as PENDING