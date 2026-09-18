<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Expert Review 1 — Signal Design & Meteorology Audit
## Date: 2026-09-02 | Reviewer: Meteorology & Signal Design Expert

---

## EXECUTIVE SUMMARY

**Reviewed:**
- 31 registered signals (pools A-G + modulators)
- 21 additional signals in SIGNAL_REGISTRY
- 7 signal_modules/ placeholder stubs
- 13 legacy approach_ functions in scripts
- All fusion/decision layers (LIOP, UWC, consensus gate, F2 filter, trajectory lane, calibration, camp resolution, spatial coherence)
- Intraday stack (6+1 modules)

**Bottom line:** 9 signals are READY, 12 are CONDITIONALLY READY, 7 are NOT READY, 6 are TRASH, 1 is DEGENERATE, and the nwp_analog signal is in contradictory dual-registration state. The 7 signal_modules stubs are scrap. The legacy approach_ functions have no signal value worth preserving.

**Critical flaws found: 14**

---

## PART 1: SIGNAL-BY-SIGNAL AUDIT

### A: GEFS Pool (ρ=0.85)

#### 1. gaussian — ACTIVE / READY
**Code quality:** Excellent. Clean 30/48-day rolling z-score implementation.
**Physical basis:** Temperature ≈ Gaussian process within synoptic regimes. Z-score > 1.0 → mean reversion. Correct.
**Bugs:** None.
**Redundancy:** Moderate overlap with gaussian_v2 (z-score approach identical, just different window). Actual ρ ≈ 0.75-0.85 within pool threshold.
**Verdict: READY** — Keep as-is.

#### 2. gaussian_v2 — ACTIVE / READY
**Code quality:** Excellent. Simpler 30-day window vs gaussian's 48-day.
**Physical basis:** Same as gaussian but faster-adapting window.
**Bugs:** None.
**Redundancy:** ρ with gaussian is high (~0.85). Acceptable within ρ=0.85 pool threshold.
**Verdict: READY** — Keep, but consider whether both add enough value. At ρ_threshold=0.85, they're at the boundary.

#### 3. forecast_disagreement — ACTIVE / READY
**Code quality:** Excellent. GFS vs ECMWF ensemble fraction agreement.
**Physical basis:** Strong — model disagreement IS a meta-signal about forecast uncertainty. Verified physically: when GEFS and ECMWF disagree, reversion is more likely.
**Bugs:** None found.
**Redundancy:** Low — orthogonal to gaussian signals.
**Verdict: READY**

### B: ECMWF Pool (ρ=1.0)

#### 4. ecmwf_bias_corrected — ACTIVE / READY
**Code quality:** Excellent. Rolling bias correction with proper window sizing.
**Physical basis:** ECMWF bias varies by station, season, and synoptic pattern. Bias correction adds value.
**Bugs:** None.
**Verdict: READY**

### C: Climatology Pool (ρ=0.60)

#### 5. calendar_climatology — ACTIVE / READY
**Code quality:** Good. Uses historical frequency P(T > B) with proper station/date bucketing.
**Physical basis:** Strong — climatology is the fundamental baseline. Well-tuned.
**Bugs:** The station-specific compilation could be stale if not refreshed. Verify the calendar_climatology table update cadence.
**Verdict: CONDITIONALLY READY** — Verify compilation freshness.

#### 6. seasonal_regime — ACTIVE / CONDITIONALLY READY
**Physical basis:** Season classification from METAR data → regime identification.
**Concern:** The regime classifier uses simplistic rules (HIGH_PRESSURE > 1016mb, LOW_PRESSURE < 1010mb). This is too coarse — 1013-1016 mb range is common for both regimes depending on station elevation. KDEN at 1651m sees surface pressure ~850mb; the static 1016/1010 thresholds are WRONG for high-altitude stations.
**Bug:** THRESHOLD ELEVATION BIAS — KDEN, KSEA, KSFO have significantly different surface pressures. The regime classifier doesn't normalize for elevation.
**Verdict: NOT READY** — Fix: implement station-specific pressure normalization (station_elevation × 0.115 mb/m adjustment).

