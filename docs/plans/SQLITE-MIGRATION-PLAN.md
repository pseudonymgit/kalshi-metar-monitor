# D8 — SQLite Migration Plan

**Status:** Contingency plan — do NOT migrate until threshold is reached
**Threshold:** Paper test DB exceeds 500 MB
**Current estimate:** ~71 MB — well under threshold. Migration not needed for ~2 years.

---

## When to Migrate

Trigger migration if:
1. Any single SQLite DB exceeds **500 MB**
2. Query performance degrades (e.g., `PRAGMA integrity_check` takes > 30 seconds)
3. Concurrent write contention despite WAL mode (rare — only if > 10 concurrent writers)
4. Paper test results exceed 100,000 rows in a single table

---

## Option A: PostgreSQL (Recommended)

### Why
- Proper concurrent write support
- No 500 MB single-file limit
- Better query performance for complex joins
- Battle-tested with 20+ concurrent connections

### Schema

```sql
-- Stations
CREATE TABLE stations (
    code TEXT PRIMARY KEY,
    city_name TEXT,
    state TEXT,
    lat REAL,
    lon REAL
);

-- METAR observations
CREATE TABLE metar_observations (
    id BIGSERIAL PRIMARY KEY,
    station TEXT REFERENCES stations(code),
    timestamp_utc TIMESTAMPTZ NOT NULL,
    temp_f REAL,
    temp_c REAL,
    dewpoint_f REAL,
    wind_direction_deg INTEGER,
    wind_speed_kt REAL,
    pressure_mb REAL,
    raw_metar TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Partition by station, then by month
CREATE INDEX idx_metar_station_ts ON metar_observations(station, timestamp_utc DESC);

-- Trades
CREATE TABLE trades (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    station TEXT REFERENCES stations(code),
    market_type TEXT,
    direction TEXT,
    confidence REAL,
    signal_name TEXT,
    lane TEXT,
    is_shadow BOOLEAN DEFAULT FALSE,
    settled BOOLEAN DEFAULT FALSE,
    settlement_correct BOOLEAN,
    size REAL DEFAULT 0,
    pnl REAL DEFAULT 0
);

-- Settlement accuracy
CREATE TABLE settlement_accuracy (
    id BIGSERIAL PRIMARY KEY,
    epoch_id TEXT,
    station TEXT REFERENCES stations(code),
    date DATE,
    market_type TEXT,
    predicted_direction TEXT,
    predicted_confidence REAL,
    settlement_value REAL,
    prev_settlement_value REAL,
    actual_direction TEXT,
    was_correct BOOLEAN,
    signal_name TEXT,
    lane TEXT,
    settled_at TIMESTAMPTZ
);

-- Daily metrics
CREATE TABLE daily_metrics (
    date DATE PRIMARY KEY,
    phase TEXT,
    trades INTEGER,
    wins INTEGER,
    accuracy REAL,
    drawdown_pct REAL,
    cumulative_pnl REAL
);
```

### Migration Steps

1. **Export SQLite data:**
   ```bash
   sqlite3 data/metar_backfill.db ".dump" > metar_dump.sql
   ```

2. **Transform SQLite DDL to PostgreSQL:**
   - Replace `INTEGER PRIMARY KEY AUTOINCREMENT` with `BIGSERIAL PRIMARY KEY`
   - Replace `TEXT` with `VARCHAR` where appropriate
   - Replace `REAL` with `DOUBLE PRECISION` or `NUMERIC`
   - Add `REFERENCES` for foreign keys
   - Add `ON DELETE CASCADE` for cleanup
   - Add `CREATE INDEX` for query patterns

3. **Load into PostgreSQL:**
   ```bash
   createdb weather_engine
   psql weather_engine < transformed_schema.sql
   psql weather_engine < transformed_data.sql
   ```

4. **Update connection strings:**
   - Change `sqlite3.connect(...)` to `psycopg2.connect(...)`
   - Update `PRAGMA` statements to PostgreSQL equivalents
   - Replace `INSERT OR REPLACE` with `INSERT ... ON CONFLICT ... DO UPDATE`

5. **Update query patterns:**
   - Replace `LIKE` with `ILIKE` for case-insensitive search
   - Replace `GROUP BY date_utc` with `GROUP BY date_utc::date`
   - Replace `datetime()` with `NOW()` or `CURRENT_TIMESTAMP`

6. **Test with shadow traffic:**
   - Run both SQLite and PostgreSQL in parallel for 7 days
   - Compare results row-by-row
   - Verify no divergence

### Estimated Migration Time: 2-3 days

---

## Option B: Sharded SQLite (Fallback)

### Why
- Simpler — no new infrastructure
- Each shard is independent
- No new dependencies (no PostgreSQL server)

### Shard Strategy
- **By region:** NE (KNYC, KBOS, KPHL, KDCA), SE (KATL, KMIA, KTPA, KJAX), MW (KORD, KMDW, KMSP, KSTL, KMCI), SW (KDFW, KHOU, KAUS, KPHX, KDEN), NW (KSEA, KSFO, KLAX, KSLC)
- **By market type:** HIGH vs LOW (separate DBs)
- **By date:** Monthly shards (YYYY-MM.sqlite)

### SQLite Limitations
- No concurrent writes (WAL helps but doesn't solve it)
- 140TB theoretical max, but practical perf degrades > 1GB
- No built-in replication

---

## Option C: Stay on SQLite (Default — no migration)

SQLite is adequate for our projected scale:
- Current DB: ~71 MB
- 2-year projection: < 500 MB
- WAL mode handles concurrent readers
- Single-writer pattern avoids contention

**No migration needed unless we exceed 500 MB or see performance degradation.**

---

## Monitoring

### Migration Triggers
| Metric | Warning | Action |
|---|---|---|
| DB file size | > 300 MB | Plan migration |
| DB file size | > 500 MB | Trigger migration |
| Integrity check time | > 30s | Investigate indexing |
| Concurrent writers | > 5 | Consider WAL tuning |
| Query latency | > 500ms per trade | Profile and index |

### Migration Test
Before any production migration, run a 7-day shadow test:
1. Set up PostgreSQL instance
2. Run dual-write (SQLite + PostgreSQL) for 7 days
3. Compare accuracy and P&L results
4. If no divergence > 0.1%, migration is safe

---

## Decision Matrix

| Factor | SQLite (current) | PostgreSQL | Sharded SQLite |
|---|---|---|---|
| Setup complexity | None | Medium | Low |
| Operational overhead | None | Medium | Low |
| Concurrent write support | Limited | Excellent | Limited |
| Query performance | Good | Excellent | Good (per shard) |
| Max DB size (practical) | ~1 GB | Unlimited | Depends on shard count |
| Migration effort | — | 2-3 days | 1 day |
| Cost | $0 | ~$15/mo (small instance) | $0 |

**Recommendation:** Stay on SQLite. Only migrate to PostgreSQL if DB exceeds 500 MB or query performance degrades.