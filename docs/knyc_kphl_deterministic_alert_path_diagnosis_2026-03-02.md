# Deterministic Alert-Path Diagnosis — KNYC / KPHL (2026-03-02)

## Runtime authority checks executed

- Production base queried: `https://kalshi-metar-monitor.onrender.com`
- Required surfaces attempted:
  - `/metar/status`
  - `/observability/station-summary?station=KNYC|KPHL`
  - `/observability/current-epochs?station=KNYC|KPHL`
  - `/observability/day-structure?station=KNYC|KPHL`
  - `/observability/transitions?station=KNYC|KPHL`
  - `/observability/hydration-health?station=KNYC|KPHL`
  - `/observability/alert-fire-audit?station=KNYC|KPHL`
  - `/observability/recent-alerts?station=KNYC|KPHL`
  - `/observability/scheduler-health`
- Deterministic runtime result for every request: proxy tunnel denial
  - `Tunnel connection failed: 403 Forbidden`
  - `ProxyError(MaxRetryError(...))`

DB authority check:
- `ALERT_DB_PATH=/var/data/alerts.db` checked.
- Result: file absent in this environment.

## Station: KNYC

| Stage | Status |
|---|---|
| Stage 1 — Observation acceptance | ? UNKNOWN |
| Stage 2 — Bucket evolution | ? UNKNOWN |
| Stage 3 — Transition emission | ? UNKNOWN |
| Stage 4 — Hydration readiness | ? UNKNOWN |
| Stage 5 — Market evaluation | ? UNKNOWN |
| Stage 6 — Alert decision | ? UNKNOWN |
| Stage 7 — Alert delivery | ? UNKNOWN |

First stopping stage (deterministic): **not observable from available runtime authorities**.

## Station: KPHL

| Stage | Status |
|---|---|
| Stage 1 — Observation acceptance | ? UNKNOWN |
| Stage 2 — Bucket evolution | ? UNKNOWN |
| Stage 3 — Transition emission | ? UNKNOWN |
| Stage 4 — Hydration readiness | ? UNKNOWN |
| Stage 5 — Market evaluation | ? UNKNOWN |
| Stage 6 — Alert decision | ? UNKNOWN |
| Stage 7 — Alert delivery | ? UNKNOWN |

First stopping stage (deterministic): **not observable from available runtime authorities**.
