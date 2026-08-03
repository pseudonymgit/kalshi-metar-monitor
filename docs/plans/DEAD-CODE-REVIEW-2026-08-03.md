# Dead Code Review 2026-08-03 — Second Pass (Nuanced)

**Author:** Gilfoyle (subagent) | **Commitment:** Dan approval required before any deletion
**First pass:** `docs/plans/DEAD-CODE-REVIEW-2026-08-03.md` (workspace root, overwritten here)
**This pass:** Written to `prototypes/weather-engine-source/docs/plans/DEAD-CODE-REVIEW-2026-08-03.md`

---

## Executive Summary

The first pass was too aggressive — it labeled everything "dead" if it wasn't in the GEFS cron's import chain. That's wrong. The reality is more nuanced:

- **4 files** are confirmed dead per prior agreement (Category A)
- **~20 modules** in Category B represent valuable work that was built, mostly tested, but never wired into the current GEFS pipeline. They need evaluation, not deletion.
- **~15 modules** in the GEFS/phase1 transitive chains are actively wired (Category C)
- **~100+ remaining core modules** (Category D) are a mix of research/experiment code, Gray Room spec implementations, Phase 3 risk modules, P3 backtest engine variants, and WhaleWatch infrastructure. Most should be preserved, not deleted.

**Recommendation:** Do not delete anything beyond the 4 already-agreed files. The rest should be preserved, tagged, and potentially revived when the architecture stabilizes.

---

## Active Pipeline Analysis

### Pipeline 1: GEFS Paper Trading Cron (Primary — Active)
**File:** `scripts/gefs_paper_trading_cron.py` (245 lines, standalone)
**Import:** `core.kalshi_monitor._kalshi_get` (only this one function)

The GEFS cron is self-contained. It reads GEFS ensemble data from SQLite, computes ensemble fraction, compares against Kalshi market prices, and logs trades. It does NOT use the old paper_trading_engine, position_sizing, or any signal registry.

### Pipeline 2: Phase 1 Paper Trading Cron (Secondary — Wired but not active on host)
**File:** `scripts/phase1_paper_trading_cron.py`
**Imports:** `core.phase1_config`, `core.position_sizer`, `core.market_cost_model`, `core.sqlite_utils`, `core.signals`

Uses the old SignalRegistry (6 active signals) with agreement gate. Has entry_price hardcoded to 0.85 (known issue). No crontab entry found on host.

### Pipeline 3: Multi-Instance Paper Trader (Wired but not actively called)
**File:** `scripts/multi_instance_paper_trader.py` (+ dev/prod wrappers, shell launcher, 7-day validation)
**Imports:** `core.position_sizing`, `core.late_day_momentum_hourly`, `core.paper_trading_engine`, `core.kalshi_price_fetcher`, `core.instance_config`, `core.alert_builder`

This is the old v2.0 pipeline. Calls through `paper_trading_engine.PaperTrader`. Has shell launcher, dev/prod cron wrappers, and a 7-day validation script. These are infrastructure scripts, not dead code. No crontab entry found on host.

---

## Dependency Verification

### GEFS cron transitive chain (verified by AST analysis):
```
gefs_paper_trading_cron.py
  └─ core.kalshi_monitor._kalshi_get
       ├─ core.alert_schema
       ├─ core.authoritative_state
       │    └─ core.security_boundaries
       ├─ core.metar_monitor
       │    ├─ core.alert_retry_queue
       │    ├─ core.alert_schema
       │    ├─ core.authoritative_state
       │    ├─ core.kalshi_monitor
       │    ├─ core.near_miss_audit
       │    ├─ core.replay_engine
       │    │    └─ core.security_boundaries
       │    ├─ core.security_boundaries
       │    ├─ core.station_registry
       │    ├─ core.station_time
       │    └─ core.transition_emitter
       │         ├─ core.security_boundaries
       │         └─ core.settlement_epoch_logger
       │              └─ core.station_time
       ├─ core.station_registry
       └─ core.station_time
```

