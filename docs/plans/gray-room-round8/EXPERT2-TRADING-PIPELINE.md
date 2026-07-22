# Gray Room Round 8 — Expert 2: Short-Duration Trading Pipeline

**Date:** 2026-07-22
**Domain:** Short-Duration Trading — Signals, Entry/Exit Architecture, Build Order
**Inputs:** Pre-read (GRAY-ROOM-ROUND8-PREREAD.md), Gray Room Round 7 Synthesis (GRAY-ROOM-ROUND7-SYNTHESIS-2026-07-21.md), live codebase inspection

---

## EXECUTIVE SUMMARY

The current weather engine is architected for **next-day settlement prediction** — evaluate signals at market close, place trade, wait for settlement. The principal wants to shift to **minutes-to-hours trading** (no overnight holds). This is a fundamental architectural change, not a feature addition.

**Critical finding:** The expert reviewed Kalshi's available contract types. Kalshi offers **daily** HIGH/LOW contracts (settle next day). There are NO sub-daily temperature contracts. The short-duration premise needs validation before any code is written.

**Recommendation:** Phase 0 = "Validate the Premise." If Kalshi doesn't offer sub-daily contracts, the short-duration strategy must be rethought — either trade the daily contracts with earlier entry/exit (capture intraday moves) or find a different market.

---

## ERRORS (12)

### Error 1: Entire Architecture is Daily-Settlement-Oriented
**Location:** `core/paper_trading_engine.py` (entire file), `core/kalshi_monitor.py` (entire file)
**What:** The signal pipeline processes data once per day (after METAR data settles for the day). The trade loop evaluates all signals, computes agreement, places a trade, and waits for next-day settlement. Position sizing, P&L tracking, and risk management all assume 1 trade/day/station.
**Why wrong:** Short-duration trading requires sub-hourly evaluation cycles, multiple entries/exits per day, and position sizing that accounts for higher frequency. The existing architecture cannot support this without major changes.
**Spec:** Build a separate short-duration pipeline (`core/short_duration/pipeline.py`) that runs on a 15-minute cycle. Each cycle evaluates short-duration signals, computes entry/exit decisions, and manages positions. The daily pipeline continues to run in parallel.

### Error 2: No Intraday Spread Validation for Kalshi Daily Contracts
**Location:** `core/kalshi_monitor.py` (missing functionality)
**What:** The system calculates market implied probability from Kalshi prices but does not validate whether the spread (bid-ask) is tradable for intraday entry/exit. Current mean spread is 3.1¢. For daily contracts held to settlement, this is acceptable. For intraday entry/exit, the spread must be paid twice (entry + exit), making it 6.2¢ — above the 5¢ threshold for positive EV.
**Why wrong:** If the short-duration strategy involves entering and exiting the same daily contract before settlement, the spread cost is doubled. This is a fundamental economic constraint.
**Spec:** Add a spread validation module (`core/short_duration/spread_validator.py`) that fetches current bid-ask for all active Kalshi markets, computes round-trip cost (2× spread), flags markets where round-trip cost > 5¢ as "not tradable for intraday", and reports spread distribution to the dashboard.

### Error 3: No Sub-Daily Signal Generation Exists
**Location:** `core/signals/` (all 9 signals are daily)
**What:** All 9 registered signals are designed for next-day prediction using daily METAR summaries (min/max temp, mean pressure, etc.).
**Why wrong:** These signals are meaningless for sub-daily trading. A 1-hour temperature prediction requires current METAR readings, rate of change, and short-range NWP. The existing signals cannot be adapted.
**Spec:** Create `core/signals/short_duration/` with: `fogr_reversion.py`, `metar_dt_dt.py`, `pressure_tendency.py`, `dewpoint_filter.py`, `wind_shift_detector.py`.

