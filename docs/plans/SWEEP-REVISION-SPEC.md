# Sweep Revision Spec — Full-Arsenal Parameter Sweep

**Status:** Design Spec | **Author:** Donna | **Target:** Gilfoyle revises `scripts/big_sweep.py`
**Prerequisite:** All Group A/B/C builds complete (40 signals, 9 gates, 5 levers, 2 lanes, 5 modulators)
**Goal:** A single sweep that tests ALL components — not just signals in isolation

---

## 1. Current State

The current `big_sweep.py` (947 lines) does:

```
for each config in 500 LHS configs:
    for each signal in 39 signals:
        evaluate(idx, days) → (direction, confidence)
        calibrate_confidence(direction, conf) → (cal_conf, raw_conf)
        simulate_trade(direction, cal_conf, actual) → trade
    compute correlation matrix
    write output files
```

**What it does NOT test:**
- Gates (agreement, settlement, production, station skill, adaptive thresholds, liquidity, trajectory)
- Levers (continuous Kelly, variance-weighted sizing, drawdown limits)
- Lanes (Goldilocks, trajectory lane)
- Meta-modulators (signal fusion, spatial coherence)
- Calibration modes (Platt vs BMA/EMOS vs both)
- Luck elimination (post-hoc statistical validation)

**The problem:** The config space has exploded. Each gate has 2-5 parameters. Each lever has 3-5 parameters. Total config parameters are now ~40+ (up from 18). A naive sweep of 500 configs won't meaningfully explore this space.

---

## 2. Revised Sweep Architecture

### 2.1 Two-Phase Sweep

Split the sweep into two phases:

**Phase 1: Signal-Only Sweep** (existing architecture, extended)
- Tests all 40 signals × 500 configs
- Config parameters: signal thresholds, confidence floors, fee model, holdout fractions
- Output: per-signal accuracy, PnL, Sharpe, Brier
- **Purpose:** Find the best standalone signal configs

**Phase 2: Meta-Sweep** (new)
- Takes the best signal configs from Phase 1
- Tests gate/lever/lane/modulator combinations
- Config parameters:
  - Gates: agreement N/M, settlement cooldown, production thresholds, BSS threshold, adaptive threshold priors, liquidity spread threshold, trajectory match count
  - Levers: Kelly fraction, capital base, variance penalty, drawdown limits, concentration limits
  - Lanes: Goldilocks enabled, trajectory lane enabled, trajectory lane weight
  - Modulators: fusion mode (none/uwc/majority/weighted), spatial coherence on/off, calibration mode (platt/bma/emos/both)
- Uses LHS with fewer total configs (200-500) but higher parameter count (30-40)
- **Purpose:** Find the optimal gate/lever/modulator combination for the best signals

### 2.2 Why Two Phases?

Testing signals and meta-parameters in a single sweep creates an intractable config space (40+ parameters × 500 configs = meaningless coverage). Two phases:
- Phase 1: ~15 signal parameters × 500 configs = reasonable coverage
- Phase 2: ~30 meta-parameters × 200-500 configs = focused search on what matters most

The key insight: signal parameters (thresholds, weights) are independent of gate parameters (N/M, cooldown). There's no cross-interaction worth testing in a single pass.

---

## 3. Phase 1: Signal-Only Sweep

### 3.1 Config Parameters (same as current, ~15 params)

| Category | Parameters |
|:---------|:-----------|
| **Signal** | confidence_floor, edge_threshold, entry_price_min, entry_price_max, min_lookback_mult |
| **Fee** | fee_type (0=none, 1=taker, 2=taker+slippage, 3=maker), fee_deduction, slippage_budget |
| **Holdout** | holdout_start_frac, holdout_geo_frac |
| **Validation** | min_trades_report, min_trades_calibrate |
| **Calibration** | calibration (platt/bma/emos/both) — **NEW** |

### 3.2 Pipeline

```
load_data()
build_signal_registry()
for each config in LHS(500):
    for each signal in 40 signals:
        evaluate(idx, days) → (direction, confidence)
        calibrate_confidence(direction, conf, calibration) → (cal_conf, raw)
        simulate_trade(direction, cal_conf, actual) → trade
    compute_metrics()
pick_best_configs(top_k=5)
output: phase1_results.json, phase1_summary.csv
```

### 3.3 Output Changes

- Phase 1 output includes calibration mode in results metadata
- Per-signal best config recorded with calibration mode used
- Signals ranked by accuracy (min 10 trades), Sharpe (min 0.5), PnL

---

## 4. Phase 2: Meta-Sweep

### 4.1 Config Parameters (~30 params)

#### Gates (12 params)

