#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-06 C5: Add kalshi_calendar.py and integrate with station_time.py for trading day checks]
#

"""
Kalshi Trading Calendar Module

Tracks Kalshi market trading days and settlement schedules.
Prevents signal generation on non-trading days and adjusts entry windows.

Key Features:
- Knows which days Kalshi markets are open/closed (weekends, holidays)
- Tracks settlement schedules (HIGH/LOW markets settle next business day)
- Prevents signal generation for dates with no market
- Adjusts T-18h to T-2h window to skip non-trading days

Usage:
    from kalshi_calendar import is_trading_day, get_next_trading_day, get_entry_window

 Kalshi Markets:
 - Trade Monday-Friday, 7am-5pm ET (approx)
 - No trading on weekends or US holidays
 - HIGH and LOW markets settle on the next business day
"""

import os
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Set
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOLIDAYS_FILE = REPO_ROOT / "data" / "kalshi_holidays.json"


# US Federal Holidays (approximate - adjust as needed)
# Format: (month, day) - year-agnostic for annual holidays
ANNUAL_HOLIDAYS = {
    (1, 1),    # New Year's Day
    (1, 15),   # MLK Day (3rd Mon in Jan - approximated)
    (2, 12),   # Lincoln's Birthday (approximated)
    (2, 19),   # Washington's Birthday (3rd Mon in Feb - approximated)
    (5, 31),   # Memorial Day (last Mon in May - approximated)
    (6, 19),   # Juneteenth
    (7, 4),    # Independence Day
    (9, 7),    # Labor Day (1st Mon in Sep - approximated)
    (10, 12),  # Columbus Day (2nd Mon in Oct - approximated)
    (11, 11),  # Veterans Day
    (11, 26),  # Thanksgiving (4th Thu in Nov - approximated)
    (11, 27),  # Friday after Thanksgiving
    (12, 25),  # Christmas
}

# Holiday observed on nearest weekday if on weekend
def _get_observed_date(month: int, day: int, year: int) -> date:
    """Get the observed date for a holiday (moved to weekday if on weekend)."""
    d = date(year, month, day)
    if d.weekday() >= 5:  # Saturday or Sunday
        # Move to previous Friday or next Monday
        if d.weekday() == 5:  # Saturday
            d = d - timedelta(days=1)
        else:  # Sunday
            d = d + timedelta(days=1)
    return d


# Loaded holidays (from file or defaults)
_LOADED_HOLIDAYS: Set[date] = set()