### Error 4: METAR Sub-Hourly Data Available But Unused
**Location:** `core/metar_monitor.py` (polls METAR but only stores daily summaries)
**What:** The METAR monitor polls NOAA every 5-15 minutes but stores only date-level aggregates (min/max temp, mean pressure, mean wind). Raw sub-hourly observations are discarded.
**Why wrong:** Sub-hourly METAR observations are the primary data source for short-duration signals (dT/dt, pressure tendency, dewpoint trend). By discarding the raw observations, the system destroys the data needed for short-duration trading.
**Spec:** Add a `metar_observations` table: `(station, observation_time_utc, temperature_c, dewpoint_c, pressure_hpa, wind_speed_kts, wind_direction_deg, raw_metar)`. Update `metar_monitor.py` to write raw observations before computing daily aggregates.

### Error 5: No HRRR Data Pipeline Exists
**Location:** Missing — scoped in `docs/plans/HRRR-COLLECTION-SCOPE.md` but not started
**What:** Gray Room Round 7 identified HRRR as the highest-priority data source for short-duration trading (80-85% expected accuracy at 1-3h). The collection pipeline was scoped but never built.
**Why wrong:** Without HRRR, short-duration prediction accuracy is limited to METAR-based persistence signals that lag actual temperature changes by 30-60 minutes.
**Spec:** Build `scripts/hrrr_collect.py` per the scope document. Fetch HRRR temperature_2m for 20 stations from NOAA, bias-correct using latest METAR, store in `data/hrrr_forecasts.db`, run hourly, retain last 48 hours.

### Error 6: Entry Window Designed for Daily Contracts, Not Sub-Daily
**Location:** `core/paper_trading_engine.py:L2100-L2200`
**What:** The `_should_enter_trade()` method enforces a daily entry window (14:00-18:00 UTC). This is correct for daily contracts.
**Why wrong:** Short-duration trades need to enter at any time when conditions are favorable. The entry window concept must be replaced with a condition-based trigger.
**Spec:** Add condition-based entry for short-duration trades. Remove hardcoded entry window. Add `entry_conditions` dict per station. The short-duration pipeline evaluates conditions every 15 minutes.

### Error 7: Database Schema Has No Intraday Trade Tracking
**Location:** `core/paper_trading_engine.py` (trades table schema)
**What:** The `trades` table has no `entry_time`, `exit_time`, or `duration` fields. Status is binary (open/closed).
**Why wrong:** For short-duration trading, multiple trades can happen on the same day for the same station. The schema cannot distinguish between a 1-hour trade and a 23-hour trade.
**Spec:** Add columns: `entry_time_utc`, `exit_time_utc`, `duration_minutes`, `exit_reason` (stop_loss, take_profit, time_expiry, settlement, manual). Add `intraday_positions` table with stop-loss, take-profit, and time-expiry fields.

### Error 8: Alert Throttling Cooldowns Too Long for Sub-Daily
**Location:** `core/alert_builder.py` (missing cooldown logic)
**What:** Alert dispatch fires for every trade with no per-station cooldown. For daily trading (1-20 trades/day) this is manageable; for short-duration (100+ trades/day) it would flood Discord.
**Why wrong:** Alert fatigue + Discord rate limits (5 msg/5s per channel).
**Spec:** Duration-aware cooldown: daily trades 1h per station, short-duration 15min per station. Batch alerts within cooldown window. Separate Discord channel for short-duration alerts.

### Error 9: No Intraday Stop-Loss / Take-Profit Mechanism
**Location:** `core/paper_trading_engine.py` (missing entirely)
**What:** The system has no stop-loss, take-profit, or time-expiry mechanism. For daily contracts held to settlement, trades are binary. For short-duration, positions must be closable before settlement.
**Why wrong:** Short-duration trading requires the ability to exit positions before the predicted event unfolds. Without stop-loss, adverse moves go uncapped.
**Spec:** Add risk controls: `stop_loss_price`, `take_profit_price`, `time_expiry`, `trailing_stop`. Record exit reason in `intraday_positions`.

