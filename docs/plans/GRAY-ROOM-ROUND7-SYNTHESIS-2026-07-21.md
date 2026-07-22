# Gray Room Round 7 — Short-Duration Trading: Synthesis

**Date:** 2026-07-21
**Panel:** 3 experts — Short-Range Meteorologist, Market Microstructure, Signal Fusion
**Context:** Shift from daily (next-day) HIGH/LOW predictions to minutes-to-hours trading

## Why This Round

The principal stated: "The trades are going to be pretty short term and short notice. I'm not expecting to buy the day before and hold overnight... not by default."

This is a fundamental shift from the existing architecture. The current system predicts next-day settlement direction at 72.3% accuracy. Short-duration trading requires different signals, different execution, and different data sources.

---

## Expert 1: Meteorologist — Short-Range Signals

### 1. HRRR Bias-Corrected Temperature (Highest Priority)
- **Signal:** Bias-correct HRRR hourly temperature forecasts using latest METAR observation
- **Formula:** `T_forecast(h) = T_hrrr(t+h) + [T_metar(t) - T_hrrr(t)]`
- **Expected accuracy:** 80-85% at 1-3h, 70-75% at 4-6h
- **Effort:** Medium (2-3 weeks to build HRRR collection pipeline)
- **Risk:** HRRR cold bias in first 1-2 hours (spin-up) — weight METAR persistence more heavily initially

### 2. Dewpoint Trend as Moisture Regime Filter
- **Signal:** Rising dewpoint (>+2°F/3h) → cloud formation → suppress heating → apply -1 to -2°F correction
- **Falling dewpoint** (>-3°F/3h) → dry advection → enhance heating → apply +1°F correction
- **Expected lift:** +2-3pp on top of bias-corrected HRRR
- **Risk:** Coastal false positives from sea breeze

### 3. Wind Shift / Frontal Passage Detection
- **Signal:** |ΔWD_hrrr(3h)| > 45° flags front or sea breeze
- **Pressure drop (>3hPa/3h) + wind shift** → 75% chance of frontal passage within 6h
- **Sequence:** Pressure drop → wind shift → temperature change → lag 1-2h
- **Expected lift:** +2-5pp on short-term directional prediction when a front is expected

### 4. Ensemble Spread Trajectory
- **Signal:** Widening HGEFS spread at hour 2 predicts reversion/error by hour 6
- **Strongest signal:** Spread > 1.5× climatological normal → 60% chance of next 6h being wrong
- **Use case:** Active stop-loss, not entry signal

### 5. Goldilocks Intraday Arbitrage
- **Verdict:** VIABLE but rare. METAR temperature spikes (single-tick jumps of 1-2°C) do occur, typically from:
  - Solar radiation surge after cloud break (15-30 minutes after cloud clears)
  - Cold frontal passage with temperature drop
  - Sea breeze onset
- **Expected frequency:** ~5-15 events per city per year
- **Not a primary signal** — treat as bonus alert, not core strategy

---

## Expert 2: Market Microstructure — Entry/Exit Architecture

### 1. Tiered Confirmation Entry (ADVANCE)
- **Stage 1 (50%):** Enter at signal fire when ensemble spread < 2.5°C IQR
- **Stage 2 (50%):** Confirmation from first 2 METAR obs showing trajectory alignment
- **Window:** 6h to fill Stage 2 or cancel
- **Expected impact:** +0.15 to +0.25 Sharpe over all-in-at-open

### 2. Time-Decay + Confidence Decay Dual Exit (ADVANCE)
- **Time decay:** Reduce position at T-6h, T-3h, close at T-1h regardless of P&L
- **Confidence decay:** Close 50% if spread widens >40% from entry, 100% if >75%
- **Hard stop:** -40% of initial margin (circuit breaker)
- **Profit take:** 50% at +8¢ with >4h remaining
- **Expected impact:** Reduces holding time from ~18h to ~8-10h, +0.2 to +0.3 Sharpe

### 3. Entry Timing (ADVANCE)
- **Earliest:** Midnight ET (new market day) — but only if signal is fresh (< 12h old)
- **Optimal:** 6 AM local time + METAR confirmation of trajectory
- **Latest:** 2h before day's first METAR observations for the target city
- **Rule:** Never enter a position that will hold >80% of the day's remaining time

### 4. Liquidity Tiering (ADVANCE)
- **Tier 1 (>= 500 contracts volume):** Top 5 cities (NYC, LA, Chicago, Dallas, Miami) — full position size
- **Tier 2 (100-500):** Mid-tier — 50% position size
- **Tier 3 (< 100):** Thin markets — skip or 10% max
- Check Kalshi order book depth at entry time, not just volume stats

### 5. Partially-Confirmed Position (PARK)
- If Stage 2 never triggers, expert recommends holding 50% position
- **Risk:** This is a half-position on a signal that hasn't been ground-truthed
- **Recommendation:** For initial deployment, force full exit if Stage 2 misses — gather data on whether partial positions are worth it

---

## Expert 3: Signal Fusion — Sub-Daily Signals

