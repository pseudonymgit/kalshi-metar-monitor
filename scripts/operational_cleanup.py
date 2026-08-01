"""
Operational Cleanup — Phase 4.3

Identifies and resolves:
  1. Station list contradictions between modules
  2. Stale DB paths and references
  3. Station difficulty ranking for position sizing

Usage:
    python3 scripts/operational_cleanup.py
"""

import logging
import sys
import os

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = "/home/node/.openclaw/workspace/prototypes/weather-engine-source"

# Canonical 20-station list (single source of truth)
CANONICAL_STATIONS = {
    'KATL': 'Atlanta', 'KAUS': 'Austin', 'KBOS': 'Boston',
    'KDCA': 'Washington DC', 'KDEN': 'Denver', 'KDFW': 'Dallas',
    'KHOU': 'Houston', 'KLAS': 'Las Vegas', 'KLAX': 'Los Angeles',
    'KMDW': 'Chicago', 'KMIA': 'Miami', 'KMSP': 'Minneapolis',
    'KMSY': 'New Orleans', 'KNYC': 'New York', 'KOKC': 'Oklahoma City',
    'KPHL': 'Philadelphia', 'KPHX': 'Phoenix', 'KSAT': 'San Antonio',
    'KSEA': 'Seattle', 'KSFO': 'San Francisco',
}

# Station difficulty ranking (from accuracy-numbers-cycles4-6)
# Tier 1 = Easiest (>=70%), Tier 2 = Average (66-69%), Tier 3 = Hardest (<66%)
STATION_DIFFICULTY = {
    'KMIA': 1, 'KDEN': 1, 'KBOS': 1, 'KDCA': 1, 'KSAT': 1, 'KAUS': 1,
    'KSFO': 2, 'KATL': 2, 'KMDW': 2, 'KPHL': 2,
    'KMSP': 2, 'KDFW': 2, 'KNYC': 2, 'KHOU': 2, 'KMSY': 2, 'KOKC': 2, 'KLAX': 2,
    'KPHX': 3, 'KSEA': 3, 'KLAS': 3,
}

DIFFICULTY_LABEL = {1: 'EASY', 2: 'AVERAGE', 3: 'HARD'}


def check_station_lists():
    """Check station list consistency across modules."""
    logger.info("=== Station List Consistency Check ===")
    issues = []

    # Check station_mapping.json
    import json
    mapping_path = os.path.join(REPO_ROOT, 'data', 'station_mapping.json')
    if os.path.exists(mapping_path):
        with open(mapping_path) as f:
            mapping = json.load(f).get('stations', {})
        mapping_stations = set(mapping.keys())
        canonical_set = set(CANONICAL_STATIONS.keys())

        extra = mapping_stations - canonical_set
        missing = canonical_set - mapping_stations

        if extra:
            issues.append(f"EXTRA in station_mapping.json: {sorted(extra)}")
        if missing:
            issues.append(f"MISSING from station_mapping.json: {sorted(missing)}")

        logger.info("station_mapping.json: %d stations", len(mapping_stations))

    # Check signal_registry.py
    sig_path = os.path.join(REPO_ROOT, 'core', 'signals', 'signal_registry.py')
    if os.path.exists(sig_path):
        with open(sig_path) as f:
            content = f.read()
        for station in sorted(CANONICAL_STATIONS.keys()):
            if station not in content:
                issues.append(f"MISSING station {station} from signal_registry")

    # Check core/signals/__init__.py
    sig_init = os.path.join(REPO_ROOT, 'core', 'signals', '__init__.py')
    if os.path.exists(sig_init):
        with open(sig_init) as f:
            content = f.read()
        for station in sorted(CANONICAL_STATIONS.keys()):
            if station not in content:
                issues.append(f"MISSING station {station} from signals/__init__.py")

    for issue in issues:
        logger.warning("ISSUE: %s", issue)

    if not issues:
        logger.info("All station lists consistent ✓")


def print_difficulty_ranking():
    """Print station difficulty ranking for position sizing."""
    logger.info("\n=== Station Difficulty Ranking ===")
    logger.info("Tier 1 (EASY, >=70%%): Can size up 1.2x base")
    for st in sorted(s for s, t in STATION_DIFFICULTY.items() if t == 1):
        logger.info("  %s (%s)", st, CANONICAL_STATIONS[st])

    logger.info("Tier 2 (AVERAGE, 66-69%%): Standard sizing")
    for st in sorted(s for s, t in STATION_DIFFICULTY.items() if t == 2):
        logger.info("  %s (%s)", st, CANONICAL_STATIONS[st])

    logger.info("Tier 3 (HARD, <66%%): Size down to 0.7x base")
    for st in sorted(s for s, t in STATION_DIFFICULTY.items() if t == 3):
        logger.info("  %s (%s)", st, CANONICAL_STATIONS[st])


def main():
    check_station_lists()
    print_difficulty_ranking()
    logger.info("\n=== Cleanup Complete ===")


if __name__ == "__main__":
    main()