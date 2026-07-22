# Gray Room Round 9 — Expert 6: Market Microstructure & Execution

**Domain:** Market Microstructure, Execution Quality, Kalshi-Specific Trading
**Date:** 2026-07-22
**Analyst:** Expert 6 — Independent analysis. No prior Gray Room findings referenced.

---

## EXECUTIVE SUMMARY

Independent analysis of the weather engine's market microstructure, execution quality, and Kalshi-specific integration. Findings based on code review of paper_trading_engine.py, kalshi_price_fetcher.py, fee_aware_filter.py, position_sizing.py, and related modules.

**Overall assessment:** The system has strong signal generation but zero execution realism. Trade costs are underestimated by 6x+, market data never flows into execution decisions, and the liquidity filter is a no-op. If this engine went live, P&L would be significantly worse than paper results suggest.

---

## ERRORS (13)

### E1: Spread assumption is hardcoded at 0.5¢ but actual mean spread is 3.1¢
- **Where:** `core/paper_trading_engine.py` Line 2017: `spread_per_contract = 0.005`
- **Source:** Pre-read states mean spread is 3.1¢. The hardcoded 0.5¢ is a 6.2x underestimation.
- **Why wrong:** The edge filter is letting through trades that have negative real-world edge. A trade with 1.0¢ edge gets approved at 0.5¢ spread cost but would be unprofitable at 3.1¢.
- **Spec to fix:** `_compute_round_trip_cost` should accept a dynamic spread parameter from the live price fetcher. Default to measured mean (3.1¢) when live data unavailable. Remove hardcoded 0.005.

### E2: Slippage assumption is hardcoded with no market-size adjustment
- **Where:** `core/paper_trading_engine.py` Line 2018-2019: `slippage_per_contract = 0.005`
- **Why wrong:** Slippage depends on trade size relative to market depth. A $500 position on a thinly traded market will incur more slippage than a $50 position on a liquid market. The flat 0.5¢ assumption ignores this completely.
- **Spec to fix:** Slippage should be a function of position_size / estimated_market_depth. Use a tiered approach: <$50 → 0.5¢, $50-200 → 1.0¢, $200-500 → 2.0¢.

### E3: No actual bid-ask spread data ever used from the Kalshi API
- **Where:** `core/paper_trading_engine.py` method `_compute_round_trip_cost`
- **Why wrong:** `kalshi_price_fetcher.py` retrieves bid/ask data from the Kalshi API, but it's never fed into the cost calculation. The cost model uses hardcoded spread assumptions instead of real market data.
- **Spec to fix:** Wire the bid/ask spread from the live price fetcher into `_compute_round_trip_cost`. When live data is available, use `(ask - bid) / 2` as the spread cost. Fall back to measured mean only when API is unavailable.

### E4: FeeAwareEntryFilter uses 2¢ spread but paper_trading_engine uses 0.5¢ spread
- **Where:** `core/fee_aware_filter.py` Line 31: `DEFAULT_SPREAD = 0.02` vs `core/paper_trading_engine.py` Line 2017: `spread_per_contract = 0.005`
- **Why wrong:** These two modules are 4x apart on the same cost parameter. A trade that passes the FeeAwareEntryFilter (2¢ spread) will have different cost assumptions when actually executed (0.5¢ spread). This inconsistency means the filter and the engine are not aligned.
- **Spec to fix:** Centralize spread assumptions into a single config. Both modules should read from the same source (Kalshi API bid/ask data, falling back to measured mean).

### E5: Position sizing uses `fill_price = market_price` with no spread/slippage
- **Where:** `core/paper_trading_engine.py` Line 1725: `fill_price = market_price`
- **Why wrong:** The system assumes it can always fill at the last traded price. In reality, you pay the ask (buying) or receive the bid (selling). For a position that buys YES at 0.60, the fill price might be 0.62 (ask), which changes the P&L calculation.
- **Spec to fix:** `fill_price` should be `market_price + spread/2` for buys and `market_price - spread/2` for sells. Use the bid/ask from the live price fetcher.