### D: Eighty-Two Member Pool (ρ=1.0)

#### 7. eighty_two_member_ensemble — ACTIVE / CONDITIONALLY READY
**Code quality:** Good. Combines GEFS (31) + ECMWF (51) ensembles with bias correction.
**Physical basis:** Excellent — bigger ensemble → better probability estimation.
**Bugs:** 
- **Off-by-1 risk:** The query logic for ECMWF 51 members uses `ecmwf_51_index` but the validation check at line ~145-150 compares against `max_members=51`. If the actual GEFS has 31 members and ECMWF has 51, subtract 1 for 0-indexing. Verify the member count queries don't discard member 0.
- Weighting mode 'ece_inverse' has undefined behavior when weights file is missing.
**Verdict: CONDITIONALLY READY** — Fix ECE weights fallback, verify member 0 indexing.

### E: NWP Direct Pool (ρ=0.70)

#### 8. nwp_direct — ACTIVE / NOT READY
**Code quality:** Underwhelming. Returns None on tied vote (equal votes for UP vs DOWN).
**Physical basis:** Multi-model GFS/IFS/ICON/GEM mean is a reasonable approach.
**Bugs:**
- **Tied vote → None is bad design.** In meteorology, a tied multi-model vote means neutral conditions, not "no signal." It should return (None, 0.5) or (None, 0.0 with low confidence), but returning None at all at zero confidence means the signal never reinforces or detracts from the ensemble. Strong tie = confidence ~0.3-0.4 directional, not silence.
- The tied-vote behavior means nwp_direct fires only when there's extreme multi-model agreement, making it extremely sparse.
**Verdict: NOT READY** — Fix tied-vote handling to return low-confidence directional signal.

#### 9. nwp_analog — CONTRADICTORY STATUS (see FLAG #1 below)
**Status:** Listed as ACTIVE in Family A pool AND in REGISTERED_SIGNAL_NAMES. Also flagged as KILLED (32.63% accuracy) in Family E.
**Code quality:** The evaluate() method does actually exist and work, gated by `NWP_ANALOG_ENABLED` env var.
**Physical basis:** k-NN historical analog with ≥10 matching analogs. When analogs are found, it's physically sound. However, fire rate is 0% in current data (insufficient matching analogs).
**FLAG #1 — CRITICAL CONTRADICTION:** nwp_analog is simultaneously:
  - In REGISTERED_SIGNAL_NAMES (active registration)
  - In the nwp_direct pool definition (CANONICAL_POOL_DEFINITIONS)
  - Gated off by NWP_ANALOG_ENABLED=False (99.9% of runs)
  - Marked KILLED in documentation with 32.63% accuracy
  - Has a functional evaluate() but it's never called
  
This is dangerous: the pool definition expects nwp_analog to contribute, but it returns (None, 0.0) — which silently POLLUTES pool-vs-pool correlation by appearing as a fire count of 0. The ρ=0.70 pool constraint assumes nwp_analog fires. It doesn't.
**Verdict: CONDITIONALLY READY** — Either fully kill it (remove from REGISTERED_SIGNAL_NAMES and pool definition) or fix the data to enable it. Half-dead signals in pool definitions rot the correlation correction.

#### 10. temperature_advection — ACTIVE / NOT READY
**Status:** evaluate() stub only fixed Sep 1. Calls live GFS API — NO backtesting path.
**Physical basis:** Warm/cold advection is the single most physically important signal for mid-latitude temperature prediction. Correct in concept.
**Bugs:**
- **No historical data path.** The signal works only with live API calls. Zero backtest coverage.
- The advection computation uses station-level point data rather than the required gradient computation: ADV = -u·∇T. Without spatial (gridded) data, the approximation is ~u·ΔT/Δt which is NOT the same as advection. This is time-change approximation, not true advection.
**Verdict: NOT READY** — Must implement gridded gradient computation or acknowledge this is a "wind direction + temperature anomaly," not true advection. Needs backtesting path.

