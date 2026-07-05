import os


def classify_hydration_health(ladder_cache_snapshot):
    snapshot = ladder_cache_snapshot if isinstance(ladder_cache_snapshot, dict) else {}
    stations = snapshot.get("stations") or []
    stale_threshold_seconds = int(os.getenv("HYDRATION_LADDER_CACHE_STALE_THRESHOLD_SECONDS", "1800"))

    station_health = []
    any_unhealthy = False

    for row in stations:
        if not isinstance(row, dict):
            continue

        station = (row.get("station") or "").strip().upper()
        hydration = row.get("hydration") or {}
        cache_present = bool(hydration.get("cache_present"))
        cache_valid = bool(hydration.get("cache_valid"))
        series_discovered = bool(hydration.get("series_discovered"))
        cache_age_seconds = row.get("ladder_cache_age_seconds")

        if not series_discovered:
            status = "DISCOVERY_MISSING"
        elif not cache_present:
            status = "CACHE_MISSING"
        elif cache_age_seconds is None or int(cache_age_seconds) > stale_threshold_seconds:
            status = "CACHE_STALE"
        elif not cache_valid:
            status = "PARTIAL_HYDRATION"
        else:
            status = "HEALTHY"

        if status != "HEALTHY":
            any_unhealthy = True

        station_health.append(
            {
                "station": station,
                "status": status,
                "cache_age_seconds": cache_age_seconds,
            }
        )

    return {
        "healthy": (not any_unhealthy) or (not station_health),
        "station_health": sorted(station_health, key=lambda row: row.get("station") or ""),
    }
