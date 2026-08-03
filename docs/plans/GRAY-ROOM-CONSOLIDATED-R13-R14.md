# Gray Room Rounds 13-14 — Consolidated Output

**Date:** 2026-08-02
**Rounds:** 13 V1 (DeepSeek V4 Flash), 13 V2 (GLM 5.2 / Luna Pro / GPT 5.4 / DeepSeek V3.1), 14 (GLM 5.2 / Luna Pro / GPT 5.4 / DeepSeek V3.1)
**Format:** All findings in tables by type. Every item has disposition.

---

## ERRORS (broken, needs fix)

| # | Finding | Expert(s) | Severity | Impact | Fix | Disp |
|---|---------|-----------|----------|--------|-----|------|
| **E1** | **Circular market price** — `market_price = 0.5 + confidence * 0.4`. Price derived from signal, not from Kalshi. Edge is a fixed function of confidence. Real market inefficiencies invisible. At conf=0.867, edge=0.02 exactly. | GPT 5.4 | 🔴 HIGH | Entire edge computation is circular. System blind to mispriced markets. | Replace with live Kalshi API price feed. | ADVANCE |
| **E2** | **No ensemble fraction** — 31-member GEFS loaded but only `mean_c` used. `ensemble_min/max/n_members` discarded. "67.1% GEFS ensemble fraction" is actually ensemble-mean-threshold model. Docs and code don't match. | GPT 5.4 | 🔴 HIGH | Spread information unused. System can't distinguish high-confidence (30/31 agree) from low-confidence (16/15 split) forecasts. | Implement member-level voting or correct docs to match reality. | ADVANCE |
| **E3** | **Entry price hardcoded to 0.85** — All 85 paper trades computed P&L at 0.85 entry. Real Kalshi prices are 0.40-0.65. Every P&L number is wrong. | GPT 5.4, DeepSeek V3.1 | 🔴 HIGH | P&L numbers untrustworthy. Kelly sizing wrong. | Replace with Kalshi API price feed. Fall back to 0.50. | ADVANCE |
| **E4** | **Kelly formula ignored market price** — `f* = (p-c)/(1-c)` didn't reference market price. At p=0.67, M=0.65: over-bet 33×. At M=0.50: over-bet 3.8×. | GPT 5.4 | 🔴 HIGH | Systematic position sizing error. Would wipe out bankroll in 30 live trades. | ✅ FIXED this session. Formula now `f* = edge/(1-c)` where `edge = abs(p - M)`. | ✅ DONE |
| **E5** | **Confidence formula uncalibrated** — `raw_conf = 0.5 + temp_diff / 20.0`. Pure heuristic. 20.0 denominator is arbitrary. Station-agnostic. No weather-regime adjustment. No empirical basis. | GPT 5.4, DeepSeek V3.1 | 🟡 HIGH | Drives both trade gate AND market price (circularly). Edge computed from uncalibrated heuristic. | Compute per-station confidence calibration: empirical win rate per temp_diff bin. Replace heuristic with calibrated curve. | ADVANCE |
| **E6** | **Fee docstring contradicts code** — Docstring: "$0.01/contract/side flat." Code: `ceil(0.07 * contracts * price * (1-price)) * 2`. 10× difference on small trades. | GPT 5.4 | 🟡 MED | P&L may be wrong for small trades. Stale docstring creates confusion. | Test against live Kalshi trade. Fix whichever is wrong. Update docstring. | ADVANCE |
| **E7** | **Fee stair-step discontinuity** — `ceil()` creates 100% fee jumps at tier boundaries (113 contracts=$2, 114 contracts=$4). | GPT 5.4 | 🟢 LOW | 1-2% P&L drift at tier boundaries. | Track fee-per-contract as average, not absolute value per tier. | PARK |
| **E8** | **Entry price 0.85 hardcoded in phase1 cron** — Line 325, never overridden. | DeepSeek V3.1 | 🟡 MED | All historical P&L computed at wrong price. | Wire Kalshi API price fetch. Fall back to 0.50. | ADVANCE |
| **E9** | **No live Kalshi price feed anywhere** — Both cron pipelines use synthetic or hardcoded prices. No system has ever read a real Kalshi market price. | GPT 5.4, DeepSeek V3.1 | 🔴 HIGH | Every trade decision, sizing, and P&L calculation uses fabricated prices. | Build Kalshi price fetcher. Wire into cron pipeline. | ADVANCE |

---

## IDEAS (unproven, may add value)

