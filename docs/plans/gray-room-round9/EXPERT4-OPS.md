# Gray Room Round 9 — Expert 4: Production Operations & DevOps

**Domain:** Deployment, Monitoring, Alerting, Configuration Management, Infrastructure  
**Codebase:** `prototypes/weather-engine-source`  
**Platform:** Render (auto-deploy) + local systemd services  
**Date:** 2026-07-22

---

## ERRORS (15 total)

### E1 — Webhook Env Var Name Mismatch: `alert_state_machine.py` DeliveryRouter

**What:** `alert_state_machine.py` reads webhook URLs from `WEBHOOK_PROD`/`WEBHOOK_DEV`/`WEBHOOK_SBOX` (lines 626–628), but every other module reads `DISCORD_WEBHOOK_PROD`/`DISCORD_WEBHOOK_DEV`/`DISCORD_WEBHOOK_SBOX`. The `load_webhooks.sh` script explicitly maps `WEBHOOK_PROD` → `DISCORD_WEBHOOK_PROD`, meaning the old variable name is never exported.

**Where:** `core/alert_state_machine.py:626–628`

```python
self._webhook_urls = {
    "PROD": os.getenv("WEBHOOK_PROD", ""),
    "DEV": os.getenv("WEBHOOK_DEV", ""),
    "SBOX": os.getenv("WEBHOOK_SBOX", ""),
}
```

**Why wrong:** The `DeliveryRouter` in `alert_state_machine.py` will always return empty strings for all webhook URLs because `WEBHOOK_PROD` is not exported in the environment. The `load_webhooks.sh` script sources `.env.webhooks` (which has `WEBHOOK_PROD`) and then exports `DISCORD_WEBHOOK_PROD` — but never re-exports `WEBHOOK_PROD`. This means the `DeliveryRouter` silently fails to deliver any alerts through its route path.

**Spec to fix:** Change lines 626–628 to read from the correct env var names:

```python
self._webhook_urls = {
    "PROD": os.getenv("DISCORD_WEBHOOK_PROD", ""),
    "DEV": os.getenv("DISCORD_WEBHOOK_DEV", ""),
    "SBOX": os.getenv("DISCORD_WEBHOOK_SBOX", ""),
}
```

---

### E2 — `delivery_router.py` Uses a Third Webhook Env Var Name

**What:** `create_default_delivery_router()` in `core/delivery_router.py` reads `ALERT_WEBHOOK_URL` (line 292). This is a THIRD naming convention, different from both `WEBHOOK_PROD` (E1) and `DISCORD_WEBHOOK_PROD` (all other modules). The `HeartbeatManager` in `heartbeat.py` imports `create_default_delivery_router` from this module.

**Where:** `core/delivery_router.py:292`

```python
discord_webhook = os.getenv('ALERT_WEBHOOK_URL')
```

**Why wrong:** No script or startup file exports `ALERT_WEBHOOK_URL`. The heartbeat system, which depends on this router, will never have a Discord delivery target configured, making heartbeats go to a console-only fallback or fail silently.

**Spec to fix:** Change to read from the canonical env var name matching the instance:

```python
instance = os.getenv('PAPER_TRADING_INSTANCE', 'PROD').upper()
webhook_key = f"DISCORD_WEBHOOK_{instance}"
discord_webhook = os.getenv(webhook_key) or os.getenv('ALERT_WEBHOOK_URL')
```

---

### E3 — `load_webhooks.sh` and `load_env.sh` Duplicate Webhook Loading Logic

**What:** Both `scripts/load_webhooks.sh` and `load_env.sh` (in repo root) read `.env.webhooks` and export `DISCORD_WEBHOOK_*` variables. They use different parsing methods (`source` vs `grep+cut`) and have different error handling (`exit 1` vs `warn`).

**Where:** `scripts/load_webhooks.sh` and `load_env.sh`