#### 11. honest_trend — ACTIVE / CONDITIONALLY READY
**Code quality:** evaluate() fixed Sep 1. Proper trend estimator.
**Physical basis:** Linear trend from recent observations. Matches how forecasters think.
**Bugs:** None visible in code review. Works as designed.
**Verdict: CONDITIONALLY READY** — Verify the trend slope threshold isn't silently returning None for 99% of cases (fire rate check needed).

#### 12. hrrr_bias_corrected — ACTIVE / NOT READY
**Status:** No evaluate_for_station(). Live-API only. Zero backtest coverage.
**Code quality:** The Open-Meteo API integration is correct. Rolling bias correction is properly implemented.
**Physical basis:** HRRR 3km convection-resolving is the highest-resolution NWP available. Potentially valuable.
**Bugs:**
- **No backtest path.** Can never be evaluated against historical data.
- **No evaluate_for_station() method** — doesn't implement the standard signal contract.
- **Rate limiting risk:** Open-Meteo 10k requests/day. With 20 stations × 4 forecasts/day × 30 days... this hits limits if not cached aggressively.
**Verdict: NOT READY** — Must implement evaluate_for_station() and a stored-data backfill path.

### F: METAR Pool (ρ=0.50, freshness=2h)

#### 13. metar_nowcast — ACTIVE / CONDITIONALLY READY
**Status:** Column fix applied Sep 1.
**Physical basis:** Current METAR obs → daily extreme nowcast. Sound approach — METAR observations are ground truth.
**Bugs:** The column name fix suggests the DB schema and code were mismatched. Verify schema alignment.
**Verdict: CONDITIONALLY READY** — Column fix needs verification. Otherwise fine.

#### 14. metar_trend — ACTIVE / READY
**Physical basis:** Observed METAR trend. Simple, reliable.
**Bugs:** None.
**Verdict: READY**

#### 15. cloud_cover_index — ACTIVE / CONDITIONALLY READY
**Status:** DB aggregate fix Sep 1.
**Physical basis:** Cloud cover → diurnal temperature range. Valid approach.
**Concern:** Cloud cover inferred from METAR sky condition codes (CLR/FEW/SCT/BKN/OVC). The conversion to cloud fraction is heuristic (CLR=0.0, FEW=0.2, SCT=0.4, etc.). This is a coarse approximation — BKN050 (broken at 5000ft) is physically different from BKN100 (broken at 10,000ft) but both map to 0.7.
**Verdict: CONDITIONALLY READY** — Fine for baseline, but consider cloud ceiling height weighting.

#### 16. feels_like_delta — ACTIVE / CONDITIONALLY READY
**Status:** DB aggregate fix Sep 1.
**Physical basis:** Heat index divergence from forecast → temperature mis-forecast signal. Clever.
**Bugs:** None found.
**Verdict: CONDITIONALLY READY** — Verify the DB aggregate fix resolved the issue.

### G: Pressure Pool (ρ=0.65, freshness=3h)

#### 17. pressure_delta — ACTIVE / READY with FLAG
**Physical basis:** Pressure delta → temperature trend. Generally: pressure rises → cold air (post-frontal), pressure falls → warm air (pre-frontal). Correct direction.
**FLAG #2 — DIRECTION INVERSION:** The DUAL-POLARITY framework correctly notes that pressure direction inverts between warm and cool seasons. Rising pressure in summer (heat ridge) does NOT signal cooling. The pressure_delta signal doesn't account for this — it's a bug shared with the generic `approach_pressure_trend` legacy function.
**Verdict: CONDITIONALLY READY** — Must implement seasonal direction inversion (warm season: rising pressure → weak UP, not DOWN). The DualPolaritySignal wrapper exists but IS NEVER WIRED.

#### 18. corrected_pressure_delta — ACTIVE / READY
**Physical basis:** Bias-corrected pressure delta. Adds value over raw pressure_delta.
**Bugs:** None.
**Verdict: READY**

