# Weather Engine — Deployment & Operations

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Render (Web Service)                       │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  gunicorn (app:app) — 2 workers                        │  │
│  │  ├── /healthz — health check + DB monitor             │  │
│  │  ├── /metar/* — METAR observation endpoints           │  │
│  │  ├── /kalshi/* — Kalshi market endpoints              │  │
│  │  ├── /trading/* — Trading dashboard                   │  │
│  │  └── /debug/* — Debug/replay endpoints                │  │
│  └───────────────────────────────────────────────────────┘  │
│  Background: WeatherCollectorService (daemon thread)         │
│  Storage: /opt/render/project/src/data/*.db                  │
└─────────────────────────────────────────────────────────────┘
```

## Environments

### Production
- **URL:** https://kalshi-metar-monitor.onrender.com
- **Plan:** Standard ($7/mo)
- **Workers:** 2
- **DB:** `metar_backfill.db` (persistent disk)
- **Auto-deploy:** Enabled (from `main` branch)
- **Health check:** `/healthz` (expects 200)

### Staging
- **URL:** https://kalshi-metar-monitor-staging.onrender.com
- **Plan:** Starter (free)
- **Workers:** 1
- **DB:** `metar_backfill_staging.db` (isolated)
- **Auto-deploy:** Disabled (manual trigger)
- **KALSHI_LIVE_DOMAIN:** `staging` (no real money)

## Deployment Pipeline

### Pre-deploy check (`make deploy-check`)
```bash
make deploy-check
```
Runs:
1. Python syntax check on all `.py` files (app.py + core/)
2. Required environment variable validation
3. SQLite database connectivity
4. `render.yaml` validation
5. Webhook URL availability check

### Deploy process
1. **Merge to main** (via GitHub PR — Dan only)
2. **Render auto-deploy** triggers from `main` branch
3. **Pre-deploy command** validates station registry
4. **Bootstrap script** runs before gunicorn starts
5. **Health check** polled by Render every 5 seconds
6. **On failure:** Render rolls back automatically

### Staging deploy
```bash
make deploy-staging   # requires render CLI
```
Or manually via Render dashboard.

## Rollback Procedure

### Automatic rollback
Render auto-rolls back if:
- Health check fails 3 consecutive times
- Pre-deploy command exits non-zero
- Build command fails

### Manual rollback
1. **Render Dashboard:** Go to service → Deploys → select last working deploy → "Rollback"
2. **Git revert:** `git revert HEAD && git push origin main`
3. **CLI:** `render deploys rollback kalshi-metar-monitor --to <deploy-id>`

### Rollback checklist
- [ ] Verify health check returns 200
- [ ] Verify METAR data collection running
- [ ] Verify Kalshi connectivity OK
- [ ] Verify trading dashboard loads
- [ ] Verify webhook delivery working
- [ ] Check logs for errors

## Monitoring

### Endpoints
- `GET /healthz` — Health check (200=OK, 503=DB disconnected)
- `GET /metar/status` — Scheduler lifecycle status
- `GET /debug/state` — Full internal state

### Dashboard
- `/trading` — Trading dashboard (real-time)
- `/metar/status` — Polling statistics

### Logs
- Render dashboard: Service → Logs
- Structured JSON logging (timestamp, level, module, event_id)

## Scheduled Tasks

| Task | Schedule | Description |
|------|----------|-------------|
| METAR collection | Every 30 min | Live weather observations |
| NWP collection | Daily 06:00 UTC | GFS model forecasts |
| Kalshi polling | Every 15 min | Market price discovery |
| Forecast disagreement | Daily 07:00 ET | GFS vs NWS divergence |

## Cron Jobs (OpenClaw)

| Job | Schedule | Model | Status |
|-----|----------|-------|--------|
| NWP Forecast Collection | 0 6 * * * UTC | `qwen/qwen3-coder-plus` | OK |
| Forecast Disagreement | 0 7 * * * ET | `qwen/qwen3-coder-plus` | Retry on transient |

## Security

- **KALSHI_API_KEY / KALSHI_API_SECRET:** Render secret env vars
- **WEBHOOK_BASE_URL:** Render secret env var
- **OPENCLAW_WEBHOOK_TOKEN:** Render secret env var
- **No secrets in code** — all via environment variables
- **KALSHI_LIVE_DOMAIN=staging** in staging environment