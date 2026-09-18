# Spec: Fusion Engine A/B Parameter in Sweep

## Problem

The sweep infrastructure needs to test both LLOP (Log-Odds Linear Opinion Pool) and UWC (Uncertainty-Weighted Cascade) as the fusion engine to determine which performs better. Currently, the unified backtest uses `SignalFusionEngine.fuse_signals()` (LLOP) from `core/fusion_logic.py`, while `core/signal_fusion.py` has a full `UncertaintyWeightedCascade` implementation. There is no sweep parameter to switch between them, and no output format adapter to make results comparable.

## Current State

### LLOP fusion (`core/fusion_logic.py`)

- **Entry point**: `SignalFusionEngine.fuse_signals(signals, city_code, use_ds_conflict=True)`
- **Input**: `list[(signal_name, direction, raw_confidence)]` — flat list of signal tuples
- **Output**: `(fused_direction, fused_prob, remaining_confidence)` — 3-tuple where `fused_direction` is `"up"`/`"down"/None`, `fused_prob` is probability of UP
- **Also provides**: `compute_conviction_score()` returning float, `should_take_trade()` returning (bool, reason, score)
- **Used by**: `unified_backtest.py` (primary), `paper_trading_engine.py` (secondary)

### UWC fusion (`core/signal_fusion.py`)

- **Entry point**: `UncertaintyWeightedCascade.fuse(signal_predictions, market_price=0.5, ...)` — 10+ parameters
- **Input**: `dict[str, tuple[str | int, float]]` — mapping signal_name → (direction, raw_confidence)
- **Output**: `dict` (diagnostic ledger) with keys including:
  - `"direction"`: `"up"` or `"down"`
  - `"confidence"`: float 0-1
  - `"bayesian_conf"`: float
  - `"n_signals_agree"`: int
  - `"edge_effective"`: float
  - `"verdict"`: dict with trade decision info
  - `"null_result"`: bool, reason
- **Significantly more inputs**: requires `market_price`, `fee_rate`, `bankroll`, `station_tier`, `hours_to_settlement`, `goldilocks_probability`, `goldilocks_epsilon_f`, `family_b_signals`, `family_c_signals`, modulators

### Sweep infrastructure

- `scripts/sweep/` directory exists with `config.py`, `data.py`, `tiers.py`, but **no `run_sweep.py`**.
- `scripts/big_sweep.py` exists but is likely the previous monolithic sweep version.
- `scripts/sweep_engine.py` exists at the scripts root.
- `scripts/per_parameter_sweep.py` also exists.

### Key gap

The two fusion engines have **completely different input signatures** and **completely different output formats**. A sweep parameter requires an adapter layer that:

1. Normalizes inputs to both engines
2. Normalizes outputs to a common format
3. Handles the richer UWC parameter set (either with defaults or from sweep config)

## Design

### Parameter definition

Add a sweep configuration parameter `fusion_engine` with values `["llop", "uwc"]`.

### Adapter layer design

Create a `FusionEngineAdapter` that wraps both engines with a common interface:

```python
class FusionEngineAdapter:
    """Adapter providing a common interface for LLOP and UWC fusion engines."""

    def __init__(self, engine: str = "llop", config: Optional[dict] = None):
        self.engine = engine
        self.config = config or {}
        self._init_engine()

    def _init_engine(self):
        if self.engine == "llop":
            from core.fusion_logic import SignalFusionEngine
            self._impl = SignalFusionEngine(**self.config.get("llop_kwargs", {}))
        elif self.engine == "uwc":
            from core.signal_fusion import UncertaintyWeightedCascade
            from core.signal_fusion import FusionModeConfig
            uwc_config = FusionModeConfig(**self.config.get("uwc_kwargs", {}))
            self._impl = UncertaintyWeightedCascade(uwc_config)

    def fuse(self, signals, market_data: dict) -> dict:
        """Fuse signals and return a standardized result dict."""
        if self.engine == "llop":
            return self._fuse_llop(signals, market_data)
        elif self.engine == "uwc":
            return self._fuse_uwc(signals, market_data)
```

### Shared output format

Both engines must produce a result dict with at minimum:

```python
{
    "direction": "up" | "down" | None,  # None = no trade
    "confidence": float,                 # 0.0-1.0
    "probability_up": float,             # 0.0-1.0
    "n_signals_agree": int,              # signals matching direction
    "total_signals": int,                # total input signals
    "edge": float,                       # |prob - 0.5|
    "engine": "llop" | "uwc",           # which engine produced the result
    "diagnostic": dict,                  # engine-specific debug info
    "null_result": bool,                 # True if engine rejected the trade
    "null_reason": str,                  # why it was rejected
}
```

### Sweep integration

The sweep runner (new `scripts/run_sweep.py` or extended `scripts/per_parameter_sweep.py`) should:

1. Accept `--fusion-engine` as a CLI parameter
2. Pass it through to `FusionEngineAdapter`
3. Run the full sweep for each fusion engine value
4. Output results in the normal sweep format with an added `fusion_engine` column

## Implementation Steps

### Step 1: Create `FusionEngineAdapter`

File: `core/fusion_adapter.py` (new)

