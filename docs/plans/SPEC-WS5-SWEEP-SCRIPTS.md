# Spec: WS5 Sweep Scripts

## Problem

The WS5 sweep requires 11 scripts to validate signal quality, independence, calibration, and baselines. None of these scripts exist yet. Each must run against real Kalshi settlement data (`data/kalshi_settlements.db`) and signal predictions, produce a JSON results file, and be independently executable.

## Current State

- **Data source**: `data/kalshi_settlements.db` with table `kalshi_settlements` containing 6,331 rows across 21 stations (including "TEST" — see Spec 5). Schema: `(id, station, series, event_ticker, target_date, kalshi_temp, markets_found, finalized_count, source, source_type, created_at, updated_at)`.
- **Signal registry**: 14 registered signals in `core/signals/__init__.py` — `REGISTERED_SIGNAL_NAMES` and `SignalRegistry` class with per-signal fire rates and confidence levels documented.
- **Existing infrastructure**: `scripts/bmode_p1_directional_sweep.py` is a reference for script pattern, `scripts/compute_trade_correlation.py` for correlation matrix computation. `scripts/compute_calibration_ceiling.py` exists.
- **No existing scripts** for: test_signal_accuracy, test_signal_independence, test_signal_fusion, test_signal_correlation, compute_reliability_curves, compute_tail_calibration, compute_calibration_stability, compute_stability_surface, run_persistence_baseline, run_market_baseline, run_coinflip_null.
- **Signal prediction data**: The `unified_backtest.py` (in `core/`) produces signal-by-signal predictions. Each script needs to either re-run predictions or load pre-computed ones.

## Design

### Common infrastructure

All scripts share:

1. **Data loading**: A common loader for `kalshi_settlements.db` (via `core.db_utils` or direct `sqlite3.connect`)
2. **Signal evaluation**: A common interface to `SignalRegistry` from `core/signals/__init__.py` for per-signal prediction generation
3. **Output format**: Unified JSON results schema with metadata (script name, timestamp, parameters, results)
4. **Error handling**: Graceful failure with partial results, never silent crashes

### Script-by-script design

---

### 1. `scripts/test_signal_accuracy.py`

**Purpose**: Per-signal directional accuracy vs ground truth for all 14 registered signals.

**Data read**: `data/kalshi_settlements.db` (settlement data), `data/metar_backfill.db` (METAR observations for signal computation).

**Computation**:
1. For each of the 14 registered signals, via `SignalRegistry`:
   - Load the signal instance
   - For each settlement record with matching station/date: compute signal prediction
   - Compare to ground truth (kalshi_temp vs forecast temperature bucket)
   - Record: correct, incorrect, no-prediction
2. For each signal: `directional_accuracy = correct / (correct + incorrect)`
3. Output per signal: signal_name, n_predictions, n_correct, n_incorrect, accuracy, fire_rate, binomial_p_value, confidence_interval (Clopper-Pearson)

**Output**: JSON file with `{"signal_accuracies": [...], "metadata": {...}}`

**Estimated time**: ~5-10 minutes for all 14 signals (depends on signal computation complexity).

**Failure modes**: Some signals require NWP data that may not be available — gracefully degrade with `"error": "signal not available"`.

---

### 2. `scripts/test_signal_independence.py`

**Purpose**: Leave-one-out marginal contribution — how much does each signal add to the ensemble?

**Data read**: Same as above + ensemble fusion from `core/fusion_logic.py` or `core/signal_fusion.py`.

**Computation**:
1. Run full ensemble fusion on all 14 signals → baseline performance
2. For each signal i: run ensemble on the 13 other signals (leave i out)
3. Compute: `marginal_contribution_i = baseline - leave_out_i`
4. Positive = signal adds value, negative = signal detracts, zero = signal redundant

**Output**: JSON with baseline performance, per-signal leave-one-out results, sorted by marginal contribution.

**Estimated time**: ~30-60 minutes (14 ensemble runs).

**Failure modes**: Fusion engine may reject insufficient signals — use fallback to simple majority vote for 13-signal case.

---

### 3. `scripts/test_signal_fusion.py`

