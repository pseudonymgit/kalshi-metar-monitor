#!/usr/bin/env python3
"""
ws7_ablation_beam_corrected.py — WS7 ablation/beam (QC2-corrected).

Implements WS7-ABLATION-BEAM-CORRECTION-SPEC:
- T11 arm: full-17 ensemble + adapter vs T0 (full-17 baseline)
- McNemar paired T3-vs-T2 test on matched station+date population
- Normalized arm IDs: T2, T3, T3a, T3b, T11
- Beam family fixed at 15 candidates, alpha=0.00333 everywhere
- Integer correct/trades accuracy (no float accumulation)
- Degenerate/dead signals excluded from inferential selection

Preserves all prior artifacts. New timestamped outputs only.
"""
import sys, os, json, logging, time, hashlib, itertools
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING)

from core.signals import REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES
from core.signals.honest_trend_signal import HonestTrendSignal
from core.statistical_significance import (
    three_condition_gate, bonferroni_alpha,
    serialize_adapter_config, beam_selection_family,
    mcnemar_test, paired_comparison_fields, integer_accuracy,
    ADAPTER_FAMILY_COMPARISONS, ADAPTER_FAMILY_ALPHA,
)
from scripts.l2_shared import L2Evaluator, L2ExperimentConfig, L2_FOLDS, get_eligible_stations
from scripts.sweep.config import STATIONS as SWEEP_STATIONS
from scripts.ws5_common import compute_baseline_accuracies

TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
OUT_DIR = "data"

eval_ready = L2Evaluator()
eval_ready.load_data()
settlements = eval_ready.settlements
eligible_11 = get_eligible_stations(settlements, "TIER-2")
tier1 = ["KDEN", "KLAX", "KMDW"]
FULL17 = list(REGISTERED_SIGNAL_NAMES)
DEG = list(DEGENERATE_SIGNAL_NAMES)
DEAD = ["asos_microstructure", "temperature_trend"]
VALID = [s for s in FULL17 if s not in DEG and s not in DEAD]
ALL_20 = list(SWEEP_STATIONS)
assert len(ALL_20) == 20

reg = eval_ready.registry
if reg.get_signal("honest_trend_B") is None:
    reg.add_signal("honest_trend_B", HonestTrendSignal())

# Baseline
baselines = {}
for fd in L2_FOLDS:
    bl = compute_baseline_accuracies(settlements=settlements, station_list=ALL_20,
                                     holdout_start=fd["holdout_start"], holdout_end=fd["holdout_end"], period="eval")
    baselines[fd["fold"]] = {"trend_B": bl.get("trend_accuracy", 0)}
baseline = max(b["trend_B"] for b in baselines.values())
print(f"Baseline (max trend_B): {baseline:.4f}")

# ── Core runner with integer accuracy ────────────────────────────────
def run_arm_integer(name, signals, extra_config, family="adapter_4", capture_preds=False):
    """Run an arm; compute accuracy from integer correct/trades per fold."""
    t0 = time.time()
    cfg = L2ExperimentConfig(signal_names=signals, station_names=ALL_20, experiment_name=name,
                             capture_predictions=capture_preds)
    for k, v in extra_config.items():
        setattr(cfg, k, v)
    result = eval_ready.evaluate_config(cfg)
    res = result.get("result", result) if isinstance(result, dict) else result
    elapsed = time.time() - t0

    # Integer accumulation per fold (spec I5)
    total_trades = 0
    total_correct = 0
    fr = res.get("fold_results", []) if isinstance(res, dict) else []
    for fd in fr:
        ev = fd.get("eval", {})
        tr = int(ev.get("trades", 0))
        acc = ev.get("accuracy", 0)
        total_trades += tr
        total_correct += int(round(acc * tr))  # round at fold level, sum integers
    acc = integer_accuracy(total_trades, total_correct)

    stats = three_condition_gate(total_correct, total_trades, baseline, family=family, accuracy=acc)
    stats["trades"] = total_trades
    stats["correct"] = total_correct
    stats["accuracy"] = acc
    stats["config"] = serialize_adapter_config(
        use_direction_weighted_adapter=extra_config.get("use_direction_weighted_adapter", False),
        use_ds_conflict=extra_config.get("use_ds_conflict", True),
        use_diversity_modifier=extra_config.get("use_diversity_modifier", True),
    )
    stats["outer_holdout_acc"] = res.get("outer_holdout", {}).get("accuracy", "N/A") if isinstance(res, dict) else "N/A"
    stats["elapsed_s"] = round(elapsed, 1)
    
    # Extract predictions if captured
    preds = []
    if capture_preds:
        fr = res.get("fold_results", []) if isinstance(res, dict) else []
        for fd in fr:
            preds.extend(fd.get("eval", {}).get("predictions", []))
    
    return stats, res, preds


