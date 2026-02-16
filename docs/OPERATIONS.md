# Operations

## Render notes
- Render uses `gunicorn app:app -t 180`
- Service must boot without side effects / blocking calls on import.
- Scheduler starts only via `POST /metar/start` (unless you add autostart later).

## Debug endpoints
- `/debug/version` confirms which functions exist in the deployed build
- `/debug/state` shows in-memory state (may be empty before first poll)

## Discord webhook
Set `ALERT_WEBHOOK_URL` to your Discord webhook URL.
Alerts are sent via HTTP POST. Failures do not crash polling.
