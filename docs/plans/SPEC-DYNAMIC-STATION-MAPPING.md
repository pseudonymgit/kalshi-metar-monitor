# Spec: Station Registry Rewrite (Dynamic Discovery)

## Problem

The station registry (`core/station_registry.py`) has a broken discovery fallback chain and missing infrastructure. The `_try_kalshi_discovery()` function calls `discover_market_derived_station_codes()` (markets endpoint, paginated, misses pages 4+, broken in container), but should call `discover_station_codes_from_series()` (series endpoint, single-call, returns all 53 weather series). Additionally, `audit_city_mapping.py` uses the broken markets endpoint. Several functions referenced in the code (`update_station_mapping_cache`, `is_station_mapping_stale`) don't exist. The `except Exception: pass` pattern hides errors. There is no staleness check for `data/station_mapping.json`.

## Current State

### Broken discovery function in `station_registry.py`

```python
def _try_kalshi_discovery():
    try:
        from core.kalshi_monitor import discover_market_derived_station_codes
        stations = discover_market_derived_station_codes(max_pages=3, page_limit=100)
        if stations:
            return stations
    except Exception:
        pass
    return None
```

This calls `discover_market_derived_station_codes()` which:
- Paginates `/markets?limit=100&status=open` (3 pages only, misses pages 4+)
- Returns a flat list of station codes (no ticker mapping)
- Fails in container (no DNS)

The correct replacement is `discover_station_codes_from_series()` which:
- Calls `/series?tags=Daily%20temperature` (single endpoint, all 53 series)
- Returns `dict[station_code → list[series_tickers]]`
- Wraps the private `_discover_series_for_stations()`

### Missing functions

- `update_station_mapping_cache()` — doesn't exist anywhere
- `is_station_mapping_stale()` — doesn't exist anywhere

### Logging gaps

All `except Exception: pass` blocks in `station_registry.py` swallow errors silently. This includes `_try_kalshi_discovery()`, `_try_cache_file()`, `_try_settlement_epochs()`.

### Missing script

No `scripts/update_station_mapping.py` exists to refresh the cache on demand or on a cron schedule.

### Station mapping file missing `last_discovered` field

`data/station_mapping.json` has `last_verified` (2026-07-28T20:40:00Z) but no `last_discovered` timestamp that records when the Kalshi series API was last queried. This is needed for staleness detection.

### Audit script uses broken endpoint

`scripts/audit_city_mapping.py` calls `discover_market_derived_station_codes()` at line 116 instead of `discover_station_codes_from_series()`.

## Design

### Fix 1: Rewrite `_try_kalshi_discovery()` to use series endpoint

Replace `discover_market_derived_station_codes` with `discover_station_codes_from_series`. The return type changes from `list[str]` to `dict[str, list[str]]` — must handle both.

Strategy: Extract station codes from the dict keys, return a `list[str]` for backward compatibility with `get_all_stations()`.

```python
def _try_kalshi_discovery():
    """Try to discover stations from Kalshi /series API.
    Returns list of station codes or None on failure."""
    try:
        from core.kalshi_monitor import discover_station_codes_from_series
        discovered = discover_station_codes_from_series()
        if discovered:
            return list(discovered.keys())
    except Exception as e:
        _logger.warning(f"Kalshi series discovery failed: {e}")
    return None
```

### Fix 2: Add `update_station_mapping_cache()`

New function in `station_registry.py` that queries Kalshi series endpoint, builds a complete mapping dict including lat/lon/station info from NWS data, and writes it to `data/station_mapping.json` with a `last_discovered` timestamp.

### Fix 3: Add `is_station_mapping_stale()`

New function that checks whether the cache file's `last_discovered` field is older than a threshold (default 24 hours), indicating the cache should be refreshed.

### Fix 4: Add logging to all `except` blocks

Replace bare `pass` with `_logger.warning(...)` in `_try_kalshi_discovery()`, `_try_cache_file()`, `_try_settlement_epochs()`.

### Fix 5: Create `scripts/update_station_mapping.py`