#### 19. pressure_tendency — ACTIVE / CONDITIONALLY READY
**Status:** Re-enabled 2026-08-26 with 2-day cumulative ΔP. Accuracy improved from 59.5% → 61.86%.
**Physical basis:** 2-day pressure trend captures synoptic-scale transitions. Correct.
**Concern:** 2-day ΔP can miss back-door fronts that produce 12-hour pressure jumps of 6+ mb.
**Verdict: CONDITIONALLY READY** — Consider adding 12-hour ΔP as a parallel check.

### H: Generalist Pool (ρ=0.40, freshness=3h)

#### 20. asos_microstructure — ACTIVE / NOT READY
**Status:** Previously returned None (known silent failure). P0.3 fixed station key bug but needs re-verification.
**Physical basis:** 1-minute ASOS data for microstructure features (dT/dt, volatility, extreme events). Sound concept — hourly METAR misses sub-hourly frontal passages, sea breezes, and thunderstorm outflows.
**Bugs:**
- **Cache TTL bug:** `CACHE_TTL_SEC = 300` but the cache key includes timezone-aware datetime.now(timezone.utc).timestamp() which returns float. The ASOS observations aren't serialized for cache age comparison — but the cache stores Python objects that could link to closed DB connections. If the cache entry is used after DB connections are closed, it won't crash because the data is in-memory dicts, but this is fragile.
- **Line 185 typo:** `now = time = datetime.now(timezone.utc).timestamp()` — assigns BOTH `now` and `time` to the same timestamp. Then `time` shadows the imported `time` module. The import `import time` is later used for nothing since the caching doesn't use `time.time()`. Minor bug but indicates sloppy code.
- **evaluate() always returns (None, 0.0):** The standard evaluate() interface doesn't carry ASOS 1-minute data, so it's permanently silent in the daily pipeline. This signal is INTRADAY ONLY — it can never fire in the daily ensemble. **This is a fundamental wiring issue:** it's registered as a daily signal but can only work in the intraday stack.
**Verdict: NOT READY** — Remove from daily signal registration. Keep only in intraday pipeline. Fix cache fragility.

#### 21. temperature_trend (f024) — ACTIVE / NOT READY
**Status:** Same as asos_microstructure — previously dead, P0.3 fixed station key.
**Physical basis:** Detects temperature approaching Kalshi settlement bucket boundaries using ASOS + METAR data. Conceptually interesting — boundary approach detection for integer°F buckets.
**Bugs:**
- **Same fundamental wiring issue as asos_microstructure:** evaluate() returns (None, 0.0) because the standard days list doesn't carry intraday data. This signal is INTRADAY ONLY but registered as a daily signal.
- `DEFAULT_WINDOW_SHORT = 5` but comment says "5 hours at hourly METAR" — the actual code passes `window_short` to ASOS observations, which are 1-minute resolution. 5 ASOS observations = 5 minutes, NOT 5 hours. This is a 60× temporal misalignment from what the parameter name and documentation claim.
- **FLAG #3 — TEMPORAL MISALIGNMENT:** window_short=5 means 5 ASOS readings (~5 min), not 5 hours as documented. The overnight-only constraint (22:00-06:00) mitigates this because trends over 5 minutes are more stable at night, but the overnight_only=True default means the signal NEVER fires during daytime hours when boundary-crossing events are most relevant (heating/cooling during the day).
- **FLAG #4 — SELF-NEGATING CONSTRAINT:** The signal defaults to overnight_only=True AND boundary_threshold=0.2°F. At night, temperature trends are typically flat or slowly declining. Finding a 0.3°F/hour slope toward a boundary 0.2°F away within 5 minutes of data is virtually impossible. This may explain why the signal was "dead" (returned None).
**Verdict: NOT READY** — Fix temporal misalignment (window should be in hours, not observations). Remove overnight-only default. Verify boundary approach logic works with hourly-granularity data.

