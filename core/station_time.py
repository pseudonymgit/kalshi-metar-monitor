from datetime import datetime, timezone
from typing import Optional

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
    "KDEN": "America/Denver",
    "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago",
    "KAUS": "America/Chicago",
    "KMIA": "America/New_York",
    "KPHL": "America/New_York",
    "KNYC": "America/New_York",
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
