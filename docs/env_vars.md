# Environment Variables

## Required for production alerting
- `ALERT_WEBHOOK_URL`
  - Destination webhook for composed ladder alerts.
  - If unset, alert send operations return a missing-webhook result and no delivery occurs.

- `ALERT_DB_PATH` (default: `/var/data/alerts.db`)
  - Durable SQLite alert audit path.
  - Persistent disk required in production.
  - Single-instance SQLite deployment model.

- `AWC_FROM_EMAIL`
- `AWC_USER_AGENT`
  - HTTP etiquette headers for weather source requests.

## Scheduler and METAR behavior
- `METAR_AUTOSTART` (default: `true`)
  - Enables one-time scheduler start through request lifecycle hooks.
  - Runtime control APIs are documented only in `docs/API_REFERENCE.md`.

- `METAR_POLL_SECONDS` (default: `60`)
  - Poll loop interval for the scheduler worker.

- `METAR_STATIONS_JSON`
  - JSON list watchlist of ICAO stations.

- `METAR_CACHE_FILE` (default: `/opt/render/project/src/data/metar_state.json`)
  - Best-effort persistent cache for state continuity.

- `METAR_DEFAULT_SOURCE` (default: `nws`)
- `METAR_STRICT` (default: `true`)
- `METAR_LOOKBACK_MIN` (default: `3`)
- `IEM_LOOKBACK_HOURS` (default: `1`)
- `TEMP_ALERT_DELTA_F`
  - Compatibility variable; transition-driven integer crossing remains authoritative.

## Alert policy
- `ALERT_ON_MISSING_LADDER`
  - Enables ladder-missing alert emission when ladder data is unavailable.

- `SUPPRESS_TEMP_ONLY_ALERTS`
  - Legacy compatibility flag.
  - Raw temp-only alerts are removed in production; transition-driven composed pathways remain.

## Kalshi weather ladder targeting
- `KALSHI_PUBLIC_BASE_URL` (default: `https://api.elections.kalshi.com/trade-api/v2`)

- `KALSHI_TARGET_STATION`
  - Restricts structured ladder monitoring to one station’s event set.
  - Station authority is market-derived in canonical operating mode.

- `KALSHI_TARGET_MARKET_TYPE`
  - Comma-separated `HIGH` and/or `LOW` filters when station targeting is enabled.
  - `HIGH,LOW` enables symmetric dual-side monitoring.

- `KALSHI_ALERT_TICKERS`
  - Optional comma-separated ticker allowlist for alert emission.
  - Filtering is applied to alert eligibility, not all internal state reads.

## Optional security/integration
- `ALERT_INGEST_SECRET`
- `HTTP_FROM_EMAIL`
- `HTTP_USER_AGENT`

## Canonical references
- API surface and endpoints: `docs/API_REFERENCE.md`
- Architecture and invariants: `docs/ARCHITECTURE.md`
- Operating governance and semantics: `docs/OPERATING_MODE.md`
- Runbooks and deployment notes: `docs/OPERATIONS.md`
- Active roadmap: `docs/ROLLING_TODO.md`
