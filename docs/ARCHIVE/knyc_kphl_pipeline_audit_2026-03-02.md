# KNYC/KPHL Deterministic Alert Pipeline Audit (2026-03-02)

## Scope
Requested audit: deterministic execution-path diagnosis for missing alerts on KNYC and KPHL.

## Mandatory data source attempted
Production observability endpoints at `https://kalshi-metar-monitor.onrender.com` were queried for:
- station summary
- current epochs
- day structure
- alert diagnostics
- transitions
- alert fire audit
- debug alerts

All requests failed with:

- `curl: (56) CONNECT tunnel failed, response 403`
- `HTTP/1.1 403 Forbidden` (envoy)

## Deterministic conclusion
A station-specific root-cause diagnosis cannot be completed from repository-only artifacts because no runtime SQLite state (`alerts.db`) or production endpoint payloads are available in this environment.

Without one of the following evidence sources, execution truth for KNYC/KPHL cannot be established:
1. Production `/observability/*` payloads for each station for the affected period.
2. Production `alerts.db` containing `metar_observations`, `transition_events`, and `alerts` rows for the affected period.

## Single stop stage identified in this environment
Pipeline tracing stopped at **Stage 0: evidence acquisition** (outside the station alert pipeline) due hard 403 network denial to production observability endpoints and absence of local persisted runtime DB.
