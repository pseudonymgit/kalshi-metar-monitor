"""
config.py — Constants, parameter grids, config for the Kalshi accuracy sweep.

Includes pre-run integrity gate check via `run_integrity_gate()`.
Both sweep scripts (gefs_grid_sweep.py, kalshi_sweep_eval.py) call this at startup.
"""

import os, sys, math
import subprocess
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
SWEEP_DIR = DATA_DIR / "sweep"
DB_PATH = str(DATA_DIR / "kalshi_settlements.db")  # Ground truth: Kalshi settlements
GEFS_DB_PATH = str(DATA_DIR / "gefs_archive.db")   # Ensemble source

os.makedirs(SWEEP_DIR, exist_ok=True)

# Stations
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# Kalshi REAL fee model: 2.05¢ per contract round-trip ($0.0205)
ROUND_TRIP_FEE = 0.0205

# Slippage budget per trade (fraction of notional)
SLIPPAGE_BUDGET = 0.002  # 0.2% slippage on entry

# Fee type options
FEE_TYPE_OPTIONS = ["none", "taker_only", "taker_and_slippage", "maker"]

# Metrics thresholds
MIN_TRADES_REPORT = 30
MIN_TRADES_CALIBRATE = 50
TRAIN_DAYS = 180
TEST_DAYS = 30

# Signals
ACTIVE_SIGNALS = [
    "gaussian", "gaussian_v2", "pressure_delta", "forecast_disagreement",
    "calendar_climatology",
]

# Sweep parameter bounds (for LHS)
LEVER_BOUNDS = {
    "kelly_fraction": (0.1, 4.0),
    "edge_threshold": (0.001, 0.2),
    "stop_loss_pct": (0.02, 0.5),
    "max_contracts": (10, 1000),
    "confidence_floor": (0.5, 0.9),
    "entry_price_max": (0.1, 0.95),
    "entry_price_min": (0.01, 0.5),
    "trajectory_gate_enabled": (0, 1),     # boolean mapped to [0,1]
    "position_sizing_model": (0, 2),       # 0=fixed, 1=kelly, 2=tiered
    "stop_loss_kind": (0, 2),              # 0=none, 1=drawdown, 2=sequential
    "fee_type": (0, 3),                     # 0=none, 1=taker_only, 2=taker_and_slippage, 3=maker
    "fee_model": (0, 0),                   # always kalshi_real (index 0)
    "ensemble_source": (0, 0),             # always gefs_only (index 0)
}

LHS_N_SAMPLES = 5000
CHECKPOINT_INTERVAL = 500


# Gate names that are hard blocks (sweep aborts if they fail)
HARD_GATE_NAMES = [
    "GEFS completeness (≥30/31 members per date/station/step)",
    "ECMWF completeness (≥49/51 members per date/station/step)",
    "Station alignment: GEFS and ECMWF sample same stations",
    "Price sanity: entry_price between $0.01 and $0.99",
    "Fee sanity: fee_per_trade / notional ≤ 0.15",
]

def run_integrity_gate(skip: bool = False) -> bool:
    """
    Run the data integrity gate as a pre-sweep check.
    Returns True if all hard gates pass, False if any hard gate fails.
    Date alignment is a soft gate (warn but don't block) until ECMWF backfill completes.
    Set skip=True to bypass during development.
    """
    if skip:
        print("[config] Skipping integrity gate (skip=True)")
        return True
    
    gate_script = str(BASE_DIR / "scripts" / "data_integrity_gates.py")
    if not os.path.exists(gate_script):
        print(f"[config] WARNING: Integrity gate script not found at {gate_script}")
        return True  # Warn but don't block
    
    print("=" * 72)
    print("  RUNNING PRE-SWEEP DATA INTEGRITY GATES...")
    print("=" * 72)
    result = subprocess.run(
        [sys.executable, gate_script, "--json"],
        capture_output=True,
        text=True,
        cwd=str(BASE_DIR),
    )
    
    # Print the gate output
    print(result.stdout)
    if result.stderr:
        print("[config] Integrity gate stderr:", result.stderr.strip()[-500:])
    
    # Parse JSON output to determine pass/fail
    # The gate prints a human-readable table followed by a JSON block;
    # find the JSON object (starts with {) in the output.
    try:
        import json as _json
        out = result.stdout.strip()
        json_start = out.find("{")
        gate_output = _json.loads(out[json_start:]) if json_start >= 0 else _json.loads(out)
        gates = gate_output.get("gates", [])
        
        # Separate hard vs soft gates
        hard_failures = []
        soft_failures = []
        for g in gates:
            if not g.get("pass"):
                if g["name"] in HARD_GATE_NAMES:
                    hard_failures.append(g["name"])
                else:
                    soft_failures.append(g["name"])
        
        if hard_failures:
            print(f"[config] HARD FAILED gates: {hard_failures}")
            print("[config] Sweep ABORTED due to critical data integrity issues.")
        if soft_failures:
            print(f"[config] SOFT FAILED gates (warnings): {soft_failures}")
            print("[config] Proceeding with sweep despite soft failures.")
        if not hard_failures and not soft_failures:
            print("[config] All integrity gates PASS. Proceeding with sweep.")
        
        return len(hard_failures) == 0
    except Exception as e:
        print(f"[config] Could not parse integrity gate results: {e}")
        return False