# Gray Room Round 9 — Expert 2: Quantitative Finance & Trading Systems

**Domain:** Quantitative Finance, Backtesting, Position Sizing, and Trading Systems
**Date:** 2026-07-22
**Analyst:** Expert 2

---

## EXECUTIVE SUMMARY

This is a weather trading engine that predicts daily HIGH/LOW temperature settlement for 20 US cities, running paper trades on Kalshi temperature markets. The codebase has **four separate position sizing implementations**, **three different cost/fee estimates**, **a fundamentally broken settlement P&L calculation**, and **systematic confusion between forecast probability and historical win rate** in its Kelly criterion. The trading system is currently not producing economically meaningful P&L figures.

**Critical path items:**
1. Settlement P&L is wrong — temperature buckets are treated as contract prices
2. Kelly criterion receives `analytical_prob` as `win_rate` — invalidates all position sizing
3. Four competing sizing systems with contradictory formulas and caps
4. No individual trade stop-loss or take-profit
5. Sharpe ratio is unannualized — incomparable to standard benchmarks

---

## ERRORS (10+)

### ERROR 1: Settlement P&L Uses Temperature Bucket as Contract Price

**What:** The settlement P&L calculation treats `settlement_bucket` (a temperature bucket, e.g., 85°F) as if it were a contract price in cents.

**Where:** `core/paper_trading_engine.py` line 2215

```python
settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0
```

**Why wrong:** The `settlement_value` comes from `settlement_bucket` in the `settlement_epochs` table. This is a temperature bucket value (e.g., 85 for the 85°F bucket, 32 for 32°F), not a contract price. The comparison `> 50.0` is treating the temperature as if it were a binary threshold (above/below 50°F = 10°C), which is not how Kalshi temperature markets work. Kalshi contracts have specific strike prices — a HIGH contract at strike 85°F pays $1 if the actual high is ≥ 85°F, $0 otherwise. The settlement logic needs to compare the actual temperature against the **contract's strike price**, not a hardcoded 50°F threshold.

**Impact:** Trades in cities where temperatures are typically above 50°F (e.g., Miami, Los Angeles, Phoenix) will almost always settle as "wins" regardless of the actual contract strike. Trades in colder cities (e.g., Minneapolis, Denver in winter) will almost always settle as "losses." This makes all P&L figures meaningless.

**Spec to fix:**
```python
# In paper_trading_engine.py _process_settlements():
# The settlement_bucket is the actual temperature bucket value.
# We need to compare it against the contract's strike price.
# Query the contract's strike price from the trades table or market metadata.
for trade in unsettled_trades:
    trade_uuid, station, trade_type_str, filled_price, market_price, quantity, settlement_value = trade
    # settlement_value is the actual temperature bucket (e.g., 85)
    # Compare against the contract strike stored at trade entry
    # For a BUY_YES on a HIGH contract at strike 85: pays $1 if temp >= 85
    strike_price = get_contract_strike(trade_uuid)  # Must be stored per trade
    if strike_price is not None:
        settlement_contract_value = 1.0 if settlement_value >= strike_price else 0.0
    else:
        # Fallback: use the position's implied strike from market_price
        # market_price = probability that temp >= strike
        # This is NOT the strike — we need actual strike data
        _LOGGER.error("Cannot settle trade %s: no strike price recorded", trade_uuid)
        continue
```

**Dependency:** Requires storing the contract strike price (or the market ticker) at trade execution time. The `trades` table schema needs a `strike_price` column.

---

### ERROR 2: Kelly Criterion Receives Forecast Probability as Win Rate

**What:** The paper trading engine passes `analytical_prob` (forecast probability for the current trade) as the `win_rate` parameter to the Kelly sizer.

**Where:** `core/paper_trading_engine.py` line 1608

```python
# Compute edge and win rate
edge = abs(analytical_prob - market_price)
win_rate = analytical_prob
```

