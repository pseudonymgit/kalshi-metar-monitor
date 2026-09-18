#!/usr/bin/env python3
"""
l2_shared.py — Shared infrastructure for Layer 2 interaction experiments.

Provides:
- L2FoldGeometry: 2-fold temporal nested validation (Fold3/Fold4)
- L2ExperimentConfig: typed config for signal subset, station subset, fusion params
- L2Evaluator: evaluates arbitrary (signal_subset, station_subset, config) combinations
- COVERAGE_TIERS: per-station and per-cluster coverage definitions
- get_eligible_stations(), get_eligible_clusters(): tier-based station/cluster selection
- ALL_PAIRS, NON_DEGENERATE_PAIRS: all 136/78 signal pairs
- DummySignal: permutation-based dummy signal for negative controls

Usage:
    from l2_shared import L2Evaluator, L2ExperimentConfig, ALL_PAIRS
    config = L2ExperimentConfig(signal_names=["gaussian", "regime"])
    eval = L2Evaluator()
    result = eval.evaluate(config)
"""

import logging
import json
import os
import sys
import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set
from itertools import combinations
from dataclasses import dataclass, field

# Ensure we can import from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import sweep infrastructure
from scripts.ws5_common import (
    compute_fusion_accuracy, compute_pnl_metrics, compute_ground_truth_direction,
    load_settlement_data, load_daily_stats_batched,
)
from scripts.sweep.config import STATIONS as SWEEP_STATIONS, kalshi_fee, SLIPPAGE_BUDGET
from core.signals import SignalRegistry, REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES
from core.fusion_logic import SignalFusionEngine
from core.station_registry import STATION_CLUSTERS, get_cluster_for_station, get_cluster_stations

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# Fold geometry: 2-fold design (Fold3 + Fold4, per spec §2.2)
# ══════════════════════════════════════════════════════════════════════

L2_FOLDS = [
    {
        "fold": 3,
        "calibration_cutoff": "2025-11-30",
        "eval_start": "2025-12-01",
        "eval_end": "2026-02-28",
        "holdout_start": "2026-03-01",
        "holdout_end": "2026-05-14",
    },
    {
        "fold": 4,
        "calibration_cutoff": "2026-02-28",
        "eval_start": "2026-03-01",
        "eval_end": "2026-05-14",
        "holdout_start": "2026-05-15",
        "holdout_end": "2026-08-13",
    },
]

OUTER_HOLDOUT_START = "2026-05-15"
OUTER_HOLDOUT_END = "2026-08-13"

# ══════════════════════════════════════════════════════════════════════
# Coverage tiers (per spec §5.1)
# ══════════════════════════════════════════════════════════════════════

COVERAGE_TIERS = {
    "TIER-1": {"min_per_fold": 50, "description": "Primary analysis: signal isolation, pairs, triples"},
    "TIER-2": {"min_per_fold": 20, "description": "Secondary analysis: signal isolation, pairs"},
    "TIER-3": {"min_per_fold": 5,  "description": "Diagnostic only: signal isolation"},
    "MARGIN": {"min_per_fold": 0,  "description": "Excluded from all per-station claims"},
}


def get_eligible_stations(
    settlements: Dict[str, Dict[str, float]],
    min_tier: str = "TIER-2",
) -> List[str]:
    """
    Return stations meeting the minimum coverage tier across both eval folds.
    """
    tiers = {"TIER-1": 50, "TIER-2": 20, "TIER-3": 5, "MARGIN": 0}
    threshold = tiers.get(min_tier, 20)
    eligible = []
    for stn in SWEEP_STATIONS:
        if stn not in settlements:
            continue
        dates = sorted(settlements[stn].keys())
        counts = []
        for fd in L2_FOLDS:
            cnt = sum(1 for d in dates if fd["eval_start"] <= d <= fd["eval_end"])
            counts.append(cnt)
        if all(c >= threshold for c in counts):
            eligible.append(stn)
    return sorted(eligible)


def get_eligible_clusters(
    settlements: Dict[str, Dict[str, float]],
    min_tier: str = "TIER-2",
) -> Dict[str, List[str]]:
    """
    Return clusters where the aggregate per-fold count meets the tier.
    Cluster-level: sum of all stations in cluster ≥ threshold.
    """
    tiers = {"TIER-1": 50, "TIER-2": 20, "TIER-3": 5, "MARGIN": 0}
    threshold = tiers.get(min_tier, 20)
    result = {}
    for cname, stations in STATION_CLUSTERS.items():
        counts = [0, 0]
        for stn in stations:
            if stn not in settlements:
                continue
            dates = sorted(settlements[stn].keys())
            for i, fd in enumerate(L2_FOLDS):
                cnt = sum(1 for d in dates if fd["eval_start"] <= d <= fd["eval_end"])
                counts[i] += cnt
        if all(c >= threshold for c in counts):
            eligible = [s for s in stations if s in settlements]
            if eligible:
                result[cname] = eligible
    return result


