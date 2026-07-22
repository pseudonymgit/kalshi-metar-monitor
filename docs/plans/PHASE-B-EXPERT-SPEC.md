# Phase B: Calibration & Signal Validation — Expert Specification

**Author:** Meteo Stats / Quant Finance Specialist
**Date:** 2026-07-22
**Status:** Specification
**Prerequisite:** Phase A must complete first (P&L correctness fixes invalidate all existing calibration data)

---

## Table of Contents

1. [Priority 1: Calibration Pipeline — Full 22-Signal Rerun](#p1)
2. [Priority 2: Combinatorial Search with Phase 23 Signals](#p2)
3. [Priority 3: Individual Signal Benchmarking](#p3)
4. [Priority 4: Real Settlement Validation](#p4)
5. [Priority 5: Fix Hardcoded `market_prob = 0.5`](#p5)
6. [Priority 6: Stop Random Data in Calibration Dashboard](#p6)
7. [Success Criteria](#p7)
8. [Appendix: Math Reference](#appendix)

---

## <a name="p1"></a>Priority 1: Calibration Pipeline — Full 22-Signal Rerun

### Current State

The existing calibration pipeline (`core/calibration_pipeline.py`) only processed 5 combos during Phase 6 (see `data/phase6_calibration_validation.json`). The combos tested were:

| Combo | Signals |
|---|---|
| calendar_climatology | `calendar_climatology` |
| calendar_climatology+regime | `calendar_climatology`, `regime` |
| gaussian | `gaussian` |
| calendar_climatology+gaussian | `calendar_climatology`, `gaussian` |
| gaussian+regime | `gaussian`, `regime` |

All 22 signals (defined in `core/signals/__init__.py` lines 30-53) are registered and wired into `core/unified_backtest.py` `BACKTEST_SIGNALS` (lines 29-32), but none of the following have ever been individually calibrated:
- `forecast_disagreement` (existing but only in combo context)
- `wind_direction_shift` (existing but only in combo context)
- `temperature_advection` (Phase 2)
- `frontal_detector` (Phase 2)
- `nwp_direct` (Phase 2)
- `intraday_metar_confirmation` (Phase 18)
- `fogr_reversion` (Phase 18)
- `metar_dtdt` (Phase 18)
- `pressure_tendency` (Phase 18)
- `hrrr_bias_corrected` (Phase 18)
- `esdr` (Phase 18)
- `nwp_dtdt_fusion` (Phase 18)
- `spread_based_entry` (Phase 18)
- `volume_momentum` (Phase 18)
- `settlement_arbitrage` (Phase 18)
- `seasonal_regime` (Phase 23)
- `corrected_pressure_delta` (Phase 23, in `dual_polarity_signal.py`)

### Required Implementation

**Script:** `scripts/phaseB_calibration_pipeline.py` (new file)

#### 1.1 Data Sources

| Source | Path | Purpose |
|---|---|---|
| METAR daily data | `data/metar_backfill.db` — table `metar_observations` | All signal evaluation |
| Settlement epochs | `data/metar_backfill.db` — table `settlement_epochs` | Ground truth (HIGH/LOW direction) |
| Calibrated pipeline | `data/calibrated_pipeline_v3.pkl` | Existing isotonic calibrators (if compatible) |
| Station wind effects | `data/station_wind_effects.json` | For station-specific signal adjustment |
| Diurnal curves | `data/seasonal_diurnal_curves.json` | For diurnal baseline |

#### 1.2 Parameters

| Parameter | Value | Rationale |
|---|---|---|
| `train_days` | 365 (1 year) | Minimum to capture full seasonal cycle |
| `test_days` | 90 (1 quarter) | Walk-forward window |
| `min_conf` | 0.0 | Include all signals for calibration data collection |
| `stations` | All 20 standard stations | Same as `unified_backtest.py` lines 38-40 |
| `use_fusion` | False | Calibrate individual signals, not fusion output |
| `use_time_decay` | False | Calibrate raw signal output, not time-decayed |
| `calibration_min_samples` | 200 (per cell), 400 (global fallback), 800 (global) | From `calibration_pipeline.py` MIN_SAMPLES |

#### 1.3 Pipeline Steps

**Step 1: Data Collection**

For each signal × station × trading day in the backtest window:
- Run `signal.evaluate(idx, days)` through `unified_backtest.py` walk-forward
- Record: `(signal_name, station, date, direction, raw_confidence, was_correct)`
- Store in `CalibrationPipeline.history[(signal, station)]` list

Use the existing `CalibrationPipeline` class in `core/calibration_pipeline.py`:
```python
from core.calibration_pipeline import CalibrationPipeline

calib = CalibrationPipeline(
    signal_names=BACKTEST_SIGNALS,  # all 22
    city_codes=STATIONS,            # all 20
    max_history=10000,
    window_start=0
)
```

For each day idx in walk-forward:
```python
calib.update(signal_name, station, raw_conf, was_correct)
```

**Step 2: Isotonic Regression Fitting (per-signal-per-station)**

For each `(signal, station)` cell with ≥200 samples:
```python
from sklearn.isotonic import IsotonicRegression
iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
iso.fit(X, y)  # X=raw_confs, y=correct_bool (as float)
```

Fallback chain (matching existing `calibrate()` in `calibration_pipeline.py` lines 185-227):
1. Per-signal per-station → 200 min samples
2. Per-signal global → 400 min samples (across all stations)
3. Cross-signal global → 800 min samples (all signals, all stations)
4. Identity (no calibration) — return raw confidence as-is

**Step 3: Metrics Computation**

For each signal × station, compute:

1. **Accuracy** = correct / total
2. **Coverage** = total / max_possible
3. **Brier Score** = (1/N) Σ (P(correct) - outcome)²
4. **ECE** = Σ (|bin_accuracy - bin_confidence| × bin_weight) over 10 equal-width bins
5. **Sharpe Ratio** = using `compute_sharpe()` from `unified_backtest.py` with raw (confidence, correct) pairs

#### 1.4 Output Schema

Write to `data/phaseB_calibration_results.json`:

```json
{
  "metadata": {
    "timestamp": "2026-07-22T...",
    "phase": "B",
    "signals_calibrated": 22,
    "stations": 20,
    "train_days": 365,
    "test_days": 90,
    "total_trading_days_processed": "<int>"
  },
  "per_signal": {
    "calendar_climatology": {
      "raw_accuracy": 0.7028,
      "calibrated_accuracy": 0.7079,
      "raw_brier": 0.2972,
      "calibrated_brier": 0.2921,
      "ece": 0.012,
      "sharpe": 17.49,
      "coverage": 0.50,
      "total_trades": 6131,
      "calibration_cell_count": 20,
      "fallback_level": "per_signal"
    },
    ...
  },
  "per_signal_per_station": {
    "calendar_climatology.KATL": { ... },
    ...
  },
  "aggregate": {
    "avg_raw_accuracy": "<float>",
    "avg_calibrated_accuracy": "<float>",
    "avg_brier": "<float>",
    "avg_ece": "<float>",
    "signals_above_60pct": "<count>",
    "signals_above_65pct": "<count>",
    "signals_with_valid_calibrator": "<count>"
  }
}
```

#### 1.5 File Reference

| File | Lines | Relevant Code |
|---|---|---|
| `core/calibration_pipeline.py` | 1-310 | CalibrationPipeline class, refit(), calibrate(), evaluate_calibration() |
| `core/unified_backtest.py` | 1-260 | run_backtest(), compute_sharpe(), compute_brier(), compute_ece() |
| `core/unified_backtest.py` | 29-32 | BACKTEST_SIGNALS list (all 22 signals) |
| `core/unified_backtest.py` | 38-40 | STATIONS list (all 20 stations) |
| `data/phase6_calibration_validation.json` | Entire file | Previous 5-combo calibration — reference only, will be superseded |

---

## <a name="p2"></a>Priority 2: Combinatorial Search with Phase 23 Signals

### Current State

The existing combinatorial search (`data/phase6_combinatorial_search.json`) tested 127 combinations but only across 7 signals (missing 15, including all Phase 18 and Phase 23 signals). The search only used agreement_level=1 (no multi-signal agreement gates).

### Required Implementation

**Script:** `scripts/phaseB_combinatorial_search.py` (new file)

#### 2.1 Signal Families

Group signals by family to prevent over-testing correlated combos:

| Family | Signals | Count |
|---|---|---|
| **Climatology** | `calendar_climatology` | 1 |
| **Reversion** | `fogr_reversion`, `metar_dtdt`, `nwp_dtdt_fusion`, `spread_based_entry`, `settlement_arbitrage`, `volume_momentum` | 6 |
| **Gaussian** | `gaussian`, `gaussian_v2` | 2 |
| **Pressure** | `pressure_delta`, `corrected_pressure_delta`, `pressure_tendency` | 3 |
| **Wind** | `wind_direction_shift`, `temperature_advection`, `frontal_detector` | 3 |
| **NWP** | `nwp_direct`, `hrrr_bias_corrected`, `esdr` | 3 |
| **Intraday** | `intraday_metar_confirmation` | 1 |
| **Disagreement** | `forecast_disagreement` | 1 |
| **Persistence** | `persistence` | 1 |
| **Seasonal** | `seasonal_regime` | 1 |

#### 2.2 Recommended Subsets to Test

**Subset A — Core 5 (baseline):** Same as Phase 6 for reproducibility:
`calendar_climatology`, `gaussian`, `goldilocks`, `pressure_delta`, `forecast_disagreement`

**Subset B — Core 5 + Reversion:** Add the 6 reversion signals:
Subset A + `fogr_reversion`, `metar_dtdt`, `nwp_dtdt_fusion`, `spread_based_entry`, `settlement_arbitrage`, `volume_momentum`

**Subset C — Core 5 + NWP:** Add NWP and wind family:
Subset A + `nwp_direct`, `hrrr_bias_corrected`, `esdr`, `wind_direction_shift`, `temperature_advection`, `frontal_detector`

**Subset D — All pressure variants:** Test `pressure_delta` vs `corrected_pressure_delta` vs `pressure_tendency` vs all 3 together

**Subset E — Phase 23 integration:** `corrected_pressure_delta` + `seasonal_regime` + diurnal anomaly (from `station_effects.py`), with and without `calendar_climatology`

**Subset F — Full 22-signal ensemble:** All signals, all stations (this is the production target)

#### 2.3 Agreement Levels to Test

| Level | Definition | Min Signals Required |
|---|---|---|
| 1 | Any single signal fires | 1 |
| 2 | ≥2 signals agree on direction | 2 |
| 3 | ≥3 signals agree on direction | 3 |
| 4 | ≥4 signals agree on direction | 4 |
| 5 | All signals agree on direction | N (variable) |

#### 2.4 Combinatorial Search Algorithm

```python
from itertools import combinations

def test_combination(signal_names, agreement_level, min_conf=0.0):
    """
    Run backtest with specific signal subset and agreement level.
    
    Returns: {accuracy, sharpe, brier, ece, trades, coverage}
    """
    # Wire into unified_backtest.py
    result = run_backtest(
        signal_names=signal_names,
        min_conf=min_conf,
        use_fusion=False,
        use_time_decay=False
    )
    return result
```

For each combination size k (1 through 5):
- Generate `C(22, k)` combinations (but capped to prevent combinatorial explosion)
- Use random stratified sampling: ensure representation from each signal family
- Target: 100-200 combos per k, not all C(22,k)

For agreement_level ≥ 2:
- Modify `run_backtest()` in `core/unified_backtest.py` to require minimum agreement:
  ```python
  # After line 150 (signal_outputs collection)
  if len(signal_outputs) < agreement_level:
      continue  # Skip this day, not enough signals firing
  
  # Check direction agreement
  directions = set(d for _, d, _ in signal_outputs)
  if len(directions) > 1:
      continue  # Direction split, no agreement
  ```

#### 2.5 Output Schema

Write to `data/phaseB_combinatorial_search.json`:

```json
{
  "metadata": {
    "timestamp": "2026-07-22T...",
    "phase": "B",
    "signals_available": 22,
    "signals_tested": ["<all 22 names>"],
    "subsets_tested": 6,
    "total_combinations": "<int>",
    "agreement_levels_tested": [1, 2, 3, 5]
  },
  "best_per_subset": {
    "subset_a_core5": {
      "best_combo": ["calendar_climatology", "gaussian"],
      "accuracy": 0.700,
      "sharpe": 20.84
    },
    ...
  },
  "agreement_analysis": {
    "level_1": { "avg_accuracy": "...", "avg_sharpe": "...", "avg_trades": "..." },
    "level_2": { "avg_accuracy": "...", "avg_sharpe": "...", "avg_trades": "..." },
    "level_3": { ... }
  }
}
```

#### 2.6 File Reference

| File | Lines | Relevant Code |
|---|---|---|
| `data/phase6_combinatorial_search.json` | Entire | Previous 7-signal search — reference for methodology |
| `core/unified_backtest.py` | 113-195 | run_backtest() — the engine to call |
| `core/unified_backtest.py` | 29-32 | BACKTEST_SIGNALS — the full 22 signal list |
| `core/signals/__init__.py` | 30-53 | SignalRegistry — all 22 signals registered |

---

## <a name="p3"></a>Priority 3: Individual Signal Benchmarking

### Required Implementation

**Script:** `scripts/phaseB_signal_benchmarks.py` (new file)

#### 3.1 Benchmark Test Definition

For each of the 22 signals, run an individual backtest against all 20 stations, walk-forward 365/90 days.

**Metrics to track per signal:**

| Metric | Formula | Target |
|---|---|---|
| **Accuracy** | correct / total | > 0.55 (55%) |
| **Coverage** | total_trades / max_possible_trades | > 0.10 |
| **Brier Score** | (1/N) Σ (p_i - o_i)² | < 0.25 |
| **ECE** | Σ (|acc_k - conf_k| × w_k) | < 0.05 |
| **Sharpe Ratio** | mean(gross_returns) / std(gross_returns) | > 1.0 |
| **Max Drawdown** | max(peak - trough) / peak | < 0.30 |
| **Station Consistency** | % of stations with acc > 0.50 | > 60% |
| **Directional Symmetry** | |acc_up - acc_down| | < 0.10 |
| **Signal Firing Rate** | total_trades / trading_days | > 0.15 |

#### 3.2 Per-Signal Specific Tests

**3.2.1 Climatology Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `calendar_climatology` (`core/signals/calendar_climatology_signal.py`) | Standard benchmark; compare against simple historical average | Check coast vs inland accuracy diff (KLAX vs KDEN) |
| `persistence` (`core/signals/persistence_signal.py`) | Tomorrow = today; trivial baseline | Should be benchmark floor |

**3.2.2 Gaussian/Statistical Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `gaussian` (`core/signals/gaussian_signal.py`) | z-score based; test 30d, 48d windows | Check against gaussian_v2 for correlation |
| `gaussian_v2` (`core/signals/gaussian_v2_signal.py`) | Same as gaussian, different params | Check if redundant with gaussian (ρ > 0.70) |

**3.2.3 Pressure Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `pressure_delta` (`core/signals/pressure_delta_signal.py`) | 2-day pressure change — CURRENT | **Known bug:** Original physics is wrong (rising pressure → warming). Compare against corrected version. |
| `corrected_pressure_delta` (in `core/signals/dual_polarity_signal.py` class `CorrectedPressureDeltaSignal`) | Corrected physics: rising pressure → cooling | Should outperform pressure_delta |
| `pressure_tendency` (`core/signals/pressure_tendency_signal.py`) | 3-hour pressure trend | Compare with pressure_delta |

**3.2.4 Wind/Advection Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `wind_direction_shift` (`core/signals/wind_direction_shift_signal.py`) | Direction change → temp change | Low-coverage stations (KPHX: 5 trades in Phase 6) |
| `temperature_advection` (`core/signals/temperature_advection_signal.py`) | 850-mb temperature advection | Requires NWP data |
| `frontal_detector` (`core/signals/frontal_detector_signal.py`) | Frontal passage detection | Test against known frontal events |

**3.2.5 NWP Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `nwp_direct` (`core/signals/nwp_direct_signal.py`) | Direct NWP model output | **Known bug:** Returns None (no-op). Fix per Phase A first. |
| `hrrr_bias_corrected` (`core/signals/hrrr_bias_corrected_signal.py`) | HRRR with bias correction | Requires HRRR data availability |
| `esdr` (`core/signals/esdr_signal.py`) | Ensemble spread | Requires ensemble data |

**3.2.6 Reversion Signals (Phase 18)**

| Signal | Test | Edge Case |
|---|---|---|
| `fogr_reversion` (`core/signals/fogr_reversion_signal.py`) | Fear of great reversal | High-frequency, check for overfitting |
| `metar_dtdt` (`core/signals/metar_dtdt_signal.py`) | METAR temperature rate of change | Short lookback, high noise |
| `nwp_dtdt_fusion` (`core/signals/nwp_dtdt_fusion_signal.py`) | NWP + METAR fusion | Requires both NWP and METAR data |
| `spread_based_entry` (`core/signals/spread_based_entry_signal.py`) | Entry on spread conditions | Low-frequency, high-conviction |
| `volume_momentum` (`core/signals/volume_momentum_signal.py`) | Volume-based momentum | Check correlation with settlement_arbitrage |
| `settlement_arbitrage` (`core/signals/settlement_arbitrage_signal.py`) | Settlement time inefficiency | Requires settlement data |

**3.2.7 Phase 23 Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `seasonal_regime` (`core/signals/dual_polarity_signal.py` class `SeasonalRegimeClassifier`) | Classifies warm/cool/transition regime | Returns regime name as direction — need to verify compatibility with binary signal interface |
| `station_effects` (`core/station_effects.py`) | Station-specific wind→temp ΔT | Must verify `get_wind_delta_t()` outputs against actual settlement outcomes |
| `diurnal_curve` (`core/seasonal_diurnal_curve.py` class `DiurnalCurveModel`) | Expected temp anomaly from diurnal curve | Check confidence modulation by cloud cover |

**3.2.8 Other Signals**

| Signal | Test | Edge Case |
|---|---|---|
| `forecast_disagreement` (`core/signals/forecast_disagreement_signal.py`) | NWP model spread | Should be complementary to reversion cluster |
| `goldilocks` (`core/signals/goldilocks_signal.py`) | Intraday not-too-hot/not-too-cold | **Known issue:** 0.1% accuracy in Phase 6. Flag for potential removal. |
| `intraday_metar_confirmation` (`core/signals/intraday_metar_confirmation_signal.py`) | Intraday METAR trend confirmation | Short-horizon only |

#### 3.3 Signal Independence Measurement

Run `scripts/validate_signal_independence.py` against the full 22-signal set (currently only tests 17 in `ALL_SIGNAL_NAMES` at line 49-50 — needs update).

**Required changes to `scripts/validate_signal_independence.py`:**

1. Line 49-50: Add Phase 23 signal names:
```python
ALL_SIGNAL_NAMES = REVERSION_SIGNAL_NAMES | TREND_SIGNAL_NAMES | {
    'seasonal_regime',
    'corrected_pressure_delta',
    'persistence',
    'gaussian_v2',  # Already exists but not listed
    'pressure_tendency',
}
```

2. Add `'goldilocks'` to the skip list in `get_signal_predictions()` (line 90) — it's already excluded.

3. After running, check for:
   - Redundant pairs (|ρ| > 0.70) — suggest merging or removing one
   - Reversion cluster dominance — if > 60% of signals are reversion-based, add more trend signals
   - Ensemble Diversity Score (EDS) target: > 0.50

#### 3.4 Output Schema

Write to `data/phaseB_signal_benchmarks.json`:

```json
{
  "metadata": {
    "timestamp": "2026-07-22T...",
    "phase": "B",
    "signals_benchmarked": 22,
    "stations": 20,
    "train_days": 365,
    "test_days": 90
  },
  "per_signal": {
    "calendar_climatology": {
      "accuracy": 0.7028,
      "coverage": 0.50,
      "brier": 0.2972,
      "ece": 0.012,
      "sharpe": 17.49,
      "max_drawdown": 0.05,
      "station_consistency": 0.95,
      "directional_symmetry": 0.03,
      "firing_rate": 0.32,
      "pass": true,
      "warnings": ["low_coverage"]
    },
    ...
  },
  "summary": {
    "signals_passing": "<count>",
    "signals_failing": "<count>",
    "avg_accuracy": "<float>",
    "best_signal": {"name": "...", "accuracy": "..."},
    "worst_signal": {"name": "...", "accuracy": "..."},
    "signals_flagged_for_removal": ["goldilocks", "..."]
  }
}
```

#### 3.5 File Reference

| File | Lines | Relevant Code |
|---|---|---|
| `scripts/validate_signal_independence.py` | 1-280 | Independence validation — needs signal list update |
| `scripts/signal_accuracy_dashboard.py` | 80-200 | Existing signal-level analysis functions (reference pattern) |
| `core/signals/__init__.py` | 30-53 | All 22 signal class references |

---

## <a name="p4"></a>Priority 4: Real Settlement Validation

### Current State

The backtest runner (`core/unified_backtest.py`) uses `settlement_epochs` table data for ground truth (line 56-68), which is derived from historical Kalshi settlement data. However, there's no cross-validation against actual Kalshi API data to verify:
- Settlement prices are correct (not temp > 50°F as in original bug)
- Direction labeling matches actual Kalshi HIGH/LOW market structure
- The `reversion_occurred` field is correctly populated

### Required Implementation

**Script:** `scripts/phaseB_settlement_validation.py` (new file)

#### 4.1 Test Period

- **Training:** 2024-01-01 to 2025-06-30 (18 months)
- **Validation:** 2025-07-01 to 2026-06-30 (12 months) — this is the period with known Kalshi API data
- **Holdout:** 2026-07-01 to present (test against live data)

#### 4.2 Settlement Price Audit

For each closed epoch in the validation period:

```sql
SELECT local_trading_date, station, market_type, settlement_bucket, prior_settlement_bucket,
       epoch_status
FROM settlement_epochs
WHERE epoch_status = 'closed'
  AND local_trading_date BETWEEN '2025-07-01' AND '2026-06-30'
```

Cross-reference against actual Kalshi API price history:
- Query `GET /markets/{ticker}` for each settled market
- Compare `settlement_bucket` vs actual Kalshi `close_price` at settlement time
- Flag any discrepancy > 0.02 (2¢)

**Metric:** settlement_price_match_rate = matches / total_epochs
**Target:** 100% match (0% tolerance — this is ground truth)

#### 4.3 Direction Label Validation

For each HIGH market:
```
settlement_direction = 'up' if settlement_bucket > prior_settlement_bucket else 'down'
```

For each LOW market:
```
settlement_direction = 'up' if settlement_bucket < prior_settlement_bucket else 'down'
```

(Check this mapping matches the actual market structure — HIGH means temp > threshold, LOW means temp < threshold, so HIGH 'up' means the HIGH contract settled at Yes.)

#### 4.4 Backtest-to-Reality Gap Analysis

For each station × month in the validation period:
1. Run `run_backtest(signal_names=FULL_ENSEMBLE, stations=[station])`
2. Get predicted direction + confidence for each trading day
3. Compare against actual settlement outcome
4. Compute gap metrics:

| Metric | Formula |
|---|---|
| **Prediction Accuracy** | correct / total |
| **Profit Factor** | Σ(winning_trades) / |Σ(losing_trades)| |
| **Maximum Consecutive Losses** | longest losing streak |
| **Average Win / Average Loss** | mean(win_returns) / mean(loss_returns) |
| **Calibration Gap** | |avg(predicted_prob) - avg(actual_frequency)| |

#### 4.5 Expected Confusion Matrix

Output a 2×2 confusion matrix per signal per station:

```
            Actual UP    Actual DOWN
Pred UP       TP           FP
Pred DOWN     FN           TN
```

Derive: precision, recall, F1 score, Matthews Correlation Coefficient (MCC).

**Target:** MCC > 0.15 for signals to be considered useful.

#### 4.6 File Reference

| File | Lines | Relevant Code |
|---|---|---|
| `core/unified_backtest.py` | 56-68 | `load_station_data()` — settlement_epochs query |
| `data/phase6_calibration_validation.json` | Entire | Previous validation (5 combos only) |
| Kalshi API docs | `docs/plans/KALSHI_API_INTEGRATION.md` | For API query patterns |

---

## <a name="p5"></a>Priority 5: Fix Hardcoded `market_prob = 0.5`

### Current State

In `core/decision_output.py` line 170:
```python
return 0.5  # Neutral default if no data available
```

This is the fallback in `_get_market_implied_prob()` when no settlement data is available for a station/date/market_type combo. The problem is:
1. 0.5 is rarely the actual market price (Kalshi weather markets typically trade 0.10-0.85)
2. This hardcoded value means all computed edges are near-zero when data is missing
3. It silently returns a wrong value instead of raising a clear signal

There is also a live price cache (`core/round_number_anchoring.py` `get_kalshi_midpoint_price()` function at line 106) that queries the Kalshi API directly — this is the production path but the fallback in `decision_output.py` doesn't use it.

### Required Fix

**File:** `core/decision_output.py`

**Change at line 152-170:**

Replace the hardcoded fallback with a real Kalshi price lookup:

```python
def _get_market_implied_prob(self, station: str, date: str, market_type: str = "HIGH") -> float:
    """
    Get market-implied probability for a given weather market.
    Priority order: live Kalshi API → settlement-based estimate → None
    """
    # First try: live Kalshi API price
    try:
        from core.round_number_anchoring import get_kalshi_midpoint_price
        station_code = station[1:]  # Remove leading 'K' for Kalshi ticker
        threshold = 0  # Default threshold
        market_prob = get_kalshi_midpoint_price(
            f"K{market_type}{station_code}",
            market_type,
            threshold
        )
        if market_prob is not None:
            return market_prob
    except (ImportError, Exception) as e:
        _logger.warning(f"Failed to get Kalshi price for {station} {market_type}: {e}")
    
    # Second try: estimate from settlement data
    conn = sqlite3.connect(self.metar_db)
    cur = conn.cursor()
    cur.execute("""
        SELECT settlement_bucket, prior_settlement_bucket 
        FROM settlement_epochs 
        WHERE station = ? AND local_trading_date < ?
        AND market_type = ? AND epoch_status = 'closed'
        ORDER BY local_trading_date DESC LIMIT 1
    """, (station, date, market_type))
    row = cur.fetchone()
    conn.close()
    
    if row:
        settlement, prior = row
        recent_movement = settlement - prior
        base_price = 0.55 if market_type == "HIGH" else 0.45
        return max(0.05, min(0.95, base_price + (recent_movement / 50.0)))
    
    # No data available — return None instead of 0.5
    # Caller must handle None as "cannot compute edge"
    return None
```

**Change callers** (lines 272-287) to handle `None` market_prob:
```python
market_prob = self._get_market_implied_prob(station, date, market_type)
if market_prob is None:
    decision_type = "NO_DATA"
    strength = 0.0
    # Log a warning and skip this decision
    _logger.warning(f"No market data for {station} {date} {market_type}")
    return DecisionOutput(decision_type="NO_DATA", ...)
```

**Additional references to fix:**
- `scripts/real_backtest_runner.py` line 236: comment says "Buy at market price ~0.50 (assumed 50/50)" — this is documentation but should be updated to reflect real prices
- `scripts/run_7day_dev_validation.py` line 219: `market_odds = r.get('market_price', 0.5)` — change default to `None`

#### 5.1 File Reference

| File | Lines | Change |
|---|---|---|
| `core/decision_output.py` | 152-170 | Replace hardcoded 0.5 fallback with live Kalshi API + None |
| `core/decision_output.py` | 272-287 | Handle None market_prob |
| `core/round_number_anchoring.py` | 106 | `get_kalshi_midpoint_price()` — the live API function to use |
| `scripts/run_7day_dev_validation.py` | 219 | Change default from 0.5 to None |
| `scripts/real_backtest_runner.py` | 236 | Comment update |

---

## <a name="p6"></a>Priority 6: Stop Random Data in Calibration Dashboard

### Current State

`core/calibration_dashboard.py` `fetch_paper_trading_metrics()` function (line 112-128) generates simulated/dummy data when the real database is unavailable:

```python
# Simulation data if databases not available
print(f"Simulating with dummy data due to: {e}")
# Generate some simulation data
pnl = random.gauss(12.3, 25)  # Simulate daily P&L
```

This means the dashboard always displays something, but it's fake data. The `create_confidence_calibration_plot()` function (line 220) also simulates calibration data rather than using real results.

### Required Fix

**File:** `core/calibration_dashboard.py`

#### 6.1 Remove all random/simulated data generation

```python
# DELETE lines 112-128 (the except block that generates simulation data)
# REPLACE with:
except Exception as e:
    print(f"ERROR: Cannot connect to paper trading database: {e}")
    return {
        'performance_data': [],
        'overall_pnl': 0.0,
        'win_rate': 0.0,
        'version_performance': []
    }
```

#### 6.2 Wire real calibration results

In `create_confidence_calibration_plot()`, replace the simulated calibration curve (lines 220-245) with a real calibration curve loaded from `data/phaseB_calibration_results.json`:

```python
def create_confidence_calibration_plot(self):
    """Create confidence calibration plot from real calibration results."""
    try:
        with open('data/phaseB_calibration_results.json', 'r') as f:
            cal_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return go.Figure().update_layout(
            title="No calibration data available — run Phase B calibration first"
        ).to_html(full_html=False)

    # Extract per-signal calibration data
    bucket_data = cal_data.get('per_signal', {})
    # ... build reliability diagram from actual results
```

#### 6.3 Remove `import random`

Line 35: `import random  # Only needed for simulation data` — remove entirely after simulation code is removed.

#### 6.4 File Reference

| File | Lines | Change |
|---|---|---|
| `core/calibration_dashboard.py` | 35 | Remove `import random` |
| `core/calibration_dashboard.py` | 112-128 | Replace simulation data fallback with empty/error state |
| `core/calibration_dashboard.py` | 220-245 | Replace simulated calibration curve with real data |

---

## <a name="p7"></a>Success Criteria

Phase B is complete when ALL of the following are true:

### 7.1 Calibration Pipeline

| Criterion | Target | Measurement |
|---|---|---|
| Signals calibrated | 22/22 | Count of signals with valid per-signal report |
| Signals with per-station isotonic calibrator | ≥ 15 | `calibration_cell_count` ≥ 5 stations |
| Average Brier score improvement | ≥ 0.005 | `calibrated_brier < raw_brier` across all signals |
| Average ECE | < 0.05 | `ece` < 0.05 across all signal-station cells |
| Calibration data saved | File exists | `data/phaseB_calibration_results.json` with valid schema |

### 7.2 Combinatorial Search

| Criterion | Target | Measurement |
|---|---|---|
| Subsets tested | ≥ 6 | All subsets A-F defined in §2.2 |
| Agreement levels tested | ≥ 4 | Levels 1, 2, 3, 5 |
| Best ensemble accuracy | ≥ 0.70 | Top combo accuracy |
| Best ensemble Sharpe | ≥ 1.5 | Top combo Sharpe |
| Combo search saved | File exists | `data/phaseB_combinatorial_search.json` |

### 7.3 Individual Signal Benchmarks

| Criterion | Target | Measurement |
|---|---|---|
| Signal accuracy > 55% | ≥ 12 signals | Count of signals passing threshold |
| Signal accuracy > 60% | ≥ 6 signals | Count of signals passing threshold |
| Signals flagged for removal | ≤ 3 | goldilocks, regime, and any others with accuracy < 50% |
| No redundant pairs | |ρ| < 0.70 for all signal pairs |
| Ensemble Diversity Score | ≥ 0.50 | EDS from validate_signal_independence.py |
| Benchmark data saved | File exists | `data/phaseB_signal_benchmarks.json` |

### 7.4 Settlement Validation

| Criterion | Target | Measurement |
|---|---|---|
| Settlement price match rate | 100% | Against actual Kalshi API data |
| Validation period | ≥ 12 months | 2025-07-01 to 2026-06-30 |
| MCC > 0.15 | ≥ 10 signals | Minimal useful signal threshold |
| Confusion matrix computed | Yes | Per signal, per station |

### 7.5 Market_prob Fix

| Criterion | Target | Measurement |
|---|---|---|
| No hardcoded 0.5 | 0 occurrences | `grep -r "return 0.5" core/decision_output.py` returns 0 |
| Live Kalshi API wired | Yes | `_get_market_implied_prob` calls `get_kalshi_midpoint_price` |
| None handling | Yes | Callers handle `None` market_prob gracefully |

### 7.6 Dashboard Fix

| Criterion | Target | Measurement |
|---|---|---|
| No random/simulated data | 0 occurrences | `grep -r "random.gauss\|random.randint\|simulat" core/calibration_dashboard.py` returns 0 |
| Real calibration data | Yes | Dashboard loads from `phaseB_calibration_results.json` |

### 7.7 Overall Phase B Completion

```
# Phase B sign-off checklist
items = [
    "22 signals calibrated with per-station isotonic regression",
    "Combinatorial search re-run with all 22 signals",
    "Individual signal benchmarks for all 22 signals",
    "Real settlement data validated against Kalshi API",
    "Hardcoded market_prob=0.5 replaced with live API",
    "Random data removed from calibration dashboard",
    "All 22 signals have known accuracy, coverage, Brier, Sharpe, ECE",
    "Goldilocks signal flagged for removal (0.1% accuracy)",
    "Signal independence validated (no redundant pairs)",
    "Phase A must be complete before Phase B begins",
]

all_complete = all(items)  # This must be True
```

---

## <a name="appendix"></a>Appendix: Math Reference

### Brier Score

$$\\text{Brier} = \\frac{1}{N} \\sum_{i=1}^{N} (p_i - o_i)^2$$

Where:
- $p_i$ = predicted probability of UP (derived from confidence)
- $o_i$ = actual outcome (1.0 for UP, 0.0 for DOWN)
- Lower is better; 0.0 = perfect, 0.25 = random, 1.0 = always wrong

For directional predictions where confidence $c_i$ is P(correct):
$$p_i = \\begin{cases} c_i & \\text{if pred = UP} \\\ 1 - c_i & \\text{if pred = DOWN} \\end{cases}$$

### Expected Calibration Error (ECE)

$$\\text{ECE} = \\sum_{m=1}^{M} \\frac{|B_m|}{N} \\cdot |\\text{acc}(B_m) - \\text{conf}(B_m)|$$

Where:
- $M$ = number of bins (typically 10)
- $B_m$ = bin $m$
- $\\text{acc}(B_m)$ = average accuracy within bin $m$
- $\\text{conf}(B_m)$ = average confidence within bin $m$
- Target: < 0.05

### Sharpe Ratio

$$\\text{Sharpe} = \\frac{\\bar{R}}{\\sigma_R}$$

Where:
- $\\bar{R}$ = mean of trade returns
- $\\sigma_R$ = standard deviation of trade returns

For a single trade $i$:
$$R_i = \\begin{cases} +2 \\cdot c_i & \\text{if correct} \\\ -2 \\cdot c_i & \\text{if incorrect} \\end{cases}$$

(2:1 payout at fair odds, multiplied by confidence-based position size)

### Isotonic Regression Calibration

Non-parametric monotonic mapping from raw confidence $c_r$ to calibrated confidence $c_c$:

$$c_c = f(c_r) \\quad \text{where } f \\text{ is monotonic non-decreasing}$$

Fitted by minimizing:
$$\\min_f \\sum_{i=1}^{N} (y_i - f(c_{r,i}))^2 \\quad \text{s.t. } f \\text{ is isotonic}$$

Where $y_i = 1$ if prediction was correct, 0 otherwise.

Implementation: `sklearn.isotonic.IsotonicRegression` with `out_of_bounds='clip'`, `y_min=0.05`, `y_max=0.95`.

### Combinatorial Search

Number of k-signal combinations from N signals:

$$C(N, k) = \\frac{N!}{k!(N-k)!}$$

With N=22:
- $C(22, 1) = 22$ — test all single signals
- $C(22, 2) = 231$ — test all pairs
- $C(22, 3) = 1540$ — sample ~200 (stratified by family)
- $C(22, 4) = 7315$ — sample ~200
- $C(22, 5) = 26334$ — sample ~200

Total: ~883 combos tested (not all 35,442)

### Agreement Gate

For a given agreement level $A$:

$$\\text{Signal}_i \\text{ fires on day } d \\implies \\text{output}_i \\in \\{\\text{UP}, \\text{DOWN}, \\text{None}\\}$$

$$\\text{Ensemble fires on day } d \\iff |\\{i : \\text{output}_i \\neq \\text{None}\\}| \\geq A$$

$$\\text{Ensemble direction} = \\begin{cases} \\text{UP} & \\text{if } \\sum(\\text{UP}) > \\sum(\\text{DOWN}) \\\ \\text{DOWN} & \\text{otherwise} \\end{cases}$$

### Matthews Correlation Coefficient (MCC)

$$\\text{MCC} = \\frac{TP \\cdot TN - FP \\cdot FN}{\\sqrt{(TP+FP)(TP+FN)(TN+FP)(TN+FN)}}$$

- Range: [-1, +1]
- +1 = perfect prediction
- 0 = random
- -1 = always wrong
- Target: > 0.15

### Ensemble Diversity Score (EDS)

$$\\text{EDS} = 0.35 \\cdot (1 - |\\bar{\\rho}|) + 0.25 \\cdot (1 - \\bar{A}) + 0.15 \\cdot \\min(1, 5 \\cdot \\sigma_f) + 0.25 \\cdot (1 - 2 \\cdot |r - 0.5|)$$

Where:
- $\\bar{\\rho}$ = average pairwise Spearman correlation
- $\\bar{A}$ = average directional agreement rate
- $\\sigma_f$ = standard deviation of firing rates
- $r$ = reversion/total ratio (target: 0.5)
- Target: ≥ 0.50

### Station Consistency

$$\\text{station\\_consistency} = \\frac{|\\{s : \\text{acc}_s > 0.50\\}|}{|\\text{all stations}|}$$

Target: > 60% of stations

---

## Execution Order

1. **Phase A completes first** — wrong P&L makes calibration meaningless
2. Run `scripts/phaseB_calibration_pipeline.py` — calibrate all 22 signals
3. Run `scripts/phaseB_combinatorial_search.py` — re-run search with all 22 signals
4. Run `scripts/phaseB_signal_benchmarks.py` — individual signal benchmarking
5. Run `scripts/phaseB_settlement_validation.py` — validate against real Kalshi data
6. Fix `core/decision_output.py` — replace hardcoded 0.5
7. Fix `core/calibration_dashboard.py` — remove random data, wire real results
8. Update `scripts/validate_signal_independence.py` — add missing signals to list
9. **Review results** — flag signals for removal, confirm best combo, sign off

---

*End of Phase B Expert Specification*