Standalone script that:
1. Imports `update_station_mapping_cache()` from station_registry
2. Calls it to refresh the cache
3. Exits with status code 0 on success, 1 on failure
4. Can be run as a cron job

### Fix 6: Update `scripts/audit_city_mapping.py`

Replace `discover_market_derived_station_codes()` with `discover_station_codes_from_series()`.

### Fix 7: Add `last_discovered` to `data/station_mapping.json`

Add field alongside existing `last_verified`. Format: ISO 8601 UTC timestamp.

## Implementation Steps

### Step 1: Fix `_try_kalshi_discovery()` in `station_registry.py`

File: `core/station_registry.py`

Modify `_try_kalshi_discovery()`:
- Change import from `discover_market_derived_station_codes` to `discover_station_codes_from_series`
- Extract station codes from dict keys: `list(discovered.keys())`
- Add logging to the except block

### Step 2: Add logging to all `except` blocks

File: `core/station_registry.py`

Add `import logging` and `_logger = logging.getLogger(__name__)` at module level.
Replace all bare `except Exception: pass` with `except Exception as e: _logger.warning(...)`.

### Step 3: Add `update_station_mapping_cache()`

File: `core/station_registry.py`

```python
def update_station_mapping_cache():
    """Query Kalshi /series API and rebuild station_mapping.json cache.
    
    Returns True on success, False on failure.
    """
    try:
        from core.kalshi_monitor import discover_station_codes_from_series
        discovered = discover_station_codes_from_series()
        if not discovered:
            _logger.warning("Series discovery returned no stations; cache not updated")
            return False
        
        # Build mapping from existing cache as base to preserve lat/lon info
        mapping = {}
        try:
            with open(_CACHE_PATH, 'r') as f:
                existing = json.load(f)
                if isinstance(existing, dict) and 'stations' in existing:
                    mapping = existing['stations']
                elif isinstance(existing, dict):
                    mapping = existing
        except (FileNotFoundError, json.JSONDecodeError):
            mapping = {}
        
        # Add/update discovered stations
        for station in discovered:
            if station not in mapping:
                mapping[station] = {
                    "city_name": station,  # placeholder; real data comes from NWS/later verification
                    "state": "",
                    "lat": None,
                    "lon": None,
                }
        
        # Write cache with last_discovered timestamp
        cache_data = {
            "version": "2.2",
            "last_discovered": datetime.now(timezone.utc).isoformat(),
            "last_verified": "",
            "source": "Kalshi API (/series?tags=Daily%20temperature)",
            "stations": mapping,
        }
        
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, 'w') as f:
            json.dump(cache_data, f, indent=2)
        
        _logger.info(f"Station mapping cache updated: {len(mapping)} stations")
        return True
    except Exception as e:
        _logger.error(f"Failed to update station mapping cache: {e}")
        return False
```

### Step 4: Add `is_station_mapping_stale()`

File: `core/station_registry.py`

```python
def is_station_mapping_stale(max_age_hours: int = 24) -> bool:
    """Check if station_mapping.json cache is stale.
    
    Args:
        max_age_hours: Maximum age in hours before cache is considered stale
    
    Returns:
        True if cache is missing, unparseable, or older than max_age_hours
    """
    try:
        with open(_CACHE_PATH, 'r') as f:
            data = json.load(f)
        
        last_discovered = data.get('last_discovered') or data.get('last_verified')
        if not last_discovered:
            return True
        
        if isinstance(last_discovered, str):
            last_time = datetime.fromisoformat(last_discovered)
        else:
            return True
        
        age = datetime.now(timezone.utc) - last_time
        return age.total_seconds() > max_age_hours * 3600
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return True
```

### Step 5: Create `scripts/update_station_mapping.py`

File: `scripts/update_station_mapping.py`

