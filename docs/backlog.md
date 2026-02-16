# Backlog (Not in Current Phase)

## Rules
- Items in this file are explicitly OUT OF SCOPE for the current phase.
- Only pull an item into scope when we start a new phase doc (phase_02_*.md, phase_03_*.md).
- Exceptions allowed mid-phase: deploy-blockers, security issues, or correctness bugs that break Phase 1 acceptance criteria.

---

## P0 — Must do next (Phase 2 candidates)
- Persist full observation stream (minute-level when available) to storage (DB or Sheets)
- High-of-day tracking + “spike detection” reporting (capture brief 1-minute highs)
- Rate limiting + backoff strategy for NWS requests (avoid bans / handle outages)
- Per-station local time zones (explicit mapping or derived from station metadata)

## P1 — Valuable
- Admin UI endpoint to show last N observations per station
- Alert “cooldown” controls to avoid spam during oscillations
- Structured logging (JSON logs) and log retention strategy

## P2 — Later (Phase 3 candidates)
- Forecast sources, bias calculations, time-of-day bias profiles
- Backfill pipelines for history windows
- Kalshi market mapping, automation, trading strategy layer
