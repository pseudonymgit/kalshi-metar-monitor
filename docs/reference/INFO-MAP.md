# Weather Engine — Info Map

**Generated:** 2026-07-21 22:38 UTC  
**Project:** Kalshi METAR Monitor / Weather Trading Engine  
**Repo:** `prototypes/weather-engine-source`

---

## Section 1: File Structure & Dependencies

### `core/` — Runtime Pipeline

| File | Description | Dependencies | Status |
|---|---|---|---|
| `metar_monitor.py` | Polls METAR providers, normalizes observations, maintains station-local temperature state, detects settlement ladder transitions | `requests`, `sqlite3`, `threading` | ACTIVE |
| `kalshi_monitor.py` | Monitors Kalshi markets for events, fetches prices/volumes, manages market cache | `requests`, `json`, `threading` | ACTIVE |
| `paper_trading_engine.py` | Full paper trading engine: signal pipeline → position sizing → trade journal → P&L tracking | `numpy`, `json`, `sqlite3` | ACTIVE |
| `signal_fusion.py` | Three-layer fusion stack: isotonic calibration, mutual-information decorrelation, log-odds linear opinion pool (LLOP), Dempster-Shafer conflict detection | `numpy`, `scipy`, `core.calibration_pipeline` | ACTIVE |
| `agreement_gate.py` | N-of-M agreement filter requiring minimum consensus among signals before emitting a trade signal | `typing`, `enum` | ACTIVE |
| `nine_signal_ensemble.py` | **(REMOVED)** — Original 9-signal ensemble, replaced by newer fusion architecture | — | DEAD |
| `multi_model_ensemble.py` | Multi-model ensemble voting across signal outputs | `numpy` | ACTIVE |
| `conviction.py` | **(DEPRECATED)** — Placeholder for removed conviction module | — | DEAD |
| `position_sizing.py` | Confidence-weighted Kelly position sizing, fee-aware, 25% balance cap | `math`, `dataclasses`, `datetime` | ACTIVE |
| `kelly_position_sizer.py` | Standalone Kelly criterion position sizing | `math` | ACTIVE |
| `fee_aware_kelly_position_sizing.py` | Fee-aware variant of Kelly position sizing | `math` | ACTIVE |
| `strike_selector.py` | Volume-weighted strike selection from Kalshi order book | `requests`, `json` | ACTIVE |
| `risk_controls.py` | Daily loss check, drawdown tracking, consecutive loss halt, RiskState | `datetime`, `dataclasses` | ACTIVE |
| `risk_budget.py` | Risk budget allocation per trade/signal | `math` | ACTIVE |
| `stop_loss.py` | Stop-loss trigger management | `math` | ACTIVE |
| `trade_journal.py` | Trade journal for logging and replay | `json`, `sqlite3` | ACTIVE |
| `scaling_ladder.py` | Scaling ladder for position entry | `math` | ACTIVE |
| `spread_calibrator.py` | Spread/edge calibration | `numpy`, `scipy` | ACTIVE |
| `alert_builder.py` | Builds alert payloads, cooldown management, lane classification, Discord formatting | `json`, `datetime` | ACTIVE |
| `alert_dispatcher.py` | Dispatches alerts to Discord webhook, dispatch_current_alert entry point | `requests`, `json` | ACTIVE |
| `alert_formatter.py` | Discord message formatting for alerts | `json` | ACTIVE |
| `alert_schema.py` | Alert schema definitions and validation | `json`, `dataclasses` | ACTIVE |
| `alert_state_machine.py` | State machine for alert lifecycle management | `enum`, `datetime` | ACTIVE |
| `alert_throttle.py` | Rate-limiting/throttling for alert dispatch | `time`, `collections` | ACTIVE |
| `alert_retry_queue.py` | Retry queue for failed alert dispatches | `time`, `json`, `sqlite3` | ACTIVE |
| `alert_reconciliation.py` | Alert reconciliation between paper/live states | `datetime` | ACTIVE |
| `alert_integrity_monitor.py` | Monitors alert delivery integrity and completeness | `datetime`, `json` | ACTIVE |
| `delivery_router.py` | Routes alerts to appropriate channels | `json` | ACTIVE |
| `dashboard.py` | Flask + Plotly technical dashboard | `flask`, `plotly`, `pandas` | ACTIVE |
| `confidence_dashboard.py` | Monte Carlo confidence dashboard | `numpy`, `plotly` | ACTIVE |
| `calibration_dashboard.py` | Calibration quality metrics dashboard | `plotly`, `pandas` | ACTIVE |
| `calibration_pipeline.py` | Isotonic regression calibration of signal probabilities | `numpy`, `scipy` | ACTIVE |
| `data_freshness.py` | Data freshness checks and staleness detection | `datetime` | ACTIVE |
| `data_freshness_monitor.py` | Continuous monitoring of data freshness | `datetime`, `threading` | ACTIVE |
| `climatology_pillar.py` | Climatology baseline for signal comparison | `numpy`, `pandas` | ACTIVE |
| `dewpoint_modulator.py` | Dewpoint-based confidence modulation | `math` | ACTIVE |
| `cloud_cover_modulation.py` | Cloud cover confidence adjustment | `math` | ACTIVE |
| `frontal_detector.py` | Frontal boundary detection from METAR data | `math`, `numpy` | ACTIVE |
| `spatial_coherence.py` | Spatial coherence checks across stations | `numpy`, `scipy` | ACTIVE |
| `station_registry.py` | Station metadata registry (lat/lon, ICAO codes) | `json`, `csv` | ACTIVE |
| `station_skill_gate.py` | Per-station skill-based filtering | `json`, `sqlite3` | ACTIVE |
| `station_time.py` | Station-local timezone handling | `pytz` | ACTIVE |
| `ladder_cache_observability.py` | Ladder cache observability endpoints | `json`, `datetime` | ACTIVE |
| `kalshi_calendar.py` | Kalshi trading calendar/settlement schedule | `datetime`, `requests` | ACTIVE |
| `kalshi_price_fetcher.py` | Kalshi price/order-book fetcher | `requests` | ACTIVE |
| `buy_side_optimizer.py` | Buy-side execution optimization | `math` | STABLE |
| `multi_stage_execution.py` | Multi-stage order execution | `time`, `math` | ACTIVE |
| `ensemble_agreement.py` | Ensemble agreement scoring | `numpy` | ACTIVE |
| `ensemble_diversity.py` | Ensemble diversity metrics | `numpy`, `scipy` | ACTIVE |
| `liquidity_weighted_ensemble.py` | Liquidity-weighted ensemble voting | `numpy` | ACTIVE |
| `low_liquidity_traps.py` | Detects low-liquidity trap conditions | `math` | ACTIVE |
| `time_decay.py` | Time decay of signal confidence | `math`, `datetime` | ACTIVE |
| `round_number_anchoring.py` | Detects round-number anchoring bias | `math` | ACTIVE |
| `cross_platform_divergence.py` | Cross-platform price divergence detection | `math` | ACTIVE |
| `nws_revision_model.py` | NWS forecast revision model | `numpy`, `statsmodels` | ACTIVE |
| `near_miss_audit.py` | Near-miss trade audit trail | `json`, `sqlite3` | ACTIVE |
| `night_mode.py` | Night-mode trading halts | `datetime` | ACTIVE |
| `p3_api.py` | Phase 3 API endpoints | `flask` | ACTIVE |
| `p3_main.py` | Phase 3 main pipeline orchestrator | `datetime` | ACTIVE |
| `p3_scheduler.py` | Phase 3 scheduler | `threading`, `datetime` | ACTIVE |
| `p3_calibration_engine.py` | Phase 3 calibration engine | `numpy`, `scipy` | ACTIVE |
| `p3_feature_extractor.py` | Phase 3 feature extraction | `numpy`, `pandas` | ACTIVE |
| `p3_match_engine.py` | Phase 3 market matching engine | `json` | ACTIVE |
| `p3_output_formatter.py` | Phase 3 output formatting | `json` | ACTIVE |
| `p3_backtest_engine.py` | Phase 3 backtest engine (original) | `numpy`, `pandas` | STABLE |
| `p3_backtest_engine_v2.py` | Phase 3 backtest engine v2 (improved) | `numpy`, `pandas` | ACTIVE |
| `p3_trajectory_tracer.py` | Phase 3 trajectory tracing | `numpy`, `json` | ACTIVE |
| `p3_db_migration.py` | Phase 3 database migration utilities | `sqlite3` | STABLE |
| `replay_engine.py` | Replay engine for simulation/testing | `json`, `sqlite3` | ACTIVE |
| `replay_parity_validator.py` | Validates replay parity with live execution | `json`, `numpy` | ACTIVE |
| `scoring_engine.py` | Signal scoring and ranking | `numpy` | ACTIVE |
| `settlement_cascade.py` | Settlement event cascade processing | `datetime` | ACTIVE |
| `settlement_epoch_logger.py` | Settlement epoch logging | `json`, `sqlite3` | ACTIVE |
| `settlement_fidelity.py` | Settlement fidelity checks | `math` | ACTIVE |
| `security_boundaries.py` | Security boundary enforcement | `json` | ACTIVE |
| `visibility_hooks.py` | Observability hook points | `json`, `datetime` | ACTIVE |
| `observability.py` | Observability infrastructure and metrics | `json`, `datetime` | ACTIVE |
| `transition_emitter.py` | Emits transition events for state changes | `json`, `threading` | ACTIVE |
| `authoritative_state.py` | Authoritative state management | `json`, `sqlite3` | ACTIVE |
| `hydrating_health_classifier.py` | **(MISSING)** — Referenced but not found | — | BROKEN |
| `instance_config.py` | Instance configuration loading | `json`, `os` | ACTIVE |
| `instance_config_fixed.py` | Fixed/corrected instance configuration | `json`, `os` | ACTIVE |
| `instance_config_test_write.py` | Test write configuration | `json`, `os` | STABLE |
| `unified_backtest.py` | Unified backtest runner | `numpy`, `pandas` | ACTIVE |
| `sqlite_utils.py` | SQLite utility functions (connection mgmt, migrations) | `sqlite3`, `json` | ACTIVE |
| `heartbeat.py` | Heartbeat monitoring | `datetime`, `json` | ACTIVE |
| `adaptive_thresholds.py` | Adaptive threshold computation | `numpy` | ACTIVE |
| `forecast_disagreement.py` | Forecast disagreement scoring | `numpy` | ACTIVE |
| `late_day_momentum.py` | Late-day momentum computation | `numpy`, `pandas` | ACTIVE |
| `late_day_momentum_hourly.py` | Hourly late-day momentum | `numpy`, `pandas` | ACTIVE |
| `market_phase_classifier.py` | Market phase classification | `numpy`, `sklearn` | ACTIVE |
| `rdae_mos.py` | RDAE MOS (Model Output Statistics) | `numpy`, `sklearn` | ACTIVE |
| `weather_collector_service.py` | Weather data collection service | `requests`, `json` | ACTIVE |
| `cost_utils.py` | Cost calculation utilities | `math` | ACTIVE |
| `fee_aware_filter.py` | Fee-aware trade filter | `math` | ACTIVE |
| `enso_regime_bias.py` | ENSO regime bias correction | `numpy` | ACTIVE |
| `spread_momentum_signal.py` | Spread momentum signal | `numpy` | ACTIVE |

