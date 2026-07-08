# RISK GUARDRAILS (Marty's Phase 1 B1.5)

Configuration, kill-switch logic, and operational guidelines for paper trading risk controls.

## Overview

This document describes the configurable risk controls implemented in the paper trading engine to prevent excessive losses and ensure operational discipline.

## Configurable Risk Thresholds

All thresholds are defined in `core/risk_controls.py` and can be adjusted via environment variables.

### Threshold Configuration

| Parameter | Default | Environment Variable | Description |
|-----------|---------|---------------------|-------------|
| `max_daily_loss` | 300 | `MAX_DAILY_LOSS` | Maximum loss in USD before halting trading |
| `max_drawdown_pct` | 10.0 | `MAX_DRAWDOWN_PCT` | Maximum drawdown percentage before full suspension |
| `consecutive_loss_limit` | 5 | `CONSECUTIVE_LOSS_LIMIT` | Consecutive losses triggering kill switch |
| `correlation_threshold` | 0.70 | `CORRELATION_THRESHOLD` | Correlation threshold for same-direction trades |
| `signal_conflict_threshold` | 0.95 | `SIGNAL_CONFLICT_THRESHOLD` | Signal conflict threshold |
| `signal_conflict_days` | 2 | `SIGNAL_CONFLICT_DAYS` | Days of signal conflict before kill |

### How to Adjust Configuration

#### Via Environment Variables (Recommended for Production)

```bash
# Example: Tighten risk controls
export MAX_DAILY_LOSS=200
export MAX_DRAWDOWN_PCT=5.0
export CONSECUTIVE_LOSS_LIMIT=3
```

#### Via Direct Configuration

Edit `core/risk_controls.py` and modify the `RiskConfig` defaults:

```python
@dataclass(frozen=True)
class RiskConfig:
    max_daily_loss: float = 300.0  # Change this value
    max_drawdown_pct: float = 10.0  # Change this value
    # ...
```

## Kill Switch Triggers

### 1. Max Daily Loss (B1.5.1)
- **Trigger**: Daily loss exceeds `max_daily_loss` (default $300)
- **Action**: Halt trading + alert
- **Recovery**: Reset risk metrics after review

### 2. Max Drawdown Percentage (B1.5.1)
- **Trigger**: Drawdown from peak balance ≥ `max_drawdown_pct` (default 10%)
- **Action**: Full suspension of trading
- **Recovery**: Manual intervention required

### 3. Consecutive Losses (B1.5.1)
- **Trigger**: 5+ consecutive losing trades
- **Action**: Kill switch activation
- **Recovery**: Risk metrics reset after analysis

### 4. Correlation Risk (B1.5.3)
- **Trigger**: Correlation > 0.70 in same direction
- **Action**: Alert and monitor
- **Recovery**: Reduce exposure until correlation normalizes

### 5. Signal Conflict (B1.5.3)
- **Trigger**: Signal conflict > 0.95 for 2+ days
- **Action**: Alert and monitor
- **Recovery**: Review signal logic and confidence thresholds

## Station Gating (B1.5.2)

### Approved Stations

Only the following stations are eligible for trading:

- **KATL** - Atlanta
- **KBOS** - Boston
- **KMDW** - Chicago
- **KSEA** - Seattle
- **KSFO** - San Francisco
- **KJFK** - New York (via KNYC)
- **KLAX** - Los Angeles

### Implementation

The station approval gate is enforced in `place_paper_trade()`:
- All stations must pass `is_station_approved()` check
- Non-approved stations return `skipped` status with reason

## Risk Report Interface

### `risk_report()` Method

Returns a dict with:
```python
{
    "exposure_usd": float,
    "daily_pnl_usd": float,
    "max_drawdown_pct": float,
    "peak_balance_usd": float,
    "current_balance_usd": float,
    "consecutive_losses": int,
    "kill_switch_enabled": bool,
    "risk_state": "ok" | "warning" | "suspended" | "killed",
    "kill_switch_reasons": List[str],
    "config": {...},
    "timestamp_utc": str,
}
```

### `format_risk_alert()` Method

Formats risk metrics as a human-readable alert string for logging and notifications.

## Risk State Machine

```
OK (default)
  ↓ (daily loss threshold breached)
WARNING (some thresholds breached)
  ↓ (kill switch trigger activated)
KILLED (trading halted)
  ↓ (manual reset via risk_metrics.reset())
OK (back to normal operation)
```

## Usage in Paper Trading Engine

### Initialize Risk Controls

```python
trader = PaperTrader()
trader._risk_config  # Access current configuration
```

### Check Risk State Before Trading

```python
should_halt, reasons = trader.check_kill_switches()
if should_halt:
    print(trader.format_risk_alert())
    return  # Halt trading
```

### Update Risk Metrics After Trade

```python
# Automatically called after successful trade
# Can also be called manually:
trader.update_risk_metrics_on_trade(trade_pnl, 'win' | 'loss')
```

### Generate Risk Report

```python
report = trader.risk_report()
print(f"Current balance: ${report['current_balance_usd']:,.2f}")
print(f"Risk state: {report['risk_state']}")
```

## Operational Procedures

### Daily Review

1. Check risk report after daily reconciliation
2. Verify risk state is OK or WARNING (not KILLED)
3. Review kill switch reasons if triggered
4. Document any risk-related decisions

### Kill Switch Recovery

When kill switch is triggered:

1. **Halt trading immediately** - No further trades allowed
2. **Review risk report** - Identify which threshold(s) triggered
3. **Assess market conditions** - Are conditions likely to persist?
4. **Make manual decision** - Reset metrics only after review
5. **Reset risk metrics** - Use `trader._risk_metrics = RiskMetrics()` after approval

### Alert Notifications

Risk alerts are logged to instance-specific alert logs:
- PROD: `logs/alerts_prod.jsonl`
- DEV: `logs/alerts_dev.jsonl`
- SBOX: `logs/alerts_sbox.jsonl`

## Implementation Files

- **`core/risk_controls.py`** - Core risk controls and configuration
- **`core/paper_trading_engine.py`** - Integration with trading engine
- **`core/station_registry.py`** - Station approval list (approved_stations)

## Version History

- **v1.0 (2026-07-08)** - Initial implementation (B1.5)
  - Max daily loss, max drawdown, consecutive losses
  - Station gating for 7 approved stations
  - Risk report interface