**Why wrong:** Kelly criterion's `win_rate` (p in the formula f* = (p - c) / (1 - c)) must be the **historical win rate of the strategy** — what fraction of past trades were profitable. Using the forecast probability of the current trade is a category error. If the forecast probability is 0.8 and the Kelly sizer thinks this is an 80% win rate, it will size positions as if the strategy is 80% accurate, when in reality the strategy might only be 55% accurate. This systematically over-sizes positions.

**Impact:** Inflated position sizes. The Kelly sizer's `calculate_kelly_fraction(p, c)` trusts that `p` is the true win rate. If `p = 0.80` but actual win rate is 0.58, the Kelly fraction would be (0.80 - 0.031) / (1 - 0.031) = 0.793, suggesting 79% of bankroll, when the correct fraction should be (0.58 - 0.031) / (1 - 0.031) = 0.566. The 8% bankroll cap partially masks this, but the error enters the scaling chain.

**Spec to fix:**
```python
# In paper_trading_engine.py, the position sizing pipeline:
# Use the Kelly sizer's own Bayesian belief (posterior mean) as win_rate
# Or pass a rolling historical win rate from the trade journal
# The analytical_prob is the forecast, not the win rate

if self._kelly_sizer is not None:
    # Use the Kelly sizer's posterior mean as win rate
    win_rate = self._kelly_sizer.get_belief().posterior_mean
else:
    # Fallback: query trade journal for rolling 30-day win rate
    win_rate = self._get_rolling_win_rate_from_journal(station, market_type, window_days=30)

# Edge is the expected value per dollar bet, calculated correctly
# For BUY_YES: EV = analytical_prob * (1 - market_price) - (1 - analytical_prob) * market_price
edge = self._calculate_expected_value(analytical_prob, market_price, trade_type)
```

---

### ERROR 3: Edge = abs(analytical_prob - market_price) Is Wrong

**What:** The edge calculation for Kelly sizing uses `abs(analytical_prob - market_price)`, which is the probability difference, not the expected dollar return.

**Where:** `core/paper_trading_engine.py` line 1607

```python
edge = abs(analytical_prob - market_price)
```

**Why wrong:** For a binary contract at price `p`, the expected value of a BUY_YES trade is `analytical_prob * (1 - p) - (1 - analytical_prob) * p`. The code computes `abs(analytical_prob - market_price)`, which equals the true EV only for symmetric 1:1 payoffs (which Kalshi contracts are). But the conceptual error matters because the edge is then passed to the Kelly sizer, which ignores it (see Error 4). The real harm is that the code conflates probability difference with expected value, making it harder to understand and maintain.

**Impact:** Functionally hidden by payoff symmetry, but the edge value is passed to (and ignored by) the Kelly sizer. Conceptual confusion.

**Spec to fix:**
```python
def _calculate_expected_value(self, forecast_prob: float, market_price: float, trade_type: TradeType) -> float:
    """Calculate expected value per dollar for the given trade type."""
    if trade_type == TradeType.BUY_YES:
        return forecast_prob * (1.0 - market_price) - (1.0 - forecast_prob) * market_price
    elif trade_type == TradeType.BUY_NO:
        no_price = 1.0 - market_price
        no_forecast = 1.0 - forecast_prob
        return no_forecast * (1.0 - no_price) - (1.0 - no_forecast) * no_price
    elif trade_type == TradeType.SELL_YES:
        return market_price - forecast_prob * 1.0
    elif trade_type == TradeType.SELL_NO:
        no_price = 1.0 - market_price
        return no_price - (1.0 - forecast_prob) * 1.0
    return 0.0
```

---

### ERROR 4: Kelly Sizer's `calculate_kelly_fraction` Ignores the `edge` Parameter

**What:** `KellyPositionSizer.calculate_kelly_fraction(edge, win_rate)` receives `edge` but computes `f* = (win_rate - c) / (1 - c)` using only `win_rate`. The `edge` parameter is silently ignored.