### `core/signals/` — Signal Implementations

| File | Description | Dependencies | Status |
|---|---|---|---|
| `__init__.py` | SignalRegistry class with all registered signals + factory function | All signal modules | ACTIVE |
| `base_signal.py` | BaseSignal abstract class (interface: `evaluate()`, `name`, `min_lookback`) | `abc` | ACTIVE |
| `wind_direction_shift.py` | Wind direction shift signal | `numpy` | ACTIVE |
| `nwp_analog_signal.py` | NWP analog matching signal (49.2% accuracy, kept for reference) | `numpy`, `scipy` | DEAD |
| `goldilocks_signal.py` | Goldilocks temperature signal (0.11% ensemble usage) | `numpy` | ACTIVE (low utility) |
| `persistence_signal.py` | Persistence forecast signal | `numpy` | ACTIVE |
| `gaussian_signal.py` | Gaussian probability signal | `scipy` | ACTIVE |
| `gaussian_v2_signal.py` | Gaussian probability v2 signal | `scipy` | ACTIVE |
| `pressure_delta_signal.py` | Pressure delta change signal | `math` | ACTIVE |
| `forecast_disagreement_signal.py` | Forecast disagreement signal | `math` | ACTIVE |
| `calendar_climatology_signal.py` | Calendar-based climatology signal | `numpy`, `pandas` | ACTIVE |
| `temperature_advection_signal.py` | Temperature advection signal | `math`, `numpy` | ACTIVE |
| `frontal_detector_signal.py` | Frontal detector wrapper signal | `core.frontal_detector` | ACTIVE |
| `intraday_metar_confirmation_signal.py` | Intraday METAR confirmation signal | `core.metar_monitor` | ACTIVE |
| `regime_signal.py` | Regime classifier (mostly returns "unknown") | `numpy`, `sklearn` | ACTIVE (weak) |
| `nwp_direct_signal.py` | Direct NWP forecast signal | `requests`, `json` | ACTIVE |
| `simple_trend_signal.py` | Simple trend signal | `numpy` | ACTIVE |
| `dewpoint_depression_modulator.py` | Dewpoint depression modulation | `math` | ACTIVE |
| `frontal_passage_detector.py` | Frontal passage detection signal | `math` | ACTIVE |
| `intraday_metar_confirmation.py` | Intraday METAR confirmation (variant) | `core.metar_monitor` | STABLE |
| `persistence_signal.py` | (duplicate listing, see above) | — | ACTIVE |