| # | Idea | Expert(s) | Potential Impact | Test / Spec | Disp |
|----|------|-----------|-----------------|-------------|------|
| **I1** | **Ensemble fraction as actual signal** — Compute `fraction_up = count(members > prev_temp) / 31` per station. Calibrate: does 0.67 fraction = 67% wins? Compare to mean-threshold model. | GPT 5.4 | +2-5pp if ensemble spread is predictive | Run both in parallel for 30 days. Compare accuracy and Brier score per station. | ADVANCE |
| **I2** | **Epoch-based Kelly schedule** — Kelly multiplier varies by GEFS cycle: 0.70× (00Z), 0.85× (06Z), 1.00× (12Z), 0.55× (18Z). Different confidence thresholds per cycle. | GLM 5.2, Luna Pro | +5-15% risk-adjusted P&L | GLM 5.2 provided complete JSON config. Wire into PHASE1_CONFIG. Shadow-run for 30 days before activating. | ADVANCE |
| **I3** | **Portfolio correlation reduction** — 20 stations with ρ~0.3-0.5. True portfolio Kelly = individual Kelly × 1/(1+(n-1)ρ) ≈ 0.116×. Apply 3× safety headroom → 0.35×. | GLM 5.2, GPT 5.4 | Prevents 3-5× portfolio over-bet | Compute ρ from 2+ years of station data. Apply as multiplier to position size. | ADVANCE |
| **I4** | **Per-station bias correction** — KNYC under-predicted by 4.6°F (urban heat island). GEFS 0.5° grid doesn't resolve city microclimates. Compute per-station monthly bias: mean(GEFS - actual). | DeepSeek V3.1, GPT 5.4 | +2-5pp on biased stations | Compute from 30 days settlement data. Apply to GEFS forecast before computing signal. | ADVANCE |
| **I5** | **GEFS urban heat island correction** — Urban stations (KNYC, KLAX, KPHX, KDFW, KHOU) have systematic warm bias in summer. GEFS grid average = cooler than city ASOS. | DeepSeek V3.1 | +1-3pp on urban stations | Per-station summer correction factor from historical GEFS-vs-actual. | ADVANCE |
| **I6** | **ECMWF shadow test** — 51-member ECMWF at 85% backfill completion. Expected ρ~0.7 with GEFS (both NWP). Equal-weight fusion adds noise. | Multiple | +1-3pp if decorrelated | Run ECMWF fraction as shadow for 30 days. Wire only if adds >2pp over GEFS-alone. | PARK |
| **I7** | **Regime-based confidence modulation** — Summer accuracy ≠ winter accuracy. Seasonal regime detection (frontal vs stagnant) could adjust confidence thresholds per regime. | Expert 1 (Round 13) | +1-2pp | Compute per-season calibration curves. Apply regime-specific thresholds. | PARK |
| **I8** | **Spatial coherence as actual signal** — 6-region consensus currently unwired from cron. If KNYC, KBOS, KPHL all agree on direction, confidence should increase. | Luna Pro (Round 13 V2) | +0.5-1pp | Wire into cron as confidence modulator only. Shadow-run for 30 days. | PARK |
| **I9** | **Dewpoint ceiling cap** — Morning dewpoint predicts afternoon high ceiling. On high-dewpoint days (>65°F), daily HIGH is capped. GEFS may over-predict. | Expert 1 (Round 13) | +1-2pp in humid summer | Add dewpoint cap to GEFS forecast: `max_temp = min(GEFS, 86 + (dp - 65) * 0.5)`. | PARK |

---

## IMPROVEMENTS / SPECS (ready to build)

