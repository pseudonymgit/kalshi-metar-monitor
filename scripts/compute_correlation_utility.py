#!/usr/bin/env python3
"""
Fast Correlation Matrix Utility — evaluates all 40 signals to produce
a pairwise Spearman ρ matrix for redundancy analysis.

Outputs (to data/):
  signal_correlation_matrix.json   — full pairwise matrix
  signal_correlation_summary.md    — redundancy classification + disposition
  signal_disposition.csv           — per-signal disposition (ADVANCE/PARK/KILL)

Usage:
    python3 scripts/compute_correlation_utility.py
"""

import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')

# Number of stations to evaluate per signal (trade-off speed vs coverage)
EVAL_STATIONS = ['KNYC', 'KATL', 'KBOS', 'KDCA', 'KDEN', 'KLAX', 'KMDW']  # 7 stations for diversity
MIN_PREDICTIONS_PER_SIGNAL = 30  # minimum predictions for correlation
MAX_DATES_PER_STATION = 99999  # no cap — iterate all available data

CLASSIFICATION_THRESHOLDS = [
    (0.7, "HIGH_REDUNDANCY", "Poor — one should be killed or fused"),
    (0.5, "MODERATE", "Fair — consider down-weighting one"),
    (0.3, "LOW", "Good — some overlap but useful together"),
    (0.0, "ORTHOGONAL", "Excellent — keep both, provide independent info"),
]


def classify_correlation(rho: float) -> Tuple[str, str]:
    """Classify a correlation coefficient."""
    rho = abs(rho)
    for threshold, cat, desc in CLASSIFICATION_THRESHOLDS:
        if rho >= threshold:
            return cat, desc
    return "ORTHOGONAL", "Excellent — keep both, provide independent info"


def evaluate_all_signals() -> Dict:
    """
    Evaluate all signals from the sweep registry on a subset of stations.
    Returns dict of signal_name -> list of prediction directions (1 for up, 0 for down).
    """
    from scripts.big_sweep import build_signal_registry, DataCache, safe_signal_evaluate

    registry = build_signal_registry()
    active = {k: v for k, v in registry.items() if v is not None}
    logger.info(f"Loaded {len(active)} signals from registry")

    # Activate data cache
    settlements = DataCache.get_settlements()
    logger.info(f"Settlements loaded: {len(settlements)} stations")

    signal_vectors: Dict[str, List[int]] = {}
    signal_metrics: Dict[str, dict] = {}

    for sname, sobj in sorted(active.items()):
        # Skip if callable and no evaluate method (function-based)
        if callable(sobj) and not hasattr(sobj, 'evaluate'):
            logger.debug(f"  Skipping {sname} (function-based)")
            continue

        all_directions: List[int] = []
        stations_used = set()

        for station in EVAL_STATIONS:
            days = DataCache.get_metar_data(station)
            if len(days) < 10:
                continue

            if station not in settlements:
                continue
            station_s = settlements[station]

            min_lb = 2
            if hasattr(sobj, 'min_lookback') and sobj.min_lookback is not None:
                try:
                    min_lb = max(1, int(sobj.min_lookback))
                except (ValueError, TypeError):
                    pass

            for idx in range(min_lb, len(days)):
                date_str = days[idx]['date']
                if date_str not in station_s:
                    continue

                actual_temp = station_s[date_str]
                prev_date = days[idx - 1]['date']
                if prev_date not in station_s:
                    continue
                prev_temp = station_s[prev_date]
                diff = actual_temp - prev_temp
                if diff == 0:
                    continue
                actual_dir = 1 if diff > 0 else -1
                actual_dir_str = 'up' if diff > 0 else 'down'

                pd, cf = safe_signal_evaluate(sobj, idx, days)
                if pd is None:
                    continue

                all_directions.append(1 if pd == actual_dir_str else 0)
                stations_used.add(station)

        if all_directions and len(all_directions) >= MIN_PREDICTIONS_PER_SIGNAL:
            signal_vectors[sname] = all_directions
            signal_metrics[sname] = {
                'n_trades': len(all_directions),
                'n_stations': len(stations_used),
                'accuracy': sum(all_directions) / len(all_directions),
            }
            logger.info(f"  {sname}: {len(all_directions)} trades across {len(stations_used)} stations "
                        f"(acc={signal_metrics[sname]['accuracy']:.3f})")
        else:
            logger.warning(f"  {sname}: insufficient trades ({len(all_directions) if all_directions else 0})")

    logger.info(f"\nEvaluated {len(signal_vectors)} signals with sufficient data")
    return {
        "signal_names": list(signal_vectors.keys()),
        "signal_vectors": signal_vectors,
        "signal_metrics": signal_metrics,
    }