**Where:** `core/kelly_position_sizer.py` lines 155-174

```python
def calculate_kelly_fraction(self, edge: float, win_rate: float) -> float:
    c = self._cost_fraction
    kelly = (win_rate - c) / (1.0 - c)
    return max(0.0, min(1.0, kelly))
```

**Why wrong:** The function signature promises `edge` is used, but the formula only uses `win_rate`. The edge parameter is dead code. The caller passes `edge = abs(analytical_prob - market_price)` (already wrong — see Error 3), but the sizer doesn't use it anyway.

**Impact:** The caller believes it's passing edge information, the sizer pretends to accept it, but neither is correctly connected. The `edge` parameter should be removed or the method should use both.

**Spec to fix:**
```python
def calculate_kelly_fraction(self, win_rate: float) -> float:
    """Calculate Kelly fraction using historical win rate only.
    f* = (p - c) / (1 - c) for binary outcomes."""
    c = self._cost_fraction
    if c >= 1.0:
        return 0.0
    kelly = (win_rate - c) / (1.0 - c)
    return max(0.0, min(1.0, kelly))
```

---

### ERROR 5: Four Competing Position Sizing Systems with Different Formulas and Caps

**What:** The codebase has four separate position sizing implementations, each with different formulas, caps, and fee treatments.

**Where:**
1. `core/position_sizing.py` — `compute_position_size()` with `max_position_fraction=0.25`, `fraction_kelly=0.5`
2. `core/kelly_position_sizer.py` — corrected formula `f* = (p-c)/(1-c)`, 8% cap, `DEFAULT_COST_FRACTION=0.031`
3. `core/fee_aware_kelly_position_sizing.py` — `fraction_kelly=0.5`, `max_position_pct=0.25`, `fee_rate=0.0`, `edge/variance` formula
4. Inline in `paper_trading_engine.py` — uses file #2, fallback to `balance * edge * confidence * 0.08`

**Key conflicts:**
| Parameter | pos_sizing.py | kelly_sizer.py | fee_aware_kelly.py | paper_trading.py |
|---|---|---|---|---|
| Max position cap | 25% of balance | 8% of bankroll | 25% of capital | 8% of bankroll |
| Kelly fraction | 50% | Adaptive 0.5×-2× | 50% | Adaptive |
| Fee/cost rate | 0.0 | 0.031 (spread) | 0.0 | depends on instantiation |
| Formula | edge/variance | (p-c)/(1-c) | edge/(1-fee)/variance | (p-c)/(1-c) |
| Min size floor | $5 | None | None | None |

**Impact:** Different call paths produce wildly different position sizes. The 25% cap allows $2,500 positions on $10k balance, while the 8% cap allows only $800.

**Spec to fix:**
```python
# Consolidate to ONE position sizer: core/kelly_position_sizer.py
# Remove: core/position_sizing.py, core/fee_aware_kelly_position_sizing.py
# In paper_trading_engine.py, ensure only one import path:
from core.kelly_position_sizer import KellyPositionSizer
```

---

### ERROR 6: Fee Deduction Double-Counted in Daily Reconciliation

**What:** `fee_cost` is calculated at trade execution and included in `net_cost`, then `total_fees` is calculated again from `fee_rate` in `daily_reconciliation()` and subtracted from the closing balance.

**Where:**
- `core/paper_trading_engine.py` line 1734-1735: `fee_cost = abs(position_size * self.fee_rate)` → `net_cost = position_size + fee_cost * ...`
- `core/paper_trading_engine.py` line 2377: `total_fees = sum(abs(t[0]) * self.fee_rate for t in today_all_trades)`
- `core/paper_trading_engine.py` line 2385: `closing_balance = prev_balance + today_realized_pnl + total_unrealized_pnl - total_fees`

