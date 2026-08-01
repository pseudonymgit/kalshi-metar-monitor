# ADVANCE Signals — Design & Build Status

**Date:** 2026-08-01  
**Status:** Implementation Complete (Phase 1 — Stub + Design Doc)

---

## 1. Spread-Based Entry (<1d)

**File:** `core/signals/spread_based_entry_signal.py`

### What it does
Detects when the Kalshi market bid/ask spread narrows enough for profitable entry before settlement. Trades the convergence between market price and expected settlement value.

### Key logic
- `check()`: Evaluates spread vs historical percentile (25th percentile threshold), profit margin after costs, volume gate ($500/24h), time-to-settlement gate (1h min)
- `check_exit()`: Detects spread re-widening (1.5× entry spread) or stop loss (30%)
- Position sizing: 2% of 24h volume, scaled by compression strength, max $200

### Status
- [x] Signal module (full implementation)
- [ ] `scripts/advance/spread_based_entry_backtest.py` — backtest logic needed
- [ ] Backtesting against WhaleWatch historical spread data
- [ ] Paper trading integration

---

## 2. Intraday METAR Nowcasting (<1d)

**File:** `core/signals/metar_nowcast_signal.py`

### What it does
Runs real-time temperature tracking from hourly METAR feeds. Compares current METAR temperature against the forecasted daily max/min (from GEFS ensemble) to generate a confidence-weighted nowcast.

### Key logic
- `evaluate()`: Fetches latest METAR from `metar_archive.db`, checks freshness (<2h old), computes distance to forecasted extreme
- `evaluate_bucket()`: Refines confidence for a specific temperature bucket
- If temp within 3°F of forecasted max → HIGH signal, within 3°F of forecasted min → LOW signal
- Exceeded forecast by >2°F → triggers re-evaluation

### Status
- [x] Signal module (full implementation)
- [ ] METAR DB ingestion cron must be running
- [ ] GEFS ensemble integration for forecasted max/min
- [ ] Live METAR feed verification

---

## 3. HRRR Bias-Corrected (2-3d)

**File:** `core/signals/hrrr_bias_corrected_signal.py`

### What it does
Fetches HRRR model data (3km resolution, hourly) via Open-Meteo API and applies rolling bias correction per station.

### Key logic
- `get_forecast()`: Fetches 3-day hourly forecast from Open-Meteo (free, no API key), stores in `hrrr_forecasts` table
- `get_station_bias()`: Computes rolling 14-day bias from recorded actual temperatures vs HRRR predictions
- `apply_bias_correction()`: Adjusts forecast by station bias (capped at ±5°F)
- `get_daily_extremes()`: Returns bias-corrected max/min for a target date with confidence score
- `record_settlement()`: Records actual temperature at settlement for bias learning loop

### Status
- [x] Signal module (full implementation)
- [x] Bias database auto-init
- [ ] First bias warmup period (14 days of recorded actuals needed)
- [ ] Settlement callback integration for bias recording
- [ ] Rate limit verification (Open-Meteo: 10k req/day free)

---

## Next Steps

### Phase 2 — Backtesting
- Create `scripts/advance/` directory with 3 backtest scripts:
  - `spread_based_entry_backtest.py` — loads historical order book data from WhaleWatch, simulates entries
  - `metar_nowcast_backtest.py` — replays historical METAR records vs forecasted settlements
  - `hrrr_bias_warmup.py` — fetches historical HRRR data and builds initial bias estimates

### Phase 3 — Paper Trading Integration
- Wire all 3 signals into `core/paper_trading_engine.py`
- Add signal-type attribution column to paper_trading_dev.db
- Run paper trading for 1 week minimum per signal before live

### Phase 4 — Live
- Add cron entries for HRRR refresh (every 3 hours, when new HRRR runs are available: 01, 04, 07, 10, 13, 16, 19, 22 UTC)
- METAR poller runs every hour (existing `weatherapi_backfill.py` or equivalent)
- Spread entry runs on every WhaleWatch cycle (30s polling)