# ══════════════════════════════════════════════════════════════════════
# SECTION 1: Adapter family on top valid pair (T2, T3, T3a, T3b)
# ══════════════════════════════════════════════════════════════════════
# Load pair artifact for top valid pair selection
with open(f"{OUT_DIR}/ws7_auth_pairs_20260814_221944.json") as f:
    pair_data = json.load(f)
valid_pairs = [p for p in pair_data["all_pairs"]
               if all(s in VALID for s in p["signals"]) and p["trades"] > 0]
valid_pairs.sort(key=lambda x: -x["accuracy"])
TOP_PAIR = valid_pairs[0]["signals"]
print(f"Top valid pair: {TOP_PAIR}")

arm_defs = [
    ("T2", {"use_direction_weighted_adapter": False, "use_ds_conflict": True, "use_diversity_modifier": True}),
    ("T3", {"use_direction_weighted_adapter": True, "use_ds_conflict": True, "use_diversity_modifier": True}),
    ("T3a", {"use_direction_weighted_adapter": True, "use_ds_conflict": True, "use_diversity_modifier": False}),
    ("T3b", {"use_direction_weighted_adapter": False, "use_ds_conflict": False, "use_diversity_modifier": False}),
]
print("\n=== ADAPTER FAMILY (alpha=0.0125) — normalized IDs ===")
arms = {}
all_preds = {}
for aid, cfg in arm_defs:
    capture = aid in ("T2", "T3")
    stats, res, preds = run_arm_integer(f"ws7_{aid}", TOP_PAIR, cfg, capture_preds=capture)
    arms[aid] = stats
    all_preds[aid] = preds
    print(f"  {aid:<4s} acc={stats['accuracy']:.4f} p_coin={stats['p_vs_coinflip']:.2e} sig={stats['significant']}")

# T3 vs T2 true matched McNemar
from core.statistical_significance import true_mcnemar_comparison
mcnemar = true_mcnemar_comparison(all_preds.get("T3", []), all_preds.get("T2", []), "T3", "T2")
arms["_t3_vs_t2_matched"] = mcnemar
print(f"  T3-vs-T2 matched: b={mcnemar['b']}, c={mcnemar['c']}, n_matched={mcnemar['n_matched']}, p={mcnemar['p_value']:.4f}")

# ══════════════════════════════════════════════════════════════════════
# SECTION 2: T11 arm (full-17 ensemble + adapter vs T0)
# ══════════════════════════════════════════════════════════════════════
print("\n=== T11: FULL-17 ENSEMBLE ADAPTER vs T0 ===")
t0_cfg = {"use_direction_weighted_adapter": False, "use_ds_conflict": True, "use_diversity_modifier": True}
t11_cfg = {"use_direction_weighted_adapter": True, "use_ds_conflict": True, "use_diversity_modifier": True}
t0_stats, t0_res, t0_preds = run_arm_integer("ws7_T0", FULL17, t0_cfg, family="adapter_4", capture_preds=True)
t11_stats, t11_res, t11_preds = run_arm_integer("ws7_T11", FULL17, t11_cfg, family="adapter_4", capture_preds=True)
arms["T0"] = t0_stats
arms["T11"] = t11_stats
from core.statistical_significance import true_mcnemar_comparison
mcnemar_t0t11 = true_mcnemar_comparison(t11_preds, t0_preds, "T11", "T0")
arms["_t11_vs_t0_matched"] = mcnemar_t0t11
print(f"  T0  acc={t0_stats['accuracy']:.4f} sig={t0_stats['significant']}")
print(f"  T11 acc={t11_stats['accuracy']:.4f} sig={t11_stats['significant']}")
print(f"  T11-vs-T0 matched: b={mcnemar_t0t11['b']}, c={mcnemar_t0t11['c']}, n_matched={mcnemar_t0t11['n_matched']}, p={mcnemar_t0t11['p_value']:.4f}")

