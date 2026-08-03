# Gray Room Round 13: Intraday Architecture Review

**Date:** 2026-08-02
**Framing:** The weather engine is an **intraday trading system** for Kalshi daily HIGH temperature markets. This review addresses the optimal intraday architecture — signal timing, execution cadence, position management, and confidence thresholds for a system that refines predictions continuously throughout the trading day.

## Central Question

> We have a GEFS ensemble fraction system predicting Kalshi daily HIGH temperature direction with **67.1% accuracy on 85 paper trades** (+$8,809 P&L). GEFS refreshes every 4 hours (00/06/12/18 UTC). METAR data arrives continuously. We have intraday signals (frontal passage nowcast, dewpoint modulation, spatial coherence) that can refine predictions during the trading day. Markets settle at daily close.
>
> **What is the optimal intraday architecture?** Specifically:
> - Signal timing: when during the trading day do different signals matter most?
> - Re-evaluation cadence: should we re-evaluate on every GEFS refresh? Every N hours? On METAR event triggers?
> - Entry/exit strategy: when should we enter a position vs. wait for confirmation? When should we exit vs. hold to settlement?
> - Position management: should we scale in/out during the day?
> - Confidence thresholds: different thresholds for entry at 00Z vs. 18Z?
> - How does the 67.1% accuracy on daily direction translate to expected intraday P&L?

## Experts

| Expert | Role | Focus |
|--------|------|-------|
| 1 | Meteorologist | Intraday forecast evolution — when during the day do GEFS/METAR signals have the most/least predictive power? |
| 2 | Market Microstructure | Optimal intraday entry/exit timing, position management pre-settlement, market liquidity patterns |
| 3 | Signal Fusion Architect | How to blend 4-hour GEFS refresh with event-driven intraday signals (frontal, dewpoint) |
| 4 | Quant Finance | Intraday Kelly sizing, managing P&L variance across a trading day, confidence-threshold timing |

## System State for Context

| Metric | Value |
|--------|-------|
| GEFS accuracy (paper trades) | 67.1% (85 trades, 13 days) |
| ECMWF backfill | 84.7% complete (996/1,176 dates) |
| Active signals | GEFS fraction, frontal passage nowcast, dewpoint modulation, spatial coherence, spike reversion, calendar climatology |
| Refresh cadence | Every 4 hours (GEFS cron) |
| Execution | Fee-aware Kelly via position_sizer.py, risk controls wired |
| Trading horizon | Daily close (markets settle on daily high temperature) |
| Environment | DEV active, PROD stale (schema mismatch) |