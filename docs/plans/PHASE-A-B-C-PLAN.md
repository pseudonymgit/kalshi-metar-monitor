# Phase A/B/C Plan — Weather Engine

**Date:** 2026-07-22
**Status:** Planning — expert specs in progress

---

## Overview

All outstanding items organized into 3 sequential phases. Each phase has an expert specification to define scope, approach, and success criteria.

---

## Phase A: Fix the Foundation (P&L Correctness)

**Goal:** Make the P&L numbers real. Current backtest P&L is wrong because settlement price, spread, fill price, and cost model are all incorrect.

**Items:**
1. Fix settlement price calculation — currently compares temp > 50°F instead of strike price. P&L is completely wrong.
2. Fix spread assumption — using 0.5¢ instead of actual 3.1¢ mean spread
3. Centralize cost model — single source of truth for fees, spreads, slippage
4. Wire risk_controls.py into engine — kill switches, risk budgets, position limits
5. Fix conflicting position sizing systems — 3 systems exist, 1 should win
6. Fix Kelly formula — using wrong variance formula
7. Fix NwpDirectSignal evaluate() — currently a no-op (returns None)
8. Fix 4 skipped tests — function name mismatches in test_edge_cases.py
9. Fix dual_polarity_signal.py import bug — relative import beyond top-level

**Expert:** Quant Finance / Market Micro specialist

---

## Phase B: Calibration & Signal Validation

**Goal:** Validate all 22 signals against real settlement data. Re-run combinatorial search with Phase 23 signals. Calibrate individually.

**Items:**
1. Fix 2 bare excepts (already in flight via P3)
2. Fix 7 pre-existing kalshi test failures (already in flight via P3)
3. Re-run combinatorial search with Phase 23 signals (seasonal_regime, corrected_pressure_delta, station_effects)
4. Individual signal calibration for all 22 signals
5. Validate real backtest runner against real settlement data
6. Remove fake P&L from backtest engine
7. Fix market_prob hardcoded to 0.5 in dashboard
8. Stop random data in calibration dashboard

**Expert:** Meteo Stats / Quant specialist

---

## Phase C: Production Pipeline

**Goal:** Ready for actual trading. Fix operational issues, consolidate dashboards, build live trading pipeline.

**Items:**
1. Fix conflicting Flask apps (Phase 17 already replaced old ones)
2. Fix conflicting DB strategies (3 competing DB connection patterns)
3. Add structured logging to all modules
4. Fix alert pipeline env var mismatch
5. Live trading pipeline (execution, order management, portfolio tracking)
6. Kalshi API integration for actual execution
7. Real P&L tracking from settlement data
8. Cron job cleanup (clawhub fixed, backup jobs pending Sunday run)

**Expert:** Technical / Operations specialist

---

## Dependencies

- Phase A must complete before Phase B (wrong P&L invalidates calibration)
- Phase B must complete before Phase C (unvalidated signals shouldn't trade)
- P3 (kalshi tests + bare excepts) is already in flight and feeds into Phase B