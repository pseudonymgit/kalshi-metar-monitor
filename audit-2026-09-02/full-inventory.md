<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Weather Engine — Full Signal & System Inventory (2026-09-02)
## For independent expert audit — Dan Gabriel

---

## PART 1: THE 31 REGISTERED SIGNALS

### Pools and Membership

**gefs pool** (rho_within=0.85)
1. gaussian — P(T > B) via Gaussian Z-score. ACTIVE. Camp: reversion.
2. gaussian_v2 — P(T > B) via refined Gaussian. ACTIVE. Camp: reversion.
3. forecast_disagreement — GFS vs ECMWF ensemble fraction agreement. ACTIVE.

**ecmwf pool** (rho_within=1.0)
4. ecmwf_bias_corrected — ECMWF ensemble fraction, bias-corrected. ACTIVE.

**climatology pool** (rho_within=0.60)
5. calendar_climatology — Historical frequency P(T > B). ACTIVE.
6. seasonal_regime — Seasonal regime classifier. ACTIVE.

**eighty_two_member pool** (rho_within=1.0)
7. eighty_two_member_ensemble — GEFS 31 + ECMWF 51 combined. ACTIVE.

**nwp_direct pool** (rho_within=0.70)
8. nwp_direct — GFS/IFS/ICON/GEM multi-model mean. ACTIVE. Returns None on tied vote.
9. nwp_analog — k-NN historical analog. Status: KILLED (still in registered list, NWP_ANALOG_ENABLED=False).
10. temperature_advection — Warm/cold advection. ACTIVE. evaluate() stub only fixed Sep 1; calls live GFS API.
11. honest_trend — Trend estimator. ACTIVE. evaluate() fixed Sep 1.
12. hrrr_bias_corrected — HRRR 3km convection-resolving. ACTIVE. Live-API-only, no backtesting path.

**metar pool** (rho_within=0.50, freshness=2h)
13. metar_nowcast — Current METAR obs → daily extreme nowcast. ACTIVE. Column fix Sep 1.
14. metar_trend — METAR observed trend. ACTIVE.
15. cloud_cover_index — Cloud cover proxy from METAR. ACTIVE. DB aggregate fix Sep 1.
16. feels_like_delta — Heat index divergence from forecast. ACTIVE. DB aggregate fix Sep 1.

**pressure pool** (rho_within=0.65, freshness=3h)
17. pressure_delta — Pressure delta → temperature trend. ACTIVE. Timescale: intraday.
18. corrected_pressure_delta — Bias-corrected pressure delta. ACTIVE.
19. pressure_tendency — 2-day cumulative ΔP. ACTIVE. Re-enabled 2026-08-26.

**generalist pool** (rho_within=0.40, freshness=3h)
20. asos_microstructure — 1-minute resolution. ACTIVE. Previously returned None (known silent failure); status uncertain.
21. temperature_trend — Temperature trend overlay (f024). ACTIVE. Same status uncertainty.
22. ghcn_calibration — NOAA GHCN-Daily calibration. DEGENERATE (circular: METAR prediction vs METAR settlement). Excluded.
23. rdae_mos — Regime-dependent analog ensemble MOS. ACTIVE. 65.93% settlement. Verified.

**reversion pool** (always-DOWN, override lane)
24. spike_reversion — Price overshoot reversion. ACTIVE. Always DOWN. Invertible lane.
25. radiational_cooling — Clear-night cooling. ACTIVE. Always DOWN. Invertible lane.

**momentum pool** (always-UP, override lane)
26. warm_momentum — Sustained warming trend. ACTIVE. Always UP. Invertible lane.
27. cooling_failure — Failed cooling → heat continues. ACTIVE. Always UP. Invertible lane.

**nwp_upper pool** (rho_within=0.50, freshness=6h)
28. 850mb_temp_advection — Upper-atm advection. ACTIVE. Data coverage starts 2026-07-08.
29. 500mb_height_pattern — Upper-atm height pattern. ACTIVE. Same data limitation.

