"""
Epsilon — Consecutive Loss Guard (E.4)

After 3 consecutive losses: halve position.
After 5 consecutive losses: zero until next week.

Design:
  - Reads trade outcomes from paper_trading.db (or entry engine health file)
  - Tracks consecutive losses per station
  - When threshold exceeded: reduces position multiplier
  - Reset: after N losses, positions frozen for rest of week

Integration:
  - Called by entry engine before computing position sizes
  - Returns position multiplier (1.0 = normal, 0.5 = halved, 0.0 = frozen)

Deterministic math only. No AI/ML in loop.

Usage:
    python3 scripts/loss_guard.py             # Run check
    python3 scripts/loss_guard.py --reset     # Reset all counters
    python3 scripts/loss_guard.py --status    # Show current state
"""

import argparse
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_DB = REPO_ROOT / 'data' / 'paper_trading.db'
HEALTH_DIR = REPO_ROOT / 'data' / 'intraday'
STATE_FILE = HEALTH_DIR / 'loss_guard_state.json'

logger = logging.getLogger('loss_guard')

CONSECUTIVE_LOSS_HALVE_THRESHOLD = 3
CONSECUTIVE_LOSS_ZERO_THRESHOLD = 5
POSITION_HALVE_MULTIPLIER = 0.5
POSITION_ZERO_MULTIPLIER = 0.0
LOSS_RESET_DAYS = 7


