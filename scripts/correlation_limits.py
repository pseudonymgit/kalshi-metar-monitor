"""
Epsilon — Multi-Station Correlation Limits (E.3)

If two correlated stations both show edge at the same hour on the same day,
reduce the combined position by the correlation factor.

Correlation pairs (from prior research):
  - KNYC <-> KPHL:  rho = 0.72 (NE coastal)
  - KNYC <-> KDCA:  rho = 0.65 (Mid-Atlantic corridor)
  - KPHL <-> KDCA:  rho = 0.68 (Philadelphia-DC corridor)
  - KATL <-> KHOU:  rho = 0.55 (Southeastern US)
  - KDFW <-> KHOU:  rho = 0.58 (Texas triangle)
  - KLAX <-> KSFO:  rho = 0.48 (California coast)
  - KSEA <-> KSFO:  rho = 0.42 (West coast)
  - KMDW <-> KMSP:  rho = 0.50 (Upper Midwest)
  - KDEN <-> KOKC:  rho = 0.40 (Intermountain)

Reduction formula:
  For each concurrent correlated pair, the smaller position (of the two) is
  scaled down by (1 - rho * penalty_factor). The larger position is left
  intact (it is the primary exposure); only the redundant hedge is reduced.

Design:
  - Read-only over proposed positions; returns adjusted positions.
  - Deterministic math only. No AI/ML in loop.

Usage:
    python3 scripts/correlation_limits.py            # Run full check
    python3 scripts/correlation_limits.py --list-pairs
    python3 scripts/correlation_limits.py --demo
"""

import argparse
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('correlation_limits')

CORRELATION_PENALTY_FACTOR = 0.5

KNOWN_CORRELATION_PAIRS: Dict[Tuple[str, str], float] = {
    ('KNYC', 'KPHL'): 0.72,
    ('KNYC', 'KDCA'): 0.65,
    ('KPHL', 'KDCA'): 0.68,
    ('KATL', 'KHOU'): 0.55,
    ('KDFW', 'KHOU'): 0.58,
    ('KLAX', 'KSFO'): 0.48,
    ('KSEA', 'KSFO'): 0.42,
    ('KMDW', 'KMSP'): 0.50,
    ('KDEN', 'KOKC'): 0.40,
}

# Bidirectional lookup table.
STATION_CORRELATIONS: Dict[str, Dict[str, float]] = defaultdict(dict)
for (_a, _b), _rho in KNOWN_CORRELATION_PAIRS.items():
    STATION_CORRELATIONS[_a][_b] = _rho
    STATION_CORRELATIONS[_b][_a] = _rho


def get_correlation(station_a: str, station_b: str) -> float:
    """Temperature correlation coefficient between two stations (0.0 if unknown)."""
    a = (station_a or '').upper().strip()
    b = (station_b or '').upper().strip()
    if a == b:
        return 1.0
    return STATION_CORRELATIONS.get(a, {}).get(b, 0.0)


def compute_correlation_penalty(
    positions: List[Dict[str, Any]],
    penalty_factor: float = CORRELATION_PENALTY_FACTOR,
) -> Dict[str, Any]:
    """
    Compute correlation-based position reduction for a list of positions.

    Args:
        positions: list of dicts with keys 'station', 'hour_before_settlement',
                   'contracts', optionally 'position_dollars'.
        penalty_factor: fraction of correlation to apply as penalty (default 0.5).

    Returns dict with 'adjusted_positions', 'total_reduction', 'pairs_found',
    'warnings'.
    """
    if not positions:
        return {
            'adjusted_positions': [],
            'total_reduction': 0.0,
            'pairs_found': [],
            'warnings': ['no positions to adjust'],
        }

    by_station: Dict[str, Dict[str, Any]] = {}
    for p in positions:
        st = p['station'].upper().strip()
        by_station[st] = p

    stations = list(by_station.keys())
    pairs_found: List[Dict[str, Any]] = []

    total_original = sum(p.get('contracts', 0) for p in positions)

    # Track reductions per station: each station's final adjusted contracts start
    # equal to original, and we apply the largest penalty when a station appears
    # in multiple correlated pairs.
    adjusted: Dict[str, int] = {st: by_station[st].get('contracts', 0) for st in stations}
    reductions: Dict[str, int] = {st: 0 for st in stations}

    for i, st_a in enumerate(stations):
        for st_b in stations[i + 1:]:
            rho = get_correlation(st_a, st_b)
            if rho <= 0:
                continue

            pos_a = by_station[st_a]
            pos_b = by_station[st_b]
            hb_a = pos_a.get('hour_before_settlement', 0)
            hb_b = pos_b.get('hour_before_settlement', 0)
            if abs(hb_a - hb_b) > 2:
                continue

            contracts_a = pos_a.get('contracts', 0)
            contracts_b = pos_b.get('contracts', 0)
            if contracts_a <= 0 or contracts_b <= 0:
                continue

            penalty = rho * penalty_factor

            # Reduce the smaller position (redundant hedge) by the penalty.
            if contracts_a <= contracts_b:
                affected_st = st_a
                reduction = int(round(contracts_a * penalty))
            else:
                affected_st = st_b
                reduction = int(round(contracts_b * penalty))

            reduction = max(0, min(reduction, by_station[affected_st].get('contracts', 0)))

            pairs_found.append({
                'station_a': st_a,
                'station_b': st_b,
                'correlation': rho,
                'penalty_factor': penalty_factor,
                'penalty_applied': round(penalty, 4),
                'reduction_contracts': reduction,
                'affected_station': affected_st,
                'hour_a': hb_a,
                'hour_b': hb_b,
            })

            # Keep the largest reduction if a station appears in multiple pairs.
            if reduction > reductions[affected_st]:
                reductions[affected_st] = reduction
                adjusted[affected_st] = contracts_a if affected_st == st_a else contracts_b
                adjusted[affected_st] -= reduction

    total_reduction_contracts = sum(reductions.values())

    adjusted_positions: List[Dict[str, Any]] = []
    for p in positions:
        st = p['station'].upper().strip()
        adj_pos = dict(p)
        adj_pos['adjusted_contracts'] = adjusted.get(st, p.get('contracts', 0))
        adj_pos['reduction'] = p.get('contracts', 0) - adj_pos['adjusted_contracts']
        adjusted_positions.append(adj_pos)

    total_adjusted = sum(p['adjusted_contracts'] for p in adjusted_positions)
    total_reduction = 1.0 - (total_adjusted / max(total_original, 1))

    warnings: List[str] = []
    if total_reduction_contracts > 0:
        warnings.append(
            f"Reduced {total_reduction_contracts} contracts across "
            f"{len(pairs_found)} correlated pair(s) ({total_reduction:.1%} reduction)"
        )

    return {
        'adjusted_positions': adjusted_positions,
        'total_reduction': round(total_reduction, 4),
        'total_reduction_contracts': total_reduction_contracts,
        'pairs_found': pairs_found,
        'warnings': warnings,
    }


