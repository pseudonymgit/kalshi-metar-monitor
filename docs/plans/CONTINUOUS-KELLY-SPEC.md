# Continuous Kelly Criterion — Position Sizing Design Spec

**Status:** Design Spec | **Owner:** Gilfoyle | **Priority:** Phase 21.2  
**Source:** Gray Room R13 Expert 4 (Quant Finance), R14 Synthesis  
**Replaces:** Discrete 3-tier sizing ($10/$20/$50) in sweep scripts & trading engine  
**Version:** 1.0 — 2026-08-06

---

## Table of Contents

1. [Motivation & Current State](#1-motivation--current-state)
2. [Kelly Fraction Formula](#2-kelly-fraction-formula)
3. [Capital Base](#3-capital-base)
4. [Fractional Kelly & Max Leverage](#4-fractional-kelly--max-leverage)
5. [Rebalancing Frequency](#5-rebalancing-frequency)
6. [Drawdown Rules](#6-drawdown-rules)
7. [Station & Signal Concentration Limits](#7-station--signal-concentration-limits)
8. [Fee-Aware Kelly (Kalshi Fee Model)](#8-fee-aware-kelly-kalshi-fee-model)
9. [Implementation Plan](#9-implementation-plan)
10. [Configuration Reference](#10-configuration-reference)
11. [Migration Path](#11-migration-path)

---

## 1. Motivation & Current State

### Current System (to be replaced)

The engine currently uses a **discrete 3-tier position sizing** approach derived from the sweep:

```python
# scripts/sweep/kalshi_sweep_eval.py — current behavior
if position_sizing == "kelly":
    kelly_pct = edge / (1.0 - entry_price) if entry_price < 1.0 else 0
    n_contracts = int(min(max_contracts, max(1, kelly_pct * kelly_fraction * 1000)))
else:  # fixed
    n_contracts = min(max_contracts, 100)
```

And in the core `PositionSizer`:

```python
# core/position_sizer.py — edge-based tiered sizing
EDGE_TIERS = [
    (0.10, 1.00, "100% Kelly — strong edge"),
    (0.06, 0.75, "75% Kelly — moderate edge"),
    (0.03, 0.50, "50% Kelly — weak edge"),
]
# plus confidence tiers
CONFIDENCE_TIERS = [
    (0.80, 2.0),   # ≥ 80% → 2×
    (0.70, 1.5),   # 70–80% → 1.5×
    (0.60, 1.0),   # 60–70% → 1×
    (0.00, 0.5),   # < 60% → 0.5×
]
```

**Problems with discrete tiers:**
- Step-function cliff edges at each threshold (a 0.0599 edge gets 50% Kelly; 0.0601 gets 75%).
- No smooth allocation of capital as edge changes continuously.
- Confidence multiplier and edge multiplier compound non-linearly with unpredictable results.
- Cannot gracefully handle partial positions or multi-signal portfolio allocation.

### Goal

Replace with **continuous Kelly criterion** producing smooth, mathematically optimal position sizes as a continuous function of edge and confidence.

---

## 2. Kelly Fraction Formula

### 2.1 Binary Kelly (Correct Formula)

For binary weather market contracts:

- **Long (buy YES):** Profit = $(1 - P)$ per contract if correct, lose $P$ per contract if wrong.
- **Short (buy NO):** Profit = $P$ per contract if correct, lose $(1 - P)$ per contract if wrong.

The standard binary Kelly criterion:

```
f* = (p × b - q) / b
```

Where:
- `p` = estimated win probability (from signal)
- `q = 1 - p` = loss probability
- `b` = net odds received on the bet (profit-to-loss ratio)

**For a YES contract at market price M:**

```
b_yes = (1 - M - fee) / M
f*_yes = p - q / b_yes
       = p - (1-p) × M / (1 - M - fee)
```

**For a NO contract (buying "no" at price 1-M):**

```
b_no = (M - fee) / (1 - M)
f*_no = p - q / b_no
      = p - (1-p) × (1-M) / (M - fee)
```

Where `fee` = Kalshi round-trip fee fraction for this trade size (see Section 8).

### 2.2 Simplified Form (Edge-Based)

For practical implementation, when `fee` is small relative to `M`:

```
f* ≈ (p - M - fee) / (1 - M - fee)   # for long (buy YES)
f* ≈ (M - p - fee) / (M - fee)       # for short (buy NO)
```

But the recommended implementation uses the **full binary formula** in Section 2.1, which is correct for all market prices and fee sizes.

### 2.3 Edge Calculation

**Gross edge (net of market price):**

```
edge = |p - M| - fee_t
```

Where `fee_t` = estimate of round-trip fee fraction. If `edge <= 0`, no trade.

**Conservative edge (using CI lower bound):**

For small sample sizes (`N < 300` trades), use the **lower bound of the 95% Agresti-Coull confidence interval** instead of the point estimate:

```
p_adj = (wins + 2) / (n + 4)          # Agresti-Coull adjusted p
z = 1.96
p_lower = p_adj - z × sqrt(p_adj × (1 - p_adj) / (n + 4))
p_conservative = max(p_lower, 0.5 + fee)
edge_conservative = |p_conservative - M| - fee_t
```

Above 300 trades, use the point estimate `p_raw = wins / n`.

### 2.4 Python Implementation (Reference)

```python
def binary_kelly_fraction(
    win_prob: float,
    market_price: float,
    fee_fraction: float,
    direction: str,  # "long" (buy YES) or "short" (buy NO)
) -> float:
    """
    Compute optimal Kelly fraction for a binary weather market.

    Returns fraction of capital to risk (0.0 to 1.0).
    Returns 0.0 if no positive edge after fees.

    For long (buy YES): b = (1 - M - fee) / M
    For short (buy NO):  b = (M - fee) / (1 - M)
    """
    if win_prob <= 0.0 or win_prob >= 1.0:
        return 0.0
    if market_price <= 0.0 or market_price >= 1.0:
        return 0.0

    q = 1.0 - win_prob

    if direction == "long":
        if market_price >= (1.0 - fee_fraction):
            return 0.0  # can't overcome fee
        b = (1.0 - market_price - fee_fraction) / market_price
    elif direction == "short":
        if market_price <= fee_fraction:
            return 0.0
        b = (market_price - fee_fraction) / (1.0 - market_price)
    else:
        return 0.0

    f_star = (win_prob * b - q) / b

    return max(0.0, f_star)
```

---

## 3. Capital Base

### 3.1 Three-Level Capital Hierarchy

| Level | Amount | Purpose | Defined In |
|---|---|---|---|
| **Global Bankroll** | `global_bankroll` (initial: $10,000 USD) | Total trading capital | `config.json` / env var |
| **Lane Budget** | `lane_budget = global_bankroll × lane_fraction` | Per-signal-lane allocation | `LaneConfig.lane_fraction` |
| **Station Budget** | `station_budget = lane_budget × station_max_pct` | Per-station per-lane limit | `KellyConfig.station_max_pct` (default 20%) |

### 3.2 Calculation Order

```
1. Determine direction and Kelly fraction for signal → f*_raw
2. Apply per-signal modifiers → f*_adjusted
3. Convert to dollars:
     proposed_size_usd = f*_adjusted × station_budget
4. Apply station cap:
     station_size_usd = min(proposed_size_usd, station_budget × station_max_pct)
5. Apply lane cap (sum of open positions in lane):
     lane_size_usd = min(station_size_usd, remaining_lane_budget)
6. Apply global cap:
     final_size_usd = min(lane_size_usd, remaining_global_bankroll)
7. Floor to minimum trade unit:
     final_size_usd = max(final_size_usd, min_trade_usd)
```

### 3.3 Lane Budget Fractions

Default lane fractions (sum must equal 1.0):

| Lane | Fraction | Reasoning |
|---|---|---|
| Persistence | 0.15 | Historical baseline, decent but limited |
| NWP Direct | 0.25 | Primary weather model output |
| Goldilocks | 0.15 | GEFS ensemble mean + physics features |
| Trajectory | 0.15 | Analogue-based trajectory matching |
| Late-Day Momentum | 0.10 | Hourly momentum near settlement |
| Frontal Passage | 0.10 | Event-driven short-term signals |
| NWP Analog | 0.05 | KNN-based analog matching |
| Other signals | 0.05 | Residual category |

### 3.4 Implementation

```python
@dataclass
class CapitalPool:
    """Three-level capital tracking."""
    global_bankroll: float
    lane_budgets: Dict[str, float]   # lane_name → allocated $
    lane_in_play: Dict[str, float]   # lane_name → currently at risk $
    station_in_play: Dict[str, float]  # station_icao → currently at risk $

def compute_remaining_lane_budget(pool: CapitalPool, lane: str) -> float:
    return pool.lane_budgets.get(lane, 0.0) - pool.lane_in_play.get(lane, 0.0)

def compute_remaining_station_budget(pool: CapitalPool, station: str) -> float:
    return pool.station_in_play.get(station, 0.0)  # open exposure only
```

---

## 4. Fractional Kelly & Max Leverage

### 4.1 Base Fractional Kelly

Default: **0.25 Fractional Kelly** (quarter-Kelly). This is conservative enough to survive long losing streaks typical of 67% win rate systems.

```python
BASE_FRACTIONAL_KELLY = 0.25  # configurable, default quarter-Kelly
```

### 4.2 Effective Kelly (Cascading Modifiers)

```
f*_effective = f*_raw × BASE_FRACTIONAL_KELLY × epoch_modifier × drawdown_modifier × concentration_modifier
```

Each modifier is a float in (0.0, 1.0]. The product is clamped to `MAX_EFFECTIVE_FRACTION` (0.15 by default, meaning max 15% of station budget per trade).

### 4.3 Epoch Modifier (Time-of-Day Schedule)

| Window | Hours to Settlement | Modifier | Rationale |
|---|---|---|---|
| 00Z–06Z | 18–24h | 0.50 | Full uncertainty window, many GEFS cycles ahead |
| 06Z–12Z | 12–18h | 0.65 | First refresh confirms/adjusts direction |
| 12Z–18Z | 6–12h | 0.85 | Two confirmations in, high conviction |
| 18Z–24Z | 0–6h | 0.40 | Late cycle — less time to react if wrong, lower liquidity |

This replaces the `epoch_multiplier` parameter that `compute_position_size()` already accepts but which is currently hardcoded to 1.0.

### 4.4 Max Allowed Fraction

Hard caps (enforced after all modifiers):

| Constraint | Value | Source |
|---|---|---|
| `MAX_EFFECTIVE_FRACTION` | 0.15 (15% of station budget) | Configurable, defaults to 0.15 |
| `MAX_CONTRACTS` | 500 per trade | Kalshi exchange limits |
| `MIN_ENTRY_PRICE` | 0.05 | Configurable |
| `MAX_ENTRY_PRICE` | 0.95 | Configurable |

### 4.5 Position Size Floor

```python
MIN_POSITION_SIZE_USD = 10.0   # $10 minimum trade
MIN_CONTRACTS = 1              # single contract minimum
```

If `final_size_usd` computes to less than `MIN_POSITION_SIZE_USD`, round down to 0 (no trade). This prevents noise-level positions that would lose money to fees.

---

## 5. Rebalancing Frequency

### 5.1 Event-Driven Re-Evaluation

Not time-driven. Re-evaluate only when new information arrives:

| Trigger | Action | Cost Impact |
|---|---|---|
| GEFS refresh (~every 4h) | Full re-eval: compute new confidence, edge, Kelly fraction | Full round-trip friction cost if position changes |
| METAR observation update (hourly) | **Modulate confidence only** — no new trade | Zero (no trade change) |
| Frontal passage detected | Increase/decrease confidence by fixed amount | Zero (probability adjustment only) |
| Spatial coherence change | Adjust confidence by coherence delta | Zero (probability adjustment only) |
| Dewpoint confirmation | Moderate confidence within existing bounds | Zero (probability adjustment only) |
| Settlement (end of day) | Close position, record P&L, update rolling stats | Exit fee only |

### 5.2 Position Change Threshold

To prevent churn from tiny edge fluctuations, require:

```
Δf*_effective > POSITION_STICKINESS_THRESHOLD (default 0.01)
```

...before a new position size replaces the existing one. If the change is below threshold, keep the current position.

Additionally, require:

```
|new_size - current_size| / current_size > 0.10  (10% relative change)
```

...for any position modification. If either threshold is unmet, maintain the existing position. This avoids paying round-trip fees on every GEFS refresh when the edge barely moves.

### 5.3 Edge Decay Schedule

Between `t_0` (signal generation) and `t_settle` (settlement), edge decays linearly:

```
t_decay = (t_settle - t_now) / (t_settle - t_0)
edge_remaining = original_edge × t_decay
```

Apply the decayed edge to Kelly calculation at each re-eval. This prevents stale signals from being traded at full size near settlement.

---

## 6. Drawdown Rules

### 6.1 Two-Layer Drawdown Protection

| Layer | Threshold | Action |
|---|---|---|
| **Daily Loss Limit** | 5% of global bankroll in one trading day | Halt all new trades for rest of day. Close any open positions at next scheduled settlement. |
| **Trailing Drawdown** | 15% from peak global bankroll | Halve all Kelly fractions until recovery to within 10% of peak. |

### 6.2 Daily Loss Mechanics

```
if daily_net_pnl < -DAILY_LOSS_LIMIT × global_bankroll:
    # DAILY_HALT: no new trades today
    # But do NOT force-close existing positions — let them settle naturally
    set_trading_flag("daily_halt", expires="next_day_00Z")
    log_alert("DAILY HALT: PnL drawdown exceeded 5% in single day")
```

Daily loss limit resets daily at 00Z (or station-local midnight for the station's timezone).

### 6.3 Trailing Drawdown Mechanics

```
peak_bankroll = max(peak_bankroll, global_bankroll)
current_drawdown = (peak_bankroll - global_bankroll) / peak_bankroll

if current_drawdown >= TRAILING_DRAWDOWN_HALVE_THRESHOLD:  # 15%
    drawdown_modifier = 0.5  # halve all Kelly fractions
else:
    drawdown_modifier = 1.0

# Recovery condition
if current_drawdown < TRAILING_DRAWDOWN_RECOVERY_THRESHOLD:  # 10%
    drawdown_modifier = 1.0  # restore full sizing
```

### 6.4 Stop Trading Threshold

If drawdown reaches 25% from peak, **stop all trading** and notify Dan:

```
if current_drawdown >= STOP_TRADING_THRESHOLD:  # 25%
    set_trading_flag("circuit_breaker", reason="25% drawdown from peak")
    halt_all_new_trades()
    log_alert("CIRCUIT BREAKER: 25% drawdown — manual restart required")
```

Trading can only resume via explicit config override or manual flag clearance.

### 6.5 P&L Tracking Updates

After each settlement:

```python
def update_bankroll_after_settlement(
    pool: CapitalPool,
    trade_result: Dict,
) -> None:
    """
    Update bankroll after a settled trade.
    - Global bankroll changes by net P&L
    - Lane budget recalculated as: lane_fraction × new_global_bankroll
    - Peak bankroll updated for drawdown tracking
    """
    pool.global_bankroll += trade_result["net_pnl"]
    pool._peak_bankroll = max(pool._peak_bankroll, pool.global_bankroll)

    # Rebalance lane budgets proportionally
    for lane, fraction in LANE_FRACTIONS.items():
        pool.lane_budgets[lane] = pool.global_bankroll * fraction
```

---

## 7. Station & Signal Concentration Limits

### 7.1 Per-Station Cap

Maximum capital at risk for any single station (across all signal lanes):

```
STATION_MAX_PCT = 0.20  # 20% of global bankroll
```

So with $10,000 bankroll, max exposure per weather station = $2,000.

### 7.2 Per-Signal Lane Cap (within a station)

Maximum capital at risk per signal lane per station:

```
LANE_PER_STATION_MAX_PCT = 0.50  # 50% of that lane's budget
```

So for a lane with $2,000 budget, max per-station per-lane = $1,000.

### 7.3 Multi-Signal Fusion Within Same Station

When multiple signal lanes fire for the same station simultaneously:

1. Compute pro-rata allocation: each lane gets their indicated size, scaled down proportionally to stay within the station cap.
2. If the fused position exceeds the station cap, scale all lanes equally.

```python
def compute_fused_position(
    lane_signals: List[Dict],
    station_cap: float,
    lane_budgets_remaining: Dict[str, float],
) -> float:
    """
    Pro-rata scaling when multiple lanes fire for the same station.
    """
    total_indicated = sum(s["proposed_size"] for s in lane_signals)
    if total_indicated <= station_cap:
        return total_indicated

    scaler = station_cap / total_indicated
    for s in lane_signals:
        s["final_size"] = s["proposed_size"] * scaler
    return station_cap
```

### 7.4 Correlation Cluster Cap

When stations are in the same geographic cluster (same state/region), the GEFS forecasts are highly correlated. Limit cluster exposure:

```
CLUSTER_MAX_PCT = 0.35  # 35% of global bankroll per cluster
```

Clusters are defined in `station_registry.py` `get_cluster_for_station()`.

### 7.5 Liquidity Tier Discount

From `scripts/sweep/tiers.py`, `TIER_DISCOUNTS`:

| Tier | Stations | Edge Discount |
|---|---|---|
| 1 (Liquid) | KNYC, KLAX, KMDW, KDCA, KATL, KDFW, KPHL, KBOS | 0% |
| 2 (Moderate) | KDEN, KLAS, KSFO, KSEA, KMIA, KHOU | 25% |
| 3 (Thin) | KPHX, KMSP, KMSY, KAUS, KSAT, KOKC | 50% |

Edge discount applies **before** Kelly calculation:

```
edge_after_liquidity = raw_edge × (1.0 - tier_discount)
```

---

## 8. Fee-Aware Kelly (Kalshi Fee Model)

### 8.1 Kalshi Fee Formula

Kalshi charges a **taker fee** per side:

```
fee_per_side = ceil(0.07 × contracts × P × (1-P)) / 100
```

Where `contracts` is the number of contracts, `P` is the price, and the result is in dollars (the `/100` converts from cents).

### 8.2 Fee as a Fraction of Position

For a position of size `S = contracts × entry_price`:

```
entry_fee_frac = 0.07 × entry_price × (1 - entry_price) × (S / entry_price) / S
               = 0.07 × (1 - entry_price)
```

Wait — more precisely, in fractional terms:

```python
def compute_fee_fraction(contracts: int, price: float) -> float:
    """Kalshi taker fee as fraction of notional."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_cents = math.ceil(0.07 * contracts * price * (1.0 - price))
    fee_dollars = fee_cents / 100.0
    notional = contracts * price
    return fee_dollars / notional if notional > 0 else 0.0
```

For a round trip (entry + exit):

```
round_trip_fee_frac = entry_fee_frac + exit_fee_frac
```

The `exit_fee_frac` is harder to predict because the exit price is unknown. Conservative approach: assume exit at the settlement price ($1 or $0):

```python
# At exit, price is either 1.0 (win) or 0.0 (lose)
# fee = ceil(0.07 × C × P × (1-P))
# At P=1.0 or P=0.0, fee = $0 (because P×(1-P) = 0)
exit_fee_dollars = 0.0  # settlement at 0 or 1
```

So in practice, **only entry fee matters** for round-trip fee calculation. This means:

```
fee_fraction = compute_fee_fraction(contracts, entry_price)
```

### 8.3 Iterative Fee Calculation

Position size and fees are interdependent (fee depends on contracts, contracts depend on fee-adjusted edge). Solve iteratively:

```python
def find_optimal_position(
    win_prob: float,
    market_price: float,
    bankroll: float,
    fractional_kelly: float = 0.25,
    max_iterations: int = 10,
    tolerance: float = 0.01,
) -> Tuple[int, float]:
    """
    Iteratively find optimal position accounting for Kalshi fee structure.

    Returns (contracts, position_cost_usd).
    """
    contracts = 0
    for _ in range(max_iterations):
        # Fee fraction given current contract count
        fee_frac = compute_fee_fraction(contracts, market_price)

        # Binary Kelly fraction
        f_raw = binary_kelly_fraction(win_prob, market_price, fee_frac, "long")

        # Apply modifiers
        f_effective = f_raw * fractional_kelly

        # Convert to contracts
        proposed_contracts = int(f_effective * bankroll / market_price)
        proposed_contracts = max(0, min(proposed_contracts, MAX_CONTRACTS))

        # Check convergence
        if abs(proposed_contracts - contracts) <= tolerance:
            contracts = proposed_contracts
            break

        contracts = proposed_contracts

    cost = contracts * market_price + (compute_fee_fraction(contracts, market_price) * contracts * market_price)
    return contracts, cost
```

### 8.4 Simpler Approximation

For implementation simplicity in the sweeps, the current `kalshi_fee()` in `kalshi_sweep_eval.py` works fine:

```python
def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price)) / 100.0
```

The iterative approach in Section 8.3 only needs to be used in the **live trading engine**, not in backtests/sweeps (where a single pass is acceptable).

---

## 9. Implementation Plan

### Phase 1: Core Math Module (`core/continuous_kelly.py`)

Create new file `core/continuous_kelly.py` with:

```python
"""
continuous_kelly.py — Continuous Kelly Criterion Position Sizing

Replaces discrete tiered sizing with smooth, mathematically optimal
binary Kelly allocation.

Phase 21.2 — Deterministic math only. No AI/ML.

Usage:
    from core.continuous_kelly import ContinuousKellySizer, KellyConfig
    sizer = ContinuousKellySizer(bankroll=10000.0)
    contracts, details = sizer.compute("KNYC", win_prob=0.67, market_price=0.55)
"""
```

**Functions to implement:**

| Function | Purpose |
|---|---|
| `binary_kelly_fraction(win_prob, market_price, fee_frac, direction)` | Core formula (Section 2.1) |
| `compute_conservative_win_prob(wins, losses, min_trades=300)` | CI lower bound (Section 2.3) |
| `compute_fee_fraction(contracts, price)` | Kalshi fee formula (Section 8) |
| `find_optimal_position(win_prob, market_price, ...)` | Iterative solver (Section 8.3) |
| `compute_epoch_modifier(epoch_hour)` | Time-of-day schedule (Section 4.3) |
| `compute_edge_decay(signal_time, settle_time, original_edge)` | Edge decay (Section 5.3) |
| `compute_liquidity_discount(station_id)` | Tier discount (Section 7.5) |

**Class: `ContinuousKellySizer`**

| Method | Purpose |
|---|---|
| `__init__(bankroll, config)` | Initialise with pool and config |
| `compute(station, lane, win_prob, market_price, ...)` | Main public method |
| `_apply_concentration_limits(station, lane, size)` | Clamp per-station/per-lane |
| `_update_drawdown()` | Track trailing drawdown |
| `get_drawdown_state()` | Return current drawdown status |
| `record_settlement(trade_result)` | Update bankroll after settlement |

**Config dataclass:**

```python
@dataclass
class KellyConfig:
    """Continuous Kelly configuration."""
    # Capital
    global_bankroll: float = 10000.0
    lane_fractions: Dict[str, float] = field(default_factory=lambda: {
        "persistence": 0.15,
        "nwp_direct": 0.25,
        "goldilocks": 0.15,
        "trajectory": 0.15,
        "late_day_momentum": 0.10,
        "frontal": 0.10,
        "nwp_analog": 0.05,
        "other": 0.05,
    })

    # Fractional Kelly
    base_fractional_kelly: float = 0.25
    max_effective_fraction: float = 0.15

    # Drawdown
    daily_loss_limit: float = 0.05      # 5%
    trailing_drawdown_halve: float = 0.15   # 15%
    trailing_drawdown_recover: float = 0.10  # 10%
    stop_trading_drawdown: float = 0.25     # 25%

    # Concentration
    station_max_pct: float = 0.20       # 20% per station
    lane_per_station_max_pct: float = 0.50  # 50% per lane per station
    cluster_max_pct: float = 0.35       # 35% per cluster
    position_stickiness: float = 0.01   # min change to modify position
    position_relative_change: float = 0.10  # 10% relative change threshold

    # Trade filters
    min_win_prob_for_trade: float = 0.51
    min_edge_for_trade: float = 0.01    # 1% edge minimum after fees
    min_position_size: float = 10.0
    max_contracts: int = 500

    # Sample size
    ci_transition_n: int = 300  # switch from CI lower bound to point estimate

    # Fee model
    kalshi_fee_rate: float = 0.07       # 0.07 per side
    entry_price_min: float = 0.05
    entry_price_max: float = 0.95
```

### Phase 2: Integrate into Sweep Script

Modify `scripts/sweep/kalshi_sweep_eval.py`:

1. **Replace fixed/kelly position sizing choice** with `ContinuousKellySizer`.
2. **Remove `"kelly"` vs `"fixed"`** as a sampled parameter.
3. **Add new sampled parameters:**
   - `base_fractional_kelly` (range: 0.05 to 0.75)
   - `trailing_drawdown_halve` (range: 0.10 to 0.30)
   - `station_max_pct` (range: 0.05 to 0.30)
   - `position_stickiness` (range: 0.0 to 0.05)
4. **Add `epoch_modifier_enabled`** boolean parameter.
5. **Add `conservative_win_prob_enabled`** boolean parameter.

Change `simulate_trade()` to:

```python
def simulate_trade_with_continuous_kelly(
    config, station, date_str, gefs_mean_f, actual_temp_f, prev_temp_f, epoch_hour
):
    """Use ContinuousKellySizer instead of discrete fixed/kelly sizing."""

    # ... direction detection, confidence calculation same as before ...

    # Continuous Kelly sizing
    kelly_sizer = ContinuousKellySizer(
        bankroll=10_000.0,
        config=KellyConfig(
            base_fractional_kelly=config["base_fractional_kelly"],
            station_max_pct=config["station_max_pct"],
            position_stickiness=config["position_stickiness"],
            trailing_drawdown_halve=config["trailing_drawdown_halve"],
        ),
    )

    # Apply epoch modifier
    epoch_mod = compute_epoch_modifier(epoch_hour) if config.get("epoch_modifier_enabled", True) else 1.0

    contracts, details = kelly_sizer.compute(
        station=station,
        lane="nwp_direct",
        win_prob=confidence,
        market_price=entry_price,
        epoch_modifier=epoch_mod,
    )
```

### Phase 3: Integrate into Live Trading Engine

Modify `core/paper_trading_engine.py` (and `multi_stage_execution.py`):

1. **Replace `compute_position_size()` calls** from `position_sizing.py` with `ContinuousKellySizer`.
2. **Thread `epoch_hour`** through the signal pipeline.
3. **Track open positions per lane** for concentration limit enforcement.
4. **Add daily P&L tracking** for daily loss limit enforcement.
5. **Add `drawdown_modifier`** to the sizing cascade.
6. **Update `PositionSizingConfig`** to reference `KellyConfig`.

### Phase 4: Replace PositionSizer (Deprecation Path)

After phases 1-3 are verified:

1. Add deprecation warning to `core/position_sizer.py`.
2. Move edge-based tier logic into a compatibility shim.
3. Point all callers to `core/continuous_kelly.py`.
4. After 2 weeks with no issues, remove tiered sizing.

### Phase 5: Tests

Add test file `tests/test_continuous_kelly.py`:

| Test Case | What It Validates |
|---|---|
| `test_binary_kelly_long_at_50pct` | At p=0.50, M=0.50, fee=0 — f* should be 0 |
| `test_binary_kelly_long_at_edge` | At p=0.60, M=0.50 — verify positive f* |
| `test_binary_kelly_short` | Short-direction formula symmetry |
| `test_binary_kelly_fee_aware` | Fee reduces Kelly fraction correctly |
| `test_conservative_win_prob` | CI lower bound < point estimate for small n |
| `test_ci_transition` | After 300 trades, CI ≈ point estimate |
| `test_epoch_modifier` | Correct schedule mapping |
| `test_concentration_limits` | Per-station cap enforcement |
| `test_fused_position_pro_rata` | Multi-signal scaling |
| `test_daily_loss_halt` | >5% daily loss stops new trades |
| `test_trailing_drawdown_halve` | 15% drawdown halves sizes |
| `test_circuit_breaker` | 25% drawdown stops all trading |
| `test_iterative_fee_convergence` | Fee calculation converges |
| `test_liquidity_tier_discount` | Tier 3 station gets 50% edge discount |
| `test_edge_decay` | Linear decay toward settlement |
| `test_position_stickiness` | Small changes don't trigger re-trade |
| `test_invalid_inputs` | Edge cases (price=0, p=1, etc.) |

---

## 10. Configuration Reference

### 10.1 Config JSON Schema

The sweep LHS samples these continuous variables. Default values for production:

```json
{
  "continuous_kelly": {
    "base_fractional_kelly": 0.25,
    "max_effective_fraction": 0.15,
    "daily_loss_limit": 0.05,
    "trailing_drawdown_halve": 0.15,
    "trailing_drawdown_recover": 0.10,
    "stop_trading_drawdown": 0.25,
    "station_max_pct": 0.20,
    "lane_per_station_max_pct": 0.50,
    "cluster_max_pct": 0.35,
    "position_stickiness": 0.01,
    "min_edge_for_trade": 0.01,
    "min_position_size": 10.0,
    "max_contracts": 500,
    "ci_transition_n": 300,
    "epoch_modifier_enabled": true,
    "conservative_win_prob_enabled": true,
    "entry_price_min": 0.05,
    "entry_price_max": 0.95,
    "kalshi_fee_rate": 0.07
  }
}
```

### 10.2 LHS Sweep Ranges

For the next sweep run, these are the recommended sampling ranges for continuous Kelly parameters:

| Parameter | Range | Type | Notes |
|---|---|---|---|
| `base_fractional_kelly` | 0.05 – 0.75 | Uniform continuous | Quarter-Kelly (0.25) expected sweet spot |
| `trailing_drawdown_halve` | 0.10 – 0.30 | Uniform continuous | Current 0.15 in range |
| `station_max_pct` | 0.05 – 0.30 | Uniform continuous | Current 0.20 in range |
| `position_stickiness` | 0.0 – 0.05 | Uniform continuous | 0.01 default |
| `epoch_modifier_enabled` | True/False | Bernoulli (0.5) | Compare with/without |
| `conservative_win_prob_enabled` | True/False | Bernoulli (0.5) | Compare with/without |
| `ci_transition_n` | 100 – 500 | Integer uniform | 300 default |

Fixed parameters (not sampled):

| Parameter | Value | Rationale |
|---|---|---|
| `daily_loss_limit` | 0.05 | Standard risk management |
| `stop_trading_drawdown` | 0.25 | Hard circuit breaker |
| `cluster_max_pct` | 0.35 | Geographic diversification |
| `kalshi_fee_rate` | 0.07 | Kalshi fixed rate |
| `entry_price_min` | 0.05 | Market constraint |
| `entry_price_max` | 0.95 | Market constraint |

---

## 11. Migration Path

### Step 1: Parallel Run (Days 1-3)
- Deploy `core/continuous_kelly.py` as a **side-by-side module**.
- Log both the old tiered position size and the new continuous Kelly size to `trade_journal` for every signal.
- No change to actual trading yet.
- Verify: continuous Kelly sizes are within ±30% of tiered sizes for the same inputs.

### Step 2: Paper Trading Validation (Days 4-7)
- Switch paper trading (SBOX) to use `ContinuousKellySizer`.
- Run 3-4 full trading days.
- Validate: no edge-case crashes, no zero-divisions, concentration limits enforce correctly.
- Compare P&L trajectory vs old tiered sizing on replay.

### Step 3: Sweep Re-Evaluation (Day 8)
- Re-run `kalshi_sweep_eval.py` with continuous Kelly parameters added to LHS.
- Identify optimal `base_fractional_kelly` value from sweep.
- Gate: must show Sharpe ≥ 1.0 and profit factor ≥ 1.5 on best config.

### Step 4: Production Switch (Day 9)
- Flip config switch: `position_sizing_model: "tiered"` → `"continuous_kelly"`.
- Keep old sizer as fallback with config flag `fallback_to_tiered: true`.
- Monitor: position sizes, trade frequency, P&L.

### Step 5: Deprecation (Day 14)
- Add deprecation warning to `core/position_sizer.py`.
- Remove edge-based tiered sizing after 2 weeks of stable continuous Kelly operation.
- Delete `core/position_sizer.py` or move to `core/archive/`.

---

## Appendix A: Full Sizing Cascade (Execution Order)

```
Signal Event
    │
    ├─ 1. Compute direction (UP/DOWN) from signal
    ├─ 2. Compute win_prob from signal confidence + calibration
    ├─ 3. Apply CI lower bound (if n < ci_transition_n)
    ├─ 4. Fetch market price M from Kalshi
    ├─ 5. Apply liquidity tier discount to edge
    ├─ 6. Compute fee fraction from Kalshi formula
    ├─ 7. binary_kelly_fraction(win_prob, M, fee, direction)
    ├─ 8. Apply epoch modifier (time-of-day)
    ├─ 9. Apply edge decay (time-to-settlement)
    ├─ 10. Apply drawdown modifier (if in drawdown)
    ├─ 11. Apply fractional Kelly (base_fractional_kelly)
    ├─ 12. Clamp to max_effective_fraction
    ├─ 13. Convert dollars → contracts
    ├─ 14. Apply per-station concentration cap
    ├─ 15. Apply per-lane-per-station cap
    ├─ 16. Apply cluster concentration cap
    ├─ 17. Apply global bankroll remaining cap
    ├─ 18. Check position change threshold vs. current position
    ├─ 19. Floor to min_position_size / min_contracts
    └─ 20. Submit order (or no-op if below threshold)
```

## Appendix B: Comparison Table — Old vs New

| Aspect | Old (Tiered) | New (Continuous Kelly) |
|---|---|---|
| Edge → Kelly | Step function at 3 thresholds | Smooth continuous mapping |
| Fee model | Fixed `0.0205` round-trip | Kalshi `ceil(0.07×C×P×(1-P))` each side |
| Kelly formula | `(p - c) / (1 - c)` | Full binary Kelly `(p×b - q)/b` |
| Fractional Kelly | 0.50 (half-Kelly) | 0.25 (quarter-Kelly, configurable) |
| Time-of-day | None (flat) | 4-epoch schedule (0.50/0.65/0.85/0.40) |
| Drawdown | 10% → halve, 25% → halt | Two-layer: 5% daily + 15% trailing |
| Win prob | Point estimate | CI lower bound for n < 300 |
| Per-station limit | Implicit via $500 max | Explicit 20% of bankroll |
| Multi-signal | Accept all independently | Pro-rata fused allocation |
| Edge decay | None | Linear decay to settlement |
| Position stickiness | None | 1% absolute / 10% relative threshold |
| Liquidity tiers | Separate sweep module | Integrated into Kelly calculation |
| Bankroll cap | 5% of global | 15% max effective fraction (cascading) |

---

*End of spec. Gilfoyle — build `core/continuous_kelly.py` per Phase 1, wire into sweep script per Phase 2.*