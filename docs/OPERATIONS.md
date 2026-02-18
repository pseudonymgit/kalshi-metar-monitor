# Operations

## Runtime notes (Render)

- Start command: `gunicorn app:app -t 180`
- App must boot cleanly with no duplicate routes or import-time polling side effects.
- Scheduler is controlled explicitly via API (`/metar/start`, `/metar/stop`).

## Environment configuration

### Required

- `ALERT_WEBHOOK_URL`
- `AWC_FROM_EMAIL`
- `AWC_USER_AGENT`

### Optional (common)

- `METAR_STATIONS_JSON` (JSON list of ICAOs)
- `METAR_POLL_SECONDS` (default `60`)
- `METAR_DEFAULT_SOURCE` (default `nws`)
- `METAR_STRICT` (default `true`)
- `METAR_LOOKBACK_MIN` (default `3`)
- `METAR_CACHE_FILE` (default `/opt/render/project/src/data/metar_state.json`)
- `IEM_LOOKBACK_HOURS` (default `1`)

## Endpoint reference

### Health / Debug

- `GET /` → basic service health (`{"status":"ok"}`)
- `GET /debug/version` → module attrs + config sanity
- `GET /debug/state` → in-memory state snapshot

### METAR operations

- `GET /metar/window?icao=KDEN&minutes=3&source=nws`
- `GET /metar/latest?icao=KDEN&source=nws`
- `GET /metar/multi?icaos=KDEN,KLAX&source=nws`
- `GET /metar/watchlist`
- `POST /metar/watchlist` with `{"icaos":["KDEN","KLAX"]}`
- `GET /metar/metrics`
- `POST /metar/start`
- `POST /metar/stop`
- `POST /metar/force-poll`
- `POST /metar/test-alert`

## Basic curl checks

Set base URL:

```bash
BASE="https://<your-render-service>.onrender.com"
```

### Boot and debug

```bash
curl -s "$BASE/"
curl -s "$BASE/debug/version"
curl -s "$BASE/debug/state"
```

### Fetch flows

```bash
curl -s "$BASE/metar/latest?icao=KDEN&source=nws"
curl -s "$BASE/metar/window?icao=KDEN&minutes=3&source=nws"
curl -s "$BASE/metar/multi?icaos=KDEN,KLAX&source=nws"
```

### Scheduler control

```bash
curl -s -X POST "$BASE/metar/start"
curl -s -X POST "$BASE/metar/force-poll"
curl -s "$BASE/metar/metrics"
curl -s -X POST "$BASE/metar/stop"
```

### Alert plumbing

```bash
curl -s -X POST "$BASE/metar/test-alert"
```

Expected result: `{"ok":true,"sent":true}` and a Discord/webhook message.

## Operational expectations

- Alerts fire only on integer floor crosses (up or down) during station-local `11:00–19:00`.
- Daily reset is station-local date based.
- Cache persistence preserves `last_alert_floor` and `last_reset_date_local` across restart.

## GitHub remote connectivity (Codex / local automation)

If you run changes from a container or ephemeral workspace, verify the checkout is attached to
the actual GitHub repo before opening PRs.

```bash
git remote -v
```

If no `origin` exists, add one:

```bash
git remote add origin https://github.com/<owner>/kalshi-metar-monitor.git
```

If `origin` exists but is wrong, fix it:

```bash
git remote set-url origin https://github.com/<owner>/kalshi-metar-monitor.git
```

Then verify connectivity and auth:

```bash
git ls-remote origin HEAD
```


If this fails with authentication errors, provide a valid GitHub token/credential helper in the
runtime before attempting `git push` or PR creation.

### Important: local PR tooling vs real GitHub PRs

- Local automation helpers (including Codex `make_pr`) can record PR metadata, but they do **not**
  guarantee a PR exists on GitHub.
- A PR appears on GitHub only after a successful push to a remote branch and PR creation via the
  GitHub API/UI (for example `gh pr create` or the web UI).

Recommended verification sequence:

```bash
git remote -v
git ls-remote origin HEAD
git push -u origin <your-branch>
```

Then create/verify the PR with one of:

```bash
gh pr create --fill
gh pr view --web
```

or open `https://github.com/<owner>/kalshi-metar-monitor/pulls` in a browser to confirm the PR is
actually present.
