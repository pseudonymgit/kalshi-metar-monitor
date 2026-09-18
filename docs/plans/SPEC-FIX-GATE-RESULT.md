# SPEC: Fix GateResult Dict/DataClass Mismatch in Phase 2 Meta-Sweep

## 1. The Bug

**File:** `scripts/big_sweep.py`, line 929  
**Call:** `if not g_r.get("pass", True):`

`g_r` is a `GateResult` **dataclass** (from `core/gate_pipeline.py`), not a `dict`.  
Dataclass attributes are accessed via `g_r.passed`, not `g_r.get("pass", True)`.

Since `dataclass` objects have no `.get()` method, this raises an `AttributeError` on every iteration.  
The bare `except Exception: pass` on line 933 swallows the error, and **every trade candidate is silently skipped** — producing 0 trades for every meta-config.

---

## 2. The `GateResult` Dataclass (core/gate_pipeline.py)

**Location:** `core/gate_pipeline.py`, lines 25–32

```python
@dataclass
class GateResult:
    """Result of running the full gate pipeline for one trade candidate."""
    passed: bool
    reason: str                     # Human-readable summary of which gate blocked
    gate_stats: Dict[str, dict]     # Per-gate pass/fail with details
    modulated_confidence: float = 0.0
    modulated_direction: Optional[str] = None
```

**Fields:**
| Field | Type | Description |
|---|---|---|
| `passed` | `bool` | Whether all gates passed |
| `reason` | `str` | Human-readable summary |
| `gate_stats` | `Dict[str, dict]` | Per-gate pass/fail with details |
| `modulated_confidence` | `float` | Confidence after gate modulation (default 0.0) |
| `modulated_direction` | `Optional[str]` | Direction after gate modulation (default None) |

**Correct access:** `g_r.passed` (bool)

---

## 3. The Consumer (scripts/big_sweep.py, lines 885–940)

**`phase2_sweep()` function** (line 885):

```python
def phase2_sweep(args):
    ...
    from core.gate_pipeline import GatePipeline
    from core.lever_manager import LeverManager
    from core.modulator_stack import ModulatorStack
    ...
    for i, mc in enumerate(meta_configs):
        gp = GatePipeline(mc)
        lm = LeverManager(mc)
        mod = ModulatorStack(mc)

        trades = []
        for sname in list(top_configs.keys())[:10]:
            for station in STATIONS[:5]:
                try:
                    mod_r = mod.apply(station, sname, None, 0.5)             # line 927
                    g_r = gp.evaluate(sname, station, "", None, 0.5)         # line 928
                    if not g_r.get("pass", True):                            # line 929  ← BUG
                        continue
                    nc = lm.compute_position(sname, station, None, 0.5, 0.5, None)  # line 931
                    if nc > 0:
                        trades.append({"station": station, "signal": sname, "contracts": nc})
                except Exception:
                    pass                                                     # line 933
```

**Two additional API mismatches in the same block:**

---

## 4. API Mismatch #1: `GateResult.get("pass", True)` → `GateResult.passed`

**Location:** `scripts/big_sweep.py`, line 929

**Fix:**

```python
# BEFORE (broken — AttributeError on dataclass):
if not g_r.get("pass", True):
    continue

# AFTER (fixed — attribute access on dataclass):
if not g_r.passed:
    continue
```

---

## 5. API Mismatch #2: `LeverManager.compute_position()` called with `None` for positional args

**Location:** `scripts/big_sweep.py`, line 931

**Signature** (from `core/lever_manager.py`, line 61):

```python
def compute_position(
    self,
    signal_name: str,
    station: str,
    direction: str,       # ← required, 'up' or 'down'
    confidence: float,
    edge: float,
    market_price: float = 0.5,  # ← default 0.5
    variance: float = 0.25,
) -> int:
```

**Broken call:** `lm.compute_position(sname, station, None, 0.5, 0.5, None)`

Problems:
- `direction=None` — should be `"up"` or `"down"` (str)
- `market_price=None` — overrides the default `0.5` with `None`, causing `TypeError` in `_stage1_kelly_base` when `None <= 0.0` is evaluated

**Fix:**

```python
# BEFORE (broken — TypeError in stage1_kelly_base):
nc = lm.compute_position(sname, station, None, 0.5, 0.5, None)

# AFTER (fixed — uses proper types and defaults):
nc = lm.compute_position(sname, station, "up", 0.5, 0.5)
```

---

## 6. API Mismatch #3: `ModulatorStack.apply()` called with `None` for direction

**Location:** `scripts/big_sweep.py`, line 927

**Signature** (from `core/modulator_stack.py`, lines 109–111):

