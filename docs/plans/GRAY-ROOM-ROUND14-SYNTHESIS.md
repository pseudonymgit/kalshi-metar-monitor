# Gray Room Round 14 — Structured Synthesis

**Date:** 2026-08-02 20:32 UTC
**Experts:** A (GLM 5.2 — Config), B (Luna Pro — Readiness), C (GPT 5.4 — Edge Cases), D (DeepSeek V3.1 — Sequencing)
**Disposition protocol:** Every item = ADVANCE, PARK, or KILL

---

## Fresh Info: The Working Pipeline Is Not What We Described

**Expert C (GPT 5.4)** traced the code and found a critical discrepancy:

The framing document has been saying "GEFS ensemble fraction (31-member spread = P(event))" as the primary signal at 67.1% accuracy. **This is wrong.** The actual `scripts/phase1_paper_trading_cron.py` uses **ensemble mean vs threshold**, not member-level voting. The 31-member ensemble data (`ensemble_min`, `ensemble_max`, `n_members`) is loaded but **never referenced**. There is no ensemble fraction.

Furthermore, the market price is derived circularly from confidence:
```
market_price = 0.5 + confidence * 0.4
```
Which means `edge = confidence - market_price = 0.6 * confidence - 0.5`. For confidence=0.867 (minimum to clear edge_threshold=0.02), edge=0.02 exactly. **The system only catches trades that are barely above threshold** because the market price artificially tracks the confidence.

This doesn't invalidate the 67.1% accuracy — the ensemble-mean-threshold model clearly works — but the framing and the mechanism don't match. The "ensemble fraction" narrative needs correcting.

**Disposition:** **→ ADVANCE** — Update the framing docs to accurately describe the ensemble-mean-threshold mechanism. Separate the marketing language from the engineering reality.

---

## Expert A: Numerical Implementation (GLM 5.2)

### Provided: Complete JSON Config Block

```json
{
  "_comment": "Gray Room Round 14 Expert A — GLM 5.2. Corrected Kelly + epoch-based parameters.",
  "kelly": {
    "formula": "f*_star = edge/(1-c)",
    "edge_formula": "abs(win_rate - market_price)",
    "cost_fraction": 0.0205,
    "baseline_kelly": 0.1746,
    "_comment_baseline": "At p=0.67, M=0.50: edge=0.17, f*=0.1746",
    "fraction_kelly": 0.50,
    "effective_kelly": 0.0873
  },
  "epoch_multipliers": {
    "00Z": 0.70,
    "06Z": 0.85,
    "12Z": 1.00,
    "18Z": 0.55
  },
  "confidence_thresholds": {
    "00Z": 0.55,
    "06Z": 0.58,
    "12Z": 0.55,
    "18Z": 0.62
  },
  "entry_price_bounds": {
    "min": 0.25,
    "max": 0.75
  },
  "spread_limits_cents": {
    "00Z": 6,
    "06Z": 8,
    "12Z": 10,
    "18Z": 12
  },
  "edge_tiers": [
    {"threshold": 0.10, "multiplier": 1.00, "label": "strong_edge"},
    {"threshold": 0.06, "multiplier": 0.75, "label": "moderate_edge"},
    {"threshold": 0.03, "multiplier": 0.50, "label": "weak_edge"},
    {"threshold": 0.00, "multiplier": 0.00, "label": "no_trade"}
  ],
  "bankroll": {
    "max_percent_per_position": 0.05,
    "trailing_drawdown_pct": 0.20,
    "consecutive_loss_limit": 10,
    "portfolio_correlation_factor": 0.20
  },
  "_comment_correlation": {
    "n_stations": 20,
    "rho_approx": 0.40,
    "correction_factor": "1/(1+(n-1)*rho) = 1/(1+19*0.4) = 0.116",
    "safety_overage": "3x headroom → 0.35",
    "final": "0.20 (after applying headroom + 8% bankroll cap)"
  }
}
```

### Key Parameter Changes from Current Config