def apply_correlation_limits(
    positions: List[Dict[str, Any]],
    penalty_factor: float = CORRELATION_PENALTY_FACTOR,
) -> List[Dict[str, Any]]:
    """Convenience wrapper: return adjusted positions with 'adjusted_contracts' set."""
    return compute_correlation_penalty(positions, penalty_factor)['adjusted_positions']


def list_pairs() -> str:
    """Format known correlation pairs as a readable table."""
    lines = [
        'Known correlation pairs:',
        '--------------------------------------------------',
        f"{'Station A':<10} {'Station B':<10} {'rho':<8}",
        '--------------------------------------------------',
    ]
    for (a, b) in sorted(KNOWN_CORRELATION_PAIRS.keys()):
        rho = KNOWN_CORRELATION_PAIRS[(a, b)]
        lines.append(f"{a:<10} {b:<10} {rho:<8.2f}")
    lines.append('--------------------------------------------------')
    return '\n'.join(lines)


def display_demo() -> None:
    """Run a demonstration with sample positions."""
    demo_positions = [
        {'station': 'KNYC', 'hour_before_settlement': 6, 'contracts': 50, 'position_dollars': 25.0},
        {'station': 'KPHL', 'hour_before_settlement': 6, 'contracts': 30, 'position_dollars': 15.0},
        {'station': 'KDCA', 'hour_before_settlement': 6, 'contracts': 20, 'position_dollars': 10.0},
        {'station': 'KDEN', 'hour_before_settlement': 5, 'contracts': 60, 'position_dollars': 30.0},
        {'station': 'KOKC', 'hour_before_settlement': 5, 'contracts': 25, 'position_dollars': 12.5},
        {'station': 'KLAX', 'hour_before_settlement': 6, 'contracts': 15, 'position_dollars': 7.5},
    ]

    print('\nBefore correlation limits:')
    for p in demo_positions:
        print(f"  {p['station']} h-{p['hour_before_settlement']}: {p['contracts']} contracts")

    result = compute_correlation_penalty(demo_positions)

    print('\nAfter correlation limits:')
    for p in result['adjusted_positions']:
        print(f"  {p['station']} h-{p['hour_before_settlement']}: "
              f"{p['contracts']} -> {p['adjusted_contracts']} (-{p['reduction']})")

    if result['pairs_found']:
        print(f"\nCorrelated pairs found ({len(result['pairs_found'])}):")
        for pr in result['pairs_found']:
            print(f"  {pr['station_a']}-{pr['station_b']}: rho={pr['correlation']:.2f}, "
                  f"reduced {pr['reduction_contracts']} contracts on {pr['affected_station']}")

    for w in result['warnings']:
        logger.warning(w)


def main() -> int:
    ap = argparse.ArgumentParser(description='Epsilon — Multi-Station Correlation Limits (E.3)')
    ap.add_argument('--list-pairs', action='store_true', help='List known correlation pairs and exit')
    ap.add_argument('--demo', action='store_true', help='Run demonstration with sample positions')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    if args.list_pairs:
        print(list_pairs())
        return 0
    if args.demo:
        display_demo()
        return 0

    logger.info('No active entry positions found (use --demo for demonstration)')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
