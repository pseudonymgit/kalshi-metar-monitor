# CHANGELOG (last 10 broad changes):
# 1. [2026-07-06 C5: Add kalshi_calendar.py and integrate with station_time.py for trading day checks]
# 2. [2026-07-05 R4-1.3: Settlement-window entry timing (T-18h to T-2h)]
# 3. [2026-06-17 feat: L0-L4 implementation + 4 backtest fixes + goldilocks confidence scoring]
# 4. [2026-03-01 Restore metar monitor to shared station-time helpers]
#


from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import sys
import os

# Add core directory to path for kalshi_calendar import
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

try:
    from kalshi_calendar import is_trading_day, get_prev_trading_day
except ImportError:
    # Fallback if kalshi_calendar is not available
    def is_trading_day(d):
        return d.weekday() < 5  # Mon-Fri only
    def get_prev_trading_day(d):
        prev = d - timedelta(days=1)
        while not is_trading_day(prev):
            prev = prev - timedelta(days=1)
        return prev

try:
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:
    ZoneInfo = None

# Layer 4: Timezone validation
try:
    import pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False
    pytz = None


_ICAO_TZ = {
    "KATL": "America/New_York",
    "KAUS": "America/Chicago",
    "KBOS": "America/New_York",
    "KDCA": "America/New_York",
    "KDEN": "America/Denver",
    "KDFW": "America/Chicago",
    "KHOU": "America/Chicago",
    "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago",
    "KMIA": "America/New_York",
    "KMSP": "America/Chicago",
    "KNYC": "America/New_York",
    "KPHL": "America/New_York",
    "KPHX": "America/Phoenix",
    "KSEA": "America/Los_Angeles",
    "KSFO": "America/Los_Angeles",
}


# Layer 4: Timezone validation functions
def validate_timezone(tz_name: str) -> str:
    """Validate and return timezone name, fail-closed on invalid.
    
    Args:
        tz_name: Timezone name to validate
        
    Returns:
        Validated timezone name
        
    Raises:
        ValueError: If timezone is invalid
    """
    if not tz_name:
        raise ValueError("Invalid timezone: empty string")
    
    if HAS_PYTZ:
        if tz_name not in pytz.all_timezones:
            raise ValueError(f"Invalid timezone={tz_name}")
    elif ZoneInfo is None:
        # If neither pytz nor zoneinfo available, fail-open with warning
        return tz_name  # type: ignore
    
    return tz_name


def station_timezone_name(icao: str) -> str:
    return _ICAO_TZ.get((icao or "").upper(), "America/New_York")


def parse_iso_utc(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    normalized = str(timestamp).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_station_local(icao: str, dt_utc: datetime) -> datetime:
    if ZoneInfo is None:
        return dt_utc
    tz_name = validate_timezone(station_timezone_name(icao))
    return dt_utc.astimezone(ZoneInfo(tz_name))


def station_local_day_key(icao: str, timestamp_utc: Optional[str]) -> str:
    dt_utc = parse_iso_utc(timestamp_utc)
    if dt_utc is None:
        return "unknown"
    return to_station_local(icao, dt_utc).date().isoformat()


# ─── Settlement Window Timing ───────────────────────────────────────────
#
# Kalshi daily HIGH/LOW markets settle at end of local trading day.
# The entry window restricts trades to T-18h through T-2h before settlement.
# Same-day METARs are ~10x more predictive than prior-day observations.
#
# Entry window: T-18h to T-2h before settlement
# Settlement: midnight local time (end of trading day)

ENTRY_WINDOW_HOURS_BEFORE_SETTLEMENT = 18  # Don't enter more than 18h before settlement
ENTRY_WINDOW_HOURS_BEFORE_CLOSE = 2  # Don't enter within 2h of settlement (too late)


def settlement_time_utc(icao: str, trading_date: str) -> Optional[datetime]:
    """
    Calculate the settlement time in UTC for a station and trading date.
    
    Kalshi daily HIGH/LOW markets settle at end of local trading day,
    which we approximate as midnight local time of the next day.
    
    Args:
        icao: Station code (e.g., 'KATL')
        trading_date: Local trading date in YYYY-MM-DD format
    
    Returns:
        Settlement time in UTC, or None if timezone unavailable
    """
    if ZoneInfo is None:
        # Fallback: assume UTC midnight
        try:
            return datetime.fromisoformat(f"{trading_date}T23:59:59+00:00")
        except ValueError:
            return None
    
    tz_name = validate_timezone(station_timezone_name(icao))
    try:
        tz = ZoneInfo(tz_name)
        # Settlement is at midnight local time of the trading date + 1 day
        # (i.e., end of the local trading day)
        local_date = datetime.fromisoformat(f"{trading_date}T00:00:00")
        settlement_local = local_date.replace(tzinfo=tz) + timedelta(days=1)
        return settlement_local.astimezone(timezone.utc)
    except Exception:
        return None


def is_within_entry_window(
    icao: str,
    trading_date: str,
    now_utc: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Check if the current time is within the valid entry window for a market.
    
    Entry window: T-18h to T-2h before settlement.
    - Before T-18h: too early, signals not reliable enough
    - After T-2h: too late, market about to settle
    - Skips weekends and holidays (no trading on those days)
    
    Args:
        icao: Station code
        trading_date: Local trading date in YYYY-MM-DD format
        now_utc: Current time in UTC (defaults to datetime.now(timezone.utc))
    
    Returns:
        Tuple of (is_within_window, reason)
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    
    settlement = settlement_time_utc(icao, trading_date)
    if settlement is None:
        return False, "settlement_time_unknown"
    
    hours_until_settlement = (settlement - now_utc).total_seconds() / 3600.0
    
    if hours_until_settlement < 0:
        return False, f"market_already_settled ({hours_until_settlement:.1f}h ago)"
    
    # Check if we're too close to settlement (before adjusting for trading days)
    if hours_until_settlement < ENTRY_WINDOW_HOURS_BEFORE_CLOSE:
        # Check if the previous trading day is a weekend/holiday
        current_date = now_utc.date()
        if not is_trading_day(current_date):
            return False, f"current_date_not_trading ({current_date})"
        
        # Get the previous trading day
        prev_trading = get_prev_trading_day(current_date)
        
        # Check if we're still too close to settlement on the previous trading day
        # This handles cases where T-2h falls on a weekend/holiday
        hours_prev = hours_until_settlement + 24  # Add 24h to account for previous day
        if hours_prev < ENTRY_WINDOW_HOURS_BEFORE_CLOSE:
            return False, f"too_close_to_settlement ({hours_until_settlement:.1f}h < {ENTRY_WINDOW_HOURS_BEFORE_CLOSE}h)"
    
    if hours_until_settlement > ENTRY_WINDOW_HOURS_BEFORE_SETTLEMENT:
        return False, f"too_far_from_settlement ({hours_until_settlement:.1f}h > {ENTRY_WINDOW_HOURS_BEFORE_SETTLEMENT}h)"
    
    return True, f"within_entry_window ({hours_until_settlement:.1f}h to settlement)"