### Phase 1 cron transitive chain (verified):
```
phase1_paper_trading_cron.py
  ├─ core.phase1_config (no deps)
  ├─ core.position_sizer
  │    └─ core.market_cost_model (no deps)
  ├─ core.market_cost_model (no deps)
  ├─ core.sqlite_utils
  │    └─ core.db_connection
  └─ core.signals
       └─ 6 registered signals (gaussian, gaussian_v2, pressure_delta,
          forecast_disagreement, calendar_climatology, frontal_passage_intraday)
```

### Multi-Instance transitive chain (wired but not active):
```
multi_instance_paper_trader.py
  ├─ core.position_sizing (no deps)
  ├─ core.late_day_momentum_hourly (no deps)
  ├─ core.paper_trading_engine
  │    ├─ core.agreement_gate
  │    ├─ core.adaptive_thresholds
  │    ├─ core.calibration_pipeline
  │    ├─ core.ensemble_diversity
  │    ├─ core.execution_simulator
  │    ├─ core.kelly_position_sizer
  │    ├─ core.low_liquidity_traps
  │    ├─ core.multi_model_ensemble
  │    ├─ core.pnl_tracking
  │    ├─ core.risk_budget
  │    ├─ core.risk_controls
  │    ├─ core.scaling_ladder
  │    ├─ core.signals (cross-import)
  │    ├─ core.spatial_coherence
  │    ├─ core.station_skill_gate
  │    ├─ core.stop_loss
  │    ├─ core.trade_journal
  │    └─ core.alert_dispatcher
  ├─ core.kalshi_price_fetcher (no deps)
  ├─ core.instance_config (no deps)
  └─ core.alert_builder (no deps)
```

---

## Categorization

### Category A: ✅ Confirmed Dead — Already Agreed to Kill

These were identified in prior meetings and should be removed. No debate needed.

| File | Lines | Reason | Status |
|------|-------|--------|--------|
| `core/kelly_position_sizer.py` | 287 | Old Kelly sizing, replaced by `position_sizer.py` | ✅ Agreed |
| `core/fee_aware_kelly_position_sizing.py` | 311 | Fee-aware Kelly, removed in Phase 1 Fix 3 | ✅ Agreed |
| `core/variance_weighted_sizing.py` | 233 | Variance-weighted sizing, replaced | ✅ Agreed |
| `core/dashboard.py` | 760 | Old Flask app, new `trading_dashboard/` package exists | ✅ Agreed |

**Total: 4 files, ~1,591 lines**

---

### Category B: 🔴 Needs Preservation Assessment

These are pieces of work built across 14 development phases. Most are not wired into the current GEFS pipeline but represent significant engineering effort. For each, I've assessed what it does, its current state, and whether it should be kept.

#### B1: `core/paper_trading_engine.py` (3,397 lines)
**What it is:** The old v2.0 paper trading engine. Full PaperTrader class with trade execution, daily reconciliation, P&L tracking, DB management, Discord alert delivery, risk controls integration, agreement gate, and signal evaluation. Built over 14 phases.
**Status:** Wired by multi_instance_paper_trader.py (which has shell launcher, dev/prod cron wrappers, 7-day validation script). Not wired into GEFS cron.
**Assessment:** **KEEP.** This is the most substantial single module. It's the old pipeline that the GEFS cron replaced, but it contains reusable infrastructure (DB schema, alert builder integration, position sizing, reconciliation) that could be extracted. The GEFS cron is a rewrite, not a replacement — the engine is still useful as a reference implementation and for running the Phase 1 pipeline.
**Architecture note:** The GEFS cron replaced the entire engine with a simpler, self-contained script. But the engine's PaperTrader class is still importable and used by the multi-instance infrastructure.