def _next_week_date(from_date_str: str) -> str:
    """Return the Monday following the given date (inclusive next week)."""
    dt = datetime.strptime(from_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    days_ahead = (7 - dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_monday = dt + timedelta(days=days_ahead)
    return next_monday.strftime('%Y-%m-%d')


class LossGuardState:
    """Tracks consecutive losses per station, persisted to JSON."""

    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or STATE_FILE
        self.losses: Dict[str, int] = defaultdict(int)
        self.last_loss_date: Dict[str, str] = {}
        self.frozen_until: Dict[str, str] = {}
        self._loaded = False

    def load(self, state_file: Optional[Path] = None) -> None:
        """Load state from JSON file."""
        sf = state_file or self.state_file
        if not sf.exists():
            self._loaded = False
            return
        try:
            data = json.loads(sf.read_text())
            self.losses = defaultdict(int, {k: int(v) for k, v in data.get('losses', {}).items()})
            self.last_loss_date = data.get('last_loss_date', {})
            self.frozen_until = data.get('frozen_until', {})
            self._loaded = True
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load loss state: {e}")

    def save(self, state_file: Optional[Path] = None) -> None:
        """Save state to JSON file."""
        sf = state_file or self.state_file
        sf.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'losses': dict(self.losses),
            'last_loss_date': self.last_loss_date,
            'frozen_until': self.frozen_until,
            'updated_at_utc': datetime.now(timezone.utc).isoformat(),
        }
        sf.write_text(json.dumps(data, indent=2))

    def record_outcome(self, station: str, was_profitable: bool, date_str: Optional[str] = None) -> None:
        """
        Record a trade outcome for a station.

        Args:
            station: ICAO code
            was_profitable: True if the trade was profitable
            date_str: trading date YYYY-MM-DD (default: today)
        """
        st = station.upper().strip()
        today = date_str or datetime.now(timezone.utc).strftime('%Y-%m-%d')

        if was_profitable:
            # Reset the consecutive-loss streak on a win.
            self.losses[st] = 0
            self.last_loss_date.pop(st, None)
            return

        # A loss: increment streak (reset if the last loss was on a prior week).
        prev_date = self.last_loss_date.get(st)
        if prev_date is not None:
            prev_monday = _next_week_date(prev_date)
            if today >= prev_monday:
                # New week — restart the streak.
                self.losses[st] = 1
            else:
                self.losses[st] = self.losses.get(st, 0) + 1
        else:
            self.losses[st] = 1

        self.last_loss_date[st] = today

        n = self.losses[st]
        if n >= CONSECUTIVE_LOSS_ZERO_THRESHOLD:
            self.frozen_until[st] = _next_week_date(today)

    def get_position_multiplier(self, station: str, date_str: Optional[str] = None) -> float:
        """
        Position multiplier for a station based on consecutive losses.

        Returns 1.0 (normal), 0.5 (halved, 3+ losses), or 0.0 (frozen, 5+ until next week).
        """
        st = station.upper().strip()
        today = date_str or datetime.now(timezone.utc).strftime('%Y-%m-%d')

        frozen_until = self.frozen_until.get(st)
        if frozen_until is not None and today < frozen_until:
            return POSITION_ZERO_MULTIPLIER
        if frozen_until is not None and today >= frozen_until:
            # Freeze expired.
            self.frozen_until.pop(st, None)
            self.losses[st] = 0

        n = self.losses.get(st, 0)
        if n >= CONSECUTIVE_LOSS_ZERO_THRESHOLD:
            return POSITION_ZERO_MULTIPLIER
        if n >= CONSECUTIVE_LOSS_HALVE_THRESHOLD:
            return POSITION_HALVE_MULTIPLIER
        return 1.0

    def reset(self, station: Optional[str] = None) -> None:
        """Reset loss counters. If station is None, reset all."""
        if station:
            st = station.upper().strip()
            self.losses.pop(st, None)
            self.last_loss_date.pop(st, None)
            self.frozen_until.pop(st, None)
        else:
            self.losses.clear()
            self.last_loss_date.clear()
            self.frozen_until.clear()
        self.save()

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Current status for all tracked stations."""
        result = {}
        stations = set(self.losses.keys()) | set(self.frozen_until.keys())
        for st in sorted(stations):
            result[st] = {
                'consecutive_losses': self.losses.get(st, 0),
                'last_loss_date': self.last_loss_date.get(st, None),
                'frozen_until': self.frozen_until.get(st, None),
                'position_multiplier': self.get_position_multiplier(st),
            }
        return result


def get_position_multiplier(station: str, consecutive_losses: Optional[int] = None,
                            state: Optional[LossGuardState] = None) -> float:
    """
    Convenience: position multiplier for a station.

    If consecutive_losses is provided, compute the multiplier directly from the
    count (no state needed). Otherwise read from the given (or new) state.
    """
    if consecutive_losses is not None:
        n = consecutive_losses
        if n >= CONSECUTIVE_LOSS_ZERO_THRESHOLD:
            return POSITION_ZERO_MULTIPLIER
        if n >= CONSECUTIVE_LOSS_HALVE_THRESHOLD:
            return POSITION_HALVE_MULTIPLIER
        return 1.0

    state = state or LossGuardState()
    state.load()
    return state.get_position_multiplier(station)


def display_status(state: LossGuardState) -> str:
    """Render a human-readable status table."""
    lines = []
    status = state.get_status()
    if not status:
        return "(no tracked stations)"
    lines.append(f"{'station':<8} {'losses':>7} {'multiplier':>10}  last_loss/frozen")
    for st, info in status.items():
        lines.append(
            f"{st:<8} {info['consecutive_losses']:>7} "
            f"{info['position_multiplier']:>10.2f}  "
            f"loss={info['last_loss_date']} frozen={info['frozen_until']}"
        )
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='Consecutive Loss Guard')
    parser.add_argument('--reset', action='store_true', help='Reset all counters')
    parser.add_argument('--reset-station', type=str, help='Reset a single station')
    parser.add_argument('--status', action='store_true', help='Show current state')
    args = parser.parse_args()

    state = LossGuardState()
    state.load()

    if args.reset:
        state.reset()
        print('All loss counters reset.')
        return
    if args.reset_station:
        state.reset(args.reset_station)
        print(f'Reset counters for {args.reset_station.upper()}.')
        return

    print(display_status(state))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