**Why wrong:** Two different shell scripts doing the same job with different parsing logic creates a maintenance hazard. A bug in one (e.g., escape handling, quoting) could silently break webhook loading in environments that use the other script. The `cron_wrapper_prod_paper_trading.sh` sources `scripts/load_webhooks.sh`, while the systemd service files also source `load_webhooks.sh`. The `load_env.sh` is not used by any cron wrapper but exists as a potential alternative entry point.

**Spec to fix:** Consolidate into a single source of truth. Keep `scripts/load_webhooks.sh` as the canonical loader. Delete `load_env.sh` (or make it a thin wrapper). Add a `--dry-run` flag for debugging.

---

### E4 — Three Instance Config Files with Different Validation Logic

**What:** The `core/` directory contains THREE nearly-identical config files: `instance_config.py`, `instance_config_fixed.py`, and `instance_config_test_write.py`. All three define `INSTANCE_CONFIGS`, but they have different validation logic for webhook requirements:

- `instance_config.py` (current active): Only enforces webhooks for instances where `discord_enabled=True`, defaults to `DISCORD_ENABLED_PROD=false`, `DISCORD_ENABLED_DEV=false`, `DISCORD_ENABLED_SBOX=true`
- `instance_config_fixed.py`: Enforces webhooks for ALL three instances unconditionally, defaults to `DISCORD_ENABLED_PROD=true` for all
- `instance_config_test_write.py`: Same as `instance_config_fixed.py` but with different defaults

**Where:** `core/instance_config.py`, `core/instance_config_fixed.py`, `core/instance_config_test_write.py`

**Why wrong:** The cron scripts import `from instance_config import ...` (not `instance_config_fixed`), so the active validation logic depends on which file happens to be loaded. The `discord_enabled` defaults differ between files (`false` for PROD/DEV in `instance_config.py`, `true` for all in `instance_config_fixed.py`).

**Spec to fix:** Delete `instance_config_fixed.py` and `instance_config_test_write.py`. Keep only `instance_config.py`. Standardize defaults: `DISCORD_ENABLED_PROD=true`, `DISCORD_ENABLED_DEV=false`, `DISCORD_ENABLED_SBOX=false`.

---

### E5 — `instance_config.py` Validation Fails If DEV Webhook Not Set Even When Discord Disabled

**What:** The `validate_webhook_configuration()` function checks `discord_enabled` from env vars, but the `_initialize_with_validation()` function calls `validate_webhook_configuration()` at module import time. If `DISCORD_WEBHOOK_DEV` is not set and `DISCORD_ENABLED_DEV=false`, the validation should pass — but the `DISCORD_ENABLED_SBOX` default is `"true"` (line 148), which means SBOX webhook is always required.

**Where:** `core/instance_config.py:84–109`

**Why wrong:** The mixed defaults (PROD: false, DEV: false, SBOX: true) create an inconsistent operational model. If SBOX is run without a webhook, it crashes at import time. If someone sets `PAPER_TRADING_INSTANCE=DEV` but doesn't set `DISCORD_ENABLED_DEV=true`, alerts are silently disabled for DEV.

**Spec to fix:** Standardize defaults: `DISCORD_ENABLED_PROD=true`, `DISCORD_ENABLED_DEV=false`, `DISCORD_ENABLED_SBOX=false`. Only enforce webhook validation for instances where discord_enabled is true. Move validation out of module import time into explicit initialization.

---

### E6 — No `runtime.txt` for Python Version Pinning on Render

**What:** The repo has no `runtime.txt` file. Render uses this file to determine the Python version for the deployment. Without it, Render uses a default Python version that may change over time, potentially breaking the application.

**Where:** Missing file at repo root: `runtime.txt`

**Why wrong:** The `requirements.txt` includes `apscheduler>=3.10.4`, `xgboost>=2.0.0`, `flask==3.1.2` — these may have different behavior or compatibility across Python versions. A Render update to a newer Python runtime could break the scheduler or data processing without warning. The project uses `.venv` with Python 3.12 locally, but Render may default to 3.11 or 3.13.