# ══════════════════════════════════════════════════════════════════════
# SECTION 3: Beam search (fixed 15-candidate family, alpha=0.00333)
# ══════════════════════════════════════════════════════════════════════
print(f"\n=== BEAM SEARCH (alpha=0.00333, 15-candidate family) ===")
remaining = [s for s in VALID if s not in TOP_PAIR]
bm = beam_selection_family(TOP_PAIR, 15)  # fixed at 15 per predeclared family
beam_results = {"base_pair": TOP_PAIR, "selection_family": bm, "triples": {}}
for sig in remaining:
    triple = TOP_PAIR + [sig]
    stats, _, _ = run_arm_integer(f"beam_{sig}", triple, {}, family="beam_15")
    beam_results["triples"][sig] = stats
    print(f"  +{sig:<35s} acc={stats['accuracy']:.4f} sig={stats['significant']}")
# Fill missing candidates (to reach 15) with p=1.0, significant=False
all_candidates = [s for s in VALID if s not in TOP_PAIR]
for sig in all_candidates:
    if sig not in beam_results["triples"]:
        beam_results["triples"][sig] = {"accuracy": 0.0, "significant": False,
                                        "p_vs_coinflip": 1.0, "trades": 0}
best = max(beam_results["triples"].items(), key=lambda x: x[1]["accuracy"] if x[1]["trades"] > 0 else 0)
beam_results["best_triple"] = {"signal": best[0], "stats": best[1]}

# ══════════════════════════════════════════════════════════════════════
# Write artifacts
# ══════════════════════════════════════════════════════════════════════
provenance = {
    "selection_filter": {
        "exclude": ["DEGENERATE_SIGNAL_NAMES", "dead_signals", "zero_trades"],
        "valid_signals": VALID,
    },
    "pair_source": "data/ws7_auth_pairs_20260814_221944.json",
    "baseline": baseline,
    "dummy_pairs": "excluded, not executed — no-control limitation",
    "statistical_protocol": "WS7-ABLATION-BEAM-CORRECTION-SPEC-2026-08-15",
    "accuracy_method": "integer correct/trades per fold, no float accumulation",
}

abl_out = f"{OUT_DIR}/ws7_auth_ablations_v3_{TS}.json"
with open(abl_out, "w") as f:
    json.dump({
        "experiment": "ws7_authoritative_ablations_v3",
        "generated_at": TS,
        "diagnostic_universe": "full_20",
        "top_pair": TOP_PAIR,
        "provenance": provenance,
        "adapter_family": {"comparisons": ADAPTER_FAMILY_COMPARISONS,
                           "alpha_prime": ADAPTER_FAMILY_ALPHA, "predeclared": True},
        "arms": arms,
    }, f, indent=2, default=str)
print(f"\nWritten: {abl_out} ({os.path.getsize(abl_out)} bytes)")

beam_out = f"{OUT_DIR}/ws7_auth_beam_v3_{TS}.json"
with open(beam_out, "w") as f:
    json.dump({
        "experiment": "ws7_authoritative_beam_v3",
        "generated_at": TS,
        "diagnostic_universe": "full_20",
        "provenance": provenance,
        **beam_results,
    }, f, indent=2, default=str)
print(f"Written: {beam_out} ({os.path.getsize(beam_out)} bytes)")

# Verify pair artifact unchanged
with open("data/ws7_auth_pairs_20260814_221944.json", "rb") as f:
    ph = hashlib.sha256(f.read()).hexdigest()[:12]
print(f"\nPair artifact: {ph}... (expected f60790b9...: {ph == 'f60790b997b4'})")
print("\n=== WS7 ABLATION/BEAM v3 COMPLETE ===")