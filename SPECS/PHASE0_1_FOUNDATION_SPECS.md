# Weather Engine Phase 0 + 1 Foundation Scripts - Specifications
**Date:** 2026-07-03  
**Version:** 1.0  
**Status:** First checkpoint completed (P0.1, P0.3, P1.1)

---

## Overview

Core foundation scripts for weather engine, built for deterministic operation with mandatory version tracking as required by durable rules in `memory/projects/weather-engine-execution-model.md`. 

Key principles implemented:
- No AI/ML in core prediction/trading loop (AI models parked in Phase 3)
- Mandatory `trade_version` and `functionality` fields on all trades 
- Standalone script architecture
- Version-tagged analytics for performance attribution

---

## P0.1: Live METAR Collection (`scripts/metar_collect_live.py`)

### Purpose
Collects live METAR observations from NOAA AWC for all 20 Kalshi stations, storing to `metar_observations` table for continuity with NWP collection starting 2026-07-03.

### Features
- Standalone cron-friendly script
- Handles AWC URL format with proper User-Agent headers
- Basic METAR parsing (temperature, dew point, wind, pressure, visibility)
- Database storage with conflict resolution
- Rate limiting and error handling
- Date-stamped observations

### Design
```bash
# Run as: python3 scripts/metar_collect_live.py
# Suggested cron: 0 3 * * * /usr/bin/python3 [path]/scripts/metar_collect_live.py
```

### Output
- Saves records to `metar_backfill.db:metar_observations` table
- Daily report showing success/failure rates and DB state
- Timestamped observations with source tracking

---

## P0.3: Paper Trading Engine (`core/paper_trading_engine.py`)  

### Purpose
Deterministic-only paper trading engine with mandatory version tracking, designed for testing signals before live deployment. No AI/ML in decision loop.

### Features
- **Mandatory Fields**: Every trade has `trade_version` and `functionality` for complete traceability
- **Version Tagging**: Analytics tied to specific algorithm versions
- **Daily Reconciliation**: P&L, position counts, trade metrics
- **Deterministic Logic**: Signals based on historical frequency only
- **Position Tracking**: Maintains running positions with averaging

### Key Components
1. **Signal Generation**: `generate_signals()` - deterministic only based on historical patterns
2. **Trade Placement**: `place_paper_trade()` - with market vs fair value calculations  
3. **Position Management**: Track open/close states
4. **Reconciliation**: Daily P&L and performance reporting

### Usage
```bash
# Run daily: python3 -c "from core.paper_trading_engine import daily_paper_run; daily_paper_run()"
```

### Algorithm Example (`v1.0_det_signals`)
1. **Prior Reversion**: Bet against prior day movements > 2°F
2. **Calendar Patterns**: Use historical calendar day tendencies
3. **Fair Value Calc**: Historical probability vs market price comparison

---

## P1.1: Climatology Pillar (`core/climatology_pillar.py`)

### Purpose
Historical base rate calculation per bucket + station + calendar date, providing the foundational probability estimates used by paper trading engine.

### Features
- **Calendar Date Analysis**: Historical tendency for MM-DD across years
- **Bucket Probabilities**: Frequency of temperature ranges (0-20°F, 20-30°F, etc.)
- **Station-Specific**: Separate analysis per Kalshi station
- **Seasonal Indices**: Adjustment based on typical calendar behavior
- **Variance Measures**: Historical volatility and change patterns

### Key Methods
- `get_station_calendar_base_rates(station, month_day)`  
  - Returns probability of up/down moves and bucket distribution
  - Includes sample size validation and smoothing
- `get_full_calendar_climatology()` 
  - Aggregated report for all 20 stations
  - Daily analysis summary

### Integration Point
Used by paper trading to calculate `analytical_prob`:  
`P(direction | station, calendar_date) + seasonal adjustments`

---

## Compliance with Durable Rules

### Checked Items ❓
- [X] Backfill, backtesting, and paper trading remain as scripts only
- [X] AI/ML modeling items parked for Phase 3  
- [X] Mandatory version tracking in paper trading
- [X] Deterministic logic only (no AI in prediction loop)
- [X] All code suitable for manual execution

### Script Architecture 🧱
All foundation scripts are designed for:
- Manual execution by Dan or cron systems
- No agent-driven execution
- Clear output and logging
- Error isolation and handling
- Version tracking and reproducibility

---

## Next Up: P0.2, P0.4, P1.2-P1.5

### P0.2: NWP 30-day backfill script  
- Enhance existing `scripts/nwp_backfill_30d.py` to ensure proper historical collection

### P0.4: Split-backtest harness
- Enhance existing `scripts/split_backtest.py` with additional signal isolation

### P1.2: Late-day METAR plateau detection
- Algorithm to detect stabilization patterns in daily METAR readings for same-day contracts

### P1.3: Cross-platform pricing divergence  
- Comparison of Kalshi vs other platforms for equivalent contracts

### P1.4: Explicit market vs analytical output
- Clear statement of "market implied prob vs analytical fair value + confidence"

### P1.5: Calibration dashboard integration
- Integration of Brier/ECE metrics into paper-trading reports

---

**Author:** Gilfoyle (AI Agent)  
**Approval:** Requires Dan Gabriel review for P1 deliverables before Phase 2