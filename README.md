# Kalshi METAR Monitor

Operational Flask service with two tracks:
- **Phase 1 (frozen):** METAR monitoring and temperature alerting.
- **Phase 2 (manual mode):** Kalshi public market monitoring via on-demand HTTP trigger.

## Project Scope

### Phase 1 — METAR Monitoring (Frozen v1.0)
Phase 1 is immutable at version **1.0** unless explicitly version-bumped.

Current enforced semantics:
- Integer floor-cross alerts.
- Station-local alert window enforcement (11:00–19:00 local time).
- Station-local daily reset of alert memory.
- Scheduler isolation (METAR scheduler behavior is independent from Kalshi checks).
- No drift policy for polling semantics.

### Phase 2 — Kalshi Monitoring (Manual)
Phase 2 currently runs in **public API mode only**.

Current constraints:
- Uses unauthenticated public Kalshi endpoint data.
- Manual trigger endpoint: `POST /kalshi/check`.
- First-run suppression: initial snapshot seeds memory and emits zero change alerts.
- No scheduler integration.
- No trading actions.
- No rate limiting yet.

## Deployment

- Production Render URL: `https://kalshi-metar-monitor.onrender.com`
- Start command: `gunicorn app:app -t 180`
- `METAR_AUTOSTART` behavior:
  - `true` (default): METAR scheduler starts at process boot.
  - `false`: scheduler stays stopped until `POST /metar/start`.

## API Surface

### Phase 1 (`/metar/*`)
- `GET /metar/window` — ingest a strict-source window for one station and return latest-known observation.
- `GET /metar/latest` — latest observation for one station.
- `GET /metar/multi` — on-demand fetch for multiple stations.
- `GET /metar/watchlist` — read active watchlist.
- `POST /metar/watchlist` — replace active watchlist.
- `GET /metar/metrics` — poll/timeout counters and monitoring metrics.
- `GET /metar/status` — scheduler running status and key counters.
- `POST /metar/start` — start METAR scheduler.
- `POST /metar/stop` — stop METAR scheduler.
- `POST /metar/test-alert` — emit a synthetic webhook alert payload.
- `POST /metar/force-poll` — execute one immediate poll cycle.

### Phase 2 (`/kalshi/*`)
- `GET /kalshi/ping` — checks public Kalshi reachability.
- `GET /kalshi/markets` — fetches public markets (supports `limit` query param).
- `POST /kalshi/check` — manual change-detection pass against current in-memory baseline.

## Environment Variables

### Phase 1
- `METAR_STATIONS_JSON`
- `METAR_POLL_SECONDS`
- `TEMP_ALERT_DELTA_F` (retained for compatibility)
- `METAR_CACHE_FILE`
- `METAR_AUTOSTART`
- `METAR_DEFAULT_SOURCE`
- `METAR_STRICT`
- `METAR_LOOKBACK_MIN`
- `IEM_LOOKBACK_HOURS`
- `ALERT_WEBHOOK_URL`
- `ALERT_INGEST_SECRET`

### HTTP etiquette
- `AWC_FROM_EMAIL`
- `AWC_USER_AGENT`
- `HTTP_FROM_EMAIL`
- `HTTP_USER_AGENT`

### Kalshi public
- `KALSHI_PUBLIC_BASE_URL` (default: `https://api.elections.kalshi.com/trade-api/v2`)
- `KALSHI_ALERT_TICKERS` — Optional comma-separated ticker allowlist. If unset, all markets are eligible for alerts. Filtering affects alert emission only, not internal state tracking.

### Kalshi RSA (dormant)
- `KALSHI_BASE_URL`
- `KALSHI_KEY_ID`
- `KALSHI_PRIVATE_KEY_PEM`

## Governance

- **PR workflow:** all changes are branch-based and merged through Pull Request; no direct commits to `main`.
- **Master template enforcement:** `docs/CODEX_MASTER_TEMPLATE.md` rules are mandatory and remain in force.
- **Merge requirement:** PR review must include the explicit sign-off phrase: **“Phase 1 semantics preserved.”**
- **Branch discipline:** create feature/fix/phase branches from updated `main`; keep diffs minimal and scoped.

## Operational Notes

- This repository is intentionally conservative: stability and deterministic behavior are prioritized over feature expansion.
- README scope is operational-only; no roadmap commitments are made here.