#### B2: `core/radiational_cooling.py` (452 lines)
**What it is:** A LOW-only physical signal. Detects radiational cooling nights from METAR observations (cloud cover, wind, dryness, snow) and produces a bias-corrected LOW estimate that undercuts the ensemble consensus. Uses per-station base potential tables.
**Status:** Never wired into any pipeline. Built per FP 6.6 spec.
**Assessment:** **KEEP (low priority).** Physically sound. Could add edge for LOW markets under clear, calm winter conditions. Needs a metar_monitor-like observation interface to get cloud/wind data. If the GEFS pipeline stays HIGH-only, this is irrelevant. Revisit if LOW markets are added.

#### B3: `core/spatial_coherence.py` (539 lines)
**What it is:** Regional consensus gate. 6 climate regions (NE, SE, SC, MW, RW, PAC) with inverse-distance-weighted consensus. If a station disagrees with its regional neighbors, confidence is modulated. Continuous modulation (0.5x–1.3x), not binary.
**Status:** Built per FP 6.3 spec. Described as "Highest-ROI structural fix" in the roadmap (Phase 4.1). Wired by paper_trading_engine.py but not by GEFS cron.
**Assessment:** **KEEP.** This was the #1 Phase 4 priority. The GEFS cron doesn't use it because it's a standalone GEFS-only pipeline. If the GEFS pipeline ever adds METAR data, spatial coherence should be re-evaluated. The physical basis is sound — stations in the same region should agree.

#### B4: `core/adaptive_thresholds.py` (641 lines)
**What it is:** Per-signal per-station Bayesian Beta-Bernoulli posterior + dual EMA threshold controller. Uses 70% one-sided lower credible bound as base threshold, with fast/slow EMA for seasonal baseline, momentum dampening (max 5pp/day change).
**Status:** Built per FP 6.4 spec. Wired by paper_trading_engine.py but not by GEFS cron.
**Assessment:** **KEEP.** This is a sophisticated self-tuning system. The GEFS cron uses hardcoded thresholds (edge_threshold=0.02, price bounds). If the GEFS pipeline wants adaptive thresholds, this is ready. The Bayesian approach is mathematically sound.

#### B5: `core/risk_controls.py` (559 lines)
**What it is:** Real risk management with RiskConfig, RiskState, RiskMetrics, TradeResult. Implements: check_daily_loss (5% of account), check_drawdown (15% max), check_consecutive_losses (3 max). Phase 3.6 implementation.
**Status:** Phase 5.6 updated defaults. Wired by paper_trading_engine.py and intraday_trading_loop.py. Not wired into GEFS cron.
**Assessment:** **KEEP (high priority for wiring).** The GEFS cron has basic risk controls (per-day capital constraint, 25% per trade) but no real risk management. This module should be wired into the GEFS cron. The 15% drawdown limit and 3 consecutive loss limit are sensible defaults.

#### B6: `core/intraday_trading_loop.py` (777 lines)
**What it is:** Intraday trading mode with sequential trigger architecture (A→B→C→D). Stage A: METAR-based signals (FOGR, dT/dt, Pressure Tendency). Stage B: HRRR bias-corrected. Stage C: NWP+METAR fusion. Stage D: Entry/Exit rules with time decay, dual exit, spread avoidance.
**Status:** Built as Phase 18. Wired for standalone use via `python3 -m core.intraday_trading_loop`. Not wired into any cron pipeline.
**Assessment:** **KEEP.** The sequential trigger architecture is different from the GEFS ensemble approach. This is a separate trading mode for intraday refinement. Requires HRRR and NWP data infrastructure that may not be reliable yet. Revisit after the core pipeline stabilizes.

#### B7: `core/agreement_gate.py` (189 lines)
**What it is:** N-of-M agreement filter. 3-of-9 default. Filters signals that don't have sufficient consensus.
**Status:** Wired by paper_trading_engine.py. The phase1 cron has its own inline agreement gate (apply_agreement_gate function). The GEFS cron doesn't use it (it's a single signal, not an ensemble).
**Assessment:** **KEEP.** Small utility. If the GEFS pipeline ever adds multiple signals, this is the right gate.

