# Gray Room Round 13 — Full Aggregated Summary

**Date:** 2026-08-02
**Round 1:** DeepSeek V4 Flash (baseline findings)
**Round 2:** GLM 5.2, Luna Pro, GPT 5.4, DeepSeek V3.1 (deep analysis)

---

## 0. Why the 4,000 Lines of `core/` Don't Matter

Luna Pro traced every import chain. The **only pipeline producing real results** — the GEFS cron (67.1% accuracy, 85 trades, +$8,809 P&L) — imports **nothing from `core/`** except `position_sizer.py` (the Kelly sizer) and `market_cost_model.py` (the fee source).

Here's what's in `core/` and why it's unused:

| Module | Lines | Status | Why It Doesn't Matter |
|--------|-------|--------|----------------------|
| `paper_trading_engine.py` | **3,397** | ⚠️ Unused by working pipeline | Old pipeline, 40% accuracy on 15 trades. GEFS cron (245 lines) is the canonical pipeline. |
| `kelly_position_sizer.py` | 403 | ✅ Deleted | Replaced by position_sizer.py |
| `fee_aware_kelly_position_sizing.py` | 389 | ✅ Deleted | Replaced by position_sizer.py |
| `variance_weighted_sizing.py` | 616 | ✅ Deleted | Designed for 22-signal era, irrelevant post-GEFS-pivot |
| `spatial_coherence.py` | 539 | Unused by cron | Modulates confidence regionally — useful but not imported by the working cron |
| `adaptive_thresholds.py` | 641 | Unused by cron | Bayesian per-signal thresholds — not imported by the GEFS cron |
| `api_circuit_breaker.py` | 419 | Wired (Kalshi+NWP APIs) | Good to have but only fires if APIs fail |
| `risk_controls.py` | 559 | Wired (engine only) | Checked by paper_trading_engine.py which doesn't run; not imported by GEFS cron |
| `dashboard.py` | 760 | ✅ Deleted | Replaced by trading_dashboard blueprint |
| All signal modules (30+) | ~5,000 | Unused by cron | GEFS cron uses only `gefs_ensemble_fraction` — no `core/signals/*` imported |
| FP modules (6) | ~3,000 | Unused by cron | db_connection, adaptive_thresholds, spatial_coherence, radiational_cooling — none imported by cron |

**Total dead or redundant code: ~12,000 lines.** The working system is ~500 lines spread across 5 files.

---

## 1. 🔴 P0: Kelly Formula Fix (Applied)

**Finding:** `f* = (p - c) / (1 - c)` doesn't reference market price.

The correct formula for binary options:
```
edge = abs(win_rate - market_price)     # probability vs. what you pay
f* = edge / (1 - c)                      # fraction of bankroll
```

**Impact by scenario:**

| Scenario | Old f* | Correct f* | Over-bet |
|----------|--------|-----------|----------|
| p=0.67, M=0.50 (fair market) | 0.66 | 0.173 | **3.8×** |
| p=0.67, M=0.65 (expensive market) | 0.66 | 0.020 | **33×** |
| p=0.67, M=0.40 (cheap market) | 0.66 | 0.275 | 2.4× |
| p=0.55, M=0.50 (slim edge) | 0.54 | 0.051 | **10.6×** |
| p=0.40, M=0.65 (overpriced NO) | 0.40 | 0.00 (negative edge) | **∞** |

With portfolio correlation (ρ~0.4, n=20): add another 3-5× on top.

**Fix applied:** `position_sizer.py` now takes `market_price` parameter. Formula changed to `f* = abs(p - M) / (1 - c)`. Cron script updated to pass market_price through.

**Still needed:** Live market price pull from Kalshi API. Currently defaults to 0.50.

---

## 2. 🔴 P0: Station Mapping Audit

**Finding:** All 20 stations match between station_mapping.json and Kalshi settlement data. System stations → Kalshi series mapping is verified.