def compute_matrix(signal_vectors: Dict[str, List]) -> Dict[str, Dict[str, float]]:
    """Compute pairwise Spearman ρ from direction vectors."""
    signals = list(signal_vectors.keys())
    matrix = {s: {} for s in signals}

    for i, si in enumerate(signals):
        for j, sj in enumerate(signals):
            if si == sj:
                matrix[si][sj] = 1.0
                continue

            vi = signal_vectors[si]
            vj = signal_vectors[sj]

            n = min(len(vi), len(vj))
            if n < 5:
                matrix[si][sj] = None
                continue

            try:
                rho, pval = spearmanr(vi[:n], vj[:n])
                matrix[si][sj] = round(float(rho), 4) if not np.isnan(rho) else 0.0
            except Exception:
                matrix[si][sj] = None

    return matrix


def classify_signal(matrix: Dict, signal: str, signal_names: List[str]) -> Dict:
    """Classify a signal based on its correlation with others."""
    cors = []
    redundant_pairs = []
    for other in signal_names:
        if signal == other:
            continue
        rho = matrix.get(signal, {}).get(other)
        if rho is not None and rho != 0.0:
            cors.append(abs(rho))
            if abs(rho) >= 0.7:
                redundant_pairs.append((other, rho))

    mean_abs = round(float(np.mean(cors)), 4) if cors else 0.0
    max_abs = round(float(np.max(cors)), 4) if cors else 0.0
    cat, desc = classify_correlation(max_abs)

    return {
        "mean_rho": mean_abs,
        "max_rho": max_abs,
        "classification": cat,
        "description": desc,
        "redundant_pairs": [{"signal": p[0], "rho": round(p[1], 4)} for p in
                            sorted(redundant_pairs, key=lambda x: -abs(x[1]))],
        "disposition": "KILL" if (cat == "HIGH_REDUNDANCY" and mean_abs > 0.6) or max_abs >= 0.95 else "ADVANCE",
        "n_comparisons": len(cors),
    }


def generate_dispositions(matrix: Dict, signal_names: List[str],
                           signal_metrics: Optional[Dict] = None) -> List[Dict]:
    """Generate per-signal disposition table."""
    dispositions = []
    for signal in sorted(signal_names):
        info = classify_signal(matrix, signal, signal_names)
        metrics = (signal_metrics or {}).get(signal, {})
        info["signal"] = signal
        info["n_trades"] = metrics.get("n_trades", "?")
        info["n_stations"] = metrics.get("n_stations", "?")
        info["accuracy"] = metrics.get("accuracy", "?")
        dispositions.append(info)
    return dispositions