| Gate | Parameters | Range |
|:-----|:-----------|:------|
| **Agreement Gate** | enabled (0/1), n_required (1-9), m_total (3-15) | 0/1, 1-9, 3-15 |
| **Settlement Gate** | enabled (0/1), cooldown_hours (0-72) | 0/1, 0-72 |
| **Production Gate** | enabled (0/1) | 0/1 |
| **Station Skill Gate** | enabled (0/1), bss_threshold (-0.5 to 0.5) | 0/1, -0.5-0.5 |
| **Adaptive Thresholds** | enabled (0/1), prior_alpha (1-10), prior_beta (1-10) | 0/1, 1-10, 1-10 |
| **Liquidity Gate** | enabled (0/1), spread_threshold (0.01-0.10) | 0/1, 0.01-0.10 |
| **Trajectory Gate** | enabled (0/1), min_analogs (10-100) | 0/1, 10-100 |

#### Levers (5 params)

| Lever | Parameters | Range |
|:------|:-----------|:------|
| **Continuous Kelly** | kelly_fraction (0.1-1.0), capital_base (10000-200000) | 0.1-1.0, 10000-200000 |
| **Variance-Weighted Sizing** | enabled (0/1), variance_penalty (0.5-2.0) | 0/1, 0.5-2.0 |
| **Drawdown Rules** | halve_at (0.05-0.20), stop_at (0.15-0.40) | 0.05-0.20, 0.15-0.40 |
| **Concentration Limits** | max_per_station (0.10-0.50), max_per_signal (0.05-0.30) | 0.10-0.50, 0.05-0.30 |

#### Lanes (3 params)

| Lane | Parameters | Range |
|:-----|:-----------|:------|
| **Goldilocks Lane** | enabled (0/1) | 0/1 |
| **Trajectory Lane** | enabled (0/1), weight (0.05-0.30) | 0/1, 0.05-0.30 |

#### Modulators (4 params)

| Modulator | Parameters | Range |
|:----------|:-----------|:------|
| **Signal Fusion** | mode (0=none, 1=uwc, 2=majority, 3=weighted) | 0-3 |
| **Spatial Coherence** | enabled (0/1) | 0/1 |
| **Calibration** | mode (0=platt, 1=bma, 2=emos, 3=both) | 0-3 |

### 4.2 Pipeline

```
load_data()
load_phase1_best_configs()
for each meta_config in LHS(300):
    build_meta_config(gates, levers, lanes, modulators)
    for each signal in phase1_top_signals:
        evaluate(idx, days) → (direction, confidence)
        calibrate_confidence(direction, conf, cal_mode) → (cal_conf, raw)
        apply_gates(signal, station, date, direction, cal_conf) → pass/skip
        if pass:
            apply_lanes(signal, station, date, direction) → lane_mod
            apply_modulators(station, signal, direction, cal_conf) → modulated_conf
            apply_levers(station, signal, direction, modulated_conf) → contracts
            simulate_trade(direction, modulated_conf, actual, contracts) → trade
    compute_metrics(include_gate_stats)
    run_luck_elimination(trades) → p_value, luck_adj_accuracy
output: phase2_results.json, phase2_summary.csv
```

### 4.3 Key Design Decision: Gate Pipeline Order

Gates must be applied in a specific order:

```
1. Settlement Gate (check market open, weekday, cooldown)
2. Station Skill Gate (check signal has skill on this station)
3. Liquidity Gate (check market has sufficient liquidity)
4. Agreement Gate (check N-of-M signals agree on direction)
5. Trajectory Gate (check analog trajectory confirms direction)
6. Adaptive Thresholds (check confidence exceeds per-signal/per-station floor)
7. Production Gate (check signal meets real-money readiness thresholds)
```

If ANY gate fails, the trade is skipped. The order matters because:
- Settlement and Station Skill are fastest checks (no computation)
- Liquidity checks market conditions
- Agreement and Trajectory require computation from multiple signals
- Adaptive Thresholds uses historical data
- Production Gate is the final safety check

### 4.4 Output Changes

Phase 2 output includes:
- Per-gate pass/fail counts (which gates are blocking the most trades)
- Per-lever contract distribution (how much capital per signal/station)
- Per-lane trade distribution (Goldilocks vs main lane)
- Luck-adjusted metrics (p-value, luck floor subtracted)
- Correlation-adjusted metrics (portfolio-level not just signal-level)

---

## 5. Implementation Plan

### 5.1 Changes to `scripts/big_sweep.py`

1. **Add `--phase 1|2` argument** (default: 1)
2. **Phase 1** (mostly existing code):
   - Rename existing main() to `phase1_sweep()`
   - Add calibration mode to config parameters
   - Output best configs per signal
3. **Phase 2** (new):
   - Add `phase2_sweep()` function
   - Add all gate/lever/lane/modulator config parameters to LHS generator
   - Add gate pipeline (see §4.3)
   - Add lane evaluation
   - Add modulator evaluation
   - Add luck elimination
   - Add portfolio-level metrics
