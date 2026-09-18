<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Data Retention Policy

This document outlines the data retention policies implemented in the weather engine system to ensure compliance with legal requirements and optimize storage usage.

## Policy Overview

| Store | Max Age | Rationale |
|-------|---------|-----------|
| kalshi_rate_limit | 90 days | Rate limit patterns beyond 90 days are noise |
| signal_layer_state | 180 days | Signal state older than 6mo is irrelevant |
| market_cache | 7 days | Market data is transient |
| kalshi_settlements.db | 365 days | 1 year of settlement records |
| orderbook_snapshots/ | 30 days | Snapshots older than 1mo are stale |
| kalshi_backfill_*.json | 365 days | Historical backfill kept 1 year |

## Implementation Details

### Database Tables

#### kalshi_rate_limit
- **Table**: `kalshi_rate_limit` in alerts.db
- **Columns**: `created_at` (timestamp when record was created)
- **Retention**: 90 days
- **Purge Method**: DELETE records where `datetime(created_at) < datetime('now', '-90 days')`

#### signal_layer_state
- **Table**: `signal_layer_state` in alerts.db
- **Columns**: `updated_at` (timestamp when record was last updated), `created_at` (timestamp when record was created)
- **Retention**: 180 days
- **Purge Method**: DELETE records where `datetime(updated_at) < datetime('now', '-180 days')`

#### market_cache
- **Table**: `market_cache` in alerts.db
- **Columns**: `cached_at` (timestamp when record was cached), `created_at` (timestamp when record was created)
- **Retention**: 7 days
- **Purge Method**: DELETE records where `datetime(cached_at) < datetime('now', '-7 days')`

### File System Data

#### kalshi_settlements.db
- **Location**: `data/kalshi_settlements.db`
- **Retention**: 365 days
- **Purge Method**: DELETE records from tables with date columns where date < current_date - 365 days

#### orderbook_snapshots/
- **Location**: `data/orderbook_snapshots/`
- **File Pattern**: `*.json`
- **Retention**: 30 days
- **Purge Method**: Delete JSON files with modification time older than 30 days

#### kalshi_backfill_*.json
- **Location**: `data/`
- **File Pattern**: `kalshi_backfill_*.json`
- **Retention**: 365 days
- **Purge Method**: Delete JSON files with modification time older than 365 days

## Purge Function

The `purge_old_data()` function implements the retention policy and can be called periodically to clean up old data:

```python
from core.kalshi_monitor import purge_old_data

# Purge old data according to retention policy
stats = purge_old_data()
print(f"Purged {stats['kalshi_rate_limit_deleted']} rate limit entries")
print(f"Purged {stats['signal_layer_state_deleted']} signal states")
print(f"Purged {stats['market_cache_deleted']} market cache entries")
print(f"Purged {stats['kalshi_settlements_deleted']} settlement records")
print(f"Purged {stats['orderbook_snapshots_deleted']} snapshot files")
print(f"Purged {stats['kalshi_backfill_deleted']} backfill files")
```

## Schedule

Data purging should be performed regularly (e.g., daily) to prevent excessive accumulation of old data. A cron job or scheduled task should call the `purge_old_data()` function.

## Compliance Notes

- This retention policy ensures that only necessary data is retained
- Old data that is no longer relevant for operational purposes is removed
- Legal and regulatory requirements are considered in the retention periods
- Storage efficiency is maintained while preserving important historical data