def write_outputs(matrix: Dict, dispositions: List[Dict], signal_metrics: Optional[Dict] = None):
    """Write all output files."""

    # JSON matrix
    json_path = os.path.join(DATA_DIR, "signal_correlation_matrix.json")
    with open(json_path, 'w') as f:
        json.dump(matrix, f, indent=2)
    logger.info(f"Matrix JSON written to {json_path}")

    # Markdown report
    md_path = os.path.join(DATA_DIR, "signal_correlation_summary.md")
    lines = [
        "# Signal Correlation Matrix — Spearman ρ\n",
        f"**Generated:** {datetime.now().isoformat()}\n",
        "Pairwise Spearman rank correlation (ρ) between all active signals.\n",
        "Values near +1.0 = highly correlated (vote same direction).\n",
        "Values near -1.0 = inversely correlated (vote opposite).\n",
        "Values near 0.0 = orthogonal (independent signals).\n",
        f"Active signal count: {len(dispositions)}\n",
        "## Redundancy Summary\n",
        f"| {'Signal':<35} | {'Accuracy':<9} | {'nTrades':<8} | {'Mean |ρ|':<10} | {'Max |ρ|':<10} | {'Classification':<20} | {'Disposition':<12} |",
        f"| {'-'*35} | {'-'*9} | {'-'*8} | {'-'*10} | {'-'*10} | {'-'*20} | {'-'*12} |",
    ]

    for d in sorted(dispositions, key=lambda x: -x['max_rho']):
        redundant = ", ".join([f"{p['signal']}({p['rho']:.2f})" for p in d['redundant_pairs'][:3]])
        if redundant:
            redundant = f" ({redundant})"
        acc_str = f"{d['accuracy']:.3f}" if isinstance(d['accuracy'], float) else str(d['accuracy'])
        lines.append(
            f"| {d['signal']:<35} | {acc_str:<9} | {str(d['n_trades']):<8} | {d['mean_rho']:<10.4f} | "
            f"{d['max_rho']:<10.4f} | {d['classification']:<20} | {d['disposition']:<12}{redundant} |"
        )

    lines += [
        "",
        "## Interpretation Guide\n",
        "| Category | |ρ| Range | Signal Quality | Action |",
        "|----------|:---------:|:--------------:|:------:|",
        "| **ORTHOGONAL** | < 0.3 | Excellent | Keep both — provide independent information |",
        "| **LOW** | 0.3 – 0.5 | Good | Some overlap but still useful together |",
        "| **MODERATE** | 0.5 – 0.7 | Fair | Redundant — consider down-weighting one |",
        "| **HIGH_REDUNDANCY** | ≥ 0.7 | Poor | Highly redundant — one should be killed or fused |",
        "",
        "## Disposition Guide\n",
        "- **ADVANCE**: Signal is orthogonal or low-correlation — keep in ensemble\n",
        "- **KILL**: Signal is highly redundant — remove or fuse with its pair\n",
        "- **PARK**: Needs further investigation before disposition\n",
    ]

    with open(md_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    logger.info(f"Summary MD written to {md_path}")

    # CSV disposition
    csv_path = os.path.join(DATA_DIR, "signal_disposition.csv")
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'signal', 'accuracy', 'n_trades', 'n_stations', 'mean_rho', 'max_rho',
            'classification', 'disposition', 'redundant_pairs'
        ])
        writer.writeheader()
        for d in sorted(dispositions, key=lambda x: -x['max_rho']):
            writer.writerow({
                'signal': d['signal'],
                'accuracy': d.get('accuracy', ''),
                'n_trades': d['n_trades'],
                'n_stations': d['n_stations'],
                'mean_rho': d['mean_rho'],
                'max_rho': d['max_rho'],
                'classification': d['classification'],
                'disposition': d['disposition'],
                'redundant_pairs': '; '.join([f"{p['signal']}({p['rho']})" for p in d['redundant_pairs']]),
            })
    logger.info(f"Disposition CSV written to {csv_path}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"Signal Correlation Summary")
    print(f"{'='*60}")
    for d in sorted(dispositions, key=lambda x: -x['max_rho']):
        redundant = ", ".join([p['signal'] for p in d['redundant_pairs'][:2]])
        extra = f" redundant: {redundant}" if redundant else ""
        print(f"  {d['signal']:35s}  mean|ρ|={d['mean_rho']:.3f}  "
              f"max|ρ|={d['max_rho']:.3f}  {d['classification']:20s}  → {d['disposition']}{extra}")

    killed = [d for d in dispositions if d['disposition'] == 'KILL']
    advance = [d for d in dispositions if d['disposition'] == 'ADVANCE']
    print(f"\n  → {len(killed)} KILL, {len(advance)} ADVANCE of {len(dispositions)} total")
    if killed:
        print(f"  → Killed signals: {[k['signal'] for k in killed]}")
    print()


def main():
    logger.info("Starting correlation matrix computation...")
    sweep_data = evaluate_all_signals()

    signal_names = sweep_data["signal_names"]
    signal_vectors = sweep_data["signal_vectors"]
    signal_metrics = sweep_data.get("signal_metrics")

    logger.info(f"Computing {len(signal_names)}×{len(signal_names)} Spearman matrix...")
    matrix = compute_matrix(signal_vectors)

    dispositions = generate_dispositions(matrix, signal_names, signal_metrics)
    write_outputs(matrix, dispositions, signal_metrics)


if __name__ == "__main__":
    main()