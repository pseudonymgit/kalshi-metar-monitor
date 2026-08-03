# First-Principles Review — All Dead/Abandoned Signals & Orphaned Modules

**Context:** Current architecture is GEFS ensemble-mean-threshold → direction-specific calibration → Kelly sizing. 66.2% accuracy, 2,096 trades/year. Every signal assessed against: "does this add independent information to the GEFS ensemble fraction?"

---

## Tier 1: DIRECTLY COMPLEMENTARY — Worth Recalibrating & Sweeping

### 1. Trajectory Confirmation Gate
**Status:** 🗑️ Orphaned (.pyc only, source never committed)
**Gray Room estimate:** +2-4pp
**Why it could work:** GEFS ensemble fraction predicts direction. Trajectory gate checks whether the predicted temperature is on a multi-day trajectory (trending up/down over 3+ days). When the ensemble says UP but the 5-day trajectory is DOWN, the trajectory gate overrides. This is independent information — multi-day trend vs single-step forecast.
**First-principles verdict:** 🔁 **RESURRECT.** The Gray Room said +2-4pp. The trajectory information is independent of the ensemble fraction. Needs source recovery (decompile .pyc or rebuild from Gray Room spec).
**Effort to recover:** 2h
**Sweep config:** `--trajectory` flag, test on/off against calibrated baseline

### 2. Member-Weighted Voting (NEW)
**Status:** ❌ Not built yet
**Why it could work:** The 31 GEFS members are stored in blobs. We know from the calibration data that some members are more accurate per station. Instead of equal voting (one member, one vote), weight each member by its historical directional accuracy per station. This produces a more honest ensemble fraction.
**First-principles verdict:** ⚡ **BUILD.** The highest-leverage untapped signal. The data exists, the compute is cheap, and it directly addresses the overconfidence problem.
**Effort:** 4h
**Sweep config:** `--member-weights` flag, test weighted vs equal voting

### 3. ECMWF Fusion (82-member ensemble)
**Status:** 🔄 ECMWF backfill at 2025-02-25 (758 dates, still running)
**Gray Room estimate:** +1-3pp (if decorrelated from GEFS, ρ~0.7)
**Why it could work:** 51 ECMWF members + 31 GEFS = 82 members. More members = more honest spread. The ECMWF uses different physics, so it's partially decorrelated.
**First-principles verdict:** ⏳ **WAIT.** The backfill is running. When complete, build the 82-member ensemble fraction and recalibrate. Don't rebuild now — the data isn't ready.
**Effort:** 4h (when backfill completes)
**Sweep config:** 82-member vs 31-member comparison

### 4. Multi-Model Ensemble (NWP Fusion: GFS + ECMWF + ICON + GEM)
**Status:** ⚠️ Data exists (2,045 dates in nwp_forecasts.db)
**Gray Room estimate:** +1-2pp
**Why it could work:** 4 NWP models, each with different physics. GEFS is the ensemble mean of one model. Fusing across models adds real diversity.
**First-principles verdict:** 🟡 **LOW PRIORITY.** The data is ready (Edge 20), but the 4 models are different from the 31-member GEFS. This is a different signal, not a complement. Worth testing after member-weighted voting and trajectory gate.
**Effort:** 4h
**Sweep config:** `--multi-model` flag, GEFS-only vs multi-model

---

## Tier 2: MODULATORS — Could Improve Confidence or Sizing

### 5. Spatial Coherence Gate
**Status:** 🗑️ Orphaned (.pyc only, source never committed)
**Gray Room estimate:** +0.5-1pp
**Why it could work:** 6-region consensus. If 4/6 stations in the Northeast agree on direction, confidence should increase. If they disagree, confidence should decrease. This is a confidence modulator, not a signal.
**First-principles verdict:** 🟢 **WORTH REBUILDING.** Low effort, low risk. The data is cheap (just check other stations in the same region). The .pyc suggests the implementation was simple.
**Effort:** 1h
**Sweep config:** `--spatial` flag, on/off as confidence modulator