```python
#!/usr/bin/env python3
"""Script to refresh the station mapping cache from Kalshi /series API.

Usage:
    python3 scripts/update_station_mapping.py          # Normal run
    python3 scripts/update_station_mapping.py --force   # Force refresh even if not stale
"""

import sys
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.station_registry import update_station_mapping_cache, is_station_mapping_stale


def main():
    force = "--force" in sys.argv
    
    if not force and not is_station_mapping_stale():
        print("Station mapping cache is current. Use --force to refresh anyway.")
        return
    
    success = update_station_mapping_cache()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
```

### Step 6: Fix `scripts/audit_city_mapping.py`

File: `scripts/audit_city_mapping.py`

Replace in `check_kalshi_discovery()`:
```python
from core.kalshi_monitor import discover_station_codes_from_series
stations = discover_station_codes_from_series()
# This returns dict; extract keys for a list
if isinstance(stations, dict):
    stations = list(stations.keys())
```

### Step 7: Bump `data/station_mapping.json` to v2.2

File: `data/station_mapping.json`

Change `"version": "2.1"` to `"version": "2.2"`.
Add `"last_discovered": "<ISO8601 UTC timestamp>"`.

## Changed Files

| File | Change Type | Description |
|---|---|---|
| `core/station_registry.py` | MODIFY | Rewrite `_try_kalshi_discovery()` to use series endpoint |
| `core/station_registry.py` | MODIFY | Add logging to all bare `except` blocks |
| `core/station_registry.py` | ADD | New `update_station_mapping_cache()` function |
| `core/station_registry.py` | ADD | New `is_station_mapping_stale()` function |
| `scripts/update_station_mapping.py` | CREATE | New script to refresh cache on demand/cron |
| `scripts/audit_city_mapping.py` | MODIFY | Switch to `discover_station_codes_from_series()` |
| `data/station_mapping.json` | MODIFY | Bump version to 2.2, add `last_discovered` field |

## Verification Steps

```bash
# 1. Verify station_registry imports cleanly
python3 -c "from core.station_registry import get_all_stations; print('OK')"

# 2. Test new functions
python3 -c "
from core.station_registry import update_station_mapping_cache, is_station_mapping_stale
print(f'Stale: {is_station_mapping_stale()}')
print(f'Update: {update_station_mapping_cache()}')
print(f'Stale after update: {is_station_mapping_stale()}')
print('OK')
"

# 3. Verify get_all_stations() still works
python3 -c "
from core.station_registry import get_all_stations
stations = get_all_stations()
print(f'{len(stations)} stations: {stations}')
print('OK')
"

# 4. Verify update script runs
python3 scripts/update_station_mapping.py --force
echo "Exit code: $?"

# 5. Verify audit script runs
python3 scripts/audit_city_mapping.py 2>&1 | head -20

# 6. Verify station_mapping.json has last_discovered
python3 -c "
import json
data = json.load(open('data/station_mapping.json'))
print(f'Version: {data[\"version\"]}')
print(f'Last discovered: {data.get(\"last_discovered\", \"MISSING\")}')
print(f'Station count: {len(data[\"stations\"])}')
print('OK')
"
```

## Rollback Plan

1. **`_try_kalshi_discovery()` revert**: Change import back to `discover_market_derived_station_codes` and remove logging.
2. **New functions**: Revert `git checkout` on `station_registry.py` if `update_station_mapping_cache()` or `is_station_mapping_stale()` cause issues.
3. **New script**: Simply delete `scripts/update_station_mapping.py`.
4. **Audit script**: Revert the import line to `discover_market_derived_station_codes`.
5. **Cache file**: Revert version to 2.1 in `data/station_mapping.json` and remove `last_discovered`.

## Out of Scope

- Enriching station metadata (lat/lon/city_name) from NWS API — cache currently preserves what exists.
- Making `audit_city_mapping.py` a proper CI gate — it remains a diagnostic script.
- Fixing the container DNS issue — Kalshi API calls will still fail in container; the fallback chain handles this.
- Removing `discover_market_derived_station_codes()` entirely — it's still used by other consumers (verified before deprecating).
- The Kalshi settlements DB "TEST" record — handled by Spec 5.