**Purpose**: Ensemble vs best single signal A/B comparison.

**Data read**: Same as #1.

**Computation**:
1. Run fusion on all 14 signals → ensemble accuracy
2. Compute accuracy for each individual signal → best_single accuracy
3. For each signal count n (1-14): run fusion on the top-n signals (by individual accuracy) → accuracy curve
4. Report: ensemble accuracy, best single accuracy, delta, signal-count-vs-accuracy curve

**Output**: JSON with ensemble_accuracy, best_single_signal, best_single_accuracy, delta_vs_best, accuracy_by_n_signals [{n, accuracy}].

**Estimated time**: ~15-30 minutes.

**Failure modes**: As #2.

---

### 4. `scripts/test_signal_correlation.py`

**Purpose**: 14×14 correlation matrix of signal predictions.

**Data read**: Same as #1.

**Computation**:
1. For each pair of signals (i, j): compute the phi coefficient (Matthews correlation) of their binary predictions (up/down)
2. Report matrix (14×14 heatmap-printable) and summary statistics (mean off-diagonal |ρ|, max |ρ|, % of pairs with |ρ| > 0.7)
3. Signal independence audit: flag pairs with |ρ| > 0.7 as potentially redundant

**Output**: JSON with `{matrix: [[rho_ij]], signal_names: [...], mean_off_diagonal_abs_rho: float, max_abs_rho: float, redundant_pairs: [[i, j, rho]]}`.

**Estimated time**: ~10-20 minutes (O(n² × settlements) pairs).

**Failure modes**: Some signal pairs may have no overlapping predictions — use NaN with proper handling in matrix.

---

### 5. `scripts/compute_reliability_curves.py`

**Purpose**: Expected Calibration Error (ECE) and Maximum Calibration Error (MCE) per station and per direction.

**Data read**: Signal predictions with confidence values + ground truth.

**Computation**:
1. For each station: bin predictions by confidence decile (0-10%, 10-20%, ..., 90-100%)
2. For each bin: computed expected accuracy = bin midpoint, observed accuracy = correct/total
3. ECE = Σ(|bin_n|/N × |observed - expected|) over all bins
4. MCE = max(|observed - expected|) over all bins
5. Report per station, per direction (up/down), and aggregated

**Output**: JSON with `{ece: float, mce: float, per_station: {station: {ece, mce}}, per_direction: {up: {ece, mce}, down: {ece, mce}}, reliability_curves: [{station, direction, bins: [{bin_min, bin_max, n, expected, observed, gap}]}]}`.

**Estimated time**: ~5-15 minutes.

**Failure modes**: Sparse data in high-confidence bins — use minimum bin count threshold (default 5) and flag unreliable bins.

---

### 6. `scripts/compute_tail_calibration.py`

**Purpose**: Calibration specifically for p < 0.2 and p > 0.8 regions, with bootstrap confidence intervals.

**Data read**: Same as #5.

**Computation**:
1. Extract predictions in tail regions: p < 0.2 (cold) and p > 0.8 (hot)
2. For each tail region: compute observed frequency vs predicted
3. Bootstrap (1000 resamples) → 95% confidence interval on observed frequency
4. Report: whether 0.5 is inside the CI (well-calibrated), calibration gap, bootstrap CI

**Output**: JSON with `{cold_tail: {n, predicted_mean, observed, ci_lower, ci_upper, well_calibrated}, hot_tail: {...}, per_station_tails: {...}}`.

**Estimated time**: ~3-8 minutes.

**Failure modes**: Insufficient tail data for some stations — aggregate across stations if per-station data is sparse.

---

### 7. `scripts/compute_calibration_stability.py`

**Purpose**: Rolling 3-month ECE to detect calibration drift over time.

**Data read**: Same as #5, with date-based partitioning.

**Computation**:
1. Partition data into 3-month rolling windows (shift by 1 month)
2. For each window: compute ECE
3. Report: ECE time series, trend (linear regression slope of ECE vs time), drift alert if slope > threshold (e.g., 0.01/month)

**Output**: JSON with `{ece_series: [{window_start, window_end, ece, mce, n}], trend_slope: float, drift_detected: bool, per_station: {...}}`.