#### 22. ghcn_calibration — DEGENERATE / RECLASSIFY
**Status:** Flagged as DEGENERATE because evaluate() reads METAR data from days[] and compares to... METAR settlement. Circular.
**Physical basis:** GHCN-Daily TMAX as independent NOAA ground truth is EXCELLENT conceptually. The evaluate_for_station() method correctly uses actual GHCN .dly files.
**FLAG #5 — CIRCULAR VS ORTHOGONAL:** The evaluate() method (used in backtests) is circular because it reads METAR-derived high temps from the days[] list. But evaluate_for_station() is GENUINELY ORTHOGONAL — it reads independent NOAA GHCN-Daily data from file. The DEGENERATE label is only correct for evaluate(), not the signal itself. The signal should be re-enabled with evaluate_for_station() as the primary path.
**Verdict: CONDITIONALLY READY** — Remove evaluate() circular implementation. Route all backtesting through evaluate_for_station(). Then re-enable for fusion.
**Data cutoff:** 2026-02-28 — this signal expires if GHCN data isn't refreshed.

#### 23. rdae_mos — ACTIVE / READY
**Status:** 65.93% settlement accuracy. Verified.
**Physical basis:** Regime-dependent analog ensemble with MOS calibration (PAVA isotonic regression). Excellent hybrid approach.
**Bugs:** None found. Code quality is good — PAVA implementation is correct.
**Verdict: READY** — Top performer. Keep.

### I: Reversion Pool (ρ=1.0, invertible lane — always DOWN)

#### 24. spike_reversion — ACTIVE / READY
**Physical basis:** Spike direction asymmetry (Gray Room R4 validated). Up-spikes revert more reliably than down-spikes.
**Bugs:** None. The R7-A1 confidence inversion fix (transient=0.50, structural=0.10) is correctly applied.
**Verdict: READY**

#### 25. radiational_cooling — ACTIVE / READY
**Physical basis:** Four-pillar (clear skies, calm wind, low humidity, dry BL) → radiational cooling. Excellent physics.
**Bugs:** None found. The code correctly distinguishes radiational from advective cooling and handles fog, arctic air masses, and mixed cooling regimes.
**Verdict: READY** — Best physics signal in the system.

### J: Momentum Pool (ρ=1.0, invertible lane — always UP)

#### 26. warm_momentum — ACTIVE / CONDITIONALLY READY
**Physical basis:** 3+ consecutive warming days → momentum continues. Sound.
**Bugs:**
- **Streak bonus always zero:** `streak_bonus = 0.05 * max(0, self.min_streak - 2)` where `self.min_streak = 3`, so `streak_bonus = 0.05 * max(0, 3-2) = 0.05`. This is NOT what the comment says ("+0.05 per additional day"). The comment is wrong.
**Verdict: CONDITIONALLY READY** — Fix streak bonus computation to match documentation.

#### 27. cooling_failure — ACTIVE / CONDITIONALLY READY
**Physical basis:** System expects cooling (z-score > +1.0 for 2+ days) → cooling doesn't arrive → cooling failure. Good.
**Bugs:**
- **Confidence direction:** The signal returns 'down' but the comment says "predicts DOWN (cooling failure → continued warmth against cooling expectation)". If cooling fails and warmth continues, the prediction should be UP (warmer), not DOWN. **FLAG #6 — DIRECTIONAL CONTRADICTION:** The signal returns 'down' but the physical outcome of cooling failure is continued warmth → UP.
- Look at INVERTIBLE_LANE_SIGNALS: cooling_failure is listed as "always 'up'" — but the code returns 'down'. **This is a 180° directional error.** Either the code is wrong or the documentation is wrong.
**Verdict: NOT READY** — Resolve direction contradiction. If cooling fails → warmth continues → predict UP, not DOWN.

### K: NWP Upper Pool (ρ=0.50, freshness=6h)