# ══════════════════════════════════════════════════════════════════════
# Signal pair definitions
# ══════════════════════════════════════════════════════════════════════

ALL_SIGNAL_NAMES = list(REGISTERED_SIGNAL_NAMES)
NON_DEGENERATE_NAMES = [n for n in ALL_SIGNAL_NAMES if n not in DEGENERATE_SIGNAL_NAMES]

ALL_PAIRS = list(combinations(ALL_SIGNAL_NAMES, 2))          # C(17,2) = 136
NON_DEGENERATE_PAIRS = list(combinations(NON_DEGENERATE_NAMES, 2))  # C(13,2) = 78


def pair_label(pair: Tuple[str, str]) -> str:
    """Short label for a pair: 'signalA+signalB'."""
    return f"{pair[0]}+{pair[1]}"


# ══════════════════════════════════════════════════════════════════════
# DummySignal: permutation-based negative control
# ══════════════════════════════════════════════════════════════════════

class DummySignal:
    """
    Permutation-based dummy signal for negative control.
    
    Takes a real settlement-temperature series and permutes it,
    preserving temporal structure (autocorrelation, seasonality)
    while destroying the temperature→direction relationship.
    """
    name = "dummy"
    min_lookback = 0
    
    def __init__(self, seed: int = 0):
        self._rng = random.Random(seed)
        # Store the permutation mapping per station
        self._permutations: Dict[str, List[int]] = {}
    
    def fit(self, station: str, dates: List[str], temps: List[float]):
        """Generate a random permutation of the temperature series."""
        n = len(temps)
        perm = list(range(n))
        self._rng.shuffle(perm)
        self._permutations[station] = perm
        self._permuted_temps: Dict[str, List[float]] = {}
        self._permuted_temps[station] = [temps[p] for p in perm]
        self._dates = dates
    
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """
        Predict direction based on permuted temperature.
        Same structure as real signals: uses days[idx] and days[idx-1].
        """
        station = getattr(self, '_current_station', None)
        if station is None or station not in getattr(self, '_permuted_temps', {}):
            # Fallback: random prediction
            return ("up", 0.55) if self._rng.random() < 0.5 else ("down", 0.55)
        
        # Use permuted temperatures
        permuted = self._permuted_temps[station]
        if idx < 1 or idx >= len(permuted):
            return None  # Can't compute direction
        
        today_temp = permuted[idx]
        yesterday_temp = permuted[idx - 1]
        
        if today_temp is None or yesterday_temp is None:
            return None
        
        diff = today_temp - yesterday_temp
        if abs(diff) < 0.5:
            return None  # Flat
        
        direction = "up" if diff > 0 else "down"
        # Moderate confidence based on permuted data
        confidence = 0.55 + min(abs(diff) / 20.0, 0.35)
        return (direction, confidence)


def generate_dummy_signals(
    settlements: Dict[str, Dict[str, float]],
    n_dummies: int = 10,
) -> List[DummySignal]:
    """
    Generate n_dummies permutation-based dummy signals.
    Each dummy has its own random seed → different permutation.
    """
    dummies = []
    for seed in range(n_dummies):
        ds = DummySignal(seed=seed)
        for stn, date_temps in settlements.items():
            dates = sorted(date_temps.keys())
            temps = [date_temps[d] for d in dates]
            ds.fit(stn, dates, temps)
        dummies.append(ds)
    return dummies


# ══════════════════════════════════════════════════════════════════════
# Experiment config
# ══════════════════════════════════════════════════════════════════════

