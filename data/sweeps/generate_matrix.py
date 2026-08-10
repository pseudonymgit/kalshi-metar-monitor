#!/usr/bin/env python3
"""
Generate regime_config_matrix.json from per-regime sweep results.
Run after all sweeps complete.
"""
import json, os, sys
from datetime import datetime

SWEEPS_DIR = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/sweeps"
OUTPUT = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/config/regime_config_matrix.json"

# Map filter values to the config params they constrain
REGIME_FILTER_PARAMS = {"season": "regime_season_filter", "region": "regime_region_filter", "cycle": "regime_cycle_filter"}

# Regime metadata mapping
REGIME_META = {
    "season_jja": {"season": "JJA", "region": "all", "cycle": "all"},
    "season_djf": {"season": "DJF", "region": "all", "cycle": "all"},
    "season_mam": {"season": "MAM", "region": "all", "cycle": "all"},
    "season_son": {"season": "SON", "region": "all", "cycle": "all"},
    "region_continental": {"season": "all", "region": "continental", "cycle": "all"},
    "region_marine": {"season": "all", "region": "marine", "cycle": "all"},
    "region_arid": {"season": "all", "region": "arid", "cycle": "all"},
    "region_subtropical": {"season": "all", "region": "subtropical", "cycle": "all"},
    "cycle_06z": {"season": "all", "region": "all", "cycle": "06z"},
    "cycle_12z": {"season": "all", "region": "all", "cycle": "12z"},
    "summer_continental": {"season": "JJA", "region": "continental", "cycle": "all"},
    "summer_marine": {"season": "JJA", "region": "marine", "cycle": "all"},
    "winter_continental": {"season": "DJF", "region": "continental", "cycle": "all"},
    "winter_marine": {"season": "DJF", "region": "marine", "cycle": "all"},
    "jja_06z": {"season": "JJA", "region": "all", "cycle": "06z"},
    "jja_12z": {"season": "JJA", "region": "all", "cycle": "12z"},
    "djf_06z": {"season": "DJF", "region": "all", "cycle": "06z"},
    "djf_12z": {"season": "DJF", "region": "all", "cycle": "12z"},
    "mam_06z": {"season": "MAM", "region": "all", "cycle": "06z"},
    "mam_12z": {"season": "MAM", "region": "all", "cycle": "12z"},
    "son_06z": {"season": "SON", "region": "all", "cycle": "06z"},
    "son_12z": {"season": "SON", "region": "all", "cycle": "12z"},
    "goldilocks": {"season": "all", "region": "all", "cycle": "all", "lane": "goldilocks"},
    "trajectory": {"season": "all", "region": "all", "cycle": "all", "lane": "trajectory"},
}

def load_regime(name):
    path = os.path.join(SWEEPS_DIR, name, "per_parameter_results.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  Error loading {name}: {e}", file=sys.stderr)
        return None

def extract_metrics(data):
    pm = data.get("projected_metrics", {})
    return {
        "accuracy": round(pm.get("accuracy", 0), 4),
        "sharpe": round(pm.get("sharpe", 0), 2),
        "trades_per_day": round(pm.get("trades_per_day", 0), 2),
        "total_pnl": round(pm.get("total_pnl", 0), 1),
        "max_drawdown": round(pm.get("max_drawdown", 0), 4),
        "n_trades": pm.get("n_trades", None),
        "n_trading_days": pm.get("n_trading_days", None),
    }

def extract_config(data):
    cfg = data.get("config", {})
    if not cfg:
        cfg = data.get("optimal_config", {})
    skip_keys = {"_meta", "meta", "level_0", "level_1", "level_2", "level_3", "level_4"}
    return {k: v for k, v in cfg.items() if k not in skip_keys}

def apply_filter_constraints(name, config, meta):
    """Force the regime filter params to the sweep's filter values.

    The sweep optimizes regime_*_filter as free params, so every regime's recorded
    config converges to the global best (DJF/continental/12z). But the metrics in
    each result were evaluated WITH the external filter applied (evaluate_config
    overrides these keys). To make each per-regime config consistent with its own
    filter-constrained metrics, we restore the regime filter to the sweep filter.
    The optimized non-regime params (edge_threshold, sizing, etc.) were also
    derived under that filter, so they remain per-regime-optimal.

    Also handles goldilocks/trajectory lane toggles: the P1 evaluator ignores
    these lanes, but we force the flag to 1 for the dedicated lane sweeps.
    """
    cfg = dict(config)
    for filt_key, cfg_key in REGIME_FILTER_PARAMS.items():
        val = meta.get(filt_key)
        if val is not None and val != "all":
            cfg[cfg_key] = val
    # Lane-specific fixes
    if "lane" in meta:
        if meta["lane"] == "goldilocks":
            cfg["goldilocks_lane_enabled"] = 1
        elif meta["lane"] == "trajectory":
            cfg["trajectory_lane_enabled"] = 1
    return cfg