#### 28. 850mb_temp_advection — ACTIVE / CONDITIONALLY READY
**Status:** Data coverage starts 2026-07-08. ~2 months of data.
**Physical basis:** 850hPa temperature advection is THE single most important physical signal for mid-latitude temperature forecasting. Correct concept.
**Bugs:**
- **Advection approximation:** The code computes advection_strength = mag_ms × n_s_component × (1 + 0.5×temp_z). This is NOT temperature advection. True advection is -u·∇T. The code computes "meridional wind strength × temperature anomaly," which is a wind-direction-weighted temperature anomaly, not advection. **FLAG #7 — PHYSICAL APPROXIMATION:** The signal name claims "advection" but the computation is a wind-direction heuristic. This may still have predictive value but it's not physically advection.
- **Too few data:** 2 months of data vs the 30-day rolling window means the rolling statistics have very few bootstrap samples. The history query may return fewer than 5 values, forcing fallback to `hist_std = 5.0`.
**Verdict: CONDITIONALLY READY** — Rename signal to "850mb_wind_direction_temp_anomaly" or accept the approximation. Need ≥60 days of stable data.

#### 29. 500mb_height_pattern — ACTIVE / CONDITIONALLY READY
**Status:** Same data limitation as 850mb.
**Physical basis:** 500mb height anomaly → ridge/trough → warming/cooling. Valid.
**Bugs:**
- **Fallback values:** When <5 history values exist, uses `hist_mean = 5700.0, hist_std = DEFAULT_STD_HEIGHT = 60.0`. These climatological fallbacks are wrong for coastal stations (KLAX at ~5860 gpm on average, not 5700). **FLAG #8 — STATION-SPECIFIC CLIMATOLOGY:** The fallback should be station-dependent, not a universal 5700 gpm.
**Verdict: CONDITIONALLY READY** — Same data quantity issue as 850mb. Fix fallback climatology.

### L: Modulators (not in any pool)

#### 30. cross_model_divergence — ACTIVE / READY
**Status:** evaluate() stub fixed Sep 1.
**Physical basis:** GFS vs ECMWF after bias correction → divergence. When models disagree, lower confidence. Excellent meta-signal.
**Bugs:**
- **Bias temperature unit inconsistency:** nwp_forecasts.db stores temperatures in Celsius but METAR actuals are in Fahrenheit. The code correctly converts `forecast_c → forecast_f → bias = actual_f - forecast_f`. Verified correct.
**Verdict: READY** — Keep as confidence modulator.

#### 31. frontal_passage_intraday — ACTIVE / READY
**Physical basis:** Three-condition intraday frontal detector (wind shift + pressure jump + temp change). Excellent.
**Bugs:** None found. Self-tests pass. Code quality is good.
**Verdict: READY** — This is the best intraday signal in the system.

---

## PART 2: 21 SIGNALS IN SIGNAL_REGISTRY NOT IN THE 31

### ACTIVE (9 signals, code exists, wiring uncertain)

#### 32. fogr_reversion_signal (Family C)
**Code:** Archived in 2026-08-11. Loaded via `_load_archived()`.
**Physical basis:** FOGR reversion pattern — a specific intraday microstructure pattern.
**Wired?** Imported into __init__.py via archive compat. Actually WIRED in code but for safety only. Not in REGISTERED_SIGNAL_NAMES.
**Verdict: TRASH** — Archived for a reason. Remove import.

#### 33. frontal_detector_signal (Family B)
**Code:** Archived. Loaded via `FrontalDetectorSignal = _load_archived(...)`.
**Physical basis:** Daily-level frontal detection. Replaced by frontal_passage_intraday which is superior.
**Wired?** Imported for backward compat only. Not registered.
**Verdict: TRASH** — Superseded by #31. Remove import.

#### 34. frontal_passage_detector (Family B)
**Status:** Never seen this file — likely an alias in the archive compat layer.
**Verdict: TRASH** — If it exists, it's an earlier version superseded by #31.

#### 35. frontal_passage_nowcast_signal (Family B)
**Code quality:** 560+ lines. Wind-direction-aware upstream station selection, time-of-day filtering, spatial gradient detection. Good code.
**Physical basis:** Spatial nowcasting using temperature gradients between target station and upstream METAR stations. Clever concept.
**Wired?** Imported in __init__.py. Listed as "LANE ONLY" — NOT in SignalRegistry. Used in nowcasting_lane.py.
**Verdict: CONDITIONALLY READY** — Keep in nowcasting lane. Good signal but it's a nowcast, not a daily ensemble signal.

