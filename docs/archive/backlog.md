ARCHIVED — superseded; not current requirements.


## Phase 1 (METAR monitor) — remaining / polish
- [ ] Integer bucket crossing alerts (up + down) instead of delta-based float threshold
- [ ] Station-local time gating (11:00–19:00 local) for alerts
- [ ] Overnight reset per station (local midnight)
- [ ] Add explicit station timezone map (config file or env JSON)
- [ ] Reduce noise: only alert on bucket transitions, not repeated
- [ ] Add an endpoint to show station-local time + current bucket for debugging

## Phase 2 (Kalshi read-only monitoring)
- [ ] Kalshi auth (read-only)
- [ ] Market watchlist config
- [ ] Poll market data + post snapshots to Discord
- [ ] No trading / no orders

## Phase 3 (TBD)
- kept intentionally undefined
