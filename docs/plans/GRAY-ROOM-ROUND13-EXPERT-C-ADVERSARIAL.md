# Gray Room Round 13 V2 — Expert C Report
## Adversarial Scenario Analysis: Failure Modes of a 67%-Accurate GEFS Daily-HIGH Trading System

**Date:** 2026-08-02
**Analyst:** Expert C (GPT 5.4)
**Model mandate:** Enumerate every failure mode, detection method, and proven mitigation

---

## Executive Summary

The GEFS ensemble-fraction system reports 67.1% accuracy on 85 trades. This is a fragile signal from a small, autocorrelated sample. The true out-of-sample accuracy has a ±10pp confidence interval (95% CI). Twenty-five distinct failure modes are enumerated below, ranging from settlement-source mismatches to portfolio-level Kelly collapse.

**Three structural vulnerabilities that will lose money within the first 30 live trades:**

1. **Fee-model Kelly formula is incorrect** — does not reference market price; over-bets by up to 16× in high-price markets
2. **Portfolio correlation** — 20 stations are NOT independent; Kelly sizing systematically 3–5× too large
3. **Settlement-source mismatch** — Kalshi settles on NWS CLI for specific ICAO stations; GEFS calibrated against city center or wrong station

---

## Full Failure Mode Catalog

---

### 1. Settlement Station Mismatch (HIGH)
**Severity:** HIGH — structural, systematic, guaranteed for at least some stations