| # | Spec | Expert(s) | Detail | Effort | Disp |
|----|------|-----------|--------|--------|------|
| **S1** | **Corrected Kelly config** | GLM 5.2 | Complete JSON config. Key values: Kelly f*=0.1746 (corrected), portfolio factor=0.20×, max position=$175 (1.75%), entry bounds [0.25,0.75], epoch multipliers 0.70/0.85/1.00/0.55. Full block in `GRAY-ROOM-ROUND14-SYNTHESIS.md`. | 2h | ADVANCE |
| **S2** | **Three-phase rollout plan** | Luna Pro | **Phase 1 (BLOCKING):** Fix circular price, fix fee model, wire live prices — 8h. **Phase 2:** Epoch Kelly, portfolio correlation, station bias — 16h. **Phase 3:** Dashboard, stdout — 4h. | 28h total | ADVANCE |
| **S3** | **Merge sequence** | DeepSeek V3.1 | **PR1:** Delete dead code — 1h. **PR2:** Core fixes (Kelly, circular price, fee docstring) — 4h. **PR3:** Circuit breaker, GRIB purge, epoch config — 3h. **PR4:** Live price feed — 4h. Each PR independently deployable, rollback-safe. Each verified: accuracy stays ≥67%. | 12h total | ADVANCE |
| **S4** | **Go/No-Go criteria** | Luna Pro | Before real money: (1) 30 days paper, (2) ≥60% accuracy on full 30 days, (3) ≥0.30 Sharpe, (4) No single-day drawdown >10%, (5) Settlement-confirmed accuracy. Currently: **none met.** | — | PARK |
| **S5** | **Station tradability audit** | GPT 5.4 | KMSP: 0/2, -$3,216. KCLT: 5/10, -$4,140. Some stations may be net negative. Audit per-station P&L across all 85 trades. Remove or reduce sizing for unprofitable stations. | 1h | ADVANCE |
| **S6** | **Fee model verification** | GPT 5.4 | Place 1 live Kalshi trade ($1), record actual fee. Compare to `ceil(0.07 * contracts * price * (1-price)) * 2`. If mismatch, fix code or update docstring. | 0.5h | ADVANCE |
| **S7** | **Calibration curve per station** | GLM 5.2, GPT 5.4 | Replace heuristic `raw_conf = 0.5 + temp_diff / 20.0` with empirical calibration: bin temp_diff by station, compute actual win rate per bin, use as confidence. | 4h | ADVANCE |
| **S8** | **Accuracy CI reporting** | GPT 5.4 | 67.1% on 85 trades = 95% CI [56.7%, 76.4%]. Best case: 76%. Worst case: 57%. Report confidence intervals with every accuracy number. | 0.5h | ADVANCE |

---

## ALREADY FIXED THIS SESSION

| # | What | When | Status |
|---|------|------|--------|
| ✅ | Kelly formula: `f* = (p-c)/(1-c)` → `f* = edge/(1-c)` with market price | 20:07 UTC | position_sizer.py updated, cron passes market_price |
| ✅ | 3 dead Kelly sizers deleted | 05:53 UTC | kelly_position_sizer.py, fee_aware_kelly, variance_weighted all gone |
| ✅ | 8 hardcoded fees → centralized ROUND_TRIP_FEE | 06:00 UTC | instance_config.py, instance_config_fixed.py, paper_trading_engine.py |
| ✅ | 85 print() → structured logging | 06:10 UTC | paper_trading_engine.py — 0 print() remaining |
| ✅ | Circuit breaker wired: Kalshi/NWS/Open-Meteo | 06:15 UTC | kalshi_monitor.py, data_collector.py |
| ✅ | Backup process trap-fixed | 08:25 UTC | backup-openclaw-native.sh cleans /tmp/openclaw-backup-*/ on exit |
| ✅ | GRIB purge-on-parse | 18:30 UTC | ecds_tigge_backfill.py deletes GRIB after DB storage |
| ✅ | 126GB orphaned backup dirs cleaned | 08:15 UTC | /tmp/openclaw-backup-* × 21 dirs |
| ✅ | Old dashboard.py deleted | 08:05 UTC | 760L deprecated Flask module |
| ✅ | Docs updated to intraday framing | 09:05 UTC | Master roadmap + A-Mode runbook |
| ✅ | Station mapping verified | 19:45 UTC | All 20 stations match Kalshi settlement DB |

---

## CLEANUP STATUS

| Category | Total | ADVANCE | PARK | ✅ DONE |
|----------|-------|---------|------|---------|
| **Errors** | 9 | 7 | 1 | 1 (E4) |
| **Ideas** | 9 | 5 | 4 | 0 |
| **Improvements** | 8 | 7 | 1 | 0 |
| **Total** | **26** | **19** | **6** | **1** |

---

## WHAT TO DO NEXT

| Order | Item | Effort | Type | Depends On |
|-------|------|--------|------|------------|
| **1** | PR2 merge (core fixes: Kelly, circular price, fee) | 4h | Implementation | None |
| **2** | PR4 merge (live Kalshi price feed) | 4h | Implementation | PR2 |
| **3** | Station tradability audit + fee verification | 1.5h | Spec execution | None |
| **4** | PR1 merge (delete dead code) | 1h | Cleanup | None |
| **5** | Calibration curve per station | 4h | Implementation | PR2 |
| **6** | PR3 merge (epoch config, circuit breaker, GRIB) | 3h | Implementation | PR2 |
| **7** | Per-station bias correction | 4h | Implementation | PR2 + Calibration |
| **8** | Portfolio correlation matrix | 12h | Implementation | PR2 |
| **9** | ECMWF shadow test | 4h | Testing | Backfill complete |
| **10** | 30-day paper test + real-money go decision | 30d | Validation | PR2-PR4 |