| Parameter | Current | GLM 5.2 Value | Why |
|-----------|---------|---------------|-----|
| Kelly f* (p=0.67, M=0.50) | 0.663 | **0.1746** | Market price correction |
| Effective Kelly (×0.5 fractional) | 0.331 | **0.0873** | Half-Kelly on corrected baseline |
| Max position per trade ($10K bankroll) | ~$800 (8%) | **~$175 (1.75%)** | Corrected Kelly × 5% cap |
| Portfolio correlation factor | none | **0.20×** | n=20, ρ=0.4, 3× headroom |
| Epoch multipliers | flat | 0.70/0.85/1.00/0.55 | Time-dependent signal quality |
| Bankroll cap per position | 8% | **5%** | Added headroom for correlation |
| Consecutive loss limit | 8 | **10** | Wider tolerance with smaller positions |
| Entry price bounds | [0.15, 0.85] | **[0.25, 0.75]** | Tighter, avoids extremes |

**Disposition:** **→ ADVANCE** — Values are justified and config-ready. Wire into `phase1_config.py` after the Kelly fix is verified.

---

## Expert B: Production Readiness (Luna Pro)

### Phase Plan

**Phase 1: Honest Pipeline (BLOCKING — do this first)**
| | |
|---|---|
| **Changes:** | Fix circular market price formula. Pull real prices from Kalshi API. Verify fee model matches Kalshi. Fix docstring/code contradiction. |
| **Safety:** | 10/10 — fixing circular math prevents systematic edge over-estimation |
| **P&L impact:** | 9/10 — honest edge = honest sizing = honest P&L |
| **Effort:** | 8 hours |
| **Test:** | Run cron with both old and new market price. Compare trade counts, sizing, and directional accuracy. Must keep 67%+ on direction. |
| **Rollback:** | Revert marketplace formula, restore 0.85 default |
| **Disp:** | **→ ADVANCE** |

**Phase 2: Config Implementation**
| | |
|---|---|
| **Changes:** | Wire epoch-based Kelly schedule. Add portfolio correlation matrix. Add per-station bias correction. |
| **Safety:** | 7/10 — smaller positions reduce ruin risk |
| **P&L impact:** | 6/10 — improves risk-adjusted returns, not raw accuracy |
| **Effort:** | 16 hours |
| **Test:** | Backtest on 85 existing trades. Compare P&L variance, max drawdown, Sharpe ratio before/after. |
| **Rollback:** | Revert config values to flat schedule |
| **Disp:** | **→ ADVANCE** |

**Phase 3: Observability & Dashboard**
| | |
|---|---|
| **Changes:** | Wire cron output to trading dashboard blueprint. Add stdout summary to cron. Add trade-level logging in a structured format the dashboard can consume. |
| **Safety:** | 1/10 (read-only changes) |
| **P&L impact:** | 1/10 (no impact — visibility only) |
| **Effort:** | 4 hours |
| **Test:** | Run cron, verify dashboard shows correct trade data |
| **Disp:** | **→ ADVANCE** (low effort, low risk) |

### Blocking vs Nice-to-Have

| Change | Status |
|--------|--------|
| Fix circular market price formula | **BLOCKING** |
| Fix fee model docstring/code mismatch | **BLOCKING** |
| Wire Kalshi API price feed | **BLOCKING** |
| Epoch-based Kelly schedule | NTH |
| Portfolio correlation | NTH |
| Station bias correction | NTH |
| Dashboard wiring | NTH |
| Delete dead code | NTH |

### Go/No-Go Criteria (Paper → Real Money)

1. ✅ 30 days of paper trading (not done — only 13 days)
2. ✅ ≥60% accuracy on the full 30 days
3. ✅ ≥0.30 Sharpe ratio
4. ✅ No single-day drawdown >10%
5. ✅ Settlement-confirmed accuracy (not METAR proxy)

Currently met: **none of the above** (only 13 days of partial paper data).

**Disposition:** **→ PARK** for real-money. Keep paper trading for ≥30 more days after Phase 1-2 changes are deployed.

---

## Expert C: Edge Case Analysis (GPT 5.4)

