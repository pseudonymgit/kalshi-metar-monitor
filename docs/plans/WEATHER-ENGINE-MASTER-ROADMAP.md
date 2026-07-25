# Weather Engine Master Roadmap

## Overview
Master roadmap for the weather engine development and deployment. Tracks all major phases and milestones.
Updated 2026-07-25 — all 14 phases complete. Current status: **Complete — ready for 30-day paper test**.
The 30-day paper test will execute via B-mode under Gilfoyle supervision. No further phase gates remain until paper test completes.

## Phase 8: Revalidation and Release (COMPLETED)
Date: 2026-07-21

### 8.1 - Re-run Combinatorial Search
Status: COMPLETE
Output: `data/phase8_combinatorial_search.json`

### 8.2 - Calibration-Inside-Search 
Status: COMPLETE
Output: `data/phase8_calibrated_search.json`

### 8.3 - Two-Stage Parameter Sweep
Status: COMPLETE
Output: `data/phase8_parameter_sweep.json`

### 8.4 - Purged Walk-Forward CV
Status: COMPLETE
Output: `data/phase8_purged_cv_results.json`

### 8.5 - Infrastructure Hardening (COMPLETED)
Gray Room Round 7 redefined this as explicit infrastructure hardening. The original "30-Day Unattended Test Setup" doc exists but is superseded by the 7-phase roadmap.

**Completed tasks:**
- ✅ fee_rate: 0.0 → 0.0205 (paper_trading_engine.py:283)
- ✅ SQLite WAL mode: applied to 50+ production files
- ✅ Halt file mechanism: scripts/halt_check.py, wired into prod_paper_trading_cron.py
- ✅ Graceful degradation: core/operation_state.py (5 states)
- ✅ Market discovery cleanup: dead code removed from market_monitor.py
- ✅ P0 diagnostics: stalled ingestion, ingestion parity, alert emissions, integer-cross, scheduler
- ✅ Disk space monitoring (B12) — 80/90/95% thresholds
- ✅ Pre-trade integrity check (B13)
- ✅ Wire StopLossMonitor into main loop (B14)
- ✅ Unify risk config: RiskConfig vs StopLossMonitor (B15)
- ✅ Cron overlap protection (B21)
- ✅ Abort condition runbooks (B26)
- ✅ DST/timezone drift audit (B23)

**Gate:** Phase 8 complete → Phase 9

---

## Phase 9: Signal Cleanup & Diagnostics (COMPLETED)
**Date:** 2026-07-23

### Tasks
- ✅ **B5:** Run label-permutation test — passed
- ✅ **A2:** Kill daily-level GoldilocksSignal from signal list
- ✅ **C4:** KILL dead signals from active roster (persistence, metar_dtdt, pressure_tendency)
- ✅ **C7:** Fix seasonal_regime interface mismatch
- ✅ **C9:** Implement fee-adjusted accuracy metric
- ✅ **B2:** Build unified monitoring dashboard (`scripts/daily_ops_check.py`)
- ✅ **B6:** Codify Luck Elimination Protocol (13-point pass criteria)
- ✅ **B7:** Define formal success criteria

**Gate:** Phase 9 complete → Phase 10

---

## Phase 10: Goldilocks V2 & Frontal Detector (COMPLETED)
**Date:** 2026-07-24
**9 tasks — all done**

### Tasks
- ✅ **A1:** Fix Goldilocks confidence inversion — `is_daily_high` → `is_transient_spike`; signal renamed to `SpikeReversionSignal`
- ✅ **A2-lane:** LOW market detection — symmetric downward dip detection wired
- ✅ **A3:** Split into two distinct signals: `MicrostructureSpikeDetector` (real-time), kill old `MeanReversionSignal`
- ✅ **A4:** Dual-hypothesis engine — `core/dual_hypothesis_engine.py`, H1 (overreaction) vs H2 (underreaction), shadow mode
- ✅ **A5:** METAR QC flag parsing — `core/metar_qc_parser.py`, `goldilocks_qc_filter`
- ✅ **A6:** Settlement-aware execution gate — `core/settlement_execution_gate.py`, scenario classification
- ✅ **A8:** Station-rank-selective activation — `core/station_rank_selective.py`, major hubs only
- ✅ **A9:** Rolling 30-day calibration window — `core/rolling_calibration.py`
- ✅ **C10:** Frontal passage detector — `FrontalPassageIntradaySignal`, 3-condition detection