#### B8: `core/ensemble_fraction.py` (286 lines)
**What it is:** GEFS ensemble fraction bias correction: load_bias_corrections, apply_bias_correction, compute_ensemble_fraction. Uses season-aware bias tables.
**Status:** Not wired into GEFS cron (the cron implements its own simple ensemble fraction inline).
**Assessment:** **KEEP.** The GEFS cron's inline computation is simpler. This module has bias correction tables that the cron doesn't use. Could be imported to improve GEFS accuracy.

#### B9: `core/ensemble_agreement.py` (410 lines)
**What it is:** 3-of-4 NWP ensemble agreement gate using real GFS, ECMWF, ICON, GEM forecasts. Reads from data/nwp_forecasts.db.
**Status:** Not wired into any pipeline. Requires NWP data that may not be reliable.
**Assessment:** **KEEP.** This is a different approach from the signal ensemble — it's an NWP-model agreement gate. The GEFS cron uses GEFS ensemble members instead. If NWP data becomes reliable, this could be a co-signal.

#### B10: `core/ensemble_diversity.py` (111 lines)
**What it is:** Small diversity score computation. Penalizes unanimous votes (redundancy). Adjusts confidence by (0.75 + 0.25 × diversity).
**Status:** Wired by paper_trading_engine.py.
**Assessment:** **KEEP.** Small, well-designed module. If the GEFS pipeline ever uses multiple signals, diversity scoring is useful.

#### B11: `core/calibration_pipeline.py` (447 lines)
**What it is:** Walk-forward isotonic regression calibration. Hierarchical fallback: per-signal-per-city → per-signal-global → global → identity. Minimum 100 co-firing trades. Uses sklearn's IsotonicRegression.
**Status:** Wired by phase6/phase8/phase9 search scripts. Not wired into GEFS cron or phase1 cron.
**Assessment:** **KEEP.** This is calibration infrastructure used by the combinatorial search scripts. The GEFS cron doesn't need calibration (it uses raw confidence). If the pipeline ever needs calibrated probabilities, this is the module.

#### B12: `core/rolling_calibration.py` (605 lines)
**What it is:** Rolling 30-day recalibration for spike reversion confidence. Time-decay weighting, per-station per-market-type calibration. Exponential moving average of accuracy.
**Status:** Not wired into any pipeline.
**Assessment:** **KEEP (low priority).** This is a simpler alternative to calibration_pipeline.py. The spike reversion signal was removed from the registry (49.85% accuracy). Could be useful if spike reversion is revived.

#### B13: `core/station_skill_gate.py` (315 lines)
**What it is:** Per-station Brier Skill Score (BSS) gate. Assesses whether a station's signal is skillful enough to trade. Uses BSS cache with 24-hour TTL.
**Status:** Wired by paper_trading_engine.py. Not wired into GEFS cron.
**Assessment:** **KEEP.** The GEFS cron trades all 20 stations equally. This gate could prevent trading on stations where the GEFS ensemble has poor skill.

#### B14: `core/low_liquidity_traps.py` (288 lines)
**What it is:** Low-liquidity market filter. Flags markets where volume < $15K per snapshot AND spread > 1.5¢ as traps. Prevents entering illiquid markets that could suffer adverse selection.
**Status:** Wired by paper_trading_engine.py (with HAS_LOW_LIQUIDITY_TRAP guard). Not wired into GEFS cron.
**Assessment:** **KEEP.** The GEFS cron doesn't check liquidity. If the GEFS pipeline goes live, this is essential.

