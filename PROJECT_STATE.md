Kalshi METAR Monitor — Project State Snapshot
1️⃣ Current Phase

Phase: Phase 2
Status: Public Kalshi connectivity active
Last merged PR: Public markets endpoint live
Current active branch: main

2️⃣ Phase 1 (Alert Engine)

Status: FROZEN
Version: 1.0
Tag: phase1-final

Guarantees:

Integer floor-cross alerts only

No unchanged repeat alerts

Station-local 24/7 operation (11:00–19:00 window removed)

Station-local daily reset

Scheduler idempotent

No duplicate threads

Alert emission 24/7 (no time window restriction)

3️⃣ Phase 2 (Kalshi Integration)

Status: In Progress

Auth mode:

Public-only (ACTIVE)

RSA authenticated (DORMANT)

Endpoints implemented:

/kalshi/ping

/kalshi/markets

Scheduler integration:

None

Trading enabled:

No

4️⃣ Deployment

Render URL:
https://kalshi-metar-monitor.onrender.com

Autostart:
METAR_AUTOSTART = true

5️⃣ Live API Surface (Production)
Core Health

/
Health check endpoint.

/debug/version
Diagnostic info for metar_monitor module.

/debug/state
Returns current in-memory Phase 1 state.

Phase 1 — METAR Engine

/metar/window

/metar/latest

/metar/multi

/metar/watchlist

/metar/metrics

/metar/status

/metar/start

/metar/stop

/metar/test-alert

/metar/force-poll

All above endpoints are considered part of Phase 1 and must not change semantics without version bump.

Phase 2 — Kalshi

/kalshi/ping

/kalshi/markets

Public mode only. No trading.




6️⃣ Environment Variables (Production)
Phase 1 Alert Controls

ALERT_HOURS_ET

ALERT_INGEST_SECRET

ALERT_MIN_BUMP_F

ALERT_MIN_INTERVAL_SEC

ALERT_ON_INTEGER_CROSS

ALERT_TIMEZONE

ALERT_WEBHOOK_URL

TEMP_ALERT_DELTA_F

METAR / Source Controls

ASOS_RECENT_MINUTES

IEM_LOOKBACK_HOURS

METAR_LOOKBACK_MIN

METAR_DEFAULT_SOURCE

METAR_STRICT

METAR_CACHE_FILE

METAR_POLL_SECONDS

METAR_STATIONS_JSON

HTTP Etiquette

AWC_FROM_EMAIL

AWC_USER_AGENT

HTTP_FROM_EMAIL

HTTP_USER_AGENT

Kalshi (Phase 2)

KALSHI_PUBLIC_BASE_URL (ACTIVE)

KALSHI_BASE_URL (RSA mode only)

KALSHI_KEY_ID (RSA mode only)

KALSHI_PRIVATE_KEY_PEM (RSA mode only)

7️⃣  Governance Stack

PR Checklist enforced: Yes
Master Prompt Template enforced: Yes
Setup Script active: Yes
Operating Mode declared: Yes

Merge Rule:

No PR merges unless ChatGPT signs off with:

“Phase 1 semantics preserved.”

8️⃣ Next Planned Work
Immediate (Phase 2.1)

Detect Kalshi market state changes

Send Kalshi state changes to Discord

Short-term

Report relevant open trades

Add selective alert logic

Mid-term

Trade recommendation layer

Long-term

Automated trading

Multi-city expansion

Low temperature markets

API hardening / rate-limit handling

9️⃣ Risk Notes

Kalshi public API not rate-limited yet

No market state persistence

No change-diff tracking yet

No trading safeguards implemented

No order throttling logic

RSA auth unvalidated in production

END OF SNAPSHOT

10️⃣ Latest Task Result (2026-03-04)
Task reviewed: `docs: add unittest testing guide` (just-completed, not merged on `main`).
Result: REVISE.
Reason: added root `testing.md` duplicates existing `docs/testing.md` and introduces canonical-doc location ambiguity.

Current next action:
- Consolidate testing guidance into `docs/testing.md` as single source of truth and point `README.md` there.

New risks discovered:
- Documentation drift risk from duplicate testing guides in different paths.
- Index/governance mismatch risk if canonical docs are split between root and `docs/` without explicit policy.

