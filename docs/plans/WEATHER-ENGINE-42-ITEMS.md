# Weather Engine — 42-Item Prioritized Roadmap (with Dependencies)
**Updated:** 2026-08-04 23:58 UTC

**Dependency notation:** → means "blocked on" / "prerequisite below"

---

## P0 — CRITICAL FIXES (Fix now, no prerequisites)

- **1.** Unify fee model: `kalshi_fee()`, `market_cost_model.py`, `alert_formatter.py` should all use the same Kalshi formula (`ceil(0.07 × P × (1-P) × 100) / 100` per side)
- **2.** Fix `alert_formatter.py` fee default: line 49 uses 0.1% instead of 2.05¢ (20x understatement)
- **3.** DB connection context manager migration: ~60 unmanaged `sqlite3.connect()` calls → `with` blocks
- **4.** Delete empty DBs: `gefs_operational.db`, `gefs_reforecast.db` — 0 bytes, dead schemas

## P1 — PIPELINE VALIDATION (→ P0)

- **5.** Walk-forward backtest (90+ days): validated Kelly, real Kalshi prices, Monte Carlo null test, slippage sims → P0.1, P0.2
- **6.** Per-station calibration curves: walk-forward, not whole-window, one curve per station → P1.5
- **7.** UHI bias correction: ALL cities, not just 4 — nowcast stations provide regional reference → P1.6, P2.12
- **8.** Kalshi API price verification: 1 live trade to confirm fee formula, price feed, auth → P0.1, P0.2
- **9.** Re-enable paper trading cron: after PEM fix + fee fix + calibration → P1.5, P1.8

## P2 — SIGNAL EXPANSION (→ P1)

- **10.** ECDS backfill (2021-2023): 758 dates ECMWF 50-member via TIGGE, ~26h remaining → P2.13, P2.14
- **11.** ECDS backfill (2026 gap): 114 dates Jan 10→May 3, ~4h → P2.10
- **12.** Nowcast backfill complete: 9 stations → Aug 4, 43 extra stations → 2021→today → P1.7
- **13.** 82-member signal design: first-principles GEFS 31 + ECMWF 50 log-odds Bayesian fusion → P2.10, P1.6
- **14.** ECMWF shadow test: 30 days parallel, wire only if >2pp over GEFS-alone → P2.13
- **15.** Edge 20 multi-model ensemble: GFS/ECMWF/ICON/GEM deterministic, NWP data ready → P1.6

## P3 — NEW LANES (→ P1, P2)

- **16.** Gray Room → Trajectory lane: heavy informant, not gate. Epoch-based analog matching → P1.6
- **17.** Gray Room → Goldilocks lane: fleeting temp-tick microstructure alerts at bucket boundaries → P3.18
- **18.** IEM ASOS 1-min expansion: KNYC has only 16 days of 1-min data — insufficient for goldilocks → P3.17

## P4 — INFRASTRUCTURE (No prerequisites, can happen anytime)

- **19.** Centralized signal registry: 30+ signal modules, manifest with wiring status → P4
- **20.** Experiment archive: 8+ root-level scripts → `scripts/archive/` with date stamps (keep, don't delete) → P4
- **21.** Backup directory cleanup: `core.backup.YYYY_phase3_discovery/` → archive or remove → P4
- **22.** DB consolidation: merge paper_trading*.db → 1 active + 1 backup; merge forecast_disagreement*.db → 1 DB → P4
- **23.** Graphify weekly cron: already set (Monday 4am UTC, uses OpenRouter instead of OpenAI) → P4

## P5 — ADVANCED FEATURES (→ P1, P4)

- **24.** Portfolio correlation matrix: prevents 3-5× over-bet across correlated stations → P1.6
- **25.** Epoch-based Kelly schedule: 0.70/0.85/1.00/0.55 epoch multipliers, +5-15% Sharpe → P5.24
- **26.** Spatial coherence wiring: confidence modulation from geographic agreement → P2.15
- **27.** Regime-based confidence modulation: +1-2pp from regime-aware calibration → P1.6
- **28.** Dewpoint ceiling cap: +1-2pp on humid summer days → P1.6
- **29.** Low liquidity traps wiring: avoid thin markets → before real money
- **30.** Station tradability audit: per-station P&L, accuracy, Sharpe, flag bad stations → P1.5

## P6 — PRODUCTION READINESS (Phase B, independent)

- **31.** Market discovery mismatch investigation: P0 production reliability → P6
- **32.** Audit missing alert emissions: P0 production reliability → P6
- **33.** Verify ingestion parity across all active cities: P0 → P6
- **34.** Detect stalled station ingestion automatically: P0 → P6
- **35.** Confirm integer-cross detection consistency: P0 → P6
- **36.** Validate scheduler execution per station: P0 → P6
- **37.** Regression alert governance (6 items): taxonomy, triggers, replay hook, etc. → P6
- **38.** Alert governance hardening (4 items): schema freeze, event log, versioning, topology → P6
- **39.** Signal engine phases 2-8: proximity regime, price suppression, time weighting, etc. → P6

## P7 — PARK (→ everything else)

- **40.** 30-day paper test: after all fixes deployed → P1-P5
- **41.** Real-money go decision: all 5 gates met → P7.40
- **42.** Fee verification (1 live Kalshi trade): confirm formula with real trade → P7.41