@dataclass
class L2ExperimentConfig:
    """Full configuration for a Layer 2 experiment evaluation."""
    
    # Signal selection
    signal_names: List[str] = field(default_factory=lambda: list(REGISTERED_SIGNAL_NAMES))
    exclude_degenerate: bool = False
    
    # Station selection
    station_names: List[str] = field(default_factory=lambda: list(SWEEP_STATIONS))
    cluster_name: Optional[str] = None  # If set, overrides station_names with cluster stations
    
    # Fusion parameters
    fusion_method: str = "llop"  # "llop", "uwc", "ensemble_avg"
    confidence_floor: float = 0.50
    kelly_fraction: float = 1.0
    
    # Agreement/conviction parameters
    use_agreement_gate: bool = False
    conviction_threshold: float = 0.20
    min_edge_threshold: float = 0.05
    agreement_factor_bias: float = 0.3
    
    # DS-conflict and diversity
    use_ds_conflict: bool = True
    use_diversity_modifier: bool = True

    # Direction-weighted diversity adapter (spec D.2, WS3/5)
    use_direction_weighted_adapter: bool = False
    
    # Capture per-trade predictions for matched-outcome comparison (WS7)
    capture_predictions: bool = False
    
    # Calibration
    calibration_method: str = "isotonic"
    
    # Metadata
    experiment_name: str = "l2_experiment"
    experiment_label: str = ""
    
    @property
    def label(self) -> str:
        if self.experiment_label:
            return self.experiment_label
        sigs = "sig" if len(self.signal_names) <= 5 else f"{len(self.signal_names)}sigs"
        stns = "all" if len(self.station_names) == len(SWEEP_STATIONS) else f"{len(self.station_names)}stn"
        return f"{self.fusion_method}_{sigs}_{stns}"


# ══════════════════════════════════════════════════════════════════════
# L2Evaluator: the core evaluation engine
# ══════════════════════════════════════════════════════════════════════