**Not in any pool (modulators):**
30. cross_model_divergence — Multi-model divergence → confidence modifier. ACTIVE. evaluate() stub fixed Sep 1.
31. frontal_passage_intraday — Intraday front timing. ACTIVE.

### Status Summary (31 registered)
- Verified working with no known issues: ~16-18
- Partially working (data gaps, API-only, stub recently fixed): ~6
- Degenerate (excluded from fusion): 1 (ghcn_calibration)
- Invertible lane (override, not pool signals): 4
- Status uncertain (needs re-verification): 2 (asos_microstructure, temperature_trend)

---

## PART 2: SIGNALS IN SIGNAL_REGISTRY NOT IN THE 31

### ACTIVE but not in 31 registered names
32. fogr_reversion_signal (Family C) — FOGR reversion pattern. Intraday.
33. frontal_detector_signal (Family B) — Frontal passage detection. Daily.
34. frontal_passage_detector (Family B) — Alternative frontal algorithm. Daily.
35. frontal_passage_nowcast_signal (Family B) — Nowcast front position. Daily.
36. settlement_arbitrage_signal (Family C) — Price vs fair value arbitrage. Intraday.
37. simple_trend_signal (Family C) — Price momentum trend. Intraday.
38. spread_based_entry_signal (Family A) — Market spread → settlement convergence. Intraday.
39. volume_momentum_signal (Family C) — Order flow anomaly. Intraday.
40. wind_direction_shift (Family D) — Wind shift detection. Daily.
41. dewpoint_depression_modulator (Family D) — Humidity → cloud cover modifier. Daily.
42. upper_atmosphere_signal (Family B) — Combined 850mb+500mb. Daily.

### GATED
43. ai_composite_signal (Family D) — ML composite, off by default.

### KILLED (still in SIGNAL_REGISTRY)
44. persistence_signal (Family E) — 48.31% accuracy.
45. goldilocks_signal (Family E) — 49.85% negative EV.
46. late_day_momentum (Family E) — 48.31%.
47. nwp_analog_signal (Family E) — 32.63%. (Also listed in Family A/SIGNAL_REGISTRY — duplicate? Both KILLED and ACTIVE.)

### ORPHANED
48. dual_polarity_signal — Never wired.
49. esdr_signal — Never wired.
50. intraday_metar_confirmation — Replaced by METAR nowcast.
51. intraday_metar_confirmation_signal — Same replacement.
52. metar_dtdt_signal — Dead-end pipeline.
53. nwp_dtdt_fusion_signal — Different model architecture.

---

## PART 3: PLACEHOLDER STUBS (signal_modules/)

7 unimplemented placeholder functions, each ~12 lines of trivial code:
- atmospheric_pressure_tendency.py — stub
- diffusion_convergence_signal.py — stub
- humidity_inversion_anomaly.py — stub
- momentum_differential_signal.py — stub
- trend_deviation_signal.py — stub
- visibility_trend_deviation.py — stub
- wind_direction_variance.py — stub

---

## PART 4: LEGACY approach_ FUNCTIONS IN SCRIPTS

13+ standalone `approach_` functions in script files:
- ensemble_v7_sharpe_opt.py: approach_gaussian, approach_persistence, approach_gaussian_v2, approach_pressure_trend
- ensemble_v10_phase2.5.py: approach_gaussian_cloud_adjusted, approach_persistence, approach_gaussian_v2, approach_pressure
- signal_accuracy_dashboard.py: approach_gaussian, approach_gaussian_v2, approach_pressure
- ensemble_v12_comparison.py: approach_gaussian, approach_gaussian_v2, approach_pressure, approach_rdase_mos_simple

These are legacy functions that pre-date the signal module system. Not wired into production.

---

## PART 5: FUSION & DECISION LAYERS

**LIOP (Layer 1) — Default fusion engine**
- Pool-of-pools temperature belief via Beta-binomial
- 10 pools × correlation-corrected n_eff
- LIOP output: (direction, confidence, bayesian_conf, n_signals_agree)
- Verified: 59.34% settlement accuracy (26K trades)

