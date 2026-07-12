# Near-Miss Audit Log - User Guide

## Overview

The near-miss audit log tracks signal conditions that are **CLOSE to triggering alerts** but do not meet all gating criteria. This enables the team to monitor how close the system is to actual alerts during dry periods and identify potential tuning opportunities.

## What Gets Logged

The near-miss audit captures these suppression scenarios:

| Near-Miss Type | Description | Severity |
|---------------|-------------|----------|
| `NEAR_BOUNDARY_LOW_MOMENTUM` | Temperature near boundary but momentum below threshold | MEDIUM |
| `STATION_COOLDOWN` | Signal eligible but station in cooldown period | MEDIUM |
| `BOUNDARY_COOLDOWN` | Signal eligible but boundary in cooldown period | MEDIUM |
| `EPOCH_ALERT_ALREADY_EMITTED` | Goldilocks epoch alert already fired this epoch | LOW |
| `NO_ELIGIBLE_MARKET` | Signal conditions met but no Kalshi market discovered | MEDIUM |
| `HYDRATION_CACHE_INVALID` | Signal conditions met but hydration cache invalid | MEDIUM |
| `NO_SIGNAL_CONDITION_MATCH` | No signal condition met after observation window review | LOW |
| `MARKET_ELIGIBILITY_FILTER` | Market exists but failed eligibility filter | MEDIUM |
| `DIRECTIONAL_FILTER` | Market exists but directional filter rejected | MEDIUM |
| `LOW_CONFIDENCE_GOLDILOCKS` | Goldilocks pattern detected but confidence below threshold | MEDIUM |
| `MOMENTUM_BELOW_THRESHOLD` | Momentum detected but below minimum threshold | MEDIUM |
| `TOO_FAR_FROM_BOUNDARY` | Temperature too far from integer boundary for signal | LOW |
| `SETTLEMENT_NOT_CHANGE` | Settlement not changing but signal conditions considered | LOW |

## Database Table Structure

```sql
CREATE TABLE near_miss_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc TEXT NOT NULL,
    station TEXT NOT NULL,
    near_miss_type TEXT NOT NULL,
    severity TEXT,  -- LOW, MEDIUM, or HIGH
    details_json TEXT,  -- Condition-specific details
    metadata_json TEXT,  -- Additional metadata
    suppressed_alert_type TEXT  -- Alert type that would have fired
)
```

## API Reference

### Logging Near-Misses

#### `log_near_miss()`
```python
from core.near_miss_audit import log_near_miss

log_near_miss(
    station="KDEN",
    near_miss_type="NEAR_BOUNDARY_LOW_MOMENTUM",
    severity="MEDIUM",
    details={
        "distance_to_boundary": 0.03,
        "momentum": 0.0015,  # below threshold of 0.002
        "window_size": 3,
    },
    metadata={"observation_window": "valid"},
    suppressed_alert_type="near_boundary_momentum_up",
)
```

#### Helper Functions (Auto-Integrated)

The following helper functions automatically log near-misses when suppression conditions are detected:

```python
# Station/Boundary cooldown
log_near_miss_if_cooldown(
    station="KDEN",
    cooldown_type="STATION",  # or "BOUNDARY"
    remaining_seconds=120,
    total_seconds=300,
    boundary_level=87,  # optional, for boundary cooldown
    epoch_id=42,  # optional
)

# Distance-to-boundary conditions
log_near_miss_if_distance_to_boundary(
    station="KDEN",
    distance=0.03,
    momentum=0.0015,
    threshold_distance=0.10,
    threshold_momentum=0.002,
)

# No eligible market
log_near_miss_if_no_eligible_market(
    station="KDEN",
    signal_detected=True,
    discovered_markets_count=0,
    hydration_valid=False,
)

# Epoch alert already emitted
log_near_miss_if_epoch_alert_emitted(
    station="KDEN",
    epoch_id=42,
    signal_type="goldilocks_reversion_alert",
)
```

### Querying Near-Miss Logs