#### 36. settlement_arbitrage_signal (Family C)
**Code:** Archived. Loaded via `SettlementTimeArbitrageSignal = _load_archived(...)`.
**Physical basis:** Price vs fair value arbitrage. This is NOT a weather signal — it's a market microstructure signal.
**Wired?** Imported for backward compat only. Not registered.
**Verdict: TRASH** — Not a weather signal. Remove import.

#### 37. simple_trend_signal (Family C)
**Code:** Imported from `core.signals.retired.simple_trend_signal`.
**Physical basis:** Simple trend. Superseded by honest_trend and warm_momentum.
**Wired?** Not in REGISTERED_SIGNAL_NAMES. Kept as import-only.
**Verdict: TRASH** — Superseded.

#### 38. spread_based_entry_signal (Family A)
**Code:** Live code (not archived). Has evaluate() method.
**Physical basis:** Market spread → settlement convergence. This is a TRADING signal, not a weather signal.
**Wired?** Imported in __init__.py. Not in REGISTERED_SIGNAL_NAMES. Listed as DISABLED.
**Verdict: TRASH** — Not a weather signal. Move to trading module.

#### 39. volume_momentum_signal (Family C)
**Code:** Archived. `VolumeMomentumSignal = _load_archived(...)`.
**Physical basis:** Order flow anomaly. Trading signal, not weather.
**Wired?** Not in REGISTERED_SIGNAL_NAMES.
**Verdict: TRASH** — Not a weather signal. Remove import.

#### 40. wind_direction_shift (Family D)
**Status:** RETIRED 2026-08-25. Abstract evaluate() — never produced output.
**Code:** In core/signals/retired/wind_direction_shift.py.
**Wired?** Imported in __init__.py for backward compat.
**Verdict: TRASH** — Already retired. Remove import.

#### 41. dewpoint_depression_modulator (Family D)
**Status:** Never seen a file by this name. May be a planning concept, not implemented.
**Verdict: TRASH** — No code exists. Drop from tracking.