### `scripts/` — Utility & Backtesting Scripts

| File | Description | Status |
|---|---|---|
| `nwp_collect.py` | NWP forecast collection from Open-Meteo API | ACTIVE |
| `ensemble_collect.py` | HGEFS/GFS ensemble collection from ensemble-api.open-meteo.com | ACTIVE |
| `metar_collect_live.py` | Live METAR data collection | ACTIVE |
| `backfill_isd.py` | ISD weather data backfill | ACTIVE |
| `backfill_isd_fast.py` | Fast ISD backfill variant | ACTIVE |
| `backfill_isd_lite.py` | Lightweight ISD backfill | ACTIVE |
| `backfill_daily_stats.py` | Daily statistics backfill | ACTIVE |
| `populate_weather_data.py` | Weather data population script | ACTIVE |
| `kalshi_api_setup.py` | Kalshi API key and account setup | ACTIVE |
| `prod_paper_trading_cron.py` | Production paper trading cron runner | ACTIVE |
| `dev_paper_trading_cron.py` | Development paper trading cron runner | ACTIVE |
| `multi_instance_paper_trader.py` | Multi-instance paper trading runner | ACTIVE |
| `complete_backtest.py` | Full backtest runner | ACTIVE |
| `comprehensive_ensemble_backtest.py` | Comprehensive ensemble backtest | ACTIVE |
| `master_bmode_runner.py` | Master binary-mode runner | ACTIVE |
| `snapshot_db.py` | Database snapshot utility | ACTIVE |
| `signal_accuracy_dashboard.py` | Signal accuracy dashboard generator | ACTIVE |
| `generate_signal_registry_docs.py` | Signal registry documentation generator | ACTIVE |
| `send_test_alert.py` | Sends test alert to Discord | ACTIVE |
| `test_alert.py` | Alert testing script | ACTIVE |
| *(80+ additional scripts — see full listing via `find . -name '*.py' -not -path '*/.*'`)* | | |