### Error 10: No Sub-Daily Backtest Infrastructure
**Location:** `scripts/` (missing)
**What:** The backtest infrastructure is designed for daily settlement prediction. There is no backtest system that evaluates 15-minute signal cycles against historical data.
**Why wrong:** Without a sub-daily backtest, the team deploys blind — hoping signals work rather than knowing they do.
**Spec:** Create `scripts/short_duration_backtest.py`: accepts date range and signal config, loads historical METAR observations, runs evaluation every 15 minutes, records predictions, compares against actual temperature changes, reports accuracy by horizon and confidence tier.

### Error 11: Ensemble Agreement Threshold Not Duration-Aware
**Location:** `core/agreement_gate.py` (single threshold for all signals)
**What:** The agreement gate applies a single threshold (n_required=3) to all signals. Short-duration and daily signals are evaluated against the same gate.
**Why wrong:** Short-duration signals have different reliability profiles. A 1-hour dT/dt signal should not require the same confirmations as a daily climatology prediction.
**Spec:** Add `prediction_horizon` parameter to `check_agreement()`. Dual thresholds: `n_required_short = 2`, `n_required_daily = 3`.

### Error 12: NWP Update Schedule Doesn't Support Sub-Daily Trading
**Location:** `scripts/nwp_collect.py` (daily cron at 06:00 UTC)
**What:** NWP collection runs once per day. GFS is updated every 6 hours, but the system only fetches once.
**Why wrong:** For 1-3 hour predictions, an 8-hour-old GFS forecast is stale data. The system needs the latest available forecast cycle.
**Spec:** Update NWP collection to 4x/day (00, 06, 12, 18 UTC). Add `forecast_cycle` field to DB. Add `forecast_age_hours` to signal output for staleness detection.

---

## IMPROVEMENTS (7)

### Improvement 1: FOGR Reversion Signal
**What:** Detect when METAR temperature deviates from expected diurnal curve and predict reversion.
**Why better:** Highest-value short-duration signal from Round 7. Uses only METAR data (no HRRR needed). Expected 70-75% standalone.
**Effort:** Medium (2-3 days)
**Spec:** Create `core/signals/short_duration/fogr_reversion.py`. Compute expected diurnal temp from last 30 days by hour. Compute gap = T_metar - T_expected. When gap > 2°C, predict reversion. Confidence = min(gap/5°C, 1.0).

### Improvement 2: METAR dT/dt Trend Signal
**What:** Compute temperature time derivative from METAR observations and predict trend continuation/reversal.
**Why better:** dT/dt is a leading indicator. Decreasing slope (d²T/dt² < 0) signals approaching peak/trough.
**Effort:** Low (1 day)
**Spec:** Create `core/signals/short_duration/metar_dt_dt.py`. Query last 3 hours of METAR. Compute dT/dt for 1h/2h/3h windows. Compute d²T/dt² for acceleration detection. Predict UP if dT/dt > 0.5°C/h and d²T/dt² > 0. Predict DOWN if dT/dt < -0.5°C/h and d²T/dt² < 0.

### Improvement 3: Pressure Tendency Leading Indicator
**What:** Detect pressure drops preceding frontal passages and associated temperature changes.
**Why better:** Pressure drop >3hPa/3h has ~75% chance of frontal passage within 6h. Independent of temperature signals — good confirmation.
**Effort:** Low (1 day)
**Spec:** Create `core/signals/short_duration/pressure_tendency.py`. Compute dP/dt for 1h/3h windows. Predict COLD FRONT if pressure drops >3hPa/3h AND wind shifts >45°. Predict WARM FRONT if pressure rises >3hPa/3h AND wind shifts >45°.