### 1. Ensemble Spread Divergence Rate (ESDR) (ADVANCE)
- **Signal:** HGEFS spread + divergence rate as confidence modulator
- **Formula:** `spread_ratio = spread_t / normal_spread`; `divergence_rate = (spread_t - spread_{t-3}) / 3`
- **Output:** HIGH_CONFIDENCE (1.15×), NEUTRAL (1.0×), LOW_CONFIDENCE (0.75×)
- **Not a directional signal** — modulates position sizing
- **Effort:** Medium (hourly HGEFS fetch + 30d climatology + fusion hook)

### 2. Forecast-Observation Gap Reversion (FOGR) (ADVANCE)
- **Signal:** Temperature gap vs. forecast, adjusted for time of day and diurnal curve
- **Late day (>70% elapsed) + below forecast high** → HIGH likely achieved/exceeded
- **Early day (<30% elapsed) + meeting forecast high** → ceiling may be lower than expected
- **Expected lift:** +2-4pp on intraday confidence
- **Effort:** Small (existing METAR data + diurnal curve lookup)

### 3. METAR dT/dt Trend Signal (ADVANCE)
- **Signal:** Rate of temperature change over 1h, 3h, 6h windows
- **Positive dT/dt at 10 AM** → predicting high will exceed climatological median
- **Negative dT/dt at 10 AM** → high may be suppressed
- **Expected lift:** +3-5pp on intraday directional prediction
- **Best in summer** when diurnal heating is strong; weak in winter

### 4. Pressure Tendency Leading Indicator (ADVANCE)
- **Signal:** Pressure drop > 3hPa over 3 hours → leading indicator for frontal passage
- **Once confirmed by wind shift:** trade the temperature break
- **Expected lift:** +1-3pp at 3-6h horizon
- **Effort:** Small (METAR already has pressure data)

### 5. Confidence Trajectory Score (ADVANCE)
- **Formula:** `confidence = α × (1 - spread_ratio) + β × dT/dt_agreement + γ × time_remaining_fraction`
- **α=0.5, β=0.3, γ=0.2** (weights, tunable)
- **Updates hourly** — used for active position management
- **Expected lift:** +0.1 to +0.15 Sharpe

### 6. Sequential Trigger Architecture (ADVANCE)
- Sub-daily trading should NOT use majority-vote ensemble (daily design)
- Use **sequential triggers**: Enter on Signal A, confirm with B, trail with C, exit on D
- Each signal operates at its own temporal resolution
- **Expected lift:** +0.2-0.3 Sharpe over naive ensemble

### 7. Alert-Driven Design (ADVANCE)
- **Alerts:** "Temperature crossed X% of forecast high with Y% of day remaining"
- **Thresholds:** 80% of forecast high achieved by 60% of day elapsed → likely ceiling
- **Reversal alert:** "Temperature dropped 2°C in 1 hour against forecast" → cold front or cloud cover
- **Bias alert:** "Running 3°C above forecast for 3 consecutive hours" → model bias, not random noise

---

## Dispositions

| Item | Disposition | Next Action |
|---|---|---|
| HRRR collection | **ADVANCE** | Build collection pipeline (scope doc exists) |
| HGEFS hourly ensemble (rolling 4wk) | **ADVANCE** | Modify ensemble_collector for hourly + retention |
| ESDR confidence modulator | **ADVANCE** | Build after HGEFS hourly data |
| FOGR reversion signal | **ADVANCE** | Build with existing METAR data |
| METAR dT/dt trend | **ADVANCE** | Build with existing METAR data |
| Pressure tendency | **ADVANCE** | Build with existing METAR data |
| Tiered entry/exit | **ADVANCE** | Build after real-money deployment |
| Time-decay exit | **ADVANCE** | Build after real-money deployment |
| Liquidity tiering | **ADVANCE** | Build after real-money deployment |
| Sequential trigger architecture | **ADVANCE** | Phase 2 after single-signal validation |
| Confidence trajectory score | **ADVANCE** | Phase 2 (depends on ESDR, dT/dt, FOGR) |
| Alert-driven triggers | **ADVANCE** | Phase 2 (depends on signals) |
| Goldilocks intraday arb | **PARK** | Rare events (~5-15/year/city), not primary |
| Partially-confirmed positions | **PARK** | Gather data on partial position value first |

## Build Order

**Phase A (current sprint — existing data, no HRRR needed):**
1. FOGR reversion signal (METAR + diurnal curve)
2. METAR dT/dt trend signal (METAR, 1h/3h/6h windows)
3. Pressure tendency leading indicator (METAR pressure)

**Phase B (next sprint — HRRR pipeline):**
4. HRRR collection + bias correction
5. HRRR dewpoint moisture filter
6. HRRR wind shift detection

**Phase C (ensemble expansion):**
7. Modify ensemble_collector.py for hourly HGEFS + 4-week rolling retention
8. ESDR confidence modulator
9. Confidence trajectory score

**Phase D (deployment):**
10. Tiered entry/exit rules
11. Time-decay exit
12. Liquidity tiering
13. Sequential trigger architecture
14. Alert-driven triggers

---

*Synthesis compiled 2026-07-21 21:05 UTC from 3 expert analyses*