# Spec: Data File Updates

## Problem

Two data files need updates identified in the pre-sweep cleanup:

1. `data/station_mapping.json` needs a `last_discovered` field added and version bumped to 2.2
2. `data/kalshi_settlements.db` contains a "TEST" station record (1 row) that should be deleted — it's test data from development, not a real Kalshi settlement station

## Current State

### `data/station_mapping.json`

Current structure (v2.1):
```json
{
  "version": "2.1",
  "last_verified": "2026-07-28T20:40:00Z",
  "source": "Live Kalshi API (...) — settlement_sources from each series event",
  "station_count": 20,
  "stations": { ... 20 station entries ... }
}
```

Missing:
- `last_discovered` field — timestamp of the last successful Kalshi series API discovery
- Version is 2.1, needs to be 2.2

The file has 20 stations, 20 station entries, all with lat/lon/city_name/state/kalshi_series/nws_issuedby/nws_site/station_name/settlement_source_url. This is the authoritative station mapping cache.

### `data/kalshi_settlements.db`

Table `kalshi_settlements` has a "TEST" station record:

| id | station | series | event_ticker | target_date | kalshi_temp | markets_found | finalized_count | source_type |
|---|---|---|---|---|---|---|---|---|
| 6071 | TEST | KXHIGHTEST | KXHIGHTEST-26AUG01 | 2026-08-01 | 85.0 | 3 | 3 | historical_api |

This is a development artifact — a test entry that was accidentally committed. It appears in `SELECT DISTINCT station` queries, inflating the station count from 20 to 21 and potentially causing issues in any code that iterates all stations.

## Design

### Update 1: `data/station_mapping.json` — Add `last_discovered`, bump version

Changes:
1. `"version"`: `"2.1"` → `"2.2"`
2. Add `"last_discovered"`: `"2026-08-11T04:00:00Z"` (approximate timestamp of the last series discovery that produced accurate results)
3. Keep all existing fields unchanged

### Update 2: `data/kalshi_settlements.db` — Delete TEST record

Execute SQL: `DELETE FROM kalshi_settlements WHERE station = 'TEST';`

This removes 1 row. The station count goes from 21 to 20 distinct stations.

## Implementation Steps

### Step 1: Update `data/station_mapping.json`

File: `data/station_mapping.json`

1. Edit the file to change `"version": "2.1"` to `"version": "2.2"`
2. Add `"last_discovered": "2026-08-11T04:00:00Z"` after `"last_verified"`

The result:
```json
{
  "version": "2.2",
  "last_discovered": "2026-08-11T04:00:00Z",
  "last_verified": "2026-07-28T20:40:00Z",
  "source": "Live Kalshi API (...) — settlement_sources from each series event",
  "station_count": 20,
  "stations": { ... }
}
```

### Step 2: Delete TEST record from `data/kalshi_settlements.db`

Run SQL: `DELETE FROM kalshi_settlements WHERE station = 'TEST';`

This can be done via Python:
```python
import sqlite3
conn = sqlite3.connect('data/kalshi_settlements.db')
conn.execute("DELETE FROM kalshi_settlements WHERE station = 'TEST'")
conn.commit()
print(f"Deleted {conn.total_changes} rows")
conn.close()
```

Or via `sqlite3` CLI:
```bash
sqlite3 data/kalshi_settlements.db "DELETE FROM kalshi_settlements WHERE station = 'TEST'; VACUUM;"
```

## Changed Files

| File | Change Type | Description |
|---|---|---|
| `data/station_mapping.json` | MODIFY | Bump version 2.1→2.2, add `last_discovered` field |
| `data/kalshi_settlements.db` | MODIFY | Delete 1 "TEST" row from kalshi_settlements table |

## Verification Steps

```bash
# 1. Verify station_mapping.json
python3 -c "
import json
data = json.load(open('data/station_mapping.json'))
assert data['version'] == '2.2', f'Expected 2.2, got {data[\"version\"]}'
assert 'last_discovered' in data, 'Missing last_discovered field'
assert 'last_verified' in data, 'Missing last_verified field'
assert len(data['stations']) == 20, f'Expected 20 stations, got {len(data[\"stations\"])}'
print(f'version: {data[\"version\"]}')
print(f'last_discovered: {data[\"last_discovered\"]}')
print(f'station_count: {len(data[\"stations\"])}')
print('station_mapping.json: OK')
"

# 2. Verify TEST record deleted
python3 -c "
import sqlite3
conn = sqlite3.connect('data/kalshi_settlements.db')
cur = conn.cursor()
cur.execute(\"SELECT COUNT(*) FROM kalshi_settlements WHERE station = 'TEST'\")
count = cur.fetchall()[0][0]
assert count == 0, f'Expected 0 TEST records, found {count}'
cur.execute('SELECT COUNT(DISTINCT station) FROM kalshi_settlements')
station_count = cur.fetchall()[0][0]
assert station_count == 20, f'Expected 20 distinct stations, found {station_count}'
print(f'TEST records: {count} (clean)')
print(f'Distinct stations: {station_count}')
print('kalshi_settlements.db: OK')
conn.close()
"

# 3. Verify no downstream breakage
python3 -c "
from core.station_registry import get_all_stations
stations = get_all_stations()
assert 'TEST' not in stations, 'TEST still in station registry!'
print(f'Station registry clean: {len(stations)} stations, TEST not present')
print('OK')
"
```

## Rollback Plan

1. **station_mapping.json**: Revert version to 2.1, remove `last_discovered` field.
2. **kalshi_settlements.db**: Insert the TEST record back:
   ```sql
   INSERT INTO kalshi_settlements (station, series, event_ticker, target_date, kalshi_temp, markets_found, finalized_count, source_type, created_at, updated_at)
   VALUES ('TEST', 'KXHIGHTEST', 'KXHIGHTEST-26AUG01', '2026-08-01', 85.0, 3, 3, 'historical_api', '2026-08-02 04:20:00', NULL);
   ```
3. The `VACUUM` is optional — row deletion without vacuum is recoverable.

## Out of Scope

- Changing the schema of `kalshi_settlements.db` — just deleting a row.
- Adding indexes or constraints to the DB files.
- Modifying any other data files in `data/`.
- Adding `last_discovered` to the station registry's cache writing logic (that's Spec 2's responsibility — this spec only updates the file header).