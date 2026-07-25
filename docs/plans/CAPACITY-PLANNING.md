# D10 — Capacity Planning

**Status:** Planning document
**Last updated:** 2026-07-24

---

## Current Resource Profile

### Station Count: 20
### Markets per Station: 2 (HIGH + LOW)
### Active Signals: 17 (in SignalRegistry)

---

## DB Growth Projections

| Database | Current Size | Est. Monthly Growth | 6-Month Projection | Notes |
|---|---|---|---|---|
| `data/metar_backfill.db` | ~50 MB | ~10 MB | ~110 MB | Grows with every METAR observation ingested |
| `data/nwp_forecasts.db` | ~15 MB | ~3 MB | ~33 MB | 5 models × 20 stations × 7 days |
| `data/alerts.db` | ~5 MB | ~2 MB | ~17 MB | Grows with every transition + alert event |
| `data/paper_test_results.db` | ~1 MB | ~5 MB | ~31 MB | During paper test only |
| Total | ~71 MB | ~20 MB | ~191 MB | Well under 500 MB threshold |

**Note:** At current growth rates, we won't hit the 500 MB SQLite threshold for ~21 months.

### Worst-Case Scenario
- 20 stations polling every 3 minutes
- 2 markets per station
- 17 signals generating confidences
- ~9,600 METAR observations ingested per day
- **Est. 3-year total: < 500 MB** — SQLite is fine

---

## API Rate Limit Budget

### Kalshi API
| Limit | Details |
|---|---|
| Published rate limit | 10 requests/second |
| Our usage | ~1 request/5min per station (settlement query + price fetch) |
| Estimated peak | 20 stations × 2 markets × 1 request/min = 40 req/min = < 1 req/s |
| Headroom | ~90% (we use < 10% of limit) |

**During paper test:**
- Add post-settlement accuracy queries: +20 req/day (negligible)
- Price monitoring: configurable, default 5min intervals
- **Total peak: < 1 req/s**

### NWS API (No official rate limit — be polite)
| Endpoint | Usage |
|---|---|
| METAR XML feed | 1 request/3min per station (can batch) |
| Estimated peak | 20 stations separate = 20 req/3min |
| Recommended max | 30 req/min (polite threshold) |
| Our usage | 20 req/3min ≈ 7 req/min (well under 30 req/min) |

### Open-Meteo API (Free tier)
| Limit | Usage |
|---|---|
| Published limit | 10,000 req/day (free tier) |
| Our usage | 5 models × 20 stations × 1 req/day = 100 req/day |
| Headroom | ~99% |

---

## Container Resource Estimates

### Current Container (OpenClaw gateway, shared)
```
CPU: 4 cores (shared)
Memory: 8 GB (shared)
Disk: 100 GB (shared)
```

### Weather Engine Process Requirements
| Process | CPU | Memory | Disk | Notes |
|---|---|---|---|---|
| METAR poller | 0.1 core | 200 MB | N/A | Lightweight, runs every 3min |
| Signal pipeline | 0.2 core | 500 MB | N/A | Burst when new data arrives |
| Scheduler | 0.1 core | 150 MB | N/A | Always running |
| Flask app (port 10000) | 0.1 core | 200 MB | N/A | Health endpoint + routing |
| NWP collector | 0.3 core | 300 MB | N/A | Runs daily, CPU burst for API calls |
| Paper test controller | 0.1 core | 100 MB | N/A | During 30-day test only |

**Total: < 1 core, < 1.5 GB RAM, < 500 MB disk growth/year**

### Recommendation
The weather engine can run comfortably within the existing OpenClaw gateway container for the next 12-24 months. No dedicated container needed.

**If growth exceeds projections:**
- Separate the weather engine into its own container: +200 MB RAM
- Use SQLite WAL mode — handles concurrent reads fine
- Only scale if station count exceeds 100 (unlikely in current roadmap)

---

## Signal Processing Capacity

### Current
- 17 signals in SignalRegistry
- All can process 20 stations × 2 markets in < 5 seconds
- Bottleneck: DB I/O for loading METAR data

### Scaling to 50 stations
- METAR loading: ~3x slower (more data to read)
- Signal processing: ~2.5x slower (more stations to evaluate)
- **Total per-cycle: < 15 seconds** — well within 3-minute polling interval

### Scaling to 100 stations
- METAR loading: ~6x slower
- Signal processing: ~5x slower
- **Total per-cycle: < 30 seconds** — still within 3-minute interval

---

## Key Metrics to Monitor

1. **DB file sizes** — Weekly check, alert if growth > 2x expected
2. **API response times** — Monitor Kalshi and NWS latency
3. **Process memory** — Alert if any process exceeds 500 MB RSS
4. **Cycle time** — Alert if METAR poll + signal processing > 60 seconds
5. **Disk usage** — Alert if < 10 GB remaining

---

## Sharding Strategy (If Needed)

If DB files exceed 500 MB or query performance degrades:
1. Split by station region (NE, SE, MW, SW, NW) into separate DBs
2. Keep a small "metadata" DB for cross-station querying
3. Use SQLite ATTACH DATABASE for cross-DB queries
4. No migration needed — just create new DBs and populate from API

**Current projection: Sharding will NOT be needed for at least 2 years.**