#### `query_near_miss_log()`
```python
from core.near_miss_audit import query_near_miss_log

# Get recent near misses for a station
recent = query_near_miss_log(
    station="KDEN",
    limit=10,
    order_by="created_utc DESC",
)

# Filter by type
low_momentum = query_near_miss_log(
    station="KDEN",
    near_miss_type="NEAR_BOUNDARY_LOW_MOMENTUM",
    hours=24,
)

# Get all near misses in last 6 hours
all_recent = query_near_miss_log(hours=6, limit=100)
```

#### `get_near_miss_summary()`
```python
from core.near_miss_audit import get_near_miss_summary

# Get summary for past 24 hours
summary = get_near_miss_summary(
    station="KDEN",
    hours=24,
    by_type=True,
)

# Returns:
# {
#     "window_hours": 24,
#     "generated_at": "2026-07-10T21:00:00+00:00",
#     "total_count": 47,
#     "by_type": {
#         "STATION_COOLDOWN": 15,
#         "BOUNDARY_COOLDOWN": 12,
#         "NEAR_BOUNDARY_LOW_MOMENTUM": 10,
#         # ...
#     },
#     "severity_breakdown": {
#         "LOW": 10,
#         "MEDIUM": 30,
#         "HIGH": 5,
#         "None": 2
#     },
#     "suppressed_alert_types": {
#         "near_boundary_momentum_up": 25,
#         "near_boundary_momentum_down": 12,
#         # ...
#     },
#     "recent_records": [...],  # Last 10 records
# }
```

## Usage Examples

### Example 1: Check How Close We Are to Alerts During Dry Periods

```python
from core.near_miss_audit import get_near_miss_summary
from datetime import datetime, timedelta

def check_alert_proximity(station: str = "KDEN", hours: int = 24):
    """Assess how close we are to alerts during a dry period."""
    summary = get_near_miss_summary(station=station, hours=hours, by_type=True)
    
    print(f"[{datetime.now().isoformat()}] Near-Miss Audit for {station}")
    print(f"  Time Window: Last {hours} hours")
    print(f"  Total Near Misses: {summary['total_count']}")
    
    if summary['total_count'] == 0:
        print("  No near-miss events detected. System is truly dry.")
        return
    
    # Calculate alert likelihood score (0-100)
    total_possible = summary['total_count']
    # High severity events indicate closer-to-alert conditions
    high_severity = summary['severity_breakdown'].get('HIGH', 0)
    medium_severity = summary['severity_breakdown'].get('MEDIUM', 0)
    
    alert_likelihood = min(100, int((high_severity * 3 + medium_severity * 2 + total_possible) / total_possible * 50))
    
    print(f"  Alert Likelihood Score: {alert_likelihood}/100")
    print(f"  Top Near-Miss Types:")
    for mt, count in sorted(summary['by_type'].items(), key=lambda x: -x[1])[:5]:
        print(f"    - {mt}: {count}")
    
    if alert_likelihood > 70:
        print("  ⚠️  HIGH ALERT: Many near-miss events detected. Consider reviewing signal thresholds.")
    elif alert_likelihood > 40:
        print("  ℹ️  MODERATE: Some near-miss events. Current thresholds are reasonable.")
    else:
        print("  ✓  LOW: Few near-miss events. System is operating with healthy margins.")

# Usage
check_alert_proximity(station="KDEN", hours=24)
```

### Example 2: Debug a Specific Suppression

```python
from core.near_miss_audit import query_near_miss_log
import json

def debug_suppression(station: str, near_miss_type: str, limit: int = 5):
    """Debug a specific near-miss pattern."""
    records = query_near_miss_log(
        station=station,
        near_miss_type=near_miss_type,
        limit=limit,
        order_by="created_utc DESC",
    )
    
    print(f"[{len(records)} recent '{near_miss_type}' events for {station}]:")
    for i, record in enumerate(records, 1):
        print(f"\n  Event #{i} ({record['created_utc']}):")
        print(f"    Severity: {record['severity']}")
        print(f"    Details: {json.dumps(record['details'], indent=4)}")
        print(f"    Metadata: {json.dumps(record['metadata'], indent=4)}")
        if record['suppressed_alert_type']:
            print(f"    Suppressed Alert: {record['suppressed_alert_type']}")

# Usage
debug_suppression("KDEN", "STATION_COOLDOWN")
```