#### B15: `core/risk_budget.py` (307 lines)
**What it is:** 3D allocation matrix (City × Market type × Signal type). Total budget $250, max 25 concurrent positions, 20% per city, 30% per market type, 40% per signal type, 0.6 pairwise correlation cap.
**Status:** Wired by paper_trading_engine.py (with HAS_RISK_BUDGET guard). Not wired into GEFS cron.
**Assessment:** **KEEP.** The GEFS cron has no risk budget allocation. If running multiple concurrent positions, this is needed.

#### B16: `core/scaling_ladder.py` (307 lines)
**What it is:** Tiered position scaling (TIER1 $10 → TIER4 $100). Promotion criteria: consecutive wins + Sharpe/Sortino/Calmar thresholds. Reverse scaling: 2 consecutive losses → down 1 tier, drawdown >10% → down 1 tier, VIX >25 → -1 tier.
**Status:** Wired by paper_trading_engine.py (with HAS_SCALING_LADDER guard). Not wired into GEFS cron.
**Assessment:** **KEEP.** The GEFS cron uses fixed Kelly fraction (0.5). This could add performance-based scaling.

#### B17: `core/stop_loss.py` (404 lines)
**What it is:** Multi-condition stop-loss: 20 consecutive losses OR 30 calendar days → stop. Drawdown >10% → pause. Rolling 20-trade win rate <52% → pause. 7-day cooling-off. 10 paper trades before resuming.
**Status:** Wired by paper_trading_engine.py (with HAS_STOP_LOSS guard). Not wired into GEFS cron.
**Assessment:** **KEEP.** The GEFS cron has no stop-loss mechanism. If running unattended, this is essential.

#### B18: `core/alert_dispatcher.py` (160 lines)
**What it is:** Bridges alert_builder.py to Discord webhook delivery. Handles HTTP POST with retry support. Uses PAPER_TRADING_INSTANCE env var for webhook URL selection.
**Status:** Wired by paper_trading_engine.py. Not wired into GEFS cron (the cron prints to stdout and writes to DB).
**Assessment:** **KEEP.** If the GEFS pipeline ever needs Discord alerts, this is the delivery module.

#### B19: All 33 `core/signals/*` modules (~5,000 lines total)
**What they are:** 33 signal modules, of which 6 are registered in the active SignalRegistry. The remaining 27 are: built-but-unregistered signal implementations, Gray Room spec implementations, intraday signals, and deprecated signals.
**Status:** The registry only uses 6 (gaussian, gaussian_v2, pressure_delta, forecast_disagreement, calendar_climatology, frontal_passage_intraday). The remaining 27 are not imported by any active pipeline.
**Assessment:** **KEEP.** These are the result of 14 phases of signal development. Unregistered signals include:
- **ADVANCE signals** (wired into integration layer, not registry): hrrr_bias_corrected, metar_nowcast, spread_based_entry
- **Need data infrastructure:** temperature_advection, intraday_metar_confirmation (need ERA5/HRRR backfill)
- **Deprecated (MCC negative or below coin flip):** wind_direction_shift (49.72%), corrected_pressure_delta (MCC=-0.2167)
- **Intraday signals:** fogr_reversion, nwp_dtdt_fusion, metar_dtdt, pressure_tendency, esdr, nwp_direct
- **Research signals:** ai_composite_signal (ML pipeline not deployed), settlement_arbitrage_signal, volume_momentum_signal, regime_signal, simple_trend_signal, persistence_signal, frontal_detector_signal
- **Nowcast signals:** frontal_passage_nowcast_signal, frontal_passage_detector, goldilocks_signal, spike_reversion_signal, dewpoint_depression_modulator

**Recommendation:** Keep all signal files. The registry cleanly separates active from inactive. When new data sources become available (ERA5, HRRR, NWP), these signals can be re-evaluated.

#### B20: `core/execution_simulator.py` (466 lines)
**What it is:** Monte Carlo fill simulation. Models slippage, fill probability, and P&L ranges for trade execution.
**Status:** Wired by paper_trading_engine.py (with HAS_EXECUTION_SIMULATOR guard).
**Assessment:** **KEEP.** If the pipeline ever goes live, this is useful for pre-trade analysis.