### E6: LowLiquidityTrapFilter always passes because snapshot data is never populated
- **Where:** `core/paper_trading_engine.py` Lines 1489-1511, calling `LowLiquidityTrapFilter.analyze_market_liquidity`
- **Why wrong:** The `analyze_market_liquidity` method is called with `price_metadata` from `kalshi_price_fetcher`, but the fetcher never returns volume snapshot data. The `snapshots` dict is always `{}`, meaning the "all 4 snapshots < $15K" condition can never be met. The filter is effectively a no-op.
- **Spec to fix:** Either populate snapshot data from the Kalshi API, or remove the LowLiquidityTrapFilter entirely. A non-functional filter is worse than no filter — it gives a false sense of protection.

### E7: 24h volume check incorrectly flags all markets
- **Where:** `core/kalshi_price_fetcher.py` (volume_24h defaults to 0)
- **Why wrong:** Since `volume_24h` in the price metadata defaults to 0, the LowLiquidityTrapFilter condition `volume_24h < $50K` will always trigger for any market where the API response doesn't include `volume_24h_fp`. This could cause false positives.
- **Spec to fix:** Default volume_24h to None and handle the None case explicitly in the filter — skip the volume check when data is unavailable.

### E8: No market depth or order book analysis
- **Where:** Entire engine — no code reads order book depth
- **Why wrong:** For a $100-$500 position, there's no way to know if the market can absorb the trade without moving the price significantly. Weather markets on Kalshi can be thin, especially for less liquid cities.
- **Spec to fix:** Add order book depth retrieval from Kalshi API. Compute a "market impact" estimate: trade_size / total_depth_10_levels. Skip trades where impact > 2%.

### E9: Settlement price calculation uses wrong threshold for Kalshi binary contracts
- **Where:** `core/paper_trading_engine.py` `process_settlements_for_date`: `settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0`
- **Why wrong:** Kalshi temperature contracts settle at $1.00 if the condition is met (e.g., temperature > strike). The settlement_value in the DB is the actual temperature reading (e.g., 85.3°F), not a probability. Comparing temperature > 50.0°F is meaningless — all settlements would pass this threshold.
- **Spec to fix:** The settlement logic needs to compare the actual settlement temperature against the contract's strike price, not against a hardcoded 50.0 threshold. Replace the threshold-based check with proper strike price comparison.

### E10: Trade cost calculation would double-count fees if fee_rate were ever > 0
- **Where:** `core/paper_trading_engine.py` `place_paper_trade`: `net_cost = position_size + fee_cost * (1 if trade_type in [TradeType.BUY_YES, TradeType.BUY_NO] else -1)`
- **Why wrong:** `position_size` is already the dollar amount at risk. Adding `fee_cost` on top for buys means the position cost doesn't reflect the true economic exposure. Currently a no-op (fee_rate=0.0), but would cause incorrect P&L if fee_rate is ever set.
- **Spec to fix:** Remove fee_cost from net_cost. Fee cost should be a separate field in the trade record, not part of the position cost basis.

### E11: No intraday trading capability despite rising intraday signals
- **Where:** Entry window logic (T-18h to T-2h) — single entry window per day
- **Why wrong:** The system trades once per day. On Kalshi, daily contracts can be traded throughout the day, and prices fluctuate significantly based on weather updates. Intraday price movements on the same contract present opportunities that the current architecture cannot capture.
- **Spec to fix:** Add a separate intraday trading loop that runs at higher frequency (e.g., hourly). Evaluate the same daily contracts at multiple points during the day, entering when the edge is favorable and exiting when the edge diminishes.

### E12: Market price cache TTL is 60 seconds but daily run is single-threaded
- **Where:** `core/kalshi_price_fetcher.py` Line: `_PRICE_CACHE_TTL = 60.0`
- **Why wrong:** The daily paper run is a single sequential execution that processes all 20 stations. The 60-second cache TTL doesn't provide benefit in this context — prices are fetched once per station per run. The cache could serve stale data if the same ticker is queried in different contexts within the same run.
- **Spec to fix:** Reduce TTL to 0 for the daily run (always fetch fresh). Use caching only for the intraday monitoring loop. Parameterize the TTL.