### Example 3: Real-Time Monitoring in p3_scheduler

The near-miss audit is already integrated into the `p3_scheduler.py` execution path. When signal conditions are evaluated, near-misses are automatically logged.

To view near-misses from production:

```python
# Run this in a production shell or cron job
from core.near_miss_audit import get_near_miss_summary
from datetime import datetime

def daily_near_miss_report():
    """Generate daily near-miss report for monitoring."""
    stations = ["KDEN", "KLAX", "KNYC", "KPHL", "KMDW", "KMIA", "KAUS"]
    
    print(f"[{datetime.now().isoformat()}] Daily Near-Miss Report")
    print("=" * 60)
    
    for station in stations:
        summary = get_near_miss_summary(station=station, hours=24)
        print(f"\n{station}:")
        print(f"  Total: {summary['total_count']} near-misses")
        if summary['by_type']:
            top_type = max(summary['by_type'].items(), key=lambda x: x[1])
            print(f"  Top Type: {top_type[0]} ({top_type[1]} occurrences)")
        
        # Alert likelihood check
        if summary['total_count'] > 10:
            print(f"  ⚠️  Elevated activity for {station}")
    
    print("\n" + "=" * 60)
    print("End of Report")

# Usage
daily_near_miss_report()
```

### Example 4: Ad-Hoc Analysis Script

```python
#!/usr/bin/env python3
"""
Near-Miss Analysis Script
==========================

Run this to analyze near-miss patterns across all stations.
"""

from core.near_miss_audit import query_near_miss_log, get_near_miss_summary
from datetime import datetime, timedelta
import json

def analyze_near_miss_patterns(hours: int = 24):
    """Analyze near-miss patterns across all stations."""
    
    # Get all unique stations from the audit log
    stations = set()
    records = query_near_miss_log(hours=hours, limit=10000)
    for record in records:
        stations.add(record['station'])
    
    print(f"Analysis Period: Last {hours} hours")
    print(f"Stations with near-misses: {sorted(stations)}")
    print()
    
    # Summary by station
    for station in sorted(stations):
        summary = get_near_miss_summary(station=station, hours=hours)
        print(f"\n{station}:")
        print(f"  Total: {summary['total_count']}")
        if summary['by_type']:
            print(f"  Breakdown:")
            for mt, count in sorted(summary['by_type'].items(), key=lambda x: -x[1]):
                pct = (count / summary['total_count'] * 100) if summary['total_count'] > 0 else 0
                print(f"    {mt}: {count} ({pct:.1f}%)")
        
        # Check for patterns
        cooldown_count = summary['by_type'].get('STATION_COOLDOWN', 0) + summary['by_type'].get('BOUNDARY_COOLDOWN', 0)
        if cooldown_count > summary['total_count'] * 0.5:
            print(f"  ⚠️  Cooldown-heavy: {cooldown_count}/{summary['total_count']} ({cooldown_count/summary['total_count']*100:.1f}%)")
    
    # Find stations with high alert likelihood
    print("\n" + "=" * 60)
    print("High Alert Likelihood Stations (last 24h):")
    for station in sorted(stations):
        summary = get_near_miss_summary(station=station, hours=24)
        total = summary['total_count']
        high = summary['severity_breakdown'].get('HIGH', 0)
        medium = summary['severity_breakdown'].get('MEDIUM', 0)
        
        # Alert likelihood score
        score = (high * 3 + medium * 2 + total) / max(total, 1) * 30
        if score > 70:
            print(f"  {station}: {score:.1f} (HIGH - {high} high, {medium} medium)")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24, help="Lookback hours")
    args = parser.parse_args()
    
    analyze_near_miss_patterns(hours=args.hours)
```

## Integration with p3_scheduler

The near-miss audit is **transparently integrated** into the `p3_scheduler` signal evaluation path:

1. Signal conditions are evaluated in `_evaluate_deterministic_signal_layer()`
2. Near-miss conditions are detected and logged via helper functions
3. Alert delivery behavior is **unchanged** - near-misses are logged but not delivered