Implement the adapter class with:
- `__init__(engine, config)` — selects and initializes the engine
- `_fuse_llop(signals, market_data)` — converts signal list to LLOP input, calls `fuse_signals()` and `compute_conviction_score()`, produces standard output
- `_fuse_uwc(signals, market_data)` — converts signal dict to UWC input, fills in default values for UWC-only parameters, calls `fuse()`, extracts standard output
- `_null_result(reason)` — produces the standard null output

### Step 2: Verify/revert `run_sweep.py`

Determine which script is the canonical sweep runner:

```bash
# Check if scripts/sweep/run_sweep.py should exist
ls scripts/sweep/run_sweep.py  # Not found
ls scripts/run_sweep.py  # Not found
ls scripts/per_parameter_sweep.py  # Found
ls scripts/big_sweep.py  # Found — this is the big one
```

Either extend `scripts/per_parameter_sweep.py` to accept `--fusion-engine` and use the adapter, or create `scripts/run_sweep.py` as a thin wrapper.

Recommendation: Create `scripts/run_sweep.py` as a new minimal sweep runner that integrates the adapter.

### Step 3: Wire adapter into sweep runner

File: `scripts/run_sweep.py` (new design sketch)

```python
#!/usr/bin/env python3
"""
Sweep Runner — Tests fusion engine A/B (LLOP vs UWC).

Usage:
    python3 scripts/run_sweep.py --fusion-engine llop --output results_llop.json
    python3 scripts/run_sweep.py --fusion-engine uwc --output results_uwc.json
"""

import argparse
import json
from core.fusion_adapter import FusionEngineAdapter

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fusion-engine", choices=["llop", "uwc"], default="llop")
    parser.add_argument("--output", required=True)
    # ... other sweep params
    args = parser.parse_args()

    adapter = FusionEngineAdapter(engine=args.fusion_engine)
    # ... sweep logic using adapter.fuse()

if __name__ == "__main__":
    main()
```

### Step 4: Update unified_backtest.py

Add `fusion_engine` parameter to the backtest config. When `fusion_engine="uwc"`, use the adapter with UWC backend. The backtest must provide the additional UWC parameters (`market_price`, `fee_rate`, `bankroll` — already available) and populate `family_b_signals` and `family_c_signals` (may be empty lists if not available).

## Changed Files

| File | Change Type | Description |
|---|---|---|
| `core/fusion_adapter.py` | CREATE | New adapter class wrapping both LLOP and UWC |
| `scripts/run_sweep.py` | CREATE | New minimal sweep runner with fusion engine param |
| `core/unified_backtest.py` | MODIFY | Add `fusion_engine` parameter to backtest config |
| No change needed | VERIFY | Existing `signal_fusion.py` and `fusion_logic.py` remain untouched |

## Verification Steps

```bash
# 1. Verify adapter imports and initializes both engines
python3 -c "
from core.fusion_adapter import FusionEngineAdapter
llop_adapter = FusionEngineAdapter(engine='llop')
uwc_adapter = FusionEngineAdapter(engine='uwc')
print(f'LLOP engine: {llop_adapter.engine}')
print(f'UWC engine: {uwc_adapter.engine}')
print('OK')
"

# 2. Verify adapter produces same-format output from both engines
python3 -c "
from core.fusion_adapter import FusionEngineAdapter
test_signals = {'sig_a': ('up', 0.7), 'sig_b': ('down', 0.6)}
market_data = {'market_price': 0.5, 'fee_rate': 0.07, 'bankroll': 10000.0}

llop_result = FusionEngineAdapter(engine='llop').fuse(test_signals, market_data)
uwc_result = FusionEngineAdapter(engine='uwc').fuse(test_signals, market_data)

required_keys = ['direction', 'confidence', 'probability_up', 'engine']
for key in required_keys:
    assert key in llop_result, f'LLOP missing {key}'
    assert key in uwc_result, f'UWC missing {key}'
print('Both engines produce compatible output format')
print('OK')
"

# 3. Verify sweep runner runs
python3 scripts/run_sweep.py --fusion-engine llop --output /tmp/sweep_llop.json
python3 scripts/run_sweep.py --fusion-engine uwc --output /tmp/sweep_uwc.json
echo "Sweep exit codes: $?"

# 4. Compare results (both should produce similar structures)
python3 -c "
import json
llop = json.load(open('/tmp/sweep_llop.json'))
uwc = json.load(open('/tmp/sweep_uwc.json'))
print(f'LLOP trades: {llop.get(\"n_trades\", \"N/A\")}')
print(f'UWC trades: {uwc.get(\"n_trades\", \"N/A\")}')
print('OK')
"
```

## Rollback Plan

1. **Adapter**: Delete `core/fusion_adapter.py`.
2. **Sweep runner**: Delete `scripts/run_sweep.py`.
3. **unified_backtest.py**: Revert the `fusion_engine` parameter change.
4. If the adapter's output format is wrong, adjust the field extraction in `_fuse_llop()` or `_fuse_uwc()` — the source engines are not modified.

## Out of Scope

- Removing or deprecating the old fusion engine — both remain available.
- Adding new fusion algorithms (e.g., ensemble of both UWC and LLOP).
- Tuning UWC hyperparameters in the sweep — the adapter uses defaults from `DEFAULT_FUSION_CONFIG`.
- The `scripts/per_parameter_sweep.py` and `scripts/big_sweep.py` are left as-is; this spec creates a new focused sweep runner.
- Adding `family_b_signals` and `family_c_signals` generation logic — these can be empty lists if not available.