#### B21: `core/pnl_tracking.py` (889 lines)
**What it is:** Extracted P&L reconciliation, calibration, and reporting. Used by paper_trading_engine.py.
**Status:** Wired by paper_trading_engine.py.
**Assessment:** **KEEP.** The GEFS cron has its own inline P&L tracking. If the GEFS pipeline grows, this module could replace the inline code.

#### B22: `core/trade_journal.py` (788 lines)
**What it is:** Trade journal SQLite backed. Tracks timestamp, station, market, direction, signal IDs, confidence, edge, outcome.
**Status:** Wired by paper_trading_engine.py.
**Assessment:** **KEEP.** The GEFS cron writes to its own DB schema. This module provides a richer journal schema with failure mode classification.

#### B23: `core/multi_model_ensemble.py` (608 lines)
**What it is:** Multi-model ensemble signal combining GFS, ECMWF, ICON, GEM forecasts. Different from signal ensemble.
**Status:** Wired by paper_trading_engine.py (with HAS_MULTI_MODEL_ENSEMBLE guard).
**Assessment:** **KEEP.** The GEFS cron uses GEFS ensemble only. This module uses NWP models. If NWP data becomes reliable, this could be a co-signal.

---

### Category C: 🟢 Currently Wired (Keep)

Everything in the active import chains from GEFS cron and Phase 1 cron:

#### GEFS cron import chain (must keep):
| File | Lines | Role |
|------|-------|------|
| `core/kalshi_monitor.py` | 3,065 | Kalshi API client — `_kalshi_get` used by GEFS cron |
| `core/alert_schema.py` | 97 | Alert schema definitions |
| `core/authoritative_state.py` | 155 | State management with security boundaries |
| `core/security_boundaries.py` | 151 | Security enforcement |
| `core/metar_monitor.py` | 5,066 | METAR data utilities — heavy module |
| `core/station_registry.py` | 341 | 20-city station metadata |
| `core/station_time.py` | 184 | Timezone handling |
| `core/alert_retry_queue.py` | 601 | Alert retry with cooldown |
| `core/near_miss_audit.py` | 567 | Near-miss audit logging |
| `core/replay_engine.py` | 27 | Replay engine (small) |
| `core/transition_emitter.py` | 81 | State transition emission |
| `core/settlement_epoch_logger.py` | 325 | Settlement epoch timestamping |

#### Phase 1 cron import chain (keep — Phase 1 is the secondary pipeline):
| File | Lines | Role |
|------|-------|------|
| `core/phase1_config.py` | 130 | Config constants |
| `core/position_sizer.py` | 640 | Position sizing with Kelly |
| `core/market_cost_model.py` | 430 | Fee model |
| `core/sqlite_utils.py` | 108 | SQLite connection helpers |
| `core/db_connection.py` | 595 | Database connection management |
| `core/signals/__init__.py` | 110 | Signal registry with 6 active signals |

#### Multi-Instance infrastructure (keep — still wired, scripts exist):
| File | Lines | Role |
|------|-------|------|
| `core/position_sizing.py` | 564 | Confidence-weighted position sizing |
| `core/late_day_momentum_hourly.py` | 300 | Hourly late-day momentum signal |
| `core/kalshi_price_fetcher.py` | 360 | Kalshi market URL building |
| `core/instance_config.py` | 332 | Instance configuration (PROD/DEV/SBOX) |
| `core/alert_builder.py` | 720 | Alert formatting for Discord |

---

### Category D: 🟡 Other Modules (Not in Any Active Chain)