### E13: No contract selection logic — always picks nearest-expiring market
- **Where:** `core/kalshi_price_fetcher.py` `_find_current_market_for_series`
- **Why wrong:** The nearest-expiring market might not be the best to trade. If settlement is tomorrow but the spread is 5¢, it might be better to trade the D+2 contract where the spread is 2¢. The system has no logic to compare multiple available contracts.
- **Spec to fix:** Add contract selection that considers spread, days-to-settlement, and available liquidity. Prefer the contract with the best risk-adjusted edge.

---

## IMPROVEMENTS (5)

### I1: Centralize cost model into a single config module
- **What:** Create a `core/market_cost_model.py` that defines all cost parameters (spread, slippage, commission) from a single source of truth. Both `paper_trading_engine.py` and `fee_aware_filter.py` read from this module.
- **Why better:** Eliminates the 4x spread discrepancy between modules. Single change point for cost assumptions.
- **Effort:** 1-2 hours

### I2: Wire bid/ask spread from live price fetcher into execution cost
- **What:** Pass the bid/ask prices from `kalshi_price_fetcher.py` through to `_compute_round_trip_cost` and `place_paper_trade`. Use real spread data when available.
- **Why better:** Moves from hardcoded assumptions to actual market data. Execution cost reflects real market conditions.
- **Effort:** 2-3 hours

### I3: Add intraday trading loop with sub-daily entry windows
- **What:** Create a separate intraday trading mode that runs hourly. Evaluates the same daily contracts but uses more recent METAR data and NWP updates to refine the prediction. Enters when the edge/spread ratio is favorable.
- **Why better:** Captures the value of intraday price movements. Signal accuracy improves as settlement approaches (more data available).
- **Effort:** 1-2 weeks

### I4: Build market depth analysis for position sizing
- **What:** Add order book depth retrieval from Kalshi API. Use depth data to cap position sizes: `max_position = min(current_cap, depth_10_levels * 0.1)`.
- **Why better:** Prevents the system from trading more than the market can absorb. Critical for the less liquid city markets.
- **Effort:** 3-5 days

### I5: Add contract selection optimizer
- **What:** When multiple contracts are available (D+1, D+2, etc.), select the one with the best Sharpe ratio after accounting for spread and days to settlement. Prefer D+1 when spread is tight, D+2 when D+1 spread is excessive.
- **Why better:** Could improve fill quality and reduce cost by 1-2¢ per contract.
- **Effort:** 2-3 days

---

## IDEAS (3)

### Idea 1: Spread-based entry signal
- **What:** When the bid-ask spread on a market widens significantly (e.g., >2x normal), treat it as a signal that informed traders are pulling liquidity. Either avoid trading or increase the edge requirement.
- **Expected benefit:** Avoids trading during periods of information asymmetry. Reduces adverse selection risk.
- **Risk:** Could miss trades during volatile but profitable periods.
- **Spec for validation:** Compare spread-widening events against subsequent price movements. Build a spread regime classifier.

### Idea 2: Volume-based momentum signal
- **What:** Kalshi provides 24h volume data. A sudden increase in volume on a specific market could indicate informed trading. If volume spikes and price moves in the same direction as our signal, it confirms the signal. If volume spikes and price moves opposite, it's a warning.
- **Expected benefit:** Adds a market-based confirmation layer on top of the meteorological signals.
- **Risk:** Volume data might be noisy; low-volume markets might not have enough volume to detect spikes.
- **Spec for validation:** Correlate volume spikes with subsequent price movements. Build a volume-based confidence modifier.

