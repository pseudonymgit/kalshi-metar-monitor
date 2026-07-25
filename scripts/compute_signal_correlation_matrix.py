#!/usr/bin/env python3
"""
Compute pairwise Spearman correlation matrix between all active signals.

Outputs:
  1. data/signal_correlation_matrix.json — full matrix
  2. docs/weather-engine/SIGNAL_CORRELATION_MATRIX.md — human-readable report

Usage:
    python3 scripts/compute_signal_correlation_matrix.py [--db-path PATH] [--output-dir DIR]
"""

import sqlite3
import json
import math
import os
import sys
import argparse
import logging
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
from scipy.stats import spearmanr
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import SignalRegistry
from core.unified_backtest import BACKTEST_SIGNALS, DB_PATH

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def compute_signal_correlation_matrix(
    db_path: str,
    stations: List[str],
    signal_names: List[str]
) -> Dict[str, Dict[str, float]]:
    """
    Compute pairwise Spearman rank correlation between all active signals.

    For each station, runs walk-forward evaluation and collects direction+confidence
    pairs for each signal on each day. Then computes Spearman ρ between every
    signal pair across the common days where both fired.

    Returns: {sig_i: {sig_j: spearman_rho}}
    """
    registry = SignalRegistry(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Collect per-signal per-day direction and confidence
    # structure: {signal_name: {station: [(date, direction_numeric, confidence), ...]}}
    signal_data: Dict[str, Dict[str, List[Tuple[str, float, float]]]] = {
        sig: {} for sig in signal_names
    }

    logger.info(f"Computing correlation matrix for {len(signal_names)} signals, {len(stations)} stations...")

    for station in stations:
        # Load daily data
        cur.execute("""
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                   AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                   AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                   AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
            GROUP BY date_utc ORDER BY date_utc ASC
        """, (station,))
        days = []
        for r in cur.fetchall():
            if any(v is None for v in r[1:]):
                continue
            days.append({
                'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
            })

        # Evaluate each signal
        for sig_name in signal_names:
            sig = registry.get_signal(sig_name)
            if sig is None:
                continue
            sig_entries = []
            for idx in range(sig.min_lookback, len(days)):
                direction, confidence = sig.evaluate(idx, days)
                if direction is not None and confidence > 0:
                    # Encode direction as +1 (up), -1 (down)
                    dir_num = 1.0 if direction == 'up' else -1.0
                    sig_entries.append((days[idx]['date'], dir_num, confidence))
            if sig_entries:
                signal_data[sig_name][station] = sig_entries
                logger.info(f"  {sig_name} @ {station}: {len(sig_entries)} firings")

    conn.close()

    # Build pairwise correlation matrix
    # First, aggregate all (direction_numeric, confidence) across all stations and dates
    signal_vectors: Dict[str, List[Tuple[str, str, float, float]]] = {
        sig: [] for sig in signal_names
    }

    for sig_name in signal_names:
        for station, entries in signal_data.get(sig_name, {}).items():
            for date, dir_num, conf in entries:
                signal_vectors[sig_name].append((station, date, dir_num, conf))

    # Compute pairwise Spearman ρ
    matrix: Dict[str, Dict[str, float]] = {}
    for sig_i in signal_names:
        matrix[sig_i] = {}
        for sig_j in signal_names:
            if sig_i == sig_j:
                matrix[sig_i][sig_j] = 1.0
                continue

            # Common keys: match on (station, date)
            data_i = {(s, d): (dn, c) for s, d, dn, c in signal_vectors.get(sig_i, [])}
            data_j = {(s, d): (dn, c) for s, d, dn, c in signal_vectors.get(sig_j, [])}
            common_keys = sorted(set(data_i.keys()) & set(data_j.keys()))

            if len(common_keys) < 30:
                logger.info(f"  {sig_i} vs {sig_j}: only {len(common_keys)} common days, skipping")
                matrix[sig_i][sig_j] = None
                continue

            # Compute Spearman ρ for directional agreement
            # Use direction_numeric as the primary signal value
            vals_i = [data_i[k][0] for k in common_keys]
            vals_j = [data_j[k][0] for k in common_keys]

            # Also compute confidence-weighted version
            dir_agree = sum(1.0 for vi, vj in zip(vals_i, vals_j) if vi == vj) / len(common_keys)

            rho, p_value = spearmanr(vals_i, vals_j)
            # Spearmanr may return nan if all values are identical
            if math.isnan(rho):
                rho = float(dir_agree)  # fallback: agreement rate
                p_value = 1.0

            matrix[sig_i][sig_j] = round(rho, 4)
            logger.info(f"  {sig_i} vs {sig_j}: ρ={rho:.4f}, p={p_value:.4f}, n={len(common_keys)}, agree={dir_agree:.3f}")

    return matrix


def classify_correlation(rho: float) -> str:
    """Classify correlation strength."""
    if rho is None:
        return "INSUFFICIENT_DATA"
    abs_r = abs(rho)
    if abs_r >= 0.7:
        return "HIGH_REDUNDANCY"
    elif abs_r >= 0.5:
        return "MODERATE"
    elif abs_r >= 0.3:
        return "LOW"
    else:
        return "ORTHOGONAL"


def generate_md_report(
    matrix: Dict[str, Dict[str, float]],
    signal_names: List[str],
    output_path: str
):
    """Generate markdown report with correlation matrix."""
    lines = []
    lines.append("# Signal Correlation Matrix — Spearman ρ")
    lines.append("")
    lines.append(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("Pairwise Spearman rank correlation (ρ) between all active signals. ")
    lines.append("Values near +1.0 = highly correlated (vote same direction). ")
    lines.append("Values near -1.0 = inversely correlated (vote opposite). ")
    lines.append("Values near 0.0 = orthogonal (independent signals).")
    lines.append("")
    lines.append(f"Active signal count: {len(signal_names)}")
    lines.append("")

    # Summary table of redundancy
    lines.append("## Redundancy Summary")
    lines.append("")
    lines.append("| Signal | Mean |ρ| | Max |ρ| | Redundant Pairs (|ρ|≥0.7) | Classification |")
    lines.append("|--------|:-----:|:-----:|:------------------------:|:--------------:|")
    for sig_i in signal_names:
        cors = []
        redundant_pairs = []
        for sig_j in signal_names:
            if sig_i == sig_j:
                continue
            rho = matrix.get(sig_i, {}).get(sig_j)
            if rho is not None:
                cors.append(abs(rho))
                if abs(rho) >= 0.7:
                    redundant_pairs.append(f"{sig_j}({rho:.2f})")

        mean_abs = sum(cors) / len(cors) if cors else 0
        max_abs = max(cors) if cors else 0
        red_str = ", ".join(redundant_pairs) if redundant_pairs else "None"
        cls = classify_correlation(max_abs)
        lines.append(f"| {sig_i} | {mean_abs:.3f} | {max_abs:.3f} | {red_str} | {cls} |")
    lines.append("")

    # Full matrix
    lines.append("## Full Correlation Matrix")
    lines.append("")
    header = "| Signal | " + " | ".join(signal_names) + " |"
    sep = "|" + "|".join([":---"] * (len(signal_names) + 1)) + "|"
    lines.append(header)
    lines.append(sep)
    for sig_i in signal_names:
        row = [sig_i]
        for sig_j in signal_names:
            rho = matrix.get(sig_i, {}).get(sig_j)
            if rho is None:
                row.append("N/A")
            else:
                row.append(f"{rho:.3f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Interpretation
    lines.append("## Interpretation Guide")
    lines.append("")
    lines.append("| Category | |ρ| Range | Signal Quality | Action |")
    lines.append("|----------|:---------:|:--------------:|:------:|")
    lines.append("| **ORTHOGONAL** | < 0.3 | Excellent | Keep both — provide independent information |")
    lines.append("| **LOW** | 0.3 – 0.5 | Good | Some overlap but still useful together |")
    lines.append("| **MODERATE** | 0.5 – 0.7 | Fair | Redundant — consider down-weighting one |")
    lines.append("| **HIGH_REDUNDANCY** | ≥ 0.7 | Poor | Highly redundant — one should be killed or fused |")
    lines.append("")

    # Note on fee-adjusted accuracy
    lines.append("---")
    lines.append(f"*Report auto-generated by `scripts/compute_signal_correlation_matrix.py`*")

    content = "\n".join(lines)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(content)
    logger.info(f"Report written to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Compute signal correlation matrix")
    parser.add_argument("--db-path", default=DB_PATH, help="Path to METAR DB")
    parser.add_argument(
        "--output-dir", default=".",
        help="Output directory for JSON and MD (default: current dir)"
    )
    parser.add_argument("--stations", nargs="+", default=None, help="Station codes")
    args = parser.parse_args()

    signal_names = [s for s in BACKTEST_SIGNALS if s != 'goldilocks']  # exclude alias
    stations = args.stations or [
        'KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
        'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
        'KPHX','KSAT','KSEA','KSFO'
    ]

    logger.info(f"Active signals: {len(signal_names)} — {signal_names}")

    matrix = compute_signal_correlation_matrix(args.db_path, stations, signal_names)

    # Output JSON
    json_path = os.path.join(args.output_dir, "signal_correlation_matrix.json")
    os.makedirs(os.path.dirname(json_path) if os.path.dirname(json_path) else '.', exist_ok=True)
    with open(json_path, 'w') as f:
        json.dump(matrix, f, indent=2)
    logger.info(f"JSON matrix written to {json_path}")

    # Output MD
    md_path = os.path.join(args.output_dir, "docs", "weather-engine", "SIGNAL_CORRELATION_MATRIX.md")
    generate_md_report(matrix, signal_names, md_path)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Signal Correlation Matrix Summary")
    print(f"{'='*60}")
    for sig_i in signal_names:
        cors = []
        for sig_j in signal_names:
            if sig_i == sig_j:
                continue
            rho = matrix.get(sig_i, {}).get(sig_j)
            if rho is not None:
                cors.append(abs(rho))
        mean_abs = sum(cors) / len(cors) if cors else 0
        max_abs = max(cors) if cors else 0
        print(f"  {sig_i:35s}  mean|ρ|={mean_abs:.3f}  max|ρ|={max_abs:.3f}  {classify_correlation(max_abs)}")

    print(f"\nFull matrix saved to: {json_path}")
    print(f"Report saved to: {md_path}")


if __name__ == "__main__":
    main()