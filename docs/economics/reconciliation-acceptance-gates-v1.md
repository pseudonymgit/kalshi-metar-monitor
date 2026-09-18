<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Economics Lane Reconciliation & Acceptance Gates — Test Suite Spec

**Owner:** Marty Byrde (CFO) — economics lane  
**Date:** 2026-08-23 23:30 UTC  
**Status:** Test/Spec Design (executable when gate clears)  
**Trigger:** Smart review flags — economics lane NOT deployed
**Findings addressed:** DEV $166K P&L suspicious vs $10-20 sizing; 17 open trades stale; PROD 0 trades; hardcoded calibration
**Boundaries:** ❌ No credentials, ❌ No API, ❌ No trading, ❌ No promotion claims

---

## 1. Why This Exists

The smart review found the economics lane is not deployed-safe:

| Finding | Root Cause Class | This Spec Addresses |
|---------|------------------|---------------------|
| DEV $166K P&L with $10-20 sizing | Position/capital mismatch leaks into P&L | Reconciliation Test R1 (sizing↔P&L) |
| 17 open trades stale | No staleness/timeout guard | Reconciliation Test R4 (stale-trade detection) |
| PROD 0 trades while DEV trades | Config/env divergence not caught | Reconciliation Test R3 (env parity) |
| Hardcoded calibration | Calibration untracked / not versioned | Reconciliation Test R5 (calibration integrity) |

These are not the same as the cost-model acceptance criteria (C1-C7). Those validate the cost model. These validate **the economics lane itself** as a deployable, reconcilable system.

---

## 2. Reconciliation Test Suite (R1-R7)

Each test has a PASS/FAIL with a hard threshold. The lane cannot be promoted while any RED/BLOCKER test fails.

### R1: Position ↔ P&L Consistency

**Question:** Does the P&L claimed match the actual position sizes and market data?