### Idea 3: Settlement-time arbitrage on stale contracts
- **What:** In the last hour before settlement, some contracts trade at prices that don't reflect the known settlement temperature. If the actual temperature is already known (from METAR observations at the airport), there's a near-certain arb opportunity.
- **Expected benefit:** Near-risk-free P&L from settlement-time mispricing.
- **Risk:** Kalshi might have protections against this (settlement price based on official readings, not market price). Only works if the market hasn't already converged.
- **Spec for validation:** Compare last-hour prices against actual settlement values for the last 3 months. Check if profitable opportunities exist net of spread.

---

## ELEPHANTS (2)

### Elephant 1: The entire cost model is wrong — paper P&L is not real P&L
- **What:** The system uses 0.5¢ spread + 0.5¢ slippage = 1.0¢ round trip. Actual costs are 3.1¢ spread + 0.5-2.0¢ slippage = 3.6-5.1¢ round trip. This is a 3.6-5.1x underestimation of trading costs.
- **Why matters:** At 72.3% accuracy, the average edge per trade is about 2-3¢ (market price ~0.60, analytical probability ~0.72 → edge = 0.12 × stake). If costs are 4-5¢, that's 40-50% of the edge consumed by costs. The paper P&L overstates real profitability by 2-3x.
- **What happens if ignored:** Going live with the current cost model would result in significantly worse P&L than paper trading. The system would be marginally profitable at best, possibly unprofitable.
- **Spec for resolution:** 
  1. Build a proper cost model using actual Kalshi bid/ask data
  2. Rerun Phase 14 test with real costs (3.1¢ spread + 0.5¢ slippage = 3.6¢ round trip)
  3. If accuracy drops below 65% threshold, the system is not ready for live trading

### Elephant 2: Zero execution realism — no slippage, no impact, no fill uncertainty
- **What:** The paper trading engine assumes perfect fills at the last traded price. In reality, fills are uncertain, slippage varies, and market impact exists. The system has no concept of:
  - Fill probability (will the order get filled?)
  - Partial fills (only some contracts filled)
  - Price improvement (fill at better than expected price)
  - Market impact (did the trade move the price?)
- **Why matters:** Paper trading always looks better than live trading. The gap between paper and live P&L is called "slippage" and it's the single biggest destroyer of strategy returns. Without realistic execution modeling, there's no way to estimate the true profitability of the strategy.
- **What happens if ignored:** The system could show 15% return in paper trading but 0% return in live trading. The bridge between paper and live P&L is the most important gap to close before going live.
- **Spec for resolution:**
  1. Add realistic execution model: random fill price within spread, partial fill probability based on volume, market impact based on position_size / market_depth
  2. Run Monte Carlo simulation with 1000 scenarios per trade
  3. Report expected P&L range (5th percentile, median, 95th percentile) instead of single point estimate
  4. Gate: Only go live if 5th percentile P&L is positive

---

## PRIORITY TABLE

| # | Item | Type | Effort | Impact | Priority |
|---|------|------|--------|--------|----------|
| E1 | Spread assumption 6.2x off | Error | 1h | Critical | P0 |
| E5 | fill_price = market_price | Error | 0.5h | Critical | P0 |
| E9 | Settlement price wrong threshold | Error | 1h | Critical | P0 |
| E6 | Liquidity filter is no-op | Error | 1h | High | P1 |
| E4 | 4x spread discrepancy | Error | 1h | High | P1 |
| E3 | Bid/ask never used | Error | 3h | High | P1 |
| E2 | Slippage not market-aware | Error | 2h | Medium | P2 |
| E8 | No order book depth | Error | 1d | Medium | P2 |
| E11 | No intraday trading | Error | 2w | Medium | P2 |
| I1 | Centralized cost model | Improvement | 2h | High | P1 |
| I2 | Wire bid/ask into execution | Improvement | 3h | High | P1 |
| I3 | Intraday trading loop | Improvement | 1-2w | Medium | P2 |
| Elephant 1 | Cost model wrong | Elephant | 1w | Critical | P0 |
| Elephant 2 | Zero execution realism | Elephant | 2w | Critical | P0 |