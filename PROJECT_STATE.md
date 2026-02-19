Kalshi METAR Monitor — Project State Snapshot

1️⃣ Current Phase
Phase: Phase 2
Status: Public Kalshi connectivity active
Last merged PR:
Current active branch:

2️⃣ Phase 1 (Alert Engine)

Status: FROZEN
Version: 1.0
Last semantics update:

Guarantees:

- Integer floor-cross alerts only
- No unchanged repeat alerts
- Station-local 11:00–19:00 window
- Station-local daily reset
- Scheduler idempotent
- No duplicate threads
- No alert emission outside window

3️⃣ Phase 2 (Kalshi Integration)

Status: In Progress
Auth mode:

- Public-only (ACTIVE)
- RSA authenticated (DORMANT)

Endpoints implemented:

- /kalshi/ping

Scheduler integration:

None

Trading enabled:

No

4️⃣ Deployment

Render URL:
https://kalshi-metar-monitor.onrender.com

Autostart:
METAR_AUTOSTART = true

Required Environment Variables:

Phase 1:

- ALERT_WEBHOOK_URL
- METAR_STATIONS_JSON
- METAR_POLL_SECONDS

Phase 2:

- KALSHI_BASE_URL (for RSA mode)
- KALSHI_KEY_ID (for RSA mode)
- KALSHI_KEY_PEM (for RSA mode)
- KALSHI_PUBLIC_BASE_URL (optional override)

5️⃣ Governance Stack

PR Checklist enforced: Yes
Master Prompt Template enforced: Yes
Setup Script active: Yes
Operating Mode declared: Yes

Merge Rule:
No PR merges unless ChatGPT signs off with:
“Phase 1 semantics preserved.”

6️⃣ Known Constraints

- Free GitHub plan (branch protection not enforced)
- Solo-dev controlled release
- Codex container is ephemeral
- Setup script ensures origin + git identity
- All changes go through PR

7️⃣ Next Planned Work

Short-term:

- Get Kalshi API-based info
- Send basic Kalshi market state changes to Discord
- Report on relevant open trades

Mid-term:

- Logic for selective Kalshi alerts
- Actionable recommendations on what to trade or watch

Long-term:

- Automated trading
- Multi-city expansion
- Low temperature markets
- API hardening / rate-limit handling

8️⃣ Risk Notes

- Kalshi API auth not fully validated
- No trading safeguards implemented
- No order throttling logic

END OF SNAPSHOT
