# Paper Trading Alert Builder Implementation - B-MODE v2

## Implementation Summary

**Date:** 2026-07-13  
**Version:** 2.0 (B-MODE)  
**File:** `core/alert_builder.py`  
**Modified:** `core/paper_trading_engine.py`

## What Was Implemented

### 1. Alert Builder Module (`core/alert_builder.py`)

A new module implementing the finalized alert format with:

#### Opportunity Grade Scale (S/A/B/C/D/F)
- **S** - Exceptional (Edge ≥ 12%, Sharpe ≥ 2.0)
- **A** - Excellent (Edge ≥ 9%, Sharpe ≥ 1.8)
- **B** - Good (Edge ≥ 6%, Sharpe ≥ 1.5)
- **C** - Average (Edge ≥ 3%, Sharpe ≥ 1.2)
- **D** - Marginal (Edge ≥ 1%, Sharpe ≥ 1.0)
- **F** - Weak (Edge < 1%, Sharpe < 1.0)

#### Lane Variants
1. **Regular** - Standard signals (confidence 50-70%)
2. **Sure_Thing** - High confidence (≥70%)
3. **Goldilocks** - Tier 1 protected signals

#### Key Functions

```python
compute_opportunity_grade(trade_confidence, market_prob, Sharpe)
    - Computes Opportunity Grade (S/A/B/C/D/F)
    - Edge = Trade Conf - Market prob
    - Returns (grade, edge_value)

classify_lane(trade_confidence, signal_type)
    - Classifies signal into lane
    - Goldilocks: Tier 1 protected signals
    - Sure_Thing: High confidence (≥70%)
    - Regular: Standard confidence (50-70%)

build_paper_trade_alert(trade_result, station, market_type, direction, instance)
    - Builds slim paper-trade alert with full metadata
    - Returns Dict with content, grade, edge, lane, Sharpe, etc.

build_paper_trade_alert_dev(trade_result, station, market_type, direction, instance)
    - DEV variant with enhanced format
    - More compact for quick review

format_alert_for_discord(alert_data)
    - Formats alert for Discord webhook delivery
    - Returns payload with content, embeds, username
```

### 2. Paper Trading Engine Integration (`core/paper_trading_engine.py`)

Added:

#### Import
```python
from alert_builder import (
    build_paper_trade_alert,
    format_alert_for_discord,
    OpportunityGrade,
    LaneType,
    PAPER_TRADE_ALERT_SCHEMA_VERSION,
    build_paper_trade_alert_dev,
)
```

#### New Methods
```python
def build_paper_trade_alert(self, trade_result, station, market_type, direction)
    - Wrapper for alert_builder.build_paper_trade_alert
    - Uses INSTANCE for the alert

def build_paper_trade_alert_dev(self, trade_result, station, market_type, direction)
    - Wrapper for alert_builder.build_paper_trade_alert_dev

def compute_sharpe(self, trades=None)
    - Computes Sharpe ratio from trades
    - Used for Opportunity Grade calculation

def _get_daily_trades(self)
    - Helper to get trades for Sharpe calculation
```

#### Modified Methods
- `place_paper_trade`: Now includes `sharpe` in returned result
- Alert building integrated into trade execution output
- Slim layout with highlighted Trade Conf %, Market prob, Sharpe line
- Edge calculation: Trade Conf - Market prob
- Real Kalshi URLs in alerts

### 3. Alert Format (Slim Layout)

**Standard Format:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[DEV] 📍 Station: KDEN
📊 Market: HIGH
📈 Direction: UP
💰 Size: $125.50
✅ Trade Conf: HIGH (78%)
📉 Market prob: 62.00%
📈 Edge: +16.00%
📊 Sharpe: 2.3
 Opportunity Grade: **S**
 Lane: Sure Thing
🔗 Market: https://kalshi.com/markets/KXHIGHDEN-26JUL13
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v2.1 | Functionality: late_day_momentum_hourly
Trade UUID: test-uuid-123
```

**DEV Variant (More Compact):**
```
[DEV] 📍 KLAX | Market: LOW | DOWN
Size: $75.25 | Conf: MEDIUM (65%)
Market prob: 55.00% | Edge: +10.00%
Sharpe: 1.5 | Grade: **B** | Lane: Regular
https://kalshi.com/markets/KXLOWLAX
v2.1 | calendar_climatology
```

## Testing

Run: `python3 scripts/test_alert_builder.py`

All tests pass with expected output.

## B-MODE Features

1. **Opportunity Grade + Edge**: S/A/B/C/D/F grade with Edge calculation
2. **Slim Layout**: Compact, high-signal format
3. **Three Lane Variants**: Regular, Sure_Thing, Goldilocks
4. **Real Kalshi URLs**: Direct links to markets
5. **Sharpe Line**: Highlighted Sharpe ratio for quick assessment
6. **B-MODE v2**: Full schema version 2.0
7. **Main Lane**: Production-ready format
8. **DEV First**: Implement in DEV, then propagate to PROD/SBOX

## Files Modified/Created

- `core/alert_builder.py` - New module
- `core/paper_trading_engine.py` - Updated with alert builder integration
- `scripts/test_alert_builder.py` - Test script

## Next Steps

1. Review alert format in DEV
2. Adjust thresholds if needed
3. Deploy to PROD/SBOX after validation
4. Update documentation in docs/ALERT-SCHEMA-V1.0.md