**But:** GEFS 0.5° grid cell may contain multiple ASOS stations. For KMDW (Chicago Midway), the GEFS grid cell overlaps KORD (O'Hare). If GEFS forecast is for a different station than Kalshi settles on, accuracy drops.

**KNYC July 26 loss case:** GEFS predicted 80.4°F, actual was 85°F (4.6°F error). Either GEFS had a warm-season urban heat island bias, or coordinates don't align with Central Park.

**Fix applied:** Mapping verified against settlement DB. No station code mismatches found. Next step: spot-check GEFS vs NWS CLI for each station with 10+ historical days.

---

## 3. 🟡 P1: System Simplification

Recommendation: delete everything except:
- `scripts/phase1_paper_trading_cron.py` (245 lines — the working pipeline)
- `core/position_sizer.py` (the Kelly sizer)
- `core/market_cost_model.py` (fee source)
- `core/station_registry.py` (station codes + mapping)
- `core/kalshi_settlement_upkeep.py` (settlement validation via cron)
- `core/api_circuit_breaker.py` (wired, useful)
- `core/sqlite_utils.py` (shared DB utilities)

Everything else in `core/` (30+ files, ~12,000 lines) is either dead code, superseded, or unused by the working pipeline.

---

## 4. 🟡 P1: Epoch-Based Kelly Schedule

**Expert A (GLM 5.2) config:**

| Parameter | 00Z | 06Z | 12Z | 18Z |
|-----------|-----|-----|-----|-----|
| Min confidence | 0.55 | 0.58 | 0.50 | 0.62 |
| Kelly epoch multiplier | 0.50 | 0.65 | 0.85 | 0.40 |
| Max entry spread (¢) | 6¢ | 8¢ | 8¢ | 12¢ |
| Entry price bounds | [0.25, 0.75] | [0.20, 0.80] | [0.15, 0.70] | [0.30, 0.70] |
| Volume liquidity | ~40% | ~25% | ~25% | ~35% (pre-close) |

Apply as: `effective_kelly = raw_kelly × epoch_mult × disagreement_mult × drawdown_penalty`

---

## 5. Failure Mode Rankings

**Expert C (GPT 5.4) ranking by expected loss:**

| Rank | Failure Mode | Prob | Loss Severity | Expected Loss |
|------|-------------|------|---------------|---------------|
| **1** | Kelly over-bet (no market price) | HIGH | Catastrophic | Highest |
| **2** | Settlement station mismatch | MED | Medium | High |
| **3** | Correlation cascade (20 stations) | MED | High | High |
| 4 | GEFS warm-season bias (urban heat island) | MED | Medium | Medium |
| 5 | Entry price = 0.85 hardcoded | HIGH | Low | Medium |
| 6 | Kelly not accounting for fees properly | HIGH | Medium | Medium |
| 7 | Liquidity trap at settlement close | LOW | High | Medium |
| 8 | METAR vs CLI settlement discrepancy | MED | Low | Low |
| 9 | GEFS model update (distribution shift) | LOW | Low | Low |
| 10 | ECMWF-GEFS correlation (adding noise) | MED | Low | Low |

---

## 6. Execution Trace (KNYC July 26)

**Expert D (DeepSeek V3.1):** Traced the losing trade (-$52.14).

Root cause: GEFS ensemble mean was 80.4°F (26.87°C), but actual daily HIGH was 85°F (30.0°C). **3.13°C bias** — too large for ordinary GEFS error. Possible causes:
- GEFS didn't capture urban heat island effect (NYC Central Park vs. GEFS grid average)
- Warm-air advection after GEFS initialization
- Standard warm-season underprediction

The confidence formula `raw_conf = 0.5 + temp_diff / 20.0` with temp_diff=0.63°C gives 0.53 confidence. The system entered a trade with barely-above-coinflip confidence and lost.

---

## 7. Recommended Action Items (Ranked)

| # | Action | P0/P1 | Effort |
|---|--------|-------|--------|
| 1 | ✅ **Kelly formula fixed** — market price reference added | P0 | ✅ Done |
| 2 | **Pull live market prices** from Kalshi API for correct Kelly | P0 | 1 day |
| 3 | **Apply epoch-Kelly schedule** config values | P1 | 0.5 day |
| 4 | **Delete dead code** (~12,000 lines) | P1 | 1 day |
| 5 | **Spot-check station mapping** vs NWS CLI for 10+ days | P1 | 0.5 day |
| 6 | **Add portfolio correlation** to position sizing (ρ matrix) | P1 | 1 day |
| 7 | **Reduce entry_price default** (0.85 → actual market data or 0.50) | P1 | 0.5 day |