### `tests/` — Test Suite

| File | Description | Status |
|---|---|---|
| `conftest.py` | Pytest fixtures and configuration | ACTIVE |
| `test_alert_*.py` | Alert system tests (~12 files) | ACTIVE |
| `test_calibration_pipeline.py` | Calibration pipeline tests | ACTIVE |
| `test_observability_*.py` | Observability tests (~15 files) | ACTIVE |
| `test_security_boundaries.py` | Security boundary tests | ACTIVE |
| `test_scoring_engine.py` | Scoring engine tests | ACTIVE |
| *(20+ additional test files)* | | ACTIVE |

### `tools/` — Tooling

| File | Description | Status |
|---|---|---|
| `tools/collect_evidence.py` | Evidence collection for debugging | ACTIVE |
| `tools/testing/extract_observations.py` | Observation extraction | ACTIVE |
| `tools/testing/replay_observations.py` | Observation replay | ACTIVE |
| `tools/testing/validate_replay.py` | Replay validation | ACTIVE |

### Root-level Files

| File | Description | Status |
|---|---|---|
| `app.py` | Flask HTTP server for runtime control and diagnostics | ACTIVE |
| `debug_test.py` | Debugging test script | ACTIVE |
| `temp_paper_trader.py` | Temporary paper trader (transitional) | ACTIVE |
| `temp_changelog_helper.py` | Changelog helper utility | ACTIVE |
| `test_gfs_graphcast.py` | GFS GraphCast testing | STABLE |
| `test_nwp_direct.py` | NWP direct query testing | STABLE |