**Checks:**
- P&L per trade ≤ max plausible: |pnl| ≤ position_usdc (you can't make more than you risk on a long YES)
- If a YES position bought at price P, max loss = P × contracts, max gain = (1-P) × contracts
- PnL / position_usdc ratio ∈ [-1, (1-P)/P] — outside this range is impossible for a settled position

**Threshold:** 0 violations allowed. Any trade where |pnl| > position_usdc × 1.05 → RED.

**Example (from review):** $166K total P&L across trades sized $10-20 → 8,000-16,600 trades at max win each, or P&L exceeds position. Either the trade count or the $166K is wrong.

### R2: Daily P&L Rollup Consistency

**Question:** Does the daily_pnl table reconcile to the underlying trade_journal?

**Checks:**
- Σ(trade_journal.net_pnl for date d) == daily_pnl.net_pnl for date d
- Σ(fee_entry + fee_exit) == daily_pnl.fee_paid
- Σ(spread_cost + slippage_cost) == daily_pnl.spread_slippage_cost
- daily_return == net_pnl / bankroll_start

**Threshold:** Tolerance ≤ $0.01 per day. Any day off by >$0.01 → YELLOW; >$1.00 → RED.

### R3: Environment Parity

**Question:** Is DEV and PROD behavior consistent, and any divergence explained?

**Checks:**
- If PROD has 0 trades and DEV has N, is there a config flag, timing, or market difference that explains it?
- Compare: position sizing config, signal thresholds, market universe, collection status
- A "0 trades" PROD alongside "N trades" DEV is acceptable ONLY if documented (e.g., PROD intentionally paper-only, DEV collecting)

**Threshold:** Any unexplained DEV/PROD divergence in trade count, sizing, or calibration → YELLOW (block promotion until explained).

### R4: Open-Trade Staleness Guard

**Question:** Are there open positions exceeding their max-hold without resolution?

**Checks:**
- Any trade_journal row with entry_ts older than max_hold_hours + grace_period and no exit_ts → STALE
- Any open position exceeding market settlement time without settlement → STALE
- Count of stale trades: should be 0 for a healthy lane

**Threshold:** >0 stale trades per 1000 settled → RED. A "17 open trades stale" finding is exactly this failure.

### R5: Calibration Integrity

**Question:** Is the calibration data current, versioned, and not hardcoded?

**Checks:**
- cost_model_calibration must have a calibration_version with computed_at timestamp
- No calibration coefficient should be a hardcoded constant in code — must live in config or calibration table
- Calibration freshness: latest calibration_version computed_at within last 30 days → GREEN; >60 days → YELLOW
- Data source flow: calibration must be derived from live fills/quotes, not constants

**Threshold:** Any unknown-origin or hardcoded calibration coefficient → RED. "Hardcoded calibration" is exactly this failure.

### R6: Settlement Reconciliation

**Question:** Does every settled market reconcile to its recorded settlement price?

**Checks:**
- Every condition_id with settlement_truth must have matching signal exits
- settlement_hit for each: YES (was held to win) or NO (lost) — must be populated, not PENDING for closed markets
- Open markets should be PENDING; closed markets must never be PENDING
- If a market has settled for >48h and its signal entries are still PENDING → STALE/RED

**Threshold:** 0 settled-but-PENDING entries after 48h grace → RED. This catches the "0% settlement-validated" issue.

### R7: Numeric Sanity / Edge-Case Guards

**Question:** Are there impossible or corrupt values?

**Checks:**
- Prices 0 ≤ P ≤ 1 (settled contract prices are $0 or $1; entry prices in [0.01, 0.99])
- position_contracts > 0
- entry_ts ≤ exit_ts
- pnl_usdc is finite (not NaN/Inf)
- drawdown in [0, 1] (clamp schema-level)
- No negative position sizing

**Threshold:** Any violation → RED.

---

## 3. Acceptance Gate (Two-Tier, No Promotion Without Green)

### Tier 1: Reconciliation Gate (Must Be FULLY GREEN)

**All 7 reconciliation tests R1-R7 must be GREEN** (0 RED, 0 unexplained YELLOW) before the economics lane can be considered **operationally deployed**.

This is a hard, non-negotiable gate. A lane with $166K P&L that can't reconcile to $10-20 sizing **cannot pass R1** and therefore **cannot be promoted or claimed**.

```
╔══════════════════════════════════════════════════════════════╗
║             RECONCILIATION GATE (MUST BE GREEN)               ║
║  R1 position↔P&L   R2 daily rollup   R3 env parity           ║
║  R4 stale trades   R5 calibration     R6 settlement          ║
║  R7 numeric sanity                                           ║
║  ───────────────────────────────────────────────             ║
║  ALL GREEN → Economics lane operationally RELEASED           ║
║  ANY RED → BLOCKED. Fix the failing test. No claims.         ║
╚══════════════════════════════════════════════════════════════╝
```

### Tier 2: Performance Gate (Requires Tier 1 Green + Live Data)

The existing dual gate from risk-sizing-schema-v1.md §6 applies **only after** Tier 1 is green and live data (not fixtures) accumulates:

| Gate | Sharpe (daily, full-cost) | CI Lower Bound | Max Drawdown | Concentration |
|------|--------------------------|----------------|--------------|---------------|
| Good Enough | ≥0.5 | ≥0.0 | ≤30% | ≤25% |
| Promotion | ≥1.0 | ≥0.5 | ≤20% | ≤15% |

**The performance gate does not exist yet to pass.** Current status: Tier 1 RED (R1 fails — $166K P&L doesn't reconcile to sizing; R4 fails — stale trades; R5 fails — hardcoded calibration). Therefore:

> **NO PROMOTION CLAIM IS SUPPORTED. The economics lane is NOT deployed.**

---

## 4. Boundary Enforcement

| Prohibited | Enforcement |
|-----------|-------------|
| Promotion claim | Any "strategy works / Sharpe ≥ X" claim without Tier 1 green + 20 days live data → violation |
| Financial commitment | No capital deployment based on current state |
| Credentials/API | Not needed — reconciliation tests run on existing DB data |
| Shared worktree mutation | Tests run read-only against economics lane data |
| Hardcoding | R5 enforces calibration provenance |

---

## 5. Implementation

Reconciliation tests R1-R7 are **pure SQL/Python checks on the economics lane DB**. They require:
- signal_journal (or equivalent trade journal) with entry/exit/pnl/position fields
- daily_pnl rollup table
- settlement_truth table
- cost_model_calibration with versioning

**Effort: ~6-8h** to implement as a `reconcile_economics.py` script with a structured PASS/FAIL output table. Runs read-only, produces a receipt.

**Priority:** R1 (sizing↔P&L), R4 (stale), R5 (calibration integrity) first — these directly address the smart review's three concrete findings.

---

## 6. Current Status (Honest)

| Test | Status | Evidence |
|------|--------|----------|
| R1 position↔P&L | 🔴 RED (virtual) | DEV $166K P&L vs $10-20 sizing — cannot reconcile |
| R2 daily rollup | ❓ Not run | Needs correct daily_pnl population |
| R3 env parity | 🔴 RED (virtual) | PROD 0 vs DEV N unexplained |
| R4 stale trades | 🔴 RED (virtual) | 17 open trades stale |
| R5 calibration | 🔴 RED (virtual) | Hardcoded calibration flagged |
| R6 settlement | ❓ Unknown | 0% settlement-validated previously |
| R7 numeric sanity | ❓ Not run | |

**Verdict: The economics lane is NOT deployment-safe. Do not promote. Do not claim P&L. Fix tests R1/R4/R5 first.**

---

*Reconciliation & acceptance gate spec — economics lane*  
*Purpose: prevent promotion until the lane is verifiable. No promotion claims made.*