**Estimated time**: ~10-20 minutes.

**Failure modes**: Early windows with insufficient data — require minimum 100 predictions per window.

---

### 8. `scripts/compute_stability_surface.py`

**Purpose**: Parameter plateau widths — how sensitive are results to calibration parameter choices?

**Data read**: None directly — runs multiple calibration configurations.

**Computation**:
1. For each calibration parameter (e.g., n_bins, binning strategy, smoothing_alpha):
   - Vary parameter across reasonable range
   - Compute ECE for each value
2. Identify plateau region where ECE is within 1% of minimum
3. Report: optimal parameter value, plateau width (range of stable parameters), surface grid

**Output**: JSON with `{parameters: {param_name: {optimal: value, plateau_width: [low, high], surface: [{param_value, ece}]}}}`.

**Estimated time**: ~20-40 minutes (depends on parameter grid size).

**Failure modes**: Computationally intensive — limit to 3 parameters and 20 values each.

---

### 9. `scripts/run_persistence_baseline.py`

**Purpose**: Naive, 3-day, and seasonal baselines for comparison.

**Data read**: `data/kalshi_settlements.db` (Kalshi daily temperature data) + `data/metar_backfill.db` (METAR for persistence).

**Computation**:
1. Naive: predict today's temperature = yesterday's temperature
2. 3-day: predict = average of last 3 days
3. Seasonal: predict = average of same calendar day over all years in dataset
4. For each baseline: compute directional accuracy (was it warmer/colder than the market baseline?)

**Output**: JSON with `{baselines: {naive: {accuracy, n_trades, ...}, three_day: {...}, seasonal: {...}}, market_comparison: {better_than_market, worse_than_market, same}}`.

**Estimated time**: ~2-5 minutes.

**Failure modes**: Missing METAR data for some days — use available days only.

---

### 10. `scripts/run_market_baseline.py`

**Purpose**: System vs market implied probability — does the system beat the market?

**Data read**: `data/kalshi_settlements.db` (settlements + market prices).

**Computation**:
1. For each settlement event: get the market-implied probability (from market prices at close)
2. Compare system prediction probability to market-implied probability
3. Compute: system win rate when system disagrees with market, when system agrees with market
4. Brier score comparison: system Brier vs market Brier

**Output**: JSON with `{system_accuracy, market_accuracy, system_better_n, market_better_n, brier_system, brier_market, brier_delta, per_station: {...}}`.

**Estimated time**: ~5-10 minutes.

**Failure modes**: Market price data may not be available for all settlement events — use subset.

---

### 11. `scripts/run_coinflip_null.py`

**Purpose**: Two-sided binomial test against 50% with block bootstrap to account for temporal dependency.

**Data read**: System predictions from any of the above scripts, or from the unified backtest results DB.

**Computation**:
1. Collect all system predictions with direction
2. Count successes (correct direction predictions) and total predictions
3. Two-sided binomial test: p-value for H0: true accuracy = 0.5
4. Block bootstrap: resample blocks of consecutive days (block size = 5 trading days) 5000 times
5. Compute bootstrap CI and adjusted p-value accounting for temporal autocorrelation
6. Report: raw accuracy, binomial p-value, bootstrap p-value, bootstrap CI, effective sample size (accounting for block dependency)

**Output**: JSON with `{n_predictions, n_correct, raw_accuracy, binomial_p_value, bootstrap_p_value, bootstrap_ci: [low, high], effective_n, significant_at_95pct: bool, significant_at_99pct: bool, significant_after_bonferroni: bool}`.

**Estimated time**: ~5-10 minutes.

**Failure modes**: Fewer than 30 predictions — flag as insufficient sample size.

## Implementation Steps

### Step 1: Create common utility module

File: `scripts/ws5_common.py` (or place shared helpers in `scripts/sweep/` as `stats.py`)

