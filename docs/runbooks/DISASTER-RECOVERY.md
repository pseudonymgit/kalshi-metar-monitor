# D7 — Disaster Recovery Runbook

**Status:** Planning document — not yet operationalized
**Last updated:** 2026-07-24

---

## 1. Pod/Container Crash

**Symptoms:** Flask app on port 10000 unreachable, scheduler not running, health checks return 5xx.

**Procedure:**
1. Check container status: `docker ps | grep weather`
2. Restart container: `docker compose restart` (from deploy directory)
3. Verify recovery: `curl http://127.0.0.1:10000/health`
4. If restart fails: check logs `docker logs <container> --tail 50`
5. If container won't start: check disk space, port conflicts, config changes
6. Escalation: If unavailable > 5min, alert Dan via cron

**Prevention:** `scripts/daily_ops_check.py` monitors port 10000 health. Halt file mechanism (`scripts/halt_check.py`) prevents trading during degraded state.

---

## 2. SQLite DB Corruption

**Symptoms:** `PRAGMA integrity_check` fails, trades or observations cannot be read/written, scheduler errors with "database disk image is malformed".

**Procedure:**
1. Stop all processes touching the affected DB
2. Identify corrupted DB(s): `python3 -c "import sqlite3; c=sqlite3.connect('path/to/db'); c.execute('PRAGMA integrity_check')"`
3. Restore from latest backup in `/srv/backups/openclaw-next/`
4. Replay any missing data from NWS API or Kalshi API
5. Run health check to verify: `python3 scripts/daily_ops_check.py`
6. If no backup: Use `.clone` or `.backup` to salvage what's readable, then rebuild from API sources

**Prevention:** WAL mode on ALL SQLite connections, `PRAGMA busy_timeout=5000`, daily integrity check cron.

---

## 3. Stale Data Ingestion

**Symptoms:** data_freshness_monitor reports stale observations, ingestion parity > 2x median, station freshness lag > 30min.

**Procedure:**
1. Check NWS API availability: `curl https://www.weather.gov/documentation/services-web-api`
2. If NWS is down: wait (NWS API is highly available, short outages are normal)
3. If NWS is up but ingestion is stalled: check scheduler is running, check metar_monitor.py logs
4. Force re-poll: trigger the ingestion cron cycle manually
5. If specific station(s) are stale: verify station code is in ACTIVE_STATIONS, check for rate limiting

**Escalation:** If > 50% of stations are stale for > 1 hour, alert Dan.

---

## 4. Kalshi API Outage

**Symptoms:** Kalshi price fetcher errors, market discovery returns empty, trades cannot be placed.

**Procedure:**
1. Verify Kalshi API status: `curl https://api.elections.kalshi.com/trade-api/v2/`
2. If API is down: system enters DEGRADED mode — signals still computed but trades not executed
3. If API is up but authentication fails: check `KALSHI_KEY_ID` and `KALSHI_PrivateKey` env vars
4. Re-auth: re-run Kalshi auth flow if credentials expired
5. Monitor: check `daily_ops_check.py` for Kalshi-specific metrics

**Prevention:** API key rotation schedule, env var validation on startup.

---

## 5. NWP Collection Failure

**Symptoms:** NWP forecasts missing or stale, nwp_forecasts.db not updated, cron for nwp_collect.py errors.

**Procedure:**
1. Check Open-Meteo API status: `curl https://api.open-meteo.com/v1/forecast`
2. If API is up: run nwp_collect.py manually for the affected models/stations
3. If API is down: continue with METAR-only trading (directional signals don't depend on NWP)
4. Check NWP DB health: `sqlite3 data/nwp_forecasts.db "PRAGMA integrity_check"`
5. If NWP collection has been down > 24h, backfill from Open-Meteo historical API

**Prevention:** Daily NWP collection cron at 06:00 UTC. NWP is non-critical — system degrades gracefully.

---

## 6. Alert Flood

**Symptoms:** > 200 actionable alerts/day, alert pipeline overwhelmed, noise drowning out real signals.

**Procedure:**
1. Identify the source: check per-station, per-lane alert counts
2. If a single station is flooding: place that station in cooldown or remove from ACTIVE_STATIONS
3. If all stations are flooding: check for systemic issue (data quality, config error)
4. Increase cooldown thresholds temporarily to reduce volume
5. Investigate root cause during next maintenance window

**Prevention:** Per-station and per-boundary cooldowns (300s/900s), halt file mechanism, ≤ 50 actionable alerts/day target.

---

## 7. Runaway Position Sizing

**Symptoms:** Position sizes exceeding configured limits, Kelly fraction > 0.5, rapid balance changes.

**Procedure:**
1. **IMMEDIATE:** Trigger halt file: `touch data/halt_trading`
2. Verify halt: check `/health` endpoint shows HALTED state
3. Audit recent trades: check trade journal for pattern
4. Identify cause: bad calibration, fee rate misconfiguration, balance calculation error
5. Fix and test in isolated session before resuming
6. Remove halt file: `rm data/halt_trading`
7. Resume with reduced sizing for 48h

**Prevention:** KellyPositionSizer with fractional Kelly (0.5), max_position_fraction (0.25), loss limiter scale-down on consecutive losses.

---

## 8. Config Drift

**Symptoms:** Unexplained behavior changes, accuracy shifts, different signal behavior.

**Procedure:**
1. Check environment variables: compare `.env` against source of truth
2. Check config files: `diff config/ production_gate.json`
3. Check git: `git diff` — any uncommitted changes to Python files?
4. If drift found: restore from git or backup
5. Log the drift event for audit

**Prevention:** Git-tracked config, daily config drift check in monitoring dashboard.

---

## Recovery Validation

After any disaster recovery event:
1. Run all 4 health checks (ingestion, hydration, evaluation, scheduler)
2. Run `daily_ops_check.py --verbose`
3. Verify next cron cycle completes without error
4. Monitor for 1 hour at 5-min polling
5. Document the event in `data/incident_log.md`