**Why wrong:** The `fee_cost` is already embedded in the `net_cost` recorded for each trade. When `daily_reconciliation()` recalculates fees from `fee_rate` and subtracts them again, fees are double-counted.

**Impact:** With `fee_rate=0.0` this is dormant. But enabling fees will systematically understate the balance.

**Spec to fix:**
```python
# Remove the separate fee calculation from daily reconciliation.
closing_balance = prev_balance + today_realized_pnl + total_unrealized_pnl
```

---

### ERROR 7: Risk Manager Records a "Loss" on Every Trade Execution

**What:** After trade execution, `net_cost` (the cost of the trade, always positive) is passed to `RiskManager.update_after_trade()` as if it were P&L.

**Where:** `core/paper_trading_engine.py` lines 1760-1766

```python
trade_result = TradeResult(
    trade_id=trade_uuid,
    pnl=net_cost,  # Note: this is the cost, not P&L
    is_profitable=net_cost > 0,  # Always True for buys
    trade_date=date
)
```

**Why wrong:** The comment says "not finalized P&L" but the code passes it anyway. For a BUY_YES trade, `net_cost = position_size + fee_cost` — this is always positive, so `is_profitable=True`. The risk manager thinks every execution is profitable. True P&L is only known at settlement.

**Impact:** The risk manager's consecutive loss count, drawdown tracking, and daily loss limit are all based on meaningless P&L. The `max_consecutive_losses=3` check will never trigger.

**Spec to fix:**
```python
# Remove the risk update from execute_trade().
# Add it to _process_settlements() instead:
trade_result = TradeResult(
    trade_id=trade_uuid,
    pnl=realized_pnl,  # Actual settlement P&L
    is_profitable=realized_pnl > 0,
    trade_date=settlement_date
)
self._risk_manager.update_after_trade(trade_result)
```

---

### ERROR 8: Sharpe Ratio Is Unannualized

**What:** The Sharpe ratio is calculated as `mean(PnL) / std(PnL)` without annualization.

**Where:** `core/stop_loss.py` lines 183-194

```python
return mean_pnl / std  # No annualization factor
```

**Why wrong:** The threshold `SHARPE_STOP_THRESHOLD = 0.5` is meaningless without annualization. A daily Sharpe of 0.5 is excellent (annualized ~7.9), while 0.5 annualized is mediocre. The daily Sharpe is typically 0.02-0.10 for a profitable strategy, meaning the stop condition `sharpe < 0.5` will never trigger.

**Impact:** The Sharpe stop condition is effectively disabled.

**Spec to fix:**
```python
def _calculate_rolling_sharpe(self, trades, annualization_factor=252):
    daily_sharpe = mean_pnl / std
    return daily_sharpe * math.sqrt(annualization_factor)
```

---

### ERROR 9: Agreement Gate Ignores Confidence Levels

**What:** `AgreementGate.filter_signals()` counts votes without weighting by confidence, and the `min_confidence` parameter (default 0.25) is defined but never used.

**Where:** `core/agreement_gate.py` line 48

```python
def filter_signals(self, signals, min_confidence=0.25):
```

**Why wrong:** The `min_confidence` parameter is accepted but never checked against individual signal confidence scores. The method only counts direction votes. A signal with confidence 0.26 counts the same as one with 0.90. This defeats the purpose of a confidence-weighted ensemble.

**Impact:** Low-quality signals have equal voting power with high-quality ones. The N-of-M threshold is satisfied by any N signals, regardless of quality.

**Spec to fix:**
```python
def filter_signals(self, signals, min_confidence=0.25):
    """Filter signals: only count signals with confidence >= min_confidence."""
    valid_signals = [s for s in signals if self._get_confidence(s) >= min_confidence]
    # ... rest of voting logic on valid_signals only
```

---

### ERROR 10: `position_sizing.py` Docstring Claims 8 Signals but Uses Different Formula