```python
def apply(
    self,
    station: str,
    signal_name: str,
    direction: str,       # ← required, 'up' or 'down'
    confidence: float,
    ...
) -> ModulatedResult:
```

**Broken call:** `mod.apply(station, sname, None, 0.5)`

Problems:
- `direction=None` — should be `"up"` or `"down"` (str)
- Internally, the trajectory gate tries `mod_dir.upper()` which raises `AttributeError`
- `mod_r` (the return value) is never used (dead variable)

**Fix:**

```python
# BEFORE (broken — direction=None, return value unused):
mod_r = mod.apply(station, sname, None, 0.5)

# AFTER (fixed — proper direction, or remove if unused):
mod_r = mod.apply(station, sname, "up", 0.5)
# OR simply remove the line if mod_r is truly unused
```

---

## 7. Root Cause Summary

| Line | File | Broken Expression | Actual Type | Correct Expression |
|---|---|---|---|---|
| 929 | `big_sweep.py` | `g_r.get("pass", True)` | `GateResult` dataclass | `g_r.passed` |
| 931 | `big_sweep.py` | `None` for `direction` | expects `str` | `"up"` |
| 931 | `big_sweep.py` | `None` for `market_price` | expects `float` | omit (default 0.5) |
| 927 | `big_sweep.py` | `None` for `direction` | expects `str` | `"up"` |

The bare `except Exception: pass` on line 933 masks all three bugs, making the sweep silently produce 0 trades.

---

## 8. Other Files Checked — No Similar Mismatches Found

| File/Class | Return Type | Consumed Via | Status |
|---|---|---|---|
| `core/gate_pipeline.py` `GateResult` | `@dataclass` | `g_r.passed` (by design) | ✅ No other consumers show `.get()` |
| `core/lever_manager.py` `LeverState` | `@dataclass` | Internal state — not passed to sweep | ✅ No mismatch |
| `core/lever_manager.py` `compute_position()` | `int` | `nc > 0` check | ✅ Comparison is correct for `int` |
| `core/modulator_stack.py` `ModulatedResult` | `@dataclass` | `mod_r` is unused in `phase2_sweep` | ✅ Declared but dead — no mismatch |
| `core/settlement_execution_gate.py` `GateResult` | Regular class | Accessed via `.verdict` in `gate_pipeline.py` | ✅ Proper usage |
| `scripts/data_integrity_gates.py` `GateResult` | Regular class | Accessed via `.passed` | ✅ Proper usage |

---

## 9. Exact Code Changes

### File: `scripts/big_sweep.py`

**Lines 927–933** — single atomic edit:

```python
# ── BEFORE ─────────────────────────────────────────────────────
                try:
                    mod_r = mod.apply(station, sname, None, 0.5)
                    g_r = gp.evaluate(sname, station, "", None, 0.5)
                    if not g_r.get("pass", True):
                        continue
                    nc = lm.compute_position(sname, station, None, 0.5, 0.5, None)
                    if nc > 0:
                        trades.append({"station": station, "signal": sname, "contracts": nc})
                except Exception:
                    pass
# ── AFTER (fixed) ──────────────────────────────────────────────
                try:
                    mod_r = mod.apply(station, sname, "up", 0.5)
                    g_r = gp.evaluate(sname, station, "", "up", 0.5)
                    if not g_r.passed:
                        continue
                    nc = lm.compute_position(sname, station, "up", 0.5, 0.5)
                    if nc > 0:
                        trades.append({"station": station, "signal": sname, "contracts": nc})
                except Exception:
                    pass
```

**Diff summary:**
1. `None` → `"up"` for `direction` in `mod.apply()` (line 927)
2. `None` → `"up"` for `direction` in `gp.evaluate()` (line 928)
3. `g_r.get("pass", True)` → `g_r.passed` (line 929)
4. `None, 0.5, None` → `0.5, 0.5` (drop trailing `None` for `market_price`, line 931)

---

## 10. Downstream Impact

After the fix, `GatePipeline.evaluate()` will properly return a `GateResult` with `g_r.passed == True/False`. The `phase2_sweep` function will correctly count trades that pass through the gate pipeline, producing non-zero trade counts for viable meta-configs.

**Note:** The `phase2_sweep` function uses stub values (`"up"`, `0.5`, empty string for date, no market data) for all calls. This is intentional for the meta-sweep — it only tests whether the pipeline **accepts** the candidate, not whether the trade is profitable. The stub values mean:
- `bss_threshold` must be below 0.0 (or station_skill gate disabled) for any trade to pass
- `settlement_gate_enabled` must be False (empty date will fail)
- `agreement_gate_enabled` must be False (single signal won't pass N-of-M)
- In practice, only meta-configs with most gates **disabled** will produce trades