### 6. Station Skill Gate
**Status:** ✅ Source exists (core/station_skill_gate.py), .pyc exists
**Why it could work:** Per-station skill filter. If a station has historically low accuracy (KLAS at 43%), don't trade it. Simple gate.
**First-principles verdict:** 🟢 **ALREADY WIRED.** The station-sizing config in the sweep already achieves this (reduce losers 50%). The explicit gate is redundant with the per-station sizing knife.
**Effort:** Already wired. No rebuild needed.
**Sweep config:** Already covered by `--station-sizing` flag.

### 7. Agreement Gate
**Status:** ✅ Source exists (core/agreement_gate.py), .pyc exists
**Why it could work:** Require minimum GEFS member agreement before trading. Currently the system uses ensemble fraction as confidence, which already captures agreement. Redundant.
**First-principles verdict:** 🗑️ **KILL.** Redundant with ensemble fraction confidence. The calibration already handles the degree of agreement.
**Effort:** 0
**Sweep config:** N/A

### 8. Settlement Execution Gate
**Status:** ✅ Source exists (core/settlement_execution_gate.py), .pyc exists
**Why it could work:** Check settlement data before trading. Only trade if the station has recent settlement data.
**First-principles verdict:** 🟢 **WORTH WIRING.** Simple check. Prevents trading on stale data. Low effort.
**Effort:** 0.5h
**Sweep config:** N/A (not a sweep parameter, always-on guard)

### 9. Production Gate
**Status:** ✅ Source exists (core/production_gate.py), .pyc exists
**Purpose:** Safety gate for live trading (check bankroll, drawdown, etc.). Already covered by RiskManager and StopLossMonitor.
**First-principles verdict:** 🗑️ **KILL.** Redundant with existing risk controls.
**Effort:** 0
**Sweep config:** N/A

---

## Tier 3: SIGNALS — Worth Testing Against Current Baseline

### 10. Frontal Passage Detector
**Status:** ✅ Source exists (core/frontal_detector.py, core/signals/frontal_detector_signal.py, core/signals/frontal_passage_detector.py, core/signals/frontal_passage_intraday_signal.py, core/signals/frontal_passage_nowcast_signal.py)
**Built for:** Phase 4.6. Detects frontal passages from METAR data.
**Why it could work:** Frontal passages cause rapid temperature changes that GEFS may miss (the 0.5° grid smooths out fronts). This is independent information — METAR-based, not NWP-based.
**First-principles verdict:** 🟡 **TEST BUT DON'T EXPECT MUCH.** The weather engine is a daily direction predictor, not an intraday nowcaster. Frontal passages matter for intraday temperature, but the daily HIGH/LOW settlement is more influenced by the overall air mass, which GEFS captures well. The frontal detector may have been useful for the old alert-path system (which triggered on METAR crossings), but the GEFS pipeline is a different architecture.
**Effort:** 1h to merge from branch + wire
**Sweep config:** `--frontal` flag, tested as additional signal

### 11. Calendar Climatology Signal
**Status:** ✅ Source exists (core/signals/calendar_climatology_signal.py)
**Built for:** Early Phase A. Predicts based on historical averages.
**Why it could work:** Independent of GEFS — pure climatology. If a city is usually 85°F on August 15, and GEFS predicts 82°F, the climatology signal provides a counterpoint.
**First-principles verdict:** 🟡 **TEST.** This was a strong signal in earlier tests (68.9% standalone). But it's likely correlated with the GEFS mean (GEFS is already calibrated to climatology). Worth testing as a comparison.
**Effort:** 0.5h to wire
**Sweep config:** `--climatology` flag, blended as additional signal

### 12. Gaussian Signal + Gaussian V2
**Status:** ✅ Source exists (core/signals/gaussian_signal.py, core/signals/gaussian_v2_signal.py)
**Built for:** Early Phase A. Gaussian-based probability estimation.
**Why it could work:** Different statistical approach to the same data. Likely highly correlated with GEFS mean.
**First-principles verdict:** 🟡 **TEST.** Earlier tests showed Gaussian alone at 68.45% — but that was against METAR proxy, not real Kalshi data. Worth testing against the current baseline.
**Effort:** 0.5h to wire
**Sweep config:** `--gaussian` flag, tested as signal