**What:** The docstring says "Previous 8 signal types supported" and claims "Formula: Kelly fraction = edge / (1 - fee) / variance" but the implementation computes `adjusted_edge = edge - self.fee_rate` and then `kelly_fraction = adjusted_edge / variance_estimate`. This is not the formula stated in the docstring.

**Where:** `core/position_sizing.py` lines 15-20 and 161-165

**Docstring:**
```
Formula: Kelly fraction = edge / (1 - fee) / variance
```

**Implementation:**
```python
adjusted_edge = edge - self.fee_rate
kelly_fraction = (adjusted_edge / variance_estimate)
```

**Why wrong:** The docstring states `edge / (1 - fee) / variance` but the code implements `edge - fee / variance`. These are mathematically different. The docstring formula would be correct; the code formula is incorrect.

**Impact:** Another Kelly formula discrepancy. This file is one of the four competing implementations.

**Spec to fix:** Either remove this file (recommended, per Improvement 1) or fix the formula to match the docstring: `kelly_fraction = edge / (1.0 - self.fee_rate) / variance_estimate`.

---

## IMPROVEMENTS (5+)

### IMPROVEMENT 1: Consolidate to a Single Position Sizing System

**What to change:** Remove three of the four position sizing implementations. Keep only `core/kelly_position_sizer.py` (corrected formula, Bayesian belief, adaptive multiplier, 8% cap). Remove `core/position_sizing.py` and `core/fee_aware_kelly_position_sizing.py`. Update all imports.

**Why better:** Eliminates risk of importing the wrong module. The surviving implementation has the correct formula, proper Bayesian belief updates, and drawdown protection.

**Effort:** Medium (2-3 hours). Files are self-contained but imports need auditing.

**Spec for implementation:**
1. Delete `core/position_sizing.py` (after auditing all imports)
2. Delete `core/fee_aware_kelly_position_sizing.py` (after auditing all imports)
3. Update `paper_trading_engine.py` to import only from `core.kelly_position_sizer`
4. Run full test suite to catch any import errors

---

### IMPROVEMENT 2: Validate Kelly Sizer Inputs with Sanity Checks

**What to change:** Add input validation to `compute_position_size` in `kelly_position_sizer.py`: assert `0.0 <= confidence <= 1.0`, `0.0 <= win_rate <= 1.0`, `bankroll > 0`. Log warnings when inputs are suspicious.

**Why better:** Prevents the errors above (Error 2, Error 3) from silently passing through. The sizer currently accepts `win_rate = 3.5` (if `analytical_prob` happened to be > 1.0) and produces nonsensical results.

**Effort:** Low (30 minutes). Add 5-10 lines of validation.

**Spec for implementation:**
```python
def compute_position_size(self, confidence, win_rate, edge):
    # Input validation
    if not 0.0 <= confidence <= 1.0:
        _LOGGER.error("Invalid confidence=%f — clamping to [0,1]", confidence)
        confidence = max(0.0, min(1.0, confidence))
    if not 0.0 <= win_rate <= 1.0:
        _LOGGER.error("Invalid win_rate=%f — clamping to [0,1]", win_rate)
        win_rate = max(0.0, min(1.0, win_rate))
    if self.bankroll <= 0:
        _LOGGER.error("Zero or negative bankroll=%f — cannot size", self.bankroll)
        return 0.0, {"error": "invalid bankroll"}
    # ... rest of method
```

---

### IMPROVEMENT 3: Add Per-Trade Stop-Loss and Take-Profit

**What to change:** Add individual trade stop-loss (e.g., close position if price drops below 50% of entry) and take-profit (e.g., close if price exceeds 90% of max payout). Implement in `RiskManager` or a new `TradeExitManager`.

**Why better:** The system currently has aggregate stop conditions (drawdown, consecutive losses) but no per-trade risk management. A single bad trade can lose 100% of its position value.

**Effort:** Medium (4-5 hours). Requires adding exit logic to the paper trading engine, storing entry prices, and monitoring market prices.