def extract_meta(data):
    m = data.get("meta", {})
    return {
        "n_significant_params": m.get("n_significant_params", 0),
        "total_time_seconds": m.get("total_time_seconds", 0),
        "level_3_triggered": m.get("level_3_triggered", False),
        "walk_forward_passed": m.get("walk_forward_passed", False),
        "regime_sensitivity_flagged": m.get("regime_sensitivity_flagged", False),
    }

# Load all regimes
regimes = {}
for name, meta in REGIME_META.items():
    data = load_regime(name)
    if data is None:
        print(f"  MISSING: {name} - no results file")
        continue
    entry = {
        "season": meta["season"],
        "region": meta["region"],
        "cycle": meta["cycle"],
        "config": apply_filter_constraints(name, extract_config(data), meta),
        "metrics": extract_metrics(data),
        "sweep_meta": extract_meta(data),
    }
    if "lane" in meta:
        entry["lane"] = meta["lane"]
    regimes[name] = entry
    print(f"  Loaded {name}: acc={entry['metrics']['accuracy']:.3f} sharpe={entry['metrics']['sharpe']:.2f} tpd={entry['metrics']['trades_per_day']:.2f}")

# Find best/worst (use trades_per_day > 0.5 as sufficiency proxy; enrich later)
all_entries = [(k, v) for k, v in regimes.items() if v.get("metrics", {}).get("trades_per_day", 0) > 0.5]
if all_entries:
    best_by_acc = max(all_entries, key=lambda x: x[1]["metrics"]["accuracy"])
    best_by_sharpe = max(all_entries, key=lambda x: x[1]["metrics"]["sharpe"])
    worst_by_acc = min(all_entries, key=lambda x: x[1]["metrics"]["accuracy"])
    print(f"\n  Best accuracy: {best_by_acc[0]} ({best_by_acc[1]['metrics']['accuracy']:.3f})")
    print(f"  Best sharpe: {best_by_sharpe[0]} ({best_by_sharpe[1]['metrics']['sharpe']:.3f})")
    print(f"  Worst accuracy: {worst_by_acc[0]} ({worst_by_acc[1]['metrics']['accuracy']:.3f})")

# Build default config
default_config = {}
base_path = os.path.join(os.path.dirname(OUTPUT), "sweep-optimal.json")
if os.path.exists(base_path):
    base = json.load(open(base_path))
    default_config = {k: v for k, v in base.items() if not k.startswith("_")}
    print(f"\n  Default config from config/sweep-optimal.json")

# Assemble output
output = {
    "_meta": {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "engine": "per_parameter_sweep.py with bmode_p1_backtest evaluator (--fast mode)",
        "params_total": 42,
        "params_active": 13,
        "regimes_loaded": len(regimes),
        "regimes_requested": len(REGIME_META),
    },
    "regimes": regimes,
    "goldilocks": regimes.get("goldilocks", {"config": {}, "metrics": {"accuracy": 0, "sharpe": 0, "trades_per_day": 0}}),
    "trajectory": regimes.get("trajectory", {"config": {}, "metrics": {"accuracy": 0, "sharpe": 0, "trades_per_day": 0}}),
    "default_config": {
        "notes": "Fallback config used when no regime match. Source: config/sweep-optimal.json or sweep default.",
        "config": default_config
    },
    "analysis": {
        "best_accuracy": {
            "regime": best_by_acc[0] if all_entries else None,
            "accuracy": best_by_acc[1]["metrics"]["accuracy"] if all_entries else 0,
            "sharpe": best_by_acc[1]["metrics"]["sharpe"] if all_entries else 0,
        } if all_entries else None,
        "best_sharpe": {
            "regime": best_by_sharpe[0] if all_entries else None,
            "accuracy": best_by_sharpe[1]["metrics"]["accuracy"] if all_entries else 0,
            "sharpe": best_by_sharpe[1]["metrics"]["sharpe"] if all_entries else 0,
        } if all_entries else None,
        "worst_accuracy": {
            "regime": worst_by_acc[0] if all_entries else None,
            "accuracy": worst_by_acc[1]["metrics"]["accuracy"] if all_entries else 0,
        } if all_entries else None,
        "regimes_above_50pct": sum(1 for e in all_entries if e[1]["metrics"]["accuracy"] > 0.5),
        "regimes_above_sharpe_1": sum(1 for e in all_entries if e[1]["metrics"]["sharpe"] > 1.0),
        "regimes_above_1tpd": sum(1 for e in all_entries if e[1]["metrics"]["trades_per_day"] > 1.0),
    }
}

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w") as f:
    json.dump(output, f, indent=2, default=str)
print(f"\n  Written to {OUTPUT}")
print(f"  {len(regimes)}/{len(REGIME_META)} regimes loaded")