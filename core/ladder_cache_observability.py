from datetime import datetime, timezone

from core.kalshi_monitor import (
    get_cached_series_markets,
    get_hydration_prerequisite_state_snapshot,
    get_last_hydration_execution_snapshot,
)


def _parse_iso_utc(iso_timestamp):
    if not iso_timestamp:
        return None
    try:
        return datetime.fromisoformat(str(iso_timestamp).replace("Z", "+00:00"))
    except Exception:
        return None


def _cache_age_seconds(now_utc, hydrated_at_utc):
    hydrated_dt = _parse_iso_utc(hydrated_at_utc)
    if hydrated_dt is None:
        return None
    if hydrated_dt.tzinfo is None:
        hydrated_dt = hydrated_dt.replace(tzinfo=timezone.utc)
    age_seconds = int((now_utc - hydrated_dt).total_seconds())
    return max(age_seconds, 0)


def build_ladder_cache_snapshot(stations):
    now_utc = datetime.now(timezone.utc)
    hydration_prereq = get_hydration_prerequisite_state_snapshot() or {}
    hydration_execution = get_last_hydration_execution_snapshot() or {}

    normalized_stations = {
        (station or "").strip().upper()
        for station in (stations or [])
        if (station or "").strip()
    }
    station_keys = sorted(normalized_stations | set(hydration_prereq.keys()) | set(hydration_execution.keys()))

    station_rows = []
    for station in station_keys:
        prereq_state = hydration_prereq.get(station) or {}
        execution_state = hydration_execution.get(station) or {}

        series_ticker = (execution_state.get("series_ticker") or "").strip().upper()
        cached_series = get_cached_series_markets(series_ticker) if series_ticker else None
        cached_markets = (cached_series or {}).get("markets") or []
        hydrated_at_utc = (cached_series or {}).get("hydrated_at_utc")

        station_rows.append(
            {
                "station": station,
                "series_ticker": series_ticker or None,
                "market_count": len(cached_markets),
                "ladder_cache_age_seconds": _cache_age_seconds(now_utc, hydrated_at_utc),
                "hydration": {
                    "cache_present": bool(cached_series),
                    "series_discovered": bool(series_ticker),
                    "cache_valid": bool(prereq_state.get("cache_valid")),
                },
                "last_hydration_execution_utc": execution_state.get("evaluated_at_utc"),
            }
        )

    return {
        "generated_utc": now_utc.isoformat(),
        "station_count": len(station_rows),
        "stations": station_rows,
    }