**Spec for implementation:**
```python
class TradeExitManager:
    def __init__(self, stop_loss_pct=0.50, take_profit_pct=0.90):
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
    
    def check_exit(self, trade, current_market_price):
        """
        Check if a trade should be closed early.
        For BUY_YES at price p: 
          - stop loss if current_price < p * (1 - stop_loss_pct)
          - take profit if current_price > 1.0 - (1.0 - p) * (1 - take_profit_pct)
        """
        if trade.trade_type == TradeType.BUY_YES:
            entry_price = trade.filled_price
            if current_market_price < entry_price * (1.0 - self.stop_loss_pct):
                return "stop_loss"
            if current_market_price > 1.0 - (1.0 - entry_price) * (1.0 - self.take_profit_pct):
                return "take_profit"
        return None
```

---

### IMPROVEMENT 4: Add Position-Level Correlation and Concentration Limits

**What to change:** Add a max-allocation-to-any-single-station limit (e.g., 20% of bankroll) and a max-allocation-to-any-correlation-cluster limit (e.g., 40% of bankroll). Implement in `RiskManager` and check before trade execution.

**Why better:** The system currently has cluster exposure and city-pair checks, but no single-station concentration limit. If 18 of 20 stations have signals on the same day, the system could allocate 100% of bankroll to correlated positions.

**Effort:** Low (1-2 hours). Extend existing `_get_cluster_exposure` and `_get_city_pair_exposure` methods.

**Spec for implementation:**
```python
def check_concentration_limits(self, station, market_type, proposed_size, date):
    """Check all concentration limits before entering a trade."""
    # 1. Single station limit (20% of bankroll)
    station_exposure = self._get_station_exposure(station, date)
    if station_exposure + proposed_size > self.bankroll * 0.20:
        return False, "Station exposure limit exceeded"
    
    # 2. Single market type limit (15% of bankroll)
    market_exposure = self._get_market_type_exposure(market_type, date)
    if market_exposure + proposed_size > self.bankroll * 0.15:
        return False, "Market type exposure limit exceeded"
    
    # 3. Correlation cluster limit (40% of bankroll)
    cluster = get_cluster_for_station(station)
    cluster_exposure = self._get_cluster_exposure(cluster, date)
    if cluster_exposure + proposed_size > self.bankroll * 0.40:
        return False, "Cluster exposure limit exceeded"
    
    return True, "All limits passed"
```

---

### IMPROVEMENT 5: Add Settlement P&L Audit Trail and Alerting

**What to change:** After settlement, log the actual P&L breakdown (entry_price, strike_price, settlement_value, payout, fees, net_PnL) to the trade journal. Add alerting when realized P&L deviates significantly from expected P&L at entry.

**Why better:** Currently, settlement P&L is calculated and stored in the trades table, but there's no structured comparison with the expected P&L at trade entry. This makes it impossible to diagnose whether P&L errors are from mispriced entries, wrong settlement values, or calculation bugs.

**Effort:** Low (1-2 hours). Add a few columns to the trade journal and a comparison function.

**Spec for implementation:**
```python
def _log_settlement_audit(self, trade_uuid, settlement_details):
    """Log settlement audit trail to trade journal."""
    self._journal.append_entry(
        station=settlement_details['station'],
        market=settlement_details['market_type'],
        direction=settlement_details['direction'],
        outcome=JournalOutcome.SETTLED_WIN if settlement_details['realized_pnl'] > 0 else JournalOutcome.SETTLED_LOSS,
        alert_id=trade_uuid,
        position_size=settlement_details['position_size'],
        metadata={
            'entry_price': settlement_details['entry_price'],
            'strike_price': settlement_details['strike_price'],
            'settlement_value': settlement_details['settlement_value'],
            'payout': settlement_details['payout'],
            'realized_pnl': settlement_details['realized_pnl'],
            'expected_pnl_at_entry': settlement_details['expected_pnl'],
            'pnl_variance': settlement_details['realized_pnl'] - settlement_details['expected_pnl'],
        }
    )
```

