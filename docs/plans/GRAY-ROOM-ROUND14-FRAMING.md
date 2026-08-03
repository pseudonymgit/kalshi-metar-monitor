# Gray Room Round 14 — SOP: Intraday Architecture & Production Readiness

**Date:** 2026-08-02 20:25 UTC
**Status:** Full SOP protocol — each expert receives the same base packet, answers their specific question, and every output item gets ADVANCE/PARK/KILL.

---

## Base Packet (Same for All Experts)

### System State (as of this session)

**What works:**
- GEFS ensemble fraction predicting daily HIGH direction: **67.1% accuracy** (85 trades, 13 days, +$8,809 P&L)
- Fee-aware Kelly sizer — **just fixed**: formula now `f* = edge / (1-c)` where `edge = abs(win_rate - market_price)`. Previously was `f* = (p-c)/(1-c)` which ignored market price, causing 3-33× over-bet.
- ECMWF backfill: 85% complete (589 dates in DB, ~1,008 remaining, ~6 days)
- 20 stations, all verified against Kalshi settlement data
- Circuit breaker wired: Kalshi/NWS/Open-Meteo APIs (5 failures, 30s recovery)
- Risk controls: daily loss $100, drawdown 20%, consecutive losses 8, kill switches

**What's been cleaned this session:**
- 3 dead Kelly sizers deleted → 1 canonical (`position_sizer.py`)
- 8 hardcoded fees → centralized `market_cost_model.ROUND_TRIP_FEE`
- 85 print() → structured logging
- 126GB orphaned backup dirs cleaned, backup process trap-fixed
- GRIB purge-on-parse added to backfill script
- Old `dashboard.py` deleted
- Master roadmap + A-Mode runbook → intraday framing

**What's still wired but questionable:**
- `paper_trading_engine.py` (3,397L) — unused by working GEFS cron
- `adaptive_thresholds.py` (641L) — Bayesian per-signal thresholds, unwired from cron
- `spatial_coherence.py` (539L) — regional confidence modulation, unwired from cron
- `radiational_cooling.py` (452L) — unwired, approved for LOW-only
- `api_circuit_breaker.py` (419L) — wired, good
- `risk_controls.py` (559L) — used by old engine, not by GEFS cron

**What's wrong and needs fixing:**
- Entry price hardcoded to 0.85 in cron (line 325 of `phase1_paper_trading_cron.py`)
- No live market price pull from Kalshi API
- No portfolio correlation in position sizing (20 stations with ρ~0.3-0.5)
- GEFS urban heat island bias (KNYC: 4.6°F under-prediction on July 26)
- Epoch-based Kelly schedule not implemented (00/06/12/18Z multipliers)

**What's missing:**
- Stdout/observability for the cron pipeline (trades land in DB but no dashboard feed)
- Prod paper trading DB schema mismatch with DEV (prod stale 8+ days)
- Per-station calibration curves from ECMWF data when backfill completes

| Metric | Value |
|--------|-------|
| GEFS accuracy | 67.1% (85 trades, 13 days) |
| P&L | +$8,809 (paper) |
| Fee-aware Kelly | f* = edge / (1-c), c=0.0205 |
| Entry price | Hardcoded 0.85 (should be Kalshi API) |
| Bankroll | $10,000 |
| Max position | 8% ($800) |
| Active stations | 20 |
| ECMWF | 85% complete |

---

## Expert Assignments

### Expert A — Model: GLM 5.2
**Role:** Numerical Implementation Specialist
**Question:** Given the corrected Kelly formula `f* = edge/(1-c)` and the epoch-based schedule, what are the exact config values for every parameter in the GEFS cron pipeline? Provide as a JSON config block that can be dropped into `PHASE1_CONFIG`. Include: confidence thresholds per GEFS cycle, Kelly epoch multipliers, spread limits, entry price bounds, minimum trade edge, bankroll cap, drawdown limits, and the stationary correlation matrix approximating ρ~0.3-0.5 across 20 stations. Every parameter must have a numerical justification.

### Expert B — Model: Luna Pro (reasoning mode)
**Role:** Production Readiness Auditor
**Question:** The GEFS cron pipeline is 245 lines and produces 67.1% accuracy. It has no stdout output, no dashboard feed, no live price feed, no portfolio correlation, and no epoch-based sizing. What's the minimum set of changes needed to make it production-ready for a $10,000 paper account trading 20 stations daily? Rank each change by: safety impact (1-10), P&L impact (1-10), and effort (hours). Identify which changes are blocking vs. nice-to-have. Provide a three-phase rollout plan where each phase is self-contained and deployable independently.

### Expert C — Model: GPT 5.4 (Codex)
**Role:** Edge Case & Contingency Analyst
**Question:** The system just fixed a critical Kelly formula error that was causing 3-33× over-betting. What else is hiding in the codebase that could cause similar magnitude errors? Audit the assumptions: the GEFS cron uses GEFS ensemble fraction as probability → is this actually calibrated? The confidence formula `raw_conf = 0.5 + temp_diff / 20.0` → is this sound? The entry_price = 0.85 hardcode → what other hardcodes exist? The `execute_trade` function computes P&L with `ceil(0.07 * contracts * price * (1-price)) * 2` → verify this against Kalshi's actual fee structure. The station registry has 20 stations → are all 20 tradeable? The 67.1% accuracy has a ±10pp CI → what's the worst-case true accuracy? Enumerate every assumption that could be wrong, the impact if it is, and the test that would prove or disprove it.

### Expert D — Model: DeepSeek V3.1
**Role:** Integration & Sequencing Architect
**Question:** The session produced 8 actionable items, 3 bug fixes, and 12,000 lines of dead code. What is the optimal sequence to merge all changes into main and deploy to DEV? Consider: the Kelly fix is applied but needs live price feed to be effective. The epoch-Kelly schedule is config-ready but not wired. The GEFS cron has no observability. The prod DB is stale. The ECMWF backfill finishes in ~6 days. Provide a merge sequence where each PR is self-contained, testable, and doesn't break the currently-working pipeline. Include: which files to modify, test strategy per PR, rollback plan, and how to verify the pipeline still produces 67%+ accuracy after each change.