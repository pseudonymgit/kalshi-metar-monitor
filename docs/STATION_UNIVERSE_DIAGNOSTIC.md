# STATION UNIVERSE DIAGNOSTIC

## Execution Authority Check (this environment)

Attempted production runtime query:

```bash
curl -i -sS https://kalshi-metar-monitor.onrender.com/observability/runtime-authority-snapshot
```

Observed result:

- `curl: (56) CONNECT tunnel failed, response 403`
- `HTTP/1.1 403 Forbidden`
- body: `Domain forbidden`

Because production observability endpoints are unreachable from this execution environment, authoritative runtime truth for the requested universe/discovery fields is **not obtainable here**.

## Required Production Runtime Execution (Render)

Run the following directly from Render runtime authority (Render Shell/Exec on the production web service), then save the resulting JSON artifacts.

### 1) Canonical live station universe + per-station source classification

```bash
python - <<'PY'
import json
from datetime import datetime, timezone
import app
from core.kalshi_monitor import (
    get_hydration_prerequisite_state_snapshot,
    get_ladder_state_snapshot,
    get_kalshi_connectivity_snapshot,
    _SERIES_BY_STATION,
)
from persistence import get_default_config, get_state, get_watchlist

station_universe = app._canonical_live_station_universe()
stations = station_universe.get("stations") or []
market_polling = station_universe.get("market_polling_stations") or set()
configured_union = station_universe.get("configured_stations") or set()
discovered = station_universe.get("discovered_stations") or set()
watchlist = station_universe.get("watchlist_stations") or set()

cfg = get_default_config() or {}
state = get_state() or {}
watch = get_watchlist() or {}

cfg_stations = {
    s.strip().upper() for s in (cfg.get("stations") or []) if isinstance(s, str) and s.strip()
}
state_stations = {
    s.strip().upper() for s in (state.get("stations") or []) if isinstance(s, str) and s.strip()
}
watch_stations = {
    s.strip().upper() for s in (watch.get("watchlist") or []) if isinstance(s, str) and s.strip()
}

hydration_prereq = get_hydration_prerequisite_state_snapshot() or {}
ladder_state = (get_ladder_state_snapshot() or {}).get("ladder_state") or {}

series_by_station = dict(_SERIES_BY_STATION)

rows = []
for st in stations:
    source = []
    if st in market_polling:
        source.append("market_derived")
    if st in cfg_stations:
        source.append("configured_list")
    if st in state_stations:
        source.append("runtime_state")
    if st in watch_stations:
        source.append("watchlist")

    if not market_polling:
        source.append("fallback_union")

    hp = hydration_prereq.get(st) or {}
    ladder_keys = sorted(k for k in ladder_state.keys() if isinstance(k, str) and k.startswith(f"{st}_"))

    rows.append({
        "station": st,
        "source_classification": source,
        "series_discovered": bool(series_by_station.get(st)),
        "series_ticker": series_by_station.get(st),
        "last_successful_discovery_timestamp": (get_kalshi_connectivity_snapshot() or {}).get("last_series_discovery_success_utc"),
        "hydration_cache_status": "present" if ladder_keys else "missing",
        "hydration_last_updated": ((app.get_cached_series_markets(series_by_station.get(st)) or {}).get("hydrated_at_utc") if series_by_station.get(st) else None),
    })

out = {
    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    "canonical_live_station_universe": stations,
    "station_details": rows,
    "series_by_station_size": len(series_by_station),
    "discover_series_success_in_runtime_cycle": bool((get_kalshi_connectivity_snapshot() or {}).get("last_series_discovery_success_utc")),
}

print(json.dumps(out, indent=2, sort_keys=True))
PY
```

### 2) Discovery authority status

```bash
python - <<'PY'
import json
from datetime import datetime, timezone
from core.kalshi_monitor import get_kalshi_connectivity_snapshot

snap = get_kalshi_connectivity_snapshot() or {}
error = snap.get("last_series_discovery_error")

result = {
    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    "last_kalshi_series_discovery_attempt_timestamp": snap.get("last_series_discovery_success_utc"),
    "success_failure_result": "failure" if error else ("success" if snap.get("series_discovery_attempted") else "not_attempted"),
    "exception_type": None,
    "retry_behavior": "on-demand via ensure_series_discovery_loaded() when _SERIES_DISCOVERED is false",
    "fallback_triggered": "yes" if (error and not snap.get("last_series_discovery_success_utc")) else "no",
    "raw_snapshot": snap,
}

if error:
    result["exception_type"] = "unknown_from_snapshot_only"

print(json.dumps(result, indent=2, sort_keys=True))
PY
```

### 3) Explicit mode statement rule (must be data-driven)

After capturing output from step 1:

- If `source_classification` for stations includes `market_derived` and does **not** include `fallback_union`, record:
  - `System is operating in MARKET-DERIVED mode`
- Otherwise record:
  - `System is operating in FALLBACK mode`

## Current mode statement from this execution environment

Not determinable from this environment because production runtime endpoints are blocked (`403 Domain forbidden`).
