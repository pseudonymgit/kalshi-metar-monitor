# Gray Room Round 13 V2 — Full Synthesis

**Date:** 2026-08-02
**Experts:** A (GLM 5.2), B (Luna Pro), C (GPT 5.4), D (DeepSeek V3.1)

---

## 1. The System Is Simpler Than We've Been Building

**Expert B (Luna Pro)** traced imports. The working GEFS cron pipeline (67.1% accuracy, 85 trades) imports **nothing from `core/`**. It's 245 lines of standalone logic. The 3,397-line `paper_trading_engine.py`, 6 FP modules, 30+ signal files — none are wired into the pipeline that works.

**Recommendation:** Delete `paper_trading_engine.py`. Delete unwired FP modules. The minimal system needs:
- `phase1_paper_trading_cron.py` (the working GEFS cron) — 245 lines
- `position_sizer.py` (the canonical Kelly sizer)
- `market_cost_model.py` (fee source)
- `kalshi_settlement_upkeep.py` (settlement validation)
- 3 config parameters: edge threshold (0.02), Kelly fraction (0.50), entry price [0.15, 0.70]

---

## 2. The Kelly Formula Is Wrong — Fix Immediately

**Expert C (GPT 5.4)** found that `f* = (p - c) / (1 - c)` doesn't reference the market price. For binary options on Kalshi, the correct formula is:

```
f* = (p - q) / (1 - c)   where q = market_price (YES side)
```

Or equivalently: edge = (p × 1 + q × 0) - market_price = p × 1 - market_price (since q = 1-p for binary)

**Impact by order book price:**

| Scenario | Current f* | Correct f* | Over-bet |
|----------|-----------|-----------|----------|
| GEFS says 67%, market price=0.50 | 0.66 | 0.33 | **2×** |
| GEFS says 67%, market price=0.65 | 0.66 | 0.04 | **16×** |
| GEFS says 67%, market price=0.40 | 0.66 | 0.55 | 1.2× |
| GEFS says 55%, market price=0.50 | 0.54 | 0.10 | **5×** |

**At price=0.65 (common for mature markets), the system is betting 16× the correct Kelly amount.**

This is compounded by **portfolio correlation** (Expert C, Expert A):
- 20 stations, inter-station ρ ~0.3-0.5 (same weather system affects multiple cities)
- Correct portfolio-level Kelly = individual Kelly × (1 / (1 + (n-1)ρ))
- At ρ=0.4, n=20: diversification factor = 0.11 → **system is 3-5× too large even at correct single-station Kelly**

**Fix:** Add market price to Kelly calculation. Add station correlation matrix to position sizing.

---

## 3. Settlement Station Mismatch

**Expert C (GPT 5.4)** and **Expert D (DeepSeek V3.1)** both identified: Kalshi settles HIGH markets on NWS CLI data at specific ICAO stations. GEFS is calibrated against city-center coordinates. These can differ by 1-4°F.

The KNYC 2026-07-26 trade that lost -$52.14: GEFS predicted DOWN (mean 80.4°F vs yesterday 81°F), but actual HIGH was 85°F. That's a **4.6°F GEFS under-prediction** — well outside normal error range for a summer day in NYC. Either GEFS had a systematic warm bias that day, or the station coordinates don't align with Kalshi's settlement station.

**Fix:** Audit station mapping for all 20 stations. Compare GEFS coordinates vs NWS CLI stations.

---

## 4. Config-Ready Parameters

**Expert A (GLM 5.2)** produced exact numerical values:

| Parameter | 00Z | 06Z | 12Z | 18Z |
|-----------|-----|-----|-----|-----|
| Min confidence | 0.55 | 0.58 | 0.50 | 0.62 |
| Kelly epoch multiplier | 0.50 | 0.65 | 0.85 | 0.40 |
| Max spread (¢) | 6¢ | 8¢ | 8¢ | 12¢ |
| Entry price bounds | [0.25, 0.75] | [0.20, 0.80] | [0.15, 0.70] | [0.30, 0.70] |

---

## 5. Most Likely Failure Scenarios (Ranked)

**Expert C (GPT 5.4):**

1. **Kelly over-betting via missing market price** — HIGH probability, HIGH loss. The 67% accuracy masks a 2-16× over-bet. Will wipe out bankroll within 30 live trades if uncorrected.

2. **Settlement station mismatch** — MEDIUM probability, MEDIUM loss. Systematic, predictable errors that masquerade as model accuracy degradation.

3. **Correlation cascade** — MEDIUM probability, HIGH loss. Same weather system affects 20 stations → all positions lose simultaneously → 3-5× Kelly blowup.

---

## 6. Execution Trace Findings

**Expert D (DeepSeek V3.1):** Identified 25 edge cases across the 9-step trade lifecycle. Critical gaps:
- No sensor error/outlier detection for METAR or settlement data
- `raw_conf = 0.5 + temp_diff / 20.0` formula is fragile (temp_diff=0.63 → 0.53 confidence → still enters)
- No GEFS run-delay handling (if 12Z data arrives at 17Z instead of 16Z)
- No settlement dispute handling

---

## Recommended Actions (Priority Order)

1. **🔴 P0: Fix Kelly formula** — add market price reference. Change `f* = (p-c)/(1-c)` to `f* = (edge) / (1-c)` where edge = (p × 1 + q × 0) - market_price. Add station correlation matrix.
2. **🔴 P0: Verify station mapping** — audit all 20 stations against NWS CLI settlement sources
3. **🟡 P1: Simplify** — delete `paper_trading_engine.py`, unwired FP modules. ~4,000 lines → ~300
4. **🟡 P1: Apply epoch Kelly schedule** from Expert A config table
5. **🟢 P2: Add outlier detection** for settlement data, METAR gap monitoring
6. **🟢 P2: Re-run accuracy against CLI settlement** (not METAR proxy)