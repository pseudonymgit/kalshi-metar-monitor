# B-Mode Cycle 4.5: Accuracy Numbers — All Configs

**Generated:** 2026-08-01  
**Engine:** `core/unified_backtest.py` — walk-forward, 180d train / 30d test  
**Warning:** PAPER ACCURACY — unconfirmed against settlement data. Settlement-validated accuracy will differ systematically (see Cycle 4.4).

---

## Ensemble Configs Scan

| min_conf | min_agree | Accuracy | Fee-Adj | Sharpe | Brier | ECE | Trades | Max DD |
|:--------:|:---------:|:--------:|:-------:|:------:|:-----:|:---:|:------:|:------:|
| 0.0 | 1 | 62.6% | 12.1% | 0.266 | 0.348 | — | 26,921 | 0.110 |
| 0.0 | 2 | 64.4% | 13.9% | 0.319 | 0.322 | — | 20,130 | 0.090 |
| 0.0 | 3 | 66.4% | 15.9% | 0.371 | 0.284 | — | 13,214 | 0.075 |
| 0.3 | 1 | 63.4% | 12.9% | 0.277 | 0.341 | — | 25,009 | 0.110 |
| 0.3 | 2 | 65.7% | 15.2% | 0.338 | 0.309 | — | 18,211 | 0.090 |
| 0.3 | 3 | 66.7% | 16.2% | 0.374 | 0.284 | — | 13,035 | 0.075 |
| 0.5 | 1 | 64.1% | 13.6% | 0.287 | 0.345 | — | 23,502 | 0.121 |
| 0.5 | 2 | 66.9% | 16.4% | 0.356 | 0.312 | — | 16,678 | 0.092 |
| 0.5 | 3 | **68.6%** | **18.1%** | **0.405** | 0.286 | — | 11,510 | **0.073** |
| 0.6 | 1 | 64.6% | 14.1% | 0.296 | 0.350 | — | 22,057 | 0.145 |
| 0.6 | 2 | 68.3% | 17.8% | 0.384 | 0.312 | — | 14,434 | 0.090 |
| 0.6 | 3 | **71.0%** | **20.5%** | **0.456** | 0.282 | — | 8,948 | 0.077 |
| 0.7 | 1 | 65.2% | 14.7% | 0.308 | 0.348 | — | 20,986 | 0.116 |
| 0.7 | 2 | 68.9% | 18.4% | 0.397 | 0.311 | — | 13,215 | 0.117 |
| 0.7 | 3 | **72.3%** | **21.8%** | **0.487** | 0.277 | — | 6,822 | 0.087 |

### Recommendation
- **Conservative:** conf≥0.6, agree≥3 → 71.0% acc, 0.456 Sharpe, 8,948 trades
- **Aggressive:** conf≥0.5, agree≥3 → 68.6% acc, 0.405 Sharpe, 11,510 trades
- **Maximum quality:** conf≥0.7, agree≥3 → 72.3% acc, 0.487 Sharpe, 6,822 trades

---

## Per-Signal Accuracy (conf≥0.6, agree≥2)

| Signal | Accuracy | Trades |
|:-------|:--------:|:------:|
| calendar_climatology | 71.4% | 3,853 |
| pressure_delta | 69.9% | 8,458 |
| forecast_disagreement | 69.5% | 7,909 |
| gaussian | 69.4% | 10,702 |
| gaussian_v2 | 68.9% | 13,438 |

**Note:** Only 5 core signals fire reliably in the 7-signal ensemble. Others (spike_reversion, goldilocks, wind_direction_shift, corrected_pressure_delta, frontal_passage_intraday) fire in fewer trades. See full backtest output for complete breakdown.

---

## Per-Station Accuracy (conf≥0.6, agree≥2)

| Station | Accuracy | Trades | Station | Accuracy | Trades |
|:--------|:--------:|:------:|:--------|:--------:|:------:|
| KMIA | 73.5% | 573 | KDFW | 67.7% | 790 |
| KDEN | 72.4% | 779 | KNYC | 67.7% | 733 |
| KBOS | 71.7% | 711 | KHOU | 67.6% | 752 |
| KDCA | 70.3% | 744 | KMSY | 66.8% | 744 |
| KSAT | 70.2% | 754 | KOKC | 66.7% | 821 |
| KAUS | 69.9% | 761 | KLAX | 66.6% | 572 |
| KSFO | 69.1% | 554 | KPHX | 66.2% | 757 |
| KATL | 68.8% | 714 | KSEA | 63.8% | 672 |
| KMDW | 68.4% | 775 | KLAS | 63.4% | 737 |
| KPHL | 68.4% | 718 | **Avg** | **68.3%** | **14,434** |
| KMSP | 67.9% | 773 | | | |

### Station Difficulty Ranking
1. **Easiest (≥70%):** KMIA, KDEN, KBOS, KDCA, KSAT, KAUS
2. **Average (66-69%):** KSFO, KATL, KMDW, KPHL, KMSP, KDFW, KNYC, KHOU, KMSY, KOKC, KLAX
3. **Hardest (<66%):** KPHX, KSEA, KLAS

---

## Settlement Validation Status

- **`_get_strike_price()`** wired in `pnl_tracking.py` with 3-source resolution
- **settlement_epochs** table: 98,640 rows, 2021-01-01 to 2026-07-26, 29 stations
- **Validation gap:** Finalized settlement records (599) do not overlap with historical API records (5,471) by date — need to extend historical scope
- Settlement-aware backtest validation requires `kalshi_settlements` table or strike price metadata in backtest loop