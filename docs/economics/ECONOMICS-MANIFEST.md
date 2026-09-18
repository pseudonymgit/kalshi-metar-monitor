<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Weather Engine — Economics Sprint Manifest

**Sprint Lead:** Marty Byrde (CFO)  
**Date:** 2026-08-23  
**Source:** Dan sprint command (dashboard inter-session)  
**Authorization:** "Join the weather-engine sprint as economics owner"  
**Boundaries:** No credentials, no trade, no deploy, no shared worktree mutation  

## Objective

Build the **executable-economics schema and acceptance model** that converts passive Kalshi market data into production-grade daily portfolio P&L metrics. This replaces the existing modeled proxy economics (uniform $1 notional, exchange-fee-only, trade-level Sharpe) with executable economics that account for:

- Actual bid/ask spreads and market depth
- Fill/no-fill probabilities and order book dynamics
- Real fee structure (taker/maker/rebate)
- Spread + slippage costs by station and market type
- Fractional Kelly position sizing with concentration limits
- Daily portfolio P&L (correlated multi-trade returns)
- Peak-to-current drawdown tracking
- Block-bootstrap confidence intervals on all risk metrics

## Subagents Deployed

### marty-cost-modeler (session: 8dce44f8...)
**Output:** `prototypes/weather-engine-economics/cost-schema-v1.md`  
**Coverage:** Bid/ask spread model, fill/no-fill model, fee canonicalization, slippage model, trade journal SQL schema, acceptance criteria  
**Model:** `openai/gpt-5.6-luna`  
**Status:** 🔄 RUNNING  

### marty-resource-allocator (session: 220a4a49...)
**Output:** `prototypes/weather-engine-economics/risk-sizing-schema-v1.md`  
**Coverage:** Kelly-based sizing model, daily portfolio P&L schema, drawdown tracking, concentration/correlation model, bootstrap CI, acceptance model  
**Model:** `openai/gpt-5.6-luna`  
**Status:** 🔄 RUNNING  

## Current Economic Baseline (Pre-Sprint, for Reference)

| Metric | Value | Status |
|--------|-------|--------|
| Holdout accuracy (identity, Kalshi-grounded) | 56.59% | ✅ Authoritative |
| Trade-level proxy Sharpe | 1.60 | ❌ Not executable (modeled, uniform $1 notional, exchange-fee-only) |
| Daily portfolio Sharpe (full-cost estimate) | ~0.35-0.44 | ⚠️ Estimated, not canonical |
| Walk-forward Sharpe | ~0.50-0.80 | ⚠️ Estimated, pending daily computation |
| Kelly sizing | 7 sizers → 1 (A6) | ✅ Complete, not wired |
| Fee model | 7% taker, verified | ✅ Externally confirmed |
| Pairwise station correlation | rho ~0.0625 | ✅ Known, needs schema |
| N_eff (effective samples) | ~2,592 | ✅ Known, needs schema |
| Assumed notional | $1/trade (uniform) | ❌ Not executable |

## Sprint Deliverables

1. **Cost Schema** (`cost-schema-v1.md`) — designs the executable cost model
2. **Risk/Sizing Schema** (`risk-sizing-schema-v1.md`) — designs the executable risk model
3. **Integration Receipt** (`.meta/continuity/weather-engine/economics-sprint-receipt.md`) — after both complete
4. **Quality Journal Update** — routing quality record

## After Both Complete: Review Phase (2h)

1. Cross-check cost schema vs risk/sizing schema for consistency
2. Identify gaps, overlaps, contradictions
3. Specify implementation order and dependencies
4. Write integration receipt with actionable next-step summary

## Hard Gates (From Gerri Governance)

1. ✅ No calibration global enablement pending Phase 0.1
2. ✅ No sweep until clean snapshot + preflight
3. ✅ No sweep authorization until 60%+ holdout AND daily portfolio Sharpe ≥1.0 (full cost)
4. ✅ No execution from shared dirty worktree
5. ✅ No deployment, shadow, or live capital changes without separate review
6. ✅ **New (this sprint):** No trade, credentials, deploy, or shared worktree mutation