#### 42. upper_atmosphere_signal (Family B)
**Status:** Combined 850mb + 500mb. The individual signals (#28 and #29) are registered separately now.
**Verdict: TRASH** — Superseded by individual signals. Remove if it exists as a separate module.

### GATED (1 signal)

#### 43. ai_composite_signal (Family D)
**Status:** ML pipeline not deployed. Gated off.
**Verdict: TRASH** — Not deployed. No ML path exists in the weather engine. Remove.

### KILLED (5 signals, still in SIGNAL_REGISTRY)

#### 44. persistence_signal — TRASH (48.31%)
#### 45. goldilocks_signal — TRASH (49.85% negative EV)
#### 46. late_day_momentum — TRASH (48.31%)
#### 47. nwp_analog_signal — See FLAG #1 and Part 1 item #9 above
#### 48-53. See ORPHANED below

### ORPHANED (6 signals)

#### 48. dual_polarity_signal — NOT WIRED / TRASH
**Code quality:** The DualPolaritySignal wrapper class exists but is NEVER instantiated in any pipeline. It's importable but has zero wire paths.
**The concept is correct** — pressure direction does invert seasonally. But the framework is unused.
**Verdict: TRASH** — If the seasonal inversion fix for pressure_delta (FLAG #2) is needed, implement it directly in pressure_delta_signal.py, not through an unwrappable framework.

#### 49. esdr_signal — NOT WIRED / TRASH
**Code:** Archived. Never wired.
**Verdict: TRASH**

#### 50. intraday_metar_confirmation — REPLACED / TRASH
#### 51. intraday_metar_confirmation_signal — REPLACED / TRASH
**Both superseded by** metar_nowcast (#13).
**Verdict: TRASH**

#### 52. metar_dtdt_signal — DEAD END / TRASH
**Verdict: TRASH**

#### 53. nwp_dtdt_fusion_signal — DIFFERENT ARCHITECTURE / TRASH
**Verdict: TRASH**

---

## PART 3: NWP_ANALOG CONTRADICTION (Detailed)

**FLAG #1 — CRITICAL:** nwp_analog has THREE contradictory statuses:

1. **REGISTERED_SIGNAL_NAMES includes 'nwp_analog'** — It's in the 31 registered signals, confirming it's part of the live ensemble.
2. **nwp_direct pool definition includes 'nwp_analog'** — CANONICAL_POOL_DEFINITIONS lists it in the nwp_direct pool (with nwp_direct, temperature_advection, honest_trend, hrrr_bias_corrected). This means the ρ=0.70 pool constraint INCLUDES nwp_analog in its effective sample size calculation.
3. **NWP_ANALOG_ENABLED=False by default** — The env var gate means 99.9% of pipeline runs never call evaluate(). The signal returns (None, 0.0) silently.
4. **Documentation calls it KILLED** — 32.63% accuracy. Various docs say it's killed.

**Impact:** The pool-vs-pool correlation correction assumes nwp_analog fires. It doesn't. This silently inflates n_eff (effective independent signals) for the nwp_direct pool. The ρ=0.70 constraint allows correlation correction within the pool — but if a signal never fires, the correction thinks there are 5 signals in the pool when there are really 4 (or fewer). This means the ensemble weights might be slightly off.

**Resolution:** Choose ONE:
  - **KILL IT:** Remove 'nwp_analog' from REGISTERED_SIGNAL_NAMES AND from the nwp_direct pool definition. Remove the import.
  - **FIX IT:** Fix the data to provide matching analogs. Set NWP_ANALOG_ENABLED=True. Verify accuracy > 50%.
  - **HOLD IT:** Add a comment in pool definition noting it's conditionally active. Set ρ_within for the pool based on actual firing signals (4, not 5).

Currently: worst of all worlds. Half-registered, half-killed, silent pollution of pool correlation.

---

## PART 4: SIGNAL_MODULES STUBS (7 files)

**All seven are TRASH.** Here's why:

| File | Lines | What It Does | Verdict |
|---|---|---|---|
| `atmospheric_pressure_tendency.py` | ~12 | `(pressure - 1013.25) * 0.05` | **TRASH** — Trivial stub using static baseline. Pressure tendency signal (#19) already exists. |
| `diffusion_convergence_signal.py` | ~12 | `(temp - press*0.02)*0.1 + (dp/temp)*0.2` | **TRASH** — Dimensionally meaningless formula mixing temperatures and pressures. |
| `humidity_inversion_anomaly.py` | ~12 | `(dp/temp)*100 * 0.3` | **TRASH** — Not an inversion detector. A relative humidity proxy wrapped in noise. |
| `momentum_differential_signal.py` | ~12 | `(temp-15)*0.1 + (ws-5)*0.2 + (vis-10)*0.05` | **TRASH** — Random coefficients. No physical basis. |
| `trend_deviation_signal.py` | ~12 | `(temp-20)*0.1 + (press-1013)*0.01 + (ws-10)*0.05` | **TRASH** — Same pattern as above, different arbitrary constants. |
| `visibility_trend_deviation.py` | ~12 | `(vis - 10) * 0.1` | **TRASH** — Visibility alone has no direct temperature signal. |
| `wind_direction_variance.py` | ~12 | `(wd - 180) / 360 * 0.5` | **TRASH** — "Variance" detection against 180° baseline? Nonsense physics. |

**Recommendation:** Delete the entire `signal_modules/` directory. None of these stubs have viable algorithm concepts. If any of these ideas are worth pursuing (e.g., a real humidity inversion detector), they should be designed from first principles, not "filled in" around placeholders.

---

## PART 5: LEGACY approach_ FUNCTIONS (13 in scripts)

**Signal value worth preserving: ZERO.**

| Function | Script | What It Does | Preserve? |
|---|---|---|---|
| `approach_gaussian` | ensemble_v7, v10, v