**Spec to fix:** Create `runtime.txt`:
```
python-3.12.4
```

---

### E7 — `.gitignore` Excludes `*.db` Files but Startup Depends on DB Files

**What:** The `.gitignore` has `*.db` and `data/*.db` patterns, which means all SQLite databases are excluded from version control. However, the application startup in `app.py` (lines 50–53) checks for `metar_backfill.db` in a hardcoded Render path and runs a collection script if missing.

**Where:** `.gitignore` (line 3: `*.db`), `app.py` (lines 47–53)

**Why wrong:** On Render's ephemeral filesystem, the database files don't persist across deploys unless using a persistent disk. The `_refresh_metar_if_stale` function only runs on first deploy or when the DB is stale. But if the DB is missing (fresh deploy, ephemeral disk reset), the 5-minute collection timeout may not complete before the app starts serving requests. Additionally, the `paper_trading_prod.db`, `alert_state_machine.db`, and `alert_retry_queue.db` are all treated as ephemeral.

**Spec to fix:**
1. Add a Render disk mount for `/var/data/` in `render.yaml`
2. Change all DB paths to use a configurable data directory (e.g., `DATA_DIR` env var)
3. Add a bootstrap script that creates empty DBs with schemas if they don't exist
4. Document the persistence strategy in the project README

---

### E8 — Scheduler Lock File on Ephemeral Filesystem

**What:** The `InstanceLock` class in `instance_config.py` uses file-based locks (`fcntl.flock`) on a lock file in `data/.{instance}.lock`. If the lock file is on Render's ephemeral filesystem, it will be lost on restart, and the lock doesn't work across multiple Render instances.

**Where:** `core/instance_config.py:175–205`

**Why wrong:** On Render, multiple instances of the same service can be started (auto-scaling, blue-green deploy). The file-based lock only works on a shared filesystem, which doesn't exist across Render instances. This creates a race condition where two cron runs could execute simultaneously for the same instance.

