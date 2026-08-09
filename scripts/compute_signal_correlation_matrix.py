#!/usr/bin/env python3
"""
Compute pairwise Spearman correlation matrix between all active signals.

Can run in two modes:
  1. Post-sweep (--input sweep_results.json) — analyze sweep output
  2. Standalone (--db-path) — evaluate signals directly using the sweep registry

Outputs:
  data/signal_correlation_matrix.json      — full matrix
  data/signal_correlation_summary.md       — redundancy classification + disposition
  data/signal_disposition.csv              — per-signal disposition (ADVANCE/PARK/KILL)

Usage:
    python3 scripts/compute_signal_correlation_matrix.py --input data/sweep_results_v1.json
    python3 scripts/compute_signal_correlation_matrix.py --db-path data/kalshi_settlements.db
"""

import argparse
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')

# ─── Correlation Classification ────────────────────────────────

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


# ─── Mode 1: Post-sweep analysis ───────────────────────────────

def analyze_sweep_results(sweep_path: str) -> Dict:
    """Analyze sweep results JSON — extract per-signal trade directions."""
    with open(sweep_path) as f:
        data = json.load(f)

    results = data.get("signal_results", data)
    signal_names = list(results.keys())

    # Extract per-trade direction vectors for correlation analysis
    signal_vectors = {}
    for sname, sres in results.items():
        trades = sres.get("best_config_trades") or sres.get("trades") or []
        directions = [t.get("pred_direction") for t in trades if t.get("pred_direction")]
        if directions:
            # Map 'up'/'down' to 1/0 for Spearman
            signal_vectors[sname] = [1 if d == "up" else 0 for d in directions]

    return {
        "signal_names": signal_names,
        "signal_vectors": signal_vectors,
        "results": results,
    }


def compute_matrix_from_vectors(signal_vectors: Dict[str, List]) -> Dict[str, Dict[str, float]]:
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

            # Trim to same length
            n = min(len(vi), len(vj))
            if n < 5:
                matrix[si][sj] = None
                continue

            try:
                rho, _ = spearmanr(vi[:n], vj[:n])
                matrix[si][sj] = round(rho, 4) if not np.isnan(rho) else 0.0
            except Exception:
                matrix[si][sj] = None

    return matrix


# ─── Mode 2: Standalone (uses sweep registry) ──────────────────

def evaluate_signals_standalone(db_path: str) -> Dict:
    """Evaluate all signals from sweep registry against settlements."""
    from scripts.sweep_engine import build_signal_registry, METAR_DB
    from scripts.sweep.config import STATIONS

    registry = build_signal_registry()
    active = {k: v for k, v in registry.items() if v is not None}

    # Load settlement data
    from core.db_utils import with_db

    signal_vectors = {}
    signal_metrics = {}

    with with_db(db_path, row_factory=True, commit=False) as conn:
        for sname, sobj in active.items():
            # Skip function-based signals
            if callable(sobj) and not hasattr(sobj, 'evaluate'):
                continue

            directions = []
            trades = []

            for station_key in STATIONS[:8]:  # Use subset for speed
                try:
                    # Get settlements for this station
                    rows = conn.execute("""
                        SELECT date_utc, temp_f, series_type, settlement_temp
                        FROM kalshi_settlements
                        WHERE station = ? AND date_utc IS NOT NULL
                        ORDER BY date_utc ASC
                    """, (station_key,)).fetchall()

                    dates = []
                    for r in rows:
                        dates.append({
                            'date': r['date_utc'],
                            'high': r['temp_f'] if r['series_type'] == 'HIGH' else None,
                            'low': r['temp_f'] if r['series_type'] == 'LOW' else None,
                        })

                    for idx in range(len(dates)):
                        try:
                            pred = sobj.evaluate(idx, dates)
                            if pred and pred[0] is not None:
                                direction = pred[0]
                                directions.append(1 if direction == 'up' else 0)
                                trades.append({
                                    'station': station_key,
                                    'date': dates[idx]['date'],
                                    'direction': direction,
                                    'confidence': pred[1],
                                })
                        except (Exception, TypeError):
                            continue
                except Exception as e:
                    logger.debug(f"Error evaluating {sname} on {station_key}: {e}")
                    continue

            if directions:
                signal_vectors[sname] = directions
                signal_metrics[sname] = {
                    'n_trades': len(trades),
                    'n_stations': len(set(t.get('station') for t in trades)),
                }

    return {
        "signal_names": list(signal_vectors.keys()),
        "signal_vectors": signal_vectors,
        "signal_metrics": signal_metrics,
    }


# ─── Reporting ──────────────────────────────────────────────────

def classify_signal(matrix: Dict, signal: str, signal_names: List[str]) -> Dict:
    """Classify a signal based on its correlation with others."""
    cors = []
    redundant_pairs = []
    for other in signal_names:
        if signal == other:
            continue
        rho = matrix.get(signal, {}).get(other)
        if rho is not None:
            cors.append(abs(rho))
            if abs(rho) >= 0.7:
                redundant_pairs.append((other, rho))

    mean_abs = round(sum(cors) / len(cors), 4) if cors else 0.0
    max_abs = round(max(cors), 4) if cors else 0.0
    cat, desc = classify_correlation(max_abs)

    return {
        "mean_rho": mean_abs,
        "max_rho": max_abs,
        "classification": cat,
        "description": desc,
        "redundant_pairs": [{"signal": p[0], "rho": round(p[1], 4)} for p in sorted(redundant_pairs, key=lambda x: -abs(x[1]))],
        "disposition": "KILL" if cat == "HIGH_REDUNDANCY" and mean_abs > 0.6 else "ADVANCE",
        "n_comparisons": len(cors),
    }