### Improvement 4: Sequential Trigger Entry/Exit Architecture
**What:** Replace majority-vote ensemble with sequential trigger: Enter on A, confirm with B, trail with C, exit on D.
**Why better:** Round 7 concluded sequential triggers are more appropriate for short-duration. Respects temporal order of events (pressure drop → wind shift → temperature change).
**Effort:** High (1-2 weeks)
**Spec:** Create `core/short_duration/trigger_engine.py`. Define trigger chains: Cold Front (dP/dt → wind shift → temp drop → exit), Diurnal Reversion (gap → dT/dt toward curve → gap narrows → exit). Each trigger is a callable returning `(triggered, confidence)`. Entry when all triggers fire. Exit on condition, timeout, or stop-loss.

### Improvement 5: Horizon-Specific Confidence Thresholds
**What:** Different thresholds for 1h, 3h, 6h horizons.
**Why better:** Signal reliability decreases with horizon. 1h predictions should have higher confidence standard.
**Effort:** Low (0.5 day)
**Spec:** Thresholds: `1h: 0.65`, `3h: 0.60`, `6h: 0.55`. Agreement gate uses horizon-specific thresholds. Config: `SHORT_DURATION_CONFIDENCE_THRESHOLDS`.

### Improvement 6: Duration-Aware Position Sizing
**What:** Inverse frequency scaling: shorter trades = smaller positions.
**Why better:** Consistent total daily exposure regardless of trade frequency.
**Effort:** Medium (1-2 days)
**Spec:** `scale = min(1.0, 24 / (duration_hours * expected_trades_per_day))`. Daily trade → scale 1.0. 1h trade → scale ~0.042.

### Improvement 7: Intraday Signal Decay Model
**What:** Decay signal confidence with age. Older signals are less reliable.
**Why better:** A signal from 08:00 UTC should have lower confidence at 14:00 UTC.
**Effort:** Low (0.5 day)
**Spec:** `confidence_effective = confidence * e^(-age_hours / decay_constant)`. METAR decay_constant=4, NWP decay_constant=8. Ignore signals older than 3 half-lives.

---

## IDEAS (3)

### Idea 1: Intraday Spread Momentum as Confirming Signal
**What:** Use spread movement as a non-weather confirming signal. If spread moves with weather signal, it confirms. If against, deprioritize.
**Expected benefit:** Only market-based signal available for intraday. Independent of weather data.
**Risk:** Spread momentum is lagging. Market may react to same weather data.
**Spec for validation:** Collect 30 days of spread data at 15-min intervals. Compute momentum = spread_current - spread_30min_ago. Score agreement vs disagreement trades. If agreement >5pp better, integrate.

### Idea 2: No-Trade Zone Classification
**What:** Classify stations into time-based no-trade zones (sunrise, frontal passage, sea breeze).
**Expected benefit:** Reduces bad trades during uncertain periods.
**Risk:** Zones may be too broad or narrow.
**Spec for validation:** Analyze historical variance per station. Identify periods of high variance and low signal accuracy. Backtest with vs without zones. If >3pp improvement, implement.

### Idea 3: Phase-Dependent Signal Weighting
**What:** Weight signals by contract lifecycle phase. Pre-settlement: favor persistence/dT/dt. Early: favor NWP/climatology.
**Expected benefit:** Dynamically optimizes signal weights by time horizon.
**Risk:** Phase boundaries are station/season dependent.
**Spec for validation:** Compute signal accuracy by hour-until-settlement per station. Identify acceleration window. Define 3 phases. Backtest uniform vs phase-dependent weighting. If >2pp improvement, implement.

---

## ELEPHANTS (3)