**The problem:** Kalshi settles daily high/low markets on the NWS Daily Climate Report (CLI) for a specific ICAO station. The system pulls GEFS data for city-center coordinates or a different ASOS station. Example: "Chicago" market might settle on KMDW (Midway) while GEFS forecast is for KORD (O'Hare). These stations routinely report daily highs differing by 1–4°F. Reddit traders confirm station-mapping mistakes cost real money (KORD vs KMDW, KDFW vs KDAL, KHOU vs KIAH).

When the model says 67% accuracy but the station is wrong, true accuracy on the settlement reference could be 50–55% — below the fee breakeven.

**Detection method:**
- Cross-reference each KXHIGH market's settlement station (from Kalshi market rules or API) against the station used for GEFS pull
- Compute station-pair daily high differences from ISD data — flag if RMSE > 1.5°F
- Audit the first 10 live settlements vs model predictions

**Proven mitigation:**
- Pre-deployment audit: for each of 20 stations, verify Kalshi settlement ICAO matches GEFS coordinates
- For mismatched pairs (KORD vs KMDW), either re-pull GEFS for correct station or build statistical correction
- Use Apify Kalshi Weather Markets actor for pre-mapped ICAO stations
- Document the exact station per market in a lookup table

---

### 2. NWS CLI vs METAR Discrepancy (HIGH)
**Severity:** HIGH — model calibrated against the wrong reference data

**The problem:** The system consumes METAR hourly observations in near-real time. Kalshi settles on the NWS Daily Climate Report (CLI), which undergoes quality control. The CLI daily high can differ from METAR hourly max by 0.5–2.0°F because:
- METAR reports at :53 past the hour → misses sub-hourly spikes
- CLI uses 1-minute ASOS data with QC applied
- CLI 24-hr max is midnight-to-midnight LOCAL STANDARD TIME (not UTC)
- Reddit traders report cases where METAR showed 75°F but CLI resolved at 76°F (decimal rounding)

If the backtest was run against METAR data, the 67.1% accuracy does not reflect CLI-based settlement accuracy.

**Detection method:**
- Re-run all 85 backtest trades against NWS CLI historical values (NCEI NCDC database)
- Compute accuracy delta: if CLI-derived accuracy > 5pp below METAR-derived, system has calibration drift
- Track CLI-vs-METAR discrepancies per station

**Proven mitigation:**
- Recalibrate all thresholds against NWS CLI historical data, not METAR
- Add 0.5–1.0°F decision buffer: only trade when predicted margin > 1.5°F
- Pull CLI data historically from NCEI/NCDC for ALL stations before going live
- Document which data source was used in backtest vs settlement

---

### 3. DST Settlement Quirk (MED)
**Severity:** MED — affects 4 months/year

**The problem:** NWS Climate Reports use local standard time year-round for daily high reporting. During DST (March–November), the daily high is recorded 1:00 AM local to 12:59 AM local the following day — not midnight-to-midnight. This means:
- "April 15" daily max measured 1:00 AM Apr 15 – 12:59 AM Apr 16 local standard time (= 2:00 AM – 1:59 AM DST)
- A temperature spike between midnight–1:00 AM DST on Apr 16 counts toward Apr 15's high
- Model expects clean calendar-day boundary — but it doesn't exist during DST

**Detection method:**
- Check settlement dates for DST-period trades — does settlement include post-midnight readings?
- Cross-reference NWS CLI report timestamp for DST vs non-DST dates

**Proven mitigation:**
- Hard-code DST adjustment into settlement time window calculation
- Do not trade daily-high markets in first 3 days after DST transition (noisy data)
- Add +1°F uncertainty buffer during DST months

---

### 4. Correlation Cascade — Synoptic System Failure (HIGH)
**Severity:** HIGH — can exceed daily risk control within minutes

**The problem:** A single synoptic weather system affects 10–15 of 20 stations simultaneously. If GEFS mis-times or mis-characterizes the system, ALL positions in its path lose on the same day. With +$325 avg winner and -$70 avg loser, 10 simultaneous losses = -$700 in one day. The $100 daily loss limit would trigger within minutes.

The system's 67.1% accuracy on individual trades is meaningless when 10 correlated trades share the same error source. Individual-trade Kelly breaks completely at portfolio level.

**Detection method:**
- Real-time correlation tracker: cluster stations by synoptic region (Northeast, Midwest, Southeast, Plains, West Coast)
- Flag when >60% of positions in a region share same direction prediction
- Monitor GEFS ensemble spread for the controlling synoptic system — if spread > 1.5σ historical, flag cascade risk

**Proven mitigation:**
- Region-level position caps: max 3 positions per synoptic region, max 8 total simultaneous positions
- Correlated-Kelly sizing: `f_eff = f_station / (1 + (N-1)ρ)` where ρ ≈ 0.3–0.5 for same-region stations
- Synoptic regime override: if single system drives >5 stations, reduce Kelly multiplier to 0.25× for all affected
- Pre-register synoptic regions (Northeast=NYC/BOS/PHL/DC, Midwest=CHI/DET/CLE/STL, etc.)

---

### 5. Kelly Over-betting — Portfolio-Level Miscalculation (HIGH)
**Severity:** HIGH — systematically 3–5× over-betting

**The problem:** f* = (p-c)/(1-c) gives f* = 0.664; half-Kelly = 0.332 = 33% of bankroll per trade. With 20 stations and moderate correlation:
- If ρ = 0.3 (conservative for same-country cities): effective independent bets N_eff = 20 / (1 + 19×0.3) ≈ 3.1
- Portfolio Kelly = 33% × 3.1 ≈ 103% of bankroll
- If ρ = 0.5: N_eff ≈ 1.95, portfolio Kelly ≈ 64%
- System over-bets by 3–5× minimum

**Detection method:**
- Compute true portfolio-level Kelly from station correlation matrix
- Simulate Monte Carlo with historical ρ values
- Compare actual position sizes to portfolio-optimal Kelly

**Proven mitigation:**
- Apply portfolio-level Kelly: `f*_port = f*_station / (1 + (N-1)ρ_avg)`
- Use 0.10× Kelly (one-tenth) until correlation matrix estimated from ≥200 trades
- Cap single-station position at 5% of bankroll
- Document correlation assumptions explicitly

---

### 6. Fee Model Formula is Wrong (HIGH)
**Severity:** HIGH — over-bets by ≤16× depending on market price

**The problem:** The formula `f* = (p-c)/(1-c)` does not reference market price. This is NOT standard Kelly for binary markets. Correct formula:

`f* = (b(p-c) - (q)) / (b(1-c))` where b = (1-price)/price

At price = 0.50 (even odds), b = 1: both formulas agree (~33%). But:
- At price = 0.65: standard f* = 2% of bankroll. Current formula = 33%. **16× over-bet.**
- At price = 0.40: standard f* = 59%. Current formula = 33%. **Under-bet.**
- At price = 0.55: standard f* = 23%. Current formula = 33%. Over-bet by 43%.

The formula is correct ONLY at exactly even odds. In real markets where prices vary, the error is systematic.

**Detection method:**
- Audit `market_cost_model.py` and `position_sizer.py` for the actual Kelly implementation
- Compare computed f* against standard formula across the full price range (0.05–0.95)
- Run simulation: does the f* output match expected f* at various prices?

**Proven mitigation:**
- Replace with standard Kelly: `f* = max(0, (b*(p-c) - q) / (b*(1-c)))`
- Document market price dependency explicitly in code comments
- Add unit tests that verify f* at price = 0.50, 0.65, 0.80 against known values
- Never hard-code price assumption in Kelly calculation

---

### 7. Small Sample Size — Accuracy Is Overstated (HIGH)
**Severity:** HIGH — ±10pp CI means system may have no edge

**The problem:** 85 trades over ~13 days = ~6.5 trades/day across 20 stations = ~4 trades/station. With ±10pp CI at 95%, true accuracy could be 57% or 77%. The 4.64:1 win/loss ratio is from 2 days (noted in framing doc). At p = 0.57 (CI lower bound), with market price = 0.50: edge = 0.07, Kelly f* = 0.14, half-Kelly = 0.07. The system bets 5× its true edge.

Additionally, 85 trades span ~2 calendar weeks — one weather regime. July/August summer pattern is not representative of fall/winter/spring.

**Detection method:**
- Bayesian posterior: Beta(α=wins+1, β=losses+1). Compute P(true_accuracy < 0.60)
- Rolling 20-trade accuracy: flag if >5pp below 67%
- Per-station accuracy: flag any station with < 3 trades (noisy)

**Proven mitigation:**
- Do not deploy live until ≥500 trades in shadow/paper mode
- Use Bayesian Kelly: `f*_Bayes = ∫f*(p)π(p|data)dp` — naturally shrinks for small samples
- At 85 trades: use posterior median (≈66.5%) with 0.25× Kelly (quarter-Kelly), not half-Kelly
- Impose 4-week paper-only calibration period before real capital

---

### 8. Autocorrelated Temperature Streaks (MED)
**Severity:** MED — inflates apparent accuracy by 5–10pp

**The problem:** Daily high temperatures exhibit strong autocorrelation. A 5-day heat wave = 5 consecutive "increase" predictions that are trivially correct. A cold front arrival = 3–4 "decrease" predictions also trivially correct. The system could be 70%+ accurate by correctly predicting persistence of a regime, with zero skill. Conditional accuracy on REGIME-CHANGE days (the only days that require skill) may be 50–55%.

**Detection method:**
- Compute accuracy conditioned on streak day: Day 1 of new regime (hard) vs Day 3+ (easy)
- Compare to persistence baseline: does 67.1% significantly exceed "today = yesterday"?
- Track regime-change accuracy separately

**Proven mitigation:**
- Score against persistence: if system can't beat "same as yesterday" by ≥5pp, it has no edge
- Reduce Kelly on streak days (Day 3+ of same direction) — less information value
- Only trade when GEFS ensemble fraction disagrees with persistence (predicts regime change)

---

### 9. Frontal Timing Error (HIGH)
**Severity:** HIGH — #1 source of temp forecast busts

**The problem:** A frontal passage shifts daily max temperature timing by 4–8 hours. GEFS predicts the front correctly but at wrong time:
- Cold front arrives 6h early → temp peaks early, then drops — daily HIGH capped below GEFS expectation
- Warm front arrives early → early warming, then post-frontal cooling starts sooner — daily HIGH lower than GEFS predicted
- The market prices in GEFS timing; actual timing differs → system trades against market that already priced the wrong timing

**Detection method:**
- Compare METAR hourly obs against GEFS-implied frontal timing (from 850mb theta-e gradient)
- Track "time of daily max" vs GEFS-predicted time — flag if >3h discrepancy
- Compute accuracy conditioned on frontal passage days vs quiescent days

**Proven mitigation:**
- Don't trade if GEFS ensemble spread for frontal timing > 4h
- Use 12Z GEFS exclusively (closest to daily max timing)
- For stations in active frontal zones (Midwest, Northeast): reduce position size 50% during active-front synoptic patterns
- Use independent frontal passage nowcast as cross-check

---

### 10. Seasonality — Winter vs Summer Regime Transition (HIGH)
**Severity:** HIGH — system has never been tested in winter

**The problem:** The 13-day backtest captures summer (strong diurnal, insolation-driven). Winter daily highs are fundamentally different:
- Nocturnal inversions: daily high may occur at midnight after cold front passage
- Snow cover: albedo suppresses daytime highs by 3–10°F
- Shorter day length: less solar heating, more sensitivity to advection
- 12Z GEFS in winter (7 AM EST) arrives while the sun is barely up
- GEFS accuracy for winter daily highs is typically 5–15pp lower than summer for mid-latitudes

**Detection method:**
- Backfill 90+ days of GEFS data covering December–February from historical archives
- Compute seasonal accuracy split
- Track weekly rolling accuracy and flag >8pp drift from calibration baseline

**Proven mitigation:**
- Hold out 30% of capital for seasonal adaptation
- Reduce Kelly multiplier to 0.10× during first winter month; let accuracy converge
- Build season-specific calibration: separate threshold matrices for DJF, MAM, JJA, SON
- Do NOT trade first 2 weeks of November (autumn-to-winter transition is the highest-error period)

---

### 11. GEFS Model Update / Distribution Shift (HIGH)
**Severity:** HIGH — happens without notice, system won't detect until money lost

**The problem:** NCEP updates GEFS sporadically (new physics, resolution, data assimilation). GEFS v12 went operational Sep 2020. A mid-cycle update or GEFS v13 would:
- Change ensemble spread characteristics
- Shift bias patterns
- Invalidate calibrated thresholds (e.g., "0.65 fraction → 80% P(Increase)")
- System has zero drift detection capability

**Detection method:**
- Monitor GEFS-vs-observation residuals daily: Kolmogorov-Smirnov test vs calibration period
- Track mean ensemble fraction — if shifts >0.03 from calibration mean, trigger alert
- Subscribe to NCEP model change announcements

**Proven mitigation:**
- Maintain sliding calibration window (last 90 days of GEFS-obs pairs)
- On detection (mean bias > 1°C or mean fraction shift > 0.04): freeze trading, re-run calibration on first 30 days of new data
- Weekly automated backtest: re-run accuracy on last 200 trades with current vs re-calibrated thresholds
- Subscribe to NCEP EMC mailing list for upgrade notices

---

### 12. METAR Station Outage / Stale Data (MED)
**Severity:** MED — well-understood, detection gap exists

**The problem:** ASOS stations go offline (power, network, maintenance). System may use stale obs for "yesterday's high" or current temperature trajectory. If station is offline 3–6h, all predictions that day are compromised.

**Detection method:**
- Monitor METAR observation age per station: flag if >90 minutes stale
- Cross-reference with neighboring stations (if KNYC stale, check KEWR)
- Per-station heartbeat metric

**Proven mitigation:**
- Do not trade station with METAR data > 2 hours stale
- Fall back to RTMA/URMA gridded analysis as substitute
- For yesterday's settled high: use CLI data (published next morning), not raw METAR — if CLI unavailable, skip that station

---

### 13. GEFS Missing Members / Feed Interruption (MED)
**Severity:** MED — degrades ensemble fraction reliability

**The problem:** GEFS produces 31 members + 1 control. If system receives only 15 members (partial feed, extraction failure), ensemble fraction becomes noisy. With 15 members, 95% CI for ensemble fraction widens from ±0.09 to ±0.13.

**Detection method:**
- Track received member count per forecast cycle
- Flag if < 25 members available

**Proven mitigation:**
- Require ≥ 25 of 31 members (80%) before using ensemble fraction
- If missing members, apply confidence penalty proportional to `sqrt(31/received)`
- Cache previous cycle's full ensemble as fallback
- Log member availability with each trade decision

---

### 14. NWP Feed Age / Stale GEFS (HIGH)
**Severity:** HIGH — using a 12-hour-old forecast cycle

**The problem:** GEFS runs at 00/06/12/18Z. Output available at +3–4h. If NWP feed is down, system may:
- Use 00Z GEFS (12+ hours old) for a 15Z trading decision
- Use 06Z when 12Z available
- Accuracy degrades by ~5–8pp per 6 hours

**Detection method:**
- Timestamp each GEFS file on arrival
- Track age of latest GEFS cycle in hours since model time
- Flag if using cycle > 6h old

**Proven mitigation:**
- Never trade on GEFS data older than 6 hours
- NWP feed watchdog: if 12Z GEFS not available by 16Z, skip day's trading
- Separate "data freshness" risk flag that kills all positions if triggered
- Implement feed heartbeat with alert to Dan

---

### 15. Intraday Reversal — Peak Temperature Timing (MED)
**Severity:** MED — systematic but subtle

**The problem:** Daily HIGH markets settle on the day's maximum temperature. If the max is reached early (14Z local) and then temperature declines, the position is fully settled from a pricing perspective. But if the system entered LONG and the max was reached, the price converges to 1.0 — the system should profit. The risk is if the system enters LONG, the temperature rises but NOT enough to exceed yesterday's high, then reverses.

More concretely: the system predicts "daily HIGH > yesterday's HIGH" based on GEFS 12Z. Temperature rises 3°F in the morning but stalls 1°F below yesterday's high due to unexpected cloud cover. The GEFS direction signal was correct (warmer today), but the MAGNITUDE was wrong by 2°F. With small threshold edges, magnitude error flips the trade.

**Detection method:**
- Track magnitude of predicted change vs actual change
- Compute accuracy conditioned on predicted change magnitude: trades with <3°F predicted change have lower accuracy
- Monitor "time to daily max" per station

**Proven mitigation:**
- Only trade when predicted temperature change magnitude > 3°F (not just direction)
- Exit positions by 19Z (2 PM EST) to avoid late-afternoon irrelevant volatility
- For low-magnitude predictions (< 3°F), reduce position size by 50%

---

### 16. Liquidity Trap — Bid-Ask Spread Widening (MED)
**Severity:** MED — eats edge; can prevent exit

**The problem:** KXHIGH markets for less popular stations may have extremely thin order books. During pre-close rush (last 30 min), bid-ask spreads widen to 20–50¢. The 2.05% fee model assumes 2.05¢ round-trip per $1 — a 20¢ spread is 10× modeled cost. With only 2–3 contracts at each price level, position exit may be impossible at any reasonable price.

The reported 4.64:1 win/loss assumes frictionless execution. In thin markets, winners become losers after spread + slippage.

**Detection method:**
- Real-time order-book depth monitoring per market
- Effective spread = (ask - bid) / mid_price
- Flag market if: effective spread > 15¢ OR depth < 10 contracts at best bid and best ask
- Track fill rate vs intended price

**Proven mitigation:**
- Minimum liquidity filter: only trade markets with spread < 10¢ AND depth ≥ 20 contracts
- Use limit orders only (never market orders)
- Phase in position over 5 minutes using TWAP
- For thin markets: place limit orders at mid-price - 2¢, accept partial fills
- Do not trade station with < $5,000 open interest at entry time

---

### 17. ECMWF Integration — Signal Degradation (MED)
**Severity:** MED — adding noise, not signal

**The problem:** GEFS and ECMWF are both NWP models with similar physics and data assimilation. Correlation between ensemble means at day 1–3 is ρ > 0.85–0.95 for mid-latitude surface temperature. Adding ECMWF to GEFS:
- Increases member count from 31 to 82 → reduces fraction variance by ~√(82/31) ≈ 1.63×
- BUT model error is shared — "independent error" assumption violated
- Combined signal has same bias as GEFS alone, just tighter error bars → false confidence
- At ρ = 0.90, the effective independent information gain is negligible

**Detection method:**
- Compute cross-model correlation: GEFS mean vs ECMWF mean per station per forecast hour
- Compute accuracy of combined signal vs GEFS-only vs ECMWF-only on out-of-sample test set
- If combined accuracy is not > 3pp better than GEFS alone, integration fails ROI

**Proven mitigation:**
- Use inverse-variance weighting: `w_GEFS = 1/σ²_GEFS`, `w_ECMWF = 1/σ²_ECMWF`
- Do NOT equal-weight or N-member-weight — overweights ECMWF
- Run shadow mode for 200 trades before live deployment
- If combined accuracy < GEFS-only + 2pp, drop ECMWF entirely

---

### 18. Entry Price — Adverse Selection / Winner's Curse (MED)
**Severity:** MED — systematic P&L drag

**The problem:** The system buys at ask, sells at bid → 2–10¢ slippage per entry. GEFS is public data → signal is shared by other traders → liquidity competition → adverse selection. System pays more for entries, receives less for exits.

**Detection method:**
- Track entry price vs VWAP in 5-minute window
- Track entry price vs mid-quote at signal time
- Compute slippage as % of expected P&L per trade

**Proven mitigation:**
- Scale entry via limit orders at mid-quote; accept partial fills
- Avoid entering in first 5 minutes after 12Z GEFS release — let market absorb data
- Model slippage explicitly: add 0.5–1.5¢ per $1 to effective fee in Kelly formula

---

### 19. Flash Crash / Anomalous METAR (LOW)
**Severity:** LOW — high-impact but very low probability

**The problem:** A single anomalous METAR reading (e.g., sensor reporting 110°F instead of 85°F) triggers temporary market move. System enters at bad price. Next METAR (1h later) corrects the error, but system is underwater.

**Detection method:**
- Outlier detection: flag single METAR reading > 3σ from trailing 3-hour mean
- Require confirmation from second source (RTMA, neighboring station)

**Proven mitigation:**
- Never act on single METAR — require two consecutive within 1°F
- Use RTMA gridded analysis as consistency check
- 5-minute execution delay for outlier filtering

---

### 20. Consecutive Loss Limit — Tracking Logic (MED)
**Severity:** MED — risk control may not function as intended

**The problem:** Consecutive loss limit (8) tracking logic matters enormously:
- If tracked per-station: limit provides no portfolio protection (each station loses only 1–2/day)
- If tracked portfolio-wide: a single bad synoptic day triggers halt mid-session (may be correct or incorrect)
- If counter resets on wins from ANY station (not just the losing station): counter never reaches 8
- The code was recently refactored — this logic may have a bug

**Detection method:**
- Review actual consecutive-loss tracking code in the refactored system
- Simulate worst-case day: how many trades at what cadence before limit triggers?

**Proven mitigation:**
- Track consecutive losses per-station (3 consecutive = pause that station)
- Portfolio-wide consecutive loss limit of 5 (tighter than 8) as hard stop
- On breach: reduce all position sizes by 50% for 24h, don't fully halt
- Test with explicit simulation scenarios

---

### 21. Daily Loss Limit Collision (MED)
**Severity:** MED — hard stop removes remaining profitable opportunities

**The problem:** Daily loss limit ($100) can be hit by 2 losing trades (at -$70 each). If this happens early, the system stops all stations. But remaining 18 stations might be profitable. The loss may be from a cascade limited to one region.

**Detection method:**
- Track time-of-day of daily loss limit hits
- Analyze whether remaining stations post-loss would have been profitable

**Proven mitigation:**
- Per-region loss limits ($40/region) + portfolio-wide ($100)
- Soft limit at $80: scale ALL positions to 0.5× (don't full stop)
- Hard limit at $100: full stop ONLY for the losing region

---

### 22. Cloud Cover / Solar Radiation Modifier (MED)
**Severity:** MED — silent accuracy degrader

**The problem:** GEFS predicts 2m temperature; cloud cover (sub-grid) strongly modulates surface shortwave radiation. If GEFS predicts clear but actual becomes overcast, daily high suppressed by 5–15°F. Near-threshold predictions (change of 1–3°F) get flipped by unmodeled cloud effects. Worst for convective-season stations (FL, Gulf Coast, Southeast).

**Detection method:**
- Compare GEFS cloud fraction with GOES satellite observations at 12Z and 18Z
- Track accuracy conditioned on GEFS-predicted vs satellite-observed cloud cover
- Compute accuracy delta on clear vs cloudy days

**Proven mitigation:**
- GOES override: if >60% satellite-observed cloud cover when GEFS predicted <30%, reduce position size by 50%
- SPC Day 1 convective override: if ≥15% probability, reduce size by 30%
- FL/Gulf stations: built-in 30% position size penalty during summer convective season

---

### 23. Multiple Comparison / Per-Station Threshold Mining (HIGH)
**Severity:** HIGH — thresholds optimized across 20 stations on tiny samples

**The problem:** ~4 trades per station. Per-station threshold tuning fits noise. A threshold yielding 75% accuracy on 4 Chicago trades is almost certainly spurious. Out-of-sample it reverts to ~55%. The 67.1% aggregate masks that individual stations range from 25–100%, with high performers dominating the average.

**Detection method:**
- Compute per-station accuracy with confidence intervals
- Flag any station with accuracy > 80% on < 10 trades
- Compute pooled (single threshold) vs per-station accuracy — if pooled > per-station, tuning is overfit

**Proven mitigation:**
- Use pooled threshold: one ensemble fraction threshold for ALL stations, calibrated on ALL trades
- If per-station thresholds are needed, use Bayesian hierarchical model sharing information across stations
- Minimum 30 trades per station before allowing per-station thresholds (this will take months)
- Apply shrinkage: `θ_station = λ × θ_global + (1-λ) × θ_raw` where λ = 30 / (30 + n_station)

---

### 24. Weekend / Holiday Gap (MED)
**Severity:** MED — predictable but unmodeled

**The problem:** Kalshi weekend/holiday markets may have different settlement procedures (e.g., Monday settlement for Saturday's daily high). NWS CLI reports for weekend days may be combined or delayed. Holiday staffing reduces NWS capacity → CLI delayed 24–48h.

**Detection method:**
- Calendar-based: identify weekends and US Federal holidays
- Monitor NWS CLI publication timestamp per station — flag if >36h delay
- Compare prediction day vs settlement day reference period

**Proven mitigation:**
- Do not trade daily-high for weekend days (Fri 18Z → Mon settlement gap too large)
- Skip trading on days preceding a Federal holiday
- For Monday markets: ensure weekend gap doesn't leave stale METAR data as reference
- CLI delay tracking: if CLI for yesterday hasn't published by 14Z today, don't trade today

---

### 25. Risk Control Code Defect from Refactor (HIGH)
**Severity:** HIGH — recently refactored, untested

**The problem:** The system was "fresh from Angular/Node rewrite." The DEV pipeline produced 30 trades (40% — wrong pipeline, noted as ignored). If risk control logic wasn't tested during refactor, documented limits may not match actual behavior. Common defects:
- Loss limit on gross P&L instead of net (fees excluded → limit triggers late)
- Consecutive loss counter resets on wins from DIFFERENT stations (never triggers)
- Drawdown computed as rolling window instead of peak-to-trough
- Kill switch not wired to actual trading API
- Circuit breaker tripped by test data in production

**Detection method:**
- Unit-test every risk control function with known inputs/outputs
- Inject scenario: station loses 10 consecutive trades → does kill switch fire?
- Inject stale GEFS data → does circuit breaker prevent trading?
- A/B test old system vs new system on same input data

**Proven mitigation:**
- Dedicated risk control test suite with 100+ scenarios
- Parallel shadow mode: risk-controlled system + duplicate without controls, both in simulation
- ALL risk control actions logged to separate append-only file
- Weekly formal verification of risk control configuration values
- 2-week parallel run before cutting over to refactored system alone

---

### 26. Multiple Time Zone Effect — West Coast Underperformance (MED)
**Severity:** MED — West Coast stations structurally disadvantaged

**The problem:** 20 stations span US time zones. 12Z GEFS = 7 AM ET, 6 AM CT, 5 AM MT, 4 AM PT. For West Coast stations:
- 12Z arrives at 4 AM local — temperatures at overnight minimum
- 8+ hours until daily max is reached
- Morning marine layer, afternoon sea breeze, coastal eddies — highly localized effects GEFS cannot resolve
- A lot can change between 4 AM and 3 PM that GEFS 12Z cannot capture

**Detection method:**
- Compute accuracy by time zone (Eastern, Central, Mountain, Pacific)
- Compute accuracy by local time of GEFS 12Z delivery
- Likely finding: Eastern stations have highest accuracy, Pacific stations lowest

**Proven mitigation:**
- Apply time zone penalty to Western stations: 0.7× Kelly for MT, 0.5× for PT
- Consider using 18Z GEFS (= 10 AM PST) for West Coast stations
- If accuracy on Pacific stations is >5pp below Eastern, remove them from trade set

---

## Top 3 Most Likely Failure Scenarios

Ranked by probability-weighted loss, with rationale.

### #1: Fee-Model Kelly Formula Error + Portfolio Correlation (Compound)
**Probability:** ~60% (near-certain that the formula error exists and correlation exists)
**Expected loss per 100 trades:** $500–$1,500

The fee-aware Kelly formula `f* = (p-c)/(1-c)` does not reference market price. At prices above 0.50, it over-bets systematically. Combined with portfolio-level correlation (ignored entirely), the system is betting 3–16× the Kelly-optimal amount. This will manifest as:
- Small wins followed by one large loss (the classic Kelly over-bet pattern)
- Over time, the drawdown limit (20%) will trigger from accumulated excess loss
- The 4.64:1 win/loss ratio will converge toward 1.5:1 or worse as over-betting drags

**Detection:** Compare computed position sizes against standard Kelly formula at current market prices. Compute portfolio-level Kelly with correlation matrix.

**Mitigation:** Fix Kelly formula immediately. Apply portfolio-level correlation adjustment. Use 0.10× Kelly initially.

---

### #2: Settlement Source Mismatch (NWS CLI vs METAR + Station Selection)
**Probability:** ~50% (at least several of 20 stations will be wrong)
**Expected loss:** $200–$800 before detection (approximately 10–30 trades)

The system is calibrated against METAR hourly observations. Kalshi settles on NWS Daily Climate Reports for specific ICAO stations. If even 3 of 20 stations have station-mapping errors, and the CLI vs METAR discrepancy is systematic at 0.5–2.0°F, the system is trading with:
- Wrong reference data (METAR vs CLI)
- Potentially wrong reference station (city vs ICAO)
- Combined error of 1–3°F → direction prediction accuracy drops below 55%

This is especially dangerous because the system will appear to be working (some stations win, it's just "noise") while slowly leaking money. By the time the pattern is detectable, 20–30 trades have been lost.

**Detection:** Audit the station mapping for all 20 markets against Kalshi API. Re-run backtest against NWS CLI data. Compare first 10 live settlements to model predictions.

**Mitigation:** Fix all station mappings. Add 1.5°F decision buffer. Recalibrate against CLI data. Cross-validate against first 10 live settlement outcomes.

---

### #3: Regime Break + Correlation Cascade (Compound)
**Probability:** ~15% in first 30 trading days (probability increases with time)
**Expected loss:** $500–$1,000 (full daily risk limit hit within hours, plus psychological impact)

The system has been tested in ~13 summer days of one weath
The system has been tested in ~13 summer days of one weather regime. A transition to fall (September–October) brings:
- Jet stream strengthening → more frontal passages → correlation cascade risk increases 3×
- Mixed convective/convective-to-stratiform precipitation → stochastic cloud cover
- Hurricane season tail risk (especially for Southeast stations: ATL, CLT, MIA, TPA, HOU)

A correlation cascade event (cold front affecting 10+ stations simultaneously) combined with Kelly over-betting means:
- 10 simultaneous losses at -$70 average = -$700 portfolio loss
- Daily $100 loss limit hit within 30 minutes of market open
- System goes into risk-control lockdown for the rest of the day
- After lockdown, the weather system passes and the remaining stations (which would have won) are not traded

**Detection:** Monitor synoptic-scale GEFS ensemble spread across all stations. Compute real-time portfolio correlation. Track daily loss limit breaches by time-of-day.

**Mitigation:** Region-level position caps (max 3 per region), region-level loss limits ($40), correlated-Kelly sizing, synoptic override when >5 stations share same controlling system. Do NOT use a single portfolio-wide hard stop — use graduated response (reduce 50% → soft stop region → hard stop only if multi-region).

---

## Summary Table: All 26 Failure Modes

| # | Failure Mode | Severity | Detection | Mitigation Priority |
|---|---|---|---|---|
| 1 | Settlement station mismatch | HIGH | Station map audit | FIX BEFORE LIVE |
| 2 | NWS CLI vs METAR discrepancy | HIGH | Backtest vs CLI data | FIX BEFORE LIVE |
| 3 | DST settlement quirk | MED | Calendar check | Code fix |
| 4 | Correlation cascade | HIGH | Region correlation tracker | FIX BEFORE LIVE |
| 5 | Portfolio Kelly over-bet | HIGH | Effective N computation | FIX BEFORE LIVE |
| 6 | Fee model formula wrong | HIGH | Kelly formula audit | FIX IMMEDIATELY |
| 7 | Small sample size | HIGH | Bayesian posterior | 0.25× Kelly, more data |
| 8 | Autocorrelation streaks | MED | Persistence baseline check | Regime-change scoring |
| 9 | Frontal timing error | HIGH | 850mb theta-e tracking | Position reduction |
| 10 | Seasonal regime change | HIGH | Rolling accuracy monitor | Season-specific thresholds |
| 11 | GEFS model update | HIGH | KS test on residuals | Sliding calibration window |
| 12 | METAR station outage | MED | Observation age tracker | Feed fallback |
| 13 | GEFS missing members | MED | Member count monitor | ≥25 member requirement |
| 14 | NWP feed age | HIGH | Timestamp freshness | Skip day if >6h old |
| 15 | Intraday peak timing | MED | Magnitude threshold | Only trade Δ > 3°F |
| 16 | Liquidity trap | MED | Spread + depth monitor | Market filter |
| 17 | ECMWF signal degradation | MED | Cross-model correlation | Weighted blending |
| 18 | Adverse selection | MED | Slippage tracking | Limit orders, delay entry |
| 19 | Flash crash / anomalous METAR | LOW | 3σ outlier detection | Two-reading confirmation |
| 20 | Loss limit tracking logic | MED | Code review, simulation | Per-station + portfolio limits |
| 21 | Daily loss limit collision | MED | Time-of-day analysis | Graduated, region-based |
| 22 | Cloud cover modifier | MED | GOES satellite comparison | Satellite override |
| 23 | Per-station threshold mining | HIGH | Station accuracy CI | Pooled thresholds |
| 24 | Weekend/holiday gap | MED | Calendar awareness | Skip gap days |
| 25 | Risk control code defect | HIGH | Unit test suite | Shadow mode parallel run |
| 26 | West Coast time zone | MED | Accuracy by time zone | 0.5× Kelly for PT stations |

---

## Immediate Actions Required Before Going Live

1. **Fix the Kelly formula** — replace `f* = (p-c)/(1-c)` with standard formula that accounts for market price
2. **Fix the Kelly formula again** — add portfolio-level correlation adjustment
3. **Audit station mappings** — verify all 20 KXHIGH markets map to the correct ICAO station
4. **Recalibrate against CLI** — re-run backtest against NWS Daily Climate Report data, not METAR
5. **Add region-based risk controls** — position caps, loss limits, correlation tracking
6. **Implement sliding calibration** — detect GEFS distribution shifts automatically
7. **Apply Bayesian Kelly** — account for the ±10pp CI from 85-trade sample
8. **Add West Coast penalty** — or remove PT stations from the trade set

## Watch Items (Non-Blocking but Important)

- Track per-station accuracy with CIs — flag any station with <10 trades after 60 trading days
- Monitor Kelly fraction vs actual P&L — if P&L variance exceeds Kelly-predicted variance, correlation is higher than modeled
- Log all NWS CLI settlement comparisons vs model prediction for the first 50 live trades
- Seasonal accuracy audit at each solstice/equinox transition
