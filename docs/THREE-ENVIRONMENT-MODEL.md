# Three-Environment Model (weather-engine-source)

**Last Updated:** 2026-07-08

## Environments

| Environment   | Short Name | Purpose                          | Data Freshness          | Alerts | Risk Guardrails |
|---------------|------------|----------------------------------|-------------------------|--------|-----------------|
| Production    | prod       | Live trading signals             | 4–6 hours (host cron)   | Yes    | Active (B1.5)   |
| Sandbox       | sbox       | Testing & validation             | Host cron (4–6h)        | Yes    | Active          |
| Development   | dev        | Development & debugging          | Local / host cron       | Yes    | Active          |

## Data Update Cadence

- Host cron (`update_weather_data.sh`) runs every 6 hours at :00.
- Updates `metar_backfill.db` and `nwp_forecasts.db`.
- Commits and pushes to `main` → triggers Render auto-deploy for prod.
- Sandbox and dev should either pull the latest committed DBs or run the collector locally.

## Alert Delivery

- All three environments (`prod`, `sbox`, `dev`) are allowed to send alerts.
- Webhook URL is controlled via the `ALERT_WEBHOOK_URL` environment variable per environment.
- Execution domain is normalized in `core/kalshi_monitor.py`:
  - "production" → "prod"
  - "sandbox" → "sbox"
  - "development" / "dev" → "dev"
- `_send_alert()` in `core/metar_monitor.py` allows the three normalized domains.
- Alerts use the retry queue + dead-letter handling in `core/alert_retry_queue.py`.

## Risk Guardrails (B1.5)

Active in all three environments:
- `max_daily_loss = 300`
- `max_drawdown_pct = 10.0`
- `consecutive_loss_limit = 5` (production target; relaxed only for tuning)

## Current Limitations

- Single Render disk mounted at `/var/data` (used primarily for `alerts.db`).
- Weather DBs (`metar_backfill.db`, `nwp_forecasts.db`) are committed to Git.
- Freshness on prod is limited to the 4–6h host cron + Render deploy time.

## Future Options (on backlog)

- Dedicated data disk for weather DBs
- Move heavy tables to Render Postgres
- Reevaluate cloud hosting provider

## Test Commands

```bash
# From host or container
python3 scripts/send_test_alert.py --webhook "$ALERT_WEBHOOK_URL" --domain prod
python3 scripts/send_test_alert.py --webhook "$ALERT_WEBHOOK_URL" --domain sbox
python3 scripts/send_test_alert.py --webhook "$ALERT_WEBHOOK_URL" --domain dev
```