def generate_disposition_table(
    matrix: Dict, signal_names: List[str], signal_metrics: Optional[Dict] = None
) -> List[Dict]:
    """Generate per-signal disposition table."""
    dispositions = []
    for signal in sorted(signal_names):
        info = classify_signal(matrix, signal, signal_names)
        metrics = (signal_metrics or {}).get(signal, {})
        info["signal"] = signal
        info["n_trades"] = metrics.get("n_trades", "?")
        info["n_stations"] = metrics.get("n_stations", "?")
        dispositions.append(info)
    return dispositions


def write_md_report(matrix: Dict, dispositions: List[Dict], output_path: str):
    """Write Markdown report with redundancy summary and disposition."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    lines = [
        "# Signal Correlation Matrix — Spearman ρ\n",
        f"**Generated:** {datetime.now().isoformat()}\n",
        "Pairwise Spearman rank correlation (ρ) between all active signals.\n",
        "Values near +1.0 = highly correlated (vote same direction).\n",
        "Values near -1.0 = inversely correlated (vote opposite).\n",
        "Values near 0.0 = orthogonal (independent signals).\n",
        f"Active signal count: {len(dispositions)}\n",
        "## Redundancy Summary\n",
        f"| {'Signal':<35} | {'Mean |ρ|':<10} | {'Max |ρ|':<10} | {'Classification':<20} | {'Disposition':<12} |",
        f"| {'-'*35} | {'-'*10} | {'-'*10} | {'-'*20} | {'-'*12} |",
    ]

    for d in sorted(dispositions, key=lambda x: -x['max_rho']):
        redundant = ", ".join([f"{p['signal']}({p['rho']:.2f})" for p in d['redundant_pairs'][:3]])
        if d['redundant_pairs']:
            redundant = f" ({redundant})"
        lines.append(
            f"| {d['signal']:<35} | {d['mean_rho']:<10.4f} | {d['max_rho']:<10.4f} | "
            f"{d['classification']:<20} | {d['disposition']:<12} |"
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
        "",
    ]

    with open(output_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    logger.info(f"Report written to {output_path}")


def write_disposition_csv(dispositions: List[Dict], output_path: str):
    """Write disposition table as CSV."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'signal', 'mean_rho', 'max_rho', 'classification',
            'disposition', 'n_trades', 'n_stations', 'redundant_pairs'
        ])
        writer.writeheader()
        for d in sorted(dispositions, key=lambda x: -x['max_rho']):
            writer.writerow({
                'signal': d['signal'],
                'mean_rho': d['mean_rho'],
                'max_rho': d['max_rho'],
                'classification': d['classification'],
                'disposition': d['disposition'],
                'n_trades': d['n_trades'],
                'n_stations': d['n_stations'],
                'redundant_pairs': '; '.join([f"{p['signal']}({p['rho']})" for p in d['redundant_pairs']]),
            })
    logger.info(f"Disposition CSV written to {output_path}")


# ─── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Signal Correlation Matrix — Redundancy Analysis")
    parser.add_argument('--input', '-i', type=str, default=None,
                        help='Sweep results JSON (post-sweep mode)')
    parser.add_argument('--db-path', type=str,
                        default=os.path.join(DATA_DIR, 'kalshi_settlements.db'),
                        help='Settlements DB path (standalone mode)')
    parser.add_argument('--output-dir', type=str, default=DATA_DIR,
                        help='Output directory')
    args = parser.parse_args()

    if args.input and os.path.exists(args.input):
        logger.info("Post-sweep mode: analyzing %s", args.input)
        sweep_data = analyze_sweep_results(args.input)
    else:
        logger.info("Standalone mode: evaluating signals from %s", args.db_path)
        sweep_data = evaluate_signals_standalone(args.db_path)

    signal_names = sweep_data["signal_names"]
    signal_vectors = sweep_data["signal_vectors"]
    signal_metrics = sweep_data.get("signal_metrics")

    logger.info(f"Analyzing {len(signal_names)} signals")

    matrix = compute_matrix_from_vectors(signal_vectors)

    # Generate disposition
    dispositions = generate_disposition_table(matrix, signal_names, signal_metrics)

    # Write outputs
    json_path = os.path.join(args.output_dir, "signal_correlation_matrix.json")
    with open(json_path, 'w') as f:
        json.dump(matrix, f, indent=2)
    logger.info(f"Matrix JSON written to {json_path}")

    md_path = os.path.join(args.output_dir, "signal_correlation_summary.md")
    write_md_report(matrix, dispositions, md_path)

    csv_path = os.path.join(args.output_dir, "signal_disposition.csv")
    write_disposition_csv(dispositions, csv_path)

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


if __name__ == "__main__":
    main()