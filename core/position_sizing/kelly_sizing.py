"""
Epsilon — Per-Hour Kelly Sizing (E.2)

Kelly fraction peaks at the optimal entry window, decays toward settlement.

Design:
  - Coastal stations (KNYC, KLAX, KSEA, KSFO, KMIA, KBOS, KPHL, KDCA, KATL):
    Kelly = 0.15 at h-8, linear decay -> 0.05 at h-1
  - Continental stations (KDEN, KDFW, KMDW, KMSP, KOKC, KPHX, KLAS, KHOU, KAUS, KMSY, KSAT):
    Kelly = 0.20 at h-5, linear decay -> 0.05 at h-1
  - Before peak: maintain peak Kelly (entry only fires when edge exists)
  - After h-1: Kelly -> 0.0 (too close to settlement)

Deterministic math only. No AI/ML in loop.

Usage:
    from core.position_sizing.kelly_sizing import KellyTimeSizer

    sizer = KellyTimeSizer(initial_capital=10000.0)
    kelly = sizer.get_kelly_fraction("KNYC", hour_before_settlement=8)
    contracts = sizer.compute_contracts("KNYC", 8, edge=0.05, entry_price=0.50)
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

COASTAL_STATIONS = frozenset({
    'KATL', 'KBOS', 'KDCA', 'KLAX', 'KMIA', 'KNYC', 'KPHL', 'KSEA', 'KSFO',
})
CONTINENTAL_STATIONS = frozenset({
    'KAUS', 'KDEN', 'KDFW', 'KHOU', 'KLAS', 'KMDW', 'KMSP', 'KMSY', 'KOKC',
    'KPHX', 'KSAT',
})
ALL_STATIONS = sorted(COASTAL_STATIONS | CONTINENTAL_STATIONS)

KELLY_PARAMS = {
    'coastal': {
        'peak_hour_before': 8,
        'peak_kelly': 0.15,
        'min_hour_before': 1,
        'min_kelly': 0.05,
    },
    'continental': {
        'peak_hour_before': 5,
        'peak_kelly': 0.20,
        'min_hour_before': 1,
        'min_kelly': 0.05,
    },
}

# Daily contract cap (shared across stations) and per-station safety floor.
DAILY_CONTRACT_CAP = 100


def classify_station(station: str) -> str:
    """Return 'coastal', 'continental', or 'unknown'."""
    st = (station or '').upper().strip()
    if st in COASTAL_STATIONS:
        return 'coastal'
    if st in CONTINENTAL_STATIONS:
        return 'continental'
    return 'unknown'


def get_kelly_fraction(station: str, hour_before_settlement: float) -> float:
    """
    Per-hour Kelly fraction for a station at a given hour-before-settlement.

    Args:
        station: ICAO station code (e.g. 'KNYC', 'KDEN')
        hour_before_settlement: hours until settlement (1 = 1h before, 8 = 8h before)

    Returns:
        Kelly fraction in [0.0, peak_kelly]. Fraction of capital to risk.
    """
    st_type = classify_station(station)
    if st_type == 'unknown':
        logger.warning(f"Unknown station type for {station}, defaulting to coastal")
        st_type = 'coastal'

    params = KELLY_PARAMS[st_type]
    peak_hb = params['peak_hour_before']
    peak_kelly = params['peak_kelly']
    min_hb = params['min_hour_before']
    min_kelly = params['min_kelly']

    hb = float(hour_before_settlement)

    if hb >= peak_hb:
        # Before/at peak: hold peak (entry only fires on edge, no ramp-up needed).
        return peak_kelly

    if hb <= 0:
        # At/after settlement: no edge left.
        return 0.0

    if hb <= min_hb:
        # At the minimum window, floor at min_kelly.
        return min_kelly

    # Linear decay from peak_kelly (at peak_hb) down to min_kelly (at min_hb).
    fraction = (hb - min_hb) / (peak_hb - min_hb)
    kelly = min_kelly + fraction * (peak_kelly - min_kelly)
    return max(min_kelly, min(peak_kelly, kelly))


def compute_position_size(
    capital: float,
    station: str,
    hour_before_settlement: float,
    edge: float,
    entry_price: float,
    base_kelly_fraction: Optional[float] = None,
    max_position_fraction: float = 0.25,
    max_contracts: int = 500,
    min_contracts: int = 1,
) -> Tuple[int, float, Dict]:
    """
    Compute position size in contracts using per-hour Kelly sizing.

    Pipeline:
      1. Get per-hour Kelly fraction for station/hour
      2. Compute raw Kelly: f* = edge / (1 - entry_price)
      3. Apply fractional Kelly: effective = f* * hourly_kelly_fraction
      4. Convert to dollars: position_dollars = capital * effective
      5. Cap at max_position_fraction of capital
      6. Convert to contracts: contracts = position_dollars / entry_price
      7. Apply hard caps (max_contracts, min_contracts)

    Returns:
        (n_contracts, position_dollars, details_dict)
    """
    details: Dict = {}

    if capital is None or capital <= 0:
        details['skip_reason'] = 'zero capital'
        return (0, 0.0, details)

    if edge is None or edge <= 0:
        details['skip_reason'] = 'no edge'
        return (0, 0.0, details)

    if entry_price is None or entry_price <= 0 or entry_price >= 1:
        details['skip_reason'] = 'invalid entry_price'
        return (0, 0.0, details)

    hourly_kelly = (
        base_kelly_fraction
        if base_kelly_fraction is not None
        else get_kelly_fraction(station, hour_before_settlement)
    )
    details['hourly_kelly_fraction'] = hourly_kelly

    if hourly_kelly <= 0:
        details['skip_reason'] = 'kelly zero (too close to settlement)'
        return (0, 0.0, details)

    # Raw Kelly fraction for a binary contract with entry_price = win probability.
    raw_kelly = edge / (1.0 - entry_price)
    effective = raw_kelly * hourly_kelly
    effective = max(0.0, min(effective, max_position_fraction))

    position_dollars = capital * effective
    details['position_dollars'] = position_dollars

    if position_dollars <= 0:
        details['skip_reason'] = 'position size zero'
        return (0, 0.0, details)

    n_contracts = int(position_dollars / entry_price)
    n_contracts = min(n_contracts, max_contracts)

    if n_contracts < min_contracts:
        details['skip_reason'] = f'below min_contracts ({n_contracts} < {min_contracts})'
        return (0, 0.0, details)

    details['raw_kelly'] = raw_kelly
    details['effective_kelly'] = effective
    return (n_contracts, position_dollars, details)


class KellyTimeSizer:
    """
    Per-hour Kelly sizer with stateful capital tracking.

    Tracks remaining capital after each position sizing call.
    """

    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.remaining_capital = initial_capital
        self._daily_contracts: Dict[str, int] = {}
        self._station_contracts: Dict[str, Dict[str, int]] = {}

    def get_kelly_fraction(self, station: str, hour_before_settlement: float) -> float:
        """Delegate to standalone get_kelly_fraction."""
        return get_kelly_fraction(station, hour_before_settlement)

    def compute_contracts(
        self,
        station: str,
        hour_before_settlement: float,
        edge: float,
        entry_price: float,
        date_str: Optional[str] = None,
        max_contracts: int = 500,
        min_contracts: int = 1,
    ) -> int:
        """
        Compute and track position size using remaining capital.

        Reduces remaining_capital by the position cost.
        Prevents exceeding the max daily contract cap.
        """
        today = date_str or datetime.now(timezone.utc).strftime('%Y-%m-%d')

        n_contracts, pos_dollars, details = compute_position_size(
            capital=self.remaining_capital,
            station=station,
            hour_before_settlement=hour_before_settlement,
            edge=edge,
            entry_price=entry_price,
            max_contracts=max_contracts,
            min_contracts=min_contracts,
        )

        if n_contracts <= 0:
            return 0

        # Daily contract cap across all stations.
        daily_total = self._daily_contracts.get(today, 0)
        if daily_total + n_contracts > DAILY_CONTRACT_CAP:
            allowed = DAILY_CONTRACT_CAP - daily_total
            if allowed < min_contracts:
                logger.info(f"[{today}] Daily contract cap reached ({daily_total}/{DAILY_CONTRACT_CAP})")
                return 0
            n_contracts = allowed
            pos_dollars = n_contracts * entry_price
            logger.info(f"[{today}] Daily contract cap: scaling to {n_contracts} contracts")

        self._daily_contracts[today] = daily_total + n_contracts
        self.remaining_capital -= pos_dollars

        self._station_contracts.setdefault(station, {})
        self._station_contracts[station][today] = (
            self._station_contracts[station].get(today, 0) + n_contracts
        )
        return n_contracts

    def reset_daily(self, date_str: Optional[str] = None) -> None:
        """Reset daily tracking for a new day."""
        today = date_str or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self._daily_contracts[today] = 0
        for st in self._station_contracts:
            self._station_contracts[st][today] = 0

    @property
    def capital_used(self) -> float:
        return self.initial_capital - self.remaining_capital


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print('=' * 60)
    print('PER-HOUR KELLY SIZING — SELF-TEST')
    print('=' * 60)

    print('\n--- Test 1: Known peaks ---')
    print(f"  KNYC h-8: kelly={get_kelly_fraction('KNYC', 8):.4f} (expected 0.1500)")
    print(f"  KDEN h-5: kelly={get_kelly_fraction('KDEN', 5):.4f} (expected 0.2000)")

    print('\n--- Test 2: Decay curves ---')
    for hb in (12, 10, 8, 6, 4, 2, 1, 0):
        print(f"  KNYC h-{hb:<2}: kelly={get_kelly_fraction('KNYC', hb):.4f}")
    for hb in (12, 10, 7, 5, 3, 2, 1, 0):
        print(f"  KDEN h-{hb:<2}: kelly={get_kelly_fraction('KDEN', hb):.4f}")

    print('\n--- Test 3: Position sizing ---')
    capital = 10000.0
    for station, hb, edge, price in (
        ('KNYC', 8, 0.05, 0.55),
        ('KDEN', 5, 0.10, 0.50),
        ('KNYC', 2, 0.05, 0.55),
        ('KDEN', 1, 0.10, 0.50),
    ):
        n, pos, details = compute_position_size(capital, station, hb, edge, price)
        print(f"  {station} h-{hb}: kelly={details.get('hourly_kelly_fraction', 0):.4f}, "
              f"contracts={n}, pos=${pos:.2f}")
        if n <= 0 and 'skip_reason' in details:
            print(f"    -> SKIP ({details['skip_reason']})")

    print('\n--- Test 4: KellyTimeSizer stateful ---')
    sizer = KellyTimeSizer(initial_capital=5000.0)
    for station, hb, edge, price in (
        ('KNYC', 8, 0.05, 0.55),
        ('KDEN', 5, 0.10, 0.50),
        ('KNYC', 6, 0.04, 0.55),
    ):
        n = sizer.compute_contracts(station, hb, edge, price)
        print(f"  {station} h-{hb}: {n} contracts, capital remaining=${sizer.remaining_capital:.2f}")

    print('\nDone.')