Shared functions:
- `load_settlements()` — returns DataFrame-like list of dicts from `data/kalshi_settlements.db`
- `load_signal_registry()` — returns `SignalRegistry` instance from `core/signals/__init__.py`
- `compute_directional_accuracy(predictions, ground_truths)` — reusable accuracy computation
- `binomial_ci(successes, trials, alpha=0.05)` — Clopper-Pearson interval
- `bootstrap_resample(data, n_iterations, block_size)` — block bootstrap
- `ece_from_bins(bins_data)` — ECE/MCE from precomputed bins

### Step 2: Create each script

For each of the 11 scripts:
- Parse CLI args (at minimum `--output` for results file)
- Load data
- Compute metrics
- Write JSON output
- Print summary to stdout

### Step 3: Add CLI standardization

All scripts should support:
```bash
python3 scripts/test_signal_accuracy.py --output results/accuracy.json
python3 scripts/test_signal_accuracy.py --output results/accuracy.json --verbose
python3 scripts/test_signal_accuracy.py --help  # Document
```

### Step 4: Create results directory

Add `results/` directory to repo root for sweep outputs.

## Changed Files

| File | Change Type | Description |
|---|---|---|
| `scripts/ws5_common.py` | CREATE | Shared utilities for all 11 scripts |
| `scripts/test_signal_accuracy.py` | CREATE | Per-signal accuracy vs ground truth |
| `scripts/test_signal_independence.py` | CREATE | Leave-one-out marginal contribution |
| `scripts/test_signal_fusion.py` | CREATE | Ensemble vs best single signal A/B |
| `scripts/test_signal_correlation.py` | CREATE | 14×14 correlation matrix |
| `scripts/compute_reliability_curves.py` | CREATE | ECE/MCE per station/direction |
| `scripts/compute_tail_calibration.py` | CREATE | p<0.2, p>0.8 calibration with bootstrap |
| `scripts/compute_calibration_stability.py` | CREATE | Rolling 3-month ECE |
| `scripts/compute_stability_surface.py` | CREATE | Parameter plateau widths |
| `scripts/run_persistence_baseline.py` | CREATE | Naive/3-day/seasonal baselines |
| `scripts/run_market_baseline.py` | CREATE | System vs market implied probability |
| `scripts/run_coinflip_null.py` | CREATE | Two-sided binomial with block bootstrap |
| `results/` | CREATE | Directory for sweep output files |

## Verification Steps

```bash
# 1. Test common utilities load
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from ws5_common import load_settlements, load_signal_registry
s = load_settlements()
print(f'{len(s)} settlement records loaded')
r = load_signal_registry('data/kalshi_settlements.db')
print(f'{len(r.get_registered_signal_names())} registered signals')
print('OK')
"

# 2. Test a few scripts
python3 scripts/test_signal_accuracy.py --output /tmp/ws5_accuracy.json
python3 scripts/test_signal_correlation.py --output /tmp/ws5_correlation.json
python3 scripts/run_coinflip_null.py --output /tmp/ws5_coinflip.json
echo "Exit codes: $?"

# 3. Verify JSON output format
python3 -c "
import json
for f in ['/tmp/ws5_accuracy.json', '/tmp/ws5_correlation.json', '/tmp/ws5_coinflip.json']:
    data = json.load(open(f))
    assert 'metadata' in data
    print(f'{f}: OK (keys: {list(data.keys())[:5]})')
"

# 4. Run remaining scripts
for script in test_signal_independence test_signal_fusion compute_reliability_curves compute_tail_calibration compute_calibration_stability compute_stability_surface run_persistence_baseline run_market_baseline; do
    python3 scripts/${script}.py --output /tmp/ws5_${script}.json
    echo "$script exit: $?"
done
```

## Rollback Plan

1. **New files only**: Delete `scripts/ws5_common.py` and all 11 individual scripts.
2. **Results directory**: Remove `results/` if empty.
3. **No existing code modified**: All scripts are new files, so rollback is trivial.

## Out of Scope

- Integrating these scripts into the sweep orchestration pipeline — they are standalone.
- Creating the sweep runner (`scripts/run_sweep.py`) — that's Spec 3's responsibility.
- Computing signal predictions for signals that require NWP data — those signals gracefully degrade.
- Optimizing runtime beyond what's described — first-pass implementations are acceptable.
- Creating unit tests for the scripts — validation is manual via output inspection.