### Orphaned `.pyc` Files (no `.py` source found)

*None found in project source directories.*

---

## Section 2: Data Flow

### Data Sources

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────────────┐
│  METAR (NWS)     │     │  NWP (Open-Meteo) │     │  HGEFS Ensemble             │
│  aviationweather  │     │  api.open-meteo   │     │  ensemble-api.open-meteo    │
│  .gov/adds/datas  │     │  .com/v1/forecast  │     │  .com/v1/ensemble           │
│  et/.../metars    │     │                   │     │  (31 members)               │
└────────┬────────┘     └────────┬─────────┘     └─────────────┬───────────────┘
         │                       │                             │
         ▼                       ▼                             ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────────────┐
│ data/metar_     │     │ data/nwp_        │     │ nwp_forecasts.db            │
│ backfill.db     │     │ forecasts.db     │     │ (ensemble member data)      │
│ (observations)  │     │ (forecasts)      │     │                             │
└────────┬────────┘     └──────────────────┘     └─────────────────────────────┘
         │                       │                             │
         ▼                       ▼                             ▼
```

### Signal Pipeline

```
METAR obs ──► Signals (14 registered) ──► Signal Fusion (3-layer LLOP)
                                              │
                                              ▼
                                        Agreement Gate (N-of-M)
                                              │
                                        agreed=N? ──No──► NO TRADE
                                              │
                                              Yes
                                              ▼
                                        Position Sizing (Kelly)
                                              │
                                              ▼
                                        Trade Execution
                                              │
                                              ▼
                                        Trade Journal / P&L
```

### Alert Pipeline

```
Engine State ──► alert_builder.build_paper_trade_alert()
                    │
                    ▼
                format_alert_for_discord()
                    │
                    ▼
                dispatch_current_alert() ──► Discord Webhook
                    │
                    ▼
                Alert Integrity Monitor
                    │
                    ▼
                Alert Retry Queue (on failure)
