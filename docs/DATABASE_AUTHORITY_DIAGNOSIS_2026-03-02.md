# DATABASE AUTHORITY DIAGNOSIS — 2026-03-02

## Scope and evidence

This diagnosis is based on repository source and documented runtime conventions.
No speculative runtime reconstruction is used.

## SQLite connection initialization points

All SQLite opens in production code read `ALERT_DB_PATH` and default to `/var/data/alerts.db`:

- `core/metar_monitor.py`:
  - `_alert_db_path()` returns `os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")`.
  - Write paths: `transition_events` and `alerts` tables.
- `core/settlement_epoch_logger.py`:
  - `_alert_db_path()` returns `os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")`.
  - Write path: `settlement_epochs` table.
- `core/observability.py`, `core/kalshi_monitor.py`, and read helpers in `app.py`:
  - Read-only SQLite opens against the same `ALERT_DB_PATH` resolution.

## Path resolution details

### Database filename

Authoritative SQLite filename is `alerts.db` by default, with full default path:

- `/var/data/alerts.db`

### Absolute vs relative behavior

- Default is absolute (`/var/data/alerts.db`).
- If `ALERT_DB_PATH` is set to a relative value, code does **not** normalize it to absolute; SQLite resolves relative to process working directory.

### Environment-variable override

- `ALERT_DB_PATH` is the sole path override for SQLite persistence.

### Temporary-directory usage

- No runtime SQLite path points to `/tmp/*` in production code.
- No production SQLite initializer references `alerts_export.db`.

## Final resolved runtime path by persistence domain

- Ingestion persistence: **not persisted to SQLite** in current codepath; ingestion state is persisted via JSON cache file `METAR_CACHE_FILE` (default `/opt/render/project/src/data/metar_state.json`).
- Transition persistence: SQLite at `ALERT_DB_PATH` (default `/var/data/alerts.db`), table `transition_events`.
- Alert persistence: SQLite at `ALERT_DB_PATH` (default `/var/data/alerts.db`), table `alerts`.
- Settlement transition persistence: SQLite at `ALERT_DB_PATH` (default `/var/data/alerts.db`), table `settlement_epochs`.

## Render-specific filesystem analysis

- Resolved default DB path is `/var/data/alerts.db` (absolute path, not `/tmp`, not working-directory relative).
- Repository operating docs define this path as durable and require a persistent disk for production.
- Therefore, persistence authority is `/var/data/alerts.db` on the Render persistent disk mount expected at `/var/data`.

## Required output

- ✓ actual runtime DB path: `/var/data/alerts.db` (unless explicitly overridden by `ALERT_DB_PATH` at deploy runtime).
- ✓ whether filesystem is persistent: intended persistent storage at `/var/data` per production docs.
- ✓ whether restart destroys state: **No**, if `/var/data` is mounted to Render persistent disk as required; **Yes** only if deployment violates that requirement.
- ✓ reason exported `alerts_export.db` was empty: exported artifact is not the authoritative runtime DB path and was a zero-byte local file.
- ✓ single deterministic root cause: export targeted the wrong file (`alerts_export.db`) instead of the configured runtime DB (`ALERT_DB_PATH` → `/var/data/alerts.db`).
- ✓ minimal safe correction: export/copy the DB from `ALERT_DB_PATH` (production default `/var/data/alerts.db`) and stop using ad-hoc `alerts_export.db` in working directory.
