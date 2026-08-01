# Runbooks — Abort Conditions

**Location:** `docs/runbooks/`
**Purpose:** One page per abort condition: cause, diagnosis, fix, resume.
**Author:** Gilfoyle
**Date:** 2026-08-01

---

## Runbook: Disk < 10% Free

**Trigger:** `core/disk_monitor.py` raises `RuntimeError("pre_trade_disk_check")`

### Cause
Log files, DB WAL files, or GRIB data filling the disk.

### Diagnosis
```bash
df -h /home/node/.openclaw/workspace/prototypes/weather-engine-source/data
du -sh /home/node/.openclaw/workspace/prototypes/weather-engine-source/data/*.db-wal
du -sh /home/node/.openclaw/workspace/prototypes/weather-engine-source/data/tigge_grib/
du -sh /home/node/.openclaw/workspace/prototypes/weather-engine-source/logs/
```

### Fix
1. Checkpoint WAL files: `from core.db_connection import force_checkpoint; force_checkpoint('metar_backfill')`
2. Remove old GRIB files: `rm data/tigge_grib/*.grib` (ECMWF backfill artifacts)
3. Truncate logs: `> logs/forecast_disagreement_collector.log`
4. If urgent: remove old sweep checkpoint files in `data/`

### Resume
Call `pre_trade_disk_check()` again. Must pass before trading resumes.

---

## Runbook: Cron Overlap Detected

**Trigger:** `core/lock_file.py` returns `acquired=False` — previous cron still running

### Cause
Previous run hasn't completed (stuck on API call, DB contention, or infinite loop)

### Diagnosis
```bash
cat /tmp/weather_engine_locks/{name}.lock    # Shows PID and timestamp
ps aux | grep $(cat /tmp/weather_engine_locks/{name}.lock | cut -d: -f1)
```

### Fix
1. Check if process is legitimately still running (may be a long sweep)
2. If hung: `kill <PID>`, then `rm /tmp/weather_engine_locks/{name}.lock`
3. If process stuck on DB: `sqlite3 data/metar_backfill.db "PRAGMA wal_checkpoint(TRUNCATE);"`

### Resume
Lock auto-clears on process exit. For manual cleanup, remove the lock file and re-run.

---

## Runbook: Kalshi API Circuit Open

**Trigger:** `core/api_circuit_breaker.py` transitions to OPEN state (3+ consecutive failures)

### Cause
Kalshi API down, rate limited (429), or credentials expired.

### Diagnosis
```bash
python3 -c "from core.kalshi_price_fetcher import get_live_market_price; print(get_live_market_price('KDEN'))"
```

### Fix
1. If rate limited: wait 60s (circuit auto-recovers with exponential backoff)
2. If API down: no action needed — circuit uses fallback prices (0.5)
3. If credentials expired: check `KALSHI_PUBLIC_BASE_URL` env var
4. To force reset: `python3 -c "from core.api_circuit_breaker import get_circuit; get_circuit('kalshi','market_price').force_state('CLOSED')"`

### Resume
Circuit auto-transitions to HALF_OPEN after timeout. Success on HALF_OPEN returns to CLOSED.

---

## Runbook: Alert Pipeline Env Var Missing

**Trigger:** `core/delivery_router.py` logs "No ALERT_WEBHOOK_URL found"

### Cause
Neither `ALERT_WEBHOOK_URL`, `DISCORD_WEBHOOK`, nor `DISCORD_WEBHOOK_PROD` is set.

### Diagnosis
```bash
echo $ALERT_WEBHOOK_URL
echo $DISCORD_WEBHOOK_PROD
env | grep -i webhook
```

### Fix
1. Set the environment variable: `export DISCORD_WEBHOOK_PROD=https://discord.com/api/webhooks/...`
2. Restart the process

### Resume
Alerts will begin routing once the webhook URL is configured.

---

## Runbook: Settlement Price Not Available

**Trigger:** `_get_strike_price()` returns None — trade settlement skipped

### Cause
Kalshi API unreachable AND no market_cache entry for this station

### Diagnosis
```bash
python3 -c "from core.kalshi_price_fetcher import get_live_market_price; print(get_live_market_price('KMSP'))"
```

### Fix
1. If API is down: wait for recovery
2. If station not mapped: `STATION_TO_KALSHI_CODE` in `kalshi_price_fetcher.py` may need update
3. Trade is left OPEN for manual resolution — no automatic fix

### Resume
Re-run trade settlement once API recovers or station mapping is fixed.

---

## Runbook: ECMWF Backfill Failure

**Trigger:** PID 829549 exits with error code

### Cause
CDS API timeout, authentication, or data request limit.

### Diagnosis
```bash
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
python3 scripts/sweep/check_backfill.py  # if exists
ls -la data/tigge_archive.db            # check file size
```

### Fix
1. Check CDS API quota: https://cds.climate.copernicus.eu
2. Restart backfill: `python3 scripts/ecmwf_backfill.py --resume`
3. If quota exhausted: wait 24h

### Resume
Backfill auto-resumes from last checkpoint if `--resume` flag is passed.

---

## Runbook: Paper Trading DB FULL_SYNC

**Trigger:** SQLite FULL synchronous mode slows writes

### Cause
`paper_trading_dev/live/prod/sbox.db` use `PRAGMA synchronous=FULL` for durability.

### Diagnosis
```bash
sqlite3 data/paper_trading_dev.db "PRAGMA synchronous;"
```

### Fix
This is intentional — FULL sync prevents data loss on power loss. If performance is an issue:
1. Switch to NORMAL: `PRAGMA synchronous=NORMAL;`
2. This trades durability for speed — only do during non-critical periods

### Resume
No action needed. System runs correctly under FULL sync; it's just slower.

---

## Runbook: Database Connection Pool Exhaustion

**Trigger:** `sqlite3.OperationalError: database is locked`

### Cause
Two processes hitting the same DB simultaneously, or a long-running transaction.

### Diagnosis
```bash
ls -la data/*.db-wal          # Check WAL file size (large = uncheckpointed)
fuser data/metar_backfill.db  # Check which PIDs hold the file
```

### Fix
1. Wait: `busy_timeout` handles retries up to configured timeout
2. If persistent: kill the conflicting process
3. WAL checkpoint: `python3 -c "from core.db_connection import force_checkpoint; force_checkpoint('metar_backfill')"`

### Resume
System auto-recovers once the conflicting process releases the lock.