### 13. Forecast Disagreement Signal
**Status:** ✅ Source exists (core/signals/forecast_disagreement_signal.py)
**Built for:** Early Phase A. Measures disagreement between NWP models.
**Why it could work:** Low disagreement = high confidence. High disagreement = low confidence. This is a confidence modulator, not a signal.
**First-principles verdict:** 🟡 **TEST.** Already partially captured by the ensemble fraction (which measures member agreement). But cross-model disagreement (GFS vs ECMWF vs ICON) is different from within-GEFS agreement. Worth testing.
**Effort:** 1h
**Sweep config:** `--disagreement` flag, as confidence modulator

### 14. NWP Analog Signal
**Status:** ✅ Source exists (core/signals/nwp_analog_signal.py)
**Built for:** Phase 2. k-NN analog matching against historical NWP data.
**First-principles verdict:** 🗑️ **KILL.** The Gray Room analyzed this extensively. The analog-matching approach (k-NN) tested at 32.63% directional accuracy — below coin flip. The GEFS ensemble fraction is a better approach (predicts based on physics, not historical patterns).
**Effort:** 0
**Sweep config:** N/A

### 15-21. Other Signals (Persistence, Pressure Delta, Wind Direction, Dewpoint, etc.)
**Status:** ✅ Source exists for all
**First-principles verdict:** 🗑️ **KILL ALL.** These signals were built for the old alert-path system (METAR-based integer-cross detection). They predict temperature at a specific point in time, not daily HIGH/LOW direction. The GEFS ensemble fraction already captures the relevant physics. None of these signals add independent information to a 31-member NWP ensemble.
**Effort:** 0
**Sweep config:** N/A

---

## Tier 4: ALREADY KILLED — Stay Dead

### 22. Goldilocks Daily Signal
**Status:** ✅ Killed (49.85% negative EV)
**First-principles verdict:** 🗑️ **KILL.** The Gray Room V2 confirmed this. The $0.15 minimum price kills daily signal viability. The microstructure edge detector variant (sub-15-minute entry/exit) is a different product pathway, not a daily signal. Keep dead.

### 23. Late-Day Momentum
**Status:** ✅ Killed (48.31% directional accuracy)
**First-principles verdict:** 🗑️ **KILL.** Comprehensive split backtest showed below coin flip. 4 different threshold variants tested. All failed.

### 24. NWP Analog (k-NN)
**Status:** ✅ Killed (32.63%)
**First-principles verdict:** 🗑️ **KILL.** Below coin flip. The GEFS ensemble fraction is the correct approach.

---

## Summary: What to Recalibrate & Sweep

| Priority | Item | Effort | Expected Impact | Sweep Config |
|----------|------|--------|-----------------|-------------|
| **P0** | Member-weighted voting | 4h | +2-5pp | `--member-weights` |
| **P1** | Trajectory confirmation gate | 2h | +2-4pp | `--trajectory` |
| **P2** | Spatial coherence modulator | 1h | +0.5-1pp | `--spatial` |
| **P3** | Frontal detector | 1h | +0.5-1.5pp | `--frontal` |
| **P4** | Calendar climatology | 0.5h | +0-1pp | `--climatology` |
| **P5** | Gaussian signal | 0.5h | +0-1pp | `--gaussian` |
| **P6** | Forecast disagreement | 1h | +0-0.5pp | `--disagreement` |
| **P7** | Multi-model (NWP) fusion | 4h | +1-2pp | `--multi-model` |
| **PARK** | ECMWF 82-member fusion | 4h | +1-3pp | (wait for backfill) |
| **KILL** | Station skill gate, agreement gate, production gate, settlement execution gate, persistence, pressure, wind, dewpoint, frontal signals, nowcast, NWP analog, goldilocks, late-day momentum | 0 | — | — |

**Total effort:** ~14h for P0-P7. Outcome: a comprehensive sweep of every signal worth testing against the current GEFS architecture.

**Sweep protocol:** Run each config independently against the calibrated baseline. Compare P&L, Sharpe, accuracy. Only keep signals that add ≥1pp accuracy or ≥5% P&L improvement.