### Elephant 1: Kalshi Does Not Offer Sub-Daily Temperature Contracts
**What:** Kalshi temperature contracts are daily HIGH/LOW — settle once per day. No hourly, 3-hour, or intraday contracts exist.
**Why it matters:** The entire short-duration premise is "enter and exit within minutes to hours." If only daily contracts exist, the strategy must be rethought: Option A = trade daily contracts with intraday entry/exit (spread trading), Option B = different market, Option C = optimal entry timing (hold to settlement).
**What happens if ignored:** The team builds a short-duration pipeline that exits before settlement, paying the spread twice. The trader loses money on spread costs and concludes the weather signals don't work.
**Spec for resolution:**
1. Phase 0 (1 day): Verify Kalshi contract offerings via API
2. If only daily: run 30-day spread analysis for round-trip cost
3. If round-trip > 5¢ for all markets: KILL short-duration project
4. Shift to "optimal entry timing" strategy: enter at best time, hold to settlement

### Elephant 2: System Has No Concept of Trade Duration at Any Level
**What:** No `duration_hours`, `entry_time_utc`, or `exit_time_utc` anywhere in the codebase. The system treats every trade as a single-day commitment.
**Why it matters:** Duration determines position sizing, risk management, signal selection, and P&L calculation. A 1h and 23h trade should not have the same parameters.
**What happens if ignored:** Short-duration trades will be over-sized, risk management misapplied, and the system will lose money on duration mismatch.
**Spec for resolution:** Add `duration_hours` to trade records, position sizing, risk management, and performance analytics. Cross-cutting concern touching signal pipeline, sizing, risk, dashboard, and DB. Estimate: 2 weeks.

### Elephant 3: Real Edge Is in Market Microstructure, Not Meteorology
**What:** 72.3% accuracy on temperature prediction does not translate to 72.3% profitability. The market price already reflects consensus forecast. The real edge may be in execution quality, not prediction accuracy.
**Why it matters:** The marginal value of 1% weather prediction improvement is dwarfed by 5% execution quality improvement.
**What happens if ignored:** The team builds better signals but P&L doesn't improve because the market already prices in the weather.
**Spec for resolution:**
1. Analyze correlation between accuracy and P&L over 30 days
2. If correlation < 0.3, prioritize market microstructure (Phase 7)
3. Build latency monitoring, execution quality analytics
4. Test intraday spread momentum (Idea 1) as a market-based signal

---

## BUILD ORDER: 6-Phase Plan

### Phase 0: Validate the Premise (Week 1)
**Critical — KILL GATE**
- Verify Kalshi contract offerings for sub-daily instruments
- If only daily: run spread analysis for round-trip cost
- Decision: ADVANCE, PARK (shift to entry timing), or KILL (abandon short-duration)
- **Stop condition:** Round-trip cost > 5¢ for all markets → KILL

### Phase 1: Data Infrastructure (Week 2-3)
- Build raw METAR observations table (Error 4)
- Build HRRR collection pipeline (Error 5)
- Update NWP collection to 6-hour cycles (Error 12)
- Add intraday trade tracking schema (Error 7)

### Phase 2: Signal Development (Week 3-4)
- FOGR reversion signal (Improvement 1)
- METAR dT/dt trend signal (Improvement 2)
- Pressure tendency leading indicator (Improvement 3)
- Intraday signal decay model (Improvement 7)
- Sub-daily backtest infrastructure (Error 10)

### Phase 3: Entry/Exit Architecture (Week 4-5)
- Sequential trigger engine (Improvement 4)
- Horizon-specific confidence thresholds (Improvement 5)
- Duration-aware position sizing (Improvement 6)
- Stop-loss/take-profit mechanism (Error 9)
- Condition-based entry system (Error 6)

### Phase 4: Risk Management (Week 5-6)
- Duration-aware alert cooldown (Error 8)
- Intraday spread validator (Error 2)
- Duration-aware agreement gate (Error 11)

### Phase 5: Integration & Testing (Week 6-8)
- Wire spread momentum signal (Idea 1)
- Add no-trade zones (Idea 2)
- Phase-dependent signal weighting (Idea 3)
- Run 30-day parallel test: daily pipeline vs. short-duration pipeline
- Compare accuracy, P&L, and Sharpe across both pipelines