### Top 3 Hidden Errors

**Error 1: Ensemble-mean-threshold is not ensemble fraction [HIGH]**
| | |
|---|---|
| **What:** | Code loads GEFS member data but only uses `mean_c` — the ensemble mean. The 31-member ensemble spread data is unused. The "67.1% GEFS ensemble fraction" is actually "ensemble mean vs previous day's temperature threshold." |
| **Impact:** | The GEFS ensemble spread contains valuable uncertainty information. Ignoring it misses: (a) when members disagree (low confidence should reduce size), (b) when extreme members point to different regime (cold/warm outlier in ensemble). |
| **Test:** | Implement actual member-level voting: compute `fraction_up = count(members_forecast > prev_temp) / 31`. Compare hit rates for different fraction thresholds. |
| **Severity:** | HIGH — the system's described mechanism doesn't match the implemented code. Depth of impact unknown. |
| **Disp:** | **→ ADVANCE** — implement ensemble fraction, compare to ensemble-mean-threshold. If fraction is better, switch. If not, update docs. |

**Error 2: Circular confidence→edge computation [HIGH]**
| | |
|---|---|
| **What:** | `market_price = 0.5 + confidence * 0.4` then `edge = confidence - market_price`. This is a fixed function: `edge = 0.6*confidence - 0.5`. With confidence=0.55, edge= -0.17 (no trade). With confidence=0.867, edge=0.02 (barely trades). With confidence=0.99, edge=0.094 (ten trades). **Edge is artificially clamped to a narrow range** regardless of what the market actually thinks. |
| **Impact:** | If real market price is 0.40 for a signal with confidence=0.67, true edge=0.27, but system computes edge= -0.098 and refuses the trade. The system is blind to market inefficiencies where the GEFS signal disagrees with the market price. |
| **Test:** | Pull real Kalshi market prices for 30 historical trades. Compare: (a) edge using real market price, (b) edge using circular formula. Count how many real-positive-edge trades the circular formula would reject. |
| **Severity:** | HIGH — fundamental flaw in the trading logic. |
| **Disp:** | **→ ADVANCE** — fix immediately. Replace with live Kalshi API price feed. |

**Error 3: Fee model docstring contradicts code [MED]**
| | |
|---|---|
| **What:** | Docstring says "Kalshi fee is $0.01/contract/side flat (Fix 6)." Code computes `ceil(0.07 * contracts * price * (1-price)) * 2`. Kalshi charges both sides. The 0.07 is Kalshi's fee rate for weather markets. The code is likely correct and the docstring is stale, but this has never been verified against a live trade. |
| **Impact:** | The `ceil()` creates stair-step fee jumps. A trade of 113 contracts pays $2; 114 contracts pays $4 (100% jump for <1% position increase). This creates a position-sizing discontinuity at every fee-tier boundary. |
| **Test:** | Log into Kalshi dev account, place minimal test trade ($1), check actual fee charged. Compare to both formulas. |
| **Severity:** | MED — fee is small relative to contract value but the discontinuity could cause 1-2% P&L drift at tier boundaries. |
| **Disp:** | **→ ADVANCE** — test against live Kalshi fee. Fix docstring. |

### Hidden Assumption Rankings (Probability × Impact)

| Rank | Assumption | P × I | Disposition |
|------|------------|-------|-------------|
| **1** | Ensemble fraction = ensemble mean threshold | HIGH | ADVANCE — implement fraction or correct docs |
| **2** | Circular market price approximates real market | HIGH | ADVANCE — replace with API feed |
| **3** | Fee formula matches Kalshi actual fees | MED | ADVANCE — test against live trade |
| 4 | 67.1% accuracy will hold out-of-sample | MED | PARK — let 30-day paper test confirm |
| 5 | All 20 stations are tradeable | MED | ADVANCE — check per-station P&L |
| 6 | GEFS distribution is stationary across model versions | LOW | PARK |
| 7 | METAR is always available | LOW | PARK |
| 8 | Entry price 0.85 is reasonable approximation | HIGH | ADVANCE — already flagged, needs fix |