**Gate:** Phase 10 complete → Phase 11

---

## Phase 11: Fusion & Calibration Overhaul (COMPLETED)
**Date:** 2026-07-24
**3 tasks — all done**

### Tasks
- ✅ **C8:** Variance-weighted ensemble position sizing — wired into `paper_trading_engine.py`
- ✅ **R6:** Bayesian log-odds fusion — `core/signal_fusion.py`, MI-decorrelation + log-odds
- ✅ **C3:** Three-lane architecture — `core/lane_manager.py`, formal adoption

**Gate:** Phase 11 complete → Phase 12

---

## Phase 12: Spatial Coherence Gate (COMPLETED)
**Date:** 2026-07-24
**2 tasks — all done**

### Tasks
- ✅ **D4:** Spatial correlation gate / cluster independence tracking — `core/spatial_coherence.py`, cross-station correlation matrix
- ✅ **D2:** Process-aware trade coordination — prevents self-trading across lanes

**Gate:** Phase 12 complete → Phase 13

---

## Phase 13: 30-Day Formal Paper Test (COMPLETED — SETUP PHASE)
**Date:** 2026-07-24 — setup complete, test infrastructure ready
**4 tasks — all done**

### Tasks
- ✅ **B1:** Three-phase rollout (Smoke → Stabilization → Autonomous) — controller wired in `core/paper_test_controller.py`
- ✅ **B10:** Goldilocks shadow mode — active from Phase B onward
- ✅ **B11:** Graduate test rollout ($100 → $500 → $2,500 scale) — graduated allocation configured
- ✅ **D3:** Settlement-confirmed accuracy logging — `core/pnl_tracking.py`

### Notes
- The full 30-day paper test will run under B-mode with Gilfoyle supervision
- **A-Mode dispatcher is disabled (buried)** — all execution runs through B-mode only
- **Phase dispatcher cron is disabled** — phase advancement is manual

**Gate:** Luck Elimination Protocol verification → Phase 14

---

## Phase 14: Production Prep (COMPLETED)
**Date:** 2026-07-25
**5 tasks — all done**

### Tasks
- ✅ **A10:** Goldilocks real-money allocation gate — `core/production_gate.py`, gates: ≥60% accuracy, 30 days shadow, ≥0.30 Sharpe
- ✅ **D6:** Fix loss distribution / structural Sharpe problem — `core/risk_controls.py`, skew-aware position limits
- ✅ **D7:** Disaster recovery runbook — `docs/runbooks/DISASTER_RECOVERY.md`
- ✅ **D10:** Capacity planning / stress testing — `docs/CAPACITY_PLANNING.md`
- ✅ **D8:** SQLite migration to PostgreSQL — assessed, deferred (not proven necessary for current scale)

---

## Reference: Gray Room Round 7 Sources
- Full synthesis: `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS.md`
- Expert 1 (Market Microstructure/METAR): GRAY-ROOM-ROUND7-EXPERT1-MARKET-MICROSTRUCTURE.md
- Expert 2 (Trading Ops/Automation): GRAY-ROOM-ROUND7-EXPERT2-TRADING-OPS.md
- Expert 3 (Signal Architecture/Roadmap): GRAY-ROOM-ROUND7-EXPERT3-SIGNAL-ARCHITECTURE.md
- Expert 4 (Risk/Systems): GRAY-ROOM-ROUND7-EXPERT4-RISK-SYSTEMS.md
- Continuity state: `.meta/continuity/weather-engine/a-mode-state.json`
- A-Mode runbook: `docs/plans/A-MODE-RUNBOOK.md`