class L2Evaluator:
    """
    Evaluates arbitrary (signal_subset, station_subset, config) combinations
    using the 2-fold nested temporal design.
    """
    
    def __init__(self, settlements_db: str = "data/kalshi_settlements.db",
                 metar_db: str = "data/metar_backfill.db"):
        self.settlements_db = settlements_db
        self.metar_db = metar_db
        self._settlements: Optional[Dict[str, Dict[str, float]]] = None
        self._metar_batched: Optional[Dict[str, List[Dict]]] = None
        self._registry: Optional[SignalRegistry] = None
    
    def load_data(self):
        """Load settlements, METAR, and signal registry."""
        self._settlements = load_settlement_data(self.settlements_db)
        self._metar_batched = load_daily_stats_batched(self.metar_db, list(SWEEP_STATIONS))
        self._registry = SignalRegistry(db_path=self.metar_db)
        logger.info("Loaded %d stations, %d signals",
                     len(self._settlements), len(REGISTERED_SIGNAL_NAMES))
    
    @property
    def settlements(self) -> Dict[str, Dict[str, float]]:
        if self._settlements is None:
            self.load_data()
        return self._settlements
    
    @property
    def metar_batched(self) -> Dict[str, List[Dict]]:
        if self._metar_batched is None:
            self.load_data()
        return self._metar_batched
    
    @property
    def registry(self) -> SignalRegistry:
        if self._registry is None:
            self.load_data()
        return self._registry
    
    def evaluate_config(self, config: L2ExperimentConfig, fold: Optional[dict] = None) -> Dict[str, Any]:
        """
        Evaluate a single config on a single fold (or all folds).
        
        Returns dict with per-fold results and aggregate.
        """
        # Determine station list
        if config.cluster_name:
            stations = get_cluster_stations(config.cluster_name)
        else:
            stations = config.station_names
        
        # Filter to available settlements
        station_list = [s for s in stations if s in self.settlements and s in self.metar_batched]
        if not station_list:
            return {"error": "No eligible stations", "config": config.label, "trades": 0}
        
        # Build fuser with appropriate parameters
        fusion_method = config.fusion_method
        
        if fusion_method == "uwc":
            from core.fusion_logic import UncertaintyWeightedCascade, FusionModeConfig
            fconfig = FusionModeConfig(mode="uwc", floor_type="none")
            fuser = UncertaintyWeightedCascade(fconfig)
        else:
            # LLOP — standard SignalFusionEngine
            fuser = SignalFusionEngine(
                signal_names=config.signal_names,
                city_codes=station_list,
                use_direction_weighted_adapter=config.use_direction_weighted_adapter,
            )
        
        folds_to_run = [fold] if fold else L2_FOLDS
        
        fold_results = []
        for fd in folds_to_run:
            # Evaluate eval period
            eval_result = compute_fusion_accuracy(
                settlements=self.settlements,
                metar_batched=self.metar_batched,
                registry=self.registry,
                fuser=fuser,
                fusion_method=fusion_method,
                station_list=station_list,
                confidence_floor=config.confidence_floor,
                kelly_fraction=config.kelly_fraction,
                calibration_cutoff_date=fd["calibration_cutoff"],
                holdout_start=fd["holdout_start"],
                holdout_end=fd["holdout_end"],
                period="eval",
                exclude_degenerate=config.exclude_degenerate,
                calibration_method=config.calibration_method,
                eval_signal_names=config.signal_names,
                fold_id=fd["fold"],  # WS7 matched-outcome correction
            )
            
            # Evaluate holdout period
            holdout_result = compute_fusion_accuracy(
                settlements=self.settlements,
                metar_batched=self.metar_batched,
                registry=self.registry,
                fuser=fuser,
                fusion_method=fusion_method,
                station_list=station_list,
                confidence_floor=config.confidence_floor,
                kelly_fraction=config.kelly_fraction,
                calibration_cutoff_date=fd["calibration_cutoff"],
                holdout_start=fd["holdout_start"],
                holdout_end=fd["holdout_end"],
                period="holdout",
                exclude_degenerate=config.exclude_degenerate,
                calibration_method=config.calibration_method,
                eval_signal_names=config.signal_names,
                fold_id=fd["fold"],  # WS7 matched-outcome correction
            )
            
            fold_results.append({
                "fold": fd["fold"],
                "eval": {
                    "accuracy": eval_result.get("accuracy", 0.0),
                    "trades": eval_result.get("total_trades", 0),
                    "calibration_quality": eval_result.get("calibration_quality", {}),
                    "per_signal": eval_result.get("per_signal", {}),
                    "per_station": eval_result.get("per_station", {}),
                    "failure_counters": eval_result.get("failure_counters", {}),
                    "predictions": eval_result.get("predictions", []) if config.capture_predictions else [],
                },
                "holdout": {
                    "accuracy": holdout_result.get("accuracy", 0.0),
                    "trades": holdout_result.get("total_trades", 0),
                    "calibration_quality": holdout_result.get("calibration_quality", {}),
                    "per_signal": holdout_result.get("per_signal", {}),
                    "per_station": holdout_result.get("per_station", {}),
                    "failure_counters": holdout_result.get("failure_counters", {}),
                },
            })
        
        # Outer holdout (sealed window)
        outer_ho = compute_fusion_accuracy(
            settlements=self.settlements,
            metar_batched=self.metar_batched,
            registry=self.registry,
            fuser=fuser,
            fusion_method=fusion_method,
            station_list=station_list,
            confidence_floor=config.confidence_floor,
            kelly_fraction=config.kelly_fraction,
            calibration_cutoff_date=L2_FOLDS[-1]["calibration_cutoff"],
            holdout_start=OUTER_HOLDOUT_START,
            holdout_end=OUTER_HOLDOUT_END,
            period="holdout",
            exclude_degenerate=config.exclude_degenerate,
            calibration_method=config.calibration_method,
            eval_signal_names=config.signal_names,
            fold_id=999,  # WS7 matched-outcome correction: outer holdout marker
        )
        
        # Aggregate
        eval_accs = [fr["eval"]["accuracy"] for fr in fold_results]
        mean_eval_acc = sum(eval_accs) / len(eval_accs) if eval_accs else 0.0
        
        return {
            "config": config.label,
            "experiment_name": config.experiment_name,
            "signal_count": len(config.signal_names),
            "station_count": len(station_list),
            "station_list": station_list,
            "cluster": config.cluster_name,
            "fusion_method": fusion_method,
            "use_agreement_gate": config.use_agreement_gate,
            "conviction_threshold": config.conviction_threshold,
            "use_ds_conflict": config.use_ds_conflict,
            "use_diversity_modifier": config.use_diversity_modifier,
            "confidence_floor": config.confidence_floor,
            "fold_results": fold_results,
            "mean_eval_accuracy": round(mean_eval_acc, 4),
            "outer_holdout": {
                "accuracy": outer_ho.get("accuracy", 0.0),
                "trades": outer_ho.get("total_trades", 0),
                "calibration_quality": outer_ho.get("calibration_quality", {}),
                "per_signal": outer_ho.get("per_signal", {}),
                "per_station": outer_ho.get("per_station", {}),
            },
            "provenance": {
                "settlements_db": self.settlements_db,
                "metar_db": self.metar_db,
                "n_registered_signals": len(REGISTERED_SIGNAL_NAMES),
                "n_degenerate_signals": len(DEGENERATE_SIGNAL_NAMES),
                "n_stations": len(station_list),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }


# ══════════════════════════════════════════════════════════════════════
# Full-17 baseline control
# ══════════════════════════════════════════════════════════════════════

def run_full_baseline(evaluator: L2Evaluator, output_path: str = None) -> Dict[str, Any]:
    """Run the full-17-signal baseline as negative control for all Layer 2 experiments."""
    config = L2ExperimentConfig(
        signal_names=list(REGISTERED_SIGNAL_NAMES),
        experiment_name="full_17_baseline",
        experiment_label="full17_llop_cf50",
    )
    result = evaluator.evaluate_config(config)
    if output_path:
        with open(output_path, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        logger.info("Full baseline written to %s", output_path)
    return result


# ══════════════════════════════════════════════════════════════════════
# Quick test: verify infrastructure loads correctly
# ══════════════════════════════════════════════════════════════════════

def self_test():
    """Run quick self-test to verify infrastructure loads and computes correctly."""
    print("=" * 60)
    print("L2 SHARED INFRASTRUCTURE — SELF TEST")
    print("=" * 60)
    
    # 1. Pair counts
    assert len(ALL_PAIRS) == 136, f"Expected 136 pairs, got {len(ALL_PAIRS)}"
    assert len(NON_DEGENERATE_PAIRS) == 78, f"Expected 78 non-degenerate, got {len(NON_DEGENERATE_PAIRS)}"
    print(f"  Pairs: {len(ALL_PAIRS)} all, {len(NON_DEGENERATE_PAIRS)} non-degenerate")
    
    # 2. Fold geometry
    assert len(L2_FOLDS) == 2, f"Expected 2 folds, got {len(L2_FOLDS)}"
    for fd in L2_FOLDS:
        assert fd["calibration_cutoff"] < fd["eval_start"], f"Fold {fd['fold']}: cutoff not before eval"
        assert fd["eval_start"] < fd["holdout_start"], f"Fold {fd['fold']}: eval not before holdout"
    print(f"  Folds: 2-fold design (Fold3, Fold4) — calibration <= eval < holdout")
    
    # 3. Load data
    eval = L2Evaluator()
    eval.load_data()
    print(f"  Data: {len(eval.settlements)} stations, {len(eval.metar_batched)} with METAR")
    
    # 4. Eligible stations
    eligible = get_eligible_stations(eval.settlements, "TIER-2")
    assert len(eligible) >= 4, f"Expected >=4 eligible stations, got {len(eligible)}"
    print(f"  Eligible (TIER-2): {len(eligible)} stations — {', '.join(eligible[:5])}...")
    
    # 5. Eligible clusters
    clusters = get_eligible_clusters(eval.settlements, "TIER-2")
    assert len(clusters) >= 2, f"Expected >=2 eligible clusters, got {len(clusters)}"
    print(f"  Eligible clusters (TIER-2): {list(clusters.keys())}")
    
    # 6. Single signal isolation
    from core.signals import REGISTERED_SIGNAL_NAMES
    sig = REGISTERED_SIGNAL_NAMES[0]
    config = L2ExperimentConfig(
        signal_names=[sig],
        station_names=eligible[:2],
        experiment_name="self_test",
        experiment_label=f"isolation_{sig}",
    )
    result = eval.evaluate_config(config)
    assert result.get("fold_results"), f"No fold results for {sig}"
    print(f"  Isolation ({sig}): {len(result['fold_results'])} folds, "
          f"eval acc={result['mean_eval_accuracy']:.4f}")
    
    # 7. Pair evaluation
    pair = ALL_PAIRS[0]
    config2 = L2ExperimentConfig(
        signal_names=list(pair),
        station_names=eligible[:2],
        experiment_name="self_test",
        experiment_label=f"pair_{pair_label(pair)}",
    )
    result2 = eval.evaluate_config(config2)
    assert result2.get("fold_results"), f"No fold results for pair {pair}"
    print(f"  Pair ({pair_label(pair)}): {len(result2['fold_results'])} folds, "
          f"eval acc={result2['mean_eval_accuracy']:.4f}")
    
    # 8. Dummy signal
    from core.signals import REGISTERED_SIGNAL_NAMES
    dummies = generate_dummy_signals(eval.settlements, n_dummies=3)
    print(f"  Dummy signals: {len(dummies)} generated")
    
    # 9. Diversity adapter
    config3 = L2ExperimentConfig(
        signal_names=list(REGISTERED_SIGNAL_NAMES)[:3],
        station_names=eligible[:2],
        use_diversity_modifier=True,
        experiment_name="self_test",
        experiment_label="diversity_on",
    )
    config4 = L2ExperimentConfig(
        signal_names=list(REGISTERED_SIGNAL_NAMES)[:3],
        station_names=eligible[:2],
        use_diversity_modifier=False,
        experiment_name="self_test",
        experiment_label="diversity_off",
    )
    r3 = eval.evaluate_config(config3)
    r4 = eval.evaluate_config(config4)
    print(f"  Diversity ON: {r3['mean_eval_accuracy']:.4f}, OFF: {r4['mean_eval_accuracy']:.4f}")
    
    print(f"\n  {'=' * 50}")
    print(f"  SELF TEST PASSED — Infrastructure ready")
    print(f"  {'=' * 50}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    self_test()