```

### Collection Pipeline

```
METAR: metar_monitor.py ──► METAR observations ──► metar_backfill.db
NWP:   scripts/nwp_collect.py ──► Open-Meteo ──► nwp_forecasts.db
Ens:   scripts/ensemble_collect.py ──► Ensemble API ──► nwp_forecasts.db (member_index)
```

---

## Section 3: Signal Registry

| Signal Key | Type/Class | Accuracy | Source | Status |
|---|---|---|---|---|
| `wind_direction_shift` | WindDirectionShiftSignal | — | METAR | ACTIVE |
| `nwp_analog` | NwpAnalogSignal | 49.2% | NWP | DEAD |
| `goldilocks` | GoldilocksSignal | 0.11% (ensemble usage) | METAR | ACTIVE (low) |
| `persistence` | PersistenceSignal | — | METAR | ACTIVE |
| `gaussian` | GaussianSignal | — | Climatology | ACTIVE |
| `gaussian_v2` | GaussianV2Signal | — | Climatology | ACTIVE |
| `pressure_delta` | PressureDeltaSignal | — | METAR | ACTIVE |
| `forecast_disagreement` | ForecastDisagreementSignal | — | NWP | ACTIVE |
| `calendar_climatology` | CalendarClimatologySignal | — | Climatology | ACTIVE |
| `temperature_advection` | TemperatureAdvectionSignal | — | METAR | ACTIVE |
| `frontal_detector` | FrontalDetectorSignal | — | METAR | ACTIVE |
| `intraday_metar_confirmation` | IntradayMetarConfirmationSignal | — | METAR | ACTIVE |
| `regime` | RegimeSignal | weak | Mixed | ACTIVE (weak) |
| `nwp_direct` | NwpDirectSignal | — | NWP | ACTIVE |

**Agreement Gate Findings** (from signal audit):
- 3/9 signals agreeing = 63.9% directional accuracy
- 5/9 signals agreeing = 76.7% directional accuracy
- `agree=N` currently tested at `agree=1` (not `agree=3` as recommended)

---

## Section 4: Decision Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-07-06 | SH3: Fee-aware Kelly position sizing | Required for production-grade risk management |
| 2026-07-19 | HGEFS/GFS Ensemble integration | 31-member probability distributions for uncertainty quantification |
| 2026-07-19 | GFS GraphCast deferred | Returns NULL values; requires further investigation |
| 2026-07-20 | Signal freshness experiment KILLED | Architecturally misaligned for Kalshi markets |
| 2026-07-21 | Signal fusion: 3-layer LLOP + Dempster-Shafer | Gray Room Round 3 recommendation |
| 2026-07-21 | Agreement gate: N-of-M consensus filter | Signal audit: 3/9 = 63.9%, 5/9 = 76.7% |
| 2026-07-21 | `nine_signal_ensemble.py` removed | Replaced by newer fusion architecture |
| 2026-07-21 | Phase 10-14 marked complete | Cross-validation, calibration, trajectory tracing done |
| — | AI/ML gate: CLOSED for steps 1-4 | Gray Room threshold: ≥58% directional accuracy, ≥65% confidence, ≥3 analogs, Sharpe ≥1.0 |
| — | Model routing: paper trading/backtesting loops use **scripts only**, no AI calls | TOOLS.md model-spend guardrails |

---

## Section 5: Known Issues

1. **Alert delivery not reaching Dan** — Alerts formatted and dispatched but haven't reached Dan in ~3 days. Debugging delivery path.
2. **Phase 14 test at agree=1, not agree=3** — Agreement gate tested with N=1, but recommended threshold is N=3 (63.9% accuracy).
3. **Dead signals still in registry** — `nwp_analog` (49.2%, DEAD) and `regime` (weak, mostly "unknown") still registered.
4. **GoldilocksSignal** — 0.11% ensemble usage; marked for separation to its own lane.
5. **No real-money Kalshi orders** — All trading is paper-only; real API key exists but unused.
6. **`core.backup.20260617_phase3_discovery/`** — Backup directory with 25+ stale `.py` files; not part of active codebase.
7. **`hydration_health_classifier.py`** — Referenced but may be broken/missing in current `core/`.

---

## Section 6: Dashboard Status

| Dashboard | File | Tech Stack | Status |
|---|---|---|---|
| Technical Dashboard | `core/dashboard.py` | Flask + Plotly | ACTIVE |
| Confidence Dashboard | `core/confidence_dashboard.py` | Monte Carlo + Plotly | ACTIVE |
| Calibration Dashboard | `core/calibration_dashboard.py` | Plotly + Pandas | ACTIVE |
| Signal Accuracy Dashboard | `scripts/signal_accuracy_dashboard.py` | Standalone script | ACTIVE |
| Trading Dashboard (Phase 17) | Not yet built | Planned | PENDING |

**Planned:** Phase 17 will add a dedicated trading dashboard for real-time P&L, open positions, and execution monitoring.