All remaining modules in `core/` that are not in Categories A, B, or C. These are predominantly:
- **Phase 3 risk modules** (now Category B, listed above)
- **P3 backtest engine** variants (p3_backtest_engine.py, p3_backtest_engine_v2.py, p3_calibration_engine.py, p3_db_migration.py, p3_feature_extractor.py, p3_main.py, p3_match_engine.py, p3_output_formatter.py, p3_scheduler.py, p3_trajectory_tracer.py, p3_api.py) — ~4,000+ lines of P3-era backtesting infrastructure
- **Alert infrastructure** (alert_formatter.py, alert_integrity_monitor.py, alert_reconciliation.py, alert_state_machine.py, alert_throttle.py) — ~2,500 lines of alert lifecycle management
- **Data pipeline** (data_collector.py, data_processor.py, data_freshness.py, data_freshness_monitor.py, db_health_monitor.py, db_schema.py) — ~4,000+ lines
- **Observability** (observability.py, health_monitor.py, heartbeat.py, heartbeat_monitor.py, ladder_cache_observability.py) — ~2,000+ lines
- **Market infrastructure** (market_monitor.py, market_phase_classifier.py, order_book_baseline.py, order_book_collector.py, order_manager.py) — ~3,000+ lines
- **Trade execution** (trade_execution.py, multi_stage_execution.py, settlement_cascade.py, settlement_execution_gate.py, settlement_fidelity.py, settlement_processor.py, strike_selector.py, contract_selection_optimizer.py) — ~3,000+ lines
- **WhaleWatch** (whale_detector.py, whale_watch_db.py, whale_signal_feeder.py, whale_goldilocks_fusion.py, polymarket_whale_db.py, polymarket_kalshi_feeder.py) — ~2,000+ lines
- **Signal processing** (signal_fusion.py, signal_pipeline.py, fusion_logic.py, forecast_confidence_modulator.py, forecast_disagreement.py, frontal_detector.py, goldilocks_predictive.py) — ~4,000+ lines
- **Legacy/P3** (rdae_mos.py, enso_regime_bias.py, climatology_pillar.py, cross_platform_divergence.py, dual_hypothesis_engine.py, liquidity_weighted_ensemble.py, nine_signal_ensemble.py, nws_revision_model.py) — ~3,000+ lines
- **Utilities** (cost_utils.py, lock_file.py, disk_monitor.py, compatibility_checks.py, structured_logger.py, visibility_hooks.py, weather_collector_service.py, station_effects.py, station_rank_selective.py, replay_parity_validator.py, scoring_engine.py, seasonal_diurnal_curve.py, time_decay.py, trading_modes.py, unified_backtest.py, conviction.py, operation_state.py, production_gate.py, round_number_anchoring.py, spread_calibrator.py, spread_momentum_signal.py, fee_aware_filter.py, metar_qc_parser.py, dewpoint_modulator.py, cloud_cover_modulation.py, calibrate_all_signals.py, calibration_dashboard.py, confidence_dashboard.py, buy_side_optimizer.py, api_circuit_breaker.py, delivery_router.py, lane_manager.py, late_day_momentum.py, paper_test_controller.py, paper_trader.py, live_trader.py, live_trading_loop.py, price_fetcher.py, hydration_health_classifier.py, kalshi_calendar.py, decision_output.py) — ~20,000+ lines

**Assessment:** These are all research, experimental, or legacy modules. Most are not wired into any active pipeline. Some represent significant engineering effort (P3 backtest engine, data processor, signal fusion, market monitor). **Do not delete any of these without a specific architectural decision.** They represent the history of the project and may contain useful code for future phases.

---

## Summary Statistics

| Category | Count | Lines | Action |
|----------|-------|-------|--------|
| A: Confirmed dead | 4 | ~1,591 | Delete (already agreed) |
| B: Needs preservation | ~23 | ~15,000 | Keep, evaluate for wiring |
| C: Currently wired | ~20 | ~13,000 | Keep (required) |
| D: Other modules | ~100+ | ~30,000+ | Keep (do not delete) |
| **Total** | **~147** | **~60,000** | **Delete only 4 files** |

---

## Recommendations