**Spec to fix:** Replace file-based lock with a database-backed lock (using SQLite's `BEGIN IMMEDIATE` or a dedicated `scheduler_locks` table). Add a lock TTL so stale locks from crashed processes are automatically cleared.

---

### E9 — Systemd Service Files Use Hardcoded Absolute Paths for gaddams User

**What:** The two `.service` files (`scripts/weather-collector-dev.service`, `scripts/weather-collector-sbox.service`) use hardcoded paths under `/home/gaddams/.openclaw-next/workspace/` for the user `gaddams`, working directory, and Python interpreter.

**Where:** `scripts/weather-collector-dev.service`, `scripts/weather-collector-sbox.service`

**Why wrong:** These paths are hardcoded to a specific user's home directory and a specific repository layout. If the code moves, the user changes, or the deployment is on a different machine, the services will fail. The `ExecStartPre` sourcing `load_webhooks.sh` also uses a hardcoded path.

**Spec to fix:** Replace hardcoded paths with environment variables or a config file. Use `EnvironmentFile=/etc/weather-engine/env` and `WorkingDirectory=/opt/weather-engine`.

---

### E10 — `heartbeat.py` Uses `gc.get_objects()` for Runtime Discovery

**What:** The `HeartbeatManager.get_scheduler_status()` and `get_last_trade_timestamp()` methods iterate over all Python objects via `gc.get_objects()` to find `MetarMonitor` and `PaperTradingEngine` instances.

**Where:** `core/heartbeat.py:100–112`, `core/heartbeat.py:209–227`

**Why wrong:** `gc.get_objects()` is a debugging tool, not a production discovery mechanism. It iterates over every Python object in the heap, which is O(n) in total objects and can be extremely slow on a long-running process. It has no ordering guarantees — if there are multiple instances of the target class, it arbitrarily picks one.

**Spec to fix:** Replace with explicit dependency injection. Pass the instances to the `HeartbeatManager` constructor.

---

### E11 — `heartbeat.py` Uses Fragile File Reading for Trade Log Tail

**What:** `HeartbeatManager.get_last_trade_timestamp()` reads the last line of a JSONL file by seeking from the end. The logic has a bug: it seeks to the second-to-last character, then reads forward one byte looking for a newline. If the file is empty or has only one line without a trailing newline, the logic raises `OSError`.

**Where:** `core/heartbeat.py:234–250`

**Why wrong:** If the file is empty, `f.seek(-2, 2)` raises `OSError: [Errno 22] Invalid argument`. If the file has exactly one line (no trailing newline), the while loop may read past the beginning. The `except Exception` swallows all errors, making failures invisible.

**Spec to fix:** Use a simpler approach: read the file in reverse chunks, find the last `\n`, and parse the JSON line above it. Add proper error handling for empty files and malformed JSON.

---

### E12 — No Log Rotation or Log Management

**What:** The logging setup in `instance_config.py`'s `setup_instance_logger()` creates a `FileHandler` that writes to a single log file indefinitely. There is no log rotation, size limit, or archival strategy.

**Where:** `core/instance_config.py:297–320`

**Why wrong:** On a long-running system, log files grow unbounded. The PROD instance, running every 5 minutes, could generate megabytes of logs per day. Without rotation, the log file will consume all available disk space, causing the application to fail.

**Spec to fix:** Replace `FileHandler` with `RotatingFileHandler` (maxBytes=10MB, backupCount=5). Add a log retention policy: archive logs older than 30 days to compressed storage, delete logs older than 90 days.

---

### E13 — `alert_retry_queue.py` Imports from `metar_monitor` Creating a Brittle Dependency

**What:** The `_retry_delivery_batch()` function in `alert_retry_queue.py` imports `_send_alert` from `core.metar_monitor` at runtime (inside the function). This creates a tight coupling between the retry queue and the metar monitor module.

**Where:** `core/alert_retry_queue.py:160`

```python
from core.metar_monitor import _send_alert
```

**Why wrong:** If `metar_monitor.py` has import errors (e.g., missing dependencies, circular imports), the retry queue silently fails. The `_send_alert` function is named with a leading underscore, indicating it's private — importing private functions across modules is a code smell.

**Spec to fix:** Extract the webhook POST logic into a shared utility module (`core/webhook_client.py`) that both `metar_monitor.py` and `alert_retry_queue.py` can import.

---

### E14 — `alert_retry_queue.py` Has `sqlite3.connect` with `timeout=1`

**What:** Multiple SQLite connections in the retry queue use `timeout=1` (1 second timeout). This is an extremely short timeout for database operations, especially when WAL mode is not explicitly enabled.

**Where:** `core/alert_retry_queue.py` (multiple locations)

**Why wrong:** With `timeout=1`, if the database is locked (e.g., by another process or a concurrent cron run), the connection will raise `sqlite3.OperationalError: database is locked` after just 1 second. On a busy system with multiple cron jobs running simultaneously, this will cause frequent retry queue failures.

**Spec to fix:** Increase timeout to 10 seconds and enable WAL mode:

```python
conn = sqlite3.connect(db_path, timeout=10)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA busy_timeout=10000;")
```

---

### E15 — `alert_state_machine.py` Has Inconsistent Indentation

**What:** The `_ensure_schema()` method and most other methods in `alert_state_machine.py` have inconsistent indentation. PRAGMA statements like `conn.execute("PRAGMA journal_mode=WAL;")` are at the wrong indentation level (4 spaces instead of 8 spaces inside the `try:` block), which would raise `IndentationError` at runtime.

**Where:** `core/alert_state_machine.py` (lines 115–116 and ~15+ other locations)

**Why wrong:** This is a Python syntax error. If this file is imported and executed, it will raise `IndentationError`. The fact that the file exists suggests it was either never run in production or was only partially tested.

**Spec to fix:** Fix indentation consistently. The PRAGMAs should be set immediately after `conn = sqlite3.connect(...)` and before the `try:` block:

```python
conn = sqlite3.connect(self._db_path)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA busy_timeout=5000;")
try:
    ...
```

---

## IMPROVEMENTS (5 total)

### I1 — Add Structured Logging (JSON Format)

**What to change:** Replace the current plain-text logging format with structured JSON logging that includes `timestamp`, `level`, `module`, `instance`, `event_id`, and a structured `message` field.

**Where:** `core/instance_config.py:setup_instance_logger()`

**Why better:** Structured logging enables automated log parsing, alerting, and integration with log aggregation services (Datadog, Grafana Loki, AWS CloudWatch). It also enables correlation of events across instances (PROD vs DEV).

**Effort:** Medium (2–3 hours)

**Spec for implementation:** Create a `JsonFormatter` class that outputs `{"timestamp": ..., "level": ..., "module": ..., "instance": ..., "message": ...}`. Add a `logging.Filter` to inject the `instance` field into all log records.

---

### I2 — Add a Health Check Endpoint for Render's Auto-Deploy

**What to change:** Add a `/healthz` endpoint to the Flask app that returns a proper HTTP status code (200 OK or 503 Service Unavailable) for Render's health check system. The current `/health` endpoint always returns 200 even if the database is disconnected.

**Where:** `app.py` (add new route)

**Why better:** Render's auto-deploy pipeline can be configured to check the health endpoint before routing traffic. A proper health check that returns 503 when the database is missing or the scheduler is down will prevent the new version from serving traffic until it's fully ready.

**Effort:** Low (30 minutes)

**Spec for implementation:** Add `/healthz` that checks DB connectivity, scheduler status, and environment variable validity. Return 200 with `{"status": "ok"}` or 503 with `{"status": "degraded", "issues": [...]}`.

---

### I3 — Add a Pre-Start Health Bootstrap Script

**What to change:** Create a `scripts/bootstrap.sh` script that runs before the application starts. It should: create `data/` and `logs/` directories, initialize empty SQLite databases with correct schemas, validate required environment variables, source webhook configuration, and print a summary of the configuration.

**Where:** New file: `scripts/bootstrap.sh`

**Why better:** Currently, the application assumes directories exist and databases are initialized. A bootstrapping script makes the startup deterministic and provides clear error messages when configuration is missing.

**Effort:** Low (1 hour)

**Spec for implementation:** `set -euo pipefail`, create directories, source webhooks, validate env vars with `: "${VAR:?message}"` syntax, print config summary.

---

### I4 — Add a Deployment Checklist to the Makefile

**What to change:** Add a `make deploy-check` target to the `Makefile` that runs all pre-deployment checks: validate Python syntax, validate environment variables, check that `runtime.txt` exists, run self-tests, check webhook connectivity via a dry-run POST.

**Where:** `Makefile` (extend with new target)

**Why better:** A pre-deployment checklist catches common issues before they reach production. The current `make test-all` only runs domain-specific tests — it doesn't check deployment infrastructure.

**Effort:** Low (1 hour)

**Spec for implementation:** Add a `deploy-check` target that runs `python3 -m py_compile`, checks for `runtime.txt`, validates `.env.webhooks`, and runs `make test-all`.

---

### I5 — Add a `render.yaml` for Infrastructure-as-Code Deployment

**What to change:** Create a `render.yaml` file that defines the Render service configuration declaratively, including environment variables, disks, health check paths, and scaling.

**Where:** New file: `render.yaml`

**Why better:** Currently, the Render deployment is configured manually through the Render dashboard. Infrastructure-as-code ensures the deployment is reproducible and version-controlled. Hardcoded paths like `/opt/render/project/src/data` in `app.py` should be derived from the config.

**Effort:** Medium (2 hours)

**Spec for implementation:** Define a `render.yaml` with the web service type, Python env, build/start commands, health check path, env vars (including `RENDER=true`, `DATA_DIR=/var/data`), and a persistent disk mount at `/var/data`.

---

## IDEAS (3 total)

### Idea A — Unified Alert Delivery Pipeline with Health Dashboard

**The idea:** Create a single `AlertPipeline` class that consolidates all alert delivery paths (Discord, console, retry queue, state machine). Currently, `alert_dispatcher.py`, `alert_builder.py`, `alert_state_machine.py`, `alert_retry_queue.py`, `delivery_router.py`, and `heartbeat.py` all have overlapping delivery logic.

**Expected benefit:**
- Eliminates the triple webhook env var name bug (E1, E2)
- Single retry/backoff configuration
- Consistent delivery metrics (delivered, failed, pending, dead-letter counts)
- Easier to add new delivery channels (SMS, email, PagerDuty)

**Risk:** Medium — refactoring four modules into one risks introducing regressions in the alert delivery path.

**Spec for validation:**
1. Run the existing alert delivery path for 100 alerts and record delivery success rate
2. Implement the unified pipeline in a sidecar module
3. Run the same 100 alerts through the new pipeline
4. Compare: delivery rate, latency, error handling, and dead-letter behavior
5. Only migrate if the new pipeline has >=99% delivery rate and <=10% increase in latency

---

### Idea B — Proactive Monitoring with Synthetic Heartbeat Detection

**The idea:** Deploy a separate health-check service (or cron job) that sends a synthetic heartbeat to the Discord webhook every 5 minutes and verifies delivery. If the heartbeat is not received within 10 minutes, send an alert via a secondary channel (e.g., email-to-SMS, or Render's built-in notification system).

**Expected benefit:**
- Catches Discord webhook failures before users notice
- Detects network partitions, rate limiting, and webhook URL changes
- Provides an independent verification of the delivery pipeline

**Risk:** Low — the synthetic heartbeat is a separate process that doesn't affect the main engine.

**Spec for validation:**
1. Run `scripts/heartbeat_canary.py` that sends a test message to Discord every 5 minutes
2. Store the timestamp of the last successful delivery in a simple file or DB
3. A separate monitoring script checks if the last delivery was >10 minutes ago
4. If so, log to stderr and attempt to send via a fallback webhook
5. Run for 72 hours and measure: detection latency, false positive rate, delivery reliability

---

### Idea C — Database Health Monitoring with Automatic Recovery

**The idea:** Add a `DatabaseHealthMonitor` that periodically checks all SQLite databases for integrity (`PRAGMA integrity_check`), monitors file sizes, and automatically attempts recovery if corruption is detected.

**Expected benefit:**
- Prevents data loss from silent database corruption
- Auto-recovery means the engine can survive database issues
- Early detection of disk space issues (before writes fail)
- Single dashboard for all database health status

**Risk:** Low — integrity checks are read-only. The recovery process should be a separate script that requires manual approval for destructive operations.

**Spec for validation:**
1. Create `scripts/db_health_check.py` that runs `PRAGMA integrity_check` on all databases
2. Log results and expose via `/health` endpoint
3. If a database is corrupted, copy it to `data/corrupted/` and create a fresh one
4. Run for 1 week, measuring: check duration (<1s per DB), false positives, disk usage

---

## ELEPHANTS (2 total)

### Elephant 1 — Fragmented Alert Delivery Pipeline with Three Incompatible Code Paths

**What:** The codebase has THREE independent, incompatible alert delivery systems:

| System | Module | Env Var Used | State Machine | Retry Queue |
|---|---|---|---|---|
| 1. DeliveryRouter | `alert_state_machine.py` | `WEBHOOK_PROD` | Built-in | No |
| 2. AlertDispatcher | `alert_dispatcher.py` | `DISCORD_WEBHOOK_PROD` | No | No |
| 3. DeliveryRouter (new) | `delivery_router.py` | `ALERT_WEBHOOK_URL` | No | No |

System 1 reads `WEBHOOK_PROD` (which is never exported → always empty → always silent failure).
System 2 reads `DISCORD_WEBHOOK_PROD` (correct) but has no retry logic.
System 3 reads `ALERT_WEBHOOK_URL` (which is never exported → always empty → Discord target not created).

The `heartbeat.py` module uses System 3. The `multi_instance_paper_trader.py` uses System 2 and `alert_builder.py`. The `alert_state_machine.py` DeliveryRouter is not used by any cron job or startup script.

**Why matters:** This is a critical operational failure. The primary alert delivery system has three competing implementations, two of which are silently broken (System 1 reading wrong env var, System 3 reading unset env var). Operators have no way to know their alerts are not being delivered. When the engine detects a trading opportunity, the alert may be routed through a broken path, logged but never sent, or sent through a non-existent webhook URL.

**What happens if ignored:**
- Operators miss critical trading alerts (the human-in-the-loop notification path is effectively dead)
- The engine may appear to be working (trades execute, dashboard shows data) but alerts are silently dropped
- Confidence in the system erodes — operators begin ignoring or distrusting alerts
- Debugging takes hours because the failure is silent (no error logs for empty env vars)

**Spec for resolution:**
1. Audit all env var reads for webhook URLs across the entire codebase
2. Standardize on `DISCORD_WEBHOOK_PROD` / `DISCORD_WEBHOOK_DEV` / `DISCORD_WEBHOOK_SBOX` everywhere
3. Add a startup validation that logs a WARNING if no webhook URL is found for the active instance
4. Add a `/observability/delivery-chain` endpoint that shows the full delivery path
5. Delete `alert_state_machine.py`'s standalone `DeliveryRouter` class (duplicate of `delivery_router.py`)
6. Make `delivery_router.py` read from `DISCORD_WEBHOOK_*` env vars, not `ALERT_WEBHOOK_URL`

---

### Elephant 2 — No Backup, No Disaster Recovery, No Data Persistence Strategy

**What:** The entire system has:
- No automated database backup mechanism
- No disaster recovery plan
- Databases stored on ephemeral filesystem (Render) or local disk (systemd) with no backup
- `.gitignore` excludes `*.db` files, meaning databases are not version-controlled
- The `scripts/` directory has 80+ scripts but zero backup scripts
- The systemd service files have `Restart=always` but no data persistence guarantee — on restart, the ledger databases are reset

**Why matters:** The paper trading engine maintains a trade ledger, account balance, and position history in SQLite databases. If the Render instance is redeployed, scaled down, or the disk is reset, ALL trading history is lost including:
- Open positions (can't be closed = infinite risk)
- Account balance (reset to initial $10,000)
- P&L history (lost forever)
- Alert state machine history (lost)
- Retry queue state (lost)

This is a financial system with real money implications. Data loss is not just an inconvenience — it can cause incorrect position sizing, duplicate trades, and financial exposure.

**What happens if ignored:**
- A Render deploy wipes the trade ledger
- The engine restarts with $10,000 balance, unaware of open positions
- New trades are sized based on incorrect balance
- P&L tracking is reset to zero, hiding losses
- If real money ever replaces paper trading, this becomes a catastrophic financial loss event

**Spec for resolution:**
1. **Immediate:** Add a cron job that runs `scripts/backup_databases.sh` every 15 minutes:
   ```bash
   #!/bin/bash
   BACKUP_DIR="/srv/backups/weather-engine/$(date +%Y%m%d)"
   mkdir -p "$BACKUP_DIR"
   for db in data/*.db; do
       sqlite3 "$db" ".backup $BACKUP_DIR/$(basename $db).$(date +%H%M)"
   done
   find /srv/backups/weather-engine/ -type d -mtime +1 -exec rm -rf {} +
   ```
2. **Short-term:** Migrate databases to Render's persistent disk (`/var/data/`) and update all DB paths
3. **Medium-term:** Add a `--restore-from-backup` flag to the startup script that restores the latest backup before starting
4. **Long-term:** Consider PostgreSQL for the trade ledger (SQLite doesn't support concurrent writes well, which is problematic for the three-lane architecture)