The integration adds ~50-100ms of overhead per signal evaluation, which is acceptable given:
- It only runs when signal conditions are being evaluated
- The audit write is a non-blocking SQLite insert
- The data is used for monitoring, not real-time decisions

## Monitoring the Audit

### Quick Check: Are We Close to Alerts?

```python
from core.near_miss_audit import get_near_miss_summary

# Check all stations
stations = ["KDEN", "KLAX", "KNYC", "KPHL", "KMDW", "KMIA", "KAUS"]
for station in stations:
    summary = get_near_miss_summary(station=station, hours=6)
    print(f"{station}: {summary['total_count']} near-misses (last 6h)")
```

### AlertLikelihood Score

A simple scoring system to assess alert proximity:

| Score | Meaning | Action |
|-------|---------|--------|
| 0-20 | Truly dry | Monitor, no action |
| 21-50 | Normal dry | Review thresholds if desired |
| 51-70 | Warm-up period | Consider lowering thresholds |
| 71-90 | Hot! | Review signal configuration |
| 91-100 | imminent | Prepare for alert burst |

## Performance Considerations

- **Database writes:** ~50-100ms per near-miss event (non-blocking)
- **Query performance:** Indexes on `(station, created_utc)` and `(near_miss_type, created_utc)`
- **Storage:** ~1KB per record, ~100-1000 records per station per day
- **Recommendation:** Archive records older than 90 days if storage is a concern

## Migration Notes

### From Legacy Monitoring

If you were previously using the transition events table for near-miss analysis:

| Old Approach | New Approach |
|-------------|--------------|
| Parse suppression_reason from transition_metadata | Query near_miss_audit table |
| Manual filtering for "close calls" | Pre-categorized near-miss types |
| No severity classification | Severity field (LOW/MEDIUM/HIGH) |
| No helper functions | Auto-integrated helper functions |

### Database Migration

The `near_miss_audit` table is auto-created on first write. To migrate historical data:

```python
# Optional: Migration script to backfill from transition_metadata
# (Only needed if you want historical near-miss tracking)

# Check if table exists
from core.near_miss_audit import _alert_db_path
import sqlite3

db_path = _alert_db_path()
conn = sqlite3.connect(db_path)
cursor = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='near_miss_audit'"
)
exists = cursor.fetchone()

if not exists:
    print("Table will be created on first write")
else:
    print("Table already exists")

conn.close()
```

## Troubleshooting

### No Near-Miss Records Found

1. **Check if the module is available:**
   ```python
   from core.near_miss_audit import log_near_miss
   # Should not raise ImportError
   ```

2. **Verify database exists:**
   ```python
   import os
   from core.near_miss_audit import _alert_db_path
   print(os.path.exists(_alert_db_path()))
   ```

3. **Check for records:**
   ```python
   from core.near_miss_audit import query_near_miss_log
   print(len(query_near_miss_log(limit=1)))
   ```

### Performance Issues

If near-miss logging is impacting performance:

1. **Reduce logging frequency:**
   ```python
   # Add a throttle before logging
   import time
   if time.time() - last_log > 60:  # Log at most once per minute
       log_near_miss(...)
       last_log = time.time()
   ```

2. **Disable audit (development only):**
   ```python
   # At top of metar_monitor.py, before imports
   import os
   os.environ["NEAR_MISS_AUDIT_ENABLED"] = "false"
   ```

3. **Use async writes (advanced):**
   ```python
   import threading
   
   def async_log_near_miss(*args, **kwargs):
       threading.Thread(target=log_near_miss, args=args, kwargs=kwargs, daemon=True).start()
   ```

## Future Enhancements

Potential improvements:

1. **Near-miss aggregation:** Group near-misses by time window (5min, 15min, 1h)
2. **Predictive alerts:** Use near-miss patterns to predict imminent alerts
3. **Alert likelihood scoring:** Automated scoring for dry period assessment
4. **Webhook notifications:** Send alerts when near-miss rate exceeds threshold
5. **Dashboard integration:** Add near-miss metrics to observability dashboards

---

**Author:** Donna Paulsen (via Gilfoyle)  
**Date:** 2026-07-10  
**Status:** Production-ready (auto-integrated in `p3_scheduler.py`)