4. **New import requirements:**
   - All gate modules (already imported)
   - All lever modules (continuous_kelly, variance_weighted_sizing)
   - All lane modules (lane_goldilocks, trajectory_lane)
   - All modulator modules (signal_fusion, spatial_coherence)
   - Luck elimination module

### 5.2 New Files

| File | Purpose |
|:-----|:--------|
| `core/gate_pipeline.py` | Orchestrates the 7-gate pipeline in correct order |
| `core/lever_manager.py` | Manages position sizing (Kelly + variance + drawdown + concentration) |
| `core/lane_manager_v2.py` | Manages multi-lane evaluation (main + Goldilocks + trajectory) |
| `core/modulator_stack.py` | Applies modulators in correct order (fusion → spatial → calibration) |

### 5.3 Gate Pipeline Module (`core/gate_pipeline.py`)

```python
class GatePipeline:
    def evaluate(signal_name, station, date, direction, confidence) -> GateResult:
        # Order: Settlement → StationSkill → Liquidity → Agreement → Trajectory → Adaptive → Production
        # Returns: (pass: bool, reason: str, gate_stats: dict)
```

### 5.4 Lever Manager Module (`core/lever_manager.py`)

```python
class LeverManager:
    def compute_position(signal_name, station, direction, confidence, edge, state) -> int:
        # Order: Kelly base → Variance adjustment → Drawdown multiplier → Concentration cap
        # Returns: n_contracts
```

### 5.5 Lane Manager V2 Module (`core/lane_manager_v2.py`)

```python
class LaneManagerV2:
    def evaluate_all(station, date, signals_output) -> LaneResult:
        # Evaluate each lane (main, Goldilocks, trajectory)
        # Blend lane outputs into unified position
```

### 5.6 Modulator Stack Module (`core/modulator_stack.py`)

```python
class ModulatorStack:
    def apply(station, signal_name, direction, confidence) -> ModulatedResult:
        # Apply: Fusion → Spatial Coherence → Calibration
        # Returns: (modulated_direction, modulated_confidence)
```

---

## 6. Config Space Analysis

| Phase | Parameters | Configs | Coverage |
|:------|:----------:|:-------:|:---------|
| 1 (current) | 15 | 500 | 33 configs/param |
| 2 (proposed) | 30 | 300 | 10 configs/param |

Phase 2 has lower coverage per parameter (10 vs 33), but the meta-parameters are binary flags (on/off) or small ranges, not continuous. The LHS sampling will adequately explore the space.

**Recommendation:** Run Phase 1 first. Analyze results. Then run Phase 2 with parameters selected based on Phase 1 findings (e.g., if signal fusion doesn't help, remove its parameters from Phase 2).

---

## 7. Risk Factors

| Risk | Likelihood | Impact | Mitigation |
|:-----|:----------:|:------:|:-----------|
| Phase 2 config space too large | Medium | High | Reduce to 200 configs, focus on binary flags first |
| Gate interactions unpredictable | Medium | Medium | Gate pipeline order is logical but untested |
| Sweep runtime too long | Medium | High | Phase 1: ~1h, Phase 2: ~2h — acceptable |
| Luck elimination computationally expensive | Low | Low | Run only on final best configs, not all configs |
| Goldilocks lane incompatible with daily sweep | Medium | High | Goldilocks is intraday — may need separate runner |

---

## 8. Output Specification

### Phase 1 Outputs

| File | Format | Content |
|:-----|:-------|:--------|
| `data/sweep_phase1_results.json` | JSON | Per-signal × per-config results |
| `data/sweep_phase1_summary.csv` | CSV | Per-signal best config + metrics |
| `data/sweep_phase1_top_configs.json` | JSON | Top 5 configs per signal |

### Phase 2 Outputs

| File | Format | Content |
|:-----|:-------|:--------|
| `data/sweep_phase2_results.json` | JSON | Per-meta-config results |
| `data/sweep_phase2_summary.csv` | CSV | Best meta-config + gate/lever/lane stats |
| `data/sweep_phase2_gate_stats.json` | JSON | Per-gate pass/fail counts |
| `data/sweep_phase2_luck_stats.json` | JSON | Luck-adjusted metrics |
| `data/sweep_phase2_portfolio.csv` | CSV | Portfolio-level correlation-adjusted metrics |

---

## 9. Migration Path

1. **Gilfoyle revises `scripts/big_sweep.py`** per this spec (4-6h)
2. **Build new modules**: gate_pipeline.py, lever_manager.py, lane_manager_v2.py, modulator_stack.py (2-3h)
3. **Run Phase 1** (~1h compute)
4. **Analyze Phase 1 results** (Donna + Gilfoyle, ~1h)
5. **Run Phase 2** (~2h compute)
6. **Final analysis** — correlation matrix, disposition table, expert panel

**Total: ~10-12h of AI/subagent work + ~3h compute**