---

## Expert D: Merge Sequence (DeepSeek V3.1)

### PR1: Cleanup & Dead-Code Removal [ADVANCE]
| | |
|---|---|
| **Files:** | Delete 3 dead sizers, dashboard.py, any unreferenced FP modules |
| **Risk:** | None — imports not used by working pipeline |
| **Test:** | Run GEFS cron for 7 days, verify accuracy ≥67% |
| **Rollback:** | `git revert` |
| **Effort:** | 1 hour |

### PR2: Core Bug Fixes [ADVANCE] — ⚠️ Integrate Expert C findings
| | |
|---|---|
| **Files:** | position_sizer.py (Kelly fix), market_cost_model.py, phase1_paper_trading_cron.py (pass market_price, fix circular formula, fix fee docstring) |
| **Risk:** | MED — position sizes will change significantly (3-10× smaller). Must verify direction accuracy holds. |
| **Test:** | Run cron on same 7-day window. Compare: trade count, accuracy, P&L. Accuracy must stay ≥67% (should — direction is unchanged, only sizing changes). |
| **Rollback:** | `git revert` |
| **Effort:** | 4 hours |

### PR3: Reliability & Config [ADVANCE]
| | |
|---|---|
| **Files:** | circuit breaker wiring, epoch config, GRIB purge, backup trap |
| **Risk:** | LOW — additive, no logic changes to trading pipeline |
| **Test:** | Simulate API failure, verify circuit breaker. Run cron 7 days, verify accuracy ≥67%. |
| **Rollback:** | `git revert` |
| **Effort:** | 3 hours |

### PR4: Live Market Price Feed [ADVANCE — after PR2]
| | |
|---|---|
| **Files:** | phase1_paper_trading_cron.py (replace 0.85 with Kalshi API call) |
| **Risk:** | MED — introduces network dependency. Must handle API failure gracefully. |
| **Test:** | Compare live prices to hardcoded for 30 trades. Verify system doesn't crash on API failure. |
| **Rollback:** | Fall back to hardcoded 0.50 if API fails |
| **Effort:** | 4 hours |

### Deferred: Portfolio correlation, station bias, ECMWF integration [PARK]
These are future work once PR1-4 are merged and verified on DEV.

---

## Master Disposition Table

| Item | Expert | Disposition | Priority |
|------|--------|-------------|----------|
| 🔴 Fix circular market price formula | B, C | **ADVANCE** | **1** |
| 🔴 Replace ensemble-mean with ensemble fraction (or correct docs) | C | **ADVANCE** | **2** |
| 🔴 Wire live Kalshi API price feed | B, C, D | **ADVANCE** | **3** |
| 🟡 Apply corrected Kelly config (GLM 5.2 values) | A, B | **ADVANCE** | **4** |
| 🟡 Fix fee model docstring/code mismatch | C | **ADVANCE** | **5** |
| 🟡 Test fee formula against live Kalshi trade | C | **ADVANCE** | **6** |
| 🟡 Delete dead code (PR1) | D | **ADVANCE** | **7** |
| 🟡 Wire circuit breaker + GRIB purge (PR3) | D | **ADVANCE** | **8** |
| 🟢 Check per-station P&L for untradeable stations | C | **ADVANCE** | **9** |
| 🟢 Add epoch-based Kelly schedule | A, B | **ADVANCE** | **10** |
| 🟢 Add portfolio correlation factor | A, B | **ADVANCE** | **11** |
| 🟢 Add per-station bias correction | A, B | **ADVANCE** | **12** |
| 🟢 Wire dashboard to cron | B | **ADVANCE** | **13** |
| 🔵 ECMWF integration (shadow test) | B | **PARK** | After backfill |
| 🔵 Real-money go decision | B | **PARK** | After 30d paper |
| 🔵 Delete 12,000 lines of dead code at once | D | **PARK** | Low urgency |
| 🔵 GEFS model version distribution shift | C | **PARK** | Low probability |
| 🔵 METAR availability monitoring | C | **PARK** | Low risk |