**UWC (Uncertainty-Weighted Cascade) — Optional**
- 4-layer: pool-of-pools → settlement belief → bet sizing → regime
- More sophisticated but lower accuracy when enabled (50.70% vs LIOP 59.34%)

**Camp Dispute Resolution**
- Classifies signals as reversion, momentum, or baseline
- If all reversion signals agree opposite to all momentum signals, confidence is discounted

**Trajectory Lane**
- 615-line module, UNWIRED in daily pipeline. Wired in intraday stack.
- Projects settlement trajectory (not just direction)

**Calibration Pipeline**
- Platt scaling per signal per station
- Holdout-sealed, requires enabling

**Consensus Gate**
- +1.64pp boost, 63.82% when multiple signals agree

**F2 Quality Filter**
- w40_t0.58: 65.32% accuracy, +3.15pp boost

**Spatial Coherence**
- Cross-station consistency check

**Regime Modulation**
- Weather regime → precision modulation

**Freshness Weighting**
- Signal decay based on freshness_half_life

**Cross-Pool Correlation**
- n_eff correction factor for within-pool signals

---

## PART 6: INTRADAY STACK (core/intraday/)

Built 2026-08-31, ~3,800 lines across 6+1 modules:
- pipeline.py — Hourly data pipeline (METAR/ASOS/NWP → hourly buckets)
- calibration.py — Per-(station, hour_before) Platt scaling
- fusion.py — Hourly LIOP fusion + F2 filter + trajectory lane
- recalibration_loop.py — Continuous Kalman-ish belief updates
- entry_engine.py — Entry timing with Kelly + guardrails
- backtest.py — Hourly backtest & validation framework
- signal_evaluator.py — Per-hour signal evaluation

Current status: Gilfoyle fixing Q2/Q3 bugs from Expert Review #1. P0.2+P0.3 signal fixes applied Sep 1. Expert review #2 pending.

---

## PART 7: RISK & EXECUTION LAYERS

- Low liquidity trap filter (Edge 3)
- Position sizer (Kelly × 0.25, cap 8%, fee 0.0205)
- Kill switch enforcement
- Multi-instance runner guard
- PWW crossfeed consumer (wired into paper_trading_engine.py)

---

## PART 8: DATA SOURCES & DATABASES

- weather_engine.db — Core signal storage
- metar_observations — Raw METAR
- nwp_forecasts.db — 197MB, GFS/ECMWF/ICON/GEM
- tigge_archive.db — TIGGE historical ensemble (182K rows, 1,821 dates, 20 stations, zero gaps)
- kalshi_settlements.db — Kalshi settlement data (9,807 records)
- radar_reports — Weather radar
- metar_backfill.db — Historical METAR (backfill)
- weatherapi_archive.db — WeatherAPI historical (backfill)
- alerts.db, trade_journal.db, gefs_archive.db, whalewatch_crossfeed.db

---

## PART 9: KNOWN ISSUES & UNCERTAINTIES

A. Settlement coverage: 17.2% of trades have Kalshi API verification. Hit API wall at 26.75%.
B. nwp_analog: Listed as BOTH ACTIVE (Family A pool) and KILLED (Family E) — contradiction.
C. asos_microstructure & temperature_trend: Previously dead (returned None). P0.3 fixed station key bug, but need re-verification.
D. hrrr_bias_corrected: No evaluate_for_station() — live-API only, zero backtest coverage.
E. temperature_advection: evaluate() fixed Sep 1 but calls live GFS API. No stored historical data.
F. 850mb/500mb: Data starts July 2026. 4+ months before full historical coverage (needs ~60 days current).
G. The 21 signals in SIGNAL_REGISTRY not in 31 registered: some ACTIVE, some with code, level of wiring unknown.
H. nwp_analog signal: also KILLED in SIGNAL_REGISTRY. Registration status contradicting its ACTIVE status.
I. NwpAnalogSignal is a class but evaluate() is a stub returning (None, 0.0) unless NWP_ANALOG_ENABLED=True.
J. signal_modules/ stubs: 7 files in a directory not connected to any pipeline.
K. approach_ functions: 13 legacy functions in scripts not integrated into signal system.