---

## IDEAS (3+)

### IDEA 1: Implement Dynamic Strike Selection Based on Confidence-Weighted Expected Value

**The idea:** Instead of always trading the nearest-to-the-money strike, select the strike that maximizes `expected_value * (1 - probability_of_fill)` given the current confidence level. High-confidence predictions should trade further out-of-the-money (higher leverage, higher EV per dollar), while low-confidence predictions should trade closer to the money.

**Expected benefit:** Better risk-adjusted returns. The current system trades at the market price (which is the probability-weighted average of all strikes) but doesn't select which strike to trade. By selecting the optimal strike, we can get higher EV per dollar of risk.

**Risk:** Requires understanding the full Kalshi ladder (all strikes and their prices), which adds complexity. The optimal strike changes with market conditions.

**Spec for validation:**
1. Add a `StrikeSelector` module that takes (forecast_temp, confidence, market_ladder) and returns the optimal strike.
2. Backtest: for each historical epoch, compute the optimal strike vs. the "market price" strike, and compare returns.
3. Measure: Sharpe ratio improvement, win rate, max drawdown.
4. Gate: only deploy if optimal-strike Sharpe > market-price Sharpe by ≥ 0.2.

---

### IDEA 2: Implement a Volatility-Adjusted Position Sizer Using At-The-Money Implied Volatility

**The idea:** Use the Kalshi market's implied volatility (derived from the option chain) to adjust position sizes. When implied volatility is high (wide spread between strikes), reduce position size. When implied volatility is low, increase position size. This is the option-trading equivalent of the Kelly criterion adjusting for variance.

**Expected benefit:** More consistent risk-adjusted returns. The current system ignores market-implied volatility, which is a key input to position sizing in any options market.

**Risk:** Requires fetching the full option chain, which increases API calls. Implied volatility estimation from binary options is non-trivial.

**Spec for validation:**
1. Implement `implied_volatility_from_ladder(market_ladder)` — compute IV from the slope of the option chain.
2. Modify `kelly_position_sizer.py` to accept a volatility multiplier: `position_size *= (target_vol / current_vol)`.
3. Backtest: compare volatility-adjusted sizing vs. fixed sizing.
4. Gate: only deploy if volatility-adjusted Sharpe > baseline Sharpe.

---

### IDEA 3: Build a Cross-Sectional Mean-Reversion Overlay for Station Pairs

**The idea:** Add a mean-reversion overlay that trades the spread between correlated station pairs. For example, if KDEN (Denver) and KOKC (Oklahoma City) are both forecasting HIGH temperature, and the Kalshi market for KDEN is at 0.85 while KOKC is at 0.65, the spread is abnormally wide based on their historical correlation. Trade the convergence: short KDEN, long KOKC.

**Expected benefit:** Additional return stream uncorrelated with directional predictions. The signals are already correlated (same weather pattern), so the spread should be tighter than the sum of individual errors.

**Risk:** Correlation regime changes break the relationship. Requires careful station pair selection and robust correlation estimation.

**Spec for validation:**
1. Build a correlation matrix of the 20 stations from historical settlement data.
2. Identify the top 5 most tightly correlated pairs (r > 0.7).
3. For each pair, compute the historical spread distribution (difference in market probabilities).
4. When the current spread exceeds 2 standard deviations from the mean, trade the convergence.
5. Backtest: paper trade the overlay for 90 days. Measure: Sharpe, win rate, correlation with directional strategy.
6. Gate: only deploy if overlay Sharpe > 0.5 and correlation with main strategy < 0.3.

---

## ELEPHANTS (2+)

### ELEPHANT 1: The P&L Calculation Is Wrong and Invalidates All Performance Metrics