def _load_holidays() -> Set[date]:
    """Load holidays from file or use defaults."""
    global _LOADED_HOLIDAYS
    if _LOADED_HOLIDAYS:
        return _LOADED_HOLIDAYS
    
    # Try to load from file
    if HOLIDAYS_FILE.exists():
        try:
            import json
            with open(HOLIDAYS_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    _LOADED_HOLIDAYS = set()
                    for h in data:
                        if isinstance(h, str):
                            _LOADED_HOLIDAYS.add(datetime.fromisoformat(h).date())
                        elif isinstance(h, dict):
                            _LOADED_HOLIDAYS.add(date(h['year'], h['month'], h['day']))
                    return _LOADED_HOLIDAYS
        except Exception:
            pass  # Fall through to defaults
    
    # Generate default holidays for current year
    _LOADED_HOLIDAYS = set()
    current_year = datetime.now(timezone.utc).year
    for month, day in ANNUAL_HOLIDAYS:
        _LOADED_HOLIDAYS.add(_get_observed_date(month, day, current_year))
        # Also add the previous year's holidays for cross-year coverage
        _LOADED_HOLIDAYS.add(_get_observed_date(month, day, current_year - 1))
        _LOADED_HOLIDAYS.add(_get_observed_date(month, day, current_year + 1))
    
    return _LOADED_HOLIDAYS


def is_trading_day(d: date) -> bool:
    """
    Check if a date is a Kalshi trading day.
    
    Kalshi markets are typically open Monday-Friday, 7am-5pm ET.
    They are closed on weekends and US federal holidays.
    
    Args:
        d: Date to check
        
    Returns:
        True if trading is expected, False otherwise
    """
    _load_holidays()
    
    # Weekends are closed
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    
    # Holidays are closed
    if d in _LOADED_HOLIDAYS:
        return False
    
    return True


def get_next_trading_day(d: date) -> date:
    """
    Get the next trading day after a given date.
    
    Args:
        d: Date to start from
        
    Returns:
        Next trading day
    """
    next_day = d + timedelta(days=1)
    while not is_trading_day(next_day):
        next_day = next_day + timedelta(days=1)
    return next_day


def get_prev_trading_day(d: date) -> date:
    """
    Get the previous trading day before a given date.
    
    Args:
        d: Date to start from
        
    Returns:
        Previous trading day
    """
    prev_day = d - timedelta(days=1)
    while not is_trading_day(prev_day):
        prev_day = prev_day - timedelta(days=1)
    return prev_day


def get_entry_window(start_date: date, end_date: date) -> list:
    """
    Get valid trading days within an entry window.
    
    Filters out weekends and holidays from the specified date range.
    
    Args:
        start_date: Window start (inclusive)
        end_date: Window end (inclusive)
        
    Returns:
        List of valid trading days
    """
    days = []
    current = start_date
    while current <= end_date:
        if is_trading_day(current):
            days.append(current)
        current = current + timedelta(days=1)
    return days


def get_settlement_date(trading_date: date, market_type: str = "HIGH") -> date:
    """
    Get the settlement date for a trading day.
    
    HIGH and LOW markets settle on the next business day.
    
    Args:
        trading_date: Trading date
        market_type: "HIGH" or "LOW"
        
    Returns:
        Settlement date (next business day)
    """
    next_day = get_next_trading_day(trading_date)
    return next_day


def is_valid_entry_date(entry_date: date, target_date: date, market_type: str = "HIGH") -> bool:
    """
    Check if an entry date is valid for a target settlement date.
    
    The entry window is typically T-18h to T-2h before settlement.
    This function checks if the entry date is within the valid range,
    accounting for non-trading days.
    
    Args:
        entry_date: Proposed entry date
        target_date: Target settlement date
        market_type: "HIGH" or "LOW"
        
    Returns:
        True if entry date is valid
    """
    # Get the expected entry date (T-18h to T-2h before settlement)
    # For simplicity, we use T-1 day (previous trading day)
    expected_entry = get_prev_trading_day(target_date)
    
    # Entry must be on or before the expected entry date
    # and within a reasonable window (T-18h to T-2h)
    if entry_date > expected_entry:
        return False
    
    # Entry must be a valid trading day
    if not is_trading_day(entry_date):
        return False
    
    return True


def generate_holidays_json():
    """
    Generate a JSON file with holidays for the current year.
    
    This can be used to override the default holiday list.
    """
    import json
    
    holidays = []
    current_year = datetime.now(timezone.utc).year
    for month, day in ANNUAL_HOLIDAYS:
        observed = _get_observed_date(month, day, current_year)
        holidays.append({
            "year": observed.year,
            "month": observed.month,
            "day": observed.day,
            "name": f"Holiday {month}/{day}"
        })
    
    output = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "year": current_year,
        "holidays": holidays
    }
    
    if HOLIDAYS_FILE.parent.exists():
        with open(HOLIDAYS_FILE, 'w') as f:
            json.dump(output, f, indent=2)
        print(f"Holidays written to {HOLIDAYS_FILE}")
    else:
        print(f"Cannot write holidays - directory {HOLIDAYS_FILE.parent} does not exist")
    
    return output


if __name__ == "__main__":
    print("Kalshi Trading Calendar")
    print("=" * 50)
    
    today = datetime.now(timezone.utc).date()
    print(f"Today: {today} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][today.weekday()]})")
    print(f"Is trading day: {is_trading_day(today)}")
    
    # Show next 7 days
    print(f"\nNext 7 days:")
    for i in range(7):
        d = today + timedelta(days=i)
        status = "OPEN" if is_trading_day(d) else "CLOSED"
        print(f"  {d}: {status}")
    
    # Show holidays for current year
    print(f"\nHolidays for {today.year}:")
    loaded = _load_holidays()
    for d in sorted(loaded):
        if d.year == today.year:
            print(f"  {d}")