### R1: Delete only the 4 Category A files
- `core/kelly_position_sizer.py`
- `core/fee_aware_kelly_position_sizing.py`
- `core/variance_weighted_sizing.py`
- `core/dashboard.py`

These have been agreed upon. No debate needed.

### R2: Preserve all Category B modules
Do not delete any of them. They represent significant engineering effort across 14 phases. The most valuable ones for wiring into the GEFS pipeline are:

**Priority 1 — Wire into GEFS cron:**
- `core/risk_controls.py` — The GEFS cron has no real risk management. This is essential for unattended operation.
- `core/stop_loss.py` — Complementary to risk controls. Prevents catastrophic loss.
- `core/risk_budget.py` — Needed if the GEFS pipeline ever runs concurrent positions.

**Priority 2 — Re-evaluate for GEFS enhancement:**
- `core/ensemble_fraction.py` — Bias correction tables that the GEFS cron doesn't use.
- `core/station_skill_gate.py` — Could gate trading on low-skill stations.
- `core/spatial_coherence.py` — Regional consensus, highest-ROI structural fix from Gray Room.

**Priority 3 — Keep for reference:**
- `core/paper_trading_engine.py` — Reference implementation, used by multi-instance infrastructure.
- `core/calibration_pipeline.py` — Used by combinatorial search scripts.
- All signal modules — May be re-evaluated when new data sources become available.

### R3: Do not delete Category D modules
Category D contains ~100+ modules representing ~30,000+ lines of research, experimental, and legacy code. These include:
- P3 backtest engine variants (used by Phase 6/8/9 search scripts)
- Alert infrastructure (alert_state_machine, alert_throttle, alert_reconciliation)
- Data pipeline (data_collector, data_processor, data_freshness_monitor)
- WhaleWatch infrastructure (separate project)
- Observability, health monitoring, heartbeat

Many of these modules are imported by Phase 6/8/9 search scripts (in `scripts/`). Deleting them would break those scripts. Keep all of them.

### R4: Audit import chains regularly
- The first pass was wrong because it didn't trace transitive imports from `paper_trading_engine.py` (which is wired by multi-instance infrastructure)
- The first pass also missed that Phase 6/8/9 search scripts import many modules directly
- Future audits should trace from ALL scripts, not just active cron pipelines

### R5: Tag modules, don't delete them
- For modules that are truly dead but not yet agreed upon, add a `# DEAD: <reason>` header comment
- This preserves the code while making it clear it's not used
- Future architects can find and revive dead code without searching through git history

---

## Appendix: Module-by-Module Import Status

### GEFS cron (active) — imports only:
`core.kalshi_monitor._kalshi_get`

### Phase 1 cron (secondary) — imports:
`core.phase1_config`, `core.position_sizer`, `core.market_cost_model`, `core.sqlite_utils`, `core.signals`

### Multi-instance (infrastructure) — imports:
`core.position_sizing`, `core.late_day_momentum_hourly`, `core.paper_trading_engine`,
`core.kalshi_price_fetcher`, `core.instance_config`, `core.alert_builder`

### Paper_trading_engine transitively imports:
`core.agreement_gate`, `core.adaptive_thresholds`, `core.calibration_pipeline`,
`core.ensemble_diversity`, `core.execution_simulator`, `core.kelly_position_sizer`,
`core.low_liquidity_traps`, `core.multi_model_ensemble`, `core.pnl_tracking`,
`core.risk_budget`, `core.risk_controls`, `core.scaling_ladder`, `core.signals`,
`core.spatial_coherence`, `core.station_skill_gate`, `core.stop_loss`,
`core.trade_journal`, `core.alert_dispatcher`

### Phase 6/8/9 search scripts import:
`core.calibration_pipeline`, `core.dewpoint_modulator`, `core.signals`,
`core.unified_backtest`, `core.spatial_coherence`

---

*End of Dead Code Review 2026-08-03 (Second Pass)*