**What:** The settlement P&L calculation treats the temperature bucket as a binary outcome (above/below 50°F) rather than comparing against the contract's strike price. See Error 1 for the full analysis.

**Why matters:** This is not a minor bug — it fundamentally invalidates every P&L figure, win rate, Sharpe ratio, and performance metric the system produces. The system cannot distinguish between a profitable trade (buying a 85°F strike at 0.55 when the actual high is 87°F) and a losing trade (buying the same strike when the actual high is 83°F). All trades are judged by whether the temperature is above or below 50°F, which is almost always true in the summer and almost always false in the winter for most US cities.

**What happens if ignored:**
- The system will appear to have a 90%+ win rate in summer months (all cities above 50°F)
- The system will appear to have a <10% win rate in winter months (all cities below 50°F)
- The "win rate" is entirely driven by seasonality, not by signal quality
- Any attempt to optimize the system will optimize for the wrong objective
- The Gray Room's entire evaluation framework (accuracy, confidence calibration, Sharpe) is based on garbage P&L

**Spec for resolution:**
1. **Immediate fix (1 day):** Add a `strike_price` column to the `trades` table. At trade execution, record the market's strike price (from the ticker or market metadata). In `_process_settlements()`, compare `settlement_bucket >= strike_price` to determine contract payout.
2. **Validation (1 day):** Re-run settlement for all historical trades with the corrected logic. Compare old vs. new P&L. The difference should be dramatic.
3. **Audit (1 day):** Generate a report of P&L by station, by month, and by strike. This will reveal which signals were actually profitable.
4. **Gate:** Do not deploy any further trading logic changes until this is fixed and validated.

---

### ELEPHANT 2: No Separation Between Signal Generation and Trading/Execution Layers

**What:** The paper trading engine (`paper_trading_engine.py` at 3104 lines) conflates signal generation, position sizing, execution, settlement, P&L calculation, risk management, journaling, and daily reconciliation into a single monolithic class. The `kelly_position_sizer.py` (imported by the engine) has its own conflicting position sizing formula. The `stop_loss.py` has its own risk management. The `risk_controls.py` has yet another. None of these are cleanly separated into a pipeline.

**Why matters:** This architecture makes it impossible to:
- **Test in isolation:** You can't test the position sizing without running the full engine
- **Swap components:** You can't replace the Kelly sizer without modifying the engine
- **Compare strategies:** You can't run two different sizing strategies side-by-side
- **Backtest cleanly:** The backtest engine (`p3_backtest_engine.py`) doesn't use the same P&L logic as the live engine
- **Audit:** You can't see the full decision chain (signal → size → fill → P&L) in one place

**What happens if ignored:**
- The system becomes increasingly fragile as more features are added
- Each new signal type requires modifying the 3104-line engine
- The risk of conflicting implementations grows (as we already see with 4 position sizers)
- The backtest results diverge further from live trading results
- Debugging becomes prohibitively expensive

**Spec for resolution:**
1. **Refactor into a trading pipeline (1-2 weeks):**
   ```
   TradingPipeline:
     - SignalGenerator: produces (station, market, direction, confidence, prob)
     - PositionSizer: takes (signal, bankroll, win_rate) → position_size
     - TradeFilter: applies risk checks, concentration limits, fee filters
     - TradeExecutor: places trade, records to DB
     - SettlementProcessor: watches for settlement, realizes P&L
     - Journal: records all decisions and outcomes
     - PnLCalculator: computes P&L from position data (single source of truth)
   ```
2. **Extract P&L calculation (3 days):** Create a `PnLCalculator` class that computes P&L for any trade given its entry price, strike, settlement value, and trade type. This is the single source of truth for all P&L.
3. **Make backtest use the same P&L (2 days):** Refactor `p3_backtest_engine.py` to use the same `PnLCalculator` as the live engine.
4. **Gate:** Do not add new features until the pipeline